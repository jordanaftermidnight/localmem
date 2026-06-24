"""Data models — entry types, events, queries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


# --- Enums ---

class EntryType(str, Enum):
    BEHAVIORAL_OBSERVATION = "behavioral_observation"
    INFERENCE_CHAIN = "inference_chain"
    ROUTING_DECISION = "routing_decision"
    DIARY = "diary"
    KNOWLEDGE_TRIPLE = "knowledge_triple"
    GENERIC = "generic"


# --- Core Entry ---

class Entry(BaseModel):
    id: str = Field(default_factory=_uuid)
    wing: str
    room: str
    agent_id: str
    entry_type: EntryType = EntryType.GENERIC
    content: str
    summary: str | None = None
    # Importance is bounded so a hostile client cannot dominate retrieval
    # rankings or evade retention by writing absurd values. Pydantic enforces
    # the range at instance construction and at MCP-tool argument coercion.
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    pinned: bool = False
    is_summary: bool = False


# --- Behavioral Observation ---

class BehavioralObservation(BaseModel):
    detector_id: str
    detector_name: str
    observation: str
    severity: float = 0.5
    context: dict[str, Any] = Field(default_factory=dict)
    co_occurring_detectors: list[str] = Field(default_factory=list)
    graph_refs: list[str] = Field(default_factory=list)


# --- Inference Chain ---

class ReasoningStep(BaseModel):
    step: int
    action: str
    result: str


class InferenceChain(BaseModel):
    chain_id: str = Field(default_factory=_uuid)
    query: str
    reasoning_steps: list[ReasoningStep] = Field(default_factory=list)
    conclusion: str
    confidence: float = 0.5
    context_entries_used: list[str] = Field(default_factory=list)


# --- Routing Decision ---

class ProviderHealth(BaseModel):
    status: str
    latency_ms: float
    error_rate: float


class AlternativeConsidered(BaseModel):
    provider: str
    model: str
    rejected_reason: str


class RoutingDecision(BaseModel):
    provider: str
    model: str
    reason: str
    alternatives_considered: list[AlternativeConsidered] = Field(default_factory=list)
    health_snapshot: dict[str, ProviderHealth] = Field(default_factory=dict)


# --- Knowledge Triple ---

class Triple(BaseModel):
    id: str = Field(default_factory=_uuid)
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    valid_from: str = Field(default_factory=_now)
    valid_to: str | None = None
    source_agent: str
    source_entry_id: str | None = None
    created_at: str = Field(default_factory=_now)
    superseded_by: str | None = None


# --- Events ---

class ContradictionEvent(BaseModel):
    old_triple: Triple
    new_triple: Triple
    subject: str
    predicate: str
    old_value: str
    new_value: str
    timestamp: str = Field(default_factory=_now)


# --- Diary ---

class DiaryEntry(BaseModel):
    id: str = Field(default_factory=_uuid)
    agent_id: str
    timestamp: str = Field(default_factory=_now)
    content: str
    mood: str | None = None
    tags: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


# --- Query Types ---

class SearchQuery(BaseModel):
    query: str
    wing: str | None = None
    room: str | None = None
    tags: list[str] | None = None
    entry_type: EntryType | None = None
    after: str | None = None
    before: str | None = None
    limit: int = 10


class GraphQuery(BaseModel):
    """Query for the behavioral pattern graph."""
    operation: str  # "path", "neighbors", "community", "centrality", "temporal"
    source_node: str | None = None
    target_node: str | None = None
    depth: int = 3
    start_time: str | None = None
    end_time: str | None = None


class SearchResult(BaseModel):
    entry: Entry
    score: float
    source: str = "hybrid"  # "dense", "sparse", "hybrid"


class WakeContext(BaseModel):
    agent_id: str
    l0_manifest: dict[str, Any]
    l1_entries: list[Entry]
    total_tokens_estimate: int


class PatternAlert(BaseModel):
    pattern_type: str  # tool_sequence, provider_preference, node_cluster, cross_wing
    source_wing: str
    description: str
    strength: float  # 0-1
    details: dict[str, Any] = Field(default_factory=dict)
    detected_at: str = Field(default_factory=_now)
