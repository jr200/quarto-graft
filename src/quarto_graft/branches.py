from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List, TypedDict

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateSyntaxError
import pygit2

from .constants import (
    GRAFTS_CONFIG_FILE,
    GRAFTS_MANIFEST_FILE,
    PROTECTED_BRANCHES,
    ROOT,
    WORKTREES_CACHE,
    TRUNK_TEMPLATES_DIR,
    MAIN_DOCS,
)
from .git_utils import remove_worktree, run_git, worktrees_for_branch
from .yaml_utils import get_yaml_loader

logger = logging.getLogger(__name__)


def _python_package_name(seed: str) -> str:
    """Create a safe, importable Python package name from the graft name."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", seed)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "graft"
    if cleaned[0].isdigit():
        cleaned = f"g_{cleaned}"
    return cleaned.lower()


def _project_slug(package_name: str) -> str:
    """Project slug suitable for package/distribution names."""
    return package_name.replace("_", "-")


SHORTCODE_PATTERN = re.compile(r"{{\s*[<%].*?[>%]\s*}}")


def _escape_quarto_shortcodes(text: str) -> str:
    """
    Convert Quarto shortcodes ({{< ... >}} / {{% ... %}}) into literal strings so
    Jinja will not attempt to parse them as template expressions.
    """
    def _repl(match: re.Match[str]) -> str:
        literal = match.group(0).replace("\\", "\\\\").replace("'", "\\'")
        return f"{{{{ '{literal}' }}}}"

    return SHORTCODE_PATTERN.sub(_repl, text)


def _render_template_tree(template_dir: Path, dest_dir: Path, context: Dict[str, str]) -> None:
    """
    Render a template directory (Jinja2) into dest_dir.

    File and directory names, as well as file contents, are rendered.
    Binary files are copied as-is if they cannot be decoded as UTF-8.
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )

    for src_path in sorted(template_dir.rglob("*")):
        if src_path.name.startswith(".DS_Store"):
            continue
        rel = src_path.relative_to(template_dir).as_posix()
        rendered_rel = env.from_string(rel).render(context)
        dest_path = dest_dir / Path(rendered_rel)

        if src_path.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)
            continue

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        in_site_dir = "_site" in src_path.relative_to(template_dir).parts

        try:
            text = src_path.read_text(encoding="utf-8")
            safe_text = _escape_quarto_shortcodes(text)
            rendered = env.from_string(safe_text).render(context)
            dest_path.write_text(rendered, encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(src_path, dest_path)
        except TemplateSyntaxError:
            # Skip Jinja templating for pre-rendered site assets; copy as-is
            if in_site_dir:
                shutil.copy2(src_path, dest_path)
            else:
                raise


def _purge_pycache(root: Path) -> None:
    """Remove __pycache__ directories and stray .pyc files under root (excluding .git)."""
    for path in root.rglob("__pycache__"):
        if ".git" in path.parts:
            continue
        shutil.rmtree(path, ignore_errors=True)
    for path in root.rglob("*.pyc"):
        if ".git" in path.parts:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class ManifestEntry(TypedDict, total=False):
    """Type definition for entries in grafts.lock manifest."""

    last_good: str
    last_checked: str
    title: str
    branch_key: str
    exported: List[str]

class BranchSpec(TypedDict):
    """Configuration for a single graft branch."""

    name: str          # logical graft name
    branch: str        # git branch name
    local_path: str    # worktree directory under .grafts-cache/
    collar: str        # attachment point in trunk _quarto.yaml


def branch_to_key(branch: str) -> str:
    """Convert branch name to filesystem-safe key."""
    return branch.replace("/", "-")


def _open_repo() -> pygit2.Repository:
    """Open the main git repository at ROOT."""
    git_dir = pygit2.discover_repository(str(ROOT))
    if not git_dir:
        raise RuntimeError(f"No git repository found at {ROOT}")
    return pygit2.Repository(git_dir)


def remove_from_grafts_config(branch: str) -> List[str]:
    """
    Remove a branch from grafts.yaml.

    Returns:
        List of local_path keys removed (for cleaning worktrees).
    """
    if not GRAFTS_CONFIG_FILE.exists():
        return []

    yaml_loader = get_yaml_loader()
    data = yaml_loader.load(GRAFTS_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    branches_list = data.get("branches", [])
    if not isinstance(branches_list, list):
        return []

    kept: List = []
    removed_keys: List[str] = []

    for item in branches_list:
        if isinstance(item, str):
            if item == branch:
                removed_keys.append(branch_to_key(item))
                continue
        elif isinstance(item, dict):
            if item.get("branch") == branch:
                local_path = str(item.get("local_path") or item.get("name") or branch)
                removed_keys.append(branch_to_key(local_path))
                continue
        kept.append(item)

    if len(kept) != len(branches_list):
        data["branches"] = kept
        temp_file = GRAFTS_CONFIG_FILE.with_suffix(".yaml.tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            yaml_loader.dump(data, f)
        temp_file.replace(GRAFTS_CONFIG_FILE)

    return removed_keys


def load_manifest() -> Dict[str, ManifestEntry]:
    """Load the grafts.lock manifest file."""
    if not GRAFTS_MANIFEST_FILE.exists():
        return {}
    try:
        return json.loads(GRAFTS_MANIFEST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse manifest {GRAFTS_MANIFEST_FILE}: {e}")
        return {}


def save_manifest(manifest: Dict[str, ManifestEntry]) -> None:
    """Save the grafts.lock manifest file atomically."""
    temp_file = GRAFTS_MANIFEST_FILE.with_suffix(".lock.tmp")
    temp_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_file.replace(GRAFTS_MANIFEST_FILE)


def _validate_label(label: str, value: str) -> None:
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{label} must not contain whitespace: '{value}'")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ValueError(
            f"Invalid {label} '{value}': only letters, digits, ., _, /, and - are allowed"
        )


def read_branches_list(path: Path | None = None) -> List[BranchSpec]:
    path = path or GRAFTS_CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(f"No grafts.yaml found at {path}")

    yaml_loader = get_yaml_loader()
    data = yaml_loader.load(path.read_text(encoding="utf-8")) or {}
    raw_list = data.get("branches", [])
    if not isinstance(raw_list, list):
        raise ValueError("grafts.yaml 'branches' must be a list")

    specs: List[BranchSpec] = []
    seen_branches: set[str] = set()
    seen_local_paths: set[str] = set()

    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            raise ValueError(
                f"grafts.yaml entry {idx} must be a dict with keys: name, branch, collar. "
                f"Optional: local_path. Got: {type(item).__name__}"
            )

        if "name" not in item or "branch" not in item:
            raise ValueError("Each graft in grafts.yaml must include 'name' and 'branch'")
        if "collar" not in item:
            raise ValueError("Each graft in grafts.yaml must include 'collar' (attachment point)")

        name = str(item.get("name", "")).strip()
        branch = str(item.get("branch", "")).strip()
        local_path = str(item.get("local_path") or name).strip()
        collar = str(item.get("collar", "")).strip()
        spec: BranchSpec = {"name": name, "branch": branch, "local_path": local_path, "collar": collar}

        if not spec["name"] or not spec["branch"] or not spec["collar"]:
            raise ValueError("grafts.yaml entries must include non-empty 'name', 'branch', and 'collar'")

        _validate_label("graft name", spec["name"])
        _validate_label("git branch name", spec["branch"])
        _validate_label("local_path", spec["local_path"])
        _validate_label("collar", spec["collar"])

        if spec["branch"] in PROTECTED_BRANCHES:
            protected_list = ", ".join(f"'{b}'" for b in sorted(PROTECTED_BRANCHES))
            raise ValueError(f"Invalid grafts.yaml. Cannot contain protected branches: {protected_list}")

        if spec["branch"] in seen_branches:
            logger.warning("Duplicate branch '%s' found in grafts.yaml; ignoring subsequent entries", spec["branch"])
            continue
        if spec["local_path"] in seen_local_paths:
            logger.warning(
                "Duplicate local_path '%s' found in grafts.yaml; ignoring subsequent entries", spec["local_path"]
            )
            continue
        seen_branches.add(spec["branch"])
        seen_local_paths.add(spec["local_path"])
        specs.append(spec)

    if PROTECTED_BRANCHES.intersection(seen_branches):
        protected_list = ", ".join(f"'{b}'" for b in sorted(PROTECTED_BRANCHES))
        raise ValueError(f"Invalid grafts.yaml. Cannot contain protected branches: {protected_list}")

    return specs


def new_graft_branch(
    name: str,
    template: str | Path,
    collar: str,
    push: bool = False,
    branch_name: str | None = None,
    local_path: str | None = None,
) -> tuple[Path, str | None]:
    """
    Create a new orphan graft branch from a template.

    Args:
        name: Display name for the graft
        template: Template name (str) or direct path to template directory (Path)
        collar: Attachment point in trunk _quarto.yaml (e.g., 'main', 'notes', 'bugs')
        push: Whether to push the new branch to remote
        branch_name: Git branch name (defaults to name)
        local_path: Local worktree path (defaults to name)

    Returns:
        A tuple of (worktree_path, trunk_instructions_content)
        trunk_instructions_content is None if no TRUNK_INSTRUCTIONS.md was found

    The graft's display name (`name`) can differ from the git branch name (`branch_name`).
    """
    if any(ch.isspace() for ch in name):
        raise RuntimeError("Graft name must not contain whitespace")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", name):
        raise RuntimeError(
            f"Invalid graft name '{name}': only letters, digits, ., _, /, and - are allowed"
        )

    branch = branch_name or name
    if any(ch.isspace() for ch in branch):
        raise RuntimeError("Git branch name must not contain whitespace")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise RuntimeError(
            f"Invalid git branch name '{branch}': only letters, digits, ., _, /, and - are allowed"
        )

    if branch in PROTECTED_BRANCHES:
        raise RuntimeError(f"'{branch}' is a protected branch name, cannot use for graft branch")

    loc_path = local_path or name
    if any(ch.isspace() for ch in loc_path):
        raise RuntimeError("local_path must not contain whitespace")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", loc_path):
        raise RuntimeError(
            f"Invalid local_path '{loc_path}': only letters, digits, ., _, /, and - are allowed"
        )

    repo = _open_repo()
    already_local = branch in repo.branches.local
    already_remote = f"origin/{branch}" in repo.branches.remote

    if already_local or already_remote:
        where = []
        if already_local:
            where.append("local")
        if already_remote:
            where.append("remote")
        where_str = "/".join(where)
        raise RuntimeError(
            f"Branch '{branch}' already exists ({where_str}); won't create a new graft with this name."
        )

    template_dir = template
    template_name = template.name

    if not template_dir.exists() or not template_dir.is_dir():
        raise RuntimeError(f"Graft template directory not found: {template_dir}")

    # Create worktree + new branch
    branch_key = branch_to_key(loc_path)
    wt_dir = WORKTREES_CACHE / branch_key
    if wt_dir.exists():
        raise RuntimeError(
            f"Worktree directory {wt_dir} already exists; refusing to overwrite for new graft."
        )

    WORKTREES_CACHE.mkdir(exist_ok=True)
    logger.info(f"[new-graft] Creating worktree for new branch '{branch}' at {wt_dir}...")
    if repo.head_is_unborn:
        raise RuntimeError(
            "Cannot create a graft because the repository has no commits yet. "
            "Commit your trunk files first, then retry."
        )
    # pygit2.add_worktree does not accept keyword args; default ref is HEAD
    repo.add_worktree(branch_key, str(wt_dir))
    wt_repo = pygit2.Repository(str(wt_dir))

    # Reset worktree to clean state
    wt_repo.reset(wt_repo.head.target, pygit2.GIT_RESET_HARD)
    wt_repo.checkout_head(strategy=pygit2.GIT_CHECKOUT_FORCE)

    # Remove all files except .git metadata
    for entry in wt_dir.iterdir():
        if entry.name.startswith(".git"):
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    wt_repo.index.read()
    wt_repo.index.clear()
    wt_repo.index.write()
    _purge_pycache(wt_dir)

    pkg_name = _python_package_name(name)
    context = {
        "graft_name": name,
        "graft_branch": branch,
        "graft_local_path": loc_path,
        "graft_slug": branch_key,
        "package_name": pkg_name,
        "project_slug": _project_slug(pkg_name),
    }

    logger.info(f"[new-graft] Rendering template '{template_name}' with context: {context}")
    _render_template_tree(template_dir, wt_dir, context)

    # Check for TRUNK_INSTRUCTIONS.md
    trunk_instructions_path = wt_dir / "TRUNK_INSTRUCTIONS.md"
    trunk_instructions_content = None
    if trunk_instructions_path.exists():
        trunk_instructions_content = trunk_instructions_path.read_text(encoding="utf-8")
        trunk_instructions_path.unlink()  # Delete so it doesn't get committed
        logger.info("[new-graft] Found TRUNK_INSTRUCTIONS.md; removed from worktree")

    # Stage and optionally commit/push
    wt_repo.index.add_all()
    wt_repo.index.write()
    if len(wt_repo.index) > 0:
        tree_id = wt_repo.index.write_tree()
        sig = wt_repo.default_signature
        _commit_id = wt_repo.create_commit(
            f"refs/heads/{branch}",
            sig,
            sig,
            f"Initialize graft from template '{template_name}'",
            tree_id,
            [],  # orphan commit
        )
        wt_repo.set_head(f"refs/heads/{branch}")
        wt_repo.state_cleanup()

        if push:
            logger.info(f"[new-graft] Pushing new branch '{branch}' to origin...")
            try:
                run_git(["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd=wt_dir)
            except Exception as e:
                logger.warning(f"[new-graft] Push failed: {e}")
    else:
        logger.info("[new-graft] Template produced no files to commit; skipping push.")

    # Append branch name to grafts.yaml if not already present
    yaml_loader = get_yaml_loader()
    if GRAFTS_CONFIG_FILE.exists():
        data = yaml_loader.load(GRAFTS_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    else:
        data = {}

    branches_list = data.get("branches", [])
    exists = any(
        (isinstance(item, dict) and item.get("branch") == branch)
        or (isinstance(item, str) and item == branch)
        for item in branches_list
    )

    if not exists:
        entry: Dict[str, str] = {"name": name, "branch": branch, "collar": collar}
        if loc_path != name:
            entry["local_path"] = loc_path
        branches_list.append(entry)
        data["branches"] = branches_list
        temp_file = GRAFTS_CONFIG_FILE.with_suffix(".yaml.tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            yaml_loader.dump(data, f)
        temp_file.replace(GRAFTS_CONFIG_FILE)
        logger.info(f"[new-graft] Added '{branch}' to grafts.yaml")
    else:
        logger.info(f"[new-graft] '{branch}' already exists in grafts.yaml; not adding")

    logger.info(f"[new-graft] New graft branch '{branch}' ready in worktree: {wt_dir}")
    return wt_dir, trunk_instructions_content


def destroy_graft(branch: str, delete_remote: bool = True) -> Dict[str, List[str]]:
    """
    Remove all traces of a graft branch:
    - delete worktrees under .grafts-cache/
    - delete local branch (force)
    - delete remote branch (if requested)
    - remove from grafts.yaml and grafts.lock
    """
    summary: Dict[str, List[str]] = {
        "worktrees_removed": [],
        "config_removed": [],
        "manifest_removed": [],
    }

    manifest = load_manifest()

    removed_keys = remove_from_grafts_config(branch)
    summary["config_removed"] = removed_keys

    branch_key = branch_to_key(branch)
    worktree_candidates: set[str | Path] = set(removed_keys + [branch_key])

    # If manifest has a branch_key, include it
    manifest_entry = manifest.get(branch)
    if manifest_entry and manifest_entry.get("branch_key"):
        worktree_candidates.add(manifest_entry["branch_key"])

    # Also include any worktrees currently checked out at this branch
    for wt_path in worktrees_for_branch(branch):
        worktree_candidates.add(wt_path)
        try:
            worktree_candidates.add(wt_path.relative_to(WORKTREES_CACHE))
        except ValueError:
            pass

    for key in sorted(worktree_candidates, key=lambda x: str(x)):
        if isinstance(key, Path):
            wt_dir = key
        else:
            wt_dir = WORKTREES_CACHE / key
        if wt_dir.exists():
            logger.info(f"[destroy] Removing worktree {wt_dir}")
            remove_worktree(wt_dir, force=True)
            summary["worktrees_removed"].append(str(wt_dir))

    # Ensure git forgets any stale worktree entries
    try:
        run_git(["worktree", "prune"], cwd=ROOT)
    except Exception:
        logger.info("[destroy] worktree prune failed; continuing")

    repo = _open_repo()

    # Delete local branch (force)
    if branch in repo.branches.local:
        try:
            repo.branches.delete(branch)
            logger.info(f"[destroy] Deleted local branch '{branch}'")
        except Exception:
            logger.info(f"[destroy] Failed to delete local branch '{branch}'")

    if delete_remote:
        try:
            run_git(["push", "origin", f":refs/heads/{branch}"], cwd=ROOT)
            logger.info(f"[destroy] Deleted remote branch '{branch}'")
        except Exception:
            logger.info(f"[destroy] Remote branch '{branch}' could not be deleted or not found")

    if branch in manifest:
        manifest.pop(branch, None)
        save_manifest(manifest)
        summary["manifest_removed"].append(branch)

    return summary


def init_trunk(
    name: str,
    template: str | Path,
    overwrite: bool = False,
    with_templates: list[str] | None = None
) -> Path:
    """
    Initialize the trunk (docs/) from a template.

    Args:
        name: Name of the main site/project (used as Jinja2 template parameter)
        template: Template name (str) or direct path to template directory (Path)
        overwrite: If True, overwrite existing docs/ directory
        with_templates: Optional list of addons to include from trunk-templates/with-addons/

    Returns:
        Path to the initialized docs directory
    """
    # Support both string names (legacy) and direct paths (new multi-source)
    if isinstance(template, str):
        template_dir = TRUNK_TEMPLATES_DIR / template
        template_name = template
    else:
        template_dir = template
        template_name = template.name

    if not template_dir.exists() or not template_dir.is_dir():
        raise RuntimeError(f"Trunk template directory not found: {template_dir}")

    # Identify top-level conflicts
    top_level_targets = [MAIN_DOCS / entry.name for entry in template_dir.iterdir()]
    conflicts = [p for p in top_level_targets if p.exists()]
    if conflicts and not overwrite:
        conflict_names = ", ".join(p.name for p in conflicts)
        raise RuntimeError(
            f"Trunk files already exist in this directory: {conflict_names}. "
            "Use --overwrite to replace them."
        )

    if conflicts and overwrite:
        for path in conflicts:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    logger.info(f"[trunk-init] Initializing trunk from template '{template_name}' with name '{name}'...")

    # Create Jinja2 context for trunk
    context = {
        "trunk_name": name,
        "project_name": name,
        "site_name": name,
    }

    logger.info(f"[trunk-init] Rendering template '{template_name}' with context: {context}")
    _render_template_tree(template_dir, MAIN_DOCS, context)
    logger.info(f"[trunk-init] Trunk initialized from template '{template_name}' at {MAIN_DOCS}")

    # Apply additional "with" templates
    if with_templates:
        with_dir = TRUNK_TEMPLATES_DIR / "with-addons"
        for with_name in with_templates:
            with_template_dir = with_dir / with_name
            if not with_template_dir.exists() or not with_template_dir.is_dir():
                logger.warning(f"[trunk-init] addon '{with_name}' not found, skipping")
                continue

            logger.info(f"[trunk-init] Applying addon: {with_name}")
            # Render addon with same context
            _render_template_tree(with_template_dir, MAIN_DOCS, context)

    return MAIN_DOCS
