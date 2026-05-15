"""Tests for MetadataStore — triples, contradiction detection, diaries, taxonomy."""

import pytest
import os
import tempfile

from localmem.config import LocalmemConfig, StorageConfig
from localmem.metadata_store import MetadataStore
from localmem.models import DiaryEntry, Triple


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


# --- Triple Tests ---


class TestTriples:
    async def test_add_and_query(self, store):
        t = Triple(
            subject="observer",
            predicate="primary_model",
            object="claude-sonnet-4",
            source_agent="router",
        )
        result = await store.add_triple(t)
        assert result is None  # No contradiction

        triples = await store.query_triples(subject="observer")
        assert len(triples) == 1
        assert triples[0].object == "claude-sonnet-4"

    async def test_contradiction_detection(self, store):
        t1 = Triple(
            subject="observer",
            predicate="primary_model",
            object="claude-sonnet-4",
            source_agent="router",
        )
        await store.add_triple(t1)

        t2 = Triple(
            subject="observer",
            predicate="primary_model",
            object="gpt-4o",
            source_agent="router",
        )
        contradiction = await store.add_triple(t2)

        assert contradiction is not None
        assert contradiction.old_value == "claude-sonnet-4"
        assert contradiction.new_value == "gpt-4o"
        assert contradiction.subject == "observer"
        assert contradiction.predicate == "primary_model"

    async def test_contradiction_supersedes_old(self, store):
        t1 = Triple(
            subject="tools",
            predicate="status",
            object="active",
            source_agent="router",
        )
        await store.add_triple(t1)

        t2 = Triple(
            subject="tools",
            predicate="status",
            object="degraded",
            source_agent="router",
        )
        await store.add_triple(t2)

        # Only the new triple should be active
        active = await store.query_triples(subject="tools", active_only=True)
        assert len(active) == 1
        assert active[0].object == "degraded"

        # Both should appear when including inactive
        all_triples = await store.query_triples(subject="tools", active_only=False)
        assert len(all_triples) == 2

    async def test_no_contradiction_same_value(self, store):
        t1 = Triple(
            subject="router",
            predicate="provider",
            object="anthropic",
            source_agent="router",
        )
        await store.add_triple(t1)

        t2 = Triple(
            subject="router",
            predicate="provider",
            object="anthropic",
            source_agent="router",
        )
        result = await store.add_triple(t2)
        assert result is None  # Same value, no contradiction

    async def test_timeline(self, store):
        for val in ["v1", "v2", "v3"]:
            t = Triple(
                subject="observer",
                predicate="version",
                object=val,
                source_agent="observer",
            )
            await store.add_triple(t)

        timeline = await store.triple_timeline("observer", "version")
        assert len(timeline) == 3
        objects = [t.object for t in timeline]
        assert objects == ["v1", "v2", "v3"]

    async def test_query_by_predicate(self, store):
        await store.add_triple(
            Triple(subject="router", predicate="status", object="healthy", source_agent="router")
        )
        await store.add_triple(
            Triple(subject="observer", predicate="status", object="active", source_agent="observer")
        )
        await store.add_triple(
            Triple(subject="router", predicate="latency", object="245ms", source_agent="router")
        )

        status_triples = await store.query_triples(predicate="status")
        assert len(status_triples) == 2


# --- Diary Tests ---


class TestDiary:
    async def test_write_and_read(self, store):
        entry = DiaryEntry(
            agent_id="observer",
            content="Detector 07 flagged coherence anomaly during ethical dilemma.",
            mood="concerned",
            tags=["coherence", "ethical-dilemma"],
            references=["obs:uuid-1"],
        )
        diary_id = await store.write_diary(entry)
        assert diary_id == entry.id

        entries = await store.read_diary(agent_id="observer")
        assert len(entries) == 1
        assert entries[0].content == entry.content
        assert entries[0].mood == "concerned"
        assert "coherence" in entries[0].tags

    async def test_read_all_agents(self, store):
        for agent in ["router", "tools", "observer"]:
            await store.write_diary(
                DiaryEntry(agent_id=agent, content=f"{agent} diary entry")
            )

        all_entries = await store.read_diary()
        assert len(all_entries) == 3

    async def test_read_with_limit(self, store):
        for i in range(10):
            await store.write_diary(
                DiaryEntry(agent_id="observer", content=f"Entry {i}")
            )

        entries = await store.read_diary(agent_id="observer", limit=5)
        assert len(entries) == 5


# --- Taxonomy Tests ---


class TestTaxonomy:
    async def test_register_and_list_wings(self, store):
        await store.register_room("router", "routing")
        await store.register_room("observer", "observations")
        await store.register_room("tools", "chains")

        wings = await store.list_wings()
        assert set(wings) == {"router", "observer", "tools"}

    async def test_register_and_list_rooms(self, store):
        await store.register_room("observer", "observations")
        await store.register_room("observer", "patterns")
        await store.register_room("observer", "correlations")

        rooms = await store.list_rooms(wing="observer")
        assert len(rooms) == 3
        room_names = {r["room"] for r in rooms}
        assert room_names == {"observations", "patterns", "correlations"}

    async def test_entry_count_increments(self, store):
        await store.register_room("router", "routing")
        await store.register_room("router", "routing")
        await store.register_room("router", "routing")

        rooms = await store.list_rooms(wing="router")
        assert rooms[0]["entry_count"] == 2  # First call creates (0), next two increment


