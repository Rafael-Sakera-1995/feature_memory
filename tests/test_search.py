"""Tests for Embedder + FAISSIndex.

We do NOT call OpenAI in tests. The Embedder is exercised in disabled
mode (api_key=None) and the FAISSIndex is fed hand-crafted vectors so we
can assert ranking properties deterministically.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from feature_memory.models import IndexEntry
from feature_memory.search import (
    Embedder,
    FAISSIndex,
    embed_text_for_entry,
    _normalize,
)


# --- helpers ----------------------------------------------------------------


def _unit(*components: float) -> list[float]:
    """Convenience: pad to dim=4 and L2-normalize so cosine == dot product."""
    vec = list(components) + [0.0] * (4 - len(components))
    norm = math.sqrt(sum(c * c for c in vec))
    return [c / norm for c in vec] if norm > 0 else vec


# --- _normalize -------------------------------------------------------------


class TestNormalize:
    def test_normalizes_unit_norm(self) -> None:
        v = _normalize([3.0, 4.0])
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0)

    def test_zero_vector_stays_zero(self) -> None:
        assert _normalize([0.0, 0.0]) == [0.0, 0.0]


# --- embed_text_for_entry ---------------------------------------------------


class TestEmbedTextForEntry:
    def test_joins_summary_name_tags(self) -> None:
        entry = IndexEntry(
            slug="x",
            name="Quick Task",
            summary="Lightweight tasks for users.",
            key_paths=[],
            tags=["tasks", "productivity"],
        )
        text = embed_text_for_entry(entry)
        assert "Lightweight tasks" in text
        assert "Quick Task" in text
        assert "productivity" in text


# --- Embedder ---------------------------------------------------------------


class TestEmbedderDisabled:
    def test_no_api_key_means_disabled(self) -> None:
        emb = Embedder(api_key=None, dim=8)
        assert not emb.is_enabled()

    def test_disabled_returns_zero_vectors(self) -> None:
        emb = Embedder(api_key=None, dim=8)
        vectors = emb.embed(["a", "b"])
        assert vectors == [[0.0] * 8, [0.0] * 8]

    def test_disabled_embed_one(self) -> None:
        emb = Embedder(api_key=None, dim=4)
        assert emb.embed_one("x") == [0.0, 0.0, 0.0, 0.0]

    def test_embed_empty_list(self) -> None:
        emb = Embedder(api_key=None, dim=8)
        assert emb.embed([]) == []


# --- FAISSIndex -------------------------------------------------------------


class TestFAISSIndexAddSearch:
    def test_add_and_search_finds_closest(self) -> None:
        idx = FAISSIndex(dim=4)
        idx.add("apple", _unit(1.0, 0.0))
        idx.add("orange", _unit(0.0, 1.0))
        results = idx.search(_unit(0.95, 0.1), k=2)
        assert results[0][0] == "apple"
        assert results[0][1] > results[1][1]

    def test_add_replaces_on_same_slug(self) -> None:
        idx = FAISSIndex(dim=4)
        idx.add("x", _unit(1.0, 0.0))
        idx.add("x", _unit(0.0, 1.0))
        assert idx.size() == 1

    def test_remove(self) -> None:
        idx = FAISSIndex(dim=4)
        idx.add("a", _unit(1.0, 0.0))
        idx.add("b", _unit(0.0, 1.0))
        idx.remove("a")
        assert idx.size() == 1
        results = idx.search(_unit(1.0, 0.0), k=5)
        assert [slug for slug, _ in results] == ["b"]

    def test_search_empty_returns_empty(self) -> None:
        idx = FAISSIndex(dim=4)
        assert idx.search(_unit(1.0, 0.0), k=5) == []

    def test_search_zero_query_returns_empty(self) -> None:
        idx = FAISSIndex(dim=4)
        idx.add("a", _unit(1.0, 0.0))
        assert idx.search([0.0, 0.0, 0.0, 0.0], k=5) == []

    def test_dim_mismatch_raises(self) -> None:
        idx = FAISSIndex(dim=4)
        with pytest.raises(ValueError):
            idx.add("x", [1.0, 0.0])
        with pytest.raises(ValueError):
            idx.search([1.0, 0.0], k=5)


class TestFAISSIndexPersistence:
    def test_dump_load_round_trip(self) -> None:
        idx = FAISSIndex(dim=4)
        idx.add("a", _unit(1.0, 0.0))
        idx.add("b", _unit(0.0, 1.0))
        idx.add("c", _unit(0.5, 0.5))

        jsonl = idx.dump_jsonl()
        assert jsonl.count("\n") == 3

        round_tripped = FAISSIndex.from_jsonl(jsonl, dim=4)
        assert round_tripped.size() == 3

        # Search results should agree on ranking after round-trip.
        original_top = idx.search(_unit(1.0, 0.0), k=1)[0][0]
        new_top = round_tripped.search(_unit(1.0, 0.0), k=1)[0][0]
        assert original_top == new_top == "a"

    def test_empty_dump(self) -> None:
        idx = FAISSIndex(dim=4)
        assert idx.dump_jsonl() == ""

    def test_from_jsonl_empty(self) -> None:
        idx = FAISSIndex.from_jsonl("", dim=4)
        assert idx.size() == 0

    def test_from_jsonl_skips_malformed(self) -> None:
        idx = FAISSIndex.from_jsonl(
            '{"slug": "a", "vector": [1.0, 0.0, 0.0, 0.0]}\n'
            "not json\n"
            '{"slug": "b", "vector": [0.0, 1.0, 0.0, 0.0]}\n',
            dim=4,
        )
        assert idx.size() == 2
