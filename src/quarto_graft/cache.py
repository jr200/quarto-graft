"""Render cache for faster incremental trunk builds.

Stores rendered HTML per-page on a ``_cache`` orphan branch.
Cache entries are keyed by ``sha256(exported_file_content)``.

The _cache branch always has exactly one rootless commit (no history).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pygit2

from . import constants
from .constants import CACHE_BRANCH, GRAFTS_BUILD_RELPATH

logger = logging.getLogger(__name__)

CACHE_MANIFEST_NAME = "cache-manifest.json"


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def content_hash(path: Path) -> str:
    """Return the hex sha256 digest of a file's content."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def content_hash_bytes(data: bytes) -> str:
    """Return the hex sha256 digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Low-level git helpers for the _cache branch
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _get_repo() -> pygit2.Repository:
    """Return a per-thread cached pygit2.Repository for the project root."""
    repo = getattr(_thread_local, "repo", None)
    if repo is None:
        git_dir = pygit2.discover_repository(str(constants.ROOT))
        if git_dir is None:
            raise RuntimeError(f"No git repository found at {constants.ROOT}")
        repo = pygit2.Repository(git_dir)
        _thread_local.repo = repo
    return repo


def cache_branch_exists() -> bool:
    """Return True if the local _cache branch exists."""
    repo = _get_repo()
    return CACHE_BRANCH in repo.branches.local


def ensure_local_cache_branch() -> bool:
    """Ensure a local ``_cache`` branch exists, creating from ``origin/_cache`` if needed.

    In CI (fresh checkout), ``git fetch`` populates ``origin/_cache`` but does
    not create a local branch.  This helper bridges that gap so the build can
    read cached pages.

    Returns True if the local ``_cache`` branch exists after this call.
    """
    repo = _get_repo()
    if CACHE_BRANCH in repo.branches.local:
        return True
    remote_ref = f"refs/remotes/origin/{CACHE_BRANCH}"
    if remote_ref not in repo.references:
        return False
    target_oid = repo.references[remote_ref].resolve().target
    commit = repo.get(target_oid)
    repo.branches.create(CACHE_BRANCH, commit)  # type: ignore[arg-type]
    logger.info(f"[cache] Created local '{CACHE_BRANCH}' branch from origin/{CACHE_BRANCH}")
    return True


def _get_cache_tree() -> tuple[pygit2.Repository, pygit2.Tree] | None:
    """Return (repo, tree) for the _cache branch tip, or None."""
    repo = _get_repo()
    try:
        commit = repo.revparse_single(CACHE_BRANCH)
        tree = commit.peel(pygit2.Tree)
        return repo, tree
    except (KeyError, pygit2.GitError):
        return None


def _iter_tree_blobs(
    repo: pygit2.Repository,
    tree: pygit2.Tree,
    prefix: str = "",
) -> list[tuple[str, pygit2.Oid, int]]:
    """Recursively yield (path, blob_oid, filemode) for every blob in *tree*."""
    entries: list[tuple[str, pygit2.Oid, int]] = []
    for entry in tree:
        path = f"{prefix}{entry.name}" if not prefix else f"{prefix}/{entry.name}"
        obj = repo.get(entry.id)
        if isinstance(obj, pygit2.Tree):
            entries.extend(_iter_tree_blobs(repo, obj, path))
        else:
            entries.append((path, entry.id, entry.filemode))
    return entries


# ---------------------------------------------------------------------------
# Cache manifest (stored as cache-manifest.json on the _cache branch)
# ---------------------------------------------------------------------------


def load_cache_manifest() -> dict[str, Any]:
    """Load the cache manifest from the _cache branch.

    Returns a dict like::

        {
            "version": 1,
            "pages": {
                "graft-key/page.qmd": {
                    "content_hash": "abc...",
                    "cached_at": "...",
                    "output_files": ["graft-key/page.html", ...]
                }
            }
        }
    """
    result = _get_cache_tree()
    if result is None:
        return {"version": 1, "pages": {}}
    repo, tree = result
    try:
        entry = tree[CACHE_MANIFEST_NAME]
        blob = repo.get(entry.id)
        return json.loads(blob.data)  # type: ignore[union-attr]
    except (KeyError, json.JSONDecodeError):
        return {"version": 1, "pages": {}}


# ---------------------------------------------------------------------------
# Reading cached files
# ---------------------------------------------------------------------------


def lookup_cached_page(
    branch_key: str,
    source_relpath: str,
    expected_hash: str,
) -> dict[str, Any] | None:
    """Check if *source_relpath* in *branch_key* is cached with *expected_hash*.

    Returns the manifest entry dict if found and hash matches, else None.
    """
    manifest = load_cache_manifest()
    page_key = f"{branch_key}/{source_relpath}"
    entry = manifest.get("pages", {}).get(page_key)
    if entry is None:
        return None
    if entry.get("content_hash") != expected_hash:
        return None
    return entry


def restore_cached_files(
    branch_key: str,
    output_files: list[str],
    dest_dir: Path,
) -> bool:
    """Copy cached output files from the _cache branch into *dest_dir*.

    *output_files* are paths relative to ``<branch_key>/`` on the _cache branch.
    Files are written to ``dest_dir/<output_file>``.

    Returns True if all files were restored, False on any failure.
    """
    result = _get_cache_tree()
    if result is None:
        return False
    repo, tree = result
    for relpath in output_files:
        cache_path = f"{branch_key}/{relpath}"
        try:
            entry = tree[cache_path]
            blob = repo.get(entry.id)
            dest = dest_dir / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob.data)  # type: ignore[union-attr]
        except (KeyError, pygit2.GitError):
            logger.warning(f"[cache] Failed to restore {cache_path} from cache")
            return False
    return True


# ---------------------------------------------------------------------------
# Writing / updating the cache
# ---------------------------------------------------------------------------


def _commit_rootless_tree(
    repo: pygit2.Repository,
    tree_id: pygit2.Oid,
    message: str = "cache update",
) -> pygit2.Oid:
    """Create a rootless (parentless) commit on the _cache branch from *tree_id*."""
    sig = pygit2.Signature("quarto-graft-cache", "cache@quarto-graft.local")
    commit_id = repo.create_commit(
        None,  # don't auto-update ref (fails if branch already exists with no parents)
        sig,
        sig,
        message,
        tree_id,
        [],  # no parents → rootless commit
    )
    # Force-update the branch ref to point at the new rootless commit
    repo.references.create(f"refs/heads/{CACHE_BRANCH}", commit_id, force=True)
    return commit_id


def _write_rootless_commit(
    repo: pygit2.Repository,
    index: pygit2.Index,
    message: str = "cache update",
) -> pygit2.Oid:
    """Write *index* as a rootless (parentless) commit on the _cache branch."""
    tree_id = index.write_tree(repo)
    return _commit_rootless_tree(repo, tree_id, message)


def update_cache_after_render(
    site_dir: Path,
    graft_build_states: dict[str, dict[str, Any]],
) -> int:
    """Capture newly rendered pages from *site_dir* into the _cache branch.

    *graft_build_states* maps ``branch_key`` → dict with:
        - ``page_hashes``: dict[source_relpath, content_hash]
        - ``cached_pages``: list[source_relpath]  (pages that were served from cache)

    For each page NOT in ``cached_pages``, extracts the rendered HTML from
    ``site_dir/grafts__/<branch_key>/`` and stores it on the _cache branch.

    Returns the number of newly cached pages.
    """
    repo = _get_repo()
    affected_keys = set(graft_build_states.keys())

    # Partition the existing _cache tree: only walk subtrees for affected grafts.
    # Unaffected graft subtrees are preserved by OID (no traversal needed).
    unaffected_entries: list[tuple[str, pygit2.Oid, int]] = []
    affected_blobs: dict[str, tuple[pygit2.Oid, int]] = {}

    result = _get_cache_tree()
    if result is not None:
        _, tree = result
        for te in tree:
            if te.name == CACHE_MANIFEST_NAME:
                continue
            if te.name in affected_keys:
                obj = repo.get(te.id)
                if isinstance(obj, pygit2.Tree):
                    for path, oid, mode in _iter_tree_blobs(repo, obj, te.name):
                        affected_blobs[path] = (oid, mode)
            else:
                unaffected_entries.append((te.name, te.id, te.filemode))  # type: ignore[arg-type]

    # Load existing manifest
    manifest = load_cache_manifest()
    pages = manifest.get("pages", {})
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    new_count = 0

    # Process each graft
    for branch_key, state in graft_build_states.items():
        page_hashes: dict[str, str] = state.get("page_hashes", {})
        cached_pages: list[str] = state.get("cached_pages", [])
        cached_set = set(cached_pages)

        # Determine current page set for this graft
        current_page_keys = {f"{branch_key}/{rp}" for rp in page_hashes}

        # Prune deleted pages from cache
        to_remove = []
        for page_key in list(pages.keys()):
            if page_key.startswith(f"{branch_key}/") and page_key not in current_page_keys:
                to_remove.append(page_key)
        for key in to_remove:
            # Remove output files from affected blobs BEFORE popping
            entry = pages.get(key, {})
            for of in entry.get("output_files", []):
                affected_blobs.pop(f"{branch_key}/{of}", None)
            pages.pop(key, None)

        # Cache newly rendered pages
        for source_relpath, h in page_hashes.items():
            if source_relpath in cached_set:
                continue  # Already cached, skip

            page_key = f"{branch_key}/{source_relpath}"

            # Find the rendered output in _site/
            # Source .qmd → rendered .html
            source_path = Path(source_relpath)
            html_relpath = source_path.with_suffix(".html").as_posix()
            site_graft_dir = site_dir / GRAFTS_BUILD_RELPATH / branch_key

            rendered_html = site_graft_dir / html_relpath
            if not rendered_html.exists():
                logger.warning(f"[cache] Rendered file not found: {rendered_html}")
                continue

            # Collect all output files for this page (html + assets like page_files/)
            output_files: list[str] = [html_relpath]

            # Check for associated asset directories (e.g., page_files/)
            asset_dir_name = source_path.stem + "_files"
            asset_dir = site_graft_dir / asset_dir_name
            if asset_dir.exists():
                for asset_file in asset_dir.rglob("*"):
                    if asset_file.is_file():
                        asset_rel = asset_file.relative_to(site_graft_dir).as_posix()
                        output_files.append(asset_rel)

            # Store output files as blobs
            for of in output_files:
                full_path = site_graft_dir / of
                if full_path.exists():
                    blob_id = repo.create_blob(full_path.read_bytes())
                    affected_blobs[f"{branch_key}/{of}"] = (blob_id, pygit2.GIT_FILEMODE_BLOB)

            # Update manifest entry
            pages[page_key] = {
                "content_hash": h,
                "cached_at": now,
                "output_files": output_files,
            }
            new_count += 1
            logger.info(f"[cache] Cached {page_key} ({len(output_files)} files)")

    # Write updated manifest
    manifest["pages"] = pages
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    manifest_blob_id = repo.create_blob(manifest_json)

    # Build root tree: unaffected subtrees (by OID) + rebuilt affected subtrees + manifest
    root_builder = repo.TreeBuilder()

    for name, oid, filemode in unaffected_entries:
        root_builder.insert(name, oid, filemode)

    for branch_key in affected_keys:
        prefix = f"{branch_key}/"
        graft_entries = {path[len(prefix) :]: v for path, v in affected_blobs.items() if path.startswith(prefix)}
        if graft_entries:
            idx = pygit2.Index()
            for path, (oid, mode) in sorted(graft_entries.items()):
                idx.add(pygit2.IndexEntry(path, oid, mode))  # type: ignore[arg-type]
            root_builder.insert(branch_key, idx.write_tree(repo), pygit2.GIT_FILEMODE_TREE)

    root_builder.insert(CACHE_MANIFEST_NAME, manifest_blob_id, pygit2.GIT_FILEMODE_BLOB)

    _commit_rootless_tree(repo, root_builder.write())
    logger.info(f"[cache] Updated _cache branch ({new_count} new pages cached)")
    return new_count


# ---------------------------------------------------------------------------
# Cache clearing
# ---------------------------------------------------------------------------


def clear_cache(graft_name: str | None = None, delete_remote: bool = True) -> None:
    """Delete the _cache branch (local and optionally remote) and recreate empty.

    If *graft_name* is given, only remove that graft's entries from the cache.
    """
    repo = _get_repo()

    if graft_name is not None:
        # Partial clear: remove specific graft entries
        _clear_graft_from_cache(repo, graft_name)
        return

    # Full clear: delete and recreate
    if CACHE_BRANCH in repo.branches.local:
        repo.branches.delete(CACHE_BRANCH)
        logger.info(f"[cache] Deleted local branch '{CACHE_BRANCH}'")

    if delete_remote:
        try:
            origin = repo.remotes["origin"]
            origin.push(
                [f":refs/heads/{CACHE_BRANCH}"],
                callbacks=_get_auth_callbacks(),
            )
            logger.info(f"[cache] Deleted remote branch '{CACHE_BRANCH}'")
        except (KeyError, pygit2.GitError) as e:
            logger.debug(f"[cache] Remote delete skipped: {e}")

    # Create empty _cache branch
    _create_empty_cache_branch(repo)
    logger.info(f"[cache] Recreated empty '{CACHE_BRANCH}' branch")


def _clear_graft_from_cache(repo: pygit2.Repository, graft_name: str) -> None:
    """Remove all cache entries for a specific graft."""
    from .branches import branch_to_key

    branch_key = branch_to_key(graft_name)

    result = _get_cache_tree()
    if result is None:
        return

    _, tree = result

    # Update manifest: remove entries for this graft
    manifest = load_cache_manifest()
    pages = manifest.get("pages", {})
    to_remove = [k for k in pages if k.startswith(f"{branch_key}/")]
    for k in to_remove:
        pages.pop(k, None)
    manifest["pages"] = pages

    manifest_json = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    manifest_blob_id = repo.create_blob(manifest_json)

    # Rebuild root tree: copy all top-level entries except the graft being cleared.
    # No subtree walking needed — we just skip the graft's entry entirely.
    root_builder = repo.TreeBuilder()
    for te in tree:
        if te.name == branch_key:
            continue  # drop this graft's subtree
        if te.name == CACHE_MANIFEST_NAME:
            continue  # replaced below
        root_builder.insert(te.name, te.id, te.filemode)  # type: ignore[arg-type]

    root_builder.insert(CACHE_MANIFEST_NAME, manifest_blob_id, pygit2.GIT_FILEMODE_BLOB)
    _commit_rootless_tree(repo, root_builder.write(), message=f"remove {branch_key} from cache")
    logger.info(f"[cache] Removed '{branch_key}' from cache")


def _create_empty_cache_branch(repo: pygit2.Repository) -> None:
    """Create a _cache branch with an empty manifest."""
    manifest = {"version": 1, "pages": {}}
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    blob_id = repo.create_blob(manifest_json)

    index = pygit2.Index()
    entry = pygit2.IndexEntry(CACHE_MANIFEST_NAME, blob_id, pygit2.GIT_FILEMODE_BLOB)  # type: ignore[arg-type]
    index.add(entry)
    _write_rootless_commit(repo, index, message="initialize cache")


def _get_auth_callbacks() -> pygit2.RemoteCallbacks:
    """Reuse auth callbacks from git_utils."""
    from .git_utils import _get_auth_callbacks as _auth

    return _auth()


# ---------------------------------------------------------------------------
# Navigation post-processing
# ---------------------------------------------------------------------------

# Quarto's sidebar does not nest <nav> elements — inner structure is ul/li/div/a.
# Non-greedy .*? is safe here; if Quarto ever nests <nav>, switch to html.parser.
_SIDEBAR_PATTERN = re.compile(
    r'(<nav\s[^>]*id=["\']quarto-sidebar["\'][^>]*>)(.*?)(</nav>)',
    re.DOTALL,
)

# Matches the word "active" inside a class="..." attribute value.
_CLASS_ACTIVE_RE = re.compile(r'(class="[^"]*?)\bactive\b\s*', re.DOTALL)


def _extract_sidebar(html: str) -> str | None:
    """Extract the full ``<nav id="quarto-sidebar">...</nav>`` from *html*."""
    m = _SIDEBAR_PATTERN.search(html)
    if m:
        return m.group(0)
    return None


def _replace_sidebar(html: str, fresh_sidebar: str, page_href: str) -> str:
    """Replace the sidebar in *html* with *fresh_sidebar* and set the active link."""
    # 1. Strip "active" only from class attributes (not from page titles or text).
    sidebar = _CLASS_ACTIVE_RE.sub(r"\1", fresh_sidebar)
    # Clean up any trailing whitespace left inside the class value.
    sidebar = re.sub(r'\s+"', '"', sidebar)

    # 2. Add "active" to the existing class attribute of the <a> matching page_href.
    #    Quarto renders: <a href="page.html" class="sidebar-item-text sidebar-link">
    #    We prepend "active " inside the class value to avoid a duplicate class attr.
    escaped_href = re.escape(page_href)

    # Try href-before-class attribute order (Quarto's default)
    sidebar, n = re.subn(
        rf'(<a\s[^>]*?href=["\'](?:\./)?{escaped_href}["\'][^>]*?class=")([^"]*")',
        r"\1active \2",
        sidebar,
        count=1,
    )
    if n == 0:
        # Try class-before-href attribute order
        sidebar = re.sub(
            rf'(<a\s[^>]*?class=")([^"]*"[^>]*?href=["\'](?:\./)?{escaped_href}["\'])',
            r"\1active \2",
            sidebar,
            count=1,
        )

    # 3. Replace sidebar in original HTML.  Use a lambda to avoid re.sub
    #    interpreting backslash sequences (\1, \n, etc.) in the sidebar content.
    return _SIDEBAR_PATTERN.sub(lambda _: sidebar, html, count=1)


def fix_navigation(
    site_dir: Path,
    cached_graft_keys: list[str],
    fresh_page_path: Path | None = None,
) -> int:
    """Post-process cached pages in *site_dir* to inject fresh navigation.

    Extracts the sidebar from a freshly rendered page and replaces the
    sidebar in every cached page.

    Args:
        site_dir: The ``_site/`` output directory after ``quarto render``.
        cached_graft_keys: Branch keys whose cached pages need nav fixing.
        fresh_page_path: Explicit path to a freshly rendered HTML page.
            If None, searches for ``site_dir/index.html``.

    Returns:
        Number of pages updated.
    """
    # Find a freshly rendered page to extract sidebar from
    if fresh_page_path is None:
        fresh_page_path = site_dir / "index.html"

    if not fresh_page_path.exists():
        # Try to find any .html in site_dir root
        for candidate in site_dir.glob("*.html"):
            fresh_page_path = candidate
            break

    if not fresh_page_path or not fresh_page_path.exists():
        logger.warning("[cache] No freshly rendered page found to extract navigation from")
        return 0

    fresh_html = fresh_page_path.read_text(encoding="utf-8")
    fresh_sidebar = _extract_sidebar(fresh_html)
    if fresh_sidebar is None:
        logger.warning("[cache] Could not extract sidebar from freshly rendered page")
        return 0

    updated = 0
    for branch_key in cached_graft_keys:
        graft_dir = site_dir / GRAFTS_BUILD_RELPATH / branch_key
        if not graft_dir.exists():
            continue
        for html_file in graft_dir.rglob("*.html"):
            try:
                page_html = html_file.read_text(encoding="utf-8")
                if _SIDEBAR_PATTERN.search(page_html) is None:
                    continue  # Not a full page (might be an asset)
                page_href = html_file.relative_to(site_dir).as_posix()
                fixed = _replace_sidebar(page_html, fresh_sidebar, page_href)
                if fixed != page_html:
                    html_file.write_text(fixed, encoding="utf-8")
                    updated += 1
            except Exception as e:
                logger.warning(f"[cache] Failed to fix nav in {html_file}: {e}")

    logger.info(f"[cache] Updated navigation in {updated} cached pages")
    return updated


def propagate_nav_to_cache(
    site_dir: Path,
    cached_graft_keys: list[str],
) -> int:
    """Write nav-fixed HTML from *site_dir* back to the ``_cache`` branch.

    After :func:`fix_navigation` patches cached pages in ``_site/``, this
    function propagates those changes to the ``_cache`` branch so that
    subsequent builds restore pages with up-to-date sidebars.

    Returns the number of files updated in the cache.
    """
    result = _get_cache_tree()
    if result is None:
        return 0

    repo, tree = result

    # Collect all existing blobs from the tree, keyed by path
    all_blobs: dict[str, tuple[pygit2.Oid, int]] = {}
    top_level: list[tuple[str, pygit2.Oid, int]] = []  # entries not in affected keys

    affected_set = set(cached_graft_keys)
    for te in tree:
        if te.name == CACHE_MANIFEST_NAME:
            top_level.append((te.name, te.id, te.filemode))  # type: ignore[arg-type]
            continue
        if te.name in affected_set:
            obj = repo.get(te.id)
            if isinstance(obj, pygit2.Tree):
                for path, oid, mode in _iter_tree_blobs(repo, obj, te.name):
                    all_blobs[path] = (oid, mode)
        else:
            top_level.append((te.name, te.id, te.filemode))  # type: ignore[arg-type]

    updated = 0
    for branch_key in cached_graft_keys:
        graft_dir = site_dir / GRAFTS_BUILD_RELPATH / branch_key
        if not graft_dir.exists():
            continue
        for html_file in graft_dir.rglob("*.html"):
            rel = html_file.relative_to(site_dir / GRAFTS_BUILD_RELPATH).as_posix()
            if rel not in all_blobs:
                continue  # Only update files already in the cache
            new_data = html_file.read_bytes()
            old_oid, mode = all_blobs[rel]
            # Skip if content hasn't changed
            old_blob = repo.get(old_oid)
            if old_blob is not None and old_blob.data == new_data:  # type: ignore[attr-defined]
                continue
            new_oid = repo.create_blob(new_data)
            all_blobs[rel] = (new_oid, mode)
            updated += 1

    if updated == 0:
        return 0

    # Rebuild the tree with updated blobs
    root_builder = repo.TreeBuilder()

    for name, oid, filemode in top_level:
        root_builder.insert(name, oid, filemode)

    for branch_key in affected_set:
        prefix = f"{branch_key}/"
        graft_entries = {path[len(prefix) :]: v for path, v in all_blobs.items() if path.startswith(prefix)}
        if graft_entries:
            idx = pygit2.Index()
            for path, (oid, mode) in sorted(graft_entries.items()):
                idx.add(pygit2.IndexEntry(path, oid, mode))  # type: ignore[arg-type]
            root_builder.insert(branch_key, idx.write_tree(repo), pygit2.GIT_FILEMODE_TREE)

    _commit_rootless_tree(repo, root_builder.write(), message="propagate nav fixes to cache")
    logger.info(f"[cache] Propagated navigation fixes to _cache branch ({updated} files)")
    return updated


# ---------------------------------------------------------------------------
# Search index post-processing
# ---------------------------------------------------------------------------


class _SearchContentParser(HTMLParser):
    """Extract title, section headings, and body text from a Quarto HTML page."""

    # Tags whose content should be ignored entirely
    _SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.sections: list[tuple[str, list[str]]] = []  # (heading, text_chunks)
        self._current_heading: str | None = None
        self._current_chunks: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_main = False
        self._skip_depth = 0  # nesting depth inside a skipped tag
        self._heading_depth = 0
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth > 0:
            if tag in self._SKIP_TAGS:
                self._skip_depth += 1
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "main":
            self._in_main = True
        if self._in_main and tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            # Flush previous section
            if self._current_heading is not None or self._current_chunks:
                self.sections.append((self._current_heading or "", self._current_chunks))
            self._current_heading = None
            self._current_chunks = []
            self._heading_depth += 1
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth > 0:
            if tag in self._SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self.title = " ".join(self._title_parts).strip()
        if tag == "main":
            # Flush last section
            if self._current_heading is not None or self._current_chunks:
                self.sections.append((self._current_heading or "", self._current_chunks))
            self._in_main = False
        if self._heading_depth > 0 and tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_depth -= 1
            if self._heading_depth == 0:
                self._current_heading = " ".join(self._heading_parts).strip()
                self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
        if not self._in_main:
            return
        if self._heading_depth > 0:
            self._heading_parts.append(data)
        else:
            text = data.strip()
            if text:
                self._current_chunks.append(text)


def _parse_search_content(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse an HTML page and return ``(title, [(section_heading, section_text), ...])``.

    Each section corresponds to a heading within ``<main>``.  The first
    section may have an empty heading (content before the first heading).
    """
    parser = _SearchContentParser()
    parser.feed(html)
    title = parser.title or ""
    result: list[tuple[str, str]] = []
    for heading, chunks in parser.sections:
        text = " ".join(chunks).strip()
        result.append((heading, text))
    # If no sections found, return a single empty-headed section
    if not result:
        result.append(("", ""))
    return title, result


