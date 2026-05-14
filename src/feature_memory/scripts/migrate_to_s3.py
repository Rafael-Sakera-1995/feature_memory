"""One-shot migration script: local `features/` directory -> AWS (V3 layout).

What it does:

1. Copies every active feature `.md` from the local features/ dir to
   `s3://{bucket}/{prefix}/features/{slug}.md`. Idempotent: skip if the
   destination object exists and its ETag matches the local md5 (S3 ETags
   for single-part PUTs ARE md5 of body).
2. Embeds each feature's `summary + name + tags` via AWS Bedrock Titan v2
   and upserts the resulting vector into S3 Vectors at
   `{vector_bucket}/{index_name}` keyed by slug. Vector metadata is
   `{name, summary}` so that `list_features` and `search_features` can
   return slim preview entries without an extra round-trip.

V3 has no `caches/index.json` - the server reads slugs straight from
S3 Vectors via ListVectors.

Run once per environment (dev, staging, prod). Re-runs are safe and cheap
- byte-identical files are skipped on the S3 side, and S3 Vectors `PutVectors`
is upsert semantics so re-embedding just overwrites.

Usage:

    feature-memory-migrate \\
        --features-dir features/ \\
        --bucket prod.connecteam.feature-memory \\
        --vector-bucket prod.connecteam.feature-memory-vectors \\
        --region eu-central-1 \\
        [--bedrock-region us-east-1] \\
        [--create-vector-bucket --create-vector-index]  # dev only
        [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path

from ..models import IndexEntry
from ..search import Embedder, S3VectorsIndex, embed_text_for_entry
from ..server import _markdown_to_feature, _index_entry
from ..storage import S3Storage, StorageNotFound
from ..store import list_slugs


logger = logging.getLogger(__name__)


def _file_md5(path: Path) -> str:
    """md5 of file bytes - matches S3's ETag for single-part uploads (<5GB)."""
    return hashlib.md5(path.read_bytes()).hexdigest()


def migrate(
    *,
    features_dir: Path,
    bucket: str,
    vector_bucket: str | None,
    prefix: str = "",
    region: str = "us-east-1",
    bedrock_region: str | None = None,
    bedrock_model_id: str = "amazon.titan-embed-text-v2:0",
    embedding_dim: int = 1024,
    vector_index_name: str = "features",
    dry_run: bool = False,
    embedder: Embedder | None = None,
    vectors: S3VectorsIndex | None = None,
    create_vector_bucket: bool = False,
    create_vector_index: bool = False,
) -> dict[str, int]:
    """Run the migration. Returns counts of {uploaded, skipped, errors, vectors}."""
    if not features_dir.exists():
        raise FileNotFoundError(f"features_dir does not exist: {features_dir}")

    storage = S3Storage(bucket=bucket, prefix=prefix, region=region)

    if vector_bucket and vectors is None:
        vectors = S3VectorsIndex(
            vector_bucket=vector_bucket,
            index_name=vector_index_name,
            region=region,
            dim=embedding_dim,
        )

    if vectors is not None:
        if create_vector_bucket and not dry_run:
            vectors.ensure_bucket()
        if create_vector_index and not dry_run:
            vectors.ensure_index(distance_metric="cosine")

    if embedder is None:
        embedder = Embedder(
            region=bedrock_region or region,
            model_id=bedrock_model_id,
            dim=embedding_dim,
            enabled=vectors is not None,
        )

    counts = {"uploaded": 0, "skipped": 0, "errors": 0, "vectors": 0}
    entries: list[IndexEntry] = []

    for slug in list_slugs(features_dir):
        local_path = features_dir / f"{slug}.md"
        try:
            content = local_path.read_text(encoding="utf-8")
            feature = _markdown_to_feature(content)
            entries.append(_index_entry(feature))
        except Exception:
            logger.exception("failed to parse %s; skipping", local_path)
            counts["errors"] += 1
            continue

        # Idempotency check: skip the markdown PUT if the body is byte-identical.
        already_synced = False
        try:
            _, remote_meta = storage.get_md(slug)
            if remote_meta.etag and remote_meta.etag == _file_md5(local_path):
                logger.info("skip %s (ETag match)", slug)
                counts["skipped"] += 1
                already_synced = True
        except StorageNotFound:
            pass

        if not already_synced:
            if dry_run:
                logger.info("DRY RUN: would upload features/%s.md", slug)
            else:
                storage.put_md(slug, content)
                logger.info("uploaded features/%s.md", slug)
            counts["uploaded"] += 1

    # Embed + upsert vectors. Bedrock invokes are single-text; PutVectors is
    # batched. Re-embedding everything is cheap (under a cent for ~50 features).
    # Metadata is the slim {name, summary} blob that the server returns from
    # list_features / search_features (Option A - everything else lives in .md).
    if vectors is not None and embedder.is_enabled() and entries and not dry_run:
        logger.info(
            "embedding %d features via bedrock %s -> s3vectors %s/%s",
            len(entries),
            embedder._model_id,  # noqa: SLF001 - logging only
            vectors.vector_bucket,
            vectors.index_name,
        )
        texts = [embed_text_for_entry(e) for e in entries]
        embeds = embedder.embed(texts)
        items: list[tuple[str, list[float], dict | None]] = []
        for entry, vec in zip(entries, embeds):
            metadata: dict = {"name": entry.name, "summary": entry.summary}
            items.append((entry.slug, vec, metadata))
        vectors.upsert_many(items)
        counts["vectors"] = len(items)
        logger.info("upserted %d vectors", len(items))
    elif vectors is None:
        logger.warning(
            "no --vector-bucket given; skipping vector index. The server will "
            "not be able to serve `search_features` until you migrate vectors."
        )
    elif not embedder.is_enabled():
        logger.warning("embedder disabled; skipping vector index")

    return counts


