"""Unit tests for `feature_memory.index`."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from feature_memory.index import INDEX_FILENAME, build_index, read_index, write_index
from feature_memory.models import Feature, FeatureBody, Frontmatter
from feature_memory.store import ARCHIVED_DIR_NAME, write_feature


@pytest.fixture
def features_dir(tmp_path: Path) -> Path:
    d = tmp_path / "features"
    d.mkdir()
    (d / ARCHIVED_DIR_NAME).mkdir()
    return d


def _feature(slug: str, name: str, **fm_overrides) -> Feature:
    fm_kwargs = dict(
        name=name,
        slug=slug,
        summary=f"summary for {name}",
        key_paths=[f"src/{slug}/**"],
        tags=["t"],
        created_at=date(2026, 4, 23),
        updated_at=date(2026, 4, 23),
    )
    fm_kwargs.update(fm_overrides)
    return Feature(frontmatter=Frontmatter(**fm_kwargs), body=FeatureBody())


class TestBuildIndex:
    def test_empty(self, features_dir: Path) -> None:
        assert build_index(features_dir) == []

    def test_single(self, features_dir: Path) -> None:
        write_feature(_feature("quick-task", "Quick Task"), features_dir)
        entries = build_index(features_dir)
        assert len(entries) == 1
        assert entries[0].slug == "quick-task"
        assert entries[0].key_paths == ["src/quick-task/**"]

    def test_multiple_sorted_by_slug(self, features_dir: Path) -> None:
        write_feature(_feature("zeta", "Zeta"), features_dir)
        write_feature(_feature("alpha", "Alpha"), features_dir)
        slugs = [e.slug for e in build_index(features_dir)]
        assert slugs == ["alpha", "zeta"]

    def test_excludes_archived(self, features_dir: Path) -> None:
        write_feature(_feature("active", "Active"), features_dir)
        archived = features_dir / ARCHIVED_DIR_NAME / "old.md"
        archived.write_text(
            "---\nname: Old\nslug: old\nsummary: x\nkey_paths: []\n"
            "dependencies: []\ntags: []\ncreated_at: 2026-04-23\nupdated_at: 2026-04-23\n---\n"
        )
        slugs = [e.slug for e in build_index(features_dir)]
        assert slugs == ["active"]

    def test_carries_parent_feature(self, features_dir: Path) -> None:
        write_feature(_feature("parent", "Parent"), features_dir)
        write_feature(
            _feature("parent-child", "Parent Child", parent_feature="parent"),
            features_dir,
        )
        by_slug = {e.slug: e for e in build_index(features_dir)}
        assert by_slug["parent-child"].parent_feature == "parent"
        assert by_slug["parent"].parent_feature is None


class TestWriteIndex:
    def test_writes_json(self, features_dir: Path) -> None:
        write_feature(_feature("quick-task", "Quick Task"), features_dir)
        path = write_index(features_dir)
        assert path == features_dir / INDEX_FILENAME
        data = json.loads(path.read_text())
        assert isinstance(data, list)
        assert data[0]["slug"] == "quick-task"
        assert "parent_feature" not in data[0]

    def test_overwrites(self, features_dir: Path) -> None:
        write_feature(_feature("a", "A"), features_dir)
        write_index(features_dir)
        write_feature(_feature("b", "B"), features_dir)
        path = write_index(features_dir)
        slugs = [row["slug"] for row in json.loads(path.read_text())]
        assert slugs == ["a", "b"]


class TestReadIndex:
    def test_missing_file_returns_none(self, features_dir: Path) -> None:
        assert read_index(features_dir) is None

    def test_returns_entries_when_fresh(self, features_dir: Path) -> None:
        write_feature(_feature("a", "A"), features_dir)
        write_feature(_feature("b", "B"), features_dir)
        write_index(features_dir)
        cached = read_index(features_dir)
        assert cached is not None
        assert [e.slug for e in cached] == ["a", "b"]

    def test_stale_when_md_newer_than_index(self, features_dir: Path) -> None:
        import os
        import time

        write_feature(_feature("a", "A"), features_dir)
        write_index(features_dir)
        time.sleep(0.01)
        md = features_dir / "a.md"
        # Touch the .md to a newer mtime; do NOT rewrite the index.
        new_mtime = md.stat().st_mtime + 5
        os.utime(md, (new_mtime, new_mtime))
        assert read_index(features_dir) is None

    def test_stale_when_slug_set_differs(self, features_dir: Path) -> None:
        write_feature(_feature("a", "A"), features_dir)
        write_index(features_dir)
        # Add a new feature on disk without rebuilding the index. The new file's
        # mtime IS newer than the index too, but we also want to make sure the
        # slug-set check catches added-files.
        write_feature(_feature("b", "B"), features_dir)
        # Force the index to look "older than the new file" — already true via
        # write order, but spell it out.
        assert read_index(features_dir) is None

    def test_stale_when_slug_removed_outside_mcp(
        self, features_dir: Path
    ) -> None:
        write_feature(_feature("a", "A"), features_dir)
        write_feature(_feature("b", "B"), features_dir)
        write_index(features_dir)
        # Manual delete (e.g. git checkout) — index now lists a slug that
        # doesn't exist on disk. mtime check passes but slug-set differs.
        (features_dir / "b.md").unlink()
        assert read_index(features_dir) is None

    def test_malformed_json_returns_none(self, features_dir: Path) -> None:
        write_feature(_feature("a", "A"), features_dir)
        (features_dir / INDEX_FILENAME).write_text("{not valid json")
        assert read_index(features_dir) is None

    def test_invalid_schema_returns_none(self, features_dir: Path) -> None:
        write_feature(_feature("a", "A"), features_dir)
        (features_dir / INDEX_FILENAME).write_text(
            json.dumps([{"slug": "a", "wrong_shape": True}])
        )
        assert read_index(features_dir) is None
