from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, TypedDict

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateSyntaxError

from .constants import GRAFTS_CONFIG_FILE, GRAFTS_MANIFEST_FILE, PROTECTED_BRANCHES, ROOT, WORKTREES
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
            rendered = env.from_string(text).render(context)
            dest_path.write_text(rendered, encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(src_path, dest_path)
        except TemplateSyntaxError:
            # Skip Jinja templating for pre-rendered site assets; copy as-is
            if in_site_dir:
                shutil.copy2(src_path, dest_path)
            else:
                raise


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
    local_path: str    # worktree directory under .grafts/


def branch_to_key(branch: str) -> str:
    """Convert branch name to filesystem-safe key."""
    return branch.replace("/", "-")


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
        if isinstance(item, str):
            logger.warning(
                "grafts.yaml entry %d is a string; interpreting as name=branch=local_path=%s. "
                "Consider migrating to dict form with name/branch/local_path.",
                idx,
                item,
            )
            spec: BranchSpec = {"name": item, "branch": item, "local_path": item}
        elif isinstance(item, dict):
            if "name" not in item or "branch" not in item:
                raise ValueError("Each graft in grafts.yaml must include 'name' and 'branch'")
            name = str(item.get("name", "")).strip()
            branch = str(item.get("branch", "")).strip()
            local_path = str(item.get("local_path") or name).strip()
            spec = {"name": name, "branch": branch, "local_path": local_path}
        else:
            raise ValueError("Each grafts.yaml entry must be a mapping with name/branch/local_path")

        if not spec["name"] or not spec["branch"]:
            raise ValueError("grafts.yaml entries must include non-empty 'name' and 'branch'")

        _validate_label("graft name", spec["name"])
        _validate_label("git branch name", spec["branch"])
        _validate_label("local_path", spec["local_path"])

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
    template: str,
    push: bool = False,
    branch_name: str | None = None,
    local_path: str | None = None,
) -> Path:
    """
    Create a new orphan graft branch from a template under templates/<template>.
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

    # Check branch doesn't already exist
    already_local = False
    already_remote = False
    try:
        run_git(["show-ref", "--verify", f"refs/heads/{branch}"])
        already_local = True
    except subprocess.CalledProcessError:
        pass

    try:
        run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"])
        already_remote = True
    except subprocess.CalledProcessError:
        pass

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

    template_dir = ROOT / "templates" / template
    if not template_dir.exists() or not template_dir.is_dir():
        raise RuntimeError(f"Template '{template}' not found under templates/")

    # Create worktree + new branch
    branch_key = branch_to_key(loc_path)
    wt_dir = WORKTREES / branch_key
    if wt_dir.exists():
        raise RuntimeError(
            f"Worktree directory {wt_dir} already exists; refusing to overwrite for new graft."
        )

    WORKTREES.mkdir(exist_ok=True)
    logger.info(f"[new-graft] Creating worktree for new branch '{branch}' at {wt_dir}...")
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wt_dir), "checkout", "--orphan", branch],
        check=True,
    )

    # Ensure the worktree starts empty before seeding with the template
    for child in wt_dir.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    pkg_name = _python_package_name(name)
    context = {
        "graft_name": name,
        "graft_branch": branch,
        "graft_local_path": loc_path,
        "graft_slug": branch_key,
        "package_name": pkg_name,
        "project_slug": _project_slug(pkg_name),
    }

    logger.info(f"[new-graft] Rendering template '{template}' with context: {context}")
    _render_template_tree(template_dir, wt_dir, context)

    # Optionally push to origin
    if push:
        logger.info(f"[new-graft] Creating initial commit and pushing new branch '{branch}' to origin...")
        subprocess.run(
            ["git", "-C", str(wt_dir), "add", "-A"],
            check=True,
        )
        status = subprocess.run(
            ["git", "-C", str(wt_dir), "status", "--porcelain"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if status:
            subprocess.run(
                ["git", "-C", str(wt_dir), "commit", "-m", f"Initialize graft from template '{template}'"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(wt_dir), "push", "-u", "origin", "HEAD"],
                check=True,
            )
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
        entry: Dict[str, str] = {"name": name, "branch": branch}
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
    return wt_dir


def destroy_graft(branch: str, delete_remote: bool = True) -> Dict[str, List[str]]:
    """
    Remove all traces of a graft branch:
    - delete worktrees under .grafts/
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
            worktree_candidates.add(wt_path.relative_to(WORKTREES))
        except ValueError:
            pass

    for key in sorted(worktree_candidates, key=lambda x: str(x)):
        if isinstance(key, Path):
            wt_dir = key
        else:
            wt_dir = WORKTREES / key
        if wt_dir.exists():
            logger.info(f"[destroy] Removing worktree {wt_dir}")
            remove_worktree(wt_dir, force=True)
            summary["worktrees_removed"].append(str(wt_dir))

    # Ensure git forgets any stale worktree entries
    try:
        run_git(["worktree", "prune"], cwd=ROOT)
    except subprocess.CalledProcessError:
        logger.info("[destroy] git worktree prune failed; continuing")

    # Delete local branch (force)
    try:
        run_git(["branch", "-D", branch], cwd=ROOT)
        logger.info(f"[destroy] Deleted local branch '{branch}'")
    except subprocess.CalledProcessError:
        logger.info(f"[destroy] Local branch '{branch}' not found or already removed")

    if delete_remote:
        res = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            logger.info(f"[destroy] Deleted remote branch '{branch}'")
        else:
            logger.info(
                f"[destroy] Remote branch '{branch}' could not be deleted or not found: {res.stderr.strip()}"
            )

    if branch in manifest:
        manifest.pop(branch, None)
        save_manifest(manifest)
        summary["manifest_removed"].append(branch)

    return summary
