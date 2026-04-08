from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from . import constants
from .archive import find_quarto_command, is_prerendered
from .branches import (
    BranchSpec,
    ManifestEntry,
    branch_to_key,
    load_manifest,
    read_branches_list,
    save_manifest,
)
from .cache import (
    cache_branch_exists,
    content_hash,
    ensure_local_cache_branch,
    load_cache_manifest,
    restore_cached_files,
)
from .constants import PRERENDER_DIR_NAME, PRERENDER_MANIFEST_NAME
from .git_utils import (
    fetch_origin,
    managed_worktree,
    ref_exists,
    rev_parse,
)
from .quarto_config import (
    collect_exported_relpaths,
    derive_section_title,
    expand_nav_globs,
    extract_nav_structure,
    filter_nav_missing,
    load_quarto_config,
)

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    branch: str
    branch_key: str
    title: str
    status: Literal["ok", "fallback", "broken", "skipped"]
    head_sha: str | None
    last_good_sha: str | None
    built_at: str
    exported_relpaths: list[str]
    exported_dest_paths: list[Path]
    nav_structure: Any = None
    prerendered: bool = False
    duration_secs: float = 0.0
    error_message: str | None = None
    # Per-page cache info: source_relpath → content_hash
    page_hashes: dict[str, str] | None = None
    # Source relpaths of pages served from cache (subset of exported_relpaths)
    cached_pages: list[str] | None = None


def _temp_worktree_name(branch_key: str, label: str) -> str:
    """Return a unique, short worktree name to avoid collisions."""
    return f"{label}-{branch_key}-{uuid4().hex[:6]}"


def inject_failure_header(
    qmd: Path,
    branch: str,
    head_sha: str | None,
    last_good_sha: str,
) -> None:
    """Inject a warning header when using fallback content."""
    text = qmd.read_text(encoding="utf-8")

    if head_sha:
        head_short = head_sha[:7] if len(head_sha) >= 7 else head_sha
        head_line = f"failed to build at latest commit `{head_short}`."
    else:
        head_line = "failed to build at its latest known HEAD (branch missing or unreachable)."

    last_good_short = last_good_sha[:7] if len(last_good_sha) >= 7 else last_good_sha

    header = f"""::: callout-warning
This branch **`{branch}`** {head_line}

You are seeing content from the last known good commit **`{last_good_short}`**.
:::

"""
    qmd.write_text(header + text, encoding="utf-8")


def inject_graft_source_metadata(dest: Path, branch: str, source_rel: str) -> None:
    """Inject ``_graft-branch`` and ``_graft-source-path`` into a .qmd file's
    YAML frontmatter so that the Lua filter can fix the "View Source" link.
    """
    text = dest.read_text(encoding="utf-8")
    lines = text.split("\n")

    meta_lines = [
        f'_graft-branch: "{branch}"',
        f'_graft-source-path: "{source_rel}"',
    ]

    if lines and lines[0].strip() == "---":
        # Find the closing ---
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                for j, ml in enumerate(meta_lines):
                    lines.insert(i + j, ml)
                break
    else:
        lines = ["---"] + meta_lines + ["---"] + lines

    dest.write_text("\n".join(lines), encoding="utf-8")


