"""Correction operations.

Pure: given an existing `Feature` and a list of `Correction` ops, produce
the new `Feature` and a unified diff. Each correction overwrites the
`last_update` entry with a description of what was removed and the
user's stated reason. When multiple corrections run in one call, the
final correction's entry is what survives in `last_update`.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from .merge import _make_diff
from .models import (
    Correction,
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


CORRECTION_AUTHOR = "correction"


class CorrectionTargetNotFound(Exception):
    """Raised when a remove_* op is given a string that doesn't exist."""

    def __init__(self, op: str, target: str, slug: str) -> None:
        super().__init__(
            f"{op}: target {target!r} not found in feature {slug!r}; corrections are exact-match"
        )
        self.op = op
        self.target = target
        self.slug = slug


def _remove_exact(items: list[str], target: str, op: str, slug: str) -> list[str]:
    if target not in items:
        raise CorrectionTargetNotFound(op, target, slug)
    return [item for item in items if item != target]


def _entry_for_correction(correction: Correction, today: date) -> UpdateEntry:
    if isinstance(correction, RemoveFlow):
        change = f"Removed flow: {correction.text!r} - {correction.reason}"
    elif isinstance(correction, RemoveGotcha):
        change = f"Removed gotcha: {correction.text!r} - {correction.reason}"
    elif isinstance(correction, RemoveKeyPath):
        change = f"Removed key_path: {correction.path!r} - {correction.reason}"
    elif isinstance(correction, RemoveDependency):
        change = f"Removed dependency: {correction.slug!r} - {correction.reason}"
    elif isinstance(correction, ReplaceSummary):
        change = f"Replaced summary - {correction.reason}"
    else:  # pragma: no cover - exhaustive on the union
        raise ValueError(f"unknown correction type: {type(correction).__name__}")
    return UpdateEntry(date=today, author=CORRECTION_AUTHOR, change=change)


def apply_corrections(
    feature: Feature,
    corrections: Sequence[Correction],
    *,
    today: date | None = None,
) -> tuple[Feature, str]:
    """Apply each correction in order. Returns (new_feature, diff)."""
    if not corrections:
        raise ValueError("corrections list must not be empty")

    today = today or date.today()
    fm = feature.frontmatter
    body = feature.body

    flows = list(body.flows)
    gotchas = list(body.gotchas)
    key_paths = list(fm.key_paths)
    dependencies = list(fm.dependencies)
    summary = fm.summary
    last_update = body.last_update

    for correction in corrections:
        if isinstance(correction, RemoveFlow):
            flows = _remove_exact(flows, correction.text, "remove_flow", fm.slug)
        elif isinstance(correction, RemoveGotcha):
            gotchas = _remove_exact(gotchas, correction.text, "remove_gotcha", fm.slug)
        elif isinstance(correction, RemoveKeyPath):
            key_paths = _remove_exact(key_paths, correction.path, "remove_key_path", fm.slug)
        elif isinstance(correction, RemoveDependency):
            dependencies = _remove_exact(
                dependencies, correction.slug, "remove_dependency", fm.slug
            )
        elif isinstance(correction, ReplaceSummary):
            summary = correction.new_summary
        else:  # pragma: no cover
            raise ValueError(f"unknown correction type: {type(correction).__name__}")

        last_update = _entry_for_correction(correction, today)

    new_fm = Frontmatter(
        name=fm.name,
        slug=fm.slug,
        summary=summary,
        key_paths=key_paths,
        dependencies=dependencies,
        parent_feature=fm.parent_feature,
        tags=list(fm.tags),
        created_at=fm.created_at,
        updated_at=today,
    )
    new_body = FeatureBody(
        overview=body.overview,
        architecture=body.architecture,
        flows=flows,
        gotchas=gotchas,
        last_update=last_update,
        extra_sections=list(body.extra_sections),
    )
    new_feature = Feature(frontmatter=new_fm, body=new_body)
    diff = _make_diff(feature, new_feature)
    return new_feature, diff
