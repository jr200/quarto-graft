from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from typing import Dict, NoReturn, Set, TextIO

from .branches import new_graft_branch, read_branches_list
from .build import update_manifests, build_branch
from .git_utils import cleanup_orphan_worktrees, ensure_worktree, run_git
from .quarto_config import apply_manifest
from .branches import destroy_graft, load_manifest
from .constants import PROTECTED_BRANCHES, ROOT


def _add_manifest_flag(parser: argparse.ArgumentParser) -> None:
    """Add common --no-update-manifest flag to a parser."""
    parser.add_argument(
        "--no-update-manifest",
        action="store_true",
        help="Do not update grafts.lock",
    )


def _configure_logging() -> None:
    """Configure basic logging from env (QBB_LOG_LEVEL)."""
    level_name = os.getenv("QBB_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
    )


def build_graft_main() -> None:
    parser = argparse.ArgumentParser(description="Build a single graft branch")
    parser.add_argument("--branch", required=True, help="Branch name (e.g. chapter1)")
    _add_manifest_flag(parser)
    args = parser.parse_args()

    res = build_branch(args.branch, update_manifest=not args.no_update_manifest)
    print(
        f"[{res.branch}] status={res.status}, files={len(res.exported_dest_paths)}, "
        f"head={res.head_sha}, last_good={res.last_good_sha}"
    )


def build_trunk_main() -> None:
    parser = argparse.ArgumentParser(description="Collate grafts and build trunk")
    _add_manifest_flag(parser)
    args = parser.parse_args()

    results = update_manifests(update_manifest=not args.no_update_manifest)
    branch_specs = read_branches_list()
    print("Manifest summary:")
    for spec in branch_specs:
        b = spec["branch"]
        r = results.get(b)
        if not r:
            continue
        print(f"  {b}: {r.status} ({len(r.exported_dest_paths)} files)")

    apply_manifest()

def graft_get_main() -> None:
    """Fetch (worktree) an existing graph into to the local .grafts folder."""
    parser = argparse.ArgumentParser(
        description="Prepare/ensure a git worktree for a branch under .grafts/<branch>."
    )
    parser.add_argument("branch", help="Git Branch name (e.g. chapter1)")
    args = parser.parse_args()

    wt_dir = ensure_worktree(args.branch)
    print(f"Worktree ready at: {wt_dir}")
    print(f"To work on it, run: cd {wt_dir}")


def new_graft_main() -> None:
    """Create a new graft branch from a named template."""
    available_templates = _discover_templates()

    class _TemplateParser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            self.print_help(sys.stderr)
            _print_templates_help(available_templates)
            self.exit(2, f"\n{self.prog}: error: {message}\n")

    parser = _TemplateParser(
        description="Create a new graft branch from a named template.",
        formatter_class=lambda prog: argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=28),
    )
    parser.add_argument(
        "name",
        help="Name of the new graft branch (e.g. demo).",
    )
    parser.add_argument(
        "-t",
        "--template",
        required=True,
        metavar="T",
        help=_template_help(available_templates),
    )
    parser.add_argument(
        "--branch-name",
        metavar="GIT_BRANCH",
        help="Git branch name to create (default: graft/<name>).",
    )
    parser.add_argument(
        "--push",
        action="store_false",
        help="Push the new branch to origin.",
    )
    args = parser.parse_args()

    if available_templates and args.template not in available_templates:
        parser.error(f"Template '{args.template}' not found under templates/")

    branch_name = args.branch_name or f"graft/{args.name}"

    wt_dir = new_graft_branch(
        name=args.name,
        template=args.template,
        push=args.push,
        branch_name=branch_name,
    )

    print(f"New orphan graft branch '{branch_name}' created from template '{args.template}'.")
    print(f"Git Worktree ready at: {wt_dir}")


def _discover_templates() -> list[str]:
    """Return sorted list of template directory names under templates/."""
    templates_dir = ROOT / "templates"
    if not templates_dir.exists():
        return []
    return sorted(
        entry.name
        for entry in templates_dir.iterdir()
        if entry.is_dir()
    )


def _template_help(templates: list[str]) -> str:
    base = "Template name under templates/."
    if templates:
        return base
    return f"{base} (no templates discovered under {ROOT / 'templates'})."


def _print_templates_help(templates: list[str]) -> None:
    header = "\nAvailable templates (templates/):"
    if not templates:
        sys.stderr.write(f"{header}\n  - none found under {ROOT / 'templates'}\n")
        return
    lines = "\n".join(f"  - {name}" for name in templates)
    sys.stderr.write(f"{header}\n{lines}\n")


def _discover_grafts() -> Dict[str, Set[str]]:
    """Return branches from git, grafts.yaml, and grafts.lock."""
    git_branches = _git_local_branches()
    yaml_branches = _yaml_branches()
    manifest_branches = set(load_manifest().keys())

    def _filter(branches: Set[str]) -> Set[str]:
        return {b for b in branches if b not in PROTECTED_BRANCHES}

    return {
        "all": _filter(git_branches | yaml_branches | manifest_branches),
        "git": _filter(git_branches),
        "grafts.yaml": _filter(yaml_branches),
        "grafts.lock": _filter(manifest_branches),
    }


