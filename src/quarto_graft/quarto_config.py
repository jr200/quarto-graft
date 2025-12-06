from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .branches import BranchSpec, branch_to_key, load_manifest, read_branches_list, save_manifest
from .constants import (
    GRAFT_COLLAR_MARKER,
    QUARTO_PROJECT_YAML,
    QUARTO_CONFIG_YAML
)
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

def load_quarto_config(docs_dir: Path) -> Dict[str, Any]:
    """Load Quarto configuration from docs directory."""
    qfile_yaml = docs_dir / QUARTO_CONFIG_YAML
    if qfile_yaml.exists():
        cfg_path = qfile_yaml
    else:
        raise RuntimeError(f"No {QUARTO_CONFIG_YAML} found in {docs_dir}")
    yaml_loader = get_yaml_loader()
    return yaml_loader.load(cfg_path.read_text(encoding="utf-8")) or {}


def list_available_collars(config_path: Path | None = None) -> List[str]:
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

    collars: List[str] = []

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


def flatten_quarto_contents(entries: Any) -> List[str]:
    """
    Flatten Quarto-style contents/chapters structures into an ordered list of files.
    """
    files: List[str] = []

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
                if key in node and isinstance(node[key], list):
                    for child in node[key]:
                        walk(child)

    if isinstance(entries, list):
        for e in entries:
            walk(e)

    return files


def collect_exported_relpaths(docs_dir: Path, cfg: Dict[str, Any]) -> List[str]:
    """
    Determine which *source documents* to export from this branch's docs/,
    preserving the branch author's intended order as far as possible.
    """
    project = cfg.get("project") or {}
    render_spec = project.get("render")

    website = cfg.get("website") or {}
    sidebar = website.get("sidebar") or {}
    sidebar_contents = sidebar.get("contents")

    book = cfg.get("book") or {}
    book_chapters = book.get("chapters")

    relpaths: List[str] = []

    # website.sidebar.contents: use nav order
    files_from_sidebar = flatten_quarto_contents(sidebar_contents)
    if files_from_sidebar:
        for rel in files_from_sidebar:
            p = docs_dir / rel
            if not p.exists():
                continue
            if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                continue
            relpaths.append(p.relative_to(docs_dir).as_posix())
        if relpaths:
            return relpaths

    # book.chapters: for branch-type "book" projects
    files_from_book = flatten_quarto_contents(book_chapters)
    if files_from_book:
        for rel in files_from_book:
            p = docs_dir / rel
            if not p.exists():
                continue
            if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                continue
            relpaths.append(p.relative_to(docs_dir).as_posix())
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


def derive_section_title(cfg: Dict[str, Any], branch: str) -> str:
    """Derive the section title from Quarto configuration or use branch name."""
    website = cfg.get("website") or {}
    book = cfg.get("book") or {}
    title = website.get("title") or book.get("title") or branch
    return str(title)

def is_collar_marker(item: Any) -> bool:
    """Check if item is a collar marker (_GRAFT_COLLAR)."""
    return isinstance(item, Mapping) and GRAFT_COLLAR_MARKER in item


def _find_all_collars(seq: List[Any]) -> Dict[str, tuple[List[Any], int]]:
    """
    Find all collar markers in the structure.
    Returns dict mapping collar_name -> (list_ref, index).
    """
    collars: Dict[str, tuple[List[Any], int]] = {}

    def search(items: List[Any]) -> None:
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

    with open(quarto_file, "rt") as fp:
        yaml_loader = get_yaml_loader()
        # data = yaml_loader.load(text) or {}
        data = yaml_loader.load(fp) or {}

    project = data.get("project") or {}
    project_type = str(project.get("type") or "").lower()

    manifest = load_manifest()
    branches: List[BranchSpec] = read_branches_list()
    branch_set = {b["branch"] for b in branches}

    # Prune manifest entries for branches no longer listed
    removed = [b for b in manifest.keys() if b not in branch_set]
    if removed:
        logger.info("Pruning grafts removed from grafts.yaml: %s", ", ".join(removed))
        for b in removed:
            manifest.pop(b, None)
        save_manifest(manifest)

    # Build auto-generated items grouped by collar
    def build_collar_items(item_type: str) -> Dict[str, List[Any]]:
        """Build items grouped by collar. item_type is 'part' (book) or 'section' (website)."""
        collar_items: Dict[str, List[Any]] = {}
        content_key = "chapters" if item_type == "part" else "contents"

        for spec in branches:
            branch = spec["branch"]
            collar = spec["collar"]
            entry = manifest.get(branch)
            if not entry:
                continue
            title = entry.get("title") or spec["name"]
            branch_key = entry.get("branch_key") or branch_to_key(spec["local_path"])
            exported: List[str] = entry.get("exported") or []
            if not exported:
                continue

            paths = [f"grafts__/{branch_key}/{rel}" for rel in exported]
            item = {
                item_type: title,
                content_key: paths,
                "_autogen_branch": branch,
            }

            if collar not in collar_items:
                collar_items[collar] = []
            collar_items[collar].append(item)
        return collar_items

    # Helper: update all collars with their grafts
    def splice_collars(seq: List[Any], collar_items: Dict[str, List[Any]]) -> None:
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
                if "_autogen_branch" not in ch:
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
    temp_file = quarto_file.with_suffix(".yaml.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        yaml_loader.dump(data, f)
    temp_file.replace(quarto_file)

    logger.info("Synced docs/ with manifest:")
    for spec in branches:
        branch = spec["branch"]
        entry = manifest.get(branch)
        if not entry or not entry.get("exported"):
            continue
        logger.info(
            f"  - {branch}: {len(entry['exported'])} files -> title '{entry.get('title')}'"
        )
