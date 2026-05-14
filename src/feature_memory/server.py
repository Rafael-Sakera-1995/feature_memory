"""FastMCP server.

Two modes:

- **stdio** (V1 default): local single-user, talks to a `LocalFSStorage` on
  a directory. Used by Cursor's local MCP plumbing and by tests.
- **streamable-http** (V3): hosted at feature-memory.kosmos.connecteam.com.
  Talks to S3 (markdown) + S3 Vectors (semantic search) + Bedrock (embeddings).
  Derives the patch author from an HTTP auth header so the agent cannot
  self-attribute writes to someone else.

The tool surface is identical across modes; only the construction and
transport differ. All mutating tools go through the `Storage` abstraction
so the local and remote paths share one code path.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path
from typing import Annotated, Callable

import frontmatter
import yaml
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from . import audit
from .correction import CorrectionTargetNotFound, apply_corrections
from .merge import apply_patch
from .models import (
    ArchiveResult,
    Config,
    Correction,
    CorrectResult,
    CreateFeatureResult,
    Feature,
    FeatureBody,
    FeaturePatch,
    Frontmatter,
    GetFeatureResult,
    IndexEntry,
    UpdateEntry,
    UpdateResult,
)
from .search import Embedder, S3VectorsIndex, embed_text_for_entry
from .storage import (
    LocalFSStorage,
    S3Storage,
    Storage,
    StorageConflict,
    StorageNotFound,
    write_with_retry,
)
from .store import (
    derive_unique_slug,
    parse_body,
    serialize_body,
    slugify,
)


logger = logging.getLogger(__name__)


SERVER_INSTRUCTIONS = """\
Feature Memory MCP - a hosted knowledge base of product features.

