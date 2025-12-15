from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .branches import BranchSpec, branch_to_key, load_manifest, read_branches_list, save_manifest
from .constants import (
    GRAFT_COLLAR_MARKER,
    QUARTO_CONFIG_YAML,
    QUARTO_PROJECT_YAML,
    YAML_AUTOGEN_MARKER,
)
from .file_utils import atomic_write_yaml
from .yaml_utils import get_yaml_loader

logger = logging.getLogger(__name__)

# Source formats we are willing to import from grafts
SUPPORTED_SOURCE_EXTS = {
    ".qmd",
    ".md",
    ".rmd",
    ".rmarkdown",
    ".ipynb",
}

def load_quarto_config(docs_dir: Path) -> dict[str, Any]:
    """Load Quarto configuration from docs directory."""
    qfile_yaml = docs_dir / QUARTO_CONFIG_YAML
    if qfile_yaml.exists():
        cfg_path = qfile_yaml
    else:
        raise RuntimeError(f"No {QUARTO_CONFIG_YAML} found in {docs_dir}")
    yaml_loader = get_yaml_loader()
    return yaml_loader.load(cfg_path.read_text(encoding="utf-8")) or {}


def list_available_collars(config_path: Path | None = None) -> list[str]:
    """
    List all available collar attachment points defined in the trunk _quarto.yaml.

    Searches for _GRAFT_COLLAR markers in the sidebar/chapters structure.
    Returns a list of collar names (e.g., ['main', 'notes', 'bugs']).
    """
    config_path = config_path or QUARTO_PROJECT_YAML
    if not config_path.exists():
        raise RuntimeError(f"No {QUARTO_CONFIG_YAML} found at {config_path}")

    yaml_loader = get_yaml_loader()
    config = yaml_loader.load(config_path.read_text(encoding="utf-8")) or {}

    collars: list[str] = []

    def find_collars(node: Any) -> None:
        """Recursively search for _GRAFT_COLLAR markers."""
        if isinstance(node, dict):
            # Check if this dict has a _GRAFT_COLLAR key
            if GRAFT_COLLAR_MARKER in node:
                collar_name = node[GRAFT_COLLAR_MARKER]
                if isinstance(collar_name, str) and collar_name not in collars:
                    collars.append(collar_name)
            # Recurse into dict values
            for value in node.values():
                find_collars(value)
        elif isinstance(node, list):
            # Recurse into list items
            for item in node:
                find_collars(item)

    # Search in website.sidebar.contents
    sidebar_contents = config.get("website", {}).get("sidebar", {}).get("contents", [])
    find_collars(sidebar_contents)

    # Search in book.chapters
    book_chapters = config.get("book", {}).get("chapters", [])
    find_collars(book_chapters)

    return collars


def flatten_quarto_contents(entries: Any) -> list[str]:
    """
    Flatten Quarto-style contents/chapters structures into an ordered list of files.
    """
    files: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            files.append(node)
            return
        if isinstance(node, dict):
            if "file" in node and isinstance(node["file"], str):
                files.append(node["file"])
            elif "href" in node and isinstance(node["href"], str):
                files.append(node["href"])
            for key in ("contents", "chapters"):
                if key in node:
                    val = node[key]
                    # Handle both list and string values for contents/chapters
                    if isinstance(val, list):
                        for child in val:
                            walk(child)
                    elif isinstance(val, str):
                        files.append(val)

    if isinstance(entries, list):
        for e in entries:
            walk(e)

    return files


def extract_nav_structure(cfg: dict[str, Any]) -> Any:
    """
    Extract the navigation structure (sidebar or chapters) from a graft's _quarto.yaml.
    Returns the raw contents/chapters structure to be preserved in the manifest.
    """
    website = cfg.get("website") or {}
    sidebar = website.get("sidebar") or {}
    sidebar_contents = sidebar.get("contents")

    if sidebar_contents:
        return sidebar_contents

    book = cfg.get("book") or {}
    book_chapters = book.get("chapters")

    if book_chapters:
        return book_chapters

    return None


