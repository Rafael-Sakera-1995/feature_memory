"""Storage backend tests.

`LocalFSStorage` is exercised end-to-end against a tmp dir. `S3Storage` is
exercised against `moto`'s mock S3 to verify the ETag conditional-write
contract holds. We do NOT hit real AWS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feature_memory.storage import (
    LocalFSStorage,
    S3Storage,
    StorageConflict,
    StorageNotFound,
    write_with_retry,
)


# --- LocalFSStorage ---------------------------------------------------------


@pytest.fixture
def local_storage(tmp_path: Path) -> LocalFSStorage:
    return LocalFSStorage(tmp_path / "features")


class TestLocalFSStorageBasics:
    def test_put_and_get_md(self, local_storage: LocalFSStorage) -> None:
        meta = local_storage.put_md("alpha", "hello world")
        assert meta.etag is not None
        content, read_meta = local_storage.get_md("alpha")
        assert content == "hello world"
        assert read_meta.etag == meta.etag

    def test_get_missing_raises(self, local_storage: LocalFSStorage) -> None:
        with pytest.raises(StorageNotFound):
            local_storage.get_md("nope")

    def test_list_slugs_sorted(self, local_storage: LocalFSStorage) -> None:
        local_storage.put_md("zeta", "z")
        local_storage.put_md("alpha", "a")
        assert local_storage.list_slugs() == ["alpha", "zeta"]


class TestLocalFSStorageConditional:
    def test_if_match_accepts_correct_etag(self, local_storage: LocalFSStorage) -> None:
        meta1 = local_storage.put_md("alpha", "v1")
        meta2 = local_storage.put_md("alpha", "v2", if_match=meta1.etag)
        assert meta2.etag != meta1.etag
        content, _ = local_storage.get_md("alpha")
        assert content == "v2"

    def test_if_match_rejects_stale_etag(self, local_storage: LocalFSStorage) -> None:
        local_storage.put_md("alpha", "v1")
        with pytest.raises(StorageConflict):
            local_storage.put_md("alpha", "v2", if_match="deadbeef")

    def test_if_match_on_missing_raises_conflict(
        self, local_storage: LocalFSStorage
    ) -> None:
        with pytest.raises(StorageConflict):
            local_storage.put_md("ghost", "v1", if_match="anything")


class TestLocalFSStorageArchive:
    def test_archive_moves_file(self, local_storage: LocalFSStorage) -> None:
        local_storage.put_md("alpha", "x")
        path = local_storage.archive_md("alpha")
        assert "_archived" in path
        assert "alpha" in path
        assert local_storage.is_archived("alpha")
        with pytest.raises(StorageNotFound):
            local_storage.get_md("alpha")

    def test_archive_missing_raises(self, local_storage: LocalFSStorage) -> None:
        with pytest.raises(StorageNotFound):
            local_storage.archive_md("nope")

    def test_archive_collision_raises_conflict(
        self, local_storage: LocalFSStorage
    ) -> None:
        local_storage.put_md("alpha", "x")
        local_storage.archive_md("alpha")
        local_storage.put_md("alpha", "y")
        with pytest.raises(StorageConflict):
            local_storage.archive_md("alpha")


class TestLocalFSStorageCache:
    def test_cache_round_trip(self, local_storage: LocalFSStorage) -> None:
        assert local_storage.get_cache("foo.json") is None
        local_storage.put_cache("foo.json", '{"k": 1}')
        assert local_storage.get_cache("foo.json") == '{"k": 1}'

    def test_cache_overwrite(self, local_storage: LocalFSStorage) -> None:
        local_storage.put_cache("foo.json", "v1")
        local_storage.put_cache("foo.json", "v2")
        assert local_storage.get_cache("foo.json") == "v2"


class TestLocalFSStorageAudit:
    def test_audit_event_persisted(self, local_storage: LocalFSStorage) -> None:
        key = local_storage.append_audit({"action": "test", "slug": "x"})
        assert key.endswith(".json")
        # Find the file under the audit dir and verify contents.
        audit_files = list(Path(local_storage.features_dir / ".audit").rglob("*.json"))
        assert len(audit_files) == 1
        loaded = json.loads(audit_files[0].read_text())
        assert loaded["action"] == "test"


class TestWriteWithRetry:
    def test_succeeds_first_try(self, local_storage: LocalFSStorage) -> None:
        local_storage.put_md("alpha", "v1")
        meta = write_with_retry(
            local_storage,
            "alpha",
            read_and_apply=lambda current, _etag: current + " + patch",
        )
        assert meta.etag is not None
        content, _ = local_storage.get_md("alpha")
        assert content == "v1 + patch"

    def test_retries_on_conflict(self, local_storage: LocalFSStorage) -> None:
        local_storage.put_md("alpha", "v1")

        calls = {"n": 0}

        def apply(current: str, _etag: str | None) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                # Race: someone else writes between our read and our put.
                local_storage.put_md("alpha", "external write")
            return current + " + ours"

        meta = write_with_retry(local_storage, "alpha", read_and_apply=apply)
        assert meta.etag is not None
        assert calls["n"] == 2  # one conflict + one success
        content, _ = local_storage.get_md("alpha")
        # On retry we re-read so we see "external write" as current.
        assert content == "external write + ours"


# --- S3Storage (via moto) ---------------------------------------------------


@pytest.fixture
def s3_storage():
    """S3Storage backed by moto's in-memory S3.

    moto's mock_aws context handles AWS calls without network. Bucket is
    created fresh per test so each test sees an empty store.
    """
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="feature-memory-test")
        storage = S3Storage(
            bucket="feature-memory-test",
            prefix="prod",
            region="us-east-1",
            client=client,
        )
        yield storage


class TestS3StorageBasics:
    def test_put_and_get_md(self, s3_storage: S3Storage) -> None:
        meta = s3_storage.put_md("alpha", "hello s3")
        assert meta.etag is not None
        content, read_meta = s3_storage.get_md("alpha")
        assert content == "hello s3"
        assert read_meta.etag == meta.etag

    def test_get_missing_raises(self, s3_storage: S3Storage) -> None:
        with pytest.raises(StorageNotFound):
            s3_storage.get_md("nope")

    def test_list_slugs_excludes_archived(self, s3_storage: S3Storage) -> None:
        s3_storage.put_md("zeta", "z")
        s3_storage.put_md("alpha", "a")
        s3_storage.archive_md("alpha")
        slugs = s3_storage.list_slugs()
        assert slugs == ["zeta"]


class TestS3StorageArchive:
    def test_archive_round_trip(self, s3_storage: S3Storage) -> None:
        s3_storage.put_md("alpha", "x")
        archived_key = s3_storage.archive_md("alpha")
        assert "_archived" in archived_key
        assert s3_storage.is_archived("alpha")
        with pytest.raises(StorageNotFound):
            s3_storage.get_md("alpha")

    def test_archive_missing_raises(self, s3_storage: S3Storage) -> None:
        with pytest.raises(StorageNotFound):
            s3_storage.archive_md("nope")


class TestS3StorageCacheAndAudit:
    def test_cache_round_trip(self, s3_storage: S3Storage) -> None:
        assert s3_storage.get_cache("foo.json") is None
        s3_storage.put_cache("foo.json", '{"k": 1}')
        assert s3_storage.get_cache("foo.json") == '{"k": 1}'

    def test_audit_blob_persisted(self, s3_storage: S3Storage) -> None:
        key = s3_storage.append_audit({"action": "test"})
        assert key.endswith(".json")
        assert "audit/" in key
