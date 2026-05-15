"""Shared health-snapshot logic used by both the MCP tool and the REST endpoint."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import LocalmemConfig
from .embedder import Embedder
from .graph_store import GraphStore
from .metadata_store import MetadataStore
from .vector_store import VectorStore


async def health_snapshot(
    *,
    config: LocalmemConfig,
    embedder: Embedder,
    vector_store: VectorStore,
    metadata_store: MetadataStore,
    graph_store: GraphStore,
    start_time: float,
    worker: Any | None = None,
) -> dict[str, Any]:
    target_wings = config.all_wings()
    try:
        counts = dict(
            zip(
                target_wings,
                await asyncio.gather(*(vector_store.count(wing=w) for w in target_wings)),
            )
        )
        vs_status = "ok"
    except Exception as e:
        counts = {}
        vs_status = f"err: {e}"

    try:
        wings = await metadata_store.list_wings()
        ms_status = "ok"
    except Exception as e:
        wings = []
        ms_status = f"err: {e}"

    try:
        gs = await graph_store.stats()
        gs_status = "ok"
    except Exception as e:
        gs = {}
        gs_status = f"err: {e}"

    all_ok = vs_status == "ok" and ms_status == "ok" and gs_status == "ok"
    snapshot: dict[str, Any] = {
        "status": "healthy" if all_ok else "degraded",
        "uptime_seconds": round(time.time() - start_time, 1),
        "vector_store": {
            "status": vs_status,
            "entries": counts,
            "mode": config.storage.qdrant_mode,
            "url": (
                config.storage.qdrant_url
                if config.storage.qdrant_mode == "server"
                else None
            ),
        },
        "metadata_store": {"status": ms_status, "wings": wings},
        "graph_store": {"status": gs_status, **gs},
        "embedding": {
            "model": config.embedding.model,
            "device": embedder.resolved_device,
            "sparse": embedder.has_sparse,
        },
        "retention": {
            "enabled": config.retention.enabled,
        },
    }
    if worker is not None and config.retention.enabled:
        try:
            ws = worker.status()
            snapshot["retention"].update({
                "worker_running": ws.running,
                "worker_in_flight": ws.in_flight,
                "queue_size": ws.queue_size,
                "dirty_wings": ws.dirty_wings,
                "last_consolidation_at": ws.last_consolidation_at,
                "last_archive_at": ws.last_archive_at,
            })
        except Exception as e:
            snapshot["retention"]["worker_error"] = str(e)
    return snapshot