def create_broken_stub(
    branch_key: str,
    branch: str,
    head_sha: str | None,
    out_dir: Path,
) -> list[Path]:
    """Create a stub page when no successful build exists."""
    msg_sha = (
        f" at commit `{head_sha[:7]}`"
        if head_sha and len(head_sha) >= 7
        else (f" at commit `{head_sha}`" if head_sha else "")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "index.qmd"
    target.write_text(
        f"""---
title: "{branch_key}"
---

::: callout-warning
This branch **`{branch}`** failed to build{msg_sha}, and there is no previous successful build recorded.

Please fix the build for branch **`{branch}`**.
:::
""",
        encoding="utf-8",
    )
    return [target]


def _convert_source_to_qmd(src: Path, dest_qmd: Path) -> None:
    """
    Convert a branch source file (non-notebook) to a .qmd file for inclusion in the main book.
    """
    dest_qmd.parent.mkdir(parents=True, exist_ok=True)

    suffix = src.suffix.lower()
    if suffix == ".qmd":
        shutil.copy2(src, dest_qmd)
        return

    if suffix in {".md", ".rmd", ".rmarkdown"}:
        quarto_cmd = find_quarto_command()
        subprocess.run(
            [*quarto_cmd, "convert", str(src), "--output", str(dest_qmd)],
            check=True,
        )
        return

    logger.warning(f"_convert_source_to_qmd called on unsupported type: {src}")


def _export_from_worktree(
    branch: str,
    branch_key: str,
    ref: str,
    worktree_name: str,
    inject_warning: bool = False,
    warn_head_sha: str | None = None,
    warn_last_good_sha: str | None = None,
    use_cache: bool = True,
) -> tuple[str, str, list[str], list[Path], Any, bool, dict[str, str] | None, list[str] | None]:
    """
    Export content from a worktree for the given ref.
    Returns: (sha, section_title, exported_relpaths, exported_dest_paths,
              nav_structure, prerendered, page_hashes, cached_pages)
    """
    try:
        with managed_worktree(ref, worktree_name) as wt_dir:
            sha = rev_parse("HEAD", cwd=wt_dir)

            project_dir = wt_dir
            cfg = load_quarto_config(project_dir)
            section_title = derive_section_title(cfg, branch)
            nav_structure = extract_nav_structure(cfg)

            # Check for pre-rendered content
            prerender_dir = wt_dir / PRERENDER_DIR_NAME
            if is_prerendered(wt_dir):
                logger.info(f"[{branch}] Using pre-rendered content from {PRERENDER_DIR_NAME}/")
                dest_dir = constants.GRAFTS_BUILD_DIR / branch_key
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                # Copy pre-rendered content (exclude the manifest JSON)
                shutil.copytree(
                    prerender_dir,
                    dest_dir,
                    ignore=shutil.ignore_patterns(PRERENDER_MANIFEST_NAME),
                )

                # Collect all exported files
                exported_relpaths = [p.relative_to(dest_dir).as_posix() for p in dest_dir.rglob("*") if p.is_file()]
                prerendered_dest_paths = [dest_dir / r for r in exported_relpaths]

                return sha, section_title, exported_relpaths, prerendered_dest_paths, nav_structure, True, None, None

            # Normal source file export
            src_relpaths = collect_exported_relpaths(project_dir, cfg)

            # Expand glob patterns (e.g. "investigations/**") in the nav
            # structure into explicit file entries so that cached pages can
            # be individually converted to href links later.
            nav_structure = expand_nav_globs(nav_structure, src_relpaths)
            # Remove entries for files that no longer exist in the graft
            nav_structure = filter_nav_missing(nav_structure, src_relpaths)

            dest_dir = constants.GRAFTS_BUILD_DIR / branch_key
            dest_dir.mkdir(parents=True, exist_ok=True)

            exported_dest_paths: list[Path] = []
            exported_relpaths_for_main: list[str] = []

            # Per-page cache tracking
            page_hashes: dict[str, str] = {}
            cached_pages_list: list[str] = []

            # Load cache manifest once if caching is enabled
            cache_manifest = None
            if use_cache and cache_branch_exists():
                cache_manifest = load_cache_manifest()

            for src_rel in src_relpaths:
                src = project_dir / src_rel
                if not src.exists():
                    logger.warning(f"[{branch}] source listed but missing: {src_rel}")
                    continue

                # Compute content hash for every page
                h = content_hash(src)
                page_hashes[src_rel] = h

                # Check cache for this page
                if cache_manifest is not None:
                    page_key = f"{branch_key}/{src_rel}"
                    cached_entry = cache_manifest.get("pages", {}).get(page_key)
                    if cached_entry and cached_entry.get("content_hash") == h:
                        output_files = cached_entry.get("output_files", [])
                        if restore_cached_files(branch_key, output_files, dest_dir):
                            cached_pages_list.append(src_rel)
                            for of in output_files:
                                exported_dest_paths.append(dest_dir / of)
                            exported_relpaths_for_main.append(src_rel)
                            logger.info(f"[{branch}] cache hit: {src_rel}")
                            continue
                        logger.warning(f"[{branch}] cache restore failed for {src_rel}, falling back to export")

                # Cache miss or no cache — export source as .qmd
                ext = src.suffix.lower()

                if ext == ".ipynb":
                    dest_rel = src_rel
                    dest = dest_dir / dest_rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                else:
                    rel_obj = Path(src_rel)
                    if rel_obj.suffix.lower() == ".qmd":
                        dest_rel = rel_obj.as_posix()
                    else:
                        dest_rel = rel_obj.with_suffix(".qmd").as_posix()

                    dest = dest_dir / dest_rel
                    _convert_source_to_qmd(src, dest)

                    inject_graft_source_metadata(dest, branch, src_rel)

                    if inject_warning and warn_last_good_sha:
                        inject_failure_header(dest, branch, warn_head_sha, warn_last_good_sha)

                exported_dest_paths.append(dest)
                exported_relpaths_for_main.append(dest_rel)

            cache_hits = len(cached_pages_list)
            cache_misses = len(page_hashes) - cache_hits
            if cache_manifest is not None:
                logger.info(f"[{branch}] cache: {cache_hits} hits, {cache_misses} misses")

            return (
                sha,
                section_title,
                exported_relpaths_for_main,
                exported_dest_paths,
                nav_structure,
                False,
                page_hashes,
                cached_pages_list,
            )
    except Exception as e:
        logger.error(f"[{branch}] Export from worktree failed: {e}", exc_info=True)
        raise


def _update_manifest_entry(
    manifest: dict[str, ManifestEntry],
    branch: str,
    branch_key: str,
    title: str,
    nav_structure: Any = None,
    last_good: str | None = None,
    now: str | None = None,
    prerendered: bool = False,
    cached_pages: list[str] | None = None,
) -> None:
    """Update a manifest entry for a branch."""
    if now is None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    entry: ManifestEntry = {
        "last_checked": now,
        "title": title,
        "branch_key": branch_key,
    }
    if nav_structure is not None:
        entry["structure"] = nav_structure
    if last_good:
        entry["last_good"] = last_good
    if prerendered:
        entry["prerendered"] = True
    if cached_pages:
        entry["cached_pages"] = cached_pages

    manifest[branch] = entry


def _create_broken_stub_and_update_manifest(
    manifest: dict[str, ManifestEntry],
    branch: str,
    branch_key: str,
    head_sha: str | None,
    update_manifest: bool,
    now: str,
) -> tuple[list[Path], list[str]]:
    """Create a broken stub and optionally update the manifest."""
    dest_dir = constants.GRAFTS_BUILD_DIR / branch_key
    exported_dest_paths = create_broken_stub(branch_key, branch, head_sha, dest_dir)
    exported_relpaths = [p.relative_to(dest_dir).as_posix() for p in exported_dest_paths]

    if update_manifest:
        _update_manifest_entry(manifest, branch, branch_key, branch, now=now)
        save_manifest(manifest)

    return exported_dest_paths, exported_relpaths


def _branch_exists(ref: str) -> bool:
    """Check if a git reference exists."""
    return ref_exists(ref)


def build_branch(
    spec: BranchSpec | str, update_manifest: bool = True, fetch: bool = True, use_cache: bool = True
) -> BuildResult:
    """
    Build a single branch into grafts__/<branch_key>/... with fallback logic.
    """
    t0 = time.monotonic()

    if isinstance(spec, str):
        spec = {"name": spec, "branch": spec, "collar": ""}  # type: ignore[assignment]

    branch = spec["branch"]
    graft_name = spec["name"]

    manifest = load_manifest()
    entry = manifest.get(branch, {})
    prev_last_good = entry.get("last_good")

    branch_key = branch_to_key(graft_name)

    # Prefer remote ref if available, otherwise fall back to local
    head_ref = f"origin/{branch}" if _branch_exists(f"origin/{branch}") else branch
    head_sha: str | None = None

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    title: str = graft_name  # default
    exported_relpaths: list[str] = []
    exported_dest_paths: list[Path] = []
    status: Literal["ok", "fallback", "broken", "skipped"]
    nav_structure: Any = None
    prerendered: bool = False
    error_message: str | None = None
    result_last_good: str | None = prev_last_good
    page_hashes: dict[str, str] | None = None
    cached_pages: list[str] | None = None

    if fetch:
        fetch_origin()
    if use_cache:
        ensure_local_cache_branch()

    # Validate branch exists before attempting build
    if not _branch_exists(head_ref):
        logger.error(f"Branch '{head_ref}' does not exist after fetch")
        error_message = f"Branch '{head_ref}' does not exist"
        # Check if we have a last_good to fall back to
        if prev_last_good and _branch_exists(prev_last_good):
            last_good_short = prev_last_good[:7] if len(prev_last_good) >= 7 else prev_last_good
            logger.info(f"Using last_good commit {last_good_short} for branch {branch}")
            try:
                (
                    sha,
                    title,
                    exported_relpaths,
                    exported_dest_paths,
                    nav_structure,
                    prerendered,
                    page_hashes,
                    cached_pages,
                ) = _export_from_worktree(
                    branch=branch,
                    branch_key=branch_key,
                    ref=prev_last_good,
                    worktree_name=_temp_worktree_name(branch_key, "lastgood"),
                    inject_warning=True,
                    warn_head_sha=None,
                    warn_last_good_sha=prev_last_good,
                    use_cache=use_cache,
                )
                status = "fallback"
                result_last_good = prev_last_good
                if update_manifest:
                    _update_manifest_entry(
                        manifest,
                        branch,
                        branch_key,
                        title,
                        nav_structure=nav_structure,
                        last_good=prev_last_good,
                        now=now,
                        prerendered=prerendered,
                        cached_pages=cached_pages,
                    )
                    save_manifest(manifest)
            except Exception as e:
                logger.error(f"[{branch}] Fallback build also failed: {e}", exc_info=True)
                error_message = f"Branch missing; fallback also failed: {e}"
                status = "broken"
                result_last_good = None
                exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                    manifest, branch, branch_key, None, update_manifest, now
                )
                title = branch
        else:
            # No branch and no fallback
            status = "broken"
            result_last_good = None
            exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                manifest, branch, branch_key, None, update_manifest, now
            )
            title = branch
    else:
        try:
            head_sha = rev_parse(head_ref)
            (
                sha,
                title,
                exported_relpaths,
                exported_dest_paths,
                nav_structure,
                prerendered,
                page_hashes,
                cached_pages,
            ) = _export_from_worktree(
                branch=branch,
                branch_key=branch_key,
                ref=head_ref,
                worktree_name=_temp_worktree_name(branch_key, "head"),
                use_cache=use_cache,
            )
            status = "ok"
            result_last_good = sha
            if update_manifest:
                _update_manifest_entry(
                    manifest,
                    branch,
                    branch_key,
                    title,
                    nav_structure=nav_structure,
                    last_good=sha,
                    now=now,
                    prerendered=prerendered,
                    cached_pages=cached_pages,
                )
                save_manifest(manifest)
        except Exception as e:
            logger.warning(f"[{branch}] HEAD build failed: {e}", exc_info=True)
            error_message = str(e)
            if prev_last_good and _branch_exists(prev_last_good):
                try:
                    (
                        sha,
                        title,
                        exported_relpaths,
                        exported_dest_paths,
                        nav_structure,
                        prerendered,
                        page_hashes,
                        cached_pages,
                    ) = _export_from_worktree(
                        branch=branch,
                        branch_key=branch_key,
                        ref=prev_last_good,
                        worktree_name=_temp_worktree_name(branch_key, "lastgood"),
                        inject_warning=True,
                        warn_head_sha=head_sha or prev_last_good,
                        warn_last_good_sha=prev_last_good,
                        use_cache=use_cache,
                    )
                    status = "fallback"
                    result_last_good = prev_last_good
                    if update_manifest:
                        _update_manifest_entry(
                            manifest,
                            branch,
                            branch_key,
                            title,
                            nav_structure=nav_structure,
                            last_good=prev_last_good,
                            now=now,
                            prerendered=prerendered,
                            cached_pages=cached_pages,
                        )
                        save_manifest(manifest)
                except Exception as fallback_err:
                    logger.error(f"[{branch}] Fallback build also failed: {fallback_err}", exc_info=True)
                    error_message = f"{e} (fallback also failed: {fallback_err})"
                    status = "broken"
                    result_last_good = None
                    exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                        manifest, branch, branch_key, head_sha, update_manifest, now
                    )
                    title = branch
            else:
                # No fallback available – stub page
                status = "broken"
                result_last_good = None
                exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                    manifest, branch, branch_key, head_sha, update_manifest, now
                )
                title = branch

    duration = time.monotonic() - t0

    return BuildResult(
        branch=branch,
        branch_key=branch_key,
        title=title,
        status=status,
        head_sha=head_sha,
        last_good_sha=result_last_good,
        built_at=now,
        exported_relpaths=exported_relpaths,
        exported_dest_paths=exported_dest_paths,
        nav_structure=nav_structure,
        prerendered=prerendered,
        duration_secs=duration,
        error_message=error_message,
        page_hashes=page_hashes,
        cached_pages=cached_pages,
    )


