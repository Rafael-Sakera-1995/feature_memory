"""Storage abstraction for the feature memory.

Two backends:

- `LocalFSStorage`: writes markdown + caches + audit blobs to a directory on
  disk. Used in dev, tests, and as a fallback if DevOps prefers a kosmos-
  mounted persistent volume over S3.
- `S3Storage`: writes the same logical objects to an S3 bucket with optimistic
  concurrency via `If-Match` ETags on `PutObject`.

Callers never branch on backend: they go through the `Storage` protocol.

Layout (identical for both backends, just rooted differently):

    features/{slug}.md
    features/_archived/{slug}.md
    caches/index.json
    caches/embeddings.jsonl
    audit/YYYY-MM-DD/{ts}-{uuid}.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import BlobMetadata


logger = logging.getLogger(__name__)


ARCHIVED_DIR_NAME = "_archived"


class StorageNotFound(Exception):
    """The requested object does not exist (analogous to S3 404)."""


class StorageConflict(Exception):
    """Conditional write failed: the ETag we sent did not match the live object.

    Caller should re-read, re-apply patch, and retry. Mirrors S3's
    412 PreconditionFailed.
    """


@runtime_checkable
class Storage(Protocol):
    """Backend-agnostic storage contract used by the server.

    Implementations must be thread-safe for concurrent reads. Writes happen
    in the server's request handlers; we rely on optimistic ETag matching
    rather than locking, so concurrent writes from different replicas (V2.1)
    or different requests do not need internal mutexes.
    """

    # --- markdown source of truth ---
    def get_md(self, slug: str) -> tuple[str, BlobMetadata]:
        """Return the raw markdown contents and current metadata for `slug`.

        Raises StorageNotFound if the slug is not present.
        """
        ...

    def put_md(
        self,
        slug: str,
        content: str,
        *,
        if_match: str | None = None,
    ) -> BlobMetadata:
        """Write `content` for `slug`.

        If `if_match` is not None, the write is conditional on the live
        ETag matching; otherwise it is unconditional. Raises StorageConflict
        on ETag mismatch.
        """
        ...

    def list_slugs(self) -> list[str]:
        """Active feature slugs (excludes archived)."""
        ...

    def archive_md(self, slug: str) -> str:
        """Move `features/{slug}.md` to `features/_archived/{slug}.md`.

        Returns the logical path of the archived object (informational; used
        for the `ArchiveResult.archived_path` field).
        """
        ...

    def is_archived(self, slug: str) -> bool:
        """True if `slug` exists under the archived prefix."""
        ...

    # --- derived caches ---
    def get_cache(self, name: str) -> str | None:
        """Read a cache file (`name` is e.g. 'index.json'). None on miss."""
        ...

    def put_cache(self, name: str, content: str) -> None:
        """Write a cache file. Unconditional - caches are derived, not authoritative."""
        ...

    # --- audit trail ---
    def append_audit(self, payload: dict) -> str:
        """Persist an audit event. Returns the storage key (for tests/logs)."""
        ...


# --- Local filesystem implementation ----------------------------------------


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class LocalFSStorage:
    """Filesystem-backed Storage.

    Synthesizes an ETag as sha256 of file contents at read time. This means
    the optimistic-locking contract works identically to S3 in tests: callers
    pass the ETag they read; if the on-disk content has changed in between,
    its current hash will differ and the conditional `put_md` raises
    StorageConflict.
    """

    def __init__(
        self,
        features_dir: Path,
        *,
        caches_dir: Path | None = None,
        audit_dir: Path | None = None,
    ) -> None:
        """`features_dir` is the directory containing `{slug}.md` files.

        Sub-directories default to:
        - archived = features_dir/_archived
        - caches   = features_dir            (e.g. features_dir/index.json)
        - audit    = features_dir/.audit

        Caches share the features_dir by default so the V1 on-disk layout
        (a single `features/` with `{slug}.md` files + `index.json`) is
        preserved. Audit blobs live under a hidden `.audit/` to avoid
        polluting the directory listing - they are large and forensic-only.
        """
        self._features_dir = Path(features_dir).resolve()
        self._archived_dir = self._features_dir / ARCHIVED_DIR_NAME
        self._caches_dir = (
            Path(caches_dir).resolve() if caches_dir else self._features_dir
        )
        self._audit_dir = (
            Path(audit_dir).resolve() if audit_dir else self._features_dir / ".audit"
        )
        for d in (self._features_dir, self._archived_dir, self._caches_dir, self._audit_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def features_dir(self) -> Path:
        """Exposed for legacy code paths that still take a `features_dir` arg.

        Migration target: callers should use the Storage methods directly.
        """
        return self._features_dir

    def _md_path(self, slug: str) -> Path:
        return self._features_dir / f"{slug}.md"

    def _archived_path(self, slug: str) -> Path:
        return self._archived_dir / f"{slug}.md"

    def get_md(self, slug: str) -> tuple[str, BlobMetadata]:
        path = self._md_path(slug)
        if not path.exists():
            raise StorageNotFound(f"slug {slug!r} not found")
        content = path.read_text(encoding="utf-8")
        return content, BlobMetadata(etag=_sha256(content))

    def put_md(
        self,
        slug: str,
        content: str,
        *,
        if_match: str | None = None,
    ) -> BlobMetadata:
        path = self._md_path(slug)
        with self._lock:
            if if_match is not None:
                if not path.exists():
                    raise StorageConflict(
                        f"if_match supplied but slug {slug!r} does not exist"
                    )
                current = path.read_text(encoding="utf-8")
                if _sha256(current) != if_match:
                    raise StorageConflict(
                        f"ETag mismatch on slug {slug!r}: caller had {if_match[:8]}..."
                    )
            self._features_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            return BlobMetadata(etag=_sha256(content))

    def list_slugs(self) -> list[str]:
        if not self._features_dir.exists():
            return []
        return sorted(
            p.stem
            for p in self._features_dir.glob("*.md")
            if p.is_file() and not p.name.startswith(".")
        )

    def archive_md(self, slug: str) -> str:
        src = self._md_path(slug)
        if not src.exists():
            raise StorageNotFound(f"slug {slug!r} not found")
        self._archived_dir.mkdir(parents=True, exist_ok=True)
        dst = self._archived_path(slug)
        if dst.exists():
            raise StorageConflict(
                f"archive target for {slug!r} already exists at {dst}"
            )
        src.rename(dst)
        return str(dst)

    def is_archived(self, slug: str) -> bool:
        return self._archived_path(slug).exists()

    def get_cache(self, name: str) -> str | None:
        path = self._caches_dir / name
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def put_cache(self, name: str, content: str) -> None:
        self._caches_dir.mkdir(parents=True, exist_ok=True)
        path = self._caches_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def append_audit(self, payload: dict) -> str:
        now = datetime.now(timezone.utc)
        day_dir = self._audit_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        key = f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
        path = day_dir / key
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)


# --- S3 implementation ------------------------------------------------------


class S3Storage:
    """S3-backed Storage with optimistic concurrency on `put_md`.

    Requires a `boto3` client. ETag-conditional PUTs use the S3 conditional
    writes feature (`IfMatch` parameter on `put_object`, GA in 2024).
    Callers see `StorageConflict` when the precondition fails so they can
    re-read and retry.

    Bucket layout (all paths are prefixed with `s3_prefix` if set):

        features/{slug}.md
        features/_archived/{slug}.md
        caches/{name}
        audit/YYYY-MM-DD/{HHMMSS}-{uuid8}.json
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        client=None,
    ) -> None:
        """Construct an S3 storage backend.

        `endpoint_url` is for non-AWS S3-compatible servers (LocalStack,
        MinIO, real S3 from a non-default region with FIPS, etc.). When
        unset, boto3 uses the default AWS endpoint for the given region.
        """
        if not bucket:
            raise ValueError("S3Storage requires a non-empty bucket name")
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise RuntimeError(
                "boto3 is required for S3Storage; install with `pip install boto3`"
            ) from exc

        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._region = region
        if client is not None:
            self._client = client
        else:
            client_kwargs: dict = {"region_name": region}
            if endpoint_url:
                client_kwargs["endpoint_url"] = endpoint_url
            self._client = boto3.client("s3", **client_kwargs)
        self._lock = threading.Lock()

    def _key(self, *parts: str) -> str:
        clean = [p for p in parts if p]
        if self._prefix:
            return "/".join([self._prefix, *clean])
        return "/".join(clean)

    def _md_key(self, slug: str) -> str:
        return self._key("features", f"{slug}.md")

    def _archived_key(self, slug: str) -> str:
        return self._key("features", ARCHIVED_DIR_NAME, f"{slug}.md")

    def get_md(self, slug: str) -> tuple[str, BlobMetadata]:
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._md_key(slug))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise StorageNotFound(f"slug {slug!r} not found") from exc
            raise

        body = response["Body"].read().decode("utf-8")
        etag = response.get("ETag", "").strip('"') or None
        version_id = response.get("VersionId")
        return body, BlobMetadata(etag=etag, version_id=version_id)

    def put_md(
        self,
        slug: str,
        content: str,
        *,
        if_match: str | None = None,
    ) -> BlobMetadata:
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]

        kwargs: dict = dict(
            Bucket=self._bucket,
            Key=self._md_key(slug),
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        if if_match is not None:
            kwargs["IfMatch"] = if_match

        try:
            response = self._client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "PreconditionFailed" or status == 412:
                raise StorageConflict(
                    f"ETag mismatch on slug {slug!r}; another writer raced us"
                ) from exc
            raise

        etag = response.get("ETag", "").strip('"') or None
        version_id = response.get("VersionId")
        return BlobMetadata(etag=etag, version_id=version_id)

    def list_slugs(self) -> list[str]:
        prefix = self._key("features") + "/"
        archived_prefix = self._key("features", ARCHIVED_DIR_NAME) + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        slugs: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.startswith(archived_prefix):
                    continue
                if not key.endswith(".md"):
                    continue
                slugs.append(Path(key).stem)
        return sorted(slugs)

    def archive_md(self, slug: str) -> str:
        from botocore.exceptions import ClientError

        src_key = self._md_key(slug)
        dst_key = self._archived_key(slug)
        try:
            self._client.head_object(Bucket=self._bucket, Key=dst_key)
            raise StorageConflict(
                f"archive target for {slug!r} already exists at s3://{self._bucket}/{dst_key}"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise

        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=dst_key,
                CopySource={"Bucket": self._bucket, "Key": src_key},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise StorageNotFound(f"slug {slug!r} not found") from exc
            raise

        self._client.delete_object(Bucket=self._bucket, Key=src_key)
        return dst_key

    def is_archived(self, slug: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=self._archived_key(slug))
            return True
        except ClientError:
            return False

    def get_cache(self, name: str) -> str | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=self._key("caches", name)
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read().decode("utf-8")

    def put_cache(self, name: str, content: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key("caches", name),
            Body=content.encode("utf-8"),
            ContentType="application/json" if name.endswith(".json") else "text/plain",
        )

    def append_audit(self, payload: dict) -> str:
        now = datetime.now(timezone.utc)
        key = self._key(
            "audit",
            now.strftime("%Y-%m-%d"),
            f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.json",
        )
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return key


# --- Retry helper -----------------------------------------------------------


def write_with_retry(
    storage: Storage,
    slug: str,
    *,
    read_and_apply,
    max_retries: int = 3,
):
    """Optimistic-concurrency write loop.

    `read_and_apply` is a callable `(current_content, current_etag) -> new_content`
    that produces the new serialized markdown to write. On `StorageConflict`
    we re-read and re-apply (relying on the patch being conflict-free /
    additive, which is the V1 design guarantee).

    Returns the final BlobMetadata after a successful write.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            current, meta = storage.get_md(slug)
        except StorageNotFound:
            current, meta = "", BlobMetadata(etag=None)

        new_content = read_and_apply(current, meta.etag)

        try:
            return storage.put_md(slug, new_content, if_match=meta.etag)
        except StorageConflict as exc:
            last_exc = exc
            logger.warning(
                "write_with_retry: conflict on slug %s attempt %d/%d",
                slug,
                attempt + 1,
                max_retries,
            )
            time.sleep(0.05 * (attempt + 1))
            continue

    raise StorageConflict(
        f"write_with_retry exhausted {max_retries} attempts for slug {slug!r}"
    ) from last_exc