# --- Importance Tests ---


class TestImportance:
    async def test_update_and_get_top(self, store):
        await store.update_importance("entry-1", base_score=0.9)
        await store.update_importance("entry-2", base_score=0.3)
        await store.update_importance("entry-3", base_score=0.7)

        top = await store.get_top_entries(limit=2)
        assert len(top) == 2
        assert top[0]["entry_id"] == "entry-1"  # Highest score first

    async def test_access_count_increments(self, store):
        await store.update_importance("entry-1", base_score=0.5)
        await store.update_importance("entry-1")  # Access without score change
        await store.update_importance("entry-1")

        top = await store.get_top_entries(limit=1)
        assert top[0]["access_count"] == 3


# --- Migration Tests ---


class TestMigration:
    async def test_schema_version_recorded(self, store):
        import aiosqlite
        async with aiosqlite.connect(store._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT MAX(version) AS v FROM schema_version")
            row = await cursor.fetchone()
            assert row["v"] >= 1

    async def test_pinned_column_present(self, store):
        import aiosqlite
        async with aiosqlite.connect(store._db_path) as db:
            cursor = await db.execute("PRAGMA table_info(importance)")
            cols = [r[1] for r in await cursor.fetchall()]
            assert "pinned" in cols

    async def test_consolidated_sources_table_present(self, store):
        import aiosqlite
        async with aiosqlite.connect(store._db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='consolidated_sources'"
            )
            assert await cursor.fetchone() is not None

    async def test_migration_idempotent(self, store, tmp_path):
        from localmem.config import LocalmemConfig, StorageConfig
        from localmem.metadata_store import MetadataStore

        cfg = LocalmemConfig(
            storage=StorageConfig(
                base_path=str(tmp_path),
                sqlite_path=str(tmp_path / "test.db"),
                qdrant_path=str(tmp_path / "qdrant"),
                graph_path=str(tmp_path / "graph.json"),
            )
        )
        # Re-initialize same DB — should not error or duplicate version rows.
        s2 = MetadataStore(cfg)
        await s2.initialize()
        s3 = MetadataStore(cfg)
        await s3.initialize()

        import aiosqlite
        async with aiosqlite.connect(cfg.storage.sqlite_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 1")
            row = await cursor.fetchone()
            # Migration only logs once even after multiple init calls.
            assert row[0] == 1


# --- Pinning Tests ---


class TestPinning:
    async def test_set_and_check(self, store):
        await store.set_pinned("e1", True, wing="router")
        assert await store.is_pinned("e1") is True

        await store.set_pinned("e1", False)
        assert await store.is_pinned("e1") is False

    async def test_unknown_entry_not_pinned(self, store):
        assert await store.is_pinned("does-not-exist") is False

    async def test_list_pinned_filters_wing(self, store):
        await store.set_pinned("e-router", True, wing="router")
        await store.set_pinned("e-observer", True, wing="observer")
        await store.set_pinned("e-tools", False, wing="tools")

        all_pinned = await store.list_pinned()
        assert {p["entry_id"] for p in all_pinned} == {"e-router", "e-observer"}

        router_only = await store.list_pinned(wing="router")
        assert [p["entry_id"] for p in router_only] == ["e-router"]

    async def test_pin_preserves_importance(self, store):
        await store.update_importance("e1", base_score=0.8, wing="observer")
        await store.set_pinned("e1", True)

        top = await store.get_top_entries(wing="observer", limit=1)
        assert top[0]["entry_id"] == "e1"
        assert top[0]["score"] == 0.8
        assert top[0]["pinned"] is True


# --- Consolidated Sources Tests ---


class TestConsolidatedSources:
    async def test_add_and_get(self, store):
        sources = [
            {"source_id": "src-1", "source_text_hash": "h1", "source_importance": 0.4, "source_wing": "router"},
            {"source_id": "src-2", "source_text_hash": "h2", "source_importance": 0.3, "source_wing": "router"},
        ]
        n = await store.add_consolidated_sources("sum-1", sources)
        assert n == 2

        rows = await store.get_consolidated_sources("sum-1")
        ids = {r["source_id"] for r in rows}
        assert ids == {"src-1", "src-2"}

    async def test_lookup_summary_for_source(self, store):
        await store.add_consolidated_sources("sum-1", [{"source_id": "src-a"}])
        assert await store.get_summary_for_source("src-a") == "sum-1"
        assert await store.get_summary_for_source("not-there") is None

    async def test_empty_sources_no_op(self, store):
        n = await store.add_consolidated_sources("sum-x", [])
        assert n == 0
        assert await store.get_consolidated_sources("sum-x") == []
