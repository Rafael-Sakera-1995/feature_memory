"""In-memory index + persistence helpers.

Two layers:

1. `MemoryIndex` (V2): an in-memory `dict[slug -> IndexEntry]` plus a FAISS
   index for semantic search. Single source of truth for `list_features`
   and `search_features` while the server is up. Hydrated from Storage on
   startup; mutated synchronously on every write; flushed to Storage
   (`caches/index.json` + `caches/embeddings.jsonl`) on a debounced timer.

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
from .search import Embedder, FAISSIndex, embed_text_for_entry
from .storage import Storage, StorageNotFound
from .store import list_slugs, read_feature


logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"
EMBEDDINGS_FILENAME = "embeddings.jsonl"


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


# --- V2 in-memory index -----------------------------------------------------


class _Debouncer:
    """Coalesces frequent `trigger()` calls into a single delayed `flush_fn()`.

    Pattern: when an update lands, we want to refresh the on-storage caches
    (index.json + embeddings.jsonl) but not on every keystroke. We schedule a
    timer; subsequent triggers reset the timer; when it fires, we call
    `flush_fn` once. Thread-safe.
    """

    def __init__(self, delay_seconds: float, flush_fn: Callable[[], None]) -> None:
        self._delay = delay_seconds
        self._fn = flush_fn
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        if self._delay <= 0:
            # Synchronous mode: useful for tests and the V1 stdio path where
            # we want the on-disk cache to be in sync with every write.
            try:
                self._fn()
            except Exception:  # pragma: no cover - defensive
                logger.exception("debouncer flush failed (sync mode)")
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def flush_now(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        try:
            self._fn()
        except Exception:  # pragma: no cover - defensive
            logger.exception("debouncer flush failed")

    def _fire(self) -> None:
        try:
            self._fn()
        except Exception:  # pragma: no cover - defensive
            logger.exception("debouncer flush failed")


class MemoryIndex:
    """In-memory state for the V2 hosted service.

    Holds:
    - `_entries`: dict[slug -> IndexEntry] - powers `list_features`.
    - `_faiss`: FAISSIndex - powers `search_features`.

    Mutations come from the server's write paths (create / update / correct
    / archive). After each mutation the caller invokes `upsert` or `remove`
    and `schedule_flush` to coalesce a cache write.

    Hydration: `from_storage` loads the cached `index.json` if present, and
    `embeddings.jsonl` if present. If a cache is missing, we re-parse the
    canonical .md files via the storage's `list_slugs` + `get_md` path.
    """

    def __init__(
        self,
        storage: Storage,
        embedder: Embedder,
        *,
        debounce_seconds: float = 60.0,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._entries: dict[str, IndexEntry] = {}
        self._faiss = FAISSIndex(dim=embedder.dim)
        self._lock = threading.Lock()
        self._debouncer = _Debouncer(debounce_seconds, self._flush_caches)

    # --- read API ---

    def list_entries(self) -> list[IndexEntry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.slug)

    def get_entry(self, slug: str) -> IndexEntry | None:
        with self._lock:
            return self._entries.get(slug)

    def search(self, query: str, k: int) -> list[tuple[IndexEntry, float]]:
        if not self._embedder.is_enabled():
            return []
        vector = self._embedder.embed_one(query)
        if not any(vector):
            return []
        hits = self._faiss.search(vector, k)
        results: list[tuple[IndexEntry, float]] = []
        with self._lock:
            for slug, score in hits:
                entry = self._entries.get(slug)
                if entry is not None:
                    results.append((entry, score))
        return results

    # --- write API ---

    def upsert(self, entry: IndexEntry) -> None:
        """Add or replace `entry` in memory and re-embed its summary."""
        with self._lock:
            self._entries[entry.slug] = entry
        if self._embedder.is_enabled():
            vector = self._embedder.embed_one(embed_text_for_entry(entry))
            self._faiss.add(entry.slug, vector)

    def remove(self, slug: str) -> None:
        with self._lock:
            self._entries.pop(slug, None)
        self._faiss.remove(slug)

    def schedule_flush(self) -> None:
        self._debouncer.trigger()

    def flush_now(self) -> None:
        self._debouncer.flush_now()

    # --- persistence ---

    def _flush_caches(self) -> None:
        payload = [e.model_dump(mode="json", exclude_none=True) for e in self.list_entries()]
        self._storage.put_cache(
            INDEX_FILENAME, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )
        self._storage.put_cache(EMBEDDINGS_FILENAME, self._faiss.dump_jsonl())

    @classmethod
    def from_storage(
        cls,
        storage: Storage,
        embedder: Embedder,
        *,
        debounce_seconds: float = 60.0,
        parse_feature: Callable[[str, str], IndexEntry] | None = None,
    ) -> "MemoryIndex":
        """Hydrate a `MemoryIndex` from Storage.

        `parse_feature(slug, content) -> IndexEntry` is injected so we don't
        introduce a circular import on `store.py`. The server passes a small
        lambda that wraps `store.parse_body` + `Frontmatter` validation.
        """
        idx = cls(storage, embedder, debounce_seconds=debounce_seconds)

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

        # Load cached embeddings if present; otherwise re-embed (only if enabled).
        emb_cached = storage.get_cache(EMBEDDINGS_FILENAME)
        if emb_cached and emb_cached.strip():
            idx._faiss = FAISSIndex.from_jsonl(emb_cached, dim=embedder.dim)
            # Drop any cached embeddings that no longer correspond to an active entry.
            stale = [s for s in list(idx._faiss._slug_to_id) if s not in idx._entries]
            for slug in stale:
                idx._faiss.remove(slug)
            # Embed any new entries that aren't in the cache.
            missing = [e for e in entries if not idx._faiss.has(e.slug)]
            if missing and embedder.is_enabled():
                vectors = embedder.embed([embed_text_for_entry(e) for e in missing])
                idx._faiss.add_many(zip([e.slug for e in missing], vectors))
        elif embedder.is_enabled() and entries:
            vectors = embedder.embed([embed_text_for_entry(e) for e in entries])
            idx._faiss.add_many(zip([e.slug for e in entries], vectors))

        return idx
