"""Tests for GraphStore — behavioral pattern graph reasoning."""

import pytest

from localmem.config import LocalmemConfig, StorageConfig, GraphConfig
from localmem.graph_store import GraphStore
from localmem.models import GraphQuery


@pytest.fixture
async def store(tmp_path):
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            graph_path=str(tmp_path / "graph.json"),
            sqlite_path=str(tmp_path / "test.db"),
            qdrant_path=str(tmp_path / "qdrant"),
        ),
        graph=GraphConfig(persistence_debounce_seconds=0.1),
    )
    s = GraphStore(cfg)
    await s.initialize()
    yield s
    await s.shutdown()


class TestNodeEdgeOperations:
    async def test_add_node(self, store):
        await store.add_node("obs:1", {"type": "observation", "detector": "coherence"})
        stats = await store.stats()
        assert stats["nodes"] == 1

    async def test_add_edge(self, store):
        await store.add_node("obs:1", {"type": "observation"})
        await store.add_node("obs:2", {"type": "observation"})
        await store.add_edge("obs:1", "obs:2", {"relation": "co_occurred_with"})

        stats = await store.stats()
        assert stats["nodes"] == 2
        assert stats["edges"] == 1

    async def test_remove_node(self, store):
        await store.add_node("obs:1")
        assert await store.remove_node("obs:1") is True
        assert await store.remove_node("obs:nonexistent") is False

        stats = await store.stats()
        assert stats["nodes"] == 0

    async def test_edge_auto_creates_nodes(self, store):
        await store.add_edge("obs:1", "obs:2", {"relation": "test"})
        stats = await store.stats()
        assert stats["nodes"] == 2  # mcp_server.localmem_graph_add ensures this


class TestGraphQueries:
    async def _build_chain(self, store):
        """Build: obs:1 -> obs:2 -> obs:3 -> pattern:1"""
        for i in range(1, 4):
            await store.add_node(
                f"obs:{i}",
                {"type": "observation", "detector": f"det-{i}", "timestamp": f"2026-04-0{i}T12:00:00Z"},
            )
        await store.add_node(
            "pattern:1",
            {"type": "pattern", "name": "coherence-shift", "frequency": 5, "first_seen": "2026-04-01T00:00:00Z"},
        )
        await store.add_edge("obs:1", "obs:2", {"relation": "co_occurred_with", "time_delta_seconds": 30})
        await store.add_edge("obs:2", "obs:3", {"relation": "preceded_by", "time_delta_seconds": 60})
        await store.add_edge("obs:3", "pattern:1", {"relation": "instance_of"})

    async def test_shortest_path(self, store):
        await self._build_chain(store)

        result = await store.query(
            GraphQuery(operation="path", source_node="obs:1", target_node="pattern:1")
        )
        assert result["path"] == ["obs:1", "obs:2", "obs:3", "pattern:1"]
        assert result["length"] == 3
        assert len(result["edges"]) == 3

    async def test_path_not_found(self, store):
        await store.add_node("obs:isolated")
        await self._build_chain(store)

        result = await store.query(
            GraphQuery(operation="path", source_node="obs:isolated", target_node="pattern:1")
        )
        assert result["path"] is None

    async def test_neighbors(self, store):
        await self._build_chain(store)

        result = await store.query(
            GraphQuery(operation="neighbors", source_node="obs:2", depth=1)
        )
        assert "obs:2" in result["neighbors"]
        # Depth 1 should include direct neighbors
        assert len(result["neighbors"]) >= 2

    async def test_neighbors_depth_2(self, store):
        await self._build_chain(store)

        result = await store.query(
            GraphQuery(operation="neighbors", source_node="obs:1", depth=2)
        )
        assert "obs:3" in result["neighbors"]

    async def test_centrality(self, store):
        await self._build_chain(store)

        result = await store.query(GraphQuery(operation="centrality"))
        assert "centrality" in result
        assert len(result["centrality"]) > 0

    async def test_community_detection(self, store):
        # Build two disconnected clusters
        await store.add_edge("a:1", "a:2")
        await store.add_edge("a:2", "a:3")
        await store.add_edge("b:1", "b:2")
        await store.add_edge("b:2", "b:3")

        result = await store.query(GraphQuery(operation="community"))
        assert result["count"] >= 2  # At least two communities

    async def test_temporal_subgraph(self, store):
        await self._build_chain(store)

        result = await store.query(
            GraphQuery(
                operation="temporal",
                start_time="2026-04-01T00:00:00Z",
                end_time="2026-04-02T23:59:59Z",
            )
        )
        # obs:1 and obs:2 fall in the time window
        assert result["nodes"] == 2

    async def test_unknown_operation(self, store):
        result = await store.query(GraphQuery(operation="nonexistent"))
        assert "error" in result

    async def test_node_not_found(self, store):
        result = await store.query(
            GraphQuery(operation="neighbors", source_node="nonexistent")
        )
        assert "error" in result


class TestPatterns:
    async def test_get_patterns(self, store):
        await store.add_node(
            "pattern:1",
            {"type": "pattern", "name": "coherence-shift", "frequency": 12},
        )
        await store.add_node(
            "pattern:2",
            {"type": "pattern", "name": "rare-event", "frequency": 1},
        )
        await store.add_node(
            "pattern:3",
            {"type": "pattern", "name": "linguistic-drift", "frequency": 7},
        )

        patterns = await store.get_patterns(min_frequency=2)
        assert len(patterns) == 2
        assert patterns[0]["name"] == "coherence-shift"  # Sorted by frequency desc
        assert patterns[1]["name"] == "linguistic-drift"

    async def test_empty_graph_patterns(self, store):
        patterns = await store.get_patterns()
        assert patterns == []


class TestPersistence:
    async def test_persist_and_reload(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                base_path=str(tmp_path),
                graph_path=str(tmp_path / "graph.json"),
                sqlite_path=str(tmp_path / "test.db"),
                qdrant_path=str(tmp_path / "qdrant"),
            ),
            graph=GraphConfig(persistence_debounce_seconds=0),
        )

        # Write
        store1 = GraphStore(cfg)
        await store1.initialize()
        await store1.add_node("obs:1", {"type": "observation"})
        await store1.add_node("obs:2", {"type": "observation"})
        await store1.add_edge("obs:1", "obs:2", {"relation": "test"})
        await store1.shutdown()

        # Reload
        store2 = GraphStore(cfg)
        await store2.initialize()
        stats = await store2.stats()
        assert stats["nodes"] == 2
        assert stats["edges"] == 1
        await store2.shutdown()


class TestStats:
    async def test_empty_stats(self, store):
        stats = await store.stats()
        assert stats["nodes"] == 0
        assert stats["edges"] == 0
        assert stats["density"] == 0.0
        assert stats["weakly_connected_components"] == 0

    async def test_populated_stats(self, store):
        await store.add_edge("a", "b")
        await store.add_edge("b", "c")
        await store.add_edge("c", "a")

        stats = await store.stats()
        assert stats["nodes"] == 3
        assert stats["edges"] == 3
        assert stats["weakly_connected_components"] == 1
        assert stats["density"] > 0
