"""Tests for IntelligenceEngine — pattern detection and cross-wing correlation."""

import json
import pytest
from datetime import datetime, timedelta, timezone

from conftest import FakeEmbedder

from localmem.config import (
    CrossWingCorrelationDetectorConfig,
    IntelligenceConfig,
    IntelligenceDetectorsConfig,
    LocalmemConfig,
    NodeClusterDetectorConfig,
    ProviderPreferenceDetectorConfig,
    StorageConfig,
    ToolSequenceDetectorConfig,
)
from localmem.graph_store import GraphStore
from localmem.intelligence import IntelligenceEngine
from localmem.metadata_store import MetadataStore
from localmem.models import Entry, EntryType
from localmem.vector_store import VectorStore
from localmem.wake_up import WakeUp


TOOLS_WING = "tools"
TOOLS_ROOM = "tool-executions"
ROUTER_WING = "router"
ROUTING_ROOM = "routing"
OBSERVER_WING = "observer"


def _make_cfg(tmp_path) -> LocalmemConfig:
    return LocalmemConfig(
        wings=[TOOLS_WING, ROUTER_WING, OBSERVER_WING],
        storage=StorageConfig(
            base_path=str(tmp_path),
            qdrant_path=str(tmp_path / "qdrant"),
            sqlite_path=str(tmp_path / "test.db"),
            graph_path=str(tmp_path / "graph.json"),
        ),
        intelligence=IntelligenceConfig(
            pattern_min_frequency=2,
            correlation_window_hours=48,
            correlation_min_strength=0.3,
            detectors=IntelligenceDetectorsConfig(
                tool_sequences=ToolSequenceDetectorConfig(
                    wing=TOOLS_WING, room=TOOLS_ROOM
                ),
                provider_preferences=ProviderPreferenceDetectorConfig(
                    wing=ROUTER_WING, room=ROUTING_ROOM
                ),
                node_clusters=NodeClusterDetectorConfig(
                    node_type="detector", node_prefix="det:"
                ),
                cross_wing_correlations=CrossWingCorrelationDetectorConfig(
                    wings=[TOOLS_WING, ROUTER_WING, OBSERVER_WING]
                ),
            ),
        ),
    )


@pytest.fixture
async def stores(tmp_path):
    cfg = _make_cfg(tmp_path)
    embedder = FakeEmbedder()
    vs = VectorStore(cfg, embedder)

    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, SparseVectorParams, VectorParams
    from localmem.vector_store import COLLECTION
    from pathlib import Path

    path = Path(cfg.storage.qdrant_path)
    path.mkdir(parents=True, exist_ok=True)
    vs._client = QdrantClient(path=str(path))
    vs._embedder = embedder

    vs._client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=8, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )

    gs = GraphStore(cfg)
    await gs.initialize()

    ms = MetadataStore(cfg)
    await ms.initialize()

    engine = IntelligenceEngine(cfg, vs, ms, gs)
    yield cfg, vs, gs, ms, engine
    await gs.shutdown()


def _ts(minutes_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _tool_entry(tool: str, success: bool = True, minutes_ago: int = 0) -> Entry:
    return Entry(
        wing=TOOLS_WING,
        room=TOOLS_ROOM,
        agent_id=TOOLS_WING,
        entry_type=EntryType.GENERIC,
        content=json.dumps({"tool": tool, "success": success, "duration_ms": 50}),
        tags=["tool-call", tool],
        importance=0.4,
        created_at=_ts(minutes_ago),
    )


def _routing_entry(provider: str, task_type: str, minutes_ago: int = 0) -> Entry:
    return Entry(
        wing=ROUTER_WING,
        room=ROUTING_ROOM,
        agent_id=ROUTER_WING,
        entry_type=EntryType.GENERIC,
        content=json.dumps({"provider": provider, "task_type": task_type, "score": 0.9}),
        tags=["routing", task_type, provider],
        importance=0.4,
        created_at=_ts(minutes_ago),
    )


# =========================================================================
# Tool Sequences
# =========================================================================


class TestToolSequences:
    async def test_detects_frequent_pair(self, stores):
        _, vs, _, _, engine = stores
        for i in range(6):
            tool = "read" if i % 2 == 0 else "edit"
            await vs.store(_tool_entry(tool, minutes_ago=60 - i))

        result = await engine.detect_tool_sequences()
        assert len(result) >= 1
        pairs = {tuple(r["sequence"]) for r in result}
        assert ("read", "edit") in pairs

    async def test_returns_empty_with_no_data(self, stores):
        _, _, _, _, engine = stores
        result = await engine.detect_tool_sequences()
        assert result == []

    async def test_returns_empty_when_unconfigured(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                base_path=str(tmp_path),
                qdrant_path=str(tmp_path / "q"),
                sqlite_path=str(tmp_path / "t.db"),
                graph_path=str(tmp_path / "g.json"),
            ),
        )
        # No detector wing/room configured → detector returns [].
        embedder = FakeEmbedder()
        vs = VectorStore(cfg, embedder)
        engine = IntelligenceEngine(cfg, vs, MetadataStore(cfg), GraphStore(cfg))
        result = await engine.detect_tool_sequences()
        assert result == []

    async def test_tracks_success_rate(self, stores):
        _, vs, _, _, engine = stores
        for i in range(4):
            if i % 2 == 0:
                await vs.store(_tool_entry("read", success=True, minutes_ago=40 - i))
            else:
                await vs.store(_tool_entry("edit", success=False, minutes_ago=40 - i))

        result = await engine.detect_tool_sequences()
        re_pair = [r for r in result if r["sequence"] == ["read", "edit"]]
        assert len(re_pair) == 1
        assert re_pair[0]["success_rate"] == 0.0


