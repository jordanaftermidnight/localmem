"""Tests for contradiction detection — the most critical logic path in LOCALMEM.

Contradiction detection ensures that when a new triple conflicts with an active one,
the old triple is superseded and a ContradictionEvent is returned to the calling agent.
This prevents silent accumulation of conflicting facts (MemPalace's known gap).
"""

import pytest

from localmem.config import LocalmemConfig, StorageConfig
from localmem.metadata_store import MetadataStore
from localmem.models import Triple


@pytest.fixture
async def store(tmp_path):
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            sqlite_path=str(tmp_path / "test.db"),
            qdrant_path=str(tmp_path / "qdrant"),
            graph_path=str(tmp_path / "graph.json"),
        )
    )
    s = MetadataStore(cfg)
    await s.initialize()
    return s


class TestContradictionChains:
    """Test multi-step contradiction chains: A -> B -> C."""

    async def test_three_step_chain(self, store):
        t1 = Triple(subject="router", predicate="provider", object="anthropic", source_agent="router")
        t2 = Triple(subject="router", predicate="provider", object="openai", source_agent="router")
        t3 = Triple(subject="router", predicate="provider", object="google", source_agent="router")

        r1 = await store.add_triple(t1)
        assert r1 is None  # First insert, no contradiction

        r2 = await store.add_triple(t2)
        assert r2 is not None
        assert r2.old_value == "anthropic"
        assert r2.new_value == "openai"

        r3 = await store.add_triple(t3)
        assert r3 is not None
        assert r3.old_value == "openai"
        assert r3.new_value == "google"

        # Timeline should show all three
        timeline = await store.triple_timeline("router", "provider")
        assert len(timeline) == 3
        assert [t.object for t in timeline] == ["anthropic", "openai", "google"]

        # Only the latest should be active
        active = await store.query_triples(subject="router", predicate="provider", active_only=True)
        assert len(active) == 1
        assert active[0].object == "google"

    async def test_superseded_by_chain(self, store):
        """Verify the superseded_by foreign key chain is correct."""
        t1 = Triple(subject="x", predicate="state", object="a", source_agent="test")
        t2 = Triple(subject="x", predicate="state", object="b", source_agent="test")
        t3 = Triple(subject="x", predicate="state", object="c", source_agent="test")

        await store.add_triple(t1)
        await store.add_triple(t2)
        await store.add_triple(t3)

        all_triples = await store.query_triples(subject="x", active_only=False)
        # Sort by valid_from to get chronological order
        all_triples.sort(key=lambda t: t.valid_from)

        assert all_triples[0].superseded_by == t2.id
        assert all_triples[1].superseded_by == t3.id
        assert all_triples[2].superseded_by is None  # Latest, not superseded


class TestContradictionEdgeCases:
    async def test_same_value_no_contradiction(self, store):
        """Inserting the same value should not trigger contradiction."""
        t1 = Triple(subject="observer", predicate="mode", object="active", source_agent="observer")
        t2 = Triple(subject="observer", predicate="mode", object="active", source_agent="observer")

        await store.add_triple(t1)
        result = await store.add_triple(t2)
        assert result is None

    async def test_different_predicates_no_contradiction(self, store):
        """Different predicates on the same subject are independent."""
        t1 = Triple(subject="observer", predicate="mode", object="active", source_agent="observer")
        t2 = Triple(subject="observer", predicate="version", object="0.3", source_agent="observer")

        await store.add_triple(t1)
        result = await store.add_triple(t2)
        assert result is None

        active = await store.query_triples(subject="observer", active_only=True)
        assert len(active) == 2

    async def test_different_subjects_no_contradiction(self, store):
        """Same predicate on different subjects are independent."""
        t1 = Triple(subject="router", predicate="status", object="healthy", source_agent="router")
        t2 = Triple(subject="tools", predicate="status", object="degraded", source_agent="tools")

        await store.add_triple(t1)
        result = await store.add_triple(t2)
        assert result is None

    async def test_contradiction_valid_to_is_set(self, store):
        """Old triple's valid_to should be set when contradicted."""
        t1 = Triple(subject="observer", predicate="detector_count", object="16", source_agent="observer")
        t2 = Triple(subject="observer", predicate="detector_count", object="18", source_agent="observer")

        await store.add_triple(t1)
        await store.add_triple(t2)

        all_triples = await store.query_triples(
            subject="observer", predicate="detector_count", active_only=False
        )
        all_triples.sort(key=lambda t: t.valid_from)

        assert all_triples[0].valid_to is not None  # Old triple closed
        assert all_triples[1].valid_to is None  # New triple still active


class TestContradictionEventContent:
    async def test_event_has_all_fields(self, store):
        t1 = Triple(subject="tools", predicate="inference_model", object="claude-3-opus", source_agent="tools")
        t2 = Triple(subject="tools", predicate="inference_model", object="claude-sonnet-4", source_agent="tools")

        await store.add_triple(t1)
        event = await store.add_triple(t2)

        assert event is not None
        assert event.subject == "tools"
        assert event.predicate == "inference_model"
        assert event.old_value == "claude-3-opus"
        assert event.new_value == "claude-sonnet-4"
        assert event.old_triple.id == t1.id
        assert event.new_triple.id == t2.id
        assert event.timestamp is not None

    async def test_cross_wing_contradiction(self, store):
        """Agent B contradicting agent A's triple should still work."""
        t1 = Triple(subject="system", predicate="primary_provider", object="anthropic", source_agent="router")
        t2 = Triple(subject="system", predicate="primary_provider", object="openai", source_agent="tools")

        await store.add_triple(t1)
        event = await store.add_triple(t2)

        assert event is not None
        assert event.old_triple.source_agent == "router"
        assert event.new_triple.source_agent == "tools"
