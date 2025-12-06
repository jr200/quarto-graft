"""Template source management - handles local paths, URLs, GitHub repos, and archives."""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import urlopen

import pygit2

from .constants import ROOT

logger = logging.getLogger(__name__)

# Cache directory for remote templates
TEMPLATE_CACHE_DIR = ROOT / ".quarto-graft" / ".template-cache"


class TemplateSource:
    """Represents a source of templates (local path, URL, or archive)."""

    def __init__(self, spec: Dict[str, str], source_name: str = "custom"):
        """
        Initialize a template source.

        Args:
            spec: Dictionary with 'path', 'url', or 'github' key
            source_name: Human-readable name for this source
        """
        self.spec = spec
        self.source_name = source_name
        self._resolved_path: Optional[Path] = None

    def resolve(self) -> Path:
        """
        Resolve the template source to a local directory path.

        Returns:
            Path to directory containing templates

        Raises:
            RuntimeError: If source cannot be resolved
        """
        if self._resolved_path and self._resolved_path.exists():
            return self._resolved_path

        if "path" in self.spec:
            self._resolved_path = self._resolve_local_path(self.spec["path"])
        elif "url" in self.spec:
            # If the URL looks like a GitHub repo (non-archive), handle via git clone
            github_info = self._parse_github_url(self.spec["url"])
            if github_info:
                self._resolved_path = self._resolve_github(github_info["repo"], github_info.get("ref"))
            else:
                self._resolved_path = self._resolve_url(self.spec["url"])
        elif "github" in self.spec:
            self._resolved_path = self._resolve_github(
                self.spec["github"],
                self.spec.get("ref"),
            )
        else:
            raise RuntimeError(f"Template source must have 'path', 'url', or 'github': {self.spec}")

        if not self._resolved_path.exists():
            raise RuntimeError(f"Resolved template path does not exist: {self._resolved_path}")

        return self._resolved_path

    def _resolve_local_path(self, path_str: str) -> Path:
        """Resolve a local path (relative or absolute)."""
        path = Path(path_str)

        # If absolute, use as-is
        if path.is_absolute():
            logger.debug(f"[template-source] Using absolute path: {path}")
            return path

        # If relative, resolve relative to project root
        resolved = (ROOT / path).resolve()
        logger.debug(f"[template-source] Resolved relative path '{path_str}' to: {resolved}")
        return resolved

    def _resolve_url(self, url: str) -> Path:
        """
        Resolve a URL by downloading and caching it.

        Supports:
        - Direct archive URLs (.zip, .tar.gz, .tgz)
        - GitHub archive URLs
        """
        # Create cache directory
        TEMPLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Generate cache key from URL
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        parsed = urlparse(url)
        filename = Path(parsed.path).name or "templates"

        # Determine cache subdirectory
        cache_subdir = TEMPLATE_CACHE_DIR / f"{url_hash}-{filename}"

        # Check if already cached
        if cache_subdir.exists() and any(cache_subdir.iterdir()):
            logger.info(f"[template-source] Using cached templates from: {url}")
            return cache_subdir

        logger.info(f"[template-source] Downloading templates from: {url}")

        # Download the file
        try:
            with urlopen(url) as response:
                content = response.read()
        except Exception as e:
            raise RuntimeError(f"Failed to download template source from {url}: {e}")

        # Determine file type and extract
        if url.endswith(".zip") or "zip" in parsed.path.lower():
            self._extract_zip(content, cache_subdir)
        elif url.endswith((".tar.gz", ".tgz")) or "tar" in parsed.path.lower():
            self._extract_tar(content, cache_subdir)
        else:
            github_info = self._parse_github_url(url)
            if github_info:
                # Non-archive GitHub URL (e.g., https://github.com/user/repo or .../tree/ref)
                return self._resolve_github(github_info["repo"], github_info.get("ref"))
            raise RuntimeError(
                f"Unknown archive format for URL: {url}. "
                "Supported formats: .zip, .tar.gz, .tgz, or GitHub repository URLs"
            )

        logger.info(f"[template-source] Extracted templates to: {cache_subdir}")
        return cache_subdir

    def _extract_zip(self, content: bytes, dest: Path) -> None:
        """Extract a zip archive to destination."""
        import io

        dest.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # Find the root directory in the archive
            members = zf.namelist()
            if not members:
                raise RuntimeError("Empty zip archive")

            # Check if all files are in a single root directory
            root_dirs = {Path(m).parts[0] for m in members if m and not m.startswith(".")}
            if len(root_dirs) == 1:
                # Strip the root directory
                root_dir = root_dirs.pop()
                for member in members:
                    if member.startswith(root_dir + "/"):
                        target_path = dest / Path(member).relative_to(root_dir)
                        if member.endswith("/"):
                            target_path.mkdir(parents=True, exist_ok=True)
                        else:
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as source, open(target_path, "wb") as target:
                                shutil.copyfileobj(source, target)
            else:
                # Extract directly
                zf.extractall(dest)

    def _extract_tar(self, content: bytes, dest: Path) -> None:
        """Extract a tar.gz archive to destination."""
        import io

        dest.mkdir(parents=True, exist_ok=True)

        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as tf:
            # Find the root directory in the archive
            members = tf.getmembers()
            if not members:
                raise RuntimeError("Empty tar archive")

            # Check if all files are in a single root directory
            root_dirs = {Path(m.name).parts[0] for m in members if m.name and not m.name.startswith(".")}
            if len(root_dirs) == 1:
                # Strip the root directory
                root_dir = root_dirs.pop()
                for member in members:
                    if member.name.startswith(root_dir + "/"):
                        member.name = str(Path(member.name).relative_to(root_dir))
                        if member.name:  # Skip if empty after stripping
                            tf.extract(member, dest)
            else:
                # Extract directly
                tf.extractall(dest)

    def discover_templates(self, template_type: str) -> List[str]:
        """
        Discover templates of a given type from this source.

        Args:
            template_type: Either "trunk" or "graft"

        Returns:
            List of template names
        """
        try:
            resolved = self.resolve()
        except RuntimeError as e:
            logger.warning(f"[template-source] Failed to resolve source '{self.source_name}': {e}")
            return []

        # Look for templates in:
        # 1. <source>/trunk-templates/ or <source>/graft-templates/
        # 2. <source>/ (if templates are at root)
        template_dir_name = f"{template_type}-templates"
        search_paths = [
            resolved / template_dir_name,
            resolved,
        ]

        templates = []
        for search_path in search_paths:
            if not search_path.exists():
                continue

            for entry in search_path.iterdir():
                if entry.is_dir() and not entry.name.startswith((".", "with-")):
                    templates.append(entry.name)

            if templates:
                # Found templates, don't search further
                break

        return sorted(set(templates))

    def get_template_path(self, template_name: str, template_type: str) -> Optional[Path]:
        """
        Get the path to a specific template.

        Args:
            template_name: Name of the template
            template_type: Either "trunk" or "graft"

        Returns:
            Path to template directory, or None if not found
        """
        try:
            resolved = self.resolve()
        except RuntimeError:
            return None

        template_dir_name = f"{template_type}-templates"
        search_paths = [
            resolved / template_dir_name / template_name,
            resolved / template_name,
        ]

        for path in search_paths:
            if path.exists() and path.is_dir():
                return path

        return None

    # ------------------------------------------------------------------
    # GitHub handling
    # ------------------------------------------------------------------
    def _parse_github_url(self, url: str) -> Optional[Dict[str, str]]:
        """
        Parse a GitHub URL to extract repo and ref.

        Supports URLs like:
          - https://github.com/user/repo
          - https://github.com/user/repo.git
          - https://github.com/user/repo/tree/main
          - https://github.com/user/repo/tree/v1.0.0
        """
        parsed = urlparse(url)
        if parsed.netloc not in {"github.com", "www.github.com"}:
            return None

        parts = [p for p in Path(parsed.path).parts if p and p != "/"]
        if len(parts) < 2:
            return None

        user, repo = parts[0], parts[1]
        repo = repo[:-4] if repo.endswith(".git") else repo

        ref = None
        if len(parts) >= 4 and parts[2] == "tree":
            ref = parts[3]

        return {"repo": f"{user}/{repo}", "ref": ref}

    def _resolve_github(self, repo: str, ref: Optional[str] = None) -> Path:
        """
        Resolve a GitHub repository by cloning it (optionally at a ref/tag/branch).

        Cloned repos are cached under .quarto-graft/.template-cache/.
        """
        TEMPLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        ref_display = ref or "default"
        cache_key = hashlib.sha256(f"{repo}@{ref_display}".encode()).hexdigest()[:16]
        cache_dir = TEMPLATE_CACHE_DIR / f"github-{cache_key}-{repo.replace('/', '-')}"

        if cache_dir.exists() and any(cache_dir.iterdir()):
            logger.info(f"[template-source] Using cached GitHub repo {repo}@{ref_display}")
            return cache_dir

        logger.info(f"[template-source] Cloning GitHub repo {repo}{' @ ' + ref if ref else ''}")
        cache_dir.mkdir(parents=True, exist_ok=True)

        clone_url = f"https://github.com/{repo}.git"
        try:
            repo_obj = pygit2.clone_repository(
                clone_url,
                cache_dir,
                checkout_branch=ref if ref else None,
            )
            if ref:
                # If ref is not a branch, attempt to check out by rev
                try:
                    target = repo_obj.revparse_single(ref)
                    repo_obj.checkout_tree(target)
                    repo_obj.set_head(target.id)
                    repo_obj.state_cleanup()
                except KeyError:
                    # Try common tag ref
                    try:
                        target = repo_obj.revparse_single(f"refs/tags/{ref}")
                        repo_obj.checkout_tree(target)
                        repo_obj.set_head(target.id)
                        repo_obj.state_cleanup()
                    except (KeyError, pygit2.GitError):
                        raise RuntimeError(f"Ref '{ref}' not found in {clone_url}")
        except (pygit2.GitError, RuntimeError) as e:
            # Clean up failed clone
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to clone {clone_url} (ref: {ref or 'default'}): {e}")

        return cache_dir