def collect_exported_relpaths(docs_dir: Path, cfg: dict[str, Any]) -> list[str]:
    """
    Determine which *source documents* to export from this branch's docs/,
    preserving the branch author's intended order as far as possible.
    """
    def _resolve_entry(entry: str) -> list[Path]:
        """
        Resolve an entry from sidebar/chapters contents.
        Handles individual files, directories, glob patterns, and "auto".
        Returns a list of matching file paths.
        """
        # Handle "auto" - include all files except index pages
        if entry.lower() == "auto":
            matches = []
            index_names = {"index.qmd", "index.md", "index.rmd", "index.rmarkdown", "index.ipynb"}
            for p in sorted(docs_dir.rglob("*"), key=lambda p: p.as_posix()):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                    continue
                # Exclude index files (home pages)
                if p.name.lower() in index_names:
                    continue
                # Exclude hidden files and special directories
                if any(part.startswith(".") for part in p.parts):
                    continue
                if any(part in {"_site", ".quarto", "__pycache__", "node_modules"} for part in p.parts):
                    continue
                matches.append(p)
            return matches

        # Check if it's a glob pattern (contains * or **)
        if "*" in entry:
            matches = []
            # Special handling for patterns ending with /** (recursive match)
            # In Python 3.12, glob("path/**") doesn't match files inside, only the directory
            # We need to use rglob or append "/*" to the pattern
            if entry.endswith("/**"):
                # Use rglob for recursive matching
                base_path = docs_dir / entry[:-3]  # Remove the "/**" suffix
                if base_path.exists() and base_path.is_dir():
                    for p in base_path.rglob("*"):
                        if p.is_file() and p.suffix.lower() in SUPPORTED_SOURCE_EXTS:
                            matches.append(p)
            else:
                # Use glob for other patterns
                for p in docs_dir.glob(entry):
                    if p.is_file() and p.suffix.lower() in SUPPORTED_SOURCE_EXTS:
                        matches.append(p)
            return sorted(matches, key=lambda p: p.as_posix())

        # Try direct path
        direct = docs_dir / entry

        # If it's a directory, find all supported files in it
        if direct.exists() and direct.is_dir():
            matches = []
            for p in sorted(direct.rglob("*"), key=lambda p: p.as_posix()):
                if p.is_file() and p.suffix.lower() in SUPPORTED_SOURCE_EXTS:
                    matches.append(p)
            return matches

        # If it's a file, return it
        if direct.exists() and direct.is_file():
            return [direct]

        # If not found directly, search recursively for exact relative match
        rel_path = Path(entry)
        for p in docs_dir.rglob(rel_path.name):
            if not p.is_file():
                continue
            try:
                # Check if the full relative path matches exactly
                if p.relative_to(docs_dir).as_posix() == entry:
                    return [p]
            except ValueError:
                continue

        return []

    project = cfg.get("project") or {}
    render_spec = project.get("render")

    website = cfg.get("website") or {}
    sidebar = website.get("sidebar") or {}
    sidebar_contents = sidebar.get("contents")

    book = cfg.get("book") or {}
    book_chapters = book.get("chapters")

    relpaths: list[str] = []

    # website.sidebar.contents: use nav order
    # Handle both string and list values for contents
    if isinstance(sidebar_contents, str):
        files_from_sidebar = [sidebar_contents]
    else:
        files_from_sidebar = flatten_quarto_contents(sidebar_contents)

    if files_from_sidebar:
        logger.debug(f"Processing sidebar contents: {files_from_sidebar}")
        for entry in files_from_sidebar:
            logger.debug(f"  Resolving entry: {entry!r}")
            paths = _resolve_entry(entry)
            logger.debug(f"    Found {len(paths)} path(s)")
            for p in paths:
                if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                    continue
                rel = p.relative_to(docs_dir).as_posix()
                logger.debug(f"    Adding: {rel}")
                if rel not in relpaths:
                    relpaths.append(rel)
        logger.debug(f"Total sidebar files: {len(relpaths)}")
        if relpaths:
            return relpaths

    # book.chapters: for branch-type "book" projects
    # Handle both string and list values for chapters
    if isinstance(book_chapters, str):
        files_from_book = [book_chapters]
    else:
        files_from_book = flatten_quarto_contents(book_chapters)

    if files_from_book:
        for entry in files_from_book:
            paths = _resolve_entry(entry)
            for p in paths:
                if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                    continue
                rel = p.relative_to(docs_dir).as_posix()
                if rel not in relpaths:
                    relpaths.append(rel)
        if relpaths:
            return relpaths

    # project.render: canonical, keep order
    if isinstance(render_spec, list) and render_spec:
        for entry in render_spec:
            if not isinstance(entry, str):
                continue
            for p in docs_dir.glob(entry):
                if p.is_dir():
                    continue
                if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                    continue
                rel = p.relative_to(docs_dir).as_posix()
                if rel not in relpaths:
                    relpaths.append(rel)
        if relpaths:
            return relpaths

    # Fallback: scan docs/ for supported sources (order not guaranteed)
    for p in sorted(docs_dir.rglob("*"), key=lambda path: path.as_posix()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
            continue
        if any(part in {".quarto", "_site"} for part in p.parts):
            continue
        rel = p.relative_to(docs_dir).as_posix()
        relpaths.append(rel)

    return relpaths


def derive_section_title(cfg: dict[str, Any], branch: str) -> str:
    """Derive the section title from Quarto configuration or use branch name."""
    website = cfg.get("website") or {}
    book = cfg.get("book") or {}
    title = website.get("title") or book.get("title") or branch
    return str(title)

def is_collar_marker(item: Any) -> bool:
    """Check if item is a collar marker (_GRAFT_COLLAR)."""
    return isinstance(item, Mapping) and GRAFT_COLLAR_MARKER in item


def _find_all_collars(seq: list[Any]) -> dict[str, tuple[list[Any], int]]:
    """
    Find all collar markers in the structure.
    Returns dict mapping collar_name -> (list_ref, index).
    """
    collars: dict[str, tuple[list[Any], int]] = {}

    def search(items: list[Any]) -> None:
        for idx, item in enumerate(items):
            if is_collar_marker(item):
                collar_name = item[GRAFT_COLLAR_MARKER]
                if isinstance(collar_name, str):
                    collars[collar_name] = (items, idx)
            if isinstance(item, Mapping):
                for key in ("contents", "chapters"):
                    child = item.get(key)
                    if isinstance(child, list):
                        search(child)

    search(seq)
    return collars


def apply_manifest() -> None:
    """
    Update _quarto.yaml to match docs/grafts__ content, using
    grafts.lock and grafts.yaml.
    """
    quarto_file = QUARTO_PROJECT_YAML
    # text = quarto_file.read_text(encoding="utf-8")

    with open(quarto_file) as fp:
        yaml_loader = get_yaml_loader()
        # data = yaml_loader.load(text) or {}
        data = yaml_loader.load(fp) or {}

    project = data.get("project") or {}
    project_type = str(project.get("type") or "").lower()

    manifest = load_manifest()
    branches: list[BranchSpec] = read_branches_list()
    branch_set = {b["branch"] for b in branches}

    # Prune manifest entries for branches no longer listed
    removed = [b for b in manifest.keys() if b not in branch_set]
    if removed:
        logger.info("Pruning grafts removed from grafts.yaml: %s", ", ".join(removed))
        for b in removed:
            manifest.pop(b, None)
        save_manifest(manifest)

    # Build auto-generated items grouped by collar
    def build_collar_items(item_type: str) -> dict[str, list[Any]]:
        """
        Build items grouped by collar, preserving the original structure from each graft.
        Rewrites all file paths to prepend grafts__/{branch_key}/.
        """
        collar_items: dict[str, list[Any]] = {}
        content_key = "chapters" if item_type == "part" else "contents"

        def rewrite_paths(node: Any, branch_key: str) -> Any:
            """Recursively rewrite file paths in a structure to prepend grafts__/{branch_key}/."""
            if isinstance(node, str):
                # It's a file path - prepend the graft path
                return f"grafts__/{branch_key}/{node}"
            elif isinstance(node, dict):
                # Recursively process dict values
                result = {}
                for key, value in node.items():
                    if key in (content_key, "chapters", "contents"):
                        # Recursively process contents/chapters
                        result[key] = rewrite_paths(value, branch_key)
                    elif key in ("file", "href"):
                        # These are file references
                        result[key] = f"grafts__/{branch_key}/{value}"
                    else:
                        # Keep other keys as-is
                        result[key] = value
                return result
            elif isinstance(node, list):
                # Recursively process list items
                return [rewrite_paths(item, branch_key) for item in node]
            else:
                # Return as-is for other types
                return node

        for spec in branches:
            branch = spec["branch"]
            collar = spec["collar"]
            entry = manifest.get(branch)
            if not entry:
                continue
            title = entry.get("title") or spec["name"]
            branch_key = entry.get("branch_key") or branch_to_key(spec["name"])
            structure = entry.get("structure")

            # If no structure is preserved, skip this graft
            if not structure:
                logger.warning(f"No structure found for graft '{branch}' - skipping")
                continue

            # Rewrite all paths in the structure
            rewritten_structure = rewrite_paths(structure, branch_key)

            # Wrap in a section/part with the graft title
            item = {
                item_type: title,
                content_key: rewritten_structure if isinstance(rewritten_structure, list) else [rewritten_structure],
                YAML_AUTOGEN_MARKER: branch,
            }

            if collar not in collar_items:
                collar_items[collar] = []
            collar_items[collar].append(item)
        return collar_items

    # Helper: update all collars with their grafts
    def splice_collars(seq: list[Any], collar_items: dict[str, list[Any]]) -> None:
        """Find all collar markers and inject the appropriate grafts after each."""
        collars = _find_all_collars(seq)

        # For each collar marker, inject the grafts that belong to it
        for collar_name, (target_list, marker_idx) in collars.items():
            items = collar_items.get(collar_name, [])

            # Find the end of existing autogenerated content
            end_idx = marker_idx + 1
            while end_idx < len(target_list):
                ch = target_list[end_idx]
                if not isinstance(ch, Mapping):
                    break
                if YAML_AUTOGEN_MARKER not in ch:
                    break
                end_idx += 1

            # Replace the autogenerated content
            target_list[marker_idx + 1 : end_idx] = items

    if project_type == "book" or ("book" in data and "chapters" in (data.get("book") or {})):
        # --- Book mode ---
        book = data.get("book") or {}
        chapters = book.get("chapters")
        if not isinstance(chapters, list):
            raise RuntimeError("book.chapters must be a list")

        collar_items = build_collar_items("part")
        splice_collars(chapters, collar_items)

    elif project_type == "website" or ("website" in data and "sidebar" in (data.get("website") or {})):
        # --- Website mode ---
        website = data.get("website") or {}
        sidebar = website.get("sidebar") or {}
        contents = sidebar.get("contents")
        if not isinstance(contents, list):
            raise RuntimeError("website.sidebar.contents must be a list")

        collar_items = build_collar_items("section")
        splice_collars(contents, collar_items)

    else:
        raise RuntimeError(
            "Neither book.chapters nor website.sidebar.contents found; "
            "cannot apply auto-generated chapter updates."
        )

    # Write YAML back atomically
    atomic_write_yaml(quarto_file, data)

    logger.info("Synced docs/ with manifest:")
    for spec in branches:
        branch = spec["branch"]
        entry = manifest.get(branch)
        if not entry or not entry.get("exported"):
            continue
        logger.info(
            f"  - {branch}: {len(entry['exported'])} files -> title '{entry.get('title')}'"
        )