Use this server before planning a feature (`list_features` / `search_features`,
then `get_feature`) to load expert context, and after coding (`update_feature`)
to write back what changed. Use `correct_feature` and `archive_feature` only
when the user explicitly asks for a correction or archival.
"""


# --- Feature <-> markdown helpers -------------------------------------------


def _feature_to_markdown(feature: Feature) -> str:
    fm_dict = feature.frontmatter.model_dump(mode="json", exclude_none=True)
    body_text = serialize_body(feature.body)
    yaml_block = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_block}\n---\n\n{body_text}".rstrip() + "\n"


def _markdown_to_feature(content: str) -> Feature:
    post = frontmatter.loads(content)
    fm = Frontmatter(**post.metadata)
    body = parse_body(post.content)
    return Feature(frontmatter=fm, body=body)


def _index_entry(feature: Feature) -> IndexEntry:
    fm = feature.frontmatter
    return IndexEntry(
        slug=fm.slug,
        name=fm.name,
        summary=fm.summary,
        key_paths=list(fm.key_paths),
        tags=list(fm.tags),
        parent_feature=fm.parent_feature,
    )


# --- Author resolution ------------------------------------------------------


AuthorResolver = Callable[[Context | None, UpdateEntry], str]


def _identity_author(ctx: Context | None, fallback: UpdateEntry) -> str:
    """Default: trust whatever the agent put in `patch.last_update.author`.

    Used for V1 stdio mode where there is no transport-level identity.
    """
    return fallback.author


def make_header_author_resolver(header_name: str) -> AuthorResolver:
    """Server-side author derivation for HTTP mode.

    Production deployment puts the user behind kosmos ingress, which adds a
    trusted identity header (e.g. `X-Connecteam-User: rafael`). We pull
    that out of the request and override `patch.last_update.author` so the
    audit trail cannot be spoofed by the agent.

    Falls back to the patch value if the header is missing (defense in
    depth so a misconfigured ingress doesn't black-hole all writes).
    """

    def _resolve(ctx: Context | None, fallback: UpdateEntry) -> str:
        if ctx is None:
            return fallback.author
        try:
            request = ctx.request_context.request  # type: ignore[union-attr]
            if request is None:
                return fallback.author
            value = request.headers.get(header_name)
            if value:
                return value.strip()
        except (AttributeError, KeyError):
            pass
        return fallback.author

    return _resolve


# --- Server construction ----------------------------------------------------


def _vector_metadata_for(feature: Feature) -> dict[str, str]:
    """Project a Feature into the slim metadata blob we attach to its vector.

    V3 design: vectors store only `name` and `summary` so `list_features`
    and `search_features` can return useful preview entries without any
    extra round-trips. Anything richer (tags, key_paths, body) lives in
    the .md file and is fetched via `get_feature(slug)`.
    """
    fm = feature.frontmatter
    return {"name": fm.name, "summary": fm.summary}


def _embed_text_for_feature(feature: Feature) -> str:
    """Text we feed to Bedrock when (re)indexing a feature.

    Mirrors `embed_text_for_entry` but works off the Feature directly so
    callers don't need to build an IndexEntry just to embed.
    """
    return embed_text_for_entry(_index_entry(feature))


def _reindex_feature(
    feature: Feature,
    *,
    embedder: Embedder | None,
    vectors: S3VectorsIndex | None,
) -> None:
    """Re-embed + upsert a single feature into S3 Vectors. No-op if disabled."""
    if vectors is None or embedder is None or not embedder.is_enabled():
        return
    vector = embedder.embed_one(_embed_text_for_feature(feature))
    vectors.upsert(feature.frontmatter.slug, vector, metadata=_vector_metadata_for(feature))


def _list_entries_from_storage(storage: Storage) -> list[dict]:
    """Build slim entries by reading every .md from storage. Local-mode fallback.

    O(N) S3 GETs (or local file reads) - fine for V1 stdio and tests but
    NOT what V3 production uses. Production hits `S3VectorsIndex.list_all()`
    which is paginated and returns metadata in-line with no extra GETs.
    """
    out: list[dict] = []
    for slug in storage.list_slugs():
        try:
            content, _ = storage.get_md(slug)
        except StorageNotFound:
            continue
        feat = _markdown_to_feature(content)
        out.append(_vector_metadata_for(feat) | {"slug": feat.frontmatter.slug})
    return out


def build_server(
    features_dir: Path | None = None,
    *,
    storage: Storage | None = None,
    embedder: Embedder | None = None,
    vectors: S3VectorsIndex | None = None,
    author_resolver: AuthorResolver = _identity_author,
) -> FastMCP:
    """Build a FastMCP server bound to a Storage backend.

    Two ways to call:

    - V1/local: `build_server(features_dir=...)` - LocalFSStorage with no
      vector backend. `search_features` returns []. `list_features` reads
      .md files directly. Used by stdio and every existing test.
    - V3/hosted: `build_server(storage=..., embedder=..., vectors=...)` -
      caller has constructed all AWS backends. `main()` uses this path
      for streamable-http. Server is fully stateless: every tool call
      hits S3 / S3 Vectors / Bedrock directly, nothing is cached in RAM.
    """
    if storage is None:
        if features_dir is None:
            raise ValueError("build_server requires either features_dir or storage")
        storage = LocalFSStorage(Path(features_dir).resolve())

    if embedder is None:
        embedder = Embedder(region="us-east-1", enabled=False)

    mcp = FastMCP("feature-memory", instructions=SERVER_INSTRUCTIONS)

    # --- list_features -----------------------------------------------------

    @mcp.tool(
        description=(
            "Return all active features as slim preview entries: each has slug, "
            "name, and summary. Used by the agent on the auto-detect fallback path "
            "to pick which feature to fetch in full via `get_feature`. In V3 this "
            "reads directly from S3 Vectors (one paginated ListVectors call) so "
            "it's stateless and consistent across replicas. For richer fields "
            "(tags, key_paths, parent_feature, body) call `get_feature(slug)`."
        )
    )
    def list_features() -> list[dict]:
        if vectors is None:
            return _list_entries_from_storage(storage)
        out: list[dict] = []
        for slug, metadata in vectors.list_all(return_metadata=True):
            md = metadata or {}
            out.append(
                {
                    "slug": slug,
                    "name": md.get("name", slug),
                    "summary": md.get("summary", ""),
                }
            )
        return out

    # --- search_features ---------------------------------------------------

    @mcp.tool(
        description=(
            "Semantic search over feature names+summaries. Returns up to k hits as "
            "{slug, name, summary, score} ranked by cosine similarity. Use this "
            "instead of `list_features` when the agent has a topic / question / "
            "file context rather than a known slug. Score is in [-1, 1]; treat "
            "anything below ~0.3 as a weak match. When the vector backend is not "
            "configured (e.g. local/stdio mode) this returns an empty list - fall "
            "back to `list_features`."
        )
    )
    def search_features(
        query: Annotated[str, Field(description="Free-form search text", min_length=1)],
        k: Annotated[int, Field(description="Max hits to return", ge=1, le=50)] = 10,
    ) -> list[dict]:
        if vectors is None or not embedder.is_enabled():
            return []
        query = query.strip()
        if not query:
            return []
        query_vec = embedder.embed_one(query)
        hits = vectors.query(query_vec, k, return_metadata=True)
        out: list[dict] = []
        for slug, score, md in hits:
            md = md or {}
            out.append(
                {
                    "slug": slug,
                    "name": md.get("name", slug),
                    "summary": md.get("summary", ""),
                    "score": round(float(score), 4),
                }
            )
        return out

    # --- get_feature -------------------------------------------------------

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
        if storage.is_archived(slug):
            raise ValueError(
                f"feature {slug!r} is archived; restore it from features/_archived/ if needed"
            )
        try:
            content, _ = storage.get_md(slug)
        except StorageNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc
        feat = _markdown_to_feature(content)
        return GetFeatureResult(
            frontmatter=feat.frontmatter.model_dump(mode="json", exclude_none=True),
            body_markdown=serialize_body(feat.body),
        )

    # --- update_feature ----------------------------------------------------

    @mcp.tool(
        description=(
            "Append-and-merge update of a feature. Pass a FeaturePatch (typed delta - "
            "NOT a full rewrite). Server merges the patch into the existing file, "
            "overwrites `## Last Update` with the patch's `last_update` entry, dedupes "
            "lists, rewrites the .md file, and updates the in-memory index. Returns a "
            "unified diff and any size warnings. Author is derived server-side from "
            "the request identity when running behind an authenticated transport."
        )
    )
    def update_feature(
        slug: Annotated[str, Field(description="The feature's slug")],
        patch: FeaturePatch,
        ctx: Context | None = None,
    ) -> UpdateResult:
        if storage.is_archived(slug):
            raise ValueError(f"feature {slug!r} is archived")

        resolved_author = author_resolver(ctx, patch.last_update)
        effective_patch = patch
        if resolved_author != patch.last_update.author:
            effective_patch = patch.model_copy(
                update={
                    "last_update": patch.last_update.model_copy(
                        update={"author": resolved_author}
                    )
                }
            )

        captured: dict[str, object] = {}

        def _apply(current: str, _etag: str | None) -> str:
            if not current:
                raise ValueError(f"feature {slug!r} not found")
            existing = _markdown_to_feature(current)
            new_feat, diff, warnings = apply_patch(existing, effective_patch)
            captured["feature"] = new_feat
            captured["diff"] = diff
            captured["warnings"] = warnings
            return _feature_to_markdown(new_feat)

        try:
            write_with_retry(storage, slug, read_and_apply=_apply)
        except StorageNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc
        except StorageConflict as exc:
            raise ValueError(str(exc)) from exc

        new_feat = captured["feature"]  # type: ignore[assignment]
        _reindex_feature(new_feat, embedder=embedder, vectors=vectors)
        audit.append(
            storage,
            actor=resolved_author,
            action="update_feature",
            slug=slug,
            payload={"diff_size": len(captured.get("diff", ""))},  # type: ignore[arg-type]
        )

        return UpdateResult(
            ok=True,
            diff=captured["diff"],  # type: ignore[arg-type]
            warnings=captured["warnings"],  # type: ignore[arg-type]
        )

    # --- create_feature ----------------------------------------------------

    @mcp.tool(
        description=(
            "Create a brand-new feature. The agent should call this only when the user "
            "is starting work on something that doesn't yet have a feature file. The slug "
            "is auto-derived from the name (with collision handling). Returns the new slug."
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
        ctx: Context | None = None,
    ) -> CreateFeatureResult:
        slug = _derive_unique_slug_via_storage(name, storage)
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
        storage.put_md(slug, _feature_to_markdown(feat))
        _reindex_feature(feat, embedder=embedder, vectors=vectors)

        # Resolve actor from headers for audit; create has no patch to override.
        actor = author_resolver(
            ctx, UpdateEntry(date=today, author="unknown", change="create")
        )
        audit.append(
            storage,
            actor=actor,
            action="create_feature",
            slug=slug,
            payload={"name": name},
        )
        return CreateFeatureResult(slug=slug)

    # --- correct_feature ---------------------------------------------------

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
        ctx: Context | None = None,
    ) -> CorrectResult:
        if storage.is_archived(slug):
            raise ValueError(f"feature {slug!r} is archived")

        captured: dict[str, object] = {}

        def _apply(current: str, _etag: str | None) -> str:
            if not current:
                raise ValueError(f"feature {slug!r} not found")
            existing = _markdown_to_feature(current)
            try:
                new_feat, diff = apply_corrections(existing, corrections)
            except CorrectionTargetNotFound as exc:
                raise ValueError(str(exc)) from exc
            captured["feature"] = new_feat
            captured["diff"] = diff
            return _feature_to_markdown(new_feat)

        try:
            write_with_retry(storage, slug, read_and_apply=_apply)
        except StorageNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc
        except StorageConflict as exc:
            raise ValueError(str(exc)) from exc

        new_feat = captured["feature"]  # type: ignore[assignment]
        _reindex_feature(new_feat, embedder=embedder, vectors=vectors)

        actor = author_resolver(
            ctx,
            UpdateEntry(date=date.today(), author="correction", change="correction"),
        )
        audit.append(
            storage,
            actor=actor,
            action="correct_feature",
            slug=slug,
            payload={"ops": [type(c).__name__ for c in corrections]},
        )
        return CorrectResult(ok=True, diff=captured["diff"])  # type: ignore[arg-type]

    # --- archive_feature ---------------------------------------------------

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
        reason: Annotated[
            str,
            Field(description="Why this feature is being archived", min_length=1),
        ],
        ctx: Context | None = None,
    ) -> ArchiveResult:
        if storage.is_archived(slug):
            raise ValueError(f"feature {slug!r} is already archived")
        try:
            content, _ = storage.get_md(slug)
        except StorageNotFound as exc:
            raise ValueError(f"feature {slug!r} not found") from exc

        feat = _markdown_to_feature(content)
        actor = author_resolver(
            ctx, UpdateEntry(date=date.today(), author="archive", change=reason)
        )
        feat.body.last_update = UpdateEntry(
            date=date.today(),
            author=actor,
            change=f"Archived - {reason}",
        )
        storage.put_md(slug, _feature_to_markdown(feat))
        archived_path = storage.archive_md(slug)
        if vectors is not None:
            vectors.delete(slug)
        audit.append(
            storage,
            actor=actor,
            action="archive_feature",
            slug=slug,
            payload={"reason": reason},
        )
        return ArchiveResult(ok=True, archived_path=archived_path)

    return mcp


def _derive_unique_slug_via_storage(name: str, storage: Storage) -> str:
    """Storage-backed slug uniqueness check (mirrors store.derive_unique_slug)."""
    base = slugify(name)
    active = set(storage.list_slugs())
    candidate = base
    suffix = 2
    while candidate in active or storage.is_archived(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
        if suffix > 999:
            from .store import SlugCollision

            raise SlugCollision(f"could not derive unique slug for {name!r}")
    return candidate


# --- HTTP entrypoint --------------------------------------------------------


def _build_http_app(config: Config, mcp: FastMCP, storage: Storage):
    """Wire FastMCP into a Starlette ASGI app for streamable-http transport.

    Mirrors the pattern used by Connecteam DeepWiki's MCP server. We mount
    the FastMCP-provided ASGI app at root and add a `/healthz` for kosmos.
    The server is stateless in V3 - no warm-up, no caches to flush.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    async def healthz(_request):
        # Cheap liveness: one S3 LIST. Good enough to catch broken IAM /
        # missing bucket. We do NOT count vectors here - that would put a
        # ListVectors call on every health probe.
        try:
            count = len(storage.list_slugs())
            ok = True
        except Exception:  # pragma: no cover - defensive
            count = -1
            ok = False
        return JSONResponse(
            {"ok": ok, "features": count, "transport": "streamable-http"},
            status_code=200 if ok else 503,
        )

    async def ready(_request):
        return PlainTextResponse("ok")

    inner = mcp.streamable_http_app() if hasattr(mcp, "streamable_http_app") else mcp.sse_app()

    # FastMCP ASGI app declares its own lifespan (session manager startup/
    # shutdown). We MUST run it - skipping it makes streamable-http requests
    # hang. Starlette >=0.35 wires this through the `lifespan` kwarg.
    inner_lifespan = getattr(inner.router, "lifespan_context", None)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/ready", ready),
            Mount("/", app=inner),
        ],
        lifespan=inner_lifespan,
    )


