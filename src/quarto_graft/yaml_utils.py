from __future__ import annotations

from typing import Optional

from ruamel.yaml import YAML  # type: ignore

_yaml_loader: Optional[YAML] = None


def get_yaml_loader() -> YAML:
    """Get or create a cached YAML loader instance."""
    global _yaml_loader
    if _yaml_loader is None:
        _yaml_loader = YAML()
        _yaml_loader.preserve_quotes = True
        _yaml_loader.width = 4096  # Prevent line wrapping
    return _yaml_loader