def resolve_head_sha(branch: str) -> str | None:
    """Get the current HEAD SHA for a branch, preferring the remote ref."""
    head_ref = f"origin/{branch}" if _branch_exists(f"origin/{branch}") else branch
    if not _branch_exists(head_ref):
        return None
    try:
        return rev_parse(head_ref)
    except Exception:
        return None


def _manifest_entry_from_result(result: BuildResult) -> ManifestEntry:
    """Create a manifest entry from a build result."""
    entry: ManifestEntry = {
        "last_checked": result.built_at,
        "title": result.title,
        "branch_key": result.branch_key,
    }
    if result.nav_structure is not None:
        entry["structure"] = result.nav_structure
    if result.last_good_sha:
        entry["last_good"] = result.last_good_sha
    if result.prerendered:
        entry["prerendered"] = True
    if result.cached_pages:
        entry["cached_pages"] = result.cached_pages
    return entry


def update_manifests(
    branches: list[BranchSpec | str] | None = None,
    update_manifest: bool = True,
    jobs: int = 1,
    only: set[str] | None = None,
    skip: set[str] | None = None,
    changed_only: bool = False,
    on_complete: Callable[[BuildResult], None] | None = None,
    use_cache: bool = True,
) -> dict[str, BuildResult]:
    """
    Build grafts and update the manifest.

    Args:
        branches: List of branch specs to build (default: from grafts.yaml)
        update_manifest: Whether to update grafts.lock
        jobs: Number of parallel workers (1 = sequential)
        only: If provided, only build grafts with these names
        skip: If provided, skip grafts with these names
        changed_only: If True, skip grafts where HEAD matches last_good
        on_complete: Callback invoked after each graft completes (thread-safe)
    """
    fetch_origin()
    if use_cache:
        ensure_local_cache_branch()
    if branches is None:
        branches_list: list[BranchSpec | str] = list(read_branches_list())
    else:
        branches_list = branches

    # Prune manifest entries for grafts no longer listed
    manifest = load_manifest()
    branch_set = {b if isinstance(b, str) else b["branch"] for b in branches_list}
    removed = [b for b in manifest.keys() if b not in branch_set]
    if removed:
        for b in removed:
            manifest.pop(b, None)
        save_manifest(manifest)

    # Apply --only / --skip filters
    filtered: list[BranchSpec | str] = []
    for spec in branches_list:
        graft_name = spec if isinstance(spec, str) else spec["name"]
        if only and graft_name not in only:
            continue
        if skip and graft_name in skip:
            continue
        filtered.append(spec)

    # Detect unchanged grafts for --changed and build the rest
    results: dict[str, BuildResult] = {}
    to_build: list[BranchSpec | str] = []

    for spec in filtered:
        branch_name = spec if isinstance(spec, str) else spec["branch"]
        graft_name = spec if isinstance(spec, str) else spec["name"]

        if changed_only:
            entry = manifest.get(branch_name, {})
            last_good = entry.get("last_good")
            if last_good:
                current_sha = resolve_head_sha(branch_name)
                if current_sha and current_sha == last_good:
                    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    result = BuildResult(
                        branch=branch_name,
                        branch_key=branch_to_key(graft_name),
                        title=entry.get("title", graft_name),
                        status="skipped",
                        head_sha=current_sha,
                        last_good_sha=last_good,
                        built_at=now,
                        exported_relpaths=[],
                        exported_dest_paths=[],
                        nav_structure=entry.get("structure"),
                        prerendered=entry.get("prerendered", False),
                    )
                    results[branch_name] = result
                    if on_complete:
                        on_complete(result)
                    continue
        to_build.append(spec)

    # Build grafts — parallel or sequential
    parallel = jobs > 1 and len(to_build) > 1

    if parallel:
        # In parallel mode, defer manifest updates to consolidation step
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {}
            for spec in to_build:
                branch_name = spec if isinstance(spec, str) else spec["branch"]
                graft_name = spec if isinstance(spec, str) else spec["name"]
                future = pool.submit(build_branch, spec, update_manifest=False, fetch=False, use_cache=use_cache)
                futures[future] = (branch_name, graft_name)

            for future in as_completed(futures):
                branch_name, graft_name = futures[future]
                try:
                    res = future.result()
                except Exception as e:
                    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    res = BuildResult(
                        branch=branch_name,
                        branch_key=branch_to_key(graft_name),
                        title=graft_name,
                        status="broken",
                        head_sha=None,
                        last_good_sha=None,
                        built_at=now,
                        exported_relpaths=[],
                        exported_dest_paths=[],
                        error_message=str(e),
                    )
                results[branch_name] = res
                if on_complete:
                    on_complete(res)

        # Consolidate manifest from parallel results
        if update_manifest:
            manifest = load_manifest()
            for b in removed:
                manifest.pop(b, None)
            for branch_name, res in results.items():
                if res.status == "skipped":
                    continue
                manifest[branch_name] = _manifest_entry_from_result(res)
            save_manifest(manifest)
    else:
        for spec in to_build:
            branch_name = spec if isinstance(spec, str) else spec["branch"]
            graft_name = spec if isinstance(spec, str) else spec["name"]
            logger.info(f"=== Building branch {branch_name} (graft '{graft_name}') ===")
            res = build_branch(spec, update_manifest=update_manifest, fetch=False, use_cache=use_cache)
            logger.info(f"  -> {res.status} ({len(res.exported_dest_paths)} files exported)")
            results[branch_name] = res
            if on_complete:
                on_complete(res)

    return results
