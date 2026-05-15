"""localmem MCP server — FastMCP over SSE with 22 tools."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .config import LocalmemConfig, load_config
from .embedder import Embedder
from .graph_store import GraphStore
from .health import health_snapshot
from .intelligence import IntelligenceEngine
from .logging_config import setup_logging
from .metadata_store import MetadataStore
from .metrics import MetricsCollector
from .models import (
    DiaryEntry,
    Entry,
    EntryType,
    GraphQuery,
    SearchQuery,
    Triple,
)
from .vector_store import VectorStore
from .wake_up import WakeUp

logger = logging.getLogger(__name__)

# --- Globals (initialized in lifespan) ---
config: LocalmemConfig
embedder: Embedder
vector_store: VectorStore
graph_store: GraphStore
metadata_store: MetadataStore
wake_up: WakeUp
intelligence_engine: IntelligenceEngine
metrics_collector: MetricsCollector
server_start_time: float = 0.0
write_locks: dict[str, asyncio.Lock] = {}
retention_worker: object | None = None  # BackgroundWorker; lazy-imported to avoid cycle

# `run()` writes the resolved config here before invoking `mcp.run()`, so the
# lifespan context manager can find it. None at module-import time so importers
# (tests, dashboard) don't pay the cost.
_runtime_config: LocalmemConfig | None = None


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    """Initialize stores at server startup, shut them down at exit."""
    if _runtime_config is not None:
        await initialize_stores(_runtime_config)
    try:
        yield
    finally:
        if _runtime_config is not None:
            await shutdown_stores()


mcp = FastMCP(
    "localmem",
    instructions=(
        "Local-first multi-agent memory server. Tools cover memory CRUD + hybrid "
        "search, behavioral graph traversal, knowledge triples with temporal "
        "contradiction detection, layered wake-up context loading, pattern "
        "detection, retention/archive operations, and health/metrics."
    ),
    lifespan=_lifespan,
)


def _get_lock(wing: str) -> asyncio.Lock:
    # dict.setdefault is atomic in CPython, so two concurrent calls for a
    # never-seen wing get the same Lock instance and writes serialize as
    # intended. Initialized for the standard wings in init_stores().
    return write_locks.setdefault(wing, asyncio.Lock())


# =============================================================================
# Memory Operations (6)
# =============================================================================


@mcp.tool()
async def localmem_wake(agent_id: str) -> dict[str, Any]:
    """Load L0 manifest + L1 critical context for an agent. Call on startup."""
    async with metrics_collector.track("localmem_wake"):
        ctx = await wake_up.wake(agent_id)
        return {
            "agent_id": ctx.agent_id,
            "manifest": ctx.l0_manifest,
            "critical_context": [
                {
                    "id": e.id,
                    "wing": e.wing,
                    "room": e.room,
                    "summary": e.summary or e.content[:200],
                    "importance": e.importance,
                    "tags": e.tags,
                }
                for e in ctx.l1_entries
            ],
            "token_estimate": ctx.total_tokens_estimate,
        }


@mcp.tool()
async def localmem_store(
    wing: str,
    room: str,
    agent_id: str,
    content: str,
    entry_type: str = "generic",
    summary: str | None = None,
    importance: float = 0.5,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a new entry (verbatim + metadata) in a wing:room."""
    async with metrics_collector.track("localmem_store"):
        entry = Entry(
            wing=wing,
            room=room,
            agent_id=agent_id,
            entry_type=EntryType(entry_type),
            content=content,
            summary=summary,
            importance=importance,
            tags=tags or [],
            metadata=metadata or {},
        )

        async with _get_lock(wing):
            entry_id = await vector_store.store(entry)
            await metadata_store.register_room(wing, room)
            await metadata_store.update_importance(entry_id, importance, wing=wing)

        if retention_worker is not None:
            retention_worker.signal_dirty(wing)  # type: ignore[attr-defined]

        return {"id": entry_id, "wing": wing, "room": room, "status": "stored"}


