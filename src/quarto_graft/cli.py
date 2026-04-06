from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import constants
from .archive import archive_graft, restore_graft
from .branches import branch_to_key, destroy_graft, init_trunk, load_manifest, new_graft_branch, read_branches_list
from .build import BuildResult, build_branch, resolve_head_sha, update_manifests
from .cache import (
    cache_status,
    clear_cache,
    fix_navigation,
    fix_search_index,
    propagate_nav_to_cache,
    update_cache_after_render,
)
from .constants import (
    GRAFT_TEMPLATES_DIR,
    PROTECTED_BRANCHES,
    TEMPLATE_SOURCE_BUILTIN,
    TRUNK_ADDONS_DIR,
    TRUNK_TEMPLATES_DIR,
)
from .git_utils import fetch_origin, has_commits, list_local_branches, prune_worktrees, remove_worktree
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


def _display_trunk_instructions(instructions: str, title: str = "TRUNK OWNER INSTRUCTIONS") -> None:
    """Display trunk instructions with formatted borders."""
    console.print("\n[yellow]═══════════════════════════════════════════════════════════════[/yellow]")
    console.print(f"[yellow bold]{title}[/yellow bold]")
    console.print("[yellow]═══════════════════════════════════════════════════════════════[/yellow]\n")
    console.print(instructions)
    console.print("\n[yellow]═══════════════════════════════════════════════════════════════[/yellow]")


def require_trunk() -> None:
    """
    Check if the current directory is a quarto-graft trunk.
    Raises typer.Exit if grafts.yaml is not found.
    """
    if not constants.GRAFTS_CONFIG_FILE.exists():
        console.print("[red]Error:[/red] grafts.yaml not found in current directory.")
        console.print("[yellow]Graft commands can only be run from within a quarto-graft trunk.[/yellow]")
        console.print(f"[dim]Current directory: {Path.cwd()}[/dim]")
        console.print("[dim]Please run this command from a directory containing grafts.yaml[/dim]")
        raise typer.Exit(code=1)


MAIN_MENU_COMMANDS = [
    questionary.Separator("=== General ==="),
    {"name": "status - Show build status of all grafts", "value": "status"},
    questionary.Separator("=== Trunk Commands ==="),
    {"name": "trunk init - Initialize trunk (docs/) from a template", "value": "trunk init"},
    {"name": "trunk build - Build all graft branches and update trunk", "value": "trunk build"},
    {"name": "trunk lock - Update _quarto.yaml from grafts.lock", "value": "trunk lock"},
    {"name": "trunk cache update - Capture rendered pages into cache", "value": "trunk cache update"},
    {"name": "trunk cache clear - Clear the render cache", "value": "trunk cache clear"},
    {"name": "trunk cache status - Show cache status", "value": "trunk cache status"},
    {"name": "trunk release - Create a GitHub release", "value": "trunk release"},
    questionary.Separator("=== Graft Commands ==="),
    {"name": "graft create - Create a new graft branch from a template", "value": "graft create"},
    {"name": "graft build - Build a single graft branch", "value": "graft build"},
    {"name": "graft list - List all graft branches", "value": "graft list"},
    {"name": "graft destroy - Remove a graft branch", "value": "graft destroy"},
    {"name": "graft archive - Pre-render graft for faster trunk builds", "value": "graft archive"},
    {"name": "graft restore - Remove pre-rendered content", "value": "graft restore"},
]


