"""Tests for MCP server — tool invocations and lifecycle."""

from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import pytest

from conftest import FakeEmbedder

from localmem.config import LocalmemConfig, StorageConfig, EmbeddingConfig, GraphConfig
from localmem.embedder import Embedder
from localmem.graph_store import GraphStore
from localmem.metadata_store import MetadataStore
from localmem.metrics import MetricsCollector
from localmem.models import Entry, EntryType, SearchResult, WakeContext
from localmem.vector_store import VectorStore


# We test the tool functions directly (they're just async functions)
# rather than going through the MCP transport layer.
import localmem.mcp_server as server


@pytest.fixture
async def setup_server(tmp_path):
    """Initialize all stores with test config, then tear down."""
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            qdrant_path=str(tmp_path / "qdrant"),
            sqlite_path=str(tmp_path / "test.db"),
            graph_path=str(tmp_path / "graph.json"),
        ),
        embedding=EmbeddingConfig(model="test"),
        graph=GraphConfig(persistence_debounce_seconds=0),
    )

    embedder = FakeEmbedder()

    server.config = cfg
    server.embedder = embedder
    server.metrics_collector = MetricsCollector()
    server.server_start_time = __import__("time").time()
    server.metadata_store = MetadataStore(cfg)
    server.graph_store = GraphStore(cfg)

    await server.metadata_store.initialize()
    await server.graph_store.initialize()

    # Set up vector store with fake embedder
    vs = VectorStore(cfg, embedder)
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, SparseVectorParams
    from localmem.vector_store import COLLECTION
    from qdrant_client import models
    from pathlib import Path

    path = Path(cfg.storage.qdrant_path)
    path.mkdir(parents=True, exist_ok=True)
    vs._client = QdrantClient(path=str(path))
    vs._client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=8, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )
    for field in ["wing", "room", "agent_id", "entry_type"]:
        vs._client.create_payload_index(
            collection_name=COLLECTION,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    server.vector_store = vs

    # WakeUp needs manifests dir — create a test manifest
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "observer.yaml").write_text(
        "agent: observer\nrole: Behavioral analysis\ncapabilities:\n  - pattern detection\nwake_rooms:\n  - observer:observations\n"
    )
    from localmem.wake_up import WakeUp
    server.wake_up = WakeUp(cfg, vs, server.metadata_store, str(manifests_dir))

    # Initialize write locks
    server.write_locks = {}
    for wing in ["router", "tools", "observer", "shared"]:
        server.write_locks[wing] = asyncio.Lock()

    yield

    await server.graph_store.shutdown()


class TestMemoryOperations:
    async def test_store_and_retrieve(self, setup_server):
        result = await server.localmem_store(
            wing="observer",
            room="observations",
            agent_id="observer",
            content="Coherence score dropped to 0.42",
            entry_type="behavioral_observation",
            importance=0.8,
            tags=["coherence"],
        )
        assert result["status"] == "stored"
        entry_id = result["id"]

        retrieved = await server.localmem_retrieve(entry_id)
        assert retrieved["content"] == "Coherence score dropped to 0.42"
        assert retrieved["wing"] == "observer"
        assert retrieved["importance"] == 0.8

    async def test_retrieve_not_found(self, setup_server):
        result = await server.localmem_retrieve("nonexistent")
        assert "error" in result

    async def test_search(self, setup_server):
        await server.localmem_store(
            wing="observer",
            room="observations",
            agent_id="observer",
            content="Linguistic shift detected in ethical dilemma",
            tags=["linguistic"],
        )
        results = await server.localmem_search(
            query="linguistic shift",
            wing="observer",
            limit=5,
        )
        assert isinstance(results, list)

    async def test_update_metadata(self, setup_server):
        store_result = await server.localmem_store(
            wing="observer",
            room="observations",
            agent_id="observer",
            content="Entry to update",
        )
        entry_id = store_result["id"]

        update_result = await server.localmem_update(
            entry_id=entry_id,
            importance=0.95,
            tags=["updated", "important"],
            summary="Updated summary",
        )
        assert update_result["status"] == "updated"

        retrieved = await server.localmem_retrieve(entry_id)
        assert retrieved["importance"] == 0.95
        assert set(retrieved["tags"]) == {"updated", "important"}
        assert retrieved["summary"] == "Updated summary"

    async def test_update_not_found(self, setup_server):
        result = await server.localmem_update(entry_id="nonexistent", importance=0.5)
        assert "error" in result

    async def test_delete(self, setup_server):
        store_result = await server.localmem_store(
            wing="observer",
            room="observations",
            agent_id="observer",
            content="Entry to delete",
        )
        entry_id = store_result["id"]

        delete_result = await server.localmem_delete(entry_id)
        assert delete_result["status"] == "deleted"

        retrieve_result = await server.localmem_retrieve(entry_id)
        assert "error" in retrieve_result

    async def test_delete_not_found(self, setup_server):
        result = await server.localmem_delete("nonexistent")
        assert "error" in result