def fix_search_index(
    site_dir: Path,
    graft_keys: list[str],
) -> int:
    """Ensure all graft pages are present in Quarto's ``search.json``.

    Quarto's search index only contains pages it rendered.  Pre-rendered
    cached pages are absent, and pages under ``dist/`` may also be
    excluded when resource patterns are active.  This function scans the
    rendered HTML files for each graft, extracts title/section/text, and
    appends any missing entries to ``search.json``.

    Entries already present in the index (by href) are skipped, so it is
    safe to call this for *all* grafts — not only those with cached pages.

    Args:
        site_dir: The ``_site/`` output directory after ``quarto render``.
        graft_keys: Branch keys whose pages should be ensured in the index.

    Returns:
        Number of search entries added.
    """
    search_json_path = site_dir / "search.json"
    if not search_json_path.exists():
        logger.warning("[cache] search.json not found in %s — skipping search fix", site_dir)
        return 0

    try:
        search_data = json.loads(search_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[cache] Failed to read search.json: %s", e)
        return 0

    # Collect hrefs already in the index so we don't double-add
    existing_hrefs = {entry.get("href") for entry in search_data if isinstance(entry, dict)}

    added = 0
    for branch_key in graft_keys:
        graft_dir = site_dir / GRAFTS_BUILD_RELPATH / branch_key
        if not graft_dir.exists():
            continue
        for html_file in graft_dir.rglob("*.html"):
            try:
                page_html = html_file.read_text(encoding="utf-8")
            except OSError:
                continue
            # Skip non-page HTML (fragments without <main>)
            if "<main" not in page_html:
                continue
            page_href = html_file.relative_to(site_dir).as_posix()
            title, sections = _parse_search_content(page_html)

            for section_heading, section_text in sections:
                # Build the href — with anchor for sub-sections
                if section_heading:
                    anchor = re.sub(r"[^\w\s-]", "", section_heading.lower())
                    anchor = re.sub(r"[\s]+", "-", anchor).strip("-")
                    entry_href = f"{page_href}#{anchor}"
                else:
                    entry_href = page_href

                if entry_href in existing_hrefs:
                    continue

                search_entry = {
                    "objectID": entry_href,
                    "href": entry_href,
                    "title": title,
                    "section": section_heading if section_heading else "",
                    "text": section_text,
                }
                search_data.append(search_entry)
                existing_hrefs.add(entry_href)
                added += 1

    if added:
        search_json_path.write_text(
            json.dumps(search_data, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("[cache] Added %d search entries for graft pages", added)

    return added


# ---------------------------------------------------------------------------
# Cache status
# ---------------------------------------------------------------------------


def cache_status() -> list[dict[str, Any]]:
    """Return a list of cache status entries for display.

    Each entry has: page_key, content_hash, cached_at, output_file_count.
    """
    manifest = load_cache_manifest()
    entries = []
    for page_key, info in sorted(manifest.get("pages", {}).items()):
        entries.append(
            {
                "page_key": page_key,
                "content_hash": info.get("content_hash", "?")[:12],
                "cached_at": info.get("cached_at", "?"),
                "output_files": len(info.get("output_files", [])),
            }
        )
    return entries