def show_main_menu() -> str | None:
    """Show inline command selector."""
    return questionary.select(
        "Select a command:",
        choices=MAIN_MENU_COMMANDS,  # type: ignore[arg-type]
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


def _configure_logging(log_level: str | None = None) -> None:
    """Configure basic logging from parameter, env (QBB_LOG_LEVEL), or default to INFO."""
    if log_level is None:
        log_level = os.getenv("QBB_LOG_LEVEL", "INFO")

    level_name = log_level.upper()
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
        branches = list_local_branches(cwd=constants.ROOT)
        return set(branches)
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
    top_level_targets = [constants.MAIN_DOCS / entry.name for entry in template_path.iterdir()]
    conflicts = [p for p in top_level_targets if p.exists()]

    if conflicts:
        if overwrite is None:
            overwrite = questionary.confirm(
                f"The following already exist here: {', '.join(p.name for p in conflicts)}. Overwrite?", default=False
            ).ask()

        if not overwrite:
            console.print("[yellow]Cancelled.[/yellow] Use --overwrite flag to force overwrite.")
            raise typer.Exit(code=0)

    # Prompt for addons if not provided
    if with_addons is None:
        with_dir = TRUNK_TEMPLATES_DIR / TRUNK_ADDONS_DIR
        if with_dir.exists():
            available_addons = sorted(
                [entry.name for entry in with_dir.iterdir() if entry.is_dir() and not entry.name.startswith(".")]
            )
            if available_addons:
                add_addons = questionary.confirm("Would you like to add any addons?", default=False).ask()

                if add_addons:
                    selected_addons = questionary.checkbox("Select addons to include:", choices=available_addons).ask()
                    with_addons = selected_addons if selected_addons else []
                else:
                    with_addons = []
            else:
                with_addons = []
        else:
            with_addons = []

    docs_dir, addon_instructions = init_trunk(
        name=name,
        template=template_path,
        overwrite=bool(overwrite),
        with_addons=with_addons or [],
    )
    console.print(f"[green]✓[/green] Trunk initialized from template '{template_name}' at: {docs_dir}")
    console.print(f"[dim]Site name:[/dim] {name}")
    if with_addons:
        console.print(f"  with addons: {', '.join(with_addons)}")

    # Display addon instructions if present
    for addon_name, instructions in addon_instructions:
        _display_trunk_instructions(instructions, title=f"TRUNK OWNER INSTRUCTIONS - {addon_name.upper()} ADDON")


def _print_build_summary(results: dict[str, BuildResult], branch_specs: list) -> None:
    """Print a formatted build summary table."""
    if not results:
        return

    table = Table()
    table.add_column("Graft", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Files", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Details", style="dim")

    status_colors = {"ok": "green", "skipped": "blue", "fallback": "yellow", "broken": "red"}

    for spec in branch_specs:
        b = spec["branch"] if isinstance(spec, dict) else spec
        r = results.get(b)
        if not r:
            continue

        color = status_colors.get(r.status, "white")
        status_str = f"[{color}]{r.status}[/{color}]"
        files_str = str(len(r.exported_relpaths)) if r.status != "skipped" else "—"
        time_str = f"{r.duration_secs:.1f}s" if r.duration_secs > 0 else "—"

        details = ""
        if r.status == "skipped":
            details = "unchanged"
        elif r.error_message:
            msg = r.error_message
            if len(msg) > 60:
                msg = msg[:57] + "..."
            details = msg
        elif r.prerendered:
            details = "pre-rendered"
        elif r.cached_pages:
            total = len(r.page_hashes) if r.page_hashes else 0
            details = f"cache: {len(r.cached_pages)}/{total} pages"

        name = spec["name"] if isinstance(spec, dict) else spec
        table.add_row(name, status_str, files_str, time_str, details)

    console.print(table)


def _write_build_state(
    results: dict[str, BuildResult],
    branch_specs: list,
) -> None:
    """Persist per-page hashes to a transient file for ``trunk cache update``.

    Only grafts with page_hashes (non-archived, non-broken) are included.
    The file lives under dist/ and is overwritten each build.
    """
    state: dict[str, dict] = {}
    for _branch_name, res in results.items():
        if res.page_hashes:
            state[res.branch_key] = {
                "page_hashes": res.page_hashes,
                "cached_pages": res.cached_pages or [],
            }
    constants.BUILD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    constants.BUILD_STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_build_state() -> dict[str, dict]:
    """Load the transient build state written by ``trunk build``."""
    if not constants.BUILD_STATE_FILE.exists():
        return {}
    try:
        return json.loads(constants.BUILD_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


@trunk_app.command("build")
def trunk_build(
    no_update_manifest: bool = typer.Option(
        False,
        "--no-update-manifest",
        help="Do not update grafts.lock",
    ),
    jobs: int = typer.Option(
        1,
        "--jobs",
        "-j",
        help="Number of parallel build workers",
    ),
    only: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--only",
        help="Only build these grafts (by name, repeatable)",
    ),
    skip: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--skip",
        help="Skip these grafts (by name, repeatable)",
    ),
    changed: bool = typer.Option(
        False,
        "--changed",
        help="Only rebuild grafts with new commits since last build",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass render cache and export all pages as .qmd",
    ),
) -> None:
    """Build all graft branches and update trunk."""
    require_trunk()

    branch_specs = read_branches_list()
    only_set = set(only) if only else None
    skip_set = set(skip) if skip else None

    # Count grafts that will be processed (after --only/--skip filtering)
    filtered_count = sum(
        1
        for spec in branch_specs
        if (not only_set or spec["name"] in only_set) and (not skip_set or spec["name"] not in skip_set)
    )

    if filtered_count == 0:
        console.print("[yellow]No grafts to build after filtering.[/yellow]")
        return

    # Display build configuration
    build_info = []
    if only:
        build_info.append(f"only: {', '.join(only)}")
    if skip:
        build_info.append(f"skip: {', '.join(skip)}")
    if changed:
        build_info.append("changed only")
    if jobs > 1:
        build_info.append(f"{jobs} workers")

    desc = f"Building {filtered_count} graft(s)"
    if build_info:
        desc += f" ({', '.join(build_info)})"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(desc, total=filtered_count)

        def on_complete(result: BuildResult) -> None:
            progress.advance(task)

        results = update_manifests(
            update_manifest=not no_update_manifest,
            jobs=jobs,
            only=only_set,
            skip=skip_set,
            changed_only=changed,
            on_complete=on_complete,
            use_cache=not no_cache,
        )

    console.print()
    _print_build_summary(results, branch_specs)

    # Persist per-page hashes to a transient build state file for `trunk cache update`.
    # This keeps the large page_hashes out of grafts.lock.
    _write_build_state(results, branch_specs)

    apply_manifest()
    console.print("[green]✓[/green] Trunk build complete")


