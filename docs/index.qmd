# quarto-graft

> A Python CLI for multi-author Quarto documentation using git worktrees

**Quarto Graft** is a command-line tool that lets multiple authors collaborate on a single Quarto website without merge conflicts. Each author works in an isolated git branch (a "graft"), and the main branch (the "trunk") automatically assembles everything into one unified, searchable site.

## Key Concepts

### Trunk
The **trunk** is your main branch and the foundation of your Quarto site. It defines:
- The overall site structure (navbar, sidebar, styling)
- **Collars**: named attachment points where grafts connect (e.g., "main", "notes", "bugs")
- Site configuration and templates

### Grafts
**Grafts** are isolated git branches where authors work independently. Each graft:
- Runs in its own git worktree with its own dependencies
- Can use any language or environment (Python, R, Julia, etc.)
- Specifies which **collar** it attaches to
- Gets automatically included in the trunk's navigation

### Collars
**Collars** are attachment points in the trunk's `_quarto.yaml` that organize grafts into sections:
```yaml
sidebar:
  contents:
    - section: My Grafts
      contents:
        - _GRAFT_COLLAR: main
    - section: Notes
      contents:
        - _GRAFT_COLLAR: notes
```

### Templates
Everything is **template-based** and customizable:
- **Trunk templates**: Define your site's look, feel, and structure
- **Graft templates**: Provide starter content for different types of contributions
- Templates use Jinja2 for configuration
- Create custom templates for your organization

## Why Use Quarto Graft?

**Traditional multi-author collaboration problems:**
- Merge conflicts on `main`
- Shared dependencies causing version conflicts
- One author's broken code blocks everyone
- Can't use different languages/tools per section

**Quarto Graft solutions:**
- ✅ Each author owns a branch = zero merge conflicts
- ✅ Each graft has independent dependencies
- ✅ Broken grafts use last-good fallbacks with warnings
- ✅ Mix Python, R, Julia, or any language per graft
- ✅ Trunk never executes contributor code, only renders artifacts
- ✅ Organize content with multiple collars (sections)

## What You Get

- 🚀 Python CLI (`quarto-graft`) for project management
- 📦 Customizable trunk and graft templates
- 🔧 Git worktree-based isolation
- 🎯 Multiple collar attachment points
- 🔄 Automatic navigation updates
- 💾 Last-good build fallbacks
- 🔍 Full-site search across all grafts
- ⚡ Fast trunk builds (no code execution)

## Who This Is For

- **Multi-author books and research publications**
- **Data science teams** (quant research, education platforms)
- **Internal documentation portals**
- **Open source projects** with distributed contributors
- Anyone managing versioned, multi-contributor content

## Quick Start

### Prerequisites

Before using Quarto Graft, ensure you have:

- **Python 3.11+** installed
- **Git** initialized in your project (`git init` and at least one commit)
- **Quarto CLI** installed:
  ```bash
  pip install quarto-cli
  # OR download from https://quarto.org/docs/get-started/
  ```

### Installation

```bash
pip install quarto-graft
```

### Step 1: Initialize a Trunk

The trunk is your main documentation site. Run this from your git repository root:

```bash
# Interactive mode (recommended for first-time setup)
quarto-graft trunk init

# Or non-interactive with options
quarto-graft trunk init --template markdown
```

**This creates:**
- `_quarto.yaml` - Quarto configuration with collar markers
- `grafts.yaml` - Graft branch configuration (initially empty)
- `index.qmd` - Landing page for your site

> **Note:** The command runs in your **current directory** (doesn't create a subdirectory).

### Step 2: Create Your First Graft

A graft is an isolated author branch:

```bash
# Interactive mode (will prompt for template and collar)
quarto-graft graft create demo

# Or specify all options
quarto-graft graft create demo --template py-jupyter --collar main
```

**This creates:**
- Git branch: `graft/demo` (customizable with `--branch-name`)
- Worktree at: `.grafts-cache/demo/`
- Entry in `grafts.yaml`
- Automatically pushes to origin (use `--no-push` to skip)

**Available templates:**
- `markdown` - Simple Markdown/QMD documents
- `py-jupyter` - Python + Jupyter notebooks
- `py-marimo` - Python + Marimo notebooks

### Step 3: Build and Preview

Build all grafts and update the trunk:

```bash
# Build all grafts (fetches latest, builds, updates navigation)
quarto-graft trunk build

# Preview the complete site locally
quarto preview
```

**What happens during `trunk build`:**
1. Fetches latest changes from all graft branches
2. Builds each graft in isolation (or uses last-good fallback if broken)
3. Exports content to `grafts__/<graft-name>/`
4. Updates `_quarto.yaml` with navigation
5. Creates/updates `grafts.lock` with build state

**View your site:** Open the URL from `quarto preview` (usually http://localhost:4200)

### Working on a Graft

Authors can work on grafts in two ways:

**Option 1: Use the worktree directory** (recommended for independent work)
```bash
cd .grafts-cache/demo
# Edit files, run notebooks, commit, push
git add . && git commit -m "Updated content"
git push
```

**Option 2: Checkout the branch** (if you need to switch contexts)
```bash
git checkout graft/demo
# Work normally
git add . && git commit -m "Updates"
git push
git checkout main  # Return to trunk
```

After pushing graft changes, rebuild from trunk to see updates:
```bash
quarto-graft trunk build
```

### Full Example Workflow

```bash
# 1. Set up project
mkdir my-docs && cd my-docs
git init
git commit --allow-empty -m "Initial commit"

# 2. Install quarto-graft
pip install quarto-graft

# 3. Initialize trunk
quarto-graft trunk init
git add .
git commit -m "Initialize trunk"
git push -u origin main

# 4. Create graft for Alice
quarto-graft graft create alice-chapter --collar main

# 5. Alice works in her graft
cd .grafts-cache/alice-chapter
# ... edit files ...
git add . && git commit -m "Added my chapter"
git push
cd ../..

# 6. Build and preview trunk
git checkout main
quarto-graft trunk build
quarto preview

# 7. Deploy to GitHub Pages (when ready)
quarto publish gh-pages
```

### Key Commands Reference

| Command | Description |
|---------|-------------|
| `quarto-graft trunk init` | Create trunk structure from template |
| `quarto-graft trunk build` | Build all grafts and update trunk navigation |
| `quarto-graft trunk lock` | Update `_quarto.yaml` from `grafts.lock` (no rebuild) |
| `quarto-graft graft create <name>` | Create new graft branch |
| `quarto-graft graft build --branch <name>` | Build single graft |
| `quarto-graft graft list` | Show all graft branches and their status |
| `quarto-graft graft destroy <name>` | Delete graft (branch, worktree, config) |

### Need Help?

- **Interactive mode:** Run `quarto-graft` with no arguments
- **Command help:** Use `--help` on any command
- **Issues:** https://github.com/jr200/quarto-graft/issues

## License

Released under the **MIT License**. Free to use, modify, and redistribute with attribution.
