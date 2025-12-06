from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .branches import BranchSpec, branch_to_key, load_manifest, read_branches_list, save_manifest
from .constants import (
    AUTOGEN_GRAFTS_MARKER,
    MAIN_DOCS,
    QUARTO_CONFIG_YAML,
    QUARTO_CONFIG_YML,
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
    qfile_yml = docs_dir / QUARTO_CONFIG_YML
    qfile_yaml = docs_dir / QUARTO_CONFIG_YAML
    if qfile_yml.exists():
        cfg_path = qfile_yml
    elif qfile_yaml.exists():
        cfg_path = qfile_yaml
    else:
        raise RuntimeError(f"No {QUARTO_CONFIG_YML} or {QUARTO_CONFIG_YAML} found in {docs_dir}")
    yaml_loader = get_yaml_loader()
    return yaml_loader.load(cfg_path.read_text(encoding="utf-8")) or {}


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

def is_grafts_marker(item: Any) -> bool:
    return isinstance(item, Mapping) and AUTOGEN_GRAFTS_MARKER in item


def _find_marker(seq: List[Any]) -> Optional[tuple[List[Any], int]]:
    """
    Find the list and index where the grafts marker appears, searching recursively.
    Returns (list_ref, index) or None if not found.
    """
    for idx, item in enumerate(seq):
        if is_grafts_marker(item):
            return seq, idx
        if isinstance(item, Mapping):
            for key in ("contents", "chapters"):
                child = item.get(key)
                if isinstance(child, list):
                    res = _find_marker(child)
                    if res:
                        return res
    return None


def apply_manifest() -> None:
    """
    Update docs/_quarto.yaml to match docs/grafts__ content, using
    grafts.lock and grafts.yaml.
    """
    quarto_file = MAIN_DOCS / "_quarto.yaml"
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

    # Build auto-generated items (branch -> list of chapter paths)
    def build_auto_items_for_book() -> List[Any]:
        items: List[Any] = []
        for spec in branches:
            branch = spec["branch"]
            entry = manifest.get(branch)
            if not entry:
                continue
            title = entry.get("title") or spec["name"]
            branch_key = entry.get("branch_key") or branch_to_key(spec["local_path"])
            exported: List[str] = entry.get("exported") or []
            if not exported:
                continue
            chapter_paths = [f"grafts__/{branch_key}/{rel}" for rel in exported]
            items.append(
                {
                    "part": title,
                    "chapters": chapter_paths,
                    "_autogen_branch": branch,
                }
            )
        return items

    def build_auto_items_for_website() -> List[Any]:
        items: List[Any] = []
        for spec in branches:
            branch = spec["branch"]
            entry = manifest.get(branch)
            if not entry:
                continue
            title = entry.get("title") or spec["name"]
            branch_key = entry.get("branch_key") or branch_to_key(spec["local_path"])
            exported: List[str] = entry.get("exported") or []
            if not exported:
                continue
            contents = [f"grafts__/{branch_key}/{rel}" for rel in exported]
            items.append(
                {
                    "section": title,
                    "contents": contents,
                    "_autogen_branch": branch,
                }
            )
        return items

    # Helper: update a list in-place after a marker entry
    def splice_autogen_after_marker(seq: List[Any], auto_items: List[Any]) -> None:
        found = _find_marker(seq)
        if not found:
            raise RuntimeError(
                f"Marker '{AUTOGEN_GRAFTS_MARKER}' not found in sidebar/chapters"
            )

        target_list, marker_idx = found

        end_idx = marker_idx + 1
        while end_idx < len(target_list):
            ch = target_list[end_idx]
            if not isinstance(ch, Mapping):
                break
            if "_autogen_branch" not in ch:
                break
            end_idx += 1

        target_list[marker_idx + 1 : end_idx] = auto_items

    if project_type == "book" or ("book" in data and "chapters" in (data.get("book") or {})):
        # --- Book mode ---
        book = data.get("book") or {}
        chapters = book.get("chapters")
        if not isinstance(chapters, list):
            raise RuntimeError("book.chapters must be a list")

        auto_items = build_auto_items_for_book()

        splice_autogen_after_marker(chapters, auto_items)

    elif project_type == "website" or ("website" in data and "sidebar" in (data.get("website") or {})):
        # --- Website mode ---
        website = data.get("website") or {}
        sidebar = website.get("sidebar") or {}
        contents = sidebar.get("contents")
        if not isinstance(contents, list):
            raise RuntimeError("website.sidebar.contents must be a list")

        auto_items = build_auto_items_for_website()

        splice_autogen_after_marker(contents, auto_items)

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
