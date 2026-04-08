"""Tests for build module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from quarto_graft.build import (
    BuildResult,
    _manifest_entry_from_result,
    _temp_worktree_name,
    _update_manifest_entry,
    create_broken_stub,
    inject_failure_header,
    inject_graft_source_metadata,
    resolve_head_sha,
)
from quarto_graft.constants import GRAFTS_BUILD_RELPATH

# ---------------------------------------------------------------------------
# _temp_worktree_name
# ---------------------------------------------------------------------------


class TestTempWorktreeName:
    def test_format(self):
        name = _temp_worktree_name("demo", "head")
        assert name.startswith("head-demo-")
        # 6 hex chars from uuid
        suffix = name.split("-", 2)[-1]
        assert len(suffix) == 6

    def test_uniqueness(self):
        names = {_temp_worktree_name("demo", "head") for _ in range(50)}
        assert len(names) == 50


# ---------------------------------------------------------------------------
# inject_failure_header
# ---------------------------------------------------------------------------


class TestInjectFailureHeader:
    def test_injects_header_with_sha(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("# Hello\n\nContent here.", encoding="utf-8")

        inject_failure_header(qmd, "feature/x", "abcdef1234567", "1111111222222")
        text = qmd.read_text(encoding="utf-8")

        assert "::: callout-warning" in text
        assert "feature/x" in text
        assert "abcdef1" in text  # head_sha[:7]
        assert "1111111" in text  # last_good_sha[:7]
        assert text.endswith("# Hello\n\nContent here.")

    def test_injects_header_without_head_sha(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("body", encoding="utf-8")

        inject_failure_header(qmd, "feature/x", None, "aaa1111bbb2222")
        text = qmd.read_text(encoding="utf-8")

        assert "branch missing or unreachable" in text
        assert "aaa1111" in text
        assert text.endswith("body")

    def test_short_shas_used_as_is(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("x", encoding="utf-8")

        inject_failure_header(qmd, "b", "abc", "def")
        text = qmd.read_text(encoding="utf-8")
        assert "`abc`" in text
        assert "`def`" in text


# ---------------------------------------------------------------------------
# inject_graft_source_metadata
# ---------------------------------------------------------------------------


class TestInjectGraftSourceMetadata:
    def test_injects_into_existing_frontmatter(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: Hello\n---\nBody\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/demo", "docs/index.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert '_graft-branch: "graft/demo"' in text
        assert '_graft-source-path: "docs/index.qmd"' in text
        assert text.startswith("---\n")
        assert "Body" in text

    def test_creates_frontmatter_when_missing(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("Just body text\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/notes", "notes.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert text.startswith("---\n")
        assert '_graft-branch: "graft/notes"' in text
        assert '_graft-source-path: "notes.qmd"' in text
        assert "Just body text" in text

    def test_preserves_existing_metadata(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: My Page\nauthor: me\n---\nContent\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/x", "docs/page.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert "title: My Page" in text
        assert "author: me" in text
        assert '_graft-branch: "graft/x"' in text

    def test_metadata_inserted_before_closing_delimiter(self, tmp_path):
        """Graft metadata must sit inside the frontmatter block, not after it."""
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: T\n---\nBody\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/a", "x.qmd")
        text = qmd.read_text(encoding="utf-8")

        lines = text.split("\n")
        opening = lines.index("---")
        closing = lines.index("---", opening + 1)
        between = "\n".join(lines[opening + 1 : closing])
        assert "_graft-branch" in between
        assert "_graft-source-path" in between

    def test_body_not_duplicated(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: T\n---\nOne line body\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/b", "p.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert text.count("One line body") == 1

    def test_empty_file(self, tmp_path):
        qmd = tmp_path / "page.qmd"
        qmd.write_text("", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/e", "docs/e.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert text.startswith("---\n")
        assert '_graft-branch: "graft/e"' in text

    def test_nested_source_path(self, tmp_path):
        """Source paths with sub-directories are preserved as-is."""
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: Deep\n---\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/deep", "sub/dir/page.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert '_graft-source-path: "sub/dir/page.qmd"' in text

    def test_branch_with_special_chars_quoted(self, tmp_path):
        """Branch names are YAML-quoted so special chars are safe."""
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: T\n---\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/my-feature_v2", "index.qmd")
        text = qmd.read_text(encoding="utf-8")

        assert '_graft-branch: "graft/my-feature_v2"' in text

    def test_valid_yaml_after_injection(self, tmp_path):
        """The resulting frontmatter must be parseable YAML."""
        yaml_utils = __import__("quarto_graft.yaml_utils", fromlist=["get_yaml_loader"])
        loader = yaml_utils.get_yaml_loader()

        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: Hello\nauthor: me\n---\nBody\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/demo", "docs/index.qmd")
        text = qmd.read_text(encoding="utf-8")

        # Extract frontmatter between --- delimiters
        parts = text.split("---", 2)
        fm = loader.load(parts[1])

        assert fm["_graft-branch"] == "graft/demo"
        assert fm["_graft-source-path"] == "docs/index.qmd"
        assert fm["title"] == "Hello"
        assert fm["author"] == "me"

    def test_idempotent_double_injection(self, tmp_path):
        """Running injection twice should add the metadata twice (no dedup),
        but the file should remain parseable."""
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: T\n---\nBody\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/a", "a.qmd")
        inject_graft_source_metadata(qmd, "graft/b", "b.qmd")
        text = qmd.read_text(encoding="utf-8")

        # Both injections present (last one wins in YAML)
        assert '_graft-branch: "graft/a"' in text
        assert '_graft-branch: "graft/b"' in text

    def test_works_with_inject_failure_header(self, tmp_path):
        """Graft metadata + failure header should coexist without corruption."""
        qmd = tmp_path / "page.qmd"
        qmd.write_text("---\ntitle: T\n---\nBody\n", encoding="utf-8")

        inject_graft_source_metadata(qmd, "graft/c", "docs/c.qmd")
        inject_failure_header(qmd, "graft/c", "abc1234567", "def7654321")

        text = qmd.read_text(encoding="utf-8")
        assert '_graft-branch: "graft/c"' in text
        assert "::: callout-warning" in text
        assert "Body" in text


# ---------------------------------------------------------------------------
# create_broken_stub
# ---------------------------------------------------------------------------


class TestCreateBrokenStub:
    def test_creates_index_qmd(self, tmp_path):
        out_dir = tmp_path / GRAFTS_BUILD_RELPATH / "demo"
        paths = create_broken_stub("demo", "graft/demo", "abcdef1234567", out_dir)

        assert len(paths) == 1
        assert paths[0] == out_dir / "index.qmd"
        assert paths[0].exists()

    def test_content_includes_branch_name(self, tmp_path):
        out_dir = tmp_path / "out"
        create_broken_stub("demo", "graft/demo", "abcdef1234567", out_dir)
        text = (out_dir / "index.qmd").read_text(encoding="utf-8")

        assert "graft/demo" in text
        assert "abcdef1" in text
        assert "::: callout-warning" in text

    def test_content_without_sha(self, tmp_path):
        out_dir = tmp_path / "out"
        create_broken_stub("demo", "graft/demo", None, out_dir)
        text = (out_dir / "index.qmd").read_text(encoding="utf-8")

        assert "graft/demo" in text
        assert "no previous successful build" in text

    def test_creates_parent_dirs(self, tmp_path):
        out_dir = tmp_path / "deep" / "nested" / "dir"
        paths = create_broken_stub("demo", "br", None, out_dir)
        assert paths[0].exists()

    def test_short_sha(self, tmp_path):
        out_dir = tmp_path / "out"
        create_broken_stub("demo", "br", "abc", out_dir)
        text = (out_dir / "index.qmd").read_text(encoding="utf-8")
        assert "`abc`" in text


# ---------------------------------------------------------------------------
# _update_manifest_entry
# ---------------------------------------------------------------------------


class TestUpdateManifestEntry:
    def test_basic_entry(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "branch1",
            "branch1",
            "Title",
            now="2026-01-01T00:00:00Z",
        )
        entry = manifest["branch1"]
        assert entry["title"] == "Title"
        assert entry["branch_key"] == "branch1"
        assert entry["last_checked"] == "2026-01-01T00:00:00Z"

    def test_with_nav_structure(self):
        manifest: dict = {}
        nav = [{"section": "Ch1", "contents": ["a.qmd"]}]
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            nav_structure=nav,
            now="2026-01-01T00:00:00Z",
        )
        assert manifest["b"]["structure"] == nav

    def test_with_last_good(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            last_good="abc123",
            now="2026-01-01T00:00:00Z",
        )
        assert manifest["b"]["last_good"] == "abc123"

    def test_with_prerendered(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            prerendered=True,
            now="2026-01-01T00:00:00Z",
        )
        assert manifest["b"]["prerendered"] is True

    def test_prerendered_false_omitted(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            prerendered=False,
            now="2026-01-01T00:00:00Z",
        )
        assert "prerendered" not in manifest["b"]

    def test_with_cached_pages(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            cached_pages=["p.qmd"],
            now="2026-01-01T00:00:00Z",
        )
        assert manifest["b"]["cached_pages"] == ["p.qmd"]

    def test_empty_cached_pages_omitted(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            cached_pages=[],
            now="2026-01-01T00:00:00Z",
        )
        assert "cached_pages" not in manifest["b"]

    def test_none_optional_fields_omitted(self):
        manifest: dict = {}
        _update_manifest_entry(
            manifest,
            "b",
            "b",
            "T",
            now="2026-01-01T00:00:00Z",
        )
        for key in ("structure", "last_good", "prerendered", "cached_pages"):
            assert key not in manifest["b"]

    def test_auto_generates_now(self):
        manifest: dict = {}
        _update_manifest_entry(manifest, "b", "b", "T", [])
        assert "last_checked" in manifest["b"]
        assert manifest["b"]["last_checked"].endswith("Z")


# ---------------------------------------------------------------------------
# _manifest_entry_from_result
# ---------------------------------------------------------------------------


class TestManifestEntryFromResult:
    def _make_result(self, **overrides: object) -> BuildResult:
        defaults: dict[str, object] = {
            "branch": "b",
            "branch_key": "b",
            "title": "T",
            "status": "ok",
            "head_sha": "abc",
            "last_good_sha": "abc",
            "built_at": "2026-01-01T00:00:00Z",
            "exported_relpaths": ["p.qmd"],
            "exported_dest_paths": [],
        }
        defaults.update(overrides)
        return BuildResult(**defaults)  # type: ignore[arg-type]

    def test_basic_fields(self):
        result = self._make_result()
        entry = _manifest_entry_from_result(result)
        assert entry["title"] == "T"
        assert entry["branch_key"] == "b"
        assert entry["last_checked"] == "2026-01-01T00:00:00Z"
        assert entry["last_good"] == "abc"

    def test_with_nav_structure(self):
        nav = [{"section": "A", "contents": ["a.qmd"]}]
        entry = _manifest_entry_from_result(self._make_result(nav_structure=nav))
        assert entry["structure"] == nav

    def test_without_nav_structure(self):
        entry = _manifest_entry_from_result(self._make_result(nav_structure=None))
        assert "structure" not in entry

    def test_with_prerendered(self):
        entry = _manifest_entry_from_result(self._make_result(prerendered=True))
        assert entry["prerendered"] is True

    def test_without_prerendered(self):
        entry = _manifest_entry_from_result(self._make_result(prerendered=False))
        assert "prerendered" not in entry

    def test_with_cached_pages(self):
        entry = _manifest_entry_from_result(self._make_result(cached_pages=["p.qmd"]))
        assert entry["cached_pages"] == ["p.qmd"]

    def test_without_cached_pages(self):
        entry = _manifest_entry_from_result(self._make_result(cached_pages=None))
        assert "cached_pages" not in entry

    def test_no_last_good(self):
        entry = _manifest_entry_from_result(self._make_result(last_good_sha=None))
        assert "last_good" not in entry

    def test_page_hashes_not_in_entry(self):
        """page_hashes lives in build-state.json, not in manifest."""
        entry = _manifest_entry_from_result(self._make_result(page_hashes={"p.qmd": "h1"}))
        assert "page_hashes" not in entry


# ---------------------------------------------------------------------------
# resolve_head_sha
# ---------------------------------------------------------------------------


class TestResolveHeadSha:
    def test_returns_sha_when_remote_exists(self):
        with patch("quarto_graft.build.ref_exists", side_effect=lambda ref: ref == "origin/feature"):
            with patch("quarto_graft.build.rev_parse", return_value="abc123def456"):
                result = resolve_head_sha("feature")
        assert result == "abc123def456"

    def test_falls_back_to_local(self):
        def _ref_exists(ref):
            return ref == "feature"

        with patch("quarto_graft.build.ref_exists", side_effect=_ref_exists):
            with patch("quarto_graft.build.rev_parse", return_value="local123"):
                result = resolve_head_sha("feature")
        assert result == "local123"

    def test_returns_none_when_branch_missing(self):
        with patch("quarto_graft.build.ref_exists", return_value=False):
            result = resolve_head_sha("nonexistent")
        assert result is None

    def test_returns_none_on_exception(self):
        with patch("quarto_graft.build.ref_exists", return_value=True):
            with patch("quarto_graft.build.rev_parse", side_effect=Exception("fail")):
                result = resolve_head_sha("feature")
        assert result is None


# ---------------------------------------------------------------------------
# BuildResult dataclass
# ---------------------------------------------------------------------------


class TestBuildResult:
    def test_defaults(self):
        r = BuildResult(
            branch="b",
            branch_key="bk",
            title="T",
            status="ok",
            head_sha="abc",
            last_good_sha="abc",
            built_at="now",
            exported_relpaths=[],
            exported_dest_paths=[],
        )
        assert r.nav_structure is None
        assert r.prerendered is False
        assert r.duration_secs == 0.0
        assert r.error_message is None
        assert r.page_hashes is None
        assert r.cached_pages is None

    def test_all_fields(self):
        r = BuildResult(
            branch="b",
            branch_key="bk",
            title="T",
            status="fallback",
            head_sha="abc",
            last_good_sha="def",
            built_at="now",
            exported_relpaths=["p.qmd"],
            exported_dest_paths=[Path("p.qmd")],
            nav_structure=[{"section": "A"}],
            prerendered=True,
            duration_secs=1.5,
            error_message="oops",
            page_hashes={"p.qmd": "h"},
            cached_pages=["p.qmd"],
        )
        assert r.status == "fallback"
        assert r.prerendered is True
        assert r.duration_secs == 1.5
        assert r.error_message == "oops"
