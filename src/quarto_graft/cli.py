from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.table import Table

from .branches import branch_to_key, destroy_graft, init_trunk, load_manifest, new_graft_branch, read_branches_list
from .build import build_branch, update_manifests
from .constants import (
    GRAFT_TEMPLATES_DIR,
    GRAFTS_CONFIG_FILE,
    PROTECTED_BRANCHES,
    ROOT,
    TEMPLATE_SOURCE_BUILTIN,
    TRUNK_ADDONS_DIR,
    TRUNK_TEMPLATES_DIR,
)
from .git_utils import has_commits, remove_worktree, run_git
from .quarto_config import apply_manifest
from .template_sources import TemplateSource, load_template_sources_from_config

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="quarto-graft",
    help="Quarto GitHub Pages branch graft tool",
    no_args_is_help=False,  # Changed to allow interactive mode
    invoke_without_command=True,
)
trunk_app = typer.Typer(help="Manage trunk (main documentation)", no_args_is_help=True)
graft_app = typer.Typer(help="Manage graft branches", no_args_is_help=True)

app.add_typer(trunk_app, name="trunk")
app.add_typer(graft_app, name="graft")

console = Console()


def require_trunk() -> None:
    """
    Check if the current directory is a quarto-graft trunk.
    Raises typer.Exit if grafts.yaml is not found.
    """
    if not GRAFTS_CONFIG_FILE.exists():
        console.print("[red]Error:[/red] grafts.yaml not found in current directory.")
        console.print("[yellow]Graft commands can only be run from within a quarto-graft trunk.[/yellow]")
        console.print(f"[dim]Current directory: {Path.cwd()}[/dim]")
        console.print("[dim]Please run this command from a directory containing grafts.yaml[/dim]")
        raise typer.Exit(code=1)


MAIN_MENU_COMMANDS = [
    questionary.Separator("=== Trunk Commands ==="),
    {"name": "trunk init - Initialize trunk (docs/) from a template", "value": "trunk init"},
    {"name": "trunk build - Build all graft branches and update trunk", "value": "trunk build"},
    {"name": "trunk lock - Update _quarto.yaml from grafts.lock", "value": "trunk lock"},
    questionary.Separator("=== Graft Commands ==="),
    {"name": "graft create - Create a new graft branch from a template", "value": "graft create"},
    {"name": "graft build - Build a single graft branch", "value": "graft build"},
    {"name": "graft list - List all graft branches", "value": "graft list"},
    {"name": "graft destroy - Remove a graft branch", "value": "graft destroy"},
]


def show_main_menu() -> str | None:
    """Show inline command selector."""
    return questionary.select(
        "Select a command:",
        choices=MAIN_MENU_COMMANDS,
        use_shortcuts=True,
        use_arrow_keys=True,
    ).ask()


def select_template(templates: list[str], template_type: str) -> str | None:
    """Show inline template selector."""
    if not templates:
        return None

    return questionary.select(
        f"Select {template_type} template:",
        choices=templates,
        use_shortcuts=True,
        use_arrow_keys=True,
    ).ask()