def load_template_sources_from_config() -> List[TemplateSource]:
    """
    Load template sources from grafts.yaml.

    Returns:
        List of TemplateSource objects
    """
    from .constants import GRAFTS_CONFIG_FILE
    from .yaml_utils import get_yaml_loader

    if not GRAFTS_CONFIG_FILE.exists():
        return []

    yaml_loader = get_yaml_loader()
    data = yaml_loader.load(GRAFTS_CONFIG_FILE.read_text(encoding="utf-8")) or {}

    templates_config = data.get("templates")
    if not templates_config:
        return []

    if not isinstance(templates_config, list):
        logger.warning(
            f"[template-source] 'templates' in {GRAFTS_CONFIG_FILE} should be a list, got {type(templates_config)}"
        )
        return []

    sources = []
    for idx, spec in enumerate(templates_config):
        if not isinstance(spec, dict):
            logger.warning(f"[template-source] Template source {idx} is not a dict: {spec}")
            continue

        source_name = f"custom-{idx + 1}"
        if "path" in spec:
            source_name = f"local:{spec['path']}"
        elif "url" in spec:
            source_name = f"url:{spec['url']}"
        elif "github" in spec:
            source_name = f"github:{spec['github']}"
            if spec.get("ref"):
                source_name += f"@{spec['ref']}"

        try:
            sources.append(TemplateSource(spec, source_name))
        except Exception as e:
            logger.warning(f"[template-source] Failed to create source from {spec}: {e}")

    return sources
