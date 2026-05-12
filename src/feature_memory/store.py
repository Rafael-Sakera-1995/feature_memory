"""Disk persistence for feature files.

This module knows about file paths, frontmatter, and section parsing.
It does not know about merging, validation logic beyond schema-loading,
or anything MCP-related.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

import frontmatter
import yaml

from .models import Feature, FeatureBody, Frontmatter, UpdateEntry


ARCHIVED_DIR_NAME = "_archived"
KNOWN_SECTIONS = ("Overview", "Architecture", "Flows", "Gotchas", "Last Update")
LEGACY_HISTORY_SECTION = "History"
UPDATE_LINE_RE = re.compile(
    r"^-\s*(?P<date>\d{4}-\d{2}-\d{2})\s*-\s*(?P<author>[^-]+?)\s*-\s*(?P<change>.+)$"
)


class FeatureNotFound(Exception):
    """Raised when a slug cannot be resolved to an active feature file."""


class FeatureArchived(Exception):
    """Raised when caller tries to read an archived slug as if it were active."""

    def __init__(self, slug: str, archived_path: Path) -> None:
        super().__init__(
            f"feature {slug!r} is archived at {archived_path}; restore it by hand if needed"
        )
        self.slug = slug
        self.archived_path = archived_path


class SlugCollision(Exception):
    """Raised when no free slug variant can be derived (extremely rare)."""


# --- Slug derivation ---------------------------------------------------------


def slugify(name: str) -> str:
    """Lowercase, ASCII, hyphenated.

    Non-word chars become hyphens. Repeated hyphens collapse. Leading/trailing
    hyphens stripped. Empty result raises ValueError.
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    cleaned = re.sub(r"-+", "-", hyphenated).strip("-")
    if not cleaned:
        raise ValueError(f"cannot derive slug from name {name!r}")
    return cleaned


def derive_unique_slug(name: str, features_dir: Path) -> str:
    """Slugify and add a numeric suffix if needed to avoid collisions.

    Considers both `features/` and `features/_archived/` so an archived
    feature cannot be silently reused.
    """
    base = slugify(name)
    candidate = base
    suffix = 2
    archived_dir = features_dir / ARCHIVED_DIR_NAME
    while (features_dir / f"{candidate}.md").exists() or (
        archived_dir / f"{candidate}.md"
    ).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
        if suffix > 999:
            raise SlugCollision(f"could not derive unique slug for {name!r}")
    return candidate


# --- Path helpers ------------------------------------------------------------


def feature_path(slug: str, features_dir: Path) -> Path:
    return features_dir / f"{slug}.md"


def archive_path(slug: str, features_dir: Path) -> Path:
    return features_dir / ARCHIVED_DIR_NAME / f"{slug}.md"


def list_slugs(features_dir: Path) -> list[str]:
    """Active feature slugs (excludes `_archived/` and dotfiles)."""
    if not features_dir.exists():
        return []
    return sorted(
        p.stem
        for p in features_dir.glob("*.md")
        if p.is_file() and not p.name.startswith(".")
    )


# --- Body parsing / serialization -------------------------------------------


def _parse_bullets(section_text: str) -> list[str]:
    """Read `- item` bullets from a section body. Multi-line bullets supported."""
    items: list[str] = []
    current: list[str] | None = None
    for raw in section_text.splitlines():
        if raw.startswith("- "):
            if current is not None:
                items.append("\n".join(current).rstrip())
            current = [raw[2:]]
        elif current is not None and (raw.startswith("  ") or raw.strip() == ""):
            if raw.strip() == "":
                continue
            current.append(raw[2:] if raw.startswith("  ") else raw)
        else:
            if current is not None:
                items.append("\n".join(current).rstrip())
                current = None
    if current is not None:
        items.append("\n".join(current).rstrip())
    return [i for i in items if i.strip()]


def _parse_last_update(section_text: str) -> tuple[UpdateEntry | None, list[str]]:
    """Parse `## Last Update` (or legacy `## History`) into a single entry.

    Returns (latest_entry_or_None, unparsed_raw_lines).

    Back-compat: if the section contains many lines (legacy multi-entry
    history), pick the entry with the latest date as the surviving
    `last_update`. Unparseable lines are kept as leftovers so we never
    silently drop user-authored content; on the next write they will be
    surfaced under an extra section, not the trimmed Last Update.
    """
    entries: list[UpdateEntry] = []
    leftovers: list[str] = []
    for raw in section_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = UPDATE_LINE_RE.match(line)
        if not m:
            leftovers.append(line)
            continue
        try:
            entries.append(
                UpdateEntry(
                    date=date.fromisoformat(m.group("date")),
                    author=m.group("author").strip(),
                    change=m.group("change").strip(),
                )
            )
        except ValueError:
            leftovers.append(line)
    if not entries:
        return None, leftovers
    latest = max(entries, key=lambda e: e.date)
    return latest, leftovers


def _split_sections(body_text: str) -> list[tuple[str, str]]:
    """Split body markdown into ordered (heading, content) pairs.

    Headings are level-2 (`## Foo`). Content under each heading is collected
    verbatim until the next level-2 heading. Anything before the first
    heading is returned under heading `""`.
    """
    sections: list[tuple[str, list[str]]] = []
    preamble: list[str] = []
    current: tuple[str, list[str]] | None = None
    for raw in body_text.splitlines():
        if raw.startswith("## "):
            if current is not None:
                sections.append(current)
            current = (raw[3:].strip(), [])
        else:
            if current is None:
                preamble.append(raw)
            else:
                current[1].append(raw)
    if current is not None:
        sections.append(current)

    result: list[tuple[str, str]] = []
    if any(line.strip() for line in preamble):
        result.append(("", "\n".join(preamble).strip("\n")))
    for heading, lines in sections:
        result.append((heading, "\n".join(lines).strip("\n")))
    return result


