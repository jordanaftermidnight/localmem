"""Comprehensive simulation tests — end-to-end integration across all Phase 3 features.

Simulates realistic multi-agent data flows through the full LOCALMEM pipeline:
storage → intelligence detection → alert generation → wake-up injection → MCP tool access.
"""

import json
import math
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from localmem.models import Entry, EntryType, PatternAlert, _now
from localmem.vector_store import VectorStore
from localmem.wake_up import WakeUp


@pytest.fixture
async def sim(tmp_path):
    """Full simulation environment with all stores and engine."""
    cfg = LocalmemConfig(
        wings=["tools", "router", "observer"],
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
                    wing="tools", room="tool-executions"
                ),
                provider_preferences=ProviderPreferenceDetectorConfig(
                    wing="router", room="routing"
                ),
                node_clusters=NodeClusterDetectorConfig(
                    node_type="detector", node_prefix="det:"
                ),
                cross_wing_correlations=CrossWingCorrelationDetectorConfig(
                    wings=["tools", "router", "observer"]
                ),
            ),
        ),
    )
    embedder = FakeEmbedder()
    vs = VectorStore(cfg, embedder)

    from qdrant_client import QdrantClient, models
    from qdrant_client.models import Distance, VectorParams, SparseVectorParams
    from localmem.vector_store import COLLECTION

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
    wake = WakeUp(cfg, vs, ms, manifests_dir=str(tmp_path / "manifests"))

    yield cfg, vs, gs, ms, engine, wake
    await gs.shutdown()


