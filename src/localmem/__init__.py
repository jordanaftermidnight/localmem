"""localmem — local-first multi-agent memory MCP server.

Hybrid (dense + sparse) vector search via Qdrant, behavioral pattern graphs
via NetworkX, temporal knowledge triples and per-wing taxonomy in SQLite,
plus lifecycle management (consolidation + archive) — all served over MCP/SSE
with an optional read-only browser dashboard.
"""

__version__ = "0.1.0"

from .config import LocalmemConfig, load_config
from .contradiction import detect_and_resolve
from .embedder import Embedder
from .graph_store import GraphStore
from .metadata_store import MetadataStore
from .models import (
    BehavioralObservation,
    ContradictionEvent,
    DiaryEntry,
    Entry,
    EntryType,
    GraphQuery,
    InferenceChain,
    RoutingDecision,
    SearchQuery,
    SearchResult,
    Triple,
    WakeContext,
)
from .taxonomy import Taxonomy
from .vector_store import VectorStore
from .wake_up import WakeUp
