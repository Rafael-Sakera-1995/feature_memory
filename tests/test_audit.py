"""Tests for the audit module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feature_memory import audit
from feature_memory.storage import LocalFSStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalFSStorage:
    return LocalFSStorage(tmp_path / "features")


class TestMakeEvent:
    def test_required_fields_present(self) -> None:
        event = audit.make_event(
            actor="rafael",
            action="update_feature",
            slug="x",
            payload={"diff_size": 42},
        )
        assert event["actor"] == "rafael"
        assert event["action"] == "update_feature"
        assert event["slug"] == "x"
        assert event["payload"]["diff_size"] == 42
        assert "ts" in event

    def test_none_payload_becomes_empty_dict(self) -> None:
        event = audit.make_event(
            actor="a", action="b", slug=None, payload=None
        )
        assert event["payload"] == {}
        assert event["slug"] is None


class TestAppend:
    def test_writes_event_to_storage(self, storage: LocalFSStorage) -> None:
        key = audit.append(
            storage,
            actor="rafael",
            action="update_feature",
            slug="x",
            payload={"diff_size": 12},
        )
        assert key is not None
        audit_files = list((storage.features_dir / ".audit").rglob("*.json"))
        assert len(audit_files) == 1
        loaded = json.loads(audit_files[0].read_text())
        assert loaded["actor"] == "rafael"

    def test_swallows_exceptions(self, storage: LocalFSStorage) -> None:
        class BrokenStorage:
            def append_audit(self, _payload):
                raise RuntimeError("disk full")

        # Best-effort: failure must not propagate.
        result = audit.append(
            BrokenStorage(),  # type: ignore[arg-type]
            actor="x",
            action="y",
            slug=None,
        )
        assert result is None
