"""Tests for the Prometheus text exposition + /metrics endpoint."""

from __future__ import annotations

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
from localmem.models import Entry, EntryType
from localmem.prometheus_exposition import (
    PROMETHEUS_CONTENT_TYPE,
    build_prometheus_text,
)
from localmem.vector_store import COLLECTION, VectorStore


def _build_cfg(tmp_path: Path) -> LocalmemConfig:
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
        dashboard=DashboardConfig(enabled=True, ws_push_interval_seconds=0.05),
    )


async def _wire_state(cfg: LocalmemConfig) -> tuple:
    embedder = FakeEmbedder()
    vs = VectorStore(cfg, embedder)
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, SparseVectorParams, VectorParams

    Path(cfg.storage.qdrant_path).mkdir(parents=True, exist_ok=True)
    vs._client = QdrantClient(path=cfg.storage.qdrant_path)
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
    return vs, ms, gs, metrics


@pytest.fixture
async def client(tmp_path):
    cfg = _build_cfg(tmp_path)
    vs, ms, gs, metrics = await _wire_state(cfg)
    app = api_app.create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        from localmem.api.websocket import ConnectionManager
        api_app.state.ws_manager = ConnectionManager()
        yield c, cfg, vs, ms, gs, metrics
    await gs.shutdown()


class TestBuildPrometheusText:
    def test_minimal_inputs(self):
        text = build_prometheus_text(metrics={"uptime_seconds": 12.3}, health={})
        assert "# HELP localmem_uptime_seconds" in text
        assert "# TYPE localmem_uptime_seconds gauge" in text
        assert "localmem_uptime_seconds 12.3" in text
        assert text.endswith("\n")

    def test_zero_calls_emits_default_line(self):
        text = build_prometheus_text(metrics={"uptime_seconds": 0}, health={})
        assert "localmem_tool_calls_total 0" in text
        assert "localmem_tool_errors_total 0" in text

    def test_per_tool_labels(self):
        metrics = {
            "uptime_seconds": 1,
            "tools": {
                "store": {
                    "calls": 5,
                    "errors": 1,
                    "latency_ms": {"avg": 10, "p50": 8, "p95": 12, "p99": 20},
                },
                "search": {
                    "calls": 3,
                    "errors": 0,
                    "latency_ms": {"avg": 30, "p50": 28, "p95": 40, "p99": 50},
                },
            },
        }
        text = build_prometheus_text(metrics=metrics, health={})
        assert 'localmem_tool_calls_total{tool="store"} 5' in text
        assert 'localmem_tool_calls_total{tool="search"} 3' in text
        assert 'localmem_tool_errors_total{tool="store"} 1' in text
        assert 'localmem_tool_latency_p50_milliseconds{tool="store"} 8' in text
        assert 'localmem_tool_latency_p95_milliseconds{tool="search"} 40' in text
        assert 'localmem_tool_latency_p99_milliseconds{tool="search"} 50' in text

    def test_per_wing_entries(self):
        health = {"vector_store": {"entries": {"router": 12, "observer": 7, "shared": 99}}}
        text = build_prometheus_text(metrics={"uptime_seconds": 0}, health=health)
        assert 'localmem_entries{wing="router"} 12' in text
        assert 'localmem_entries{wing="observer"} 7' in text
        assert 'localmem_entries{wing="shared"} 99' in text

    def test_graph_stats(self):
        health = {"graph_store": {"nodes": 50, "edges": 87}}
        text = build_prometheus_text(metrics={"uptime_seconds": 0}, health=health)
        assert "localmem_graph_nodes 50" in text
        assert "localmem_graph_edges 87" in text

    def test_worker_payload(self):
        text = build_prometheus_text(
            metrics={"uptime_seconds": 0},
            health={},
            worker={"running": True, "in_flight": False, "queue_size": 2},
        )
        assert "localmem_worker_running 1" in text
        assert "localmem_worker_in_flight 0" in text
        assert "localmem_worker_queue_size 2" in text

    def test_archive_payload(self):
        archive = {"wings": {
            "router": {"files": 4, "bytes": 1024},
            "tools": {"files": 2, "bytes": 512},
        }}
        text = build_prometheus_text(
            metrics={"uptime_seconds": 0}, health={}, archive=archive
        )
        assert 'localmem_archive_files{wing="router"} 4' in text
        assert 'localmem_archive_bytes{wing="tools"} 512' in text

    def test_label_escaping(self):
        metrics = {
            "uptime_seconds": 0,
            "tools": {
                'weird"name': {
                    "calls": 1, "errors": 0,
                    "latency_ms": {"avg": 0, "p50": 0, "p95": 0, "p99": 0},
                },
            },
        }
        text = build_prometheus_text(metrics=metrics, health={})
        assert 'tool="weird\\"name"' in text


class TestPrometheusEndpoint:
    async def test_200_text_plain(self, client):
        c, *_ = client
        r = await c.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert PROMETHEUS_CONTENT_TYPE.split(";")[0] in r.headers["content-type"]

    async def test_includes_uptime_and_zero_call_counts(self, client):
        c, *_ = client
        r = await c.get("/metrics")
        body = r.text
        assert "localmem_uptime_seconds" in body
        assert "localmem_tool_calls_total" in body

    async def test_reflects_tracked_tool_calls(self, client):
        c, *_ = client
        async with api_app.state.metrics.track("fake_tool"):
            pass
        r = await c.get("/metrics")
        body = r.text
        assert 'localmem_tool_calls_total{tool="fake_tool"} 1' in body
        assert 'localmem_tool_latency_p50_milliseconds{tool="fake_tool"}' in body

    async def test_reflects_per_wing_entries(self, client):
        c, _cfg, vs, *_ = client
        await vs.store(
            Entry(
                wing="router", room="r", agent_id="router",
                entry_type=EntryType.GENERIC, content="x", importance=0.5,
            )
        )
        r = await c.get("/metrics")
        assert 'localmem_entries{wing="router"} 1' in r.text