class TemplateValidator:
    """Helper class for template validation and listing with multi-source support."""

    def __init__(self, builtin_dir: Path, template_type: str):
        self.builtin_dir = builtin_dir
        self.template_type = template_type
        self._custom_sources: list[TemplateSource] | None = None

    def _get_custom_sources(self) -> list[TemplateSource]:
        """Lazy-load custom template sources from grafts.yaml."""
        if self._custom_sources is None:
            self._custom_sources = load_template_sources_from_config()
        return self._custom_sources

    def discover_templates(self) -> dict[str, Path]:
        """
        Return dictionary mapping template names to their paths.

        If there are duplicates, qualified names are used (e.g., 'builtin:markdown', 'custom-1:markdown').

        Returns:
            Dict[template_name, template_path]
        """
        # Collect templates from all sources
        templates_by_source: dict[str, dict[str, Path]] = {}

        # 1. Built-in templates (always available)
        if self.builtin_dir.exists():
            builtin_templates = {
                entry.name: entry
                for entry in self.builtin_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith("with-")
            }
            if builtin_templates:
                templates_by_source[TEMPLATE_SOURCE_BUILTIN] = builtin_templates

        # 2. Custom sources from grafts.yaml
        for source in self._get_custom_sources():
            template_names = source.discover_templates(self.template_type)
            source_templates = {}
            for name in template_names:
                path = source.get_template_path(name, self.template_type)
                if path:
                    source_templates[name] = path
            if source_templates:
                templates_by_source[source.source_name] = source_templates

        # 3. Merge templates and handle duplicates
        final_templates: dict[str, Path] = {}
        template_sources: dict[str, list[str]] = {}  # template_name -> [source_names]

        # First pass: collect which templates appear in which sources
        for source_name, templates in templates_by_source.items():
            for template_name in templates:
                if template_name not in template_sources:
                    template_sources[template_name] = []
                template_sources[template_name].append(source_name)

        # Second pass: add templates with qualification if needed
        for source_name, templates in templates_by_source.items():
            for template_name, template_path in templates.items():
                # If this template appears in multiple sources, qualify it
                if len(template_sources[template_name]) > 1:
                    qualified_name = f"{source_name}:{template_name}"
                    final_templates[qualified_name] = template_path
                else:
                    # Unique template, use simple name
                    final_templates[template_name] = template_path

        return final_templates

    def show_available_templates(self) -> None:
        """Display available templates in a formatted list."""
        templates = self.discover_templates()

        console.print(f"\n[bold]Available {self.template_type} templates:[/bold]")
        if templates:
            for name in sorted(templates.keys()):
                if ":" in name:
                    source, template = name.split(":", 1)
                    console.print(f"  • [cyan]{template}[/cyan] [dim]({source})[/dim]")
                else:
                    console.print(f"  • [cyan]{name}[/cyan]")
        else:
            console.print("  [dim]No templates found[/dim]")
        console.print()

    def select_template_interactive(self) -> tuple[str, Path]:
        """
        Show interactive template selector.

        Returns:
            Tuple of (template_name, template_path)
        """
        templates = self.discover_templates()

        if not templates:
            console.print(f"[red]Error:[/red] No {self.template_type} templates found")
            raise typer.Exit(code=1)

        # Create display choices
        choices = []
        for name in sorted(templates.keys()):
            if ":" in name:
                source, template = name.split(":", 1)
                display = f"{template} ({source})"
            else:
                display = name
            choices.append({"name": display, "value": name})

        selected = questionary.select(
            f"Select {self.template_type} template:",
            choices=choices,
            use_shortcuts=True,
            use_arrow_keys=True,
        ).ask()

        if not selected:
            console.print("[yellow]Template selection cancelled.[/yellow]")
            raise typer.Exit(code=1)

        return selected, templates[selected]

    def validate_template(self, template: str | None) -> tuple[str, Path]:
        """
        Validate template exists or show interactive selector.

        Returns:
            Tuple of (template_name, template_path)
        """
        templates = self.discover_templates()

        if template is None:
            # Launch interactive selector
            return self.select_template_interactive()

        # Check if template exists (exact match or unqualified match)
        if template in templates:
            return template, templates[template]

        # Check for partial match (unqualified name)
        matches = {name: path for name, path in templates.items() if name.endswith(f":{template}") or name == template}

        if len(matches) == 1:
            # Single match found
            name, path = next(iter(matches.items()))
            return name, path
        elif len(matches) > 1:
            # Multiple matches - ask user to qualify
            console.print(f"[red]Error:[/red] Template '{template}' is ambiguous. Please specify:")
            for name in sorted(matches.keys()):
                console.print(f"  • [cyan]{name}[/cyan]")
            raise typer.Exit(code=1)
        else:
            # No match
            console.print(f"[red]Error:[/red] Template '{template}' not found")
            self.show_available_templates()
            raise typer.Exit(code=1)


# Template validators for reuse
trunk_validator = TemplateValidator(TRUNK_TEMPLATES_DIR, "trunk")
graft_validator = TemplateValidator(GRAFT_TEMPLATES_DIR, "graft")


def _configure_logging() -> None:
    """Configure basic logging from env (QBB_LOG_LEVEL)."""
    level_name = os.getenv("QBB_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
    )


