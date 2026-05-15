"""Tests for health check and metrics MCP tools."""

import asyncio
import pytest

from conftest import FakeEmbedder

from localmem.config import (
    LocalmemConfig,
    StorageConfig,
    EmbeddingConfig,
    GraphConfig,
)
from localmem.graph_store import GraphStore
from localmem.metadata_store import MetadataStore
from localmem.metrics import MetricsCollector
from localmem.models import Entry, EntryType
from localmem.vector_store import VectorStore

import localmem.mcp_server as server


@pytest.fixture
async def health_server(tmp_path):
    cfg = LocalmemConfig(
        wings=["router", "tools", "observer"],
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

    vs = VectorStore(cfg, embedder)
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, SparseVectorParams
    from localmem.vector_store import COLLECTION
    from pathlib import Path

    path = Path(cfg.storage.qdrant_path)
    path.mkdir(parents=True, exist_ok=True)
    vs._client = QdrantClient(path=str(path))
    vs._client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=8, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )
    server.vector_store = vs
    server.write_locks = {w: asyncio.Lock() for w in ["router", "tools", "observer", "shared"]}

    yield

    await server.graph_store.shutdown()


class TestHealthTool:
    async def test_health_returns_healthy(self, health_server):
        result = await server.localmem_health()
        assert result["status"] == "healthy"
        assert result["uptime_seconds"] >= 0
        assert result["vector_store"]["status"] == "ok"
        assert result["metadata_store"]["status"] == "ok"
        assert result["graph_store"]["status"] == "ok"

    async def test_health_embedding_info(self, health_server):
        result = await server.localmem_health()
        emb = result["embedding"]
        assert emb["model"] == "test"
        assert emb["device"] == "cpu"
        assert emb["sparse"] is False

    async def test_health_counts_entries(self, health_server):
        # Store an entry in router wing
        entry = Entry(
            wing="router", room="test", agent_id="router",
            entry_type=EntryType.GENERIC, content="test entry",
            tags=[], importance=0.5,
        )
        await server.vector_store.store(entry)

        result = await server.localmem_health()
        assert result["vector_store"]["entries"]["router"] >= 1


class TestMetricsTool:
    async def test_metrics_returns_snapshot(self, health_server):
        result = await server.localmem_metrics()
        assert "uptime_seconds" in result
        assert "total_calls" in result
        assert "tools" in result

    async def test_metrics_tracks_calls(self, health_server):
        # Call health (which is tracked)
        await server.localmem_health()
        result = await server.localmem_metrics()
        # Both health and metrics calls are tracked
        assert result["total_calls"] >= 1
        assert "localmem_health" in result["tools"]

    async def test_metrics_latency_present(self, health_server):
        await server.localmem_health()
        result = await server.localmem_metrics()
        health_metrics = result["tools"]["localmem_health"]
        assert health_metrics["latency_ms"]["avg"] > 0

    async def test_metrics_after_many_calls(self, health_server):
        for _ in range(5):
            await server.localmem_health()
        result = await server.localmem_metrics()
        assert result["tools"]["localmem_health"]["calls"] >= 5
        assert result["tools"]["localmem_health"]["latency_ms"]["p95"] > 0

    async def test_metrics_tracks_store_tool(self, health_server):
        await server.localmem_store(
            wing="router", room="test", agent_id="router",
            content="metrics tracking test", importance=0.5,
        )
        result = await server.localmem_metrics()
        assert "localmem_store" in result["tools"]
        assert result["tools"]["localmem_store"]["calls"] == 1
        assert result["tools"]["localmem_store"]["errors"] == 0


class TestHealthEdgeCases:
    async def test_health_with_empty_stores(self, health_server):
        result = await server.localmem_health()
        assert result["status"] == "healthy"
        for wing in ("router", "tools", "observer", "shared"):
            assert result["vector_store"]["entries"][wing] == 0

    async def test_health_graph_stats_present(self, health_server):
        result = await server.localmem_health()
        gs = result["graph_store"]
        assert gs["status"] == "ok"
        assert "nodes" in gs
        assert "edges" in gs
