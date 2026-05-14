"""Legacy V1 on-disk index helpers.

V3 deleted the runtime in-memory index entirely - both `list_features` and
`search_features` now hit S3 Vectors directly. This module survives only
because the V1 stdio path (used by tests and single-user local dev) still
maintains a `features/index.json` file alongside the .md files. Those
helpers are kept for backward compatibility.

If you're looking for the V3 server-side path, see:
- `feature_memory.search.S3VectorsIndex.list_all` for `list_features`
- `feature_memory.search.S3VectorsIndex.query` for `search_features`
- `feature_memory.storage.Storage.get_md` for `get_feature`
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from .models import IndexEntry
from .store import list_slugs, read_feature


logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"


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
