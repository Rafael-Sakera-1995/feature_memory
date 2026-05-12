"""Index derivation.

`index.json` is a stateless rebuild from all active feature frontmatter.
Never edited by hand. Excludes archived features.

Two read paths:

- `build_index(features_dir)` — authoritative rebuild. Reads every feature
  file from disk. Always correct, never stale. Use after any state change.
- `read_index(features_dir)` — fast path. Reads `index.json` and verifies it
  is still in sync with the .md files via mtime + slug-set checks. Returns
  None if the file is missing, malformed, or stale, in which case callers
  should fall back to `build_index`.

The fast path matters because `list_features` is called on every auto-detect
flow; opening and parsing every .md file just to extract frontmatter is
wasted work as the corpus grows.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .models import IndexEntry
from .store import list_slugs, read_feature


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

    Staleness checks (cheap — just `stat` calls):

    1. The file exists.
    2. Every active `<slug>.md` has `mtime <= index.json mtime`. A newer
       feature file means someone hand-edited and we cannot trust the cache.
    3. The set of slugs in the index matches the set on disk. Catches files
       added or removed outside the MCP (e.g. a manual `git checkout`).
    4. The JSON parses and validates against `IndexEntry`.

    Any failure returns None. The caller should fall back to `build_index`
    and may want to call `write_index` to repopulate the cache.
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
