"""Unit tests for `feature_memory.store`."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from feature_memory.models import Feature, FeatureBody, Frontmatter, UpdateEntry
from feature_memory.store import (
    ARCHIVED_DIR_NAME,
    FeatureArchived,
    FeatureNotFound,
    SlugCollision,
    archive_path,
    derive_unique_slug,
    feature_path,
    list_slugs,
    move_to_archive,
    parse_body,
    read_feature,
    serialize_body,
    slugify,
    write_feature,
)


@pytest.fixture
def features_dir(tmp_path: Path) -> Path:
    d = tmp_path / "features"
    d.mkdir()
    (d / ARCHIVED_DIR_NAME).mkdir()
    return d


def _sample_feature(slug: str = "quick-task", name: str = "Quick Task") -> Feature:
    return Feature(
        frontmatter=Frontmatter(
            name=name,
            slug=slug,
            summary="Lightweight tasks for users.",
            key_paths=["src/quick-task/**"],
            dependencies=[],
            tags=["tasks"],
            created_at=date(2026, 4, 23),
            updated_at=date(2026, 4, 23),
        ),
        body=FeatureBody(
            overview="Tasks users can spin up quickly.",
            architecture="Frontend in React, backend in Flask.",
            flows=["User creates task -> stored in db"],
            gotchas=["[CRITICAL] Bulk import bypasses audit log"],
            last_update=UpdateEntry(
                date=date(2026, 4, 23), author="rafael", change="Initial extraction"
            ),
        ),
    )


class TestSlugify:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Quick Task", "quick-task"),
            ("  Quick   Task  ", "quick-task"),
            ("Quick/Task", "quick-task"),
            ("QUICK_TASK", "quick-task"),
            ("My Feature 2", "my-feature-2"),
            ("Hello, World!", "hello-world"),
        ],
    )
    def test_basic(self, name: str, expected: str) -> None:
        assert slugify(name) == expected

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            slugify("!!!")

    def test_unicode_normalization(self) -> None:
        assert slugify("Café Menu") == "cafe-menu"


class TestDeriveUniqueSlug:
    def test_no_collision(self, features_dir: Path) -> None:
        assert derive_unique_slug("Quick Task", features_dir) == "quick-task"

    def test_collision_with_active(self, features_dir: Path) -> None:
        (features_dir / "quick-task.md").write_text("---\n---\n")
        assert derive_unique_slug("Quick Task", features_dir) == "quick-task-2"

    def test_collision_with_archived(self, features_dir: Path) -> None:
        (features_dir / ARCHIVED_DIR_NAME / "quick-task.md").write_text("---\n---\n")
        assert derive_unique_slug("Quick Task", features_dir) == "quick-task-2"

    def test_chains_correctly(self, features_dir: Path) -> None:
        (features_dir / "quick-task.md").write_text("---\n---\n")
        (features_dir / "quick-task-2.md").write_text("---\n---\n")
        assert derive_unique_slug("Quick Task", features_dir) == "quick-task-3"


class TestListSlugs:
    def test_empty(self, features_dir: Path) -> None:
        assert list_slugs(features_dir) == []

    def test_excludes_archived_and_dotfiles(self, features_dir: Path) -> None:
        (features_dir / "a.md").write_text("---\n---\n")
        (features_dir / "b.md").write_text("---\n---\n")
        (features_dir / ".hidden.md").write_text("---\n---\n")
        (features_dir / ARCHIVED_DIR_NAME / "old.md").write_text("---\n---\n")
        assert list_slugs(features_dir) == ["a", "b"]

    def test_missing_dir(self, tmp_path: Path) -> None:
        assert list_slugs(tmp_path / "nope") == []


class TestRoundTrip:
    def test_write_then_read(self, features_dir: Path) -> None:
        original = _sample_feature()
        path = write_feature(original, features_dir)
        assert path == feature_path("quick-task", features_dir)
        loaded = read_feature("quick-task", features_dir)
        assert loaded == original

    def test_critical_prefix_preserved(self, features_dir: Path) -> None:
        feat = _sample_feature()
        feat.body.gotchas.append("[CRITICAL] Another important thing")
        write_feature(feat, features_dir)
        loaded = read_feature("quick-task", features_dir)
        assert loaded.body.gotchas == feat.body.gotchas

    def test_unknown_section_preserved(self, features_dir: Path) -> None:
        feat = _sample_feature()
        feat.body.extra_sections = [("Notes for QA", "Run smoke before merge.")]
        write_feature(feat, features_dir)
        loaded = read_feature("quick-task", features_dir)
        assert ("Notes for QA", "Run smoke before merge.") in loaded.body.extra_sections


class TestReadErrors:
    def test_missing_raises(self, features_dir: Path) -> None:
        with pytest.raises(FeatureNotFound):
            read_feature("nope", features_dir)

    def test_archived_raises(self, features_dir: Path) -> None:
        (features_dir / ARCHIVED_DIR_NAME / "old.md").write_text(
            "---\nname: Old\nslug: old\nsummary: x\ncreated_at: 2026-04-23\nupdated_at: 2026-04-23\n---\n"
        )
        with pytest.raises(FeatureArchived) as exc:
            read_feature("old", features_dir)
        assert exc.value.slug == "old"


class TestArchive:
    def test_move(self, features_dir: Path) -> None:
        write_feature(_sample_feature(), features_dir)
        dst = move_to_archive("quick-task", features_dir)
        assert dst == archive_path("quick-task", features_dir)
        assert dst.exists()
        assert not feature_path("quick-task", features_dir).exists()

    def test_collision_blocks(self, features_dir: Path) -> None:
        write_feature(_sample_feature(), features_dir)
        archived = archive_path("quick-task", features_dir)
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text("---\n---\n")
        with pytest.raises(SlugCollision):
            move_to_archive("quick-task", features_dir)

    def test_missing_raises(self, features_dir: Path) -> None:
        with pytest.raises(FeatureNotFound):
            move_to_archive("nope", features_dir)


class TestParseBody:
    def test_minimal(self) -> None:
        body = parse_body("")
        assert body.overview == ""
        assert body.flows == []

    def test_known_sections(self) -> None:
        text = """## Overview