def parse_body(body_text: str) -> FeatureBody:
    """Parse markdown body into a `FeatureBody`. Forgiving: missing sections OK.

    Back-compat: a legacy `## History` section is read with the same parser
    as `## Last Update` and the most recent entry survives as `last_update`.
    The next write will emit `## Last Update`.
    """
    overview = ""
    architecture = ""
    flows: list[str] = []
    gotchas: list[str] = []
    last_update: UpdateEntry | None = None
    extras: list[tuple[str, str]] = []
    update_leftovers: list[str] = []

    for heading, content in _split_sections(body_text):
        if heading == "":
            extras.append(("", content))
            continue
        normalized = heading.strip()
        if normalized == "Overview":
            overview = content.strip()
        elif normalized == "Architecture":
            architecture = content.strip()
        elif normalized == "Flows":
            flows = _parse_bullets(content)
        elif normalized == "Gotchas":
            gotchas = _parse_bullets(content)
        elif normalized in ("Last Update", LEGACY_HISTORY_SECTION):
            parsed, leftovers = _parse_last_update(content)
            if parsed is not None and (
                last_update is None or parsed.date >= last_update.date
            ):
                last_update = parsed
            if leftovers:
                update_leftovers.extend(leftovers)
        else:
            extras.append((heading, content))

    if update_leftovers:
        extras.append(("Last Update (unparsed)", "\n".join(update_leftovers)))

    return FeatureBody(
        overview=overview,
        architecture=architecture,
        flows=flows,
        gotchas=gotchas,
        last_update=last_update,
        extra_sections=extras,
    )


def _format_bullets(items: list[str]) -> str:
    if not items:
        return ""
    return "\n".join(f"- {item}" for item in items)


def _format_last_update(entry: UpdateEntry) -> str:
    return f"- {entry.date.isoformat()} - {entry.author} - {entry.change}"


def serialize_body(body: FeatureBody) -> str:
    """Serialize a `FeatureBody` back to canonical markdown.

    Section order: Overview, Architecture, Flows, Gotchas, then any
    `extra_sections` (in their original order), then `Last Update` last.
    Empty known sections are omitted.
    """
    parts: list[str] = []

    preamble_extras = [content for heading, content in body.extra_sections if heading == ""]
    other_extras = [(h, c) for h, c in body.extra_sections if h != ""]

    for content in preamble_extras:
        if content.strip():
            parts.append(content.strip())

    if body.overview.strip():
        parts.append("## Overview\n" + body.overview.strip())
    if body.architecture.strip():
        parts.append("## Architecture\n" + body.architecture.strip())
    if body.flows:
        parts.append("## Flows\n" + _format_bullets(body.flows))
    if body.gotchas:
        parts.append("## Gotchas\n" + _format_bullets(body.gotchas))

    for heading, content in other_extras:
        if heading == "Last Update (unparsed)":
            continue
        if content.strip():
            parts.append(f"## {heading}\n{content.strip()}")
        else:
            parts.append(f"## {heading}")

    if body.last_update is not None:
        parts.append("## Last Update\n" + _format_last_update(body.last_update))

    unparsed = [
        content for heading, content in body.extra_sections
        if heading == "Last Update (unparsed)"
    ]
    if unparsed and body.last_update is not None:
        parts[-1] = parts[-1] + "\n" + "\n".join(unparsed)
    elif unparsed:
        parts.append("## Last Update\n" + "\n".join(unparsed))

    return "\n\n".join(parts) + ("\n" if parts else "")


# --- File I/O ----------------------------------------------------------------


def read_feature(slug: str, features_dir: Path) -> Feature:
    """Read an active feature file. Raises if archived or missing."""
    path = feature_path(slug, features_dir)
    if not path.exists():
        archived = archive_path(slug, features_dir)
        if archived.exists():
            raise FeatureArchived(slug, archived)
        raise FeatureNotFound(slug)

    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    fm = Frontmatter(**post.metadata)
    body = parse_body(post.content)
    return Feature(frontmatter=fm, body=body)


def write_feature(feature: Feature, features_dir: Path) -> Path:
    """Write a feature back to disk. Returns the written path."""
    features_dir.mkdir(parents=True, exist_ok=True)
    path = feature_path(feature.frontmatter.slug, features_dir)
    fm_dict = feature.frontmatter.model_dump(mode="json", exclude_none=True)
    body_text = serialize_body(feature.body)
    yaml_block = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True).strip()
    full = f"---\n{yaml_block}\n---\n\n{body_text}".rstrip() + "\n"
    path.write_text(full, encoding="utf-8")
    return path


def move_to_archive(slug: str, features_dir: Path) -> Path:
    """Move an active feature into `_archived/`. Returns the new path."""
    src = feature_path(slug, features_dir)
    if not src.exists():
        raise FeatureNotFound(slug)
    dst_dir = features_dir / ARCHIVED_DIR_NAME
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        raise SlugCollision(
            f"archive target {dst} already exists; rename the archived copy first"
        )
    src.rename(dst)
    return dst
