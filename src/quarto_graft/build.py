from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, Union

from .branches import (
    ManifestEntry,
    BranchSpec,
    branch_to_key,
    load_manifest,
    read_branches_list,
    save_manifest,
)
from .git_utils import (
    fetch_origin,
    managed_worktree,
    run_git,
)
from .constants import GRAFTS_BUILD_DIR
from .quarto_config import (
    collect_exported_relpaths,
    derive_section_title,
    load_quarto_config,
)

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    branch: str
    branch_key: str
    title: str
    status: Literal["ok", "fallback", "broken"]
    head_sha: Optional[str]
    last_good_sha: Optional[str]
    built_at: str
    exported_relpaths: List[str]
    exported_dest_paths: List[Path]


def _temp_worktree_name(branch_key: str, label: str) -> str:
    """Return a unique, short worktree name to avoid collisions."""
    return f"{label}-{branch_key}-{uuid4().hex[:6]}"


def inject_failure_header(
    qmd: Path,
    branch: str,
    head_sha: Optional[str],
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


def create_broken_stub(
    branch_key: str,
    branch: str,
    head_sha: Optional[str],
    out_dir: Path,
) -> List[Path]:
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


def _find_quarto_command() -> List[str]:
    """Find the quarto command to use, checking for uv first, then falling back to quarto."""
    try:
        subprocess.run(
            ["uv", "--version"],
            check=True,
            capture_output=True,
        )
        return ["uv", "run", "quarto"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ["quarto"]


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
        quarto_cmd = _find_quarto_command()
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
    warn_head_sha: Optional[str] = None,
    warn_last_good_sha: Optional[str] = None,
) -> Tuple[str, str, List[str], List[Path]]:
    """
    Export content from a worktree for the given ref.
    """
    try:
        with managed_worktree(ref, worktree_name) as wt_dir:
            sha = run_git(["rev-parse", "HEAD"], cwd=wt_dir)

            project_dir = wt_dir
            cfg = load_quarto_config(project_dir)
            section_title = derive_section_title(cfg, branch)

            src_relpaths = collect_exported_relpaths(project_dir, cfg)

            dest_dir = GRAFTS_BUILD_DIR / branch_key
            dest_dir.mkdir(parents=True, exist_ok=True)

            exported_dest_paths: List[Path] = []
            exported_relpaths_for_main: List[str] = []

            for src_rel in src_relpaths:
                src = project_dir / src_rel
                if not src.exists():
                    logger.warning(f"[{branch}] source listed but missing: {src_rel}")
                    continue

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

                    if inject_warning and warn_last_good_sha:
                        inject_failure_header(dest, branch, warn_head_sha, warn_last_good_sha)

                exported_dest_paths.append(dest)
                exported_relpaths_for_main.append(dest_rel)

            return sha, section_title, exported_relpaths_for_main, exported_dest_paths
    except Exception as e:
        logger.error(f"[{branch}] Export from worktree failed: {e}", exc_info=True)
        raise


def _update_manifest_entry(
    manifest: Dict[str, ManifestEntry],
    branch: str,
    branch_key: str,
    title: str,
    exported_relpaths: List[str],
    last_good: Optional[str] = None,
    now: Optional[str] = None,
) -> None:
    """Update a manifest entry for a branch."""
    if now is None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    entry: ManifestEntry = {
        "last_checked": now,
        "title": title,
        "branch_key": branch_key,
        "exported": exported_relpaths,
    }
    if last_good:
        entry["last_good"] = last_good

    manifest[branch] = entry


def _create_broken_stub_and_update_manifest(
    manifest: Dict[str, ManifestEntry],
    branch: str,
    branch_key: str,
    head_sha: Optional[str],
    update_manifest: bool,
    now: str,
) -> Tuple[List[Path], List[str]]:
    """Create a broken stub and optionally update the manifest."""
    dest_dir = GRAFTS_BUILD_DIR / branch_key
    exported_dest_paths = create_broken_stub(branch_key, branch, head_sha, dest_dir)
    exported_relpaths = [p.relative_to(dest_dir).as_posix() for p in exported_dest_paths]

    if update_manifest:
        _update_manifest_entry(manifest, branch, branch_key, branch, exported_relpaths, now=now)
        save_manifest(manifest)

    return exported_dest_paths, exported_relpaths


def _branch_exists(ref: str) -> bool:
    """Check if a git reference exists."""
    try:
        run_git(["rev-parse", "--verify", ref])
        return True
    except subprocess.CalledProcessError:
        return False


def build_branch(spec: Union[BranchSpec, str], update_manifest: bool = True, fetch: bool = True) -> BuildResult:
    """
    Build a single branch into docs/grafts__/<branch_key>/... with fallback logic.
    """
    if isinstance(spec, str):
        spec = {"name": spec, "branch": spec, "local_path": spec}  # type: ignore[assignment]

    branch = spec["branch"]
    graft_name = spec["name"]
    local_path = spec.get("local_path") or graft_name

    manifest = load_manifest()
    entry = manifest.get(branch, {})
    last_good_sha = entry.get("last_good")

    branch_key = branch_to_key(local_path)
    # Prefer remote ref if available, otherwise fall back to local
    head_ref = f"origin/{branch}" if _branch_exists(f"origin/{branch}") else branch
    head_sha: Optional[str] = None

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    title: str = graft_name  # default
    exported_relpaths: List[str] = []
    exported_dest_paths: List[Path] = []
    status: Literal["ok", "fallback", "broken"]

    if fetch:
        fetch_origin()

    # Validate branch exists before attempting build
    if not _branch_exists(head_ref):
        logger.error(f"Branch '{head_ref}' does not exist after fetch")
        # Check if we have a last_good to fall back to
        if last_good_sha and _branch_exists(last_good_sha):
            last_good_short = last_good_sha[:7] if len(last_good_sha) >= 7 else last_good_sha
            logger.info(f"Using last_good commit {last_good_short} for branch {branch}")
            try:
                sha, title, exported_relpaths, exported_dest_paths = _export_from_worktree(
                    branch=branch,
                    branch_key=branch_key,
                    ref=last_good_sha,
                    worktree_name=_temp_worktree_name(branch_key, "lastgood"),
                    inject_warning=True,
                    warn_head_sha=None,
                    warn_last_good_sha=last_good_sha,
                )
                status = "fallback"
                if update_manifest:
                    _update_manifest_entry(
                        manifest, branch, branch_key, title, exported_relpaths,
                        last_good=last_good_sha, now=now
                    )
                    save_manifest(manifest)
            except Exception as e:
                logger.error(f"[{branch}] Fallback build also failed: {e}", exc_info=True)
                status = "broken"
                exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                    manifest, branch, branch_key, None, update_manifest, now
                )
                title = branch
        else:
            # No branch and no fallback
            status = "broken"
            exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                manifest, branch, branch_key, None, update_manifest, now
            )
            title = branch
    else:
        try:
            head_sha = run_git(["rev-parse", head_ref])
            sha, title, exported_relpaths, exported_dest_paths = _export_from_worktree(
                branch=branch,
                branch_key=branch_key,
                ref=head_ref,
                worktree_name=_temp_worktree_name(branch_key, "head"),
            )
            status = "ok"
            if update_manifest:
                _update_manifest_entry(
                    manifest, branch, branch_key, title, exported_relpaths,
                    last_good=sha, now=now
                )
                save_manifest(manifest)
        except Exception as e:
            logger.warning(f"[{branch}] HEAD build failed: {e}", exc_info=True)
            if last_good_sha and _branch_exists(last_good_sha):
                sha, title, exported_relpaths, exported_dest_paths = _export_from_worktree(
                    branch=branch,
                    branch_key=branch_key,
                    ref=last_good_sha,
                    worktree_name=_temp_worktree_name(branch_key, "lastgood"),
                    inject_warning=True,
                    warn_head_sha=head_sha or last_good_sha,
                    warn_last_good_sha=last_good_sha,
                )
                status = "fallback"
                if update_manifest:
                    _update_manifest_entry(
                        manifest, branch, branch_key, title, exported_relpaths,
                        last_good=last_good_sha, now=now
                    )
                    save_manifest(manifest)
            else:
                # No fallback available – stub page
                status = "broken"
                exported_dest_paths, exported_relpaths = _create_broken_stub_and_update_manifest(
                    manifest, branch, branch_key, head_sha, update_manifest, now
                )
                title = branch

    # Get last_good_sha from the already-loaded manifest (not reloading)
    last_good_sha = manifest.get(branch, {}).get("last_good")

    return BuildResult(
        branch=branch,
        branch_key=branch_key,
        title=title,
        status=status,
        head_sha=head_sha,
        last_good_sha=last_good_sha,
        built_at=now,
        exported_relpaths=exported_relpaths,
        exported_dest_paths=exported_dest_paths,
    )


def update_manifests(
    branches: Optional[List[Union[BranchSpec, str]]] = None,
    update_manifest: bool = True,
) -> Dict[str, BuildResult]:
    fetch_origin()
    if branches is None:
        branches = read_branches_list()

    # Prune manifest entries for grafts no longer listed
    manifest = load_manifest()
    branch_set = {b if isinstance(b, str) else b["branch"] for b in branches}
    removed = [b for b in manifest.keys() if b not in branch_set]
    if removed:
        for b in removed:
            manifest.pop(b, None)
        save_manifest(manifest)

    results: Dict[str, BuildResult] = {}
    for spec in branches:
        branch_name = spec if isinstance(spec, str) else spec["branch"]
        graft_name = spec if isinstance(spec, str) else spec["name"]
        logger.info(f"=== Building branch {branch_name} (graft '{graft_name}') ===")
        res = build_branch(spec, update_manifest=update_manifest, fetch=False)
        logger.info(f"  -> {res.status} ({len(res.exported_dest_paths)} files exported)")
        results[branch_name] = res
    return results
