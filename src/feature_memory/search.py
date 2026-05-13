"""Semantic search backends for the feature memory.

V3 architecture:

- `Embedder`: AWS Bedrock wrapper. Calls `bedrock-runtime.invoke_model` against
  Titan Text Embeddings v2 (`amazon.titan-embed-text-v2:0`) to produce
  L2-normalized 1024-dim vectors. Same AWS credentials as S3 - no separate
  secret to manage.
- `S3VectorsIndex`: thin wrapper over the `s3vectors` boto3 client. Provides
  `upsert / delete / query` against an S3 Vectors index. The vector index
  lives entirely in S3, not in process memory - the server is stateless w.r.t.
  search.

Only the `summary + name + tags` text of each feature is embedded; the body
is never sent to Bedrock. This keeps the index small, the search latency
predictable, and the per-query Bedrock cost trivial.

Both classes accept an injected client (`bedrock_client=` / `vectors_client=`)
to make tests trivial: pass a fake, no AWS round-trip.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .models import IndexEntry


logger = logging.getLogger(__name__)


EMBED_FIELDS = ("summary", "name", "tags")


def embed_text_for_entry(entry: IndexEntry) -> str:
    """The canonical string we embed for a feature. Public for tests + scripts."""
    return " ".join(
        [
            entry.summary.strip(),
            entry.name.strip(),
            " ".join(t.strip() for t in entry.tags),
        ]
    ).strip()


# --- Embedder (Bedrock Titan v2) --------------------------------------------


class BedrockEmbeddingsClient(Protocol):
    """Subset of bedrock-runtime that we actually use.

    Lets tests inject a fake without depending on moto's coverage of Bedrock,
    which is patchy.
    """

    def invoke_model(self, *, modelId: str, body: str, **kwargs: Any) -> dict: ...


class Embedder:
    """Bedrock Titan Text Embeddings v2 wrapper.

    Request shape (Titan v2):
        {"inputText": "...", "dimensions": 1024, "normalize": true}

    Response shape:
        {"embedding": [float, ...], "inputTextTokenCount": N}

    We pass `normalize=true` so we get L2-normalized vectors out of the box;
    no client-side normalization needed before storing in S3 Vectors.

    If `enabled=False`, the embedder is in disabled mode: `embed()` returns
    zero vectors and `is_enabled()` is False. This keeps tests + local dev
    bootable without Bedrock access - `search_features` degrades to `[]`.
    """

    def __init__(
        self,
        *,
        region: str,
        model_id: str = "amazon.titan-embed-text-v2:0",
        dim: int = 1024,
        enabled: bool = True,
        client: BedrockEmbeddingsClient | None = None,
    ) -> None:
        self._region = region
        self._model_id = model_id
        self._dim = dim
        self._enabled = enabled
        if not enabled:
            self._client: BedrockEmbeddingsClient | None = None
            return
        if client is not None:
            self._client = client
            return
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dep contract
            raise RuntimeError(
                "boto3 is required for Bedrock Embedder; install with `pip install boto3`"
            ) from exc
        self._client = boto3.client("bedrock-runtime", region_name=region)

    @property
    def dim(self) -> int:
        return self._dim

    def is_enabled(self) -> bool:
        return self._enabled

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized 1024-dim vector per input string.

        When disabled, returns zero vectors. Callers can either check
        `is_enabled()` upstream or let the empty signal flow through; both
        S3VectorsIndex.query and the MemoryIndex.search path treat zero
        vectors as "no signal".
        """
        if not texts:
            return []
        if not self.is_enabled() or self._client is None:
            return [[0.0] * self._dim for _ in texts]
        # Titan v2 invoke is one-text-per-call - batch ourselves.
        out: list[list[float]] = []
        for text in texts:
            body = json.dumps(
                {
                    "inputText": text,
                    "dimensions": self._dim,
                    "normalize": True,
                }
            )
            response = self._client.invoke_model(
                modelId=self._model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
            out.append([float(x) for x in payload["embedding"]])
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# --- S3 Vectors index -------------------------------------------------------


class S3VectorsClient(Protocol):
    """Subset of the `s3vectors` boto3 client that we actually use."""

    def put_vectors(
        self, *, vectorBucketName: str, indexName: str, vectors: list[dict]
    ) -> dict: ...

    def delete_vectors(
        self, *, vectorBucketName: str, indexName: str, keys: list[str]
    ) -> dict: ...

    def query_vectors(
        self,
        *,
        vectorBucketName: str,
        indexName: str,
        topK: int,
        queryVector: dict,
        returnDistance: bool = ...,
        returnMetadata: bool = ...,
    ) -> dict: ...

    def list_vectors(
        self,
        *,
        vectorBucketName: str,
        indexName: str,
        maxResults: int = ...,
        nextToken: str = ...,
        returnMetadata: bool = ...,
    ) -> dict: ...

    def create_vector_bucket(self, *, vectorBucketName: str, **kwargs: Any) -> dict: ...

    def create_index(
        self,
        *,
        vectorBucketName: str,
        indexName: str,
        dataType: str,
        dimension: int,
        distanceMetric: str,
        **kwargs: Any,
    ) -> dict: ...


class S3VectorsIndex:
    """In-S3 vector index. All persistence lives in AWS.

    Wraps the bare minimum of the `s3vectors` API. The server holds one
    instance per process; tests inject a fake `S3VectorsClient` and assert
    on the call shape.

    Distance vs similarity: `query_vectors` returns `distance` (lower is
    better). For the `cosine` metric, similarity = 1 - distance. We expose
    similarity scores externally so the caller-facing semantics match what
    the previous FAISS-backed implementation returned.
    """

    def __init__(
        self,
        *,
        vector_bucket: str,
        index_name: str = "features",
        region: str,
        dim: int = 1024,
        client: S3VectorsClient | None = None,
    ) -> None:
        if not vector_bucket:
            raise ValueError("S3VectorsIndex requires a non-empty vector_bucket")
        self._bucket = vector_bucket
        self._index = index_name
        self._region = region
        self._dim = dim
        if client is not None:
            self._client: S3VectorsClient = client
            return
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dep contract
            raise RuntimeError(
                "boto3 is required for S3VectorsIndex; install with `pip install boto3`"
            ) from exc
        self._client = boto3.client("s3vectors", region_name=region)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def vector_bucket(self) -> str:
        return self._bucket

    @property
    def index_name(self) -> str:
        return self._index

    def upsert(
        self,
        slug: str,
        vector: list[float],
        metadata: dict | None = None,
    ) -> None:
        """Insert-or-replace a single vector keyed by `slug`.

        S3 Vectors `PutVectors` is upsert semantics: re-putting the same key
        overwrites the previous data + metadata.
        """
        if len(vector) != self._dim:
            raise ValueError(
                f"vector dim mismatch for {slug!r}: got {len(vector)}, expected {self._dim}"
            )
        item: dict[str, Any] = {
            "key": slug,
            "data": {"float32": [float(x) for x in vector]},
        }
        if metadata:
            item["metadata"] = _sanitize_metadata(metadata)
        self._client.put_vectors(
            vectorBucketName=self._bucket,
            indexName=self._index,
            vectors=[item],
        )

    def upsert_many(self, items: list[tuple[str, list[float], dict | None]]) -> None:
        """Bulk-upsert. Single PutVectors call, much cheaper than a loop.

        S3 Vectors caps each call at 500 vectors - callers above that need to
        chunk. We're nowhere near that ceiling so don't auto-chunk.
        """
        if not items:
            return
        payload: list[dict[str, Any]] = []
        for slug, vector, metadata in items:
            if len(vector) != self._dim:
                raise ValueError(
                    f"vector dim mismatch for {slug!r}: got {len(vector)}, expected {self._dim}"
                )
            entry: dict[str, Any] = {
                "key": slug,
                "data": {"float32": [float(x) for x in vector]},
            }
            if metadata:
                entry["metadata"] = _sanitize_metadata(metadata)
            payload.append(entry)
        self._client.put_vectors(
            vectorBucketName=self._bucket,
            indexName=self._index,
            vectors=payload,
        )

    def delete(self, slug: str) -> None:
        """Remove a vector by key. Idempotent: missing keys do not error."""
        self._client.delete_vectors(
            vectorBucketName=self._bucket,
            indexName=self._index,
            keys=[slug],
        )

    def query(
        self, vector: list[float], k: int, *, return_metadata: bool = False
    ) -> list[tuple[str, float, dict | None]]:
        """Top-k nearest neighbors as `(slug, similarity, metadata)`.

        `similarity` is in [-1, 1] (1 = identical, 0 = orthogonal) regardless
        of how S3 Vectors phrases its `distance` field internally.

        When `return_metadata=True`, the third tuple element is the metadata
        dict that was stored alongside the vector at PutVectors time (e.g.
        `{"name": ..., "summary": ...}`). When `return_metadata=False`, it's
        `None`. Callers that just need the slug + score can ignore it.
        """
        if k <= 0:
            return []
        if len(vector) != self._dim:
            raise ValueError(
                f"query vector dim mismatch: got {len(vector)}, expected {self._dim}"
            )
        if not any(vector):
            return []
        response = self._client.query_vectors(
            vectorBucketName=self._bucket,
            indexName=self._index,
            topK=k,
            queryVector={"float32": [float(x) for x in vector]},
            returnDistance=True,
            returnMetadata=return_metadata,
        )
        out: list[tuple[str, float, dict | None]] = []
        for hit in response.get("vectors", []):
            slug = hit.get("key")
            distance = hit.get("distance")
            if slug is None or distance is None:
                continue
            similarity = 1.0 - float(distance)
            md = hit.get("metadata") if return_metadata else None
            out.append((slug, similarity, md))
        return out

    def list_all(self, *, return_metadata: bool = True) -> list[tuple[str, dict | None]]:
        """Enumerate every (key, metadata) tuple in the index, paginating.

        S3 Vectors paginates with `nextToken`. We loop until exhausted. At
        our scale (low thousands of features) this is one or two pages and
        ~100-300ms total - cheap enough to call inline from `list_features`
        without any RAM-side caching. If we ever cross ~50K features this
        should become a periodically-refreshed in-process snapshot instead.
        """
        out: list[tuple[str, dict | None]] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "vectorBucketName": self._bucket,
                "indexName": self._index,
                "returnMetadata": return_metadata,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            response = self._client.list_vectors(**kwargs)
            for row in response.get("vectors", []):
                key = row.get("key")
                if key is None:
                    continue
                md = row.get("metadata") if return_metadata else None
                out.append((key, md))
            next_token = response.get("nextToken")
            if not next_token:
                break
        return out

    # --- Idempotent provisioning helpers (dev/migration only) ---

    def ensure_bucket(self) -> None:
        """Idempotent CreateVectorBucket. Swallows already-exists errors.

        DEV/MIGRATION ONLY - production vector buckets should be DevOps-
        provisioned with the right tags and encryption.
        """
        try:
            from botocore.exceptions import ClientError  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dep contract
            raise RuntimeError("botocore is required") from exc
        try:
            self._client.create_vector_bucket(vectorBucketName=self._bucket)
            logger.info("created vector bucket %s in %s", self._bucket, self._region)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ConflictException", "BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                logger.info("vector bucket %s already exists - reusing", self._bucket)
                return
            raise

    def ensure_index(self, *, distance_metric: str = "cosine") -> None:
        """Idempotent CreateIndex. Swallows already-exists errors.

        DEV/MIGRATION ONLY - production indexes should be DevOps-provisioned.
        """
        try:
            from botocore.exceptions import ClientError  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dep contract
            raise RuntimeError("botocore is required") from exc
        try:
            self._client.create_index(
                vectorBucketName=self._bucket,
                indexName=self._index,
                dataType="float32",
                dimension=self._dim,
                distanceMetric=distance_metric,
            )
            logger.info(
                "created vector index %s in bucket %s (dim=%d, metric=%s)",
                self._index,
                self._bucket,
                self._dim,
                distance_metric,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ConflictException", "IndexAlreadyExists"):
                logger.info(
                    "vector index %s already exists in bucket %s - reusing",
                    self._index,
                    self._bucket,
                )
                return
            raise


def _sanitize_metadata(md: dict) -> dict:
    """S3 Vectors metadata is `Map[String, AttributeValue]` - coerce types.

    We accept primitives + flat lists of primitives; nested dicts get JSON-
    encoded to strings so they survive the round-trip without us hand-writing
    a schema for every field.
    """
    out: dict[str, Any] = {}
    for k, v in md.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v):
            out[k] = v
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out
