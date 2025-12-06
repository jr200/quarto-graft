from __future__ import annotations

from pathlib import Path

# Repo root: src/quarto_graft/constants.py -> src/quarto_graft -> src -> ROOT
ROOT = Path(__file__).resolve().parents[2]
GRAFTS_MANIFEST_FILE = ROOT / "grafts.lock"
GRAFTS_CONFIG_FILE = ROOT / "grafts.yaml"
WORKTREES = ROOT / ".grafts"
MAIN_DOCS = ROOT / "docs"
GRAFTS_BUILD_DIR = MAIN_DOCS / "grafts__"

# Quarto config filenames
QUARTO_CONFIG_YML = "_quarto.yml"
QUARTO_CONFIG_YAML = "_quarto.yaml"

# Marker for auto-generated grafts in _quarto.yaml
AUTOGEN_GRAFTS_MARKER = "_AUTOGEN_GRAFTS"

# Protected branch names that cannot be used as grafts
TRUNK_BRANCHES = {"main", "master"}
PROTECTED_BRANCHES = TRUNK_BRANCHES.union({"gh-pages"})