def _discover_grafts() -> dict[str, set[str]]:
    """Return branches from git, grafts.yaml, and grafts.lock."""
    git_branches = _git_local_branches()
    yaml_branches = _yaml_branches()
    manifest_branches = set(load_manifest().keys())

    def _filter(branches: set[str]) -> set[str]:
        return {b for b in branches if b not in PROTECTED_BRANCHES}

    return {
        "all": _filter(git_branches | yaml_branches | manifest_branches),
        "git": _filter(git_branches),
        "grafts.yaml": _filter(yaml_branches),
        "grafts.lock": _filter(manifest_branches),
    }


def _git_local_branches() -> set[str]:
    """
    Get local git branches.

    Returns:
        Set of branch names, or empty set if not in a git repository

    Raises:
        RuntimeError: If git operations fail unexpectedly
    """
    try:
        out = run_git(["for-each-ref", "refs/heads", "--format", "%(refname:short)"], cwd=ROOT)
        return {line.strip() for line in out.splitlines() if line.strip()}
    except subprocess.CalledProcessError as e:
        # Not in a git repo or no branches yet
        logger.debug(f"Could not list git branches: {e}")
        return set()
    except Exception as e:
        logger.error(f"Unexpected error listing git branches: {e}")
        console.print(f"[yellow]Warning:[/yellow] Could not list git branches: {e}")
        return set()


def _yaml_branches() -> set[str]:
    """
    Get branches defined in grafts.yaml.

    Returns:
        Set of branch names from grafts.yaml, or empty set if file doesn't exist
    """
    try:
        specs = read_branches_list()
        return {spec["branch"] for spec in specs if spec.get("branch")}
    except FileNotFoundError:
        logger.debug("grafts.yaml not found")
        return set()
    except Exception as e:
        logger.error(f"Error reading grafts.yaml: {e}")
        console.print(f"[red]Error:[/red] Failed to read grafts.yaml: {e}")
        return set()


# ============================================================================
# TRUNK COMMANDS
# ============================================================================

@trunk_app.command("list")
def trunk_list() -> None:
    """List available trunk templates."""
    trunk_validator.show_available_templates()


@trunk_app.command("init")
def trunk_init(
    name: str | None = typer.Argument(
        None,
        help="Name of the main site/project (e.g. 'My Documentation')",
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help="Template name under trunk-templates/",
    ),
    overwrite: bool | None = typer.Option(
        None,
        "--overwrite/--no-overwrite",
        help="Overwrite existing docs/ directory if it exists",
        show_default=False,
    ),
    with_addons: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--with",
        help="Include addon from trunk-templates/with-addons/NAME (can be used multiple times)",
    ),
) -> None:
    """Initialize the trunk (docs/) from a template."""
    if name is None:
        # Prompt for name
        name = questionary.text("Enter site/project name (e.g. 'My Documentation'):").ask()
        if not name:
            console.print("[red]Error:[/red] Site name cannot be empty")
            raise typer.Exit(code=1)

    template_name, template_path = trunk_validator.validate_template(template)

    # Check for conflicts in the current directory (files that template will write)
    from .constants import MAIN_DOCS
    top_level_targets = [MAIN_DOCS / entry.name for entry in template_path.iterdir()]
    conflicts = [p for p in top_level_targets if p.exists()]

    if conflicts:
        if overwrite is None:
            overwrite = questionary.confirm(
                f"The following already exist here: {', '.join(p.name for p in conflicts)}. Overwrite?",
                default=False
            ).ask()

        if not overwrite:
            console.print("[yellow]Cancelled.[/yellow] Use --overwrite flag to force overwrite.")
            raise typer.Exit(code=0)

    # Prompt for addons if not provided
    if with_addons is None:
        with_dir = TRUNK_TEMPLATES_DIR / TRUNK_ADDONS_DIR
        if with_dir.exists():
            available_addons = sorted([
                entry.name for entry in with_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            ])
            if available_addons:
                add_addons = questionary.confirm(
                    "Would you like to add any addons?",
                    default=False
                ).ask()

                if add_addons:
                    selected_addons = questionary.checkbox(
                        "Select addons to include:",
                        choices=available_addons
                    ).ask()
                    with_addons = selected_addons if selected_addons else []
                else:
                    with_addons = []
            else:
                with_addons = []
        else:
            with_addons = []

    docs_dir = init_trunk(
        name=name,
        template=template_path,
        overwrite=overwrite,
        with_addons=with_addons or [],
    )
    console.print(f"[green]✓[/green] Trunk initialized from template '{template_name}' at: {docs_dir}")
    console.print(f"[dim]Site name:[/dim] {name}")
    if with_addons:
        console.print(f"  with addons: {', '.join(with_addons)}")