def _ts(minutes_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat().replace("+00:00", "Z")


# =============================================================================
# Simulation 1: Full AGENT_TOOLS session
# =============================================================================


class TestSorielSimulation:
    """Simulates a AGENT_TOOLS coding session: tool calls, inference chains, then detect."""

    async def test_full_tools_session(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Simulate 20 tool calls with repeating patterns
        tools = ["read", "edit", "bash", "read", "edit", "read", "grep", "read", "edit", "bash",
                 "read", "edit", "bash", "read", "edit", "read", "edit", "bash", "read", "edit"]
        for i, tool in enumerate(tools):
            entry = Entry(
                wing="tools", room="tool-executions", agent_id="tools",
                entry_type=EntryType.GENERIC,
                content=json.dumps({
                    "tool": tool,
                    "success": True,
                    "duration_ms": 50 + i * 10,
                    "output_length": 200,
                }),
                tags=["tool-call", tool],
                importance=0.4,
                created_at=_ts(100 - i * 2),
            )
            await vs.store(entry)
            await ms.update_importance(entry.id, 0.4, wing="tools")
            await ms.register_room("tools", "tool-executions")

        # Also simulate some inference chains
        for i in range(5):
            entry = Entry(
                wing="tools", room="inference-chains", agent_id="tools",
                entry_type=EntryType.INFERENCE_CHAIN,
                content=json.dumps({
                    "model": "claude-sonnet-4",
                    "tokens_used": 1500 + i * 200,
                    "turn": i,
                }),
                tags=["auto-logged", "claude-sonnet-4"],
                importance=0.5,
                created_at=_ts(90 - i * 5),
            )
            await vs.store(entry)

        # Run detection
        results = await engine.run_detection()
        seqs = results["tool_sequences"]

        # Should detect read->edit as the dominant pattern
        assert len(seqs) > 0
        pair_names = [tuple(s["sequence"]) for s in seqs]
        assert ("read", "edit") in pair_names

        # Store alerts
        stored = await engine.store_alerts(results)
        assert stored > 0

        # Verify alerts are in vector store
        alerts = await vs.scroll(wing="shared", room="intelligence-alerts")
        tools_alerts = [a for a in alerts if "tools" in a.tags]
        assert len(tools_alerts) > 0

        # Verify wing-filtered top entries
        top = await ms.get_top_entries(wing="tools", limit=5)
        assert len(top) > 0
        for entry_info in top:
            assert 0 <= entry_info["effective_score"] <= 1.0


# =============================================================================
# Simulation 2: Full AGENT_ROUTER routing session
# =============================================================================


class TestIrisSimulation:
    """Simulates AGENT_ROUTER routing across multiple providers and task types."""

    async def test_full_router_routing_session(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Simulate 30 routing decisions
        routing_data = [
            # Code tasks: mostly Claude
            ("claude", "code_generation"), ("claude", "code_generation"),
            ("claude", "code_generation"), ("claude", "code_generation"),
            ("gemini", "code_generation"), ("claude", "code_generation"),
            ("claude", "code_generation"), ("claude", "code_generation"),
            # Analysis: split between Claude and Gemini
            ("claude", "analysis"), ("gemini", "analysis"),
            ("claude", "analysis"), ("gemini", "analysis"),
            ("claude", "analysis"), ("gemini", "analysis"),
            # Chat: rotating
            ("claude", "chat"), ("gemini", "chat"),
            ("groq", "chat"), ("claude", "chat"),
            ("gemini", "chat"), ("groq", "chat"),
        ]

        for i, (provider, task_type) in enumerate(routing_data):
            entry = Entry(
                wing="router", room="routing", agent_id="router",
                entry_type=EntryType.GENERIC,
                content=json.dumps({
                    "provider": provider,
                    "task_type": task_type,
                    "score": 0.9 - i * 0.01,
                    "alternatives": [{"provider": "other", "score": 0.5}],
                }),
                tags=["routing", task_type, provider],
                importance=0.4,
                created_at=_ts(60 - i),
            )
            await vs.store(entry)
            await ms.update_importance(entry.id, 0.4, wing="router")
            await ms.register_room("router", "routing")

        # Also simulate request logs with some failures
        for i in range(10):
            success = i != 3  # One failure
            entry = Entry(
                wing="router", room="requests", agent_id="router",
                entry_type=EntryType.GENERIC,
                content=json.dumps({
                    "provider": "claude",
                    "task_type": "code_generation",
                    "success": success,
                    "response_time_ms": 500 + i * 100,
                }),
                tags=["request", "claude", "success" if success else "failure"],
                importance=0.3 if success else 0.7,
                created_at=_ts(50 - i),
            )
            await vs.store(entry)

        # Run detection
        results = await engine.run_detection()
        prefs = results["provider_preferences"]

        assert len(prefs) >= 2  # At least code_generation and analysis
        code_pref = [p for p in prefs if p["task_type"] == "code_generation"]
        assert len(code_pref) == 1
        assert code_pref[0]["pattern"] == "dominant"
        assert code_pref[0]["preferred_provider"] == "claude"

        # Store and verify
        stored = await engine.store_alerts(results)
        assert stored >= 2

        # Verify router top entries are wing-filtered
        router_top = await ms.get_top_entries(wing="router", limit=5)
        assert len(router_top) > 0


# =============================================================================
# Simulation 3: Full AGENT_OBSERVER observation session
# =============================================================================


class TestEchoSimulation:
    """Simulates AGENT_OBSERVER detector activity and emergence analysis."""

    async def test_full_observer_session(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Build detector graph (6 detectors, 2 natural clusters)
        cluster1 = ["det:coherence", "det:creativity", "det:self-reference"]
        cluster2 = ["det:meta-awareness", "det:emotional-depth"]
        isolated = ["det:vocabulary"]

        for det in cluster1 + cluster2 + [isolated[0]]:
            await gs.add_node(det, {"type": "detector", "name": det, "frequency": 5})

        # Cluster 1: fully connected triangle
        for i in range(len(cluster1)):
            for j in range(i + 1, len(cluster1)):
                await gs.add_edge(cluster1[i], cluster1[j],
                                  {"relation": "co_occurred_with", "turn": 1})

        # Cluster 2: connected pair
        await gs.add_edge(cluster2[0], cluster2[1],
                          {"relation": "co_occurred_with", "turn": 2})

        # No edges to isolated detector

        # Simulate observations
        for i in range(8):
            entry = Entry(
                wing="observer", room="observations", agent_id="observer",
                entry_type=EntryType.BEHAVIORAL_OBSERVATION,
                content=json.dumps({
                    "source_agent": "observer",
                    "emergence_probability": 0.3 + i * 0.05,
                    "classification": "curious",
                    "top_signals": {"coherence": 0.8, "creativity": 0.6},
                    "turn": i,
                }),
                tags=["emergence", "curious", "observer"],
                importance=min(0.3 + i * 0.05, 1.0),
                created_at=_ts(40 - i * 3),
            )
            await vs.store(entry)

        # Run detection
        results = await engine.run_detection()
        clusters = results["node_clusters"]

        assert len(clusters) >= 1
        # Should find the triangle cluster with cohesion 1.0
        triangle = [c for c in clusters if c["size"] == 3]
        assert len(triangle) == 1
        assert triangle[0]["cohesion"] == 1.0


# =============================================================================
# Simulation 4: Cross-agent correlation
# =============================================================================


class TestCrossAgentSimulation:
    """Simulates concurrent agent activity and tests correlation detection."""

    async def test_cross_wing_activity(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Simulate concurrent activity from all 3 agents in overlapping time windows
        for i in range(10):
            # AGENT_TOOLS: tool calls every 5 minutes
            await vs.store(Entry(
                wing="tools", room="tool-executions", agent_id="tools",
                entry_type=EntryType.GENERIC,
                content=json.dumps({"tool": "read", "success": True}),
                tags=["tool-call", "read"],
                created_at=_ts(i * 5),
            ))

            # AGENT_ROUTER: routing decisions every 5 minutes (aligned)
            await vs.store(Entry(
                wing="router", room="routing", agent_id="router",
                entry_type=EntryType.GENERIC,
                content=json.dumps({"provider": "claude", "task_type": "code"}),
                tags=["routing", "code", "claude"],
                created_at=_ts(i * 5 + 1),
            ))

            # AGENT_OBSERVER: observations every 5 minutes (aligned)
            if i % 2 == 0:  # AGENT_OBSERVER only on even intervals
                await vs.store(Entry(
                    wing="observer", room="observations", agent_id="observer",
                    entry_type=EntryType.BEHAVIORAL_OBSERVATION,
                    content=json.dumps({"emergence_probability": 0.5}),
                    tags=["emergence"],
                    created_at=_ts(i * 5 + 2),
                ))

        results = await engine.run_detection()
        corrs = results["cross_wing"]

        # AGENT_TOOLS and AGENT_ROUTER should strongly correlate (always in same buckets)
        tools_router = [c for c in corrs
                       if set(c["wings"]) == {"tools", "router"}]
        if tools_router:
            assert tools_router[0]["strength"] >= 0.5


# =============================================================================
# Simulation 5: Full pipeline — store → detect → alert → wake
# =============================================================================


class TestFullPipeline:
    """End-to-end: agent logs data → intelligence runs → alerts stored → wake includes them."""

    async def test_end_to_end_pipeline(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Phase 1: Agents log data
        for i in range(5):
            await vs.store(Entry(
                wing="router", room="routing", agent_id="router",
                entry_type=EntryType.GENERIC,
                content=json.dumps({"provider": "claude", "task_type": "code"}),
                tags=["routing", "code", "claude"],
                importance=0.4,
                created_at=_ts(20 - i),
            ))

        # Phase 2: Run intelligence detection
        results = await engine.run_detection()
        assert "provider_preferences" in results

        # Phase 3: Store alerts
        stored = await engine.store_alerts(results)

        # Phase 4: Wake an agent and verify alerts are included
        if stored > 0:
            ctx = await wake.wake("router")
            alert_entries = [
                e for e in ctx.l1_entries
                if "intelligence" in e.tags
            ]
            assert len(alert_entries) > 0
            # Verify alert content is meaningful
            for a in alert_entries:
                assert a.wing == "shared"
                assert a.room == cfg.intelligence.alert_room

    async def test_pipeline_with_no_data(self, sim):
        """Pipeline should handle empty stores gracefully."""
        cfg, vs, gs, ms, engine, wake = sim

        results = await engine.run_detection()
        assert results["tool_sequences"] == []
        assert results["provider_preferences"] == []
        assert results["node_clusters"] == []
        assert results["cross_wing"] == []

        stored = await engine.store_alerts(results)
        assert stored == 0

        ctx = await wake.wake("router")
        assert ctx.agent_id == "router"
        assert len(ctx.l1_entries) == 0


# =============================================================================
# Simulation 6: Time decay ranking under realistic conditions
# =============================================================================


class TestDecaySimulation:
    """Tests time-decay importance scoring with realistic data volumes."""

    async def test_decay_with_mixed_ages(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Store 20 entries with varying ages and importance
        for i in range(20):
            entry_id = f"entry-{i}"
            await ms.update_importance(entry_id, base_score=0.5, wing="tools")

        # Age some entries artificially
        async with ms._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            # Make first 5 entries very old (300 days)
            for i in range(5):
                old_ts = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
                await db.execute(
                    "UPDATE importance SET last_accessed=? WHERE entry_id=?",
                    (old_ts, f"entry-{i}"),
                )
            # Make entries 10-14 moderately old (50 days)
            for i in range(10, 15):
                mid_ts = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat()
                await db.execute(
                    "UPDATE importance SET last_accessed=? WHERE entry_id=?",
                    (mid_ts, f"entry-{i}"),
                )
            await db.commit()

        top = await ms.get_top_entries(wing="tools", limit=20)
        assert len(top) == 20

        # All fresh entries (5-9, 15-19) should rank above old entries (0-4)
        # Entries 0-4 aged 300 days, 10-14 aged 50 days, rest are fresh
        old_ids = {f"entry-{i}" for i in range(5)}
        fresh_ids = {f"entry-{i}" for i in range(5, 10)} | {f"entry-{i}" for i in range(15, 20)}

        top10_ids = {t["entry_id"] for t in top[:10]}
        bottom10_ids = {t["entry_id"] for t in top[10:]}

        # Fresh entries should dominate the top half
        assert len(fresh_ids & top10_ids) >= 8
        # Very old entries (300 days) should be in the bottom half
        assert len(old_ids & bottom10_ids) >= 4

        # All scores should be clamped [0, 1]
        for entry_info in top:
            assert 0 <= entry_info["effective_score"] <= 1.0

    async def test_high_access_count_clamped(self, sim):
        """Verify effective_score doesn't exceed 1.0 even with high access."""
        cfg, vs, gs, ms, engine, wake = sim

        await ms.update_importance("hot-entry", base_score=0.9, wing="router")
        # Simulate 100 accesses
        for _ in range(100):
            await ms.update_importance("hot-entry", wing="router")

        top = await ms.get_top_entries(wing="router")
        assert len(top) == 1
        assert top[0]["effective_score"] <= 1.0
        assert top[0]["access_count"] == 101  # 1 initial + 100 updates


# =============================================================================
# Simulation 7: Scroll edge cases
# =============================================================================


class TestScrollSimulation:
    """Tests scroll() under various conditions."""

    async def test_scroll_empty_store(self, sim):
        cfg, vs, gs, ms, engine, wake = sim
        result = await vs.scroll(wing="router")
        assert result == []

    async def test_scroll_with_wing_filter(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Store entries in different wings
        for wing in ("router", "tools", "observer"):
            for i in range(3):
                await vs.store(Entry(
                    wing=wing, room="test", agent_id=wing,
                    entry_type=EntryType.GENERIC,
                    content=f"entry {wing}-{i}",
                ))

        router_entries = await vs.scroll(wing="router")
        assert len(router_entries) == 3
        assert all(e.wing == "router" for e in router_entries)

    async def test_scroll_with_room_filter(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        await vs.store(Entry(
            wing="router", room="routing", agent_id="router",
            entry_type=EntryType.GENERIC, content="routing entry",
        ))
        await vs.store(Entry(
            wing="router", room="requests", agent_id="router",
            entry_type=EntryType.GENERIC, content="request entry",
        ))

        routing = await vs.scroll(wing="router", room="routing")
        assert len(routing) == 1
        assert routing[0].room == "routing"

    async def test_scroll_with_tag_filter(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        await vs.store(Entry(
            wing="router", room="test", agent_id="router",
            entry_type=EntryType.GENERIC, content="tagged",
            tags=["special", "test"],
        ))
        await vs.store(Entry(
            wing="router", room="test", agent_id="router",
            entry_type=EntryType.GENERIC, content="untagged",
            tags=["other"],
        ))

        tagged = await vs.scroll(wing="router", tags=["special"])
        assert len(tagged) == 1
        assert "special" in tagged[0].tags

    async def test_scroll_respects_limit(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        for i in range(10):
            await vs.store(Entry(
                wing="observer", room="test", agent_id="observer",
                entry_type=EntryType.GENERIC, content=f"entry-{i}",
            ))

        limited = await vs.scroll(wing="observer", limit=3)
        assert len(limited) == 3


# =============================================================================
# Simulation 8: MCP tool return format validation
# =============================================================================


class TestMCPToolFormats:
    """Validates that MCP-style operations return correctly formatted data."""

    async def test_intel_detect_format(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Populate some data
        for i in range(5):
            await vs.store(Entry(
                wing="router", room="routing", agent_id="router",
                entry_type=EntryType.GENERIC,
                content=json.dumps({"provider": "claude", "task_type": "code"}),
                tags=["routing"],
                created_at=_ts(i),
            ))

        results = await engine.run_detection()
        stored = await engine.store_alerts(results)

        # Verify format matches what localmem_intel_detect would return
        response = {
            "tool_sequences": len(results.get("tool_sequences", [])),
            "provider_preferences": len(results.get("provider_preferences", [])),
            "node_clusters": len(results.get("node_clusters", [])),
            "cross_wing": len(results.get("cross_wing", [])),
            "alerts_stored": stored,
            "details": results,
        }

        assert isinstance(response["tool_sequences"], int)
        assert isinstance(response["provider_preferences"], int)
        assert isinstance(response["alerts_stored"], int)
        assert isinstance(response["details"], dict)

    async def test_intel_alerts_format(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Store an alert
        await vs.store(Entry(
            wing="shared", room=cfg.intelligence.alert_room,
            agent_id="localmem", entry_type=EntryType.GENERIC,
            content="Test alert", tags=["intelligence", "router"],
            importance=0.7,
        ))

        alerts = await vs.scroll(
            wing="shared", room=cfg.intelligence.alert_room, limit=10,
        )
        formatted = [
            {
                "id": a.id,
                "content": a.content,
                "summary": a.summary,
                "importance": a.importance,
                "tags": a.tags,
                "metadata": a.metadata,
                "created_at": a.created_at,
            }
            for a in alerts
        ]

        assert len(formatted) == 1
        assert formatted[0]["content"] == "Test alert"
        assert isinstance(formatted[0]["tags"], list)
        assert isinstance(formatted[0]["importance"], float)

    async def test_intel_report_format(self, sim):
        cfg, vs, gs, ms, engine, wake = sim

        # Store some entries with importance for distribution stats
        for i in range(3):
            entry_id = f"router-{i}"
            await ms.update_importance(entry_id, base_score=0.5 + i * 0.1, wing="router")

        results = await engine.run_detection()
        distribution = {}
        for w in ("router", "tools", "observer"):
            entries = await ms.get_top_entries(wing=w, limit=1000)
            if entries:
                scores = [e["effective_score"] for e in entries]
                distribution[w] = {
                    "count": len(entries),
                    "avg_score": round(sum(e["score"] for e in entries) / len(entries), 3),
                    "avg_effective": round(sum(scores) / len(scores), 3),
                }

        report = {"patterns": results, "importance_distribution": distribution}

        assert "patterns" in report
        assert "importance_distribution" in report
        assert "router" in report["importance_distribution"]
        assert report["importance_distribution"]["router"]["count"] == 3


# =============================================================================
# Simulation 9: PatternAlert model validation
# =============================================================================


class TestPatternAlertModel:
    def test_pattern_alert_creation(self):
        alert = PatternAlert(
            pattern_type="tool_sequence",
            source_wing="tools",
            description="read -> edit (12x)",
            strength=0.85,
            details={"frequency": 12, "success_rate": 0.95},
        )
        assert alert.pattern_type == "tool_sequence"
        assert alert.strength == 0.85
        assert alert.detected_at  # Should auto-populate

    def test_pattern_alert_defaults(self):
        alert = PatternAlert(
            pattern_type="cross_wing",
            source_wing="shared",
            description="test",
            strength=0.5,
        )
        assert alert.details == {}
        assert alert.detected_at is not None