class TestGraphOperations:
    async def test_add_node_and_stats(self, setup_server):
        await server.localmem_graph_add(
            node_id="obs:1",
            node_attributes={"type": "observation", "detector": "coherence"},
        )
        stats = await server.localmem_graph_stats()
        assert stats["nodes"] == 1

    async def test_add_edge(self, setup_server):
        result = await server.localmem_graph_add(
            source="obs:1",
            target="obs:2",
            edge_attributes={"relation": "co_occurred_with"},
        )
        assert "edge_added" in result

        stats = await server.localmem_graph_stats()
        assert stats["nodes"] == 2
        assert stats["edges"] == 1

    async def test_graph_query_neighbors(self, setup_server):
        await server.localmem_graph_add(source="a", target="b")
        await server.localmem_graph_add(source="b", target="c")

        result = await server.localmem_graph_query(
            operation="neighbors", source_node="b", depth=1
        )
        assert "b" in result["neighbors"]

    async def test_graph_query_path(self, setup_server):
        await server.localmem_graph_add(source="a", target="b")
        await server.localmem_graph_add(source="b", target="c")

        result = await server.localmem_graph_query(
            operation="path", source_node="a", target_node="c"
        )
        assert result["path"] == ["a", "b", "c"]
        assert result["length"] == 2

    async def test_graph_patterns(self, setup_server):
        await server.localmem_graph_add(
            node_id="pattern:1",
            node_attributes={"type": "pattern", "name": "coherence-shift", "frequency": 10},
        )
        patterns = await server.localmem_graph_patterns(min_frequency=2)
        assert len(patterns) == 1
        assert patterns[0]["name"] == "coherence-shift"

    async def test_graph_no_args_returns_error(self, setup_server):
        result = await server.localmem_graph_add()
        assert "error" in result


class TestKnowledgeOperations:
    async def test_add_triple(self, setup_server):
        result = await server.localmem_kg_add(
            subject="observer",
            predicate="primary_model",
            object="claude-sonnet-4",
            source_agent="router",
        )
        assert result["status"] == "added"
        assert "triple_id" in result

    async def test_contradiction_detected(self, setup_server):
        await server.localmem_kg_add(
            subject="observer",
            predicate="primary_model",
            object="claude-sonnet-4",
            source_agent="router",
        )
        result = await server.localmem_kg_add(
            subject="observer",
            predicate="primary_model",
            object="gpt-4o",
            source_agent="router",
        )
        assert "contradiction" in result
        assert result["contradiction"]["old_value"] == "claude-sonnet-4"
        assert result["contradiction"]["new_value"] == "gpt-4o"

    async def test_query_triples(self, setup_server):
        await server.localmem_kg_add(
            subject="router", predicate="status", object="healthy", source_agent="router"
        )
        results = await server.localmem_kg_query(subject="router")
        assert len(results) == 1
        assert results[0]["object"] == "healthy"

    async def test_timeline(self, setup_server):
        for val in ["v1", "v2", "v3"]:
            await server.localmem_kg_add(
                subject="observer", predicate="version", object=val, source_agent="observer"
            )
        timeline = await server.localmem_kg_timeline(subject="observer", predicate="version")
        assert len(timeline) == 3
        assert [t["object"] for t in timeline] == ["v1", "v2", "v3"]


class TestSystemOperations:
    async def test_diary_write_and_read(self, setup_server):
        write_result = await server.localmem_diary_write(
            agent_id="observer",
            content="Detector 07 flagged coherence anomaly",
            mood="concerned",
            tags=["coherence", "anomaly"],
        )
        assert write_result["status"] == "written"

        entries = await server.localmem_diary_read(agent_id="observer")
        assert len(entries) == 1
        assert entries[0]["content"] == "Detector 07 flagged coherence anomaly"
        assert entries[0]["mood"] == "concerned"

    async def test_diary_cross_agent_read(self, setup_server):
        await server.localmem_diary_write(
            agent_id="observer", content="AgentObserver diary"
        )
        await server.localmem_diary_write(
            agent_id="router", content="AgentRouter diary"
        )

        all_entries = await server.localmem_diary_read()
        assert len(all_entries) == 2

    async def test_wake(self, setup_server):
        # Store something first so L1 has data
        await server.localmem_store(
            wing="observer",
            room="observations",
            agent_id="observer",
            content="Wake test entry",
            importance=0.9,
        )

        result = await server.localmem_wake(agent_id="observer")
        assert result["agent_id"] == "observer"
        assert "manifest" in result
        assert result["manifest"]["agent"] == "observer"
        assert "critical_context" in result
        assert "token_estimate" in result


class TestConcurrency:
    async def test_concurrent_stores_to_different_wings(self, setup_server):
        """Stores to different wings should not block each other."""
        results = await asyncio.gather(
            server.localmem_store(
                wing="observer", room="obs", agent_id="observer", content="observer entry"
            ),
            server.localmem_store(
                wing="router", room="routing", agent_id="router", content="router entry"
            ),
            server.localmem_store(
                wing="tools", room="chains", agent_id="tools", content="tools entry"
            ),
        )
        assert all(r["status"] == "stored" for r in results)

    async def test_concurrent_stores_to_same_wing(self, setup_server):
        """Stores to same wing should serialize safely."""
        results = await asyncio.gather(*[
            server.localmem_store(
                wing="observer", room="obs", agent_id="observer", content=f"entry {i}"
            )
            for i in range(5)
        ])
        assert all(r["status"] == "stored" for r in results)