def _build_storage(config: Config) -> Storage:
    if config.storage_backend == "s3":
        if not config.s3_bucket:
            raise ValueError("STORAGE_BACKEND=s3 requires S3_BUCKET")
        return S3Storage(
            bucket=config.s3_bucket,
            prefix=config.s3_prefix,
            region=config.s3_region,
            endpoint_url=config.s3_endpoint_url,
        )
    if config.features_dir is None:
        raise ValueError("STORAGE_BACKEND=local requires --features-dir or FEATURES_DIR")
    return LocalFSStorage(config.features_dir)


def _build_embedder_and_vectors(
    config: Config,
) -> tuple[Embedder, S3VectorsIndex | None]:
    """Construct the Bedrock Embedder + S3VectorsIndex pair from config.

    Both are coupled: search needs both, and there's no scenario where one
    is configured without the other. If the vector bucket is not set, we
    disable the embedder too - the server still serves list/get/create/etc.,
    `search_features` just returns `[]`.

    Local/stdio path: both disabled. Tests rely on this.
    """
    if config.storage_backend != "s3" or not config.s3_vector_bucket:
        return Embedder(region=config.s3_region, enabled=False), None

    embedder = Embedder(
        region=config.effective_bedrock_region,
        model_id=config.bedrock_model_id,
        dim=config.embedding_dim,
        enabled=True,
    )
    vectors = S3VectorsIndex(
        vector_bucket=config.s3_vector_bucket,
        index_name=config.s3_vector_index_name,
        region=config.effective_vector_region,
        dim=config.embedding_dim,
    )
    return embedder, vectors


