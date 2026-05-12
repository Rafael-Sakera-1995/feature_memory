"""Patch application.

A pure function: given an existing `Feature` and a `FeaturePatch`,
produce the new `Feature`, a unified diff between the old and new
serialized markdown, and any non-blocking warnings.

No I/O. No knowledge of disk paths or MCP. Easy to unit-test.
"""

from __future__ import annotations

import difflib
from datetime import date
from typing import Iterable

import yaml

from .models import Feature, FeatureBody, FeaturePatch, Frontmatter
from .store import serialize_body


SOFT_LIMIT_FLOWS = 25
SOFT_LIMIT_GOTCHAS = 40


def _dedup_extend(existing: list[str], new_items: Iterable[str]) -> list[str]:
    seen = set(existing)
    result = list(existing)
    for item in new_items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _serialize_for_diff(feature: Feature) -> str:
    fm_dict = feature.frontmatter.model_dump(mode="json", exclude_none=True)
    yaml_block = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True).strip()
    body_text = serialize_body(feature.body)
    return f"---\n{yaml_block}\n---\n\n{body_text}".rstrip() + "\n"


def _make_diff(before: Feature, after: Feature) -> str:
    before_text = _serialize_for_diff(before).splitlines(keepends=True)
    after_text = _serialize_for_diff(after).splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        before_text,
        after_text,
        fromfile=f"{before.frontmatter.slug}.md (before)",
        tofile=f"{after.frontmatter.slug}.md (after)",
        n=3,
    )
    return "".join(diff_lines)


def _size_warnings(feature: Feature) -> list[str]:
    warnings: list[str] = []
    n_flows = len(feature.body.flows)
    n_gotchas = len(feature.body.gotchas)
    if n_flows > SOFT_LIMIT_FLOWS:
        warnings.append(
            f"feature has {n_flows} flows (>{SOFT_LIMIT_FLOWS}); consider splitting into "
            f"sub-features via `parent_feature`"
        )
    if n_gotchas > SOFT_LIMIT_GOTCHAS:
        warnings.append(
            f"feature has {n_gotchas} gotchas (>{SOFT_LIMIT_GOTCHAS}); consider splitting into "
            f"sub-features via `parent_feature`"
        )
    return warnings


def apply_patch(
    feature: Feature,
    patch: FeaturePatch,
    *,
    today: date | None = None,
) -> tuple[Feature, str, list[str]]:
    """Apply `patch` to `feature` and return (new_feature, diff, warnings).

    Pure: does not touch disk, does not mutate inputs.

    Rules (per spec Section 5, with the V1.1 last_update change):
    - Lists merge by exact-string deduplication.
    - `summary` only changes if `summary_override` is set.
    - `parent_feature` changes only via `set_parent_feature` or `clear_parent_feature`.
    - `last_update` is required and OVERWRITES the previous entry (no history list).
    - `notes_append` is added as a freeform extra section.
    - `updated_at` is auto-bumped.
    """
    if patch.set_parent_feature is not None and patch.clear_parent_feature:
        raise ValueError(
            "FeaturePatch: set_parent_feature and clear_parent_feature are mutually exclusive"
        )

    today = today or date.today()
    fm = feature.frontmatter
    body = feature.body

    new_summary = patch.summary_override if patch.summary_override is not None else fm.summary
    new_dependencies = _dedup_extend(fm.dependencies, patch.add_dependencies)
    new_key_paths = _dedup_extend(fm.key_paths, patch.add_key_paths)

    if patch.clear_parent_feature:
        new_parent: str | None = None
    elif patch.set_parent_feature is not None:
        new_parent = patch.set_parent_feature
    else:
        new_parent = fm.parent_feature

    new_fm = Frontmatter(
        name=fm.name,
        slug=fm.slug,
        summary=new_summary,
        key_paths=new_key_paths,
        dependencies=new_dependencies,
        parent_feature=new_parent,
        tags=list(fm.tags),
        created_at=fm.created_at,
        updated_at=today,
    )

    new_flows = _dedup_extend(body.flows, patch.add_flows)
    new_gotchas = _dedup_extend(body.gotchas, patch.add_gotchas)

    new_extras = list(body.extra_sections)
    if patch.notes_append:
        new_extras.append(("Notes", patch.notes_append.strip()))

    new_body = FeatureBody(
        overview=body.overview,
        architecture=body.architecture,
        flows=new_flows,
        gotchas=new_gotchas,
        last_update=patch.last_update,
        extra_sections=new_extras,
    )

    new_feature = Feature(frontmatter=new_fm, body=new_body)
    diff = _make_diff(feature, new_feature)
    warnings = _size_warnings(new_feature)
    return new_feature, diff, warnings
