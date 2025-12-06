# quarto-graft

> Compose a single Quarto site from branch-scoped worktrees.

Quarto Graft lets contributors build chapters, sections, or full sub-sites in isolated git branches while the trunk assembles everything into one **fully searchable** Quarto website.

## Why Quarto Graft exists

Traditional versioned collaboration forces everyone into `main`, leading to merge conflicts, shared heavy dependencies, and a single project shape. Quarto Graft flips that model:

- Each contributor owns **one branch = one self-contained chapter/section** with its own language, environment, and runtime.
- The trunk **never executes contributor code**; it only ingests rendered artifacts, keeping `main` fast and stable.
- Broken branches don't block the site: last-good fallbacks keep content online with visible warnings.

## What you get

- **Templated trunk and graft projects**: Choose from templates for both your main site (trunk) and contributor branches (grafts).
- Ready-to-publish Quarto scaffold (navbar, sidebar, favicon, footer, search, GitHub links).
- Branch-grafting build system using git worktrees + uv and automatic navigation updates.
- Clean contributor workflow: no cross-branch conflicts or shared dependency coupling; full local preview per branch.
- Failure handling: last-good fallbacks and visible broken-branch stubs.

## Who this is for

- Multi-author books and research publications
- Quant/science teams and education platforms
- Internal documentation portals
- ...anybody who wants to track and version threads in isolation

## Quick Start

1. Clone this repository
2. Install dependencies:
   ```bash
   uv sync
   ```
3. Initialize the trunk from a template:
   ```bash
   uv run trunk-init --template default
   ```
4. Build and preview:
   ```bash
   uv run trunk-build
   uv run quarto render docs --no-execute
   uv run quarto preview docs
   ```

## Project Structure

```
.
├── trunk-templates/  # Trunk (main site) templates
│   └── default/      # Default trunk template
├── graft-templates/  # Graft (branch) templates
│   ├── markdown/
│   ├── py-jupyter/
│   └── py-marimo/
├── docs/             # Generated trunk site (not in git)
├── .grafts-cache/    # Internal build cache for graft worktrees
├── grafts.yaml       # Graft branch configuration
└── src/              # Quarto-graft tooling
```

## License

quarto-graft is released under the **MIT License**. You are free to use, modify, and redistribute it with attribution.
