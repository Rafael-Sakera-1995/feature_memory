"""FastMCP server.

Thin adapter over the engine modules: store, merge, correction, index.
Exposes six tools per the spec.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .correction import CorrectionTargetNotFound, apply_corrections
from .index import build_index, read_index, write_index
from .merge import apply_patch
from .models import (
    ArchiveResult,
    Correction,
    CorrectResult,
    CreateFeatureResult,
    Feature,
    FeaturePatch,
    Frontmatter,
    GetFeatureResult,
    IndexEntry,
    UpdateResult,
)
from .store import (
    ARCHIVED_DIR_NAME,
    FeatureArchived,
    FeatureNotFound,
    derive_unique_slug,
    move_to_archive,
    parse_body,
    read_feature,
    write_feature,
    serialize_body
)


logger = logging.getLogger(__name__)


SERVER_INSTRUCTIONS = """\
Feature Memory MCP — a local knowledge base of product features.

Use this server before planning a feature (`list_features`, `get_feature`)
to load expert context, and after coding (`update_feature`) to write back
what changed. Use `correct_feature` and `archive_feature` only when the
user explicitly asks for a correction or archival.
"""


def build_server(features_dir: Path) -> FastMCP:
    """Construct a FastMCP server bound to the given features' directory."""
    features_dir = features_dir.resolve()
    features_dir.mkdir(parents=True, exist_ok=True)
    (features_dir / ARCHIVED_DIR_NAME).mkdir(parents=True, exist_ok=True)

    mcp = FastMCP("feature-memory", instructions=SERVER_INSTRUCTIONS)

    @mcp.tool(
        description=(
            "Return the full index of active features. Each entry has slug, name, "
            "summary, key_paths, tags, and parent_feature. Used by the agent on the "
            "auto-detect fallback path to pick which feature(s) to fetch in full. "
            "Reads from the cached `index.json` when fresh; falls back to a full "
            "rebuild if the cache is missing or stale."
        )
    )
    def list_features() -> list[IndexEntry]:
        cached = read_index(features_dir)
        if cached is not None:
            return cached
        entries = build_index(features_dir)
        write_index(features_dir)
        return entries

    @mcp.tool(
        description=(
            "Return the full content of a single feature: frontmatter (as a dict) and "
            "body_markdown (the raw markdown body, ready to inject into agent context). "
            "Errors if the slug is missing or archived."
        )
    )
    def get_feature(
        slug: Annotated[str, Field(description="The feature's slug, e.g. 'quick-task'")],
    ) -> GetFeatureResult:
        try:
            feat = read_feature(slug, features_dir)
        except FeatureArchived as exc:
            raise ValueError(str(exc)) from exc
        except FeatureNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc

        return GetFeatureResult(
            frontmatter=feat.frontmatter.model_dump(mode="json", exclude_none=True),
            body_markdown=serialize_body(feat.body),
        )

    @mcp.tool(
        description=(
            "Append-and-merge update of a feature. Pass a FeaturePatch (typed delta — "
            "NOT a full rewrite). Server merges the patch into the existing file, "
            "overwrites `## Last Update` with the patch's `last_update` entry, dedupes "
            "lists, rewrites the .md file, and rebuilds the index. Returns a unified "
            "diff and any size warnings."
        )
    )
    def update_feature(
        slug: Annotated[str, Field(description="The feature's slug")],
        patch: FeaturePatch,
    ) -> UpdateResult:
        try:
            feat = read_feature(slug, features_dir)
        except FeatureArchived as exc:
            raise ValueError(str(exc)) from exc
        except FeatureNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc

        new_feat, diff, warnings = apply_patch(feat, patch)
        write_feature(new_feat, features_dir)
        write_index(features_dir)
        return UpdateResult(ok=True, diff=diff, warnings=warnings)

    @mcp.tool(
        description=(
            "Create a brand-new feature. The agent should call this only when "
            "the user is starting work on something that doesn't yet have a "
            "feature file. The slug is auto-derived from the name (with collision "
            "handling). Returns the new slug."
        )
    )
    def create_feature(
        name: Annotated[str, Field(description="Human-readable name, e.g. 'Quick Task'")],
        summary: Annotated[str, Field(description="One-line summary (~15 words)")],
        key_paths: Annotated[list[str], Field(description="Glob patterns matching feature files")] = [],
        body: Annotated[
            str,
            Field(
                description=(
                    "Markdown body with the standard sections (## Overview, ## Architecture, "
                    "## Flows, ## Gotchas). Can be empty; sections will be filled in over time."
                )
            ),
        ] = "",
        tags: Annotated[list[str], Field(description="Free-form labels")] = [],
        dependencies: Annotated[list[str], Field(description="Slugs of features this depends on")] = [],
        parent_feature: Annotated[
            str | None,
            Field(description="Optional parent feature slug if this is a sub-feature"),
        ] = None,
    ) -> CreateFeatureResult:
        slug = derive_unique_slug(name, features_dir)
        today = date.today()
        parsed_body = parse_body(body)
        feat = Feature(
            frontmatter=Frontmatter(
                name=name,
                slug=slug,
                summary=summary,
                key_paths=list(key_paths),
                dependencies=list(dependencies),
                parent_feature=parent_feature,
                tags=list(tags),
                created_at=today,
                updated_at=today,
            ),
            body=parsed_body,
        )
        write_feature(feat, features_dir)
        write_index(features_dir)
        return CreateFeatureResult(slug=slug)

    @mcp.tool(
        description=(
            "Apply surgical corrections to a feature. ONLY call this when the user "
            "explicitly asks to remove or fix something. Each correction must include "
            "a `reason`. Server validates exact-match removals (errors if the target "
            "doesn't exist) and overwrites `## Last Update` with an entry describing "
            "the correction (last correction wins when multiple are applied at once). "
            "Returns a unified diff."
        )
    )
    def correct_feature(
        slug: Annotated[str, Field(description="The feature's slug")],
        corrections: Annotated[
            list[Correction],
            Field(description="One or more correction operations"),
        ],
    ) -> CorrectResult:
        try:
            feat = read_feature(slug, features_dir)
        except FeatureArchived as exc:
            raise ValueError(str(exc)) from exc
        except FeatureNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc

        try:
            new_feat, diff = apply_corrections(feat, corrections)
        except CorrectionTargetNotFound as exc:
            raise ValueError(str(exc)) from exc

        write_feature(new_feat, features_dir)
        write_index(features_dir)
        return CorrectResult(ok=True, diff=diff)

    @mcp.tool(
        description=(
            "Soft-delete a feature. Moves the file to features/_archived/, removes "
            "from the index, and overwrites `## Last Update` with a final entry "
            "carrying the archive reason. ONLY call this when the user explicitly "
            "says the feature is obsolete. Reversible by hand."
        )
    )
    def archive_feature(
        slug: Annotated[str, Field(description="The feature's slug")],
        reason: Annotated[str, Field(description="Why this feature is being archived", min_length=1)],
    ) -> ArchiveResult:
        try:
            feat = read_feature(slug, features_dir)
        except FeatureArchived as exc:
            raise ValueError(str(exc)) from exc
        except FeatureNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc

        from .models import UpdateEntry

        feat.body.last_update = UpdateEntry(
            date=date.today(),
            author="archive",
            change=f"Archived - {reason}",
        )
        write_feature(feat, features_dir)
        new_path = move_to_archive(slug, features_dir)
        write_index(features_dir)
        return ArchiveResult(ok=True, archived_path=str(new_path))

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="feature-memory-mcp")
    parser.add_argument(
        "--features-dir",
        required=True,
        type=Path,
        help="Path to the features/ directory containing the markdown knowledge base.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)
    server = build_server(args.features_dir)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
