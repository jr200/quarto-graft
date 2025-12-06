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

```bash
# Install
pip install quarto-graft

# Initialize a trunk (main site)
quarto-graft trunk init my-project

# Create a graft (contributor branch)
cd my-project
quarto-graft graft create demo --collar main

# Build and preview
quarto-graft build
quarto preview
```

## License

Released under the **MIT License**. Free to use, modify, and redistribute with attribution.
