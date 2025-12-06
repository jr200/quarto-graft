from __future__ import annotations

from pathlib import Path

# The CLI is meant to run from the user's project root, not the package install
# directory. ROOT is therefore the current working directory at runtime.
ROOT = Path.cwd()

# Templates are bundled with the package under src/quarto_graft/.
PACKAGE_ROOT = Path(__file__).resolve().parent
TRUNK_TEMPLATES_DIR = PACKAGE_ROOT / "trunk-templates"
GRAFT_TEMPLATES_DIR = PACKAGE_ROOT / "graft-templates"

GRAFTS_MANIFEST_FILE = ROOT / "grafts.lock"
GRAFTS_CONFIG_FILE = ROOT / "grafts.yaml"
WORKTREES_CACHE = ROOT / ".grafts-cache"  # Internal cache for build process
QUARTO_PROJECT_YAML = ROOT / "_quarto.yaml"
# The trunk is rendered into the current working directory.
MAIN_DOCS = ROOT
GRAFTS_BUILD_DIR = MAIN_DOCS / "grafts__"

# Quarto config filenames
QUARTO_CONFIG_YAML = "_quarto.yaml"

# Marker for graft attachment points in _quarto.yaml
GRAFT_COLLAR_MARKER = "_GRAFT_COLLAR"

# Protected branch names that cannot be used as grafts
TRUNK_BRANCHES = {"main", "master"}
PROTECTED_BRANCHES = TRUNK_BRANCHES.union({"gh-pages"})
