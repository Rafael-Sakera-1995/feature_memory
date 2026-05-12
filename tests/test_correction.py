"""Unit tests for `feature_memory.correction`."""

from __future__ import annotations

from datetime import date

import pytest

from feature_memory.correction import (
    CORRECTION_AUTHOR,
    CorrectionTargetNotFound,
    apply_corrections,
)
from feature_memory.models import (
    Feature,
    FeatureBody,
    Frontmatter,
    RemoveDependency,
    RemoveFlow,
    RemoveGotcha,
    RemoveKeyPath,
    ReplaceSummary,
    UpdateEntry,
)


TODAY = date(2026, 4, 23)


def _feature() -> Feature:
    fm = Frontmatter(
        name="Quick Task",
        slug="quick-task",
        summary="old summary",
        key_paths=["src/quick-task/**", "api/quick-task/**"],
        dependencies=["onboarding", "billing"],
        tags=["t"],
        created_at=date(2026, 1, 1),
        updated_at=date(2026, 1, 1),
    )
    body = FeatureBody(
        overview="o",
        flows=["existing flow A", "existing flow B"],
        gotchas=["gotcha 1", "gotcha 2"],
        last_update=UpdateEntry(date=date(2026, 1, 1), author="r", change="seed"),
    )
    return Feature(frontmatter=fm, body=body)


class TestRemovals:
    def test_remove_flow(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f,
            [RemoveFlow(text="existing flow A", reason="obsolete")],
            today=TODAY,
        )
        assert new.body.flows == ["existing flow B"]

    def test_remove_gotcha(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f, [RemoveGotcha(text="gotcha 1", reason="fixed")], today=TODAY
        )
        assert new.body.gotchas == ["gotcha 2"]

    def test_remove_key_path(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f, [RemoveKeyPath(path="api/quick-task/**", reason="api gone")], today=TODAY
        )
        assert new.frontmatter.key_paths == ["src/quick-task/**"]

    def test_remove_dependency(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f, [RemoveDependency(slug="billing", reason="decoupled")], today=TODAY
        )
        assert new.frontmatter.dependencies == ["onboarding"]


class TestReplaceSummary:
    def test_replace(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f, [ReplaceSummary(new_summary="brand new", reason="old was wrong")],
            today=TODAY,
        )
        assert new.frontmatter.summary == "brand new"


class TestErrors:
    def test_missing_target_raises(self) -> None:
        f = _feature()
        with pytest.raises(CorrectionTargetNotFound) as exc:
            apply_corrections(
                f, [RemoveFlow(text="not present", reason="x")], today=TODAY
            )
        assert exc.value.op == "remove_flow"
        assert exc.value.target == "not present"
        assert exc.value.slug == "quick-task"

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_corrections(_feature(), [], today=TODAY)


class TestLastUpdateAndDates:
    def test_last_correction_wins_in_last_update(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f,
            [
                RemoveFlow(text="existing flow A", reason="obsolete"),
                RemoveGotcha(text="gotcha 1", reason="fixed"),
                ReplaceSummary(new_summary="new", reason="cleaner"),
            ],
            today=TODAY,
        )
        assert new.body.last_update is not None
        assert new.body.last_update.author == CORRECTION_AUTHOR
        assert "Replaced summary" in new.body.last_update.change
        assert "cleaner" in new.body.last_update.change

    def test_single_correction_overwrites_seed(self) -> None:
        f = _feature()
        assert f.body.last_update is not None
        assert f.body.last_update.change == "seed"
        new, _ = apply_corrections(
            f, [RemoveFlow(text="existing flow A", reason="obsolete")], today=TODAY
        )
        assert new.body.last_update is not None
        assert new.body.last_update.author == CORRECTION_AUTHOR
        assert "Removed flow" in new.body.last_update.change
        assert "obsolete" in new.body.last_update.change

    def test_updated_at_bumped(self) -> None:
        f = _feature()
        new, _ = apply_corrections(
            f, [RemoveFlow(text="existing flow A", reason="x")], today=TODAY
        )
        assert new.frontmatter.updated_at == TODAY
        assert new.frontmatter.created_at == date(2026, 1, 1)


class TestDiff:
    def test_diff_is_unified(self) -> None:
        f = _feature()
        _, diff = apply_corrections(
            f, [RemoveFlow(text="existing flow A", reason="x")], today=TODAY
        )
        assert diff.startswith("--- ")
        assert "-- existing flow A" in diff
