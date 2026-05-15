"""Dashboard API models — serialization layer for REST + WebSocket.

Keeps qdrant/networkx internals out of the wire format.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StoreStatus(BaseModel):
    status: str
    detail: str | None = None


class EmbeddingInfo(BaseModel):
    model: str
    device: str
    sparse: bool


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded"
    uptime_seconds: float
    vector_store: dict[str, Any]
    metadata_store: dict[str, Any]
    graph_store: dict[str, Any]
    embedding: EmbeddingInfo


class LatencyStats(BaseModel):
    avg: float
    p50: float
    p95: float
    p99: float


class ToolMetric(BaseModel):
    calls: int
    errors: int
    latency_ms: LatencyStats


class MetricsResponse(BaseModel):
    uptime_seconds: float
    total_calls: int
    total_errors: int
    tools: dict[str, ToolMetric]


class RoomInfo(BaseModel):
    wing: str
    room: str
    entry_count: int
    last_written: str | None = None


class TaxonomyResponse(BaseModel):
    wings: list[str]
    rooms: list[RoomInfo]


class EntrySummary(BaseModel):
    id: str
    wing: str
    room: str
    agent_id: str
    entry_type: str
    summary: str | None = None
    preview: str  # first 200 chars of content
    importance: float
    tags: list[str]
    created_at: str
    pinned: bool = False
    is_summary: bool = False


class EntryDetail(BaseModel):
    id: str
    wing: str
    room: str
    agent_id: str
    entry_type: str
    content: str
    summary: str | None = None
    importance: float
    tags: list[str]
    refs: list[str]
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    pinned: bool = False
    is_summary: bool = False


class PinRequest(BaseModel):
    pinned: bool


class PinResponse(BaseModel):
    entry_id: str
    pinned: bool


class RunRequest(BaseModel):
    wings: list[str] | None = None
    prune: bool = True
    archive: bool = True


class WorkerStatusResponse(BaseModel):
    running: bool
    in_flight: bool
    queue_size: int
    dirty_wings: list[str]
    last_consolidation_at: str | None = None
    last_archive_at: str | None = None


class ArchiveStatsResponse(BaseModel):
    path: str
    exists: bool
    total_files: int = 0
    total_bytes: int = 0
    wings: dict[str, dict[str, int]] = Field(default_factory=dict)


class EntryListResponse(BaseModel):
    entries: list[EntrySummary]
    total: int
    limit: int
    offset: int


class SearchRequest(BaseModel):
    query: str
    wing: str | None = None
    room: str | None = None
    tags: list[str] | None = None
    entry_type: str | None = None
    limit: int = 20


class SearchHit(BaseModel):
    entry: EntrySummary
    score: float
    source: str  # "dense" | "sparse" | "hybrid"


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    query: str


class GraphStats(BaseModel):
    nodes: int
    edges: int
    density: float
    weakly_connected_components: int


class GraphNode(BaseModel):
    id: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphSubgraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class TripleResponse(BaseModel):
    id: str
    subject: str
    predicate: str
    object: str
    confidence: float
    source_agent: str
    source_entry_id: str | None = None
    valid_from: str
    valid_to: str | None = None
    created_at: str


class DiaryResponse(BaseModel):
    id: str
    agent_id: str
    timestamp: str
    content: str
    mood: str | None = None
    tags: list[str]
    references: list[str]


class AlertResponse(BaseModel):
    id: str
    content: str
    summary: str | None = None
    importance: float
    tags: list[str]
    metadata: dict[str, Any]
    created_at: str


class WSMessage(BaseModel):
    topic: str  # "health" | "metrics" | "alerts" | "entries" | "logs" | "system"
    data: dict[str, Any]
    timestamp: str
