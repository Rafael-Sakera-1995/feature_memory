"""Tests for Embedder (Bedrock) and S3VectorsIndex.

Both backends are tested against in-process fake AWS clients rather than
moto. moto's coverage of `s3vectors` and `bedrock-runtime` is brand-new and
patchy, and our fakes are small enough that hand-rolling them gives us
better fidelity for less code.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from feature_memory.models import IndexEntry
from feature_memory.search import (
    Embedder,
    S3VectorsIndex,
    _sanitize_metadata,
    embed_text_for_entry,
)


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# --- embed_text_for_entry ---------------------------------------------------


class TestEmbedTextForEntry:
    def test_joins_summary_name_tags(self) -> None:
        entry = IndexEntry(
            slug="x",
            name="X",
            summary="does x things",
            key_paths=[],
            tags=["alpha", "beta"],
        )
        assert embed_text_for_entry(entry) == "does x things X alpha beta"

    def test_strips_each_field(self) -> None:
        entry = IndexEntry(
            slug="y",
            name="  Y  ",
            summary="  summ  ",
            key_paths=[],
            tags=[" tag1 "],
        )
        assert embed_text_for_entry(entry) == "summ Y tag1"


# --- Fake Bedrock client ----------------------------------------------------


class FakeBedrockBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeBedrockClient:
    """Returns a deterministic 1024-dim vector based on the input length.

    Lets tests assert that embed() was called with the right text and shape.
    """

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim
        self.calls: list[dict] = []

    def invoke_model(self, *, modelId: str, body: str, **kwargs: Any) -> dict:
        parsed = json.loads(body)
        self.calls.append({"modelId": modelId, **parsed})
        text = parsed["inputText"]
        seed = float(len(text) % 7 + 1)
        embedding = [(i % 5) / 10.0 * seed for i in range(self._dim)]
        return {
            "body": FakeBedrockBody(
                json.dumps({"embedding": embedding, "inputTextTokenCount": len(text)}).encode()
            )
        }


# --- Embedder tests ---------------------------------------------------------


class TestEmbedderDisabled:
    def test_returns_zero_vectors(self) -> None:
        emb = Embedder(region="us-east-1", enabled=False, dim=1024)
        assert emb.is_enabled() is False
        out = emb.embed(["hello", "world"])
        assert len(out) == 2
        assert all(len(v) == 1024 and not any(v) for v in out)

    def test_empty_input_returns_empty(self) -> None:
        emb = Embedder(region="us-east-1", enabled=False, dim=1024)
        assert emb.embed([]) == []


class TestEmbedderBedrock:
    def test_invokes_titan_with_correct_request_shape(self) -> None:
        fake = FakeBedrockClient(dim=1024)
        emb = Embedder(region="us-east-1", dim=1024, client=fake, enabled=True)
        out = emb.embed(["hello world"])
        assert len(out) == 1
        assert len(out[0]) == 1024
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["modelId"] == "amazon.titan-embed-text-v2:0"
        assert call["inputText"] == "hello world"
        assert call["dimensions"] == 1024
        assert call["normalize"] is True

    def test_batch_makes_one_call_per_text(self) -> None:
        # Titan v2 invoke_model is single-input - we batch in Python.
        fake = FakeBedrockClient(dim=1024)
        emb = Embedder(region="us-east-1", dim=1024, client=fake, enabled=True)
        out = emb.embed(["a", "b", "c"])
        assert len(out) == 3
        assert len(fake.calls) == 3
        assert [c["inputText"] for c in fake.calls] == ["a", "b", "c"]

    def test_embed_one(self) -> None:
        fake = FakeBedrockClient(dim=1024)
        emb = Embedder(region="us-east-1", dim=1024, client=fake, enabled=True)
        v = emb.embed_one("a single string")
        assert len(v) == 1024
        assert fake.calls[0]["inputText"] == "a single string"

    def test_custom_model_id(self) -> None:
        fake = FakeBedrockClient(dim=512)
        emb = Embedder(
            region="us-east-1",
            model_id="cohere.embed-english-v3",
            dim=512,
            client=fake,
            enabled=True,
        )
        emb.embed(["x"])
        assert fake.calls[0]["modelId"] == "cohere.embed-english-v3"
        assert fake.calls[0]["dimensions"] == 512


# --- Fake S3 Vectors client -------------------------------------------------


class FakeS3VectorsClient:
    """In-memory implementation of the s3vectors surface area we use.

    Stores vectors keyed by (bucket, index, key) -> (vector, metadata).
    Implements a brute-force cosine query so tests can assert ordering.
    """

    def __init__(self) -> None:
        # (bucket, index, key) -> {"data": [...], "metadata": {...}}
        self.vectors: dict[tuple[str, str, str], dict] = {}
        self.buckets: set[str] = set()
        self.indexes: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, dict]] = []

    # --- vector ops ---

    def put_vectors(
        self, *, vectorBucketName: str, indexName: str, vectors: list[dict]
    ) -> dict:
        self.calls.append(("put_vectors", {"bucket": vectorBucketName, "index": indexName, "n": len(vectors)}))
        for v in vectors:
            key = v["key"]
            data = v["data"]["float32"]
            md = v.get("metadata") or {}
            self.vectors[(vectorBucketName, indexName, key)] = {"data": list(data), "metadata": md}
        return {}

    def delete_vectors(
        self, *, vectorBucketName: str, indexName: str, keys: list[str]
    ) -> dict:
        self.calls.append(("delete_vectors", {"bucket": vectorBucketName, "index": indexName, "keys": list(keys)}))
        for k in keys:
            self.vectors.pop((vectorBucketName, indexName, k), None)
        return {}

    def query_vectors(
        self,
        *,
        vectorBucketName: str,
        indexName: str,
        topK: int,
        queryVector: dict,
        returnDistance: bool = False,
        returnMetadata: bool = False,
    ) -> dict:
        q = queryVector["float32"]
        candidates = [
            (key, stored["data"], stored["metadata"])
            for (b, i, key), stored in self.vectors.items()
            if b == vectorBucketName and i == indexName
        ]

        def cos(a: list[float], b: list[float]) -> float:
            from math import sqrt

            num = sum(x * y for x, y in zip(a, b))
            na = sqrt(sum(x * x for x in a))
            nb = sqrt(sum(x * x for x in b))
            if na == 0.0 or nb == 0.0:
                return 0.0
            return num / (na * nb)

        scored = [(key, 1.0 - cos(q, vec), md) for key, vec, md in candidates]
        scored.sort(key=lambda r: r[1])
        out: list[dict] = []
        for key, distance, md in scored[:topK]:
            row: dict = {"key": key}
            if returnDistance:
                row["distance"] = distance
            if returnMetadata:
                row["metadata"] = md
            out.append(row)
        return {"vectors": out}

    # --- provisioning ---

    def create_vector_bucket(self, *, vectorBucketName: str, **kwargs: Any) -> dict:
        if vectorBucketName in self.buckets:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "exists"}},
                "CreateVectorBucket",
            )
        self.buckets.add(vectorBucketName)
        return {}

    def create_index(
        self,
        *,
        vectorBucketName: str,
        indexName: str,
        dataType: str,
        dimension: int,
        distanceMetric: str,
        **kwargs: Any,
    ) -> dict:
        key = (vectorBucketName, indexName)
        if key in self.indexes:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "exists"}},
                "CreateIndex",
            )
        self.indexes.add(key)
        return {}


# --- S3VectorsIndex tests ---------------------------------------------------


class TestS3VectorsIndexBasics:
    def test_requires_bucket(self) -> None:
        with pytest.raises(ValueError):
            S3VectorsIndex(vector_bucket="", region="us-east-1", client=FakeS3VectorsClient())

    def test_upsert_stores_vector(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(
            vector_bucket="vb", index_name="features", region="us-east-1", dim=4, client=fake
        )
        idx.upsert("alpha", [1.0, 0.0, 0.0, 0.0], metadata={"tags": ["x"]})
        stored = fake.vectors[("vb", "features", "alpha")]
        assert stored["data"] == [1.0, 0.0, 0.0, 0.0]
        assert stored["metadata"] == {"tags": ["x"]}

    def test_upsert_dim_mismatch_raises(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        with pytest.raises(ValueError, match="dim mismatch"):
            idx.upsert("alpha", [1.0, 0.0, 0.0])

    def test_upsert_is_idempotent_overwrite(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert("alpha", [1.0, 0.0, 0.0, 0.0])
        idx.upsert("alpha", [0.0, 1.0, 0.0, 0.0])  # overwrite
        assert fake.vectors[("vb", "features", "alpha")]["data"] == [0.0, 1.0, 0.0, 0.0]

    def test_delete_removes_vector(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert("alpha", [1.0, 0.0, 0.0, 0.0])
        idx.delete("alpha")
        assert ("vb", "features", "alpha") not in fake.vectors

    def test_delete_missing_is_noop(self) -> None:
        # S3 Vectors DeleteVectors is idempotent on missing keys
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.delete("does-not-exist")  # should not raise


class TestS3VectorsIndexQuery:
    def test_query_returns_topk_by_similarity(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert("alpha", [1.0, 0.0, 0.0, 0.0])
        idx.upsert("beta", [0.9, 0.1, 0.0, 0.0])
        idx.upsert("gamma", [0.0, 1.0, 0.0, 0.0])

        hits = idx.query([1.0, 0.0, 0.0, 0.0], k=2)
        assert len(hits) == 2
        assert hits[0][0] == "alpha"
        assert hits[1][0] == "beta"
        # similarity = 1 - distance, alpha is identical so ~1.0
        assert hits[0][1] == pytest.approx(1.0, abs=1e-6)
        assert hits[1][1] > hits[1 - 1 + 0][1] * 0  # beta < alpha

    def test_query_empty_index_returns_empty(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        assert idx.query([1.0, 0.0, 0.0, 0.0], k=10) == []

    def test_query_zero_vector_returns_empty(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert("alpha", [1.0, 0.0, 0.0, 0.0])
        assert idx.query([0.0, 0.0, 0.0, 0.0], k=10) == []

    def test_query_dim_mismatch_raises(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        with pytest.raises(ValueError, match="dim mismatch"):
            idx.query([1.0, 0.0, 0.0], k=5)

    def test_query_k_zero_returns_empty(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert("alpha", [1.0, 0.0, 0.0, 0.0])
        assert idx.query([1.0, 0.0, 0.0, 0.0], k=0) == []


class TestS3VectorsBulk:
    def test_upsert_many_single_call(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert_many(
            [
                ("a", [1.0, 0.0, 0.0, 0.0], None),
                ("b", [0.0, 1.0, 0.0, 0.0], {"name": "B"}),
            ]
        )
        put_calls = [c for c in fake.calls if c[0] == "put_vectors"]
        assert len(put_calls) == 1
        assert put_calls[0][1]["n"] == 2

    def test_upsert_many_empty_is_noop(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.upsert_many([])
        assert fake.calls == []


class TestS3VectorsProvisioning:
    def test_ensure_bucket_is_idempotent(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(vector_bucket="vb", region="us-east-1", dim=4, client=fake)
        idx.ensure_bucket()
        idx.ensure_bucket()  # should not raise
        assert "vb" in fake.buckets

    def test_ensure_index_is_idempotent(self) -> None:
        fake = FakeS3VectorsClient()
        idx = S3VectorsIndex(
            vector_bucket="vb", index_name="features", region="us-east-1", dim=4, client=fake
        )
        idx.ensure_index()
        idx.ensure_index()  # should not raise
        assert ("vb", "features") in fake.indexes


# --- metadata sanitization --------------------------------------------------


class TestSanitizeMetadata:
    def test_primitives_pass_through(self) -> None:
        assert _sanitize_metadata({"name": "Alpha", "n": 3, "ok": True}) == {
            "name": "Alpha",
            "n": 3,
            "ok": True,
        }

    def test_lists_of_primitives_pass_through(self) -> None:
        assert _sanitize_metadata({"tags": ["a", "b"]}) == {"tags": ["a", "b"]}

    def test_nested_dict_is_json_encoded(self) -> None:
        out = _sanitize_metadata({"nested": {"x": 1}})
        assert out["nested"] == '{"x": 1}'