@trunk_app.command("lock")
def trunk_lock() -> None:
    """Update _quarto.yaml from grafts.lock."""
    apply_manifest()
    console.print("[green]✓[/green] Updated _quarto.yaml")


@trunk_app.command("release")
def trunk_release(
    workflow: str = typer.Option(
        "quarto-graft-build-publish.yaml",
        "--workflow",
        help="GitHub Actions workflow file to trigger after release",
    ),
    trigger: bool = typer.Option(
        False,
        "--trigger",
        help="Trigger the build workflow after release",
    ),
    edit_release: bool = typer.Option(
        False,
        "--edit-release",
        "-e",
        help="Open release notes in $EDITOR before publishing",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Create a GitHub release with auto-incremented patch version.

    Generates release notes from main branch PRs and per-graft commit logs.
    Uses a two-tag system (released/<key>, releasing/<key>) for safe rollback.
    """
    from .release import (
        build_release_notes,
        compute_next_version,
        create_release,
        edit_release_notes,
        get_latest_release_tag,
        promote_tags,
        rollback_staging_tags,
        stage_graft_tags,
        trigger_workflow,
    )

    require_trunk()

    # 1. Determine versions
    console.print("[dim]Fetching latest release info...[/dim]")
    fetch_origin()
    current = get_latest_release_tag()
    next_tag = compute_next_version(current)
    console.print(f"Current: [bold]{current or '<none>'}[/bold]  →  Next: [bold green]{next_tag}[/bold green]")

    # 2. Build release notes
    console.print("[dim]Generating release notes...[/dim]")
    notes = build_release_notes(current, next_tag)

    # 2b. Open in editor if requested
    if edit_release:
        edited = edit_release_notes(notes)
        if edited is None:
            console.print("[yellow]Empty release notes — aborted.[/yellow]")
            raise typer.Exit(code=0)
        notes = edited

    # 3. Preview
    console.print()
    console.rule(f"Release notes for {next_tag}")
    console.print(notes or "[dim]No changes found.[/dim]")
    console.rule()
    console.print()

    # 4. Confirm
    if not yes:
        import questionary

        confirm = questionary.confirm(f"Create release {next_tag}?", default=False).ask()
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    # 5. Stage graft HEADs
    staged_keys = stage_graft_tags()

    # 6. Create release
    try:
        console.print(f"[dim]Creating release {next_tag} on main...[/dim]")
        url = create_release(next_tag, notes)
        console.print(f"[green]✓[/green] Release created: {url}")
    except Exception as e:
        console.print(f"[red]Release failed:[/red] {e}")
        console.print("[dim]Rolling back staging tags...[/dim]")
        rollback_staging_tags(staged_keys)
        raise typer.Exit(code=1) from None

    # 7. Promote tags
    if staged_keys:
        console.print("[dim]Promoting graft release tags...[/dim]")
        promote_tags(staged_keys)
        console.print(f"[green]✓[/green] Updated {len(staged_keys)} released/ tag(s)")

    # 8. Trigger workflow
    if trigger:
        try:
            console.print(f"[dim]Triggering {workflow}...[/dim]")
            trigger_workflow(workflow)
            console.print("[green]✓[/green] Workflow triggered")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to trigger workflow: {e}")


# ============================================================================
# TRUNK CACHE COMMANDS
# ============================================================================

cache_app = typer.Typer(help="Manage the per-page render cache", no_args_is_help=True)
trunk_app.add_typer(cache_app, name="cache")


@cache_app.command("update")
def trunk_cache_update(
    site_dir: str = typer.Option(
        "_site",
        "--site-dir",
        "-s",
        help="Path to the Quarto _site/ output directory",
    ),
) -> None:
    """Capture newly rendered pages into the cache.

    Run this after 'quarto render'. Stores rendered HTML on the _cache branch
    and fixes navigation in cached pages.
    """
    require_trunk()

    site_path = Path(site_dir)
    if not site_path.exists():
        console.print(f"[red]Error:[/red] Site directory '{site_dir}' not found.")
        console.print("[dim]Run 'quarto render' first, then 'quarto-graft trunk cache update'.[/dim]")
        raise typer.Exit(code=1)

    # Read per-page hashes from the transient build state file (written by trunk build).
    graft_build_states = _load_build_state()
    if not graft_build_states:
        console.print("[yellow]No build state found.[/yellow]")
        console.print("[dim]Run 'quarto-graft trunk build' first.[/dim]")
        raise typer.Exit(code=1)

    # Identify grafts for post-processing.
    # - all_graft_keys: every graft with a manifest entry (for search index)
    # - cached_graft_keys: only grafts with cached pages (for nav fixing)
    manifest = load_manifest()
    branch_specs = read_branches_list()
    all_graft_keys: list[str] = []
    cached_graft_keys: list[str] = []
    for spec in branch_specs:
        entry = manifest.get(spec["branch"])
        if not entry:
            continue
        bk = entry.get("branch_key") or branch_to_key(spec["name"])
        all_graft_keys.append(bk)
        if entry.get("cached_pages"):
            cached_graft_keys.append(bk)

    # Capture newly rendered pages
    with console.status("Updating render cache..."):
        new_count = update_cache_after_render(site_path, graft_build_states)

    console.print(f"[green]✓[/green] Cached {new_count} newly rendered page(s)")

    # Fix navigation in cached pages and propagate fixes to _cache branch
    if cached_graft_keys:
        with console.status("Fixing navigation in cached pages..."):
            nav_count = fix_navigation(site_path, cached_graft_keys)
        if nav_count:
            console.print(f"[green]✓[/green] Updated navigation in {nav_count} cached page(s)")
            with console.status("Propagating navigation fixes to cache..."):
                prop_count = propagate_nav_to_cache(site_path, cached_graft_keys)
            if prop_count:
                console.print(f"[green]✓[/green] Propagated navigation to {prop_count} cached file(s)")

    # Merge graft pages into search index.  Quarto's own search.json only
    # contains pages it rendered; cached pages (served as pre-rendered HTML)
    # are absent.  We also index *all* graft directories — not just cached
    # ones — because Quarto may not reliably index pages under dist/ when
    # resource patterns are present.  The dedup check inside fix_search_index
    # ensures pages already in the index are not duplicated.
    if all_graft_keys:
        with console.status("Updating search index for graft pages..."):
            search_count = fix_search_index(site_path, all_graft_keys)
        if search_count:
            console.print(f"[green]✓[/green] Added {search_count} search entry/entries for graft page(s)")


@cache_app.command("clear")
def trunk_cache_clear(
    graft: str | None = typer.Option(
        None,
        "--graft",
        "-g",
        help="Only clear cache for this graft (by name)",
    ),
    no_remote: bool = typer.Option(
        False,
        "--no-remote",
        help="Do not delete the remote _cache branch",
    ),
) -> None:
    """Clear the render cache.

    Deletes the local (and optionally remote) _cache branch and recreates it empty.
    Use --graft to clear only a specific graft's cached pages.
    """
    require_trunk()

    with console.status("Clearing cache..."):
        clear_cache(graft_name=graft, delete_remote=not no_remote)

    if graft:
        console.print(f"[green]✓[/green] Cleared cache for graft '{graft}'")
    else:
        console.print("[green]✓[/green] Cache cleared")
        if not no_remote:
            console.print("[dim]Remote _cache branch also deleted.[/dim]")


@cache_app.command("status")
def trunk_cache_status() -> None:
    """Show per-page cache status."""
    require_trunk()

    entries = cache_status()
    if not entries:
        console.print(
            "[dim]Cache is empty. Run 'trunk build' then 'quarto render' then 'trunk cache update' to populate.[/dim]"
        )
        return

    table = Table(title="Render Cache")
    table.add_column("Page", style="cyan")
    table.add_column("Hash", style="dim")
    table.add_column("Cached At")
    table.add_column("Files", justify="right")

    for e in entries:
        cached_at = e["cached_at"]
        if cached_at != "?":
            try:
                dt = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
                cached_at = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                cached_at = cached_at[:16]

        table.add_row(
            e["page_key"],
            e["content_hash"],
            cached_at,
            str(e["output_files"]),
        )

    console.print(table)


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
                collar = questionary.select("Select attachment point (collar):", choices=available_collars).ask()
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
        use_custom = questionary.confirm(f"Use default branch name '{default_branch}'?", default=True).ask()

        if not use_custom:
            branch_name = questionary.text("Enter custom branch name:", default=default_branch).ask()
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
        prune_worktrees()
        logger.debug(f"Cleaned up temporary worktree for {branch_key}")
    except Exception as e:
        logger.debug(f"Failed to clean up worktree: {e}")

    console.print(
        f"[green]✓[/green] New orphan graft branch '{git_branch_name}' created from template '{template_name}'"
    )
    console.print(f"[bold]Collar:[/bold] {collar}")

    # Display trunk instructions if present
    if trunk_instructions:
        _display_trunk_instructions(trunk_instructions)


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

    with console.status(f"Building {branch}..."):
        res = build_branch(branch, update_manifest=not no_update_manifest)

    status_colors = {"ok": "green", "skipped": "blue", "fallback": "yellow", "broken": "red"}
    color = status_colors.get(res.status, "white")

    console.print(f"\n[bold]{res.branch}[/bold]")
    console.print(f"  Status:     [{color}]{res.status}[/{color}]")
    console.print(f"  Files:      {len(res.exported_dest_paths)}")
    console.print(f"  Duration:   {res.duration_secs:.1f}s")
    console.print(f"  HEAD SHA:   {res.head_sha or '—'}")
    console.print(f"  Built SHA:  {res.last_good_sha or '—'}")
    if res.error_message:
        console.print(f"  [red]Error:[/red]    {res.error_message}")


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
    table.add_column("Pre-rendered", justify="center")

    git_branches = found_branches.get("git", set())
    yaml_branches = found_branches.get("grafts.yaml", set())
    lock_branches = found_branches.get("grafts.lock", set())
    manifest = load_manifest()

    for branch in all_branches:
        entry = manifest.get(branch, {})
        prerendered = "Yes" if entry.get("prerendered") else "—"
        table.add_row(
            branch,
            "✓" if branch in git_branches else "—",
            "✓" if branch in yaml_branches else "—",
            "✓" if branch in lock_branches else "—",
            prerendered,
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
            f"Branch '{branch}' not found in tracked branches. Continue anyway?", default=False
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

    console.print(
        "\n[yellow]Note:[/yellow] Please regenerate the main docs/navigation with: [bold]quarto-graft trunk build[/bold]"
    )


@graft_app.command("archive")
def graft_archive_cmd(
    project_dir: str | None = typer.Argument(
        None,
        help="Path to graft project directory (default: current directory)",
    ),
) -> None:
    """Pre-render graft content for faster trunk builds.

    Run this from your graft branch. It runs 'quarto render' and stores the
    output in _prerendered/ for the trunk to use directly.
    """
    dir_path = Path(project_dir) if project_dir else None

    try:
        prerender_path = archive_graft(project_dir=dir_path)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from e

    # Count files and compute total size
    files = list(prerender_path.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    total_size = sum(f.stat().st_size for f in files if f.is_file())

    if total_size > 1024 * 1024:
        size_str = f"{total_size / (1024 * 1024):.1f} MB"
    elif total_size > 1024:
        size_str = f"{total_size / 1024:.1f} KB"
    else:
        size_str = f"{total_size} bytes"

    console.print(f"[green]Pre-rendered[/green] graft content ({file_count} files, {size_str})")
    console.print(f"[dim]Output stored in:[/dim] {prerender_path}")
    console.print()
    console.print("[yellow]Next steps:[/yellow]")
    console.print("  1. You may need to update your .gitignore to track _prerendered/")
    console.print("  2. Commit _prerendered/ to your graft branch:")
    console.print("     [dim]git add _prerendered/ && git commit -m 'Pre-render graft'[/dim]")
    console.print("  3. Trunk builds will automatically use pre-rendered content")


@graft_app.command("restore")
def graft_restore_cmd(
    project_dir: str | None = typer.Argument(
        None,
        help="Path to graft project directory (default: current directory)",
    ),
) -> None:
    """Remove pre-rendered content from a graft.

    Run this from your graft branch. Removes the _prerendered/ directory
    so trunk builds will render from source again.
    """
    dir_path = Path(project_dir) if project_dir else None

    try:
        success = restore_graft(project_dir=dir_path)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from e

    if success:
        console.print("[green]Removed[/green] pre-rendered content")
        console.print("[dim]Trunk builds will now render this graft from source.[/dim]")
    else:
        console.print("[yellow]No pre-rendered content found.[/yellow] Nothing to remove.")


# ============================================================================
# STATUS COMMAND
# ============================================================================


@app.command("status")
def status_cmd(
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="Skip fetching origin before checking status",
    ),
) -> None:
    """Show build status of all grafts."""
    require_trunk()

    branch_specs = read_branches_list()
    manifest = load_manifest()

    if not branch_specs:
        console.print("[dim]No grafts configured in grafts.yaml.[/dim]")
        return

    if not no_fetch:
        with console.status("Fetching origin..."):
            fetch_origin()

    table = Table(title="Graft Status")
    table.add_column("Graft", style="cyan")
    table.add_column("Collar")
    table.add_column("Status", justify="center")
    table.add_column("Last Built")
    table.add_column("HEAD", justify="center")
    table.add_column("Built", justify="center")

    for spec in branch_specs:
        name = spec["name"]
        branch = spec["branch"]
        collar = spec["collar"]
        entry = manifest.get(branch, {})

        last_good = entry.get("last_good")
        last_checked = entry.get("last_checked", "")
        is_prerendered = entry.get("prerendered", False)

        head_sha = resolve_head_sha(branch)

        # Determine status
        if not entry:
            status = "never built"
            color = "dim"
        elif not last_good:
            status = "broken"
            color = "red"
        elif head_sha and head_sha != last_good:
            status = "stale"
            color = "yellow"
        elif head_sha is None:
            status = "missing"
            color = "red"
        else:
            status = "current"
            color = "green"

        if is_prerendered and status in ("current", "stale"):
            status += " (pre-rendered)"

        # Format last-built timestamp
        time_str = "—"
        if last_checked:
            try:
                dt = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                time_str = last_checked[:16]

        head_short = head_sha[:7] if head_sha else "—"
        built_short = last_good[:7] if last_good else "—"

        # Highlight SHA mismatch
        if head_sha and last_good and head_sha != last_good:
            head_short = f"[yellow]{head_short}[/yellow]"

        table.add_row(
            name,
            collar,
            f"[{color}]{status}[/{color}]",
            time_str,
            head_short,
            built_short,
        )

    console.print(table)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    log_level: str = typer.Option(
        None,
        "--log-level",
        "-L",
        help="Set logging level (DEBUG, INFO, WARNING, ERROR)",
        envvar="QBB_LOG_LEVEL",
    ),
) -> None:
    """Main callback - launches interactive mode if no command given."""
    _configure_logging(log_level)

    # If a subcommand was invoked, do nothing here
    if ctx.invoked_subcommand is not None:
        return

    # Launch interactive menu
    selected_command = show_main_menu()

    if selected_command is None:
        console.print("[dim]Exited.[/dim]")
        raise typer.Exit(code=0)

    # Parse and execute the selected command
    if selected_command == "status":
        status_cmd(no_fetch=False)
        return

    parts = selected_command.split()
    if len(parts) == 2:
        group, command = parts

        # Route to appropriate command handler
        if group == "trunk" and command == "init":
            trunk_init(name=None, template=None, overwrite=None, with_addons=None)
        elif group == "trunk" and command == "build":
            trunk_build(
                no_update_manifest=False,
                jobs=1,
                only=None,
                skip=None,
                changed=False,
                no_cache=False,
            )
        elif group == "trunk" and command == "lock":
            trunk_lock()
        elif group == "trunk" and command == "release":
            trunk_release(
                workflow="quarto-graft-build-publish.yaml",
                trigger=False,
                edit_release=False,
                yes=False,
            )
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
        elif group == "graft" and command == "archive":
            graft_archive_cmd(project_dir=None)
        elif group == "graft" and command == "restore":
            graft_restore_cmd(project_dir=None)
    elif len(parts) == 3:
        group, sub, command = parts
        if group == "trunk" and sub == "cache":
            if command == "update":
                trunk_cache_update(site_dir="_site")
            elif command == "clear":
                trunk_cache_clear(graft=None, no_remote=False)
            elif command == "status":
                trunk_cache_status()


def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