def _load_dotenv_if_present() -> None:
    """Load a local `.env` file if present, without overriding live env vars.

    Production (kosmos) sets env vars at the pod level; `.env` is a local-dev
    convenience only. Calling this before `Config.from_env()` is what lets
    `feature-memory-mcp` and `feature-memory-migrate` pick up the file
    automatically. Silently no-ops if `python-dotenv` is not installed.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional in stripped builds
        return
    load_dotenv(override=False)


def main() -> None:
    _load_dotenv_if_present()
    parser = argparse.ArgumentParser(prog="feature-memory-mcp")
    parser.add_argument(
        "--features-dir",
        type=Path,
        default=None,
        help="Path to the features/ directory (local backend only).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=None,
        help="Transport. Defaults to stdio for local, streamable-http for s3.",
    )
    parser.add_argument(
        "--storage-backend",
        choices=["local", "s3"],
        default=None,
        help="Override STORAGE_BACKEND env. 'local' or 's3'.",
    )
    parser.add_argument("--s3-bucket", default=None)
    parser.add_argument("--mcp-port", type=int, default=None)
    parser.add_argument("--mcp-host", default=None)
    parser.add_argument("--auth-header", default=None)
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)

    config = Config.from_env()
    overrides = {
        k: v
        for k, v in {
            "features_dir": args.features_dir,
            "transport": args.transport,
            "storage_backend": args.storage_backend,
            "s3_bucket": args.s3_bucket,
            "mcp_port": args.mcp_port,
            "mcp_host": args.mcp_host,
            "auth_header": args.auth_header,
        }.items()
        if v is not None
    }
    if overrides:
        config = config.model_copy(update=overrides)

    # Sensible defaults: s3 implies http, local implies stdio.
    if args.transport is None and "transport" not in overrides:
        if config.storage_backend == "s3":
            config = config.model_copy(update={"transport": "streamable-http"})

    storage = _build_storage(config)
    embedder, vectors = _build_embedder_and_vectors(config)

    author_resolver = (
        make_header_author_resolver(config.auth_header)
        if config.auth_header
        else _identity_author
    )

    server = build_server(
        storage=storage,
        embedder=embedder,
        vectors=vectors,
        author_resolver=author_resolver,
    )

    if config.transport == "streamable-http":
        import uvicorn

        app = _build_http_app(config, server, storage)
        logger.info(
            "starting HTTP transport on %s:%d (storage=%s, embeddings=%s, vectors=%s)",
            config.mcp_host,
            config.mcp_port,
            config.storage_backend,
            f"bedrock/{config.bedrock_model_id}" if embedder.is_enabled() else "off",
            f"s3vectors/{config.s3_vector_bucket}/{config.s3_vector_index_name}" if vectors else "off",
        )
        uvicorn.run(app, host=config.mcp_host, port=config.mcp_port, log_level=args.log_level.lower())
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