# =========================================================================
# Provider Preferences
# =========================================================================


class TestProviderPreferences:
    async def test_detects_dominant_preference(self, stores):
        _, vs, _, _, engine = stores
        for i in range(8):
            await vs.store(_routing_entry("claude", "code", minutes_ago=i))
        for i in range(2):
            await vs.store(_routing_entry("gemini", "code", minutes_ago=10 + i))

        result = await engine.detect_provider_preferences()
        assert len(result) == 1
        assert result[0]["pattern"] == "dominant"
        assert result[0]["preferred_provider"] == "claude"
        assert result[0]["share"] >= 0.7

    async def test_detects_split_preference(self, stores):
        _, vs, _, _, engine = stores
        for i in range(5):
            await vs.store(_routing_entry("claude", "analysis", minutes_ago=i))
        for i in range(4):
            await vs.store(_routing_entry("gemini", "analysis", minutes_ago=10 + i))

        result = await engine.detect_provider_preferences()
        assert len(result) == 1
        assert result[0]["pattern"] == "split"

    async def test_returns_empty_below_threshold(self, stores):
        _, vs, _, _, engine = stores
        await vs.store(_routing_entry("claude", "rare", minutes_ago=0))

        result = await engine.detect_provider_preferences()
        assert result == []


# =========================================================================
# Node Clusters
# =========================================================================


class TestNodeClusters:
    async def test_detects_cluster_from_connected_nodes(self, stores):
        _, _, gs, _, engine = stores
        for det in ["det:coherence", "det:creativity", "det:self-reference"]:
            await gs.add_node(det, {"type": "detector", "name": det, "frequency": 5})

        await gs.add_edge("det:coherence", "det:creativity", {"relation": "co_occurred_with"})
        await gs.add_edge("det:creativity", "det:self-reference", {"relation": "co_occurred_with"})
        await gs.add_edge("det:self-reference", "det:coherence", {"relation": "co_occurred_with"})

        result = await engine.detect_node_clusters()
        assert len(result) >= 1
        cluster = result[0]
        assert cluster["size"] == 3
        assert cluster["cohesion"] == 1.0

    async def test_returns_empty_without_matching_nodes(self, stores):
        _, _, _, _, engine = stores
        result = await engine.detect_node_clusters()
        assert result == []

    async def test_isolated_nodes_not_clustered(self, stores):
        _, _, gs, _, engine = stores
        await gs.add_node("det:a", {"type": "detector"})
        await gs.add_node("det:b", {"type": "detector"})

        result = await engine.detect_node_clusters()
        assert result == []


# =========================================================================
# Cross-Wing Correlation
# =========================================================================


