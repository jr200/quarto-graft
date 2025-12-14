from __future__ import annotations

import logging
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pygit2

from .constants import ROOT, TRUNK_BRANCHES, WORKTREES_CACHE

logger = logging.getLogger(__name__)


def _get_repo(cwd: Path | None = None) -> pygit2.Repository:
    """Open the git repository at cwd (or ROOT)."""
    base = cwd or ROOT
    git_dir = pygit2.discover_repository(str(base))
    if git_dir is None:
        raise RuntimeError(f"No git repository found at {base}")
    return pygit2.Repository(git_dir)


def _list_worktree_objects(repo: pygit2.Repository):
    """Return list of (name, path, head_shorthand) for worktrees."""
    worktrees = []
    if hasattr(repo, "list_worktrees"):
        for name in repo.list_worktrees():
            try:
                wt = repo.lookup_worktree(name)
                wt_repo = pygit2.Repository(str(Path(wt.path)))
                head = wt_repo.head
                shorthand = head.shorthand if head else None
                worktrees.append((name, Path(wt.path).resolve(), shorthand))
            except Exception:
                continue
    return worktrees


def _get_auth_callbacks() -> pygit2.RemoteCallbacks:
    """Create RemoteCallbacks with SSH agent authentication support."""

    class AuthCallbacks(pygit2.RemoteCallbacks):
        def credentials(self, url, username_from_url, allowed_types):
            # Try SSH agent first (most common for GitHub/GitLab)
            if allowed_types & pygit2.credentials.CredentialType.SSH_KEY:
                # Use git as username if connecting to GitHub/GitLab
                username = username_from_url or "git"
                return pygit2.KeypairFromAgent(username)
            # Fallback to default credential types
            return None

    return AuthCallbacks()


def run_git(args: list[str], cwd: Path | None = None) -> str:
    """
    Execute git commands using pygit2 (pure Python implementation).

    Supported commands:
      - for-each-ref refs/heads --format %(refname:short)
      - show-ref --verify <ref>
      - worktree list --porcelain
      - worktree prune
      - worktree remove -f <path>
      - branch -D <branch>
      - rev-parse [--verify] <ref>
      - push origin <refspec>
      - fetch --prune origin

    Raises:
        subprocess.CalledProcessError: For git command errors (for API compatibility)
        NotImplementedError: For unsupported git commands
    """
    repo = _get_repo(cwd)

    # for-each-ref refs/heads --format %(refname:short)
    if args[:2] == ["for-each-ref", "refs/heads"] and "--format" in args:
        branches = sorted(repo.branches.local)
        return "\n".join(branches)

    # show-ref --verify <ref>
    if args[:2] == ["show-ref", "--verify"] and len(args) >= 3:
        ref = args[2]
        if ref in repo.references:
            return ref
        raise subprocess.CalledProcessError(1, ["git", *args], "ref not found")

    # worktree list --porcelain
    if args[:3] == ["worktree", "list", "--porcelain"]:
        lines = []
        for _, path, shorthand in _list_worktree_objects(repo):
            lines.append(f"worktree {path}")
            if shorthand:
                lines.append(f"branch refs/heads/{shorthand}")
        return "\n".join(lines)

    # worktree prune
    if args[:2] == ["worktree", "prune"]:
        cleanup_orphan_worktrees()
        return ""

    # worktree remove -f <path>
    if args[:2] == ["worktree", "remove"] and "-f" in args:
        # Extract path (last argument that's not a flag)
        path_arg = [a for a in args if not a.startswith("-")][-1]
        path = Path(path_arg)
        name = path.name
        try:
            wt = repo.lookup_worktree(name)
            wt.prune(force=True)
        except (KeyError, Exception):
            # Best effort - may not exist
            pass
        return ""

    # branch -D <branch>
    if args[:2] == ["branch", "-D"] and len(args) == 3:
        branch = args[2]
        try:
            repo.branches.delete(branch)
        except KeyError:
            pass
        return ""

    # rev-parse [--verify] <ref>
    if args[0] == "rev-parse":
        if args[1] == "--verify" and len(args) > 2:
            ref = args[2]
        else:
            ref = args[1] if len(args) > 1 else "HEAD"
        try:
            obj = repo.revparse_single(ref)
            return str(obj.id)
        except KeyError as e:
            raise subprocess.CalledProcessError(1, ["git", *args], f"ref not found: {ref}") from e

    # push origin <refspec>
    if args[0] == "push" and len(args) >= 2:
        try:
            origin = repo.remotes["origin"]
        except KeyError as e:
            raise subprocess.CalledProcessError(1, ["git", *args], "remote 'origin' not found") from e

        # Handle deletion (push origin :refs/heads/branch)
        if args[-1].startswith(":"):
            ref_to_delete = args[-1][1:]  # Remove leading :
            try:
                origin.push([f":{ref_to_delete}"], callbacks=_get_auth_callbacks())
            except Exception as e:
                logger.debug(f"Push delete failed: {e}")
                # Not necessarily an error - branch may not exist on remote
            return ""

        # Handle normal push
        refspec = args[-1]
        try:
            origin.push([refspec], callbacks=_get_auth_callbacks())
        except Exception as e:
            raise subprocess.CalledProcessError(1, ["git", *args], f"push failed: {e}") from e
        return ""

    # fetch --prune origin
    if args[0] == "fetch":
        try:
            origin = repo.remotes["origin"]
        except KeyError:
            # No origin remote
            return ""
        prune = "--prune" in args
        try:
            origin.fetch(prune=prune, callbacks=_get_auth_callbacks())
        except Exception as e:
            raise subprocess.CalledProcessError(1, ["git", *args], f"fetch failed: {e}") from e
        return ""

    # Unsupported command
    raise NotImplementedError(
        f"Git command not supported via pygit2: {' '.join(args)}. "
        "Please file an issue at https://github.com/jr200/quarto-graft/issues"
    )


