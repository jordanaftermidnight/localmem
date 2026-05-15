"""Tests for the LOCALMEM dashboard REST + WS API."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from conftest import FakeEmbedder

from localmem.api import app as api_app
from localmem.config import (
    DashboardConfig,
    EmbeddingConfig,
    GraphConfig,
    IntelligenceConfig,
    LocalmemConfig,
    StorageConfig,
)
from localmem.graph_store import GraphStore
from localmem.intelligence import IntelligenceEngine
from localmem.metadata_store import MetadataStore
from localmem.metrics import MetricsCollector
from localmem.models import DiaryEntry, Entry, EntryType, Triple
from localmem.vector_store import COLLECTION, VectorStore


def _build_cfg(tmp_path: Path, *, auth: bool = False) -> LocalmemConfig:
    return LocalmemConfig(
        wings=["router", "tools", "observer"],
        storage=StorageConfig(
            base_path=str(tmp_path),
            qdrant_path=str(tmp_path / "qdrant"),
            sqlite_path=str(tmp_path / "m.db"),
            graph_path=str(tmp_path / "graph.json"),
        ),
        embedding=EmbeddingConfig(model="test"),
        graph=GraphConfig(persistence_debounce_seconds=0),
        intelligence=IntelligenceConfig(alert_room="intelligence-alerts"),
        dashboard=DashboardConfig(
            enabled=True,
            auth_enabled=auth,
            api_key="secret" if auth else None,
            ws_push_interval_seconds=0.05,
        ),
    )


async def _wire_state(cfg: LocalmemConfig) -> tuple:
    """Initialize stores and attach to api_app.state."""
    embedder = FakeEmbedder()

    vs = VectorStore(cfg, embedder)
    # Reuse VectorStore's initialize pipeline
    from pathlib import Path as _P
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, SparseVectorParams, VectorParams

    path = _P(cfg.storage.qdrant_path)
    path.mkdir(parents=True, exist_ok=True)
    vs._client = QdrantClient(path=str(path))
    vs._client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=8, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )

    ms = MetadataStore(cfg)
    await ms.initialize()
    gs = GraphStore(cfg)
    await gs.initialize()
    intel = IntelligenceEngine(cfg, vs, ms, gs)
    metrics = MetricsCollector()

    api_app.attach_stores(
        cfg,
        embedder=embedder,
        vector_store=vs,
        metadata_store=ms,
        graph_store=gs,
        intelligence_engine=intel,
        metrics=metrics,
        start_time=time.time(),
    )
    return vs, ms, gs, intel, metrics


@pytest.fixture
async def client(tmp_path):
    cfg = _build_cfg(tmp_path)
    vs, ms, gs, intel, metrics = await _wire_state(cfg)

    app = api_app.create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Lifespan isn't triggered automatically — manually start WS manager
        from localmem.api.websocket import ConnectionManager
        api_app.state.ws_manager = ConnectionManager()
        yield c, cfg, vs, ms, gs
    await gs.shutdown()


@pytest.fixture
async def auth_client(tmp_path):
    cfg = _build_cfg(tmp_path, auth=True)
    vs, ms, gs, intel, metrics = await _wire_state(cfg)
    app = api_app.create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        from localmem.api.websocket import ConnectionManager
        api_app.state.ws_manager = ConnectionManager()
        yield c, cfg, vs, ms, gs
    await gs.shutdown()


class TestHealth:
    async def test_health_healthy(self, client):
        c, *_ = client
        r = await c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "healthy"
        assert body["vector_store"]["status"] == "ok"
        assert body["embedding"]["model"] == "test"
        assert body["embedding"]["device"] == "cpu"
        for w in ("router", "tools", "observer", "shared"):
            assert w in body["vector_store"]["entries"]

    async def test_health_counts_entries(self, client):
        c, cfg, vs, *_ = client
        await vs.store(
            Entry(
                wing="router", room="test", agent_id="router",
                entry_type=EntryType.GENERIC, content="hi", importance=0.5,
            )
        )
        r = await c.get("/api/health")
        assert r.json()["vector_store"]["entries"]["router"] == 1


class TestMetrics:
    async def test_metrics_empty(self, client):
        c, *_ = client
        r = await c.get("/api/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total_calls"] == 0
        assert body["tools"] == {}

    async def test_metrics_after_track(self, client):
        c, *_ = client
        async with api_app.state.metrics.track("fake_tool"):
            pass
        r = await c.get("/api/metrics")
        body = r.json()
        assert body["total_calls"] == 1
        assert "fake_tool" in body["tools"]
        assert body["tools"]["fake_tool"]["calls"] == 1


class TestTaxonomy:
    async def test_empty(self, client):
        c, *_ = client
        r = await c.get("/api/taxonomy")
        assert r.status_code == 200
        assert r.json()["wings"] == []

    async def test_populated(self, client):
        c, cfg, vs, ms, _gs = client
        await ms.register_room("router", "routing")
        await ms.register_room("tools", "reasoning")
        r = await c.get("/api/taxonomy")
        body = r.json()
        assert set(body["wings"]) == {"router", "tools"}
        assert len(body["rooms"]) == 2


class TestEntries:
    async def test_list_empty(self, client):
        c, *_ = client
        r = await c.get("/api/entries")
        body = r.json()
        assert body["entries"] == []
        assert body["total"] == 0

    async def test_list_paginated(self, client):
        c, cfg, vs, ms, _gs = client
        for i in range(5):
            await vs.store(
                Entry(
                    wing="router", room="test", agent_id="router",
                    entry_type=EntryType.GENERIC,
                    content=f"entry {i}", importance=0.5,
                )
            )
        r = await c.get("/api/entries?wing=router&limit=3")
        body = r.json()
        assert len(body["entries"]) == 3
        assert body["total"] == 5

    async def test_get_detail(self, client):
        c, cfg, vs, *_ = client
        eid = await vs.store(
            Entry(
                wing="router", room="test", agent_id="router",
                entry_type=EntryType.GENERIC,
                content="full content here", importance=0.7,
                tags=["a", "b"],
            )
        )
        r = await c.get(f"/api/entries/{eid}")
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "full content here"
        assert body["tags"] == ["a", "b"]

    async def test_get_missing(self, client):
        c, *_ = client
        r = await c.get("/api/entries/nonexistent")
        assert r.status_code == 404


class TestSearch:
    async def test_search_returns_hits(self, client):
        c, cfg, vs, *_ = client
        await vs.store(
            Entry(
                wing="router", room="test", agent_id="router",
                entry_type=EntryType.GENERIC, content="unique phrase alpha",
                importance=0.5,
            )
        )
        r = await c.post(
            "/api/search",
            json={"query": "unique phrase alpha", "wing": "router", "limit": 5},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["hits"]) >= 1
        assert body["query"] == "unique phrase alpha"


class TestGraph:
    async def test_stats(self, client):
        c, *_ = client
        r = await c.get("/api/graph/stats")
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body
        assert "edges" in body

    async def test_subgraph_empty(self, client):
        c, *_ = client
        r = await c.get("/api/graph/subgraph")
        assert r.status_code == 200
        body = r.json()
        assert body["nodes"] == []
        assert body["edges"] == []

    async def test_subgraph_with_nodes(self, client):
        c, cfg, vs, ms, gs = client
        await gs.add_node("a", {"type": "test"})
        await gs.add_node("b", {"type": "test"})
        await gs.add_edge("a", "b", {"rel": "test"})
        r = await c.get("/api/graph/subgraph?node=a&depth=2")
        body = r.json()
        assert len(body["nodes"]) == 2
        assert len(body["edges"]) == 1


class TestTriples:
    async def test_empty(self, client):
        c, *_ = client
        r = await c.get("/api/triples")
        assert r.json() == []

    async def test_query(self, client):
        c, cfg, vs, ms, _gs = client
        await ms.add_triple(
            Triple(subject="router", predicate="uses", object="anthropic",
                   source_agent="test")
        )
        r = await c.get("/api/triples?subject=router")
        body = r.json()
        assert len(body) == 1
        assert body[0]["object"] == "anthropic"


class TestDiaries:
    async def test_empty(self, client):
        c, *_ = client
        r = await c.get("/api/diaries")
        assert r.json() == []

    async def test_populated(self, client):
        c, cfg, vs, ms, _gs = client
        await ms.write_diary(
            DiaryEntry(agent_id="router", content="journal", mood="curious")
        )
        r = await c.get("/api/diaries?agent_id=router")
        body = r.json()
        assert len(body) == 1
        assert body[0]["mood"] == "curious"


class TestAlerts:
    async def test_empty(self, client):
        c, *_ = client
        r = await c.get("/api/alerts")
        assert r.json() == []

    async def test_populated(self, client):
        c, cfg, vs, ms, _gs = client
        await vs.store(
            Entry(
                wing="shared", room="intelligence-alerts",
                agent_id="intel", entry_type=EntryType.GENERIC,
                content="pattern detected", importance=0.9,
                tags=["router"],
            )
        )
        r = await c.get("/api/alerts")
        body = r.json()
        assert len(body) == 1
        # wing filter
        r2 = await c.get("/api/alerts?wing=router")
        assert len(r2.json()) == 1
        r3 = await c.get("/api/alerts?wing=observer")
        assert len(r3.json()) == 0


class TestAuth:
    async def test_401_without_token(self, auth_client):
        c, *_ = auth_client
        r = await c.get("/api/health")
        assert r.status_code == 401

    async def test_200_with_token(self, auth_client):
        c, *_ = auth_client
        r = await c.get("/api/health", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200

    async def test_401_with_wrong_token(self, auth_client):
        c, *_ = auth_client
        r = await c.get("/api/health", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    async def test_401_with_malformed_header(self, auth_client):
        c, *_ = auth_client
        r = await c.get("/api/health", headers={"Authorization": "secret"})
        assert r.status_code == 401

    async def test_500_if_auth_enabled_no_key(self, tmp_path):
        cfg = _build_cfg(tmp_path, auth=True)
        cfg.dashboard.api_key = None
        await _wire_state(cfg)
        app = api_app.create_app(cfg)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            from localmem.api.websocket import ConnectionManager
            api_app.state.ws_manager = ConnectionManager()
            r = await c.get(
                "/api/health", headers={"Authorization": "Bearer anything"}
            )
            assert r.status_code == 500
            assert "api_key is not configured" in r.json()["detail"]
        # cleanup
        from localmem.api.app import state
        await state.graph_store.shutdown()

    async def test_post_endpoints_require_auth(self, auth_client):
        c, *_ = auth_client
        r = await c.post("/api/search", json={"query": "x"})
        assert r.status_code == 401