@trunk_app.command("build")
def trunk_build(
    no_update_manifest: bool = typer.Option(
        False,
        "--no-update-manifest",
        help="Do not update grafts.lock",
    ),
) -> None:
    """Build all graft branches and update trunk."""
    results = update_manifests(update_manifest=not no_update_manifest)
    branch_specs = read_branches_list()

    console.print("\n[bold]Manifest summary:[/bold]")
    for spec in branch_specs:
        b = spec["branch"]
        r = results.get(b)
        if not r:
            continue
        status_color = "green" if r.status == "success" else "yellow"
        console.print(f"  [bold]{b}:[/bold] [{status_color}]{r.status}[/{status_color}] ({len(r.exported_dest_paths)} files)")

    apply_manifest()
    console.print("\n[green]✓[/green] Trunk build complete")


@trunk_app.command("lock")
def trunk_lock() -> None:
    """Update _quarto.yaml from grafts.lock."""
    apply_manifest()
    console.print("[green]✓[/green] Updated _quarto.yaml")


# ============================================================================
# GRAFT COMMANDS
# ============================================================================

@graft_app.command("create")
def graft_create(
    name: str | None = typer.Argument(
        None,
        help="Name of the new graft branch (e.g. demo)",
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help="Template name under graft-templates/",
    ),
    collar: str | None = typer.Option(
        None,
        "--collar",
        "-c",
        help="Attachment point in trunk _quarto.yaml (e.g. main, notes, bugs)",
    ),
    branch_name: str | None = typer.Option(
        None,
        "--branch-name",
        help="Git branch name to create (default: graft/<name>)",
    ),
    push: bool = typer.Option(
        True,
        "--push/--no-push",
        help="Push the new branch to origin",
    ),
) -> None:
    """Create a new graft branch from a template."""
    if not has_commits():
        console.print(
            "[red]Error:[/red] Cannot create a graft because the repository has no commits yet.\n"
            "Commit your trunk files first, then retry."
        )
        raise typer.Exit(code=1)

    require_trunk()

    if name is None:
        # Prompt for name
        name = questionary.text("Enter graft branch name (e.g. demo):").ask()
        if not name:
            console.print("[red]Error:[/red] NAME cannot be empty")
            raise typer.Exit(code=1)

    template_name, template_path = graft_validator.validate_template(template)

    # Prompt for collar if not provided
    if collar is None:
        from .quarto_config import list_available_collars
        try:
            available_collars = list_available_collars()
            if not available_collars:
                console.print("[yellow]Warning:[/yellow] No collars found in _quarto.yaml. Using 'main' as default.")
                collar = "main"
            elif len(available_collars) == 1:
                collar = available_collars[0]
                console.print(f"[dim]Using collar:[/dim] {collar}")
            else:
                collar = questionary.select(
                    "Select attachment point (collar):",
                    choices=available_collars
                ).ask()
                if not collar:
                    console.print("[red]Error:[/red] Collar selection is required")
                    raise typer.Exit(code=1)
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Could not read collars from _quarto.yaml: {e}")
            collar = questionary.text("Enter collar name:", default="main").ask()
            if not collar:
                collar = "main"

    # Prompt for custom branch name if not provided
    if branch_name is None:
        default_branch = f"graft/{name}"
        use_custom = questionary.confirm(
            f"Use default branch name '{default_branch}'?",
            default=True
        ).ask()

        if not use_custom:
            branch_name = questionary.text(
                "Enter custom branch name:",
                default=default_branch
            ).ask()
            if not branch_name:
                branch_name = default_branch
        else:
            branch_name = default_branch

    git_branch_name = branch_name

    wt_dir, trunk_instructions = new_graft_branch(
        name=name,
        template=template_path,
        collar=collar,
        push=push,
        branch_name=git_branch_name,
    )

    # Clean up the temporary worktree created during graft initialization
    # The build process will create its own temporary worktrees as needed
    try:
        branch_key = branch_to_key(name)
        remove_worktree(branch_key, force=True)
        # Prune any stale worktree references from git
        run_git(["worktree", "prune"], cwd=ROOT)
        logger.debug(f"Cleaned up temporary worktree for {branch_key}")
    except Exception as e:
        logger.debug(f"Failed to clean up worktree: {e}")

    console.print(f"[green]✓[/green] New orphan graft branch '{git_branch_name}' created from template '{template_name}'")
    console.print(f"[bold]Collar:[/bold] {collar}")

    # Display trunk instructions if present
    if trunk_instructions:
        console.print("\n[yellow]═══════════════════════════════════════════════════════════════[/yellow]")
        console.print("[yellow bold]TRUNK OWNER INSTRUCTIONS[/yellow bold]")
        console.print("[yellow]═══════════════════════════════════════════════════════════════[/yellow]\n")
        console.print(trunk_instructions)
        console.print("\n[yellow]═══════════════════════════════════════════════════════════════[/yellow]")


