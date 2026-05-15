"""LOCALMEM dashboard FastAPI app.

Read-only REST + WebSocket façade over the live stores. Bound to
127.0.0.1:8782 by default — localhost-only, no auth required.
For remote / multi-machine use, enable dashboard.auth_enabled + api_key.

The app can run in two modes:
  - Standalone:  `localmem-dashboard` boots its own stores against on-disk paths.
  - Embedded:    same-process sidecar of `localmem-serve`; shares stores
                  by calling `attach_stores()` after MCP init.

Note: Qdrant local mode does not support concurrent writers from multiple
processes. Run dashboard EITHER standalone (no MCP server running) OR
embedded with the MCP server, never both at once.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from ..archiver import Archiver
from ..config import LocalmemConfig, load_config
from ..consolidator import Consolidator
from ..embedder import Embedder
from ..graph_store import GraphStore
from ..health import health_snapshot
from ..intelligence import IntelligenceEngine
from ..logging_config import setup_logging
from ..metadata_store import MetadataStore
from ..metrics import MetricsCollector
from ..models import Entry, SearchQuery
from ..prometheus_exposition import PROMETHEUS_CONTENT_TYPE, build_prometheus_text
from ..vector_store import VectorStore
from ..worker import BackgroundWorker
from .models import (
    AlertResponse,
    ArchiveStatsResponse,
    DiaryResponse,
    EmbeddingInfo,
    EntryDetail,
    EntryListResponse,
    EntrySummary,
    GraphEdge,
    GraphNode,
    GraphStats,
    GraphSubgraph,
    HealthResponse,
    LatencyStats,
    MetricsResponse,
    PinRequest,
    PinResponse,
    RoomInfo,
    RunRequest,
    SearchHit,
    SearchRequest,
    SearchResponse,
    TaxonomyResponse,
    ToolMetric,
    TripleResponse,
    WorkerStatusResponse,
)
from .websocket import ConnectionManager

logger = logging.getLogger(__name__)


# ── Shared state ──────────────────────────────────────────────────────


class AppState:
    config: LocalmemConfig
    embedder: Embedder
    vector_store: VectorStore
    metadata_store: MetadataStore
    graph_store: GraphStore
    intelligence_engine: IntelligenceEngine
    metrics: MetricsCollector
    start_time: float
    ws_manager: ConnectionManager
    worker: BackgroundWorker | None = None
    _push_task: asyncio.Task[None] | None = None


state = AppState()


async def _initialize_stores(cfg: LocalmemConfig) -> None:
    """Standalone init — used when dashboard runs without a sibling MCP server."""
    state.config = cfg
    state.embedder = Embedder(cfg)
    state.embedder.load()

    state.vector_store = VectorStore(cfg, state.embedder)
    await state.vector_store.initialize()

    state.metadata_store = MetadataStore(cfg)
    await state.metadata_store.initialize()

    state.graph_store = GraphStore(cfg)
    await state.graph_store.initialize()

    state.intelligence_engine = IntelligenceEngine(
        cfg, state.vector_store, state.metadata_store, state.graph_store
    )


def attach_stores(
    cfg: LocalmemConfig,
    *,
    embedder: Embedder,
    vector_store: VectorStore,
    metadata_store: MetadataStore,
    graph_store: GraphStore,
    intelligence_engine: IntelligenceEngine,
    metrics: MetricsCollector,
    start_time: float,
) -> None:
    """Attach pre-initialized stores (embedded mode)."""
    state.config = cfg
    state.embedder = embedder
    state.vector_store = vector_store
    state.metadata_store = metadata_store
    state.graph_store = graph_store
    state.intelligence_engine = intelligence_engine
    state.metrics = metrics
    state.start_time = start_time


# ── Auth dependency ───────────────────────────────────────────────────


def auth_dep(
    authorization: str | None = Header(default=None),
) -> None:
    if not state.config.dashboard.auth_enabled:
        return
    expected = state.config.dashboard.api_key
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="dashboard.auth_enabled=true but api_key is not configured",
        )
    expected_header = f"Bearer {expected}"
    provided = authorization or ""
    if not hmac.compare_digest(provided, expected_header):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Lifespan ──────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # If stores aren't attached yet (standalone mode), initialize them now
    # using the config stashed on app.state by the caller.
    if not hasattr(state, "vector_store"):
        cfg_on_app = getattr(app.state, "cfg", None)
        if cfg_on_app is None:
            raise RuntimeError(
                "Dashboard started without stores attached and without "
                "cfg on app.state — call attach_stores() or set app.state.cfg."
            )
        await _initialize_stores(cfg_on_app)
        state.metrics = MetricsCollector()
        state.start_time = time.time()

    state.ws_manager = ConnectionManager()
    state._push_task = asyncio.create_task(_push_loop())

    if state.config.retention.enabled:
        consolidator = Consolidator(
            state.config, state.vector_store, state.metadata_store,
            graph_store=state.graph_store,
        )
        archiver = Archiver(state.config, state.vector_store, state.metadata_store)
        state.worker = BackgroundWorker(
            state.config, state.vector_store, state.metadata_store,
            graph_store=state.graph_store,
            consolidator=consolidator, archiver=archiver,
        )
        state.worker.start()

    try:
        yield
    finally:
        if state.worker is not None:
            try:
                await state.worker.stop()
            except Exception as e:
                logger.debug(f"worker shutdown: {e}")
        if state._push_task:
            state._push_task.cancel()
            try:
                await state._push_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"push task shutdown: {e}")
        try:
            await state.graph_store.shutdown()
        except Exception as e:
            logger.debug(f"graph store shutdown: {e}")


async def _push_loop() -> None:
    interval = state.config.dashboard.ws_push_interval_seconds
    seen_alerts: deque[str] = deque(maxlen=1000)
    last_health_hash: int | None = None
    last_metrics_hash: int | None = None
    while True:
        try:
            await asyncio.sleep(interval)
            if state.ws_manager.client_count == 0:
                continue

            health = await _health_snapshot()
            h_hash = hash(json.dumps(health, sort_keys=True, default=str))
            if h_hash != last_health_hash:
                last_health_hash = h_hash
                await state.ws_manager.broadcast("health", health)

            metrics = _metrics_snapshot()
            m_hash = hash(json.dumps(metrics, sort_keys=True, default=str))
            if m_hash != last_metrics_hash:
                last_metrics_hash = m_hash
                await state.ws_manager.broadcast("metrics", metrics)

            alerts = await state.vector_store.scroll(
                wing="shared",
                room=state.config.intelligence.alert_room,
                limit=20,
            )
            seen_set = set(seen_alerts)
            new_alerts = [a for a in alerts if a.id not in seen_set]
            if new_alerts:
                for a in new_alerts:
                    seen_alerts.append(a.id)
                await state.ws_manager.broadcast(
                    "alerts",
                    {"new": [_alert_payload(a) for a in new_alerts]},
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"push loop error: {e}")


# ── App factory ───────────────────────────────────────────────────────


def create_app(cfg: LocalmemConfig | None = None) -> FastAPI:
    app = FastAPI(
        title="LOCALMEM Dashboard API",
        version="0.5.1",
        lifespan=lifespan,
    )

    effective = cfg or getattr(state, "config", None)
    origins = effective.dashboard.cors_origins if effective else ["http://localhost:5173"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    _register_routes(app)
    return app


# ── Helpers ───────────────────────────────────────────────────────────


def _entry_type_value(e: Entry) -> str:
    et = e.entry_type
    return et.value if hasattr(et, "value") else str(et)


def _entry_summary(e: Entry) -> EntrySummary:
    return EntrySummary(
        id=e.id,
        wing=e.wing,
        room=e.room,
        agent_id=e.agent_id,
        entry_type=_entry_type_value(e),
        summary=e.summary,
        preview=(e.content or "")[:200],
        importance=e.importance,
        tags=e.tags,
        created_at=e.created_at,
        pinned=e.pinned,
        is_summary=e.is_summary,
    )


def _entry_detail(e: Entry) -> EntryDetail:
    return EntryDetail(
        id=e.id,
        wing=e.wing,
        room=e.room,
        agent_id=e.agent_id,
        entry_type=_entry_type_value(e),
        content=e.content,
        summary=e.summary,
        importance=e.importance,
        tags=e.tags,
        refs=e.refs,
        metadata=e.metadata,
        created_at=e.created_at,
        updated_at=e.updated_at,
        pinned=e.pinned,
        is_summary=e.is_summary,
    )


def _worker_status(s) -> WorkerStatusResponse:
    return WorkerStatusResponse(
        running=s.running,
        in_flight=s.in_flight,
        queue_size=s.queue_size,
        dirty_wings=s.dirty_wings,
        last_consolidation_at=s.last_consolidation_at,
        last_archive_at=s.last_archive_at,
    )


def _alert_payload(a: Entry) -> dict[str, Any]:
    return {
        "id": a.id,
        "content": a.content,
        "summary": a.summary,
        "importance": a.importance,
        "tags": a.tags,
        "metadata": a.metadata,
        "created_at": a.created_at,
    }


async def _health_snapshot() -> dict[str, Any]:
    return await health_snapshot(
        config=state.config,
        embedder=state.embedder,
        vector_store=state.vector_store,
        metadata_store=state.metadata_store,
        graph_store=state.graph_store,
        start_time=state.start_time,
        worker=state.worker,
    )


def _metrics_snapshot() -> dict[str, Any]:
    return state.metrics.snapshot()


# ── Route registration ────────────────────────────────────────────────


def _register_routes(app: FastAPI) -> None:

    @app.get("/api/health", response_model=HealthResponse, dependencies=[Depends(auth_dep)])
    async def health() -> HealthResponse:
        snap = await _health_snapshot()
        return HealthResponse(
            status=snap["status"],
            uptime_seconds=snap["uptime_seconds"],
            vector_store=snap["vector_store"],
            metadata_store=snap["metadata_store"],
            graph_store=snap["graph_store"],
            embedding=EmbeddingInfo(**snap["embedding"]),
        )

    @app.get("/api/metrics", response_model=MetricsResponse, dependencies=[Depends(auth_dep)])
    async def metrics() -> MetricsResponse:
        snap = _metrics_snapshot()
        tools = {
            name: ToolMetric(
                calls=v["calls"],
                errors=v["errors"],
                latency_ms=LatencyStats(**v["latency_ms"]),
            )
            for name, v in snap.get("tools", {}).items()
        }
        return MetricsResponse(
            uptime_seconds=snap["uptime_seconds"],
            total_calls=snap["total_calls"],
            total_errors=snap["total_errors"],
            tools=tools,
        )

    @app.get("/metrics", dependencies=[Depends(auth_dep)])
    async def prometheus_metrics() -> PlainTextResponse:
        worker_payload: dict[str, Any] | None = None
        if state.worker is not None:
            ws = state.worker.status()
            worker_payload = {
                "running": ws.running,
                "in_flight": ws.in_flight,
                "queue_size": ws.queue_size,
            }
        archive_payload: dict[str, Any] | None = None
        if state.config.retention.archive.enabled:
            archive_payload = Archiver(
                state.config, state.vector_store, state.metadata_store
            ).stats()
        body = build_prometheus_text(
            metrics=_metrics_snapshot(),
            health=await _health_snapshot(),
            worker=worker_payload,
            archive=archive_payload,
        )
        return PlainTextResponse(content=body, media_type=PROMETHEUS_CONTENT_TYPE)

    @app.get("/api/taxonomy", response_model=TaxonomyResponse, dependencies=[Depends(auth_dep)])
    async def taxonomy() -> TaxonomyResponse:
        wings = await state.metadata_store.list_wings()
        rooms = await state.metadata_store.list_rooms()
        return TaxonomyResponse(
            wings=wings,
            rooms=[RoomInfo(**r) for r in rooms],
        )

    @app.get("/api/entries", response_model=EntryListResponse, dependencies=[Depends(auth_dep)])
    async def list_entries(
        wing: str | None = Query(None),
        room: str | None = Query(None),
        tag: list[str] | None = Query(None),
        pinned: bool | None = Query(None),
        is_summary: bool | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> EntryListResponse:
        entries = await state.vector_store.scroll(
            wing=wing, room=room, tags=tag,
            pinned=pinned, is_summary=is_summary,
            limit=limit + offset,
        )
        total = await state.vector_store.count(
            wing=wing, room=room, pinned=pinned, is_summary=is_summary,
        )
        sliced = entries[offset : offset + limit]
        return EntryListResponse(
            entries=[_entry_summary(e) for e in sliced],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/entries/{entry_id}", response_model=EntryDetail, dependencies=[Depends(auth_dep)])
    async def get_entry(entry_id: str) -> EntryDetail:
        e = await state.vector_store.retrieve(entry_id)
        if not e:
            raise HTTPException(status_code=404, detail=f"entry {entry_id} not found")
        return _entry_detail(e)

    @app.post(
        "/api/entries/{entry_id}/pin",
        response_model=PinResponse,
        dependencies=[Depends(auth_dep)],
    )
    async def pin_entry(entry_id: str, body: PinRequest) -> PinResponse:
        entry = await state.vector_store.retrieve(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"entry {entry_id} not found")
        await state.metadata_store.set_pinned(entry_id, body.pinned, wing=entry.wing)
        ok = await state.vector_store.set_pinned(entry_id, body.pinned)
        if not ok:
            raise HTTPException(
                status_code=500,
                detail="vector store payload update failed",
            )
        return PinResponse(entry_id=entry_id, pinned=body.pinned)

    @app.post("/api/search", response_model=SearchResponse, dependencies=[Depends(auth_dep)])
    async def search(req: SearchRequest) -> SearchResponse:
        from ..models import EntryType
        sq = SearchQuery(
            query=req.query,
            wing=req.wing,
            room=req.room,
            tags=req.tags,
            entry_type=EntryType(req.entry_type) if req.entry_type else None,
            limit=req.limit,
        )
        results = await state.vector_store.search(sq)
        hits = [
            SearchHit(entry=_entry_summary(r.entry), score=r.score, source=r.source)
            for r in results
        ]
        return SearchResponse(hits=hits, query=req.query)

    @app.get("/api/graph/stats", response_model=GraphStats, dependencies=[Depends(auth_dep)])
    async def graph_stats() -> GraphStats:
        s = await state.graph_store.stats()
        return GraphStats(**s)

    @app.get("/api/graph/subgraph", response_model=GraphSubgraph, dependencies=[Depends(auth_dep)])
    async def graph_subgraph(
        node: str | None = Query(None, description="center node for neighbors op"),
        depth: int = Query(2, ge=1, le=5),
        limit: int = Query(200, ge=1, le=2000),
    ) -> GraphSubgraph:
        sub = await state.graph_store.subgraph(center=node, depth=depth, limit=limit)
        return GraphSubgraph(
            nodes=[GraphNode(**n) for n in sub["nodes"]],
            edges=[GraphEdge(**e) for e in sub["edges"]],
        )

    @app.get("/api/triples", response_model=list[TripleResponse], dependencies=[Depends(auth_dep)])
    async def triples(
        subject: str | None = Query(None),
        predicate: str | None = Query(None),
        object: str | None = Query(None, alias="object"),
        active_only: bool = Query(True),
    ) -> list[TripleResponse]:
        ts = await state.metadata_store.query_triples(
            subject=subject, predicate=predicate, obj=object, active_only=active_only
        )
        return [TripleResponse(**t.model_dump()) for t in ts]

    @app.get("/api/triples/timeline/{subject}/{predicate}", response_model=list[TripleResponse], dependencies=[Depends(auth_dep)])
    async def triple_timeline(subject: str, predicate: str) -> list[TripleResponse]:
        ts = await state.metadata_store.triple_timeline(subject, predicate)
        return [TripleResponse(**t.model_dump()) for t in ts]

    @app.get("/api/diaries", response_model=list[DiaryResponse], dependencies=[Depends(auth_dep)])
    async def diaries(
        agent_id: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        after: str | None = Query(None),
        before: str | None = Query(None),
    ) -> list[DiaryResponse]:
        entries = await state.metadata_store.read_diary(
            agent_id=agent_id, limit=limit, after=after, before=before
        )
        return [DiaryResponse(**e.model_dump()) for e in entries]

    @app.post(
        "/api/prune/run",
        response_model=WorkerStatusResponse,
        dependencies=[Depends(auth_dep)],
    )
    async def trigger_prune(req: RunRequest | None = None) -> WorkerStatusResponse:
        if state.worker is None:
            raise HTTPException(
                status_code=409,
                detail="retention is disabled (set retention.enabled=true)",
            )
        await state.worker.run_now(prune=True, archive=False)
        s = state.worker.status()
        return _worker_status(s)

    @app.post(
        "/api/archive/run",
        response_model=WorkerStatusResponse,
        dependencies=[Depends(auth_dep)],
    )
    async def trigger_archive(req: RunRequest | None = None) -> WorkerStatusResponse:
        if state.worker is None:
            raise HTTPException(
                status_code=409,
                detail="retention is disabled (set retention.enabled=true)",
            )
        await state.worker.run_now(prune=False, archive=True)
        s = state.worker.status()
        return _worker_status(s)

    @app.get(
        "/api/worker/status",
        response_model=WorkerStatusResponse,
        dependencies=[Depends(auth_dep)],
    )
    async def worker_status() -> WorkerStatusResponse:
        if state.worker is None:
            return WorkerStatusResponse(
                running=False, in_flight=False, queue_size=0, dirty_wings=[],
            )
        return _worker_status(state.worker.status())

    @app.get(
        "/api/archive/stats",
        response_model=ArchiveStatsResponse,
        dependencies=[Depends(auth_dep)],
    )
    async def archive_stats() -> ArchiveStatsResponse:
        archiver = Archiver(state.config, state.vector_store, state.metadata_store)
        st = archiver.stats()
        return ArchiveStatsResponse(**st)

    @app.get("/api/alerts", response_model=list[AlertResponse], dependencies=[Depends(auth_dep)])
    async def alerts(
        wing: str | None = Query(None),
        limit: int = Query(20, ge=1, le=200),
    ) -> list[AlertResponse]:
        alert_room = state.config.intelligence.alert_room
        raw = await state.vector_store.scroll(
            wing="shared", room=alert_room, limit=limit * 2
        )
        if wing:
            raw = [a for a in raw if wing in a.tags]
        return [AlertResponse(**_alert_payload(a)) for a in raw[:limit]]

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        # Bearer auth via Sec-WebSocket-Protocol subprotocol
        # ("bearer.<key>"), so the token never appears in the URL,
        # access logs, or referer headers. Falls back to ?token= query
        # param if no subprotocol is offered (kept only to surface a
        # clearer 1008 error on legacy clients — not for new use).
        accept_protocol: str | None = None
        if state.config.dashboard.auth_enabled:
            expected = state.config.dashboard.api_key or ""
            offered = [p.strip() for p in ws.scope.get("subprotocols") or []]
            authed = False
            for proto in offered:
                if proto.startswith("bearer.") and hmac.compare_digest(
                    proto[len("bearer."):], expected
                ):
                    authed = True
                    accept_protocol = proto
                    break
            if not authed:
                await ws.close(code=1008)
                return

        await state.ws_manager.connect(ws, subprotocol=accept_protocol)
        try:
            await ws.send_json({
                "topic": "system",
                "data": {"event": "connected", "topics": state.ws_manager.get_topics(ws)},
            })
            while True:
                msg = await ws.receive_json()
                action = msg.get("action")
                topics = msg.get("topics", [])
                if action == "subscribe":
                    await state.ws_manager.subscribe(ws, topics)
                elif action == "unsubscribe":
                    await state.ws_manager.unsubscribe(ws, topics)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"WS error: {e}")
        finally:
            await state.ws_manager.disconnect(ws)


# ── Standalone entry point ────────────────────────────────────────────


async def serve(cfg: LocalmemConfig) -> None:
    """Async serve — used by both the CLI entry and `localmem dashboard`."""
    import uvicorn

    app = create_app(cfg)
    app.state.cfg = cfg  # lifespan reads this to init stores standalone

    config = uvicorn.Config(
        app=app,
        host=cfg.dashboard.host,
        port=cfg.dashboard.port,
        log_level=cfg.logging.level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info(f"LOCALMEM dashboard on {cfg.dashboard.host}:{cfg.dashboard.port}")
    await server.serve()


def run() -> None:
    """CLI entry: `localmem-dashboard [config.yaml]`."""
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "localmem.yaml"
    cfg = load_config(config_path)
    setup_logging(cfg)
    asyncio.run(serve(cfg))
