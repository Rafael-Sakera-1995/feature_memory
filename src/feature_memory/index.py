"""In-memory index + persistence helpers.

Two layers:

1. `MemoryIndex` (V3): an in-memory `dict[slug -> IndexEntry]` plus a reference
   to an `S3VectorsIndex` for semantic search. Single source of truth for
   `list_features` and `search_features` while the server is up. Hydrated from
   Storage's `caches/index.json` on startup; mutated synchronously on every
   write. The vector index itself lives entirely in S3 Vectors - no in-process
   FAISS, no debounced flush of embeddings.

2. Legacy V1 disk helpers (`build_index`, `read_index`, `write_index`) that
   operate directly on a `features/` directory. These remain for the V1
   stdio entrypoint, tests, and the migration script.

The legacy functions are unchanged in behavior so existing tests keep
passing without modification.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from .models import IndexEntry
from .search import Embedder, S3VectorsIndex, embed_text_for_entry
from .storage import Storage, StorageNotFound
from .store import list_slugs, read_feature


logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"


# --- Legacy V1 disk-based index (kept for stdio + tests + migration) -------


def build_index(features_dir: Path) -> list[IndexEntry]:
    """Read every active feature and return a list of `IndexEntry`."""
    entries: list[IndexEntry] = []
    for slug in list_slugs(features_dir):
        feat = read_feature(slug, features_dir)
        fm = feat.frontmatter
        entries.append(
            IndexEntry(
                slug=fm.slug,
                name=fm.name,
                summary=fm.summary,
                key_paths=list(fm.key_paths),
                tags=list(fm.tags),
                parent_feature=fm.parent_feature,
            )
        )
    return entries


def read_index(features_dir: Path) -> list[IndexEntry] | None:
    """Read `index.json` if it exists and is fresh. Returns None on miss/stale.

    Staleness checks (cheap stat calls):
    1. File exists.
    2. Every active .md has mtime <= index.json mtime.
    3. Set of slugs in index == set on disk.
    4. JSON parses and validates.
    """
    index_path = features_dir / INDEX_FILENAME
    if not index_path.exists():
        return None

    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        return None

    slugs_on_disk = set(list_slugs(features_dir))
    for slug in slugs_on_disk:
        md_path = features_dir / f"{slug}.md"
        try:
            if md_path.stat().st_mtime > index_mtime:
                return None
        except OSError:
            return None

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        entries = [IndexEntry.model_validate(row) for row in data]
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None

    if {e.slug for e in entries} != slugs_on_disk:
        return None

    return entries


def write_index(features_dir: Path) -> Path:
    """Rebuild and write `<features_dir>/index.json`. Returns the path."""
    entries = build_index(features_dir)
    payload = [entry.model_dump(mode="json", exclude_none=True) for entry in entries]
    path = features_dir / INDEX_FILENAME
    features_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# --- V3 in-memory entry index ----------------------------------------------


class MemoryIndex:
    """In-memory state for the V3 hosted service.

    Holds:
    - `_entries`: `dict[slug -> IndexEntry]` - powers `list_features` and is
      used to enrich search hits (the S3 Vectors query only returns slugs).
    - `_vectors`: optional `S3VectorsIndex` - powers `search_features`. When
      None (local/stdio paths), `search_features` returns `[]`.
    - `_embedder`: optional `Embedder` for re-embedding on writes + queries.
      When `is_enabled()` is False, `search()` returns `[]` and `upsert`
      skips the vector write.

    All mutations are synchronous:
    - `upsert(entry)` updates `_entries`, re-embeds, calls `S3VectorsIndex.upsert`,
      and flushes `index.json` to storage caches in one pass.
    - `remove(slug)` does the inverse.

    No debouncer. Writes are infrequent enough (human-driven, not request-
    driven) that batching offers no benefit and adds failure modes.
    """

    def __init__(
        self,
        storage: Storage,
        embedder: Embedder | None = None,
        vectors: S3VectorsIndex | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._vectors = vectors
        self._entries: dict[str, IndexEntry] = {}
        self._lock = threading.Lock()

    # --- read API ---

    def list_entries(self) -> list[IndexEntry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.slug)

    def get_entry(self, slug: str) -> IndexEntry | None:
        with self._lock:
            return self._entries.get(slug)

    def search(self, query: str, k: int) -> list[tuple[IndexEntry, float]]:
        if self._vectors is None or self._embedder is None or not self._embedder.is_enabled():
            return []
        vector = self._embedder.embed_one(query)
        if not any(vector):
            return []
        hits = self._vectors.query(vector, k)
        results: list[tuple[IndexEntry, float]] = []
        with self._lock:
            for slug, score in hits:
                entry = self._entries.get(slug)
                if entry is not None:
                    results.append((entry, score))
        return results

    # --- write API ---

    def upsert(self, entry: IndexEntry) -> None:
        """Add or replace `entry`. Re-embeds + writes to S3 Vectors synchronously."""
        with self._lock:
            self._entries[entry.slug] = entry
        if self._vectors is not None and self._embedder is not None and self._embedder.is_enabled():
            vector = self._embedder.embed_one(embed_text_for_entry(entry))
            metadata = {
                "name": entry.name,
                "tags": list(entry.tags),
            }
            if entry.parent_feature:
                metadata["parent_feature"] = entry.parent_feature
            self._vectors.upsert(entry.slug, vector, metadata=metadata)
        self._flush_index_cache()

    def remove(self, slug: str) -> None:
        with self._lock:
            self._entries.pop(slug, None)
        if self._vectors is not None:
            try:
                self._vectors.delete(slug)
            except Exception:  # pragma: no cover - defensive
                logger.exception("failed to delete vector for %s", slug)
        self._flush_index_cache()

    def flush_now(self) -> None:
        """Force-flush the entry cache. Kept for API parity with V2 callers."""
        self._flush_index_cache()

    # --- persistence ---

    def _flush_index_cache(self) -> None:
        payload = [e.model_dump(mode="json", exclude_none=True) for e in self.list_entries()]
        try:
            self._storage.put_cache(
                INDEX_FILENAME, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            )
        except Exception:  # pragma: no cover - cache writes are best-effort
            logger.exception("failed to flush %s", INDEX_FILENAME)

    @classmethod
    def from_storage(
        cls,
        storage: Storage,
        embedder: Embedder | None = None,
        vectors: S3VectorsIndex | None = None,
        *,
        parse_feature: Callable[[str, str], IndexEntry] | None = None,
        rebuild_vectors_on_hydrate: bool = False,
    ) -> "MemoryIndex":
        """Hydrate a `MemoryIndex` from Storage.

        Loads `caches/index.json` if present; falls back to re-parsing every
        `.md` via `parse_feature` if the cache is missing or stale. The
        vector index itself is NOT rebuilt here - it persists in S3 Vectors
        across restarts. Pass `rebuild_vectors_on_hydrate=True` to force a
        full re-embed + put-vectors pass; the migration script does this,
        the server boot path does not.

        `parse_feature(slug, content) -> IndexEntry` is injected so we don't
        introduce a circular import on `store.py`.
        """
        idx = cls(storage, embedder=embedder, vectors=vectors)

        cached = storage.get_cache(INDEX_FILENAME)
        entries: list[IndexEntry] = []
        if cached:
            try:
                for row in json.loads(cached):
                    entries.append(IndexEntry.model_validate(row))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                logger.warning("index.json cache malformed; rebuilding from .md files")
                entries = []

        if not entries and parse_feature is not None:
            for slug in storage.list_slugs():
                try:
                    content, _ = storage.get_md(slug)
                except StorageNotFound:
                    continue
                try:
                    entries.append(parse_feature(slug, content))
                except Exception:  # pragma: no cover - defensive
                    logger.exception("failed to parse %s during hydration", slug)

        for entry in entries:
            idx._entries[entry.slug] = entry

        if rebuild_vectors_on_hydrate and vectors is not None and embedder is not None and embedder.is_enabled() and entries:
            texts = [embed_text_for_entry(e) for e in entries]
            embeds = embedder.embed(texts)
            items: list[tuple[str, list[float], dict | None]] = []
            for entry, vec in zip(entries, embeds):
                md = {"name": entry.name, "tags": list(entry.tags)}
                if entry.parent_feature:
                    md["parent_feature"] = entry.parent_feature
                items.append((entry.slug, vec, md))
            vectors.upsert_many(items)
            logger.info("rebuilt vector index for %d entries", len(entries))

        return idx
