"""One-shot migration script: local `features/` directory -> S3 bucket.

Reads every active feature .md from the local features/ dir, copies it as-is
into `s3://{bucket}/{prefix}/features/{slug}.md`, then rebuilds:

- `caches/index.json` from the frontmatter
- `caches/embeddings.jsonl` from the summaries (only if OPENAI_API_KEY is set)

Idempotent: skip if the destination object exists AND its ETag matches the
local md5 (i.e. the file is byte-identical). S3 ETags for single-part uploads
ARE the md5 of the content - we exploit that to avoid re-uploading unchanged
files. Re-runs after edits will write the changed slugs and leave the rest
untouched.

Usage:

    feature-memory-migrate \\
        --features-dir features/ \\
        --bucket my-bucket \\
        --prefix prod \\
        --region us-east-1 \\
        [--dry-run]

Env: OPENAI_API_KEY (optional; without it, embeddings cache is skipped).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path

from ..index import EMBEDDINGS_FILENAME, INDEX_FILENAME
from ..models import IndexEntry
from ..search import Embedder, FAISSIndex, embed_text_for_entry
from ..server import _markdown_to_feature, _index_entry
from ..storage import S3Storage, StorageNotFound
from ..store import list_slugs


logger = logging.getLogger(__name__)


def _file_md5(path: Path) -> str:
    """md5 of file bytes - matches S3's ETag for single-part uploads (<5GB)."""
    return hashlib.md5(path.read_bytes()).hexdigest()


def _ensure_bucket(storage: "S3Storage", region: str) -> None:
    """Idempotent CreateBucket - swallows BucketAlreadyOwnedByYou.

    For LocalStack and one-off dev buckets only. Production buckets should
    be provisioned by DevOps with the right tags, versioning, encryption,
    etc. - this helper deliberately uses only the bare-minimum CreateBucket
    call.
    """
    from botocore.exceptions import ClientError  # type: ignore[import-not-found]

    client = storage._client  # type: ignore[attr-defined]
    bucket = storage._bucket  # type: ignore[attr-defined]
    kwargs: dict = {"Bucket": bucket}
    # us-east-1 is the only region where LocationConstraint must NOT be set.
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**kwargs)
        logger.info("created bucket %s in %s", bucket, region)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            logger.info("bucket %s already exists - reusing", bucket)
            return
        raise


def migrate(
    *,
    features_dir: Path,
    bucket: str,
    prefix: str = "",
    region: str = "us-east-1",
    dry_run: bool = False,
    embedder: Embedder | None = None,
    endpoint_url: str | None = None,
    create_bucket: bool = False,
) -> dict[str, int]:
    """Run the migration. Returns counts of {uploaded, skipped, errors}.

    `endpoint_url` lets you target LocalStack or another S3-compatible
    endpoint instead of real AWS. `create_bucket=True` does an idempotent
    pre-flight `CreateBucket` - useful for LocalStack where the bucket
    doesn't pre-exist.
    """
    if not features_dir.exists():
        raise FileNotFoundError(f"features_dir does not exist: {features_dir}")

    storage = S3Storage(
        bucket=bucket, prefix=prefix, region=region, endpoint_url=endpoint_url
    )

    if create_bucket:
        _ensure_bucket(storage, region)
    embedder = embedder or Embedder(api_key=os.environ.get("OPENAI_API_KEY"))

    counts = {"uploaded": 0, "skipped": 0, "errors": 0}
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

        # Idempotency check: compare local md5 against remote ETag. S3 ETags
        # for single-part PUTs (all of ours - features are <5MB) ARE the md5
        # of the body, so this is a cheap byte-identical check.
        try:
            _, remote_meta = storage.get_md(slug)
            if remote_meta.etag and remote_meta.etag == _file_md5(local_path):
                logger.info("skip %s (ETag match)", slug)
                counts["skipped"] += 1
                continue
        except StorageNotFound:
            pass

        if dry_run:
            logger.info("DRY RUN: would upload features/%s.md", slug)
        else:
            storage.put_md(slug, content)
            logger.info("uploaded features/%s.md", slug)
        counts["uploaded"] += 1

    # Rebuild caches if we did real work or if they're missing.
    if not dry_run and entries:
        import json

        payload = [e.model_dump(mode="json", exclude_none=True) for e in entries]
        storage.put_cache(
            INDEX_FILENAME, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )
        logger.info("wrote caches/%s", INDEX_FILENAME)

        if embedder.is_enabled():
            faiss = FAISSIndex(dim=embedder.dim)
            vectors = embedder.embed([embed_text_for_entry(e) for e in entries])
            faiss.add_many(zip([e.slug for e in entries], vectors))
            storage.put_cache(EMBEDDINGS_FILENAME, faiss.dump_jsonl())
            logger.info("wrote caches/%s (%d vectors)", EMBEDDINGS_FILENAME, len(entries))
        else:
            logger.warning(
                "OPENAI_API_KEY not set; skipping embeddings cache - "
                "the server will re-embed on first startup"
            )

    return counts


def main() -> int:
    # Load a local `.env` (gitignored, dev-only) so that AWS creds and the
    # optional OPENAI_API_KEY can live there instead of being re-exported
    # on every shell. Production migrations from a CI/kosmos job set env
    # vars directly and skip this file.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(prog="feature-memory-migrate")
    parser.add_argument("--features-dir", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("S3_ENDPOINT_URL"),
        help=(
            "S3-compatible endpoint (e.g. http://localhost:4566 for LocalStack). "
            "Defaults to S3_ENDPOINT_URL env var or real AWS."
        ),
    )
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help=(
            "Pre-flight idempotent CreateBucket. Useful for LocalStack; "
            "DO NOT use against real AWS - prod buckets are DevOps-provisioned."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    counts = migrate(
        features_dir=args.features_dir.resolve(),
        bucket=args.bucket,
        prefix=args.prefix,
        region=args.region,
        dry_run=args.dry_run,
        endpoint_url=args.endpoint_url,
        create_bucket=args.create_bucket,
    )
    logger.info("migration complete: %s", counts)
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