@graft_app.command("build")
def graft_build(
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help="Branch name (e.g. chapter1)",
    ),
    no_update_manifest: bool = typer.Option(
        False,
        "--no-update-manifest",
        help="Do not update grafts.lock",
    ),
) -> None:
    """Build a single graft branch."""
    require_trunk()

    if branch is None:
        # Prompt for branch - use select if branches exist, otherwise text input
        found_branches = _discover_grafts()
        choices = sorted(found_branches.get("all", []))
        if choices:
            branch = questionary.select(
                "Select graft branch to build:",
                choices=choices,
                use_shortcuts=True,
                use_arrow_keys=True,
            ).ask()
        else:
            branch = questionary.text("Enter branch name (e.g. chapter1):").ask()

        if not branch:
            console.print("[red]Error:[/red] Branch name required")
            raise typer.Exit(code=1)

    res = build_branch(branch, update_manifest=not no_update_manifest)

    status_color = "green" if res.status == "success" else "yellow"
    console.print(f"[bold]{res.branch}[/bold]")
    console.print(f"  Status: [{status_color}]{res.status}[/{status_color}]")
    console.print(f"  Files exported: {len(res.exported_dest_paths)}")
    console.print(f"  HEAD SHA: {res.head_sha}")
    console.print(f"  Last good SHA: {res.last_good_sha}")


@graft_app.command("list")
def graft_list() -> None:
    """List all graft branches."""
    require_trunk()

    found_branches = _discover_grafts()
    all_branches = sorted(found_branches.get("all", []))

    if not all_branches:
        console.print("[dim]No graft branches found.[/dim]")
        return

    table = Table(title="Graft Branches")
    table.add_column("Branch", style="cyan")
    table.add_column("In Git", justify="center")
    table.add_column("In grafts.yaml", justify="center")
    table.add_column("In grafts.lock", justify="center")

    git_branches = found_branches.get("git", set())
    yaml_branches = found_branches.get("grafts.yaml", set())
    lock_branches = found_branches.get("grafts.lock", set())

    for branch in all_branches:
        table.add_row(
            branch,
            "✓" if branch in git_branches else "—",
            "✓" if branch in yaml_branches else "—",
            "✓" if branch in lock_branches else "—",
        )

    console.print(table)