def list_worktree_paths() -> list[Path]:
    """Return a list of worktree paths registered with git."""
    repo = _get_repo()
    return [path for _, path, _ in _list_worktree_objects(repo)]


def is_worktree(path: Path) -> bool:
    """Check whether the given path is a registered git worktree."""
    path_resolved = path.resolve()
    return path_resolved in list_worktree_paths()


def worktrees_for_branch(branch: str) -> list[Path]:
    """Return paths of worktrees checked out at a given branch."""
    repo = _get_repo()
    paths: list[Path] = []
    for _, path, shorthand in _list_worktree_objects(repo):
        if shorthand == branch:
            paths.append(path)
    return paths


def fetch_origin() -> None:
    """Fetch and prune origin to ensure refs are up to date before building."""
    logger.info("[fetch] git fetch --prune origin")
    repo = _get_repo()
    try:
        origin = repo.remotes["origin"]
    except KeyError:
        logger.info("[fetch] No origin remote found; skipping fetch")
        return
    origin.fetch(prune=True, callbacks=_get_auth_callbacks())


def _resolve_ref(repo: pygit2.Repository, ref: str) -> pygit2.Object:
    """Resolve a ref/branch/oid to a git object."""
    # Local branch
    if ref in repo.branches.local:
        br = repo.branches[ref]
        return repo.get(br.target)

    # Remote branch (e.g., origin/feature)
    if ref in getattr(repo.branches, "remote", []):
        br = repo.branches.remote[ref]
        return repo.get(br.target)

    # Full ref name
    if ref in repo.references:
        return repo.get(repo.references[ref].target)

    # Try revparse on any other ref/oid
    try:
        return repo.revparse_single(ref)
    except Exception as e:
        raise RuntimeError(f"Reference not found: {ref} ({e})") from e


def create_worktree(ref: str, name: str) -> Path:
    """
    Create (or reuse) a git worktree for the given reference.
    """
    WORKTREES_CACHE.mkdir(exist_ok=True)
    wt_dir = WORKTREES_CACHE / name

    # Always recreate to ensure clean state
    if wt_dir.exists():
        remove_worktree(name, force=True)

    repo = _get_repo()
    target = _resolve_ref(repo, ref)

    # Add worktree (detached initially)
    repo.add_worktree(name, str(wt_dir))

    # Open the worktree repo and reset to target
    wt_repo = pygit2.Repository(str(wt_dir))
    wt_repo.reset(target.id, pygit2.GIT_RESET_HARD)

    # Try to set HEAD to branch if ref is a local branch
    branch_ref = None
    existing_heads = {sh for _, _, sh in _list_worktree_objects(repo) if sh}
    branch_name = None
    if ref in repo.branches.local:
        branch_name = ref
        branch_ref = f"refs/heads/{ref}"
    elif ref.startswith("refs/heads/"):
        branch_name = ref.split("/", 2)[-1]
        branch_ref = ref

    # If branch is already checked out in another worktree, stay detached
    if branch_name and branch_name in existing_heads:
        branch_ref = None

    if branch_ref:
        if branch_ref not in wt_repo.references:
            wt_repo.create_reference(branch_ref, target.id, force=True)
        wt_repo.set_head(branch_ref)
    else:
        wt_repo.set_head(target.id)

    wt_repo.checkout_head(strategy=pygit2.GIT_CHECKOUT_FORCE)
    wt_repo.state_cleanup()
    return wt_dir