Hello.

## Flows
- step one
- step two

## Gotchas
- watch out

## Last Update
- 2026-04-23 - rafael - initial
"""
        body = parse_body(text)
        assert body.overview == "Hello."
        assert body.flows == ["step one", "step two"]
        assert body.gotchas == ["watch out"]
        assert body.last_update is not None
        assert body.last_update.author == "rafael"

    def test_unknown_section_preserved(self) -> None:
        text = """## Overview
Hi.

## Custom
freeform stuff
"""
        body = parse_body(text)
        assert ("Custom", "freeform stuff") in body.extra_sections

    def test_unparseable_last_update_preserved(self) -> None:
        text = """## Last Update
- not a real entry
- 2026-04-23 - rafael - real one
"""
        body = parse_body(text)
        assert body.last_update is not None
        assert body.last_update.change == "real one"
        assert any("not a real entry" in c for _, c in body.extra_sections)

    def test_legacy_history_section_collapses_to_latest(self) -> None:
        text = """## History
- 2026-01-01 - r - oldest seed
- 2026-04-26 - agent - newest change
- 2026-03-10 - rafael - middle change
"""
        body = parse_body(text)
        assert body.last_update is not None
        assert body.last_update.change == "newest change"
        assert body.last_update.date == date(2026, 4, 26)


class TestSerializeBody:
    def test_section_order(self) -> None:
        body = FeatureBody(
            overview="O",
            architecture="A",
            flows=["f1"],
            gotchas=["g1"],
            last_update=UpdateEntry(date=date(2026, 4, 23), author="r", change="c"),
        )
        out = serialize_body(body)
        idx_overview = out.index("## Overview")
        idx_architecture = out.index("## Architecture")
        idx_flows = out.index("## Flows")
        idx_gotchas = out.index("## Gotchas")
        idx_last_update = out.index("## Last Update")
        assert idx_overview < idx_architecture < idx_flows < idx_gotchas < idx_last_update

    def test_empty_sections_omitted(self) -> None:
        body = FeatureBody(overview="O")
        out = serialize_body(body)
        assert "## Architecture" not in out
        assert "## Flows" not in out
        assert "## Last Update" not in out

    def test_extras_between_known_and_last_update(self) -> None:
        body = FeatureBody(
            overview="O",
            extra_sections=[("Custom", "freeform")],
            last_update=UpdateEntry(date=date(2026, 4, 23), author="r", change="c"),
        )
        out = serialize_body(body)
        assert out.index("## Custom") < out.index("## Last Update")