@graft_app.command("destroy")
def graft_destroy(
    branch: str | None = typer.Argument(
        None,
        help="Git branch name to delete (e.g. graft/chapter1)",
    ),
    keep_remote: bool = typer.Option(
        False,
        "--keep-remote",
        help="Do not delete the remote branch on origin",
    ),
) -> None:
    """Remove a graft branch locally, remotely, and from config."""
    require_trunk()

    destroyable = _discover_grafts()

    if branch is None:
        choices = sorted(destroyable.get("all", []))
        if choices:
            branch = questionary.select(
                "Select graft branch to destroy:",
                choices=choices,
                use_shortcuts=True,
                use_arrow_keys=True,
            ).ask()
        else:
            console.print("[dim]No graft branches found to destroy.[/dim]")
            raise typer.Exit(code=1)

        if not branch:
            console.print("[red]Error:[/red] Branch name required")
            raise typer.Exit(code=1)

    if branch in PROTECTED_BRANCHES:
        console.print(f"[red]Error:[/red] '{branch}' is protected and cannot be destroyed")
        raise typer.Exit(code=1)

    all_branches = sorted(destroyable.get("all", []))
    if branch not in all_branches:
        continue_anyway = questionary.confirm(
            f"Branch '{branch}' not found in tracked branches. Continue anyway?",
            default=False
        ).ask()
        if not continue_anyway:
            raise typer.Exit(code=1)

    summary = destroy_graft(branch, delete_remote=not keep_remote)

    console.print(f"\n[bold]Destruction summary for '{branch}':[/bold]")

    if summary["config_removed"]:
        console.print(f"  [green]✓[/green] Removed from grafts.yaml: {', '.join(summary['config_removed'])}")
    else:
        console.print("  [dim]—[/dim] Branch not found in grafts.yaml")

    if summary["worktrees_removed"]:
        console.print(f"  [green]✓[/green] Removed {len(summary['worktrees_removed'])} worktree(s):")
        for wt in summary["worktrees_removed"]:
            console.print(f"    • {wt}")
    else:
        console.print("  [dim]—[/dim] No worktrees removed")

    if summary["manifest_removed"]:
        console.print(f"  [green]✓[/green] Pruned from grafts.lock: {', '.join(summary['manifest_removed'])}")

    console.print("  [green]✓[/green] Deleted local branch")

    if not keep_remote:
        console.print("  [green]✓[/green] Attempted remote delete on origin")

    console.print("\n[yellow]Note:[/yellow] Please regenerate the main docs/navigation with: [bold]quarto-graft trunk build[/bold]")


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context) -> None:
    """Main callback - launches interactive mode if no command given."""
    _configure_logging()

    # If a subcommand was invoked, do nothing here
    if ctx.invoked_subcommand is not None:
        return

    # Launch interactive menu
    selected_command = show_main_menu()

    if selected_command is None:
        console.print("[dim]Exited.[/dim]")
        raise typer.Exit(code=0)

    # Parse and execute the selected command
    parts = selected_command.split()
    if len(parts) == 2:
        group, command = parts

        # Route to appropriate command handler
        if group == "trunk" and command == "init":
            trunk_init(name=None, template=None, overwrite=None, with_addons=None)
        elif group == "trunk" and command == "build":
            trunk_build(no_update_manifest=False)
        elif group == "trunk" and command == "lock":
            trunk_lock()
        elif group == "graft" and command == "create":
            graft_create(name=None, template=None, collar=None, branch_name=None, push=True)
        elif group == "graft" and command == "build":
            # Prompt for branch - use select if branches exist, otherwise text input
            found_branches = _discover_grafts()
            choices = sorted(found_branches.get("all", []))
            if choices:
                branch = questionary.select(
                    "Select graft branch to build:",
                    choices=choices,
                    use_shortcuts=True,
                    use_arrow_keys=True,
                ).ask()
            else:
                branch = questionary.text("Enter branch name (e.g. chapter1):").ask()

            if not branch:
                console.print("[red]Error:[/red] Branch name required")
                raise typer.Exit(code=1)
            graft_build(branch=branch, no_update_manifest=False)
        elif group == "graft" and command == "list":
            graft_list()
        elif group == "graft" and command == "destroy":
            # Offer interactive selection of existing graft branches
            found_branches = _discover_grafts()
            choices = sorted(found_branches.get("all", []))
            if not choices:
                console.print("[dim]No graft branches found to destroy.[/dim]")
                raise typer.Exit(code=0)

            branch = questionary.select(
                "Select graft branch to destroy:",
                choices=choices,
                use_shortcuts=True,
                use_arrow_keys=True,
            ).ask()

            if not branch:
                console.print("[red]Error:[/red] Branch name required")
                raise typer.Exit(code=1)
            graft_destroy(branch=branch, keep_remote=False)


def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