def remove_worktree(worktree_name: str | Path, force: bool = False) -> None:
    """Remove a git worktree by name or absolute path."""
    wt_dir = Path(worktree_name)
    if not wt_dir.is_absolute():
        wt_dir = WORKTREES_CACHE / wt_dir
    if not wt_dir.exists():
        return

    try:
        repo = _get_repo()
        name = wt_dir.name
        # Attempt removal via git CLI first (cleans admin dir)
        try:
            run_git(["worktree", "remove", "-f", wt_dir.as_posix()], cwd=ROOT)
        except Exception:
            # Fallback to pygit2 prune if CLI removal failed
            try:
                wt = repo.lookup_worktree(name)
                wt.prune(force=True)
            except Exception:
                logger.debug(f"git worktree remove failed for {wt_dir}, will remove admin dir manually")

        # Ensure admin dir under .git/worktrees/<name> is gone
        admin_dir = Path(repo.path) / "worktrees" / name
        if admin_dir.exists():
            shutil.rmtree(admin_dir, ignore_errors=True)

        # Remove working directory itself
        if wt_dir.exists():
            shutil.rmtree(wt_dir)
        logger.debug(f"Removed worktree: {wt_dir}")
    except Exception:
        logger.warning(f"Failed to remove worktree via pygit2/git, removing manually: {wt_dir}")
        shutil.rmtree(wt_dir, ignore_errors=True)


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
    Ensure there is a git worktree for the given branch under .grafts-cache/<branch>.
    """

    if branch in TRUNK_BRANCHES:
        raise ValueError(f"{branch} is not a graft git-branch")

    wt_dir = WORKTREES_CACHE / branch

    if wt_dir.exists():
        logger.info(f"[get-worktree] Worktree directory already exists: {wt_dir}")
        return wt_dir

    logger.info(f"[get-worktree] Creating worktree for branch '{branch}' at {wt_dir} ...")

    repo = _get_repo()
    ref = None
    if branch in repo.branches.local:
        ref = f"refs/heads/{branch}"
        logger.info(f"[get-worktree] Using local branch '{branch}'")
    elif f"refs/remotes/origin/{branch}" in repo.references:
        ref = f"refs/remotes/origin/{branch}"
        logger.info(f"[get-worktree] Using remote branch 'origin/{branch}'")
    else:
        raise RuntimeError(f"Branch '{branch}' does not exist locally or on origin")

    WORKTREES_CACHE.mkdir(exist_ok=True)
    create_worktree(ref, branch)

    logger.info(f"[get-worktree] Worktree created: {wt_dir}")
    return wt_dir


def delete_worktree(branch: str) -> None:
    """Delete the git worktree under .grafts-cache/<branch>."""
    logger.info(f"[delete-worktree] Removing worktree for branch '{branch}'")
    remove_worktree(branch)


def cleanup_orphan_worktrees() -> list[Path]:
    """
    Remove directories under .grafts-cache/ that are no longer registered with git.

    Returns:
        List of successfully removed worktree paths.
        Failed removals are logged but don't stop the cleanup process.
    """
    WORKTREES_CACHE.mkdir(exist_ok=True)
    registered = set(list_worktree_paths())
    removed: list[Path] = []
    failures: list[tuple[Path, Exception]] = []

    for path in WORKTREES_CACHE.iterdir():
        if not path.is_dir():
            continue
        if path.resolve() in registered:
            continue

        logger.info(f"[cleanup-worktrees] Removing orphaned worktree dir {path}")
        try:
            shutil.rmtree(path)
            removed.append(path)
        except PermissionError as e:
            logger.warning(f"[cleanup-worktrees] Permission denied removing {path}: {e}")
            failures.append((path, e))
        except OSError as e:
            logger.warning(f"[cleanup-worktrees] Failed to remove {path}: {e}")
            failures.append((path, e))
        except Exception as e:
            logger.error(f"[cleanup-worktrees] Unexpected error removing {path}: {e}")
            failures.append((path, e))

    if failures:
        logger.warning(
            f"[cleanup-worktrees] Failed to remove {len(failures)} orphaned worktrees. "
            "You may need to manually delete them or check permissions."
        )

    return removed