def main() -> int:
    # Load a local `.env` (gitignored, dev-only) so that AWS config can live
    # there instead of being re-exported on every shell. Production migrations
    # set env vars directly and skip this file.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(prog="feature-memory-migrate")
    parser.add_argument("--features-dir", type=Path, required=True)
    parser.add_argument(
        "--bucket",
        required=True,
        help="Markdown S3 bucket (regular S3, not vector).",
    )
    parser.add_argument(
        "--vector-bucket",
        default=os.environ.get("S3_VECTOR_BUCKET"),
        help="S3 Vectors bucket. Required for the vector upsert phase.",
    )
    parser.add_argument(
        "--vector-index-name",
        default=os.environ.get("S3_VECTOR_INDEX_NAME", "features"),
    )
    parser.add_argument("--prefix", default="")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--bedrock-region",
        default=os.environ.get("BEDROCK_REGION"),
        help="Region for Bedrock invoke. Defaults to --region.",
    )
    parser.add_argument(
        "--bedrock-model-id",
        default=os.environ.get("BEDROCK_MODEL_ID", "amazon.titan-embed-text-v2:0"),
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=int(os.environ.get("EMBEDDING_DIM", "1024")),
    )
    parser.add_argument(
        "--create-vector-bucket",
        action="store_true",
        help=(
            "Idempotent CreateVectorBucket pre-flight. DEV ONLY - prod vector "
            "buckets are DevOps-provisioned with tagging + encryption."
        ),
    )
    parser.add_argument(
        "--create-vector-index",
        action="store_true",
        help=(
            "Idempotent CreateIndex pre-flight. DEV ONLY - prod indexes are "
            "DevOps-provisioned."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    counts = migrate(
        features_dir=args.features_dir.resolve(),
        bucket=args.bucket,
        vector_bucket=args.vector_bucket,
        prefix=args.prefix,
        region=args.region,
        bedrock_region=args.bedrock_region,
        bedrock_model_id=args.bedrock_model_id,
        embedding_dim=args.embedding_dim,
        vector_index_name=args.vector_index_name,
        dry_run=args.dry_run,
        create_vector_bucket=args.create_vector_bucket,
        create_vector_index=args.create_vector_index,
    )
    logger.info("migration complete: %s", counts)
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
