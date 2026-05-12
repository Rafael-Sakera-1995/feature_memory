"""Unit tests for `feature_memory.models`."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from feature_memory.models import (
    Correction,
    Feature,
    FeatureBody,
    FeaturePatch,
    Frontmatter,
    IndexEntry,
    RemoveDependency,
    RemoveFlow,
    RemoveGotcha,
    RemoveKeyPath,
    ReplaceSummary,
    UpdateEntry,
)


def _frontmatter(**overrides) -> Frontmatter:
    base = dict(
        name="Quick Task",
        slug="quick-task",
        summary="Lightweight tasks for users.",
        key_paths=["src/quick-task/**"],
        dependencies=[],
        tags=[],
        created_at=date(2026, 4, 23),
        updated_at=date(2026, 4, 23),
    )
    base.update(overrides)
    return Frontmatter(**base)


class TestFrontmatter:
    def test_minimal_valid(self) -> None:
        fm = _frontmatter()
        assert fm.slug == "quick-task"
        assert fm.parent_feature is None

    def test_invalid_slug(self) -> None:
        with pytest.raises(ValidationError):
            _frontmatter(slug="Quick Task")

    def test_blank_summary(self) -> None:
        with pytest.raises(ValidationError):
            _frontmatter(summary="")

    def test_blank_list_item(self) -> None:
        with pytest.raises(ValidationError):
            _frontmatter(tags=["   "])

    def test_parent_feature_slug_pattern(self) -> None:
        fm = _frontmatter(parent_feature="quick-task")
        assert fm.parent_feature == "quick-task"
        with pytest.raises(ValidationError):
            _frontmatter(parent_feature="Quick_Task")

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Frontmatter(
                name="X",
                slug="x",
                summary="x",
                created_at=date.today(),
                updated_at=date.today(),
                bogus="value",
            )


class TestFeatureBody:
    def test_defaults(self) -> None:
        body = FeatureBody()
        assert body.flows == []
        assert body.last_update is None
        assert body.extra_sections == []

    def test_extra_sections_preserved(self) -> None:
        body = FeatureBody(extra_sections=[("Notes", "freeform text")])
        assert body.extra_sections == [("Notes", "freeform text")]


class TestFeature:
    def test_compose(self) -> None:
        f = Feature(frontmatter=_frontmatter(), body=FeatureBody(overview="hi"))
        assert f.frontmatter.slug == "quick-task"
        assert f.body.overview == "hi"


class TestIndexEntry:
    def test_minimal(self) -> None:
        e = IndexEntry(
            slug="quick-task",
            name="Quick Task",
            summary="x",
            key_paths=["src/**"],
            tags=[],
        )
        assert e.parent_feature is None


class TestUpdateEntry:
    def test_valid(self) -> None:
        h = UpdateEntry(date=date(2026, 4, 23), author="rafael", change="did stuff")
        assert h.author == "rafael"

    def test_blank_change_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UpdateEntry(date=date.today(), author="x", change="")


class TestFeaturePatch:
    def _required(self, **extra) -> dict:
        base = {
            "last_update": {
                "date": date(2026, 4, 23),
                "author": "rafael",
                "change": "did stuff",
            }
        }
        base.update(extra)
        return base

    def test_minimal_patch(self) -> None:
        p = FeaturePatch(**self._required())
        assert p.add_flows == []
        assert p.set_parent_feature is None
        assert p.clear_parent_feature is False

    def test_last_update_required(self) -> None:
        with pytest.raises(ValidationError):
            FeaturePatch(add_flows=["x"])  # missing last_update

    def test_set_parent_validates_slug(self) -> None:
        with pytest.raises(ValidationError):
            FeaturePatch(**self._required(set_parent_feature="Bad Slug"))

    def test_set_parent_accepts_valid_slug(self) -> None:
        p = FeaturePatch(**self._required(set_parent_feature="quick-task"))
        assert p.set_parent_feature == "quick-task"

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            FeaturePatch(**self._required(bogus="x"))


class TestCorrectionDiscriminatedUnion:
    adapter = TypeAdapter(Correction)

    def test_remove_flow(self) -> None:
        c = self.adapter.validate_python(
            {"op": "remove_flow", "text": "old flow", "reason": "obsolete"}
        )
        assert isinstance(c, RemoveFlow)
        assert c.text == "old flow"

    def test_remove_gotcha(self) -> None:
        c = self.adapter.validate_python(
            {"op": "remove_gotcha", "text": "old", "reason": "fixed"}
        )
        assert isinstance(c, RemoveGotcha)

    def test_remove_key_path(self) -> None:
        c = self.adapter.validate_python(
            {"op": "remove_key_path", "path": "src/old/**", "reason": "deleted"}
        )
        assert isinstance(c, RemoveKeyPath)

    def test_remove_dependency(self) -> None:
        c = self.adapter.validate_python(
            {"op": "remove_dependency", "slug": "old-dep", "reason": "decoupled"}
        )
        assert isinstance(c, RemoveDependency)

    def test_replace_summary(self) -> None:
        c = self.adapter.validate_python(
            {"op": "replace_summary", "new_summary": "new", "reason": "old was wrong"}
        )
        assert isinstance(c, ReplaceSummary)

    def test_unknown_op_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.adapter.validate_python(
                {"op": "nuke_everything", "reason": "lol"}
            )

    def test_reason_required(self) -> None:
        with pytest.raises(ValidationError):
            self.adapter.validate_python(
                {"op": "remove_flow", "text": "x"}  # missing reason
            )

    def test_remove_dependency_slug_validated(self) -> None:
        with pytest.raises(ValidationError):
            self.adapter.validate_python(
                {"op": "remove_dependency", "slug": "Bad Slug", "reason": "x"}
            )
