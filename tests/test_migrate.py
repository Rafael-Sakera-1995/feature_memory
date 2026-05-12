"""Migration script tests against moto's mock S3."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from feature_memory.models import Feature, FeatureBody, Frontmatter
from feature_memory.scripts.migrate_to_s3 import migrate
from feature_memory.search import Embedder
from feature_memory.storage import S3Storage
from feature_memory.store import write_feature


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _seed_features(features_dir: Path) -> None:
    features_dir.mkdir(parents=True, exist_ok=True)
    (features_dir / "_archived").mkdir(exist_ok=True)
    for slug in ("alpha", "beta", "gamma"):
        write_feature(
            Feature(
                frontmatter=Frontmatter(
                    name=slug.title(),
                    slug=slug,
                    summary=f"summary for {slug}",
                    key_paths=[f"src/{slug}/**"],
                    tags=[slug],
                    created_at=date(2026, 4, 23),
                    updated_at=date(2026, 4, 23),
                ),
                body=FeatureBody(),
            ),
            features_dir,
        )


@pytest.fixture
def s3_setup():
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="feature-memory-test")
        yield client


def _make_storage(client, prefix: str = "prod") -> S3Storage:
    return S3Storage(bucket="feature-memory-test", prefix=prefix, region="us-east-1", client=client)


class TestMigrate:
    def test_uploads_all_features(self, tmp_path: Path, s3_setup, monkeypatch) -> None:
        features_dir = tmp_path / "features"
        _seed_features(features_dir)

        # Force boto3 to reuse the mocked client by patching the default factory.
        monkeypatch.setattr(
            "feature_memory.scripts.migrate_to_s3.S3Storage",
            lambda **kwargs: _make_storage(s3_setup, prefix=kwargs.get("prefix", "")),
        )

        counts = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            prefix="prod",
            embedder=Embedder(api_key=None),
        )
        assert counts["uploaded"] == 3
        assert counts["errors"] == 0

        storage = _make_storage(s3_setup, prefix="prod")
        assert storage.list_slugs() == ["alpha", "beta", "gamma"]

        # Index cache should be populated.
        idx = storage.get_cache("index.json")
        assert idx is not None
        assert "alpha" in idx and "beta" in idx and "gamma" in idx

    def test_idempotent_second_run_skips(
        self, tmp_path: Path, s3_setup, monkeypatch
    ) -> None:
        features_dir = tmp_path / "features"
        _seed_features(features_dir)

        monkeypatch.setattr(
            "feature_memory.scripts.migrate_to_s3.S3Storage",
            lambda **kwargs: _make_storage(s3_setup, prefix=kwargs.get("prefix", "")),
        )

        first = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            embedder=Embedder(api_key=None),
        )
        assert first["uploaded"] == 3

        # Second run should be a true no-op: the migration compares local md5
        # against the remote ETag (which IS md5 for single-part PUTs - both
        # moto and real S3 behave this way), so all three slugs should skip.
        second = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            embedder=Embedder(api_key=None),
        )
        assert second == {"uploaded": 0, "skipped": 3, "errors": 0}
