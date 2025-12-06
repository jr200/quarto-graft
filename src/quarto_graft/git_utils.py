from __future__ import annotations

import logging
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from .constants import ROOT, WORKTREES, TRUNK_BRANCHES

logger = logging.getLogger(__name__)


def run_git(args: List[str], cwd: Optional[Path] = None) -> str:
    """
    Run a git command and return the stripped stdout.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def list_worktree_paths() -> List[Path]:
    """Return a list of worktree paths registered with git."""
    out = run_git(["worktree", "list", "--porcelain"], cwd=ROOT)
    paths: List[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = line.split(" ", 1)[1].strip()
            paths.append(Path(wt).resolve())
    return paths


def is_worktree(path: Path) -> bool:
    """Check whether the given path is a registered git worktree."""
    path_resolved = path.resolve()
    return path_resolved in list_worktree_paths()


def worktrees_for_branch(branch: str) -> List[Path]:
    """Return paths of worktrees checked out at a given branch."""
    out = run_git(["worktree", "list", "--porcelain"], cwd=ROOT)
    paths: List[Path] = []
    current: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line.split(" ", 1)[1].strip()).resolve()
            continue
        if line.startswith("branch ") and current is not None:
            ref = line.split(" ", 1)[1].strip()
            if ref == f"refs/heads/{branch}":
                paths.append(current)
            current = None
    return paths


def fetch_origin() -> None:
    """Fetch and prune origin to ensure refs are up to date before building."""
    logger.info("[fetch] git fetch --prune origin")
    subprocess.run(
        ["git", "fetch", "--prune", "origin"],
        cwd=ROOT,
        check=True,
    )


def create_worktree(ref: str, name: str) -> Path:
    """
    Create (or reuse) a git worktree for the given reference.
    """
    WORKTREES.mkdir(exist_ok=True)
    wt_dir = WORKTREES / name

    # Check if it's an existing valid worktree first to allow reuse within a session
    if wt_dir.exists():
        if is_worktree(wt_dir):
            logger.info(f"[worktree] Reusing existing worktree at {wt_dir} for ref {ref}")
            subprocess.run(
                ["git", "-C", str(wt_dir), "checkout", "--detach", ref],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(wt_dir), "reset", "--hard", ref],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(wt_dir), "clean", "-fdx"],
                check=True,
            )
            return wt_dir
        try:
            # Try to remove it as a worktree first
            subprocess.run(
                ["git", "worktree", "remove", str(wt_dir)],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
        except Exception as e:
            # If that fails, remove manually
            logger.debug(f"Failed to remove worktree via git, removing manually: {wt_dir} ({e})")
            shutil.rmtree(wt_dir)

    subprocess.run(
        ["git", "worktree", "add", "-f", str(wt_dir), ref],
        cwd=ROOT,
        check=True,
    )
    return wt_dir


def remove_worktree(worktree_name: str | Path, force: bool = False) -> None:
    """Remove a git worktree by name or absolute path."""
    wt_dir = Path(worktree_name)
    if not wt_dir.is_absolute():
        wt_dir = WORKTREES / wt_dir
    if not wt_dir.exists():
        return

    try:
        cmd = ["git", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(str(wt_dir))
        subprocess.run(
            cmd,
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        logger.debug(f"Removed worktree: {wt_dir}")
    except subprocess.CalledProcessError:
        # Fallback to manual removal if git worktree remove fails
        logger.warning(f"Failed to remove worktree via git, removing manually: {wt_dir}")
        shutil.rmtree(wt_dir)


@contextmanager
def managed_worktree(ref: str, name: str):
    """Context manager for managing git worktrees with automatic cleanup."""
    wt_dir = None
    try:
        wt_dir = create_worktree(ref, name)
        yield wt_dir
    finally:
        if wt_dir is not None:
            try:
                remove_worktree(name)
            except Exception as e:
                logger.warning(f"Failed to cleanup worktree {name}: {e}")


def ensure_worktree(branch: str) -> Path:
    """
    Ensure there is a git worktree for the given branch under .grafts/<branch>.
    """

    if branch in TRUNK_BRANCHES:
        raise ValueError(f"{branch} is not a graft git-branch")

    wt_dir = WORKTREES / branch

    if wt_dir.exists():
        logger.info(f"[get-worktree] Worktree directory already exists: {wt_dir}")
        return wt_dir

    logger.info(f"[get-worktree] Creating worktree for branch '{branch}' at {wt_dir} ...")

    # Determine which ref to use: local branch or origin/<branch>
    ref = None
    try:
        run_git(["show-ref", "--verify", f"refs/heads/{branch}"])
        ref = branch
        logger.info(f"[get-worktree] Using local branch '{branch}'")
    except subprocess.CalledProcessError:
        # Try remote
        try:
            run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"])
            ref = f"origin/{branch}"
            logger.info(f"[get-worktree] Using remote branch 'origin/{branch}'")
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"Branch '{branch}' does not exist locally or on origin"
            )

    WORKTREES.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-f", str(wt_dir), ref],
        cwd=ROOT,
        check=True,
    )

    logger.info(f"[get-worktree] Worktree created: {wt_dir}")
    return wt_dir


def delete_worktree(branch: str) -> None:
    """Delete the git worktree under .grafts/<branch>."""
    logger.info(f"[delete-worktree] Removing worktree for branch '{branch}'")
    remove_worktree(branch)


def cleanup_orphan_worktrees() -> List[Path]:
    """
    Remove directories under .grafts/ that are no longer registered with git.

    Returns:
        List of removed worktree paths.
    """
    WORKTREES.mkdir(exist_ok=True)
    registered = set(list_worktree_paths())
    removed: List[Path] = []
    for path in WORKTREES.iterdir():
        if not path.is_dir():
            continue
        if path.resolve() in registered:
            continue
        logger.info(f"[cleanup-worktrees] Removing orphaned worktree dir {path}")
        shutil.rmtree(path)
        removed.append(path)
    return removed