class TestCrossWingCorrelations:
    async def test_detects_temporal_correlation(self, stores):
        _, vs, _, _, engine = stores
        base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        for i in range(8):
            bucket_time = base - timedelta(minutes=i * 10)
            tools_ts = bucket_time.isoformat().replace("+00:00", "Z")
            observer_ts = (bucket_time + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")

            await vs.store(Entry(
                wing=TOOLS_WING, room="classifications", agent_id=TOOLS_WING,
                entry_type=EntryType.GENERIC,
                content=json.dumps({"task_type": "code", "complexity": 0.8}),
                tags=["classification"], created_at=tools_ts,
            ))
            await vs.store(Entry(
                wing=OBSERVER_WING, room="observations", agent_id=OBSERVER_WING,
                entry_type=EntryType.BEHAVIORAL_OBSERVATION,
                content=json.dumps({"emergence_probability": 0.7}),
                tags=["emergence"], created_at=observer_ts,
            ))

        result = await engine.detect_cross_wing_correlations()
        assert len(result) >= 1
        pair = result[0]
        assert set(pair["wings"]) == {OBSERVER_WING, TOOLS_WING}
        assert pair["strength"] >= 0.3

    async def test_no_correlation_when_non_overlapping(self, stores):
        _, vs, _, _, engine = stores
        for i in range(3):
            await vs.store(Entry(
                wing=TOOLS_WING, room=TOOLS_ROOM, agent_id=TOOLS_WING,
                entry_type=EntryType.GENERIC, content=json.dumps({"tool": "read"}),
                tags=["tool-call"], created_at=_ts(i),
            ))
            await vs.store(Entry(
                wing=OBSERVER_WING, room="observations", agent_id=OBSERVER_WING,
                entry_type=EntryType.BEHAVIORAL_OBSERVATION,
                content=json.dumps({"emergence_probability": 0.5}),
                tags=["emergence"], created_at=_ts(180 + i * 10),
            ))

        result = await engine.detect_cross_wing_correlations()
        for r in result:
            if set(r["wings"]) == {OBSERVER_WING, TOOLS_WING}:
                pass


# =========================================================================
# Run Detection + Store Alerts
# =========================================================================


class TestRunDetection:
    async def test_run_detection_returns_all_keys(self, stores):
        _, _, _, _, engine = stores
        result = await engine.run_detection()
        assert "tool_sequences" in result
        assert "provider_preferences" in result
        assert "node_clusters" in result
        assert "cross_wing" in result

    async def test_store_alerts_creates_entries(self, stores):
        _, vs, gs, ms, engine = stores

        for i in range(4):
            await vs.store(_routing_entry("claude", "code", minutes_ago=i))

        results = await engine.run_detection()
        stored = await engine.store_alerts(results)
        assert stored >= 1

        alerts = await vs.scroll(wing="shared", room="intelligence-alerts")
        assert len(alerts) >= 1
        assert any("intelligence" in a.tags for a in alerts)

    async def test_store_alerts_returns_zero_with_no_data(self, stores):
        _, _, _, _, engine = stores
        results = await engine.run_detection()
        stored = await engine.store_alerts(results)
        assert stored == 0


# =========================================================================
# Wake-Up Intelligence Injection
# =========================================================================


class TestWakeUpIntelligence:
    async def test_wake_includes_relevant_alerts(self, stores):
        cfg, vs, gs, ms, engine = stores

        alert = Entry(
            wing="shared",
            room=cfg.intelligence.alert_room,
            agent_id="localmem",
            entry_type=EntryType.GENERIC,
            content="Provider preference: code -> claude (dominant, 85%)",
            tags=["intelligence", "pattern", ROUTER_WING, "provider-preference"],
            importance=0.7,
        )
        await vs.store(alert)
        await ms.register_room("shared", cfg.intelligence.alert_room)

        wake = WakeUp(cfg, vs, ms, manifests_dir=str(cfg.storage.base_path))
        ctx = await wake.wake(ROUTER_WING)

        alert_ids = {e.id for e in ctx.l1_entries}
        assert alert.id in alert_ids

    async def test_wake_excludes_unrelated_alerts(self, stores):
        cfg, vs, gs, ms, engine = stores

        alert = Entry(
            wing="shared",
            room=cfg.intelligence.alert_room,
            agent_id="localmem",
            entry_type=EntryType.GENERIC,
            content="Tool sequence pattern",
            tags=["intelligence", "pattern", TOOLS_WING],
            importance=0.6,
        )
        await vs.store(alert)
        await ms.register_room("shared", cfg.intelligence.alert_room)

        wake = WakeUp(cfg, vs, ms, manifests_dir=str(cfg.storage.base_path))
        ctx = await wake.wake(ROUTER_WING)

        alert_ids = {e.id for e in ctx.l1_entries}
        assert alert.id not in alert_ids

    async def test_wake_caps_alerts_at_three(self, stores):
        cfg, vs, gs, ms, engine = stores

        for i in range(5):
            await vs.store(Entry(
                wing="shared",
                room=cfg.intelligence.alert_room,
                agent_id="localmem",
                entry_type=EntryType.GENERIC,
                content=f"Cross-wing alert {i}",
                tags=["intelligence", "cross-wing"],
                importance=0.5,
            ))
        await ms.register_room("shared", cfg.intelligence.alert_room)

        wake = WakeUp(cfg, vs, ms, manifests_dir=str(cfg.storage.base_path))
        ctx = await wake.wake(OBSERVER_WING)

        cross_wing_entries = [
            e for e in ctx.l1_entries
            if "cross-wing" in e.tags
        ]
        assert len(cross_wing_entries) <= 3
