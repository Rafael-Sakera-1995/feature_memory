"""End-to-end tests for the FastMCP server.

Uses `mcp.call_tool` in-process — no subprocess, no stdio. Exercises the
full data flow through the registered tools.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from feature_memory.server import build_server
from feature_memory.store import ARCHIVED_DIR_NAME, archive_path, feature_path


@pytest.fixture
def server(tmp_path: Path):
    features_dir = tmp_path / "features"
    s = build_server(features_dir)
    s.features_dir = features_dir  # type: ignore[attr-defined]  # for tests
    return s


async def _call(server, name: str, args: dict) -> dict:
    """Helper: invoke a tool and return its structured-output dict.

    FastMCP returns (content_blocks, structured_dict) from `call_tool`.
    For a `list[T]` return type the structured payload is wrapped under
    `result`; for object return types the fields are at the top level.
    """
    _, structured = await server.call_tool(name, args)
    return structured


LAST_UPDATE = {
    "date": "2026-04-23",
    "author": "rafael",
    "change": "did the thing",
}


class TestCreateAndList:
    async def test_create_then_list(self, server) -> None:
        result = await _call(
            server,
            "create_feature",
            {
                "name": "Quick Task",
                "summary": "Lightweight tasks for users.",
                "key_paths": ["src/quick-task/**"],
                "body": "## Overview\nA tasks feature.\n",
            },
        )
        assert result["slug"] == "quick-task"

        listed = await _call(server, "list_features", {})
        assert len(listed["result"]) == 1
        assert listed["result"][0]["slug"] == "quick-task"

    async def test_create_collision_appends_suffix(self, server) -> None:
        first = await _call(server, "create_feature", {"name": "X", "summary": "s"})
        second = await _call(server, "create_feature", {"name": "X", "summary": "s"})
        assert first["slug"] == "x"
        assert second["slug"] == "x-2"


class TestGetFeature:
    async def test_get_returns_frontmatter_and_body(self, server) -> None:
        await _call(
            server,
            "create_feature",
            {
                "name": "Quick Task",
                "summary": "s",
                "body": "## Overview\nHello.\n",
            },
        )
        result = await _call(server, "get_feature", {"slug": "quick-task"})
        assert result["frontmatter"]["slug"] == "quick-task"
        assert "## Overview" in result["body_markdown"]
        assert "Hello." in result["body_markdown"]

    async def test_get_missing_raises(self, server) -> None:
        with pytest.raises(Exception) as exc:
            await _call(server, "get_feature", {"slug": "nope"})
        assert "not found" in str(exc.value).lower()


class TestUpdateFeature:
    async def test_update_appends_flow_and_overwrites_last_update(self, server) -> None:
        await _call(server, "create_feature", {"name": "Quick Task", "summary": "s"})
        result = await _call(
            server,
            "update_feature",
            {
                "slug": "quick-task",
                "patch": {
                    "add_flows": ["new flow"],
                    "last_update": LAST_UPDATE,
                },
            },
        )
        assert result["ok"] is True
        assert "new flow" in result["diff"]

        feat = await _call(server, "get_feature", {"slug": "quick-task"})
        assert "new flow" in feat["body_markdown"]
        assert "## Last Update" in feat["body_markdown"]
        assert "rafael" in feat["body_markdown"]
        assert "did the thing" in feat["body_markdown"]

    async def test_second_update_replaces_last_update_entry(self, server) -> None:
        await _call(server, "create_feature", {"name": "Quick Task", "summary": "s"})
        await _call(
            server,
            "update_feature",
            {
                "slug": "quick-task",
                "patch": {"last_update": LAST_UPDATE},
            },
        )
        await _call(
            server,
            "update_feature",
            {
                "slug": "quick-task",
                "patch": {
                    "last_update": {
                        "date": "2026-04-24",
                        "author": "agent",
                        "change": "second change",
                    }
                },
            },
        )
        feat = await _call(server, "get_feature", {"slug": "quick-task"})
        body = feat["body_markdown"]
        last_update_section = body.split("## Last Update", 1)[1]
        assert "second change" in last_update_section
        assert "did the thing" not in last_update_section
        assert "rafael" not in last_update_section

    async def test_update_emits_size_warning(self, server) -> None:
        many_flows = [f"flow {i}" for i in range(30)]
        await _call(
            server,
            "create_feature",
            {
                "name": "Big",
                "summary": "s",
                "body": "## Flows\n" + "\n".join(f"- {x}" for x in many_flows) + "\n",
            },
        )
        result = await _call(
            server,
            "update_feature",
            {
                "slug": "big",
                "patch": {"last_update": LAST_UPDATE},
            },
        )
        assert any("flows" in w for w in result["warnings"])

    async def test_update_missing_raises(self, server) -> None:
        with pytest.raises(Exception):
            await _call(
                server,
                "update_feature",
                {"slug": "nope", "patch": {"last_update": LAST_UPDATE}},
            )


class TestCorrectFeature:
    async def test_remove_flow(self, server) -> None:
        await _call(
            server,
            "create_feature",
            {
                "name": "QT",
                "summary": "s",
                "body": "## Flows\n- flow A\n- flow B\n",
            },
        )
        result = await _call(
            server,
            "correct_feature",
            {
                "slug": "qt",
                "corrections": [
                    {"op": "remove_flow", "text": "flow A", "reason": "obsolete"}
                ],
            },
        )
        assert result["ok"] is True
        assert "flow A" in result["diff"]

        feat = await _call(server, "get_feature", {"slug": "qt"})
        body = feat["body_markdown"]
        flows_section = body.split("## Flows", 1)[1].split("##", 1)[0]
        assert "flow A" not in flows_section
        assert "flow B" in flows_section
        assert "Removed flow" in body
        assert "obsolete" in body

    async def test_remove_missing_raises(self, server) -> None:
        await _call(server, "create_feature", {"name": "QT", "summary": "s"})
        with pytest.raises(Exception) as exc:
            await _call(
                server,
                "correct_feature",
                {
                    "slug": "qt",
                    "corrections": [
                        {"op": "remove_flow", "text": "ghost", "reason": "x"}
                    ],
                },
            )
        assert "not found" in str(exc.value).lower()


class TestArchiveFeature:
    async def test_archive_moves_file_and_drops_from_index(self, server) -> None:
        await _call(server, "create_feature", {"name": "Old", "summary": "s"})
        features_dir: Path = server.features_dir  # type: ignore[attr-defined]

        result = await _call(
            server,
            "archive_feature",
            {"slug": "old", "reason": "merged into Tasks"},
        )
        assert result["ok"] is True
        archived = archive_path("old", features_dir)
        assert Path(result["archived_path"]) == archived
        assert archived.exists()
        assert not feature_path("old", features_dir).exists()

        listed = await _call(server, "list_features", {})
        assert listed["result"] == []

        index_path = features_dir / "index.json"
        assert json.loads(index_path.read_text()) == []

    async def test_get_archived_raises(self, server) -> None:
        await _call(server, "create_feature", {"name": "Old", "summary": "s"})
        await _call(
            server, "archive_feature", {"slug": "old", "reason": "x"}
        )
        with pytest.raises(Exception) as exc:
            await _call(server, "get_feature", {"slug": "old"})
        assert "archived" in str(exc.value).lower()
