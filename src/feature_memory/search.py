"""Semantic search over feature memories.

Two collaborators:

- `Embedder`: thin OpenAI wrapper that produces L2-normalized vectors for
  one or many strings. Centralized so tests can mock-substitute it and so we
  can swap models without touching the server.
- `FAISSIndex`: in-memory IndexFlatIP (cosine via normalized vectors), with
  an ID map so we can `add` / `remove` by slug. ~30MB at 5K features.

Only the `summary + name + tags` text is embedded; the body is never sent to
OpenAI. This keeps the index small, the search latency sub-100ms, and the
egress cost trivial. Recall on body details is a non-goal: a hit returns the
slug, the agent then calls `get_feature` to load the full body.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

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


# --- Embedder ---------------------------------------------------------------


class Embedder:
    """OpenAI embedding client wrapper.

    If `api_key` is None, the embedder is in *disabled* mode: it returns
    zero vectors and reports `is_enabled() == False`. This keeps the server
    bootable in dev/test environments without an OpenAI key; `search_features`
    degrades gracefully (returns []).
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
    ) -> None:
        self._model = model
        self._dim = dim
        self._client = None
        if api_key:
            try:
                from openai import OpenAI  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - dep contract
                raise RuntimeError(
                    "openai package is required when OPENAI_API_KEY is set"
                ) from exc
            self._client = OpenAI(api_key=api_key)

    @property
    def dim(self) -> int:
        return self._dim

    def is_enabled(self) -> bool:
        return self._client is not None

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input string.

        When disabled, returns zero vectors of the configured dim. Callers
        downstream (FAISS search) will get score 0.0 for everything, which
        the server interprets as "no semantic ranking available".
        """
        if not texts:
            return []
        if not self.is_enabled():
            return [[0.0] * self._dim for _ in texts]
        response = self._client.embeddings.create(  # type: ignore[union-attr]
            model=self._model,
            input=texts,
        )
        return [_normalize(d.embedding) for d in response.data]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize so dot product == cosine similarity in FAISS IndexFlatIP."""
    sq = sum(x * x for x in vec)
    if sq <= 0.0:
        return list(vec)
    inv = 1.0 / (sq ** 0.5)
    return [x * inv for x in vec]


# --- FAISS index ------------------------------------------------------------


class FAISSIndex:
    """In-memory cosine-similarity index keyed by slug.

    Uses faiss.IndexFlatIP wrapped in IndexIDMap so we can map back to slugs.
    Remove is implemented via remove_ids; add is unconditional (callers should
    `remove` first if re-indexing the same slug).

    Cold start: load slugs+vectors from a Storage cache via `load_jsonl`.
    Save back via `dump_jsonl` (debounced by the caller).
    """

    def __init__(self, dim: int = 1536) -> None:
        try:
            import faiss  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dep contract
            raise RuntimeError(
                "faiss-cpu is required for FAISSIndex; install with `pip install faiss-cpu`"
            ) from exc
        self._faiss = faiss
        self._dim = dim
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
        self._slug_to_id: dict[str, int] = {}
        self._id_to_slug: dict[int, str] = {}
        self._vectors: dict[str, list[float]] = {}
        self._next_id = 0

    @property
    def dim(self) -> int:
        return self._dim

    def size(self) -> int:
        return len(self._slug_to_id)

    def has(self, slug: str) -> bool:
        return slug in self._slug_to_id

    def add(self, slug: str, vector: list[float]) -> None:
        """Add or replace `slug -> vector`. Idempotent on re-add."""
        import numpy as np  # type: ignore[import-not-found]

        if len(vector) != self._dim:
            raise ValueError(
                f"vector dim mismatch: got {len(vector)}, expected {self._dim}"
            )
        if slug in self._slug_to_id:
            self.remove(slug)
        ident = self._next_id
        self._next_id += 1
        self._slug_to_id[slug] = ident
        self._id_to_slug[ident] = slug
        self._vectors[slug] = list(vector)
        vec = np.asarray([vector], dtype="float32")
        ids = np.asarray([ident], dtype="int64")
        self._index.add_with_ids(vec, ids)

    def add_many(self, items: Iterable[tuple[str, list[float]]]) -> None:
        """Bulk-add. Faster than `add` in a loop for large batches."""
        import numpy as np

        items = list(items)
        if not items:
            return
        vectors: list[list[float]] = []
        ids: list[int] = []
        for slug, vec in items:
            if len(vec) != self._dim:
                raise ValueError(
                    f"vector dim mismatch for {slug!r}: got {len(vec)}, expected {self._dim}"
                )
            if slug in self._slug_to_id:
                self.remove(slug)
            ident = self._next_id
            self._next_id += 1
            self._slug_to_id[slug] = ident
            self._id_to_slug[ident] = slug
            self._vectors[slug] = list(vec)
            vectors.append(vec)
            ids.append(ident)
        self._index.add_with_ids(
            np.asarray(vectors, dtype="float32"),
            np.asarray(ids, dtype="int64"),
        )

    def remove(self, slug: str) -> None:
        import numpy as np

        if slug not in self._slug_to_id:
            return
        ident = self._slug_to_id.pop(slug)
        self._id_to_slug.pop(ident, None)
        self._vectors.pop(slug, None)
        self._index.remove_ids(np.asarray([ident], dtype="int64"))

    def search(self, vector: list[float], k: int) -> list[tuple[str, float]]:
        """Top-k nearest neighbors as (slug, score). score is cosine similarity."""
        import numpy as np

        if len(vector) != self._dim:
            raise ValueError(
                f"query vector dim mismatch: got {len(vector)}, expected {self._dim}"
            )
        if self.size() == 0 or k <= 0:
            return []
        # Treat all-zero vector as "no signal" - return empty rather than nonsense.
        if not any(vector):
            return []
        q = np.asarray([vector], dtype="float32")
        scores, ids = self._index.search(q, min(k, self.size()))
        results: list[tuple[str, float]] = []
        for ident, score in zip(ids[0].tolist(), scores[0].tolist()):
            if ident == -1:
                continue
            slug = self._id_to_slug.get(ident)
            if slug is None:
                continue
            results.append((slug, float(score)))
        return results

    # --- Persistence (JSONL on Storage) ---

    def dump_jsonl(self) -> str:
        """Serialize the index to JSONL (one {"slug","vector"} per line).

        Uses an internal `_vectors` side cache rather than poking into the
        FAISS index; IndexIDMap's reconstruct API is fiddly and we'd rather
        spend the ~30MB to avoid that surface.
        """
        lines = [
            json.dumps({"slug": slug, "vector": self._vectors[slug]})
            for slug in self._slug_to_id
        ]
        return "\n".join(lines) + ("\n" if lines else "")

    @classmethod
    def from_jsonl(cls, content: str, dim: int = 1536) -> "FAISSIndex":
        """Reconstruct an index from `dump_jsonl` output. None-safe on empty."""
        index = cls(dim=dim)
        items: list[tuple[str, list[float]]] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                slug = row["slug"]
                vector = row["vector"]
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("from_jsonl: skipping malformed line: %r", line[:80])
                continue
            items.append((slug, vector))
        index.add_many(items)
        return index
