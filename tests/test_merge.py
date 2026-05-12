"""Unit tests for `feature_memory.merge`."""

from __future__ import annotations

from datetime import date

import pytest

from feature_memory.merge import (
    SOFT_LIMIT_FLOWS,
    SOFT_LIMIT_GOTCHAS,
    apply_patch,
)
from feature_memory.models import Feature, FeatureBody, FeaturePatch, Frontmatter, UpdateEntry


def _feature(**overrides) -> Feature:
    body_overrides = overrides.pop("body", {})
    fm = Frontmatter(
        name="Quick Task",
        slug="quick-task",
        summary="Lightweight tasks for users.",
        key_paths=["src/quick-task/**"],
        dependencies=["onboarding"],
        tags=["tasks"],
        created_at=date(2026, 1, 1),
        updated_at=date(2026, 1, 1),
        **overrides,
    )
    body_kwargs = dict(
        overview="Tasks users can spin up quickly.",
        flows=["existing flow"],
        gotchas=["existing gotcha"],
        last_update=UpdateEntry(date=date(2026, 1, 1), author="r", change="seed"),
    )
    body_kwargs.update(body_overrides)
    return Feature(frontmatter=fm, body=FeatureBody(**body_kwargs))


def _patch(**overrides) -> FeaturePatch:
    base = dict(
        last_update=UpdateEntry(
            date=date(2026, 4, 23), author="rafael", change="did stuff"
        )
    )
    base.update(overrides)
    return FeaturePatch(**base)


TODAY = date(2026, 4, 23)


class TestAdditive:
    def test_adds_flows_dedup(self) -> None:
        f = _feature()
        new, diff, warnings = apply_patch(
            f,
            _patch(add_flows=["existing flow", "new flow"]),
            today=TODAY,
        )
        assert new.body.flows == ["existing flow", "new flow"]
        assert "new flow" in diff
        assert warnings == []

    def test_adds_gotchas_dedup(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(
            f, _patch(add_gotchas=["existing gotcha", "fresh gotcha"]), today=TODAY
        )
        assert new.body.gotchas == ["existing gotcha", "fresh gotcha"]

    def test_adds_dependencies_dedup(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(
            f, _patch(add_dependencies=["onboarding", "billing"]), today=TODAY
        )
        assert new.frontmatter.dependencies == ["onboarding", "billing"]

    def test_adds_key_paths_dedup(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(
            f, _patch(add_key_paths=["src/quick-task/**", "api/quick-task/**"]), today=TODAY
        )
        assert new.frontmatter.key_paths == ["src/quick-task/**", "api/quick-task/**"]


class TestLastUpdateAndDates:
    def test_last_update_overwritten(self) -> None:
        f = _feature()
        assert f.body.last_update is not None and f.body.last_update.change == "seed"
        new, _, _ = apply_patch(f, _patch(), today=TODAY)
        assert new.body.last_update is not None
        assert new.body.last_update.change == "did stuff"
        assert new.body.last_update.author == "rafael"

    def test_updated_at_bumped(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(f, _patch(), today=TODAY)
        assert new.frontmatter.updated_at == TODAY
        assert new.frontmatter.created_at == date(2026, 1, 1)


class TestSummaryAndParent:
    def test_summary_unchanged_by_default(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(f, _patch(), today=TODAY)
        assert new.frontmatter.summary == f.frontmatter.summary

    def test_summary_override(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(
            f, _patch(summary_override="A new summary."), today=TODAY
        )
        assert new.frontmatter.summary == "A new summary."

    def test_set_parent_feature(self) -> None:
        f = _feature()
        new, _, _ = apply_patch(
            f, _patch(set_parent_feature="parent-thing"), today=TODAY
        )
        assert new.frontmatter.parent_feature == "parent-thing"

    def test_clear_parent_feature(self) -> None:
        f = _feature(parent_feature="some-parent")
        new, _, _ = apply_patch(f, _patch(clear_parent_feature=True), today=TODAY)
        assert new.frontmatter.parent_feature is None

    def test_set_and_clear_mutually_exclusive(self) -> None:
        f = _feature()
        with pytest.raises(ValueError):
            apply_patch(
                f,
                _patch(set_parent_feature="x", clear_parent_feature=True),
                today=TODAY,
            )


class TestNotesAppend:
    def test_notes_added_as_extra_section(self) -> None:
        f = _feature()
        new, diff, _ = apply_patch(
            f, _patch(notes_append="Some richer prose here."), today=TODAY
        )
        assert ("Notes", "Some richer prose here.") in new.body.extra_sections
        assert "## Notes" in diff


class TestSizeWarnings:
    def test_no_warning_when_below_limits(self) -> None:
        f = _feature()
        _, _, warnings = apply_patch(f, _patch(add_flows=["x"]), today=TODAY)
        assert warnings == []

    def test_warning_when_flows_exceed_limit(self) -> None:
        many = [f"flow {i}" for i in range(SOFT_LIMIT_FLOWS + 5)]
        f = _feature(body={"flows": many})
        _, _, warnings = apply_patch(f, _patch(), today=TODAY)
        assert any("flows" in w and "splitting" in w for w in warnings)

    def test_warning_when_gotchas_exceed_limit(self) -> None:
        many = [f"gotcha {i}" for i in range(SOFT_LIMIT_GOTCHAS + 5)]
        f = _feature(body={"gotchas": many})
        _, _, warnings = apply_patch(f, _patch(), today=TODAY)
        assert any("gotchas" in w and "splitting" in w for w in warnings)


class TestDiff:
    def test_diff_is_unified_format(self) -> None:
        f = _feature()
        _, diff, _ = apply_patch(f, _patch(add_flows=["new flow"]), today=TODAY)
        assert diff.startswith("--- ")
        assert "\n+++ " in diff
        assert "+- new flow" in diff

    def test_no_changes_still_bumps_last_update_and_date(self) -> None:
        f = _feature()
        new, diff, _ = apply_patch(f, _patch(), today=TODAY)
        assert diff != ""
        assert new.frontmatter.updated_at == TODAY
        assert new.body.last_update is not None
        assert new.body.last_update.change == "did stuff"
