"""Append-only audit trail.

Every mutating tool call (create / update / correct / archive) writes one
small JSON blob to the configured Storage. Layout is `audit/YYYY-MM-DD/...`.
The path is owned by the Storage implementation; this module is just the
event shape and a thin wrapper.

V1 deliberately has no audit *read* API. If we need to query, we'll either
attach a SIEM or expose a separate read endpoint later. Until then, audit is
forensic-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .storage import Storage


logger = logging.getLogger(__name__)


def make_event(
    *,
    actor: str,
    action: str,
    slug: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical audit-event dict. Kept pure for unit tests."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "slug": slug,
        "payload": payload or {},
    }


def append(
    storage: Storage,
    *,
    actor: str,
    action: str,
    slug: str | None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Persist an audit event. Returns the storage key or None on failure.

    Audit writes are best-effort: a failure to write the audit blob must not
    block the user-facing operation (the source of truth is still the
    markdown file and the user already sees a successful response). We log
    and swallow.
    """
    event = make_event(actor=actor, action=action, slug=slug, payload=payload)
    try:
        return storage.append_audit(event)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "audit.append failed actor=%s action=%s slug=%s", actor, action, slug
        )
        return None
