"""Pydantic schemas for the feature memory.

Pure data definitions. No I/O. No business logic.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class BlobMetadata(BaseModel):
    """Identifier returned by Storage on read/write for optimistic locking.

    For S3, `etag` is the object ETag. For local FS, we synthesize an ETag
    from a sha256 of the file contents at read time so the same `if_match`
    contract works in both backends.
    """

    model_config = ConfigDict(extra="forbid")

    etag: str | None = None
    version_id: str | None = None


class Config(BaseModel):
    """Server configuration derived from CLI args + environment.

    All fields are optional / have defaults so the V1 stdio/local flow still
    works with zero env vars. S3 + OpenAI are only required when their
    respective backends are selected.
    """

    model_config = ConfigDict(extra="forbid")

    storage_backend: Literal["local", "s3"] = "local"
    features_dir: Path | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_prefix: str = ""
    openai_api_key: str | None = None
    openai_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    transport: Literal["stdio", "streamable-http"] = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8080
    auth_header: str | None = None
    cache_debounce_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        """Build a config from environment variables. CLI args override this."""
        return cls(
            storage_backend=os.environ.get("STORAGE_BACKEND", "local"),  # type: ignore[arg-type]
            features_dir=(
                Path(os.environ["FEATURES_DIR"]).resolve()
                if os.environ.get("FEATURES_DIR")
                else None
            ),
            s3_bucket=os.environ.get("S3_BUCKET") or None,
            s3_region=os.environ.get("AWS_REGION", "us-east-1"),
            s3_prefix=os.environ.get("S3_PREFIX", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
            openai_model=os.environ.get("OPENAI_MODEL", "text-embedding-3-small"),
            embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1536")),
            transport=os.environ.get("MCP_TRANSPORT", "stdio"),  # type: ignore[arg-type]
            mcp_host=os.environ.get("MCP_HOST", "0.0.0.0"),
            mcp_port=int(os.environ.get("MCP_PORT", "8080")),
            auth_header=os.environ.get("AUTH_HEADER") or None,
            cache_debounce_seconds=int(os.environ.get("CACHE_DEBOUNCE_SECONDS", "60")),
        )


class UpdateEntry(BaseModel):
    """The single line under `## Last Update`.

    Overwritten on every `update_feature` / `correct_feature` / `archive_feature`
    call. We deliberately do NOT keep an append-only history list — the goal is
    a small, high-signal context payload, not an audit log.
    """

    model_config = ConfigDict(frozen=True)

    date: date
    author: str = Field(min_length=1)
    change: str = Field(min_length=1)


class Frontmatter(BaseModel):
    """The YAML frontmatter block at the top of a feature file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    slug: str = Field(pattern=SLUG_PATTERN)
    summary: str = Field(min_length=1)
    key_paths: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    parent_feature: str | None = Field(default=None, pattern=SLUG_PATTERN)
    tags: list[str] = Field(default_factory=list)
    created_at: date
    updated_at: date

    @field_validator("key_paths", "dependencies", "tags")
    @classmethod
    def _no_blank_items(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item or not item.strip():
                raise ValueError("list items must be non-empty strings")
        return v


class FeatureBody(BaseModel):
    """Parsed body of a feature file, split into known sections.

    Unknown sections (anything outside the standard set) are kept verbatim
    in `extra_sections` so we never lose user-authored content.
    """

    model_config = ConfigDict(extra="forbid")

    overview: str = ""
    architecture: str = ""
    flows: list[str] = Field(default_factory=list)
    gotchas: list[str] = Field(default_factory=list)
    last_update: UpdateEntry | None = None
    extra_sections: list[tuple[str, str]] = Field(default_factory=list)


class Feature(BaseModel):
    """A complete feature: frontmatter + parsed body."""

    model_config = ConfigDict(extra="forbid")

    frontmatter: Frontmatter
    body: FeatureBody


class IndexEntry(BaseModel):
    """One row of `index.json` and the `list_features` MCP tool result."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    summary: str
    key_paths: list[str]
    tags: list[str]
    parent_feature: str | None = None


class FeaturePatch(BaseModel):
    """Append-and-merge patch applied by `update_feature`.

    Only `add_*` lists, an optional summary override, an optional parent
    change, optional notes append, and a required `last_update` entry. The
    agent cannot send a full body — that is the point. `last_update` is the
    one and only freshness signal: it overwrites the previous entry rather
    than appending.
    """

    model_config = ConfigDict(extra="forbid")

    summary_override: str | None = None
    add_flows: list[str] = Field(default_factory=list)
    add_gotchas: list[str] = Field(default_factory=list)
    add_dependencies: list[str] = Field(default_factory=list)
    add_key_paths: list[str] = Field(default_factory=list)
    set_parent_feature: str | None = None
    clear_parent_feature: bool = False
    last_update: UpdateEntry
    notes_append: str | None = None

    @field_validator("set_parent_feature")
    @classmethod
    def _validate_parent_slug(cls, v: str | None) -> str | None:
        if v is None:
            return v
        import re

        if not re.match(SLUG_PATTERN, v):
            raise ValueError(f"set_parent_feature must be a valid slug: {v!r}")
        return v


# --- Correction operations (discriminated union) -----------------------------


class _CorrectionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class RemoveFlow(_CorrectionBase):
    op: Literal["remove_flow"] = "remove_flow"
    text: str = Field(min_length=1)


class RemoveGotcha(_CorrectionBase):
    op: Literal["remove_gotcha"] = "remove_gotcha"
    text: str = Field(min_length=1)


class RemoveKeyPath(_CorrectionBase):
    op: Literal["remove_key_path"] = "remove_key_path"
    path: str = Field(min_length=1)


class RemoveDependency(_CorrectionBase):
    op: Literal["remove_dependency"] = "remove_dependency"
    slug: str = Field(pattern=SLUG_PATTERN)


class ReplaceSummary(_CorrectionBase):
    op: Literal["replace_summary"] = "replace_summary"
    new_summary: str = Field(min_length=1)


Correction = Annotated[
    Union[RemoveFlow, RemoveGotcha, RemoveKeyPath, RemoveDependency, ReplaceSummary],
    Field(discriminator="op"),
]


# --- Tool result types -------------------------------------------------------


class UpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    diff: str
    warnings: list[str] = Field(default_factory=list)


class CorrectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    diff: str


class ArchiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    archived_path: str


class GetFeatureResult(BaseModel):
    """Response shape for `get_feature`."""

    model_config = ConfigDict(extra="forbid")

    frontmatter: dict
    body_markdown: str


class CreateFeatureResult(BaseModel):
    """Response shape for `create_feature`."""

    model_config = ConfigDict(extra="forbid")

    slug: str