@mcp.tool()
async def localmem_search(
    query: str,
    wing: str | None = None,
    room: str | None = None,
    tags: list[str] | None = None,
    entry_type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Hybrid search (dense + sparse). Scoped by wing/room/tags."""
    async with metrics_collector.track("localmem_search"):
        sq = SearchQuery(
            query=query,
            wing=wing,
            room=room,
            tags=tags,
            entry_type=EntryType(entry_type) if entry_type else None,
            limit=limit,
        )
        results = await vector_store.search(sq)

        for r in results:
            await metadata_store.update_importance(r.entry.id, wing=r.entry.wing)

        return [
            {
                "id": r.entry.id,
                "wing": r.entry.wing,
                "room": r.entry.room,
                "content": r.entry.content,
                "summary": r.entry.summary,
                "score": r.score,
                "source": r.source,
                "tags": r.entry.tags,
                "importance": r.entry.importance,
                "created_at": r.entry.created_at,
            }
            for r in results
        ]


@mcp.tool()
async def localmem_retrieve(entry_id: str) -> dict[str, Any]:
    """Get full verbatim entry by ID (L3 access)."""
    async with metrics_collector.track("localmem_retrieve"):
        entry = await vector_store.retrieve(entry_id)
        if not entry:
            return {"error": f"Entry '{entry_id}' not found"}
        await metadata_store.update_importance(entry_id, wing=entry.wing)
        return entry.model_dump()


@mcp.tool()
async def localmem_update(
    entry_id: str,
    importance: float | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Update metadata on an existing entry."""
    async with metrics_collector.track("localmem_update"):
        entry = await vector_store.retrieve(entry_id)
        if not entry:
            return {"error": f"Entry '{entry_id}' not found"}

        if importance is not None:
            entry.importance = importance
        if tags is not None:
            entry.tags = tags
        if summary is not None:
            entry.summary = summary

        async with _get_lock(entry.wing):
            await vector_store.store(entry)  # Upsert
            if importance is not None:
                await metadata_store.update_importance(entry_id, importance, wing=entry.wing)

        return {"id": entry_id, "status": "updated"}


@mcp.tool()
async def localmem_delete(entry_id: str) -> dict[str, Any]:
    """Soft-delete an entry."""
    async with metrics_collector.track("localmem_delete"):
        entry = await vector_store.retrieve(entry_id)
        if not entry:
            return {"error": f"Entry '{entry_id}' not found"}
        async with _get_lock(entry.wing):
            await vector_store.delete(entry_id)
        return {"id": entry_id, "status": "deleted"}


# =============================================================================
# Graph Operations (5)
# =============================================================================


@mcp.tool()
async def localmem_graph_add(
    node_id: str | None = None,
    node_attributes: dict[str, Any] | None = None,
    source: str | None = None,
    target: str | None = None,
    edge_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a node and/or edge to the behavioral pattern graph."""
    async with metrics_collector.track("localmem_graph_add"):
        result = {}
        if node_id:
            await graph_store.add_node(node_id, node_attributes)
            result["node_added"] = node_id
        if source and target:
            await graph_store.add_node(source)
            await graph_store.add_node(target)
            await graph_store.add_edge(source, target, edge_attributes)
            result["edge_added"] = {"from": source, "to": target}
        return result or {"error": "Provide node_id and/or source+target"}


@mcp.tool()
async def localmem_graph_query(
    operation: str,
    source_node: str | None = None,
    target_node: str | None = None,
    depth: int = 3,
) -> dict[str, Any]:
    """Query the graph: path, neighbors, community, centrality."""
    async with metrics_collector.track("localmem_graph_query"):
        q = GraphQuery(
            operation=operation,
            source_node=source_node,
            target_node=target_node,
            depth=depth,
        )
        return await graph_store.query(q)


@mcp.tool()
async def localmem_graph_temporal(
    start_time: str,
    end_time: str,
) -> dict[str, Any]:
    """Extract time-windowed subgraph."""
    async with metrics_collector.track("localmem_graph_temporal"):
        q = GraphQuery(
            operation="temporal",
            start_time=start_time,
            end_time=end_time,
        )
        return await graph_store.query(q)


@mcp.tool()
async def localmem_graph_patterns(min_frequency: int = 2) -> list[dict[str, Any]]:
    """List detected patterns by frequency/centrality."""
    async with metrics_collector.track("localmem_graph_patterns"):
        return await graph_store.get_patterns(min_frequency)


@mcp.tool()
async def localmem_graph_stats() -> dict[str, Any]:
    """Node/edge counts, connected components, density."""
    async with metrics_collector.track("localmem_graph_stats"):
        return await graph_store.stats()


# =============================================================================
# Knowledge Operations (3)
# =============================================================================


@mcp.tool()
async def localmem_kg_add(
    subject: str,
    predicate: str,
    object: str,
    source_agent: str,
    confidence: float = 1.0,
    source_entry_id: str | None = None,
) -> dict[str, Any]:
    """Add a temporal triple. Returns contradiction event if applicable."""
    async with metrics_collector.track("localmem_kg_add"):
        triple = Triple(
            subject=subject,
            predicate=predicate,
            object=object,
            confidence=confidence,
            source_agent=source_agent,
            source_entry_id=source_entry_id,
        )
        contradiction = await metadata_store.add_triple(triple)
        result: dict[str, Any] = {"triple_id": triple.id, "status": "added"}
        if contradiction:
            result["contradiction"] = {
                "subject": contradiction.subject,
                "predicate": contradiction.predicate,
                "old_value": contradiction.old_value,
                "new_value": contradiction.new_value,
                "timestamp": contradiction.timestamp,
            }
        return result


@mcp.tool()
async def localmem_kg_query(
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Query triples by subject/predicate/object with temporal filtering."""
    async with metrics_collector.track("localmem_kg_query"):
        triples = await metadata_store.query_triples(
            subject=subject, predicate=predicate, obj=object, active_only=active_only
        )
        return [t.model_dump() for t in triples]


@mcp.tool()
async def localmem_kg_timeline(
    subject: str,
    predicate: str,
) -> list[dict[str, Any]]:
    """Full history of a subject's predicate changes over time."""
    async with metrics_collector.track("localmem_kg_timeline"):
        triples = await metadata_store.triple_timeline(subject, predicate)
        return [t.model_dump() for t in triples]


# =============================================================================
# System Operations (3)
# =============================================================================


@mcp.tool()
async def localmem_diary_write(
    agent_id: str,
    content: str,
    mood: str | None = None,
    tags: list[str] | None = None,
    references: list[str] | None = None,
) -> dict[str, Any]:
    """Write an agent diary entry."""
    async with metrics_collector.track("localmem_diary_write"):
        entry = DiaryEntry(
            agent_id=agent_id,
            content=content,
            mood=mood,
            tags=tags or [],
            references=references or [],
        )
        diary_id = await metadata_store.write_diary(entry)

        # Also store as searchable entry in shared:agent-diaries
        search_entry = Entry(
            id=diary_id,
            wing="shared",
            room="agent-diaries",
            agent_id=agent_id,
            entry_type=EntryType.DIARY,
            content=content,
            tags=tags or [],
            importance=0.3,
        )
        async with _get_lock("shared"):
            await vector_store.store(search_entry)
            await metadata_store.register_room("shared", "agent-diaries")

        return {"diary_id": diary_id, "agent_id": agent_id, "status": "written"}


@mcp.tool()
async def localmem_diary_read(
    agent_id: str | None = None,
    limit: int = 20,
    after: str | None = None,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Read diary entries (own or other agents') with filters."""
    async with metrics_collector.track("localmem_diary_read"):
        entries = await metadata_store.read_diary(
            agent_id=agent_id, limit=limit, after=after, before=before
        )
        return [e.model_dump() for e in entries]


@mcp.tool()
async def localmem_tunnel(room: str, limit: int = 10) -> list[dict[str, Any]]:
    """Cross-wing room query — find entries from all wings matching a room name."""
    async with metrics_collector.track("localmem_tunnel"):
        results = await vector_store.search(
            SearchQuery(query="*", room=room, limit=limit)
        )
        return [
            {
                "id": r.entry.id,
                "wing": r.entry.wing,
                "room": r.entry.room,
                "content": r.entry.content[:200],
                "score": r.score,
                "agent_id": r.entry.agent_id,
            }
            for r in results
        ]


# =============================================================================
# Intelligence Operations (3)
# =============================================================================


@mcp.tool()
async def localmem_intel_detect() -> dict[str, Any]:
    """Run all pattern detectors. Returns summary of findings and stores alerts."""
    async with metrics_collector.track("localmem_intel_detect"):
        results = await intelligence_engine.run_detection()
        stored = await intelligence_engine.store_alerts(results)
        return {
            "soriel_sequences": len(results.get("soriel_sequences", [])),
            "iris_preferences": len(results.get("iris_preferences", [])),
            "echo_clusters": len(results.get("echo_clusters", [])),
            "cross_agent": len(results.get("cross_agent", [])),
            "alerts_stored": stored,
            "details": results,
        }


@mcp.tool()
async def localmem_intel_alerts(
    wing: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get recent intelligence alerts, optionally filtered by wing."""
    async with metrics_collector.track("localmem_intel_alerts"):
        alert_room = config.intelligence.alert_room
        alerts = await vector_store.scroll(
            wing="shared", room=alert_room, limit=limit * 2
        )

        if wing:
            alerts = [a for a in alerts if wing in a.tags]

        return [
            {
                "id": a.id,
                "content": a.content,
                "summary": a.summary,
                "importance": a.importance,
                "tags": a.tags,
                "metadata": a.metadata,
                "created_at": a.created_at,
            }
            for a in alerts[:limit]
        ]


@mcp.tool()
async def localmem_intel_report() -> dict[str, Any]:
    """Full intelligence report: patterns, correlations, importance distribution."""
    async with metrics_collector.track("localmem_intel_report"):
        results = await intelligence_engine.run_detection()

        # Importance distribution per wing
        distribution = {}
        for w in config.wings:
            entries = await metadata_store.get_top_entries(wing=w, limit=1000)
            if entries:
                scores = [e["effective_score"] for e in entries]
                distribution[w] = {
                    "count": len(entries),
                    "avg_score": round(sum(e["score"] for e in entries) / len(entries), 3),
                    "avg_effective": round(sum(scores) / len(scores), 3),
                }

        return {
            "patterns": results,
            "importance_distribution": distribution,
        }


# =============================================================================
# Operations (2)
# =============================================================================


@mcp.tool()
async def localmem_health() -> dict[str, Any]:
    """Server health: uptime, store connectivity, entry counts, embedding info."""
    async with metrics_collector.track("localmem_health"):
        return await health_snapshot(
            config=config,
            embedder=embedder,
            vector_store=vector_store,
            metadata_store=metadata_store,
            graph_store=graph_store,
            start_time=server_start_time,
            worker=retention_worker,
        )


@mcp.tool()
async def localmem_metrics() -> dict[str, Any]:
    """Runtime metrics: tool call counts, latencies, uptime."""
    async with metrics_collector.track("localmem_metrics"):
        return metrics_collector.snapshot()


# =============================================================================
# Server Lifecycle
# =============================================================================


async def initialize_stores(cfg: LocalmemConfig) -> None:
    global config, embedder, vector_store, graph_store, metadata_store, wake_up
    global intelligence_engine, metrics_collector, server_start_time, retention_worker

    config = cfg
    server_start_time = time.time()
    metrics_collector = MetricsCollector()
    embedder = Embedder(config)
    embedder.load()

    vector_store = VectorStore(config, embedder)
    graph_store = GraphStore(config)
    metadata_store = MetadataStore(config)
    wake_up = WakeUp(config, vector_store, metadata_store)
    intelligence_engine = IntelligenceEngine(
        config, vector_store, metadata_store, graph_store
    )

    await vector_store.initialize()
    await graph_store.initialize()
    await metadata_store.initialize()

    for wing in config.all_wings():
        write_locks[wing] = asyncio.Lock()

    if config.retention.enabled:
        from .worker import BackgroundWorker
        retention_worker = BackgroundWorker(
            config, vector_store, metadata_store, graph_store=graph_store,
        )
        retention_worker.start()

    logger.info("LOCALMEM stores initialized")


async def shutdown_stores() -> None:
    if retention_worker is not None:
        await retention_worker.stop()
    await graph_store.shutdown()
    logger.info("LOCALMEM stores shut down")


def create_server(config_path: str = "localmem.yaml") -> FastMCP:
    """Resolve config and arm the lifespan, returning the configured server.

    Sets `_runtime_config` so the `_lifespan` context manager (registered on
    the module-level `mcp`) initializes stores at startup and tears them down
    at shutdown.
    """
    global _runtime_config
    _runtime_config = load_config(config_path)
    return mcp


async def run_async(config_path: str = "localmem.yaml") -> None:
    """Async entry — use this when already inside an asyncio loop (CLI dispatch).

    FastMCP 3's blocking `server.run()` calls `anyio.run()`, which collides
    with an outer `asyncio.run()` in the CLI command runner. The HTTP variants
    expose async entry points (`run_http_async`) that compose cleanly.
    """
    setup_logging(load_config(config_path))
    server = create_server(config_path)
    cfg = _runtime_config
    assert cfg is not None
    await server.run_http_async(
        transport="sse",
        host=cfg.server.host,
        port=cfg.server.port,
    )


def run() -> None:
    """Sync entry — used by the `localmem-serve` console script (no outer loop)."""
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "localmem.yaml"
    asyncio.run(run_async(config_path))