def _git_local_branches() -> Set[str]:
    try:
        out = run_git(["for-each-ref", "refs/heads", "--format", "%(refname:short)"], cwd=ROOT)
    except subprocess.CalledProcessError:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _yaml_branches() -> Set[str]:
    try:
        specs = read_branches_list()
    except FileNotFoundError:
        return set()
    return {spec["branch"] for spec in specs if spec.get("branch")}


def _print_found_branches_help(branches: Dict[str, Set[str]], stream: TextIO = sys.stderr) -> None:
    all_branches = sorted(branches.get("all", []))
    header = "\nFound branches (excluding protected):"
    if not all_branches:
        stream.write(f"{header}\n  - none found\n")
        return
    stream.write(f"{header}\n")
    stream.write("\n".join(f"  - {b}" for b in all_branches))
    stream.write("\n\n")


def cleanup_worktrees_main() -> None:
    """Remove orphaned worktree directories under .grafts/."""
    parser = argparse.ArgumentParser(description="Cleanup orphaned worktrees under .grafts/")
    parser.parse_args()
    removed = cleanup_orphan_worktrees()
    if removed:
        print(f"Removed {len(removed)} orphaned worktree(s)")
        for p in removed:
            print(f"  {p}")
    else:
        print("No orphaned worktrees found")


def graft_destroy_main() -> None:
    """Remove a graft branch locally, remotely, and from config."""
    destroyable = _discover_grafts()

    class _DestroyParser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            self.print_help(sys.stderr)
            _print_found_branches_help(destroyable)
            self.exit(2, f"{self.prog}: error: {message}\n")

    parser = _DestroyParser(
        description="Destroy a graft branch and clean references.",
        formatter_class=lambda prog: argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=28),
    )
    parser.add_argument("branch", metavar="GIT_BRANCH", help="Git branch name to delete (e.g. graft/chapter1)")
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="Do not delete the remote branch on origin.",
    )
    args = parser.parse_args()

    if args.branch in PROTECTED_BRANCHES:
        parser.error(f"'{args.branch}' is protected and cannot be destroyed")

    summary = destroy_graft(args.branch, delete_remote=not args.keep_remote)

    if summary["config_removed"]:
        print(f"Removed from grafts.yaml: {', '.join(summary['config_removed'])}")
    else:
        print("Branch not found in grafts.yaml.")

    if summary["worktrees_removed"]:
        print("Removed worktrees:")
        for wt in summary["worktrees_removed"]:
            print(f"  {wt}")
    else:
        print("No worktrees removed.")

    if summary["manifest_removed"]:
        print(f"Pruned from grafts.lock: {', '.join(summary['manifest_removed'])}")

    print("Deleted local branch (if present).")
    if not args.keep_remote:
        print("Attempted remote delete on origin (ignore message above if branch was missing).")
    print("Please regenerate the main docs/navigation (e.g., `uv run trunk-build`).")


def graft_list_main() -> None:
    """List grafts"""
    parser = argparse.ArgumentParser(
        description="List grafts",
        formatter_class=lambda prog: argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=28),
    )
    parser.parse_args()

    found_branches = _discover_grafts()
    if not found_branches.get("all"):
        print("No graft branches found.")
        return
    _print_found_branches_help(found_branches, stream=sys.stdout)


def main() -> None:
    """Main entrypoint for debugging from VSCode."""
    if len(sys.argv) < 2:
        print("Usage: python -m quarto_graft.cli <command> [args...]")
        print("\nCommands:")
        print("  graft-build           Build a single graft branch")
        print("  trunk-lock            Update docs/_quarto.yaml")
        print("  trunk-build           Build all graft branches and update quarto")
        print("  graft-get          Ensure a worktree for a branch")
        print("  graft-new             Create a new graft branch")
        print("  graft-cleanup-orphans Remove orphaned .grafts dirs")
        print("  graft-destroy         Delete a graft branch locally/remotely and clean config")
        print("  graft-list            List graft branches eligible for destruction")
        sys.exit(1)

    command = sys.argv[1]
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    commands = {
        "graft-build": build_graft_main,
        "trunk-build": build_trunk_main,
        "trunk-lock": lambda: apply_manifest(),
        "graft-get": graft_get_main,
        "graft-new": new_graft_main,
        "graft-cleanup-orphans": cleanup_worktrees_main,
        "graft-destroy": graft_destroy_main,
        "graft-list": graft_list_main,
    }

    if command in commands:
        commands[command]()
    else:
        print(f"Unknown command: {command}")
        print("Run without arguments to see available commands.")
        sys.exit(1)


if __name__ == "__main__":
    main()
