"""One-shot migration script: local `features/` directory -> S3 bucket.

Reads every active feature .md from the local features/ dir, copies it as-is
into `s3://{bucket}/{prefix}/features/{slug}.md`, then rebuilds:

- `caches/index.json` from the frontmatter
- `caches/embeddings.jsonl` from the summaries (only if OPENAI_API_KEY is set)

Idempotent: skip if the destination object exists AND its ETag matches the
local sha256 (i.e. the file is byte-identical). Re-runs after edits will
write the changed slugs and leave the rest untouched.

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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate(
    *,
    features_dir: Path,
    bucket: str,
    prefix: str = "",
    region: str = "us-east-1",
    dry_run: bool = False,
    embedder: Embedder | None = None,
) -> dict[str, int]:
    """Run the migration. Returns counts of {uploaded, skipped, errors}."""
    if not features_dir.exists():
        raise FileNotFoundError(f"features_dir does not exist: {features_dir}")

    storage = S3Storage(bucket=bucket, prefix=prefix, region=region)
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

        # Idempotency check: compare local sha256 against remote ETag for
        # single-part uploads (which all of ours are at <5MB).
        try:
            _, remote_meta = storage.get_md(slug)
            local_hash = _file_sha256(local_path)
            if remote_meta.etag == local_hash:
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
    )
    logger.info("migration complete: %s", counts)
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
