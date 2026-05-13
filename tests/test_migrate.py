"""Migration script tests.

Uses moto's mock S3 for the markdown bucket and an in-process fake for the
S3 Vectors + Bedrock surfaces (moto coverage of both is brand-new and patchy).
The Embedder is also stubbed via a fake Bedrock client so the test runs with
no AWS credentials.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from feature_memory.models import Feature, FeatureBody, Frontmatter
from feature_memory.scripts.migrate_to_s3 import migrate
from feature_memory.search import Embedder, S3VectorsIndex
from feature_memory.storage import S3Storage
from feature_memory.store import write_feature

from .test_search import FakeBedrockClient, FakeS3VectorsClient


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


def _stub_embedder(dim: int = 1024) -> Embedder:
    return Embedder(region="us-east-1", dim=dim, client=FakeBedrockClient(dim=dim), enabled=True)


def _stub_vectors(dim: int = 1024) -> S3VectorsIndex:
    return S3VectorsIndex(
        vector_bucket="vb-test",
        index_name="features",
        region="us-east-1",
        dim=dim,
        client=FakeS3VectorsClient(),
    )


class TestMigrate:
    def test_uploads_all_features_and_vectors(self, tmp_path: Path, s3_setup, monkeypatch) -> None:
        features_dir = tmp_path / "features"
        _seed_features(features_dir)

        # Force the migration to reuse our moto S3 client.
        monkeypatch.setattr(
            "feature_memory.scripts.migrate_to_s3.S3Storage",
            lambda **kwargs: _make_storage(s3_setup, prefix=kwargs.get("prefix", "")),
        )
        vectors = _stub_vectors()
        embedder = _stub_embedder()

        counts = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            vector_bucket="vb-test",
            prefix="prod",
            embedder=embedder,
            vectors=vectors,
        )
        assert counts["uploaded"] == 3
        assert counts["errors"] == 0
        assert counts["vectors"] == 3

        storage = _make_storage(s3_setup, prefix="prod")
        assert storage.list_slugs() == ["alpha", "beta", "gamma"]
        # V3: no caches/index.json - vectors are the source of truth for the index
        assert storage.get_cache("index.json") is None

        # All three slugs landed in the vector backend with slim metadata.
        fake = vectors._client  # type: ignore[attr-defined]
        assert {key for (_, _, key) in fake.vectors.keys()} == {"alpha", "beta", "gamma"}
        for (_, _, key), stored in fake.vectors.items():
            md = stored["metadata"]
            assert set(md) == {"name", "summary"}, f"vector {key!r} metadata={md}"
            assert md["name"] == key.title()
            assert md["summary"] == f"summary for {key}"

    def test_idempotent_second_run_skips(self, tmp_path: Path, s3_setup, monkeypatch) -> None:
        features_dir = tmp_path / "features"
        _seed_features(features_dir)

        monkeypatch.setattr(
            "feature_memory.scripts.migrate_to_s3.S3Storage",
            lambda **kwargs: _make_storage(s3_setup, prefix=kwargs.get("prefix", "")),
        )
        vectors = _stub_vectors()
        embedder = _stub_embedder()

        first = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            vector_bucket="vb-test",
            embedder=embedder,
            vectors=vectors,
        )
        assert first["uploaded"] == 3

        # Second run: ETag matches local md5 -> all 3 markdown writes skipped.
        # Vectors are still re-upserted (cheap and idempotent on the S3 side).
        second = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            vector_bucket="vb-test",
            embedder=embedder,
            vectors=vectors,
        )
        assert second["uploaded"] == 0
        assert second["skipped"] == 3
        assert second["errors"] == 0
        assert second["vectors"] == 3

    def test_without_vector_bucket_skips_vectors(
        self, tmp_path: Path, s3_setup, monkeypatch
    ) -> None:
        features_dir = tmp_path / "features"
        _seed_features(features_dir)

        monkeypatch.setattr(
            "feature_memory.scripts.migrate_to_s3.S3Storage",
            lambda **kwargs: _make_storage(s3_setup, prefix=kwargs.get("prefix", "")),
        )

        # No vector_bucket -> markdown migration runs, vector phase is skipped.
        counts = migrate(
            features_dir=features_dir,
            bucket="feature-memory-test",
            vector_bucket=None,
        )
        assert counts["uploaded"] == 3
        assert counts["vectors"] == 0
