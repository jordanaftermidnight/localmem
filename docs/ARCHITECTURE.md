# localmem — Architecture

**Version:** 0.1.0
**Status:** Public release

---

## 1. Purpose

localmem is a local-first, multi-agent persistent memory system for LLM agents. It provides per-agent memory isolation, cross-agent discovery via a reserved `shared` wing, behavioral pattern graph reasoning, temporal knowledge triples, and hybrid (dense + sparse) semantic retrieval — all running on-device with zero cloud dependencies.

The example wing names used throughout this document (`router`, `tools`, `observer`) are illustrative — wings are configured in `localmem.yaml` and can be named anything. A reasonable starting layout assigns one wing per agent role (e.g., a routing/orchestrator wing, a tool-execution wing, a behavioral-observation wing) plus the implicit `shared` namespace for cross-agent context.

---

## 2. Design Principles

1. **Gardening, not engineering.** The system grows organically with the system's needs. No premature abstraction. Each component should be replaceable without rewiring the whole.

2. **Verbatim-first.** Store the original material. Derive compressed views on read. Lossy extraction (Mem0-style) destroys the reasoning context observer needs to trace behavioral emergence.

3. **Local-first, zero-cost.** No API calls in the write or read path. No cloud dependencies. Embedding inference runs on local models. Storage is SQLite + Qdrant local mode.

4. **Typed, not generic.** observer behavioral observations, tools inference chains, and router routing decisions are structurally different. Each gets a proper schema, not a universal "drawer."

5. **Multi-agent concurrent.** SSE transport, not stdio. Multiple agents connect simultaneously. Reads fan out freely; writes serialize per-namespace to avoid conflicts.

---

## 3. Spatial Taxonomy (borrowed from MemPalace, adapted)

The spatial metaphor serves human navigability, not retrieval mechanics. Retrieval uses embeddings + graph queries; the taxonomy provides organizational structure.

### 3.1 Hierarchy

```
Palace (localmem instance)
├── Wing: router
│   ├── Room: provider-routing
│   ├── Room: failover-decisions
│   ├── Room: health-snapshots
│   └── Room: configuration
├── Wing: tools
│   ├── Room: inference-chains
│   ├── Room: reasoning-traces
│   ├── Room: context-retrievals
│   └── Room: identity
├── Wing: observer
│   ├── Room: detector-observations
│   ├── Room: behavioral-patterns
│   ├── Room: correlation-events
│   └── Room: whitepaper-research
└── Wing: shared
    ├── Room: cross-agent-decisions
    ├── Room: system-events
    └── Room: agent-diaries
```

### 3.2 Naming Convention

- **Wings** = agent namespace or `shared` for cross-cutting concerns
- **Rooms** = functional domain within that agent's responsibility
- **Format**: `wing:room` (e.g., `observer:detector-observations`, `shared:agent-diaries`)
- Wings and rooms are created on first write (no pre-registration required)

### 3.3 Tunnels

When the same room name appears in multiple wings (e.g., `router:auth-migration` and `observer:auth-migration`), a tunnel query retrieves entries from all wings containing that room. This enables cross-agent discovery without explicit linking.

---

## 4. Layered Loading (L0–L3)

Adapted from MemPalace's most elegant design choice. Each agent loads context incrementally based on need, minimizing token cost at wake-up.

### L0 — Identity Manifest (~50 tokens)

A static YAML file per agent defining its role, capabilities, and critical constraints. Loaded unconditionally at every wake-up.

```yaml
# localmem/manifests/tools.yaml
agent: tools
role: Reasoning and inference layer between router and observer
capabilities:
  - multi-provider inference routing
  - reasoning chain construction
  - context-aware retrieval
constraints:
  - never route to providers flagged by router health monitor
  - preserve full reasoning trace for observer behavioral analysis
wake_rooms:
  - tools:inference-chains
  - tools:identity
```

### L1 — Critical Context (~120 tokens)

Top-K entries by `importance * recency_decay` from the agent's priority rooms. Auto-loaded after L0. Configurable K (default: 15 entries, compressed to ~120 tokens via summary views).

### L2 — Scoped Search (on-demand)

Triggered by agent query. Hybrid retrieval (dense + sparse) scoped to specified wings/rooms. Returns ranked results with source metadata.

### L3 — Full Verbatim Access (on-demand)

Direct retrieval of complete original entries by ID. Used when L2 results need full context — e.g., observer tracing the complete conversation that produced a behavioral observation.

---

## 5. Storage Architecture

### 5.1 Component Map

```
┌─────────────────────────────────────────────────┐
│                  FastMCP Server                   │
│              (SSE transport, port 8781)           │
├─────────────┬──────────────┬────────────────────┤
│ VectorStore │  GraphStore   │   MetadataStore    │
│  (Qdrant)   │ (NetworkX)   │    (SQLite)        │
│             │              │                     │
│ - hybrid    │ - multi-hop  │ - temporal triples  │
│   search    │   traversal  │ - agent diaries     │
│ - dense +   │ - community  │ - entry metadata    │
│   sparse    │   detection  │ - wing/room index   │
│ - re-rank   │ - causal     │ - importance scores │
│             │   chains     │ - timestamps        │
└─────────────┴──────────────┴────────────────────┘
        │              │               │
   qdrant_data/    graph.json     localmem.db
   (local dir)    (serialized)    (SQLite WAL)
```

### 5.2 Qdrant (Vector Search)

**Mode:** Local (in-process, no server). Uses `qdrant-client` with local storage path.

**Collections:**
- `localmem_entries` — all verbatim entries across all wings/rooms

**Schema per point:**
```json
{
  "id": "uuid",
  "vector": {
    "dense": [768-dim float array],
    "sparse": {"indices": [...], "values": [...]}
  },
  "payload": {
    "wing": "observer",
    "room": "detector-observations",
    "agent_id": "observer",
    "entry_type": "behavioral_observation",
    "content": "full verbatim text",
    "summary": "compressed view for L1 loading",
    "importance": 0.85,
    "created_at": "2026-04-08T14:30:00Z",
    "updated_at": "2026-04-08T14:30:00Z",
    "tags": ["coherence", "linguistic-shift", "detector-07"],
    "refs": ["entry-uuid-1", "entry-uuid-2"]
  }
}
```

**Search modes:**
- **Dense:** `bge-large-en-v1.5` (1024-dim) or `all-MiniLM-L6-v2` (384-dim) for speed. Configurable per deployment.
- **Sparse:** BM25-style sparse vectors via `fastembed` or manual TF-IDF. Enables keyword-sensitive retrieval tools needs.
- **Hybrid fusion:** Reciprocal Rank Fusion (RRF) combining dense and sparse results. Qdrant supports this natively.
- **Filtering:** Payload filters on `wing`, `room`, `agent_id`, `entry_type`, `tags`, and timestamp ranges.

### 5.3 NetworkX (Graph Reasoning)

**Purpose:** Behavioral pattern graphs for observer's detector correlations. Not a general-purpose knowledge graph — specifically for multi-hop reasoning about detector relationships and temporal co-occurrence.

**Graph structure:**
```python
# Nodes
G.add_node("observation:uuid-1", {
    "type": "observation",
    "detector": "coherence",
    "timestamp": "2026-04-08T14:30:00Z",
    "severity": 0.85,
    "entry_ref": "qdrant-point-uuid"
})

G.add_node("pattern:uuid-2", {
    "type": "pattern",
    "name": "coherence-linguistic-cooccurrence",
    "first_seen": "2026-04-01T00:00:00Z",
    "frequency": 12
})

# Edges
G.add_edge("observation:uuid-1", "observation:uuid-3", {
    "relation": "co_occurred_with",
    "time_delta_seconds": 45,
    "confidence": 0.92
})

G.add_edge("observation:uuid-1", "pattern:uuid-2", {
    "relation": "instance_of"
})
```

**Supported queries:**
- `shortest_path(obs_A, obs_B)` — causal chain tracing
- `bfs/dfs(node, depth=N)` — neighborhood exploration
- `community_detection(algorithm="louvain")` — detector clustering
- `temporal_subgraph(start, end)` — time-windowed graph extraction
- `degree_centrality()` / `betweenness_centrality()` — pattern importance ranking

**Persistence:** JSON serialization via `nx.node_link_data()` / `nx.node_link_graph()`. Written to disk on mutation with debounce (max once per 5 seconds). For observer's scale (hundreds to low thousands of nodes), this is more than sufficient.

### 5.4 SQLite (Metadata & Structured Data)

**Mode:** WAL (Write-Ahead Logging) for concurrent read access.

**Tables:**

```sql
-- Temporal knowledge triples (adapted from MemPalace, with contradiction detection)
CREATE TABLE triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    valid_from TEXT NOT NULL,  -- ISO 8601
    valid_to TEXT,             -- NULL = still valid
    source_agent TEXT NOT NULL,
    source_entry_id TEXT,      -- ref to Qdrant point
    created_at TEXT NOT NULL,
    superseded_by TEXT,        -- FK to triples.id (contradiction chain)
    UNIQUE(subject, predicate, valid_from)
);
CREATE INDEX idx_triples_subject ON triples(subject);
CREATE INDEX idx_triples_predicate ON triples(predicate);
CREATE INDEX idx_triples_valid ON triples(valid_from, valid_to);

-- Agent diary entries
CREATE TABLE diary (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    mood TEXT,                 -- optional agent self-report
    tags TEXT,                 -- JSON array
    references TEXT            -- JSON array of entry IDs
);
CREATE INDEX idx_diary_agent ON diary(agent_id, timestamp);

-- Wing/room registry (auto-populated on first write)
CREATE TABLE taxonomy (
    wing TEXT NOT NULL,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    entry_count INTEGER DEFAULT 0,
    last_written TEXT,
    PRIMARY KEY (wing, room)
);

-- Importance scoring (decayed over time, boosted by access)
CREATE TABLE importance (
    entry_id TEXT PRIMARY KEY,
    base_score REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    decay_rate REAL DEFAULT 0.01  -- per-day decay
);
```

**Contradiction detection:** On triple insertion, query for existing triples with the same `(subject, predicate)` where `valid_to IS NULL`. If found and the new object differs, mark the old triple's `valid_to` to now and set its `superseded_by` to the new triple's ID. Return a contradiction event to the calling agent.

---

## 6. MCP Server (FastMCP over SSE)

### 6.1 Transport

SSE (Server-Sent Events) on `http://localhost:8781/mcp`. Multiple concurrent clients supported. Each agent maintains its own SSE connection.

### 6.2 Tool Inventory (17 tools)

**Memory Operations (6):**
| Tool | Description |
|---|---|
| `localmem_wake` | Load L0 manifest + L1 critical context for an agent. Returns combined context blob. |
| `localmem_store` | Write a new entry (verbatim + metadata) to a wing:room. Auto-generates embedding. |
| `localmem_search` | Hybrid search (dense + sparse + filter). Scoped by wing/room/tags/time. Returns ranked results. |
| `localmem_retrieve` | Get full verbatim entry by ID (L3 access). |
| `localmem_update` | Update metadata (importance, tags, refs) on an existing entry. |
| `localmem_delete` | Soft-delete an entry (marks inactive, preserves for audit). |

**Graph Operations (5):**
| Tool | Description |
|---|---|
| `localmem_graph_add` | Add node or edge to the behavioral pattern graph. |
| `localmem_graph_query` | Run a graph query (path, neighbors, community, centrality). |
| `localmem_graph_temporal` | Extract time-windowed subgraph. |
| `localmem_graph_patterns` | List detected patterns by frequency/centrality. |
| `localmem_graph_stats` | Node/edge counts, connected components, density. |

**Knowledge Operations (3):**
| Tool | Description |
|---|---|
| `localmem_kg_add` | Add a temporal triple. Returns contradiction event if applicable. |
| `localmem_kg_query` | Query triples by subject/predicate/object with temporal filtering. |
| `localmem_kg_timeline` | Get the full history of a subject's predicate changes over time. |

**System Operations (3):**
| Tool | Description |
|---|---|
| `localmem_diary_write` | Write an agent diary entry. |
| `localmem_diary_read` | Read diary entries (own or other agents') with filters. |
| `localmem_tunnel` | Cross-wing room query — find entries from all wings matching a room name. |

---

## 7. Entry Types & Schemas

### 7.1 observer — Behavioral Observation

```json
{
  "entry_type": "behavioral_observation",
  "detector_id": "coherence-07",
  "detector_name": "Semantic Coherence Analyzer",
  "observation": "Response coherence score dropped to 0.42 during ethical dilemma prompt, recovering to 0.89 within 3 exchanges",
  "severity": 0.85,
  "context": {
    "prompt_category": "ethical-dilemma",
    "model": "claude-sonnet-4-20250514",
    "session_id": "sess-uuid"
  },
  "co_occurring_detectors": ["linguistic-shift-03", "hesitation-pattern-11"],
  "graph_refs": ["observation:uuid-co1", "observation:uuid-co2"]
}
```

### 7.2 tools — Inference Chain

```json
{
  "entry_type": "inference_chain",
  "chain_id": "chain-uuid",
  "query": "Should we route this prompt to Claude or GPT-4 given the ethical sensitivity?",
  "reasoning_steps": [
    {"step": 1, "action": "Retrieved router provider health", "result": "Both providers healthy"},
    {"step": 2, "action": "Checked observer ethical sensitivity flag", "result": "High sensitivity detected"},
    {"step": 3, "action": "Applied routing policy", "result": "Claude preferred for ethical content"}
  ],
  "conclusion": "Route to Claude Sonnet via Anthropic API",
  "confidence": 0.94,
  "context_entries_used": ["entry-uuid-1", "entry-uuid-2", "entry-uuid-3"]
}
```

### 7.3 router — Routing Decision

```json
{
  "entry_type": "routing_decision",
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "reason": "Primary provider healthy, latency 245ms, cost tier 2",
  "alternatives_considered": [
    {"provider": "openai", "model": "gpt-4o", "rejected_reason": "Higher latency (380ms)"}
  ],
  "health_snapshot": {
    "anthropic": {"status": "healthy", "latency_ms": 245, "error_rate": 0.002},
    "openai": {"status": "healthy", "latency_ms": 380, "error_rate": 0.005}
  }
}
```

---

## 8. Embedding Strategy

### 8.1 Model Selection

| Model | Dimensions | Speed | Quality | Use Case |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | 384 | Fast | Good | Default for development, diary entries, logs |
| `bge-large-en-v1.5` | 1024 | Moderate | Excellent | tools reasoning retrieval, observer observations |
| `nomic-embed-text-v1.5` | 768 | Moderate | Excellent | Alternative if BGE unavailable |

**Configuration:** Model is set per-collection or globally in config. Switching models requires re-embedding (migration script provided).

### 8.2 Sparse Vectors

Generated via `fastembed` (Qdrant's companion library) or a simple TF-IDF vectorizer trained on the corpus. Sparse vectors enable keyword-sensitive retrieval — critical when tools queries for specific technical terms that dense embeddings might miss.

### 8.3 Hybrid Fusion

Qdrant's native Reciprocal Rank Fusion (RRF):
```python
# Pseudocode
results = qdrant.query_points(
    collection_name="localmem_entries",
    prefetch=[
        Prefetch(query=dense_vector, using="dense", limit=20),
        Prefetch(query=sparse_vector, using="sparse", limit=20),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    limit=10,
    query_filter=Filter(must=[
        FieldCondition(key="wing", match=MatchValue(value="tools")),
    ])
)
```

---

## 9. Concurrency Model

### 9.1 Read Path

All reads are lock-free:
- Qdrant local mode supports concurrent reads natively
- SQLite WAL mode supports unlimited concurrent readers
- NetworkX graph is read from an in-memory snapshot (copy-on-read if mutation in progress)

### 9.2 Write Path

Writes serialize per-namespace:
- A `write_lock` per wing prevents interleaving of writes to the same namespace
- Different agents writing to different wings proceed in parallel
- Writes to `shared:*` rooms acquire a global shared-write lock

```python
# Write lock structure
locks = {
    "router": asyncio.Lock(),
    "tools": asyncio.Lock(),
    "observer": asyncio.Lock(),
    "shared": asyncio.Lock(),
}

async def store_entry(wing: str, room: str, entry: Entry):
    async with locks[wing]:
        # 1. Generate embedding
        # 2. Write to Qdrant
        # 3. Update SQLite metadata
        # 4. Update taxonomy
        # 5. Optionally update graph
```

### 9.3 Graph Mutations

NetworkX is not thread-safe. All graph mutations go through a dedicated `GraphStore` that uses an `asyncio.Lock()` and debounced persistence (write to disk at most once per 5 seconds, or on shutdown).

---

## 10. Contradiction Detection

When `localmem_kg_add` receives a new triple that conflicts with an active triple:

1. Query existing: `SELECT * FROM triples WHERE subject=? AND predicate=? AND valid_to IS NULL`
2. If found and `object != new_object`:
   a. Set old triple's `valid_to = now()`
   b. Set old triple's `superseded_by = new_triple.id`
   c. Insert new triple with `valid_from = now()`
   d. Return `ContradictionEvent(old_triple, new_triple)` to the calling agent
3. The calling agent (typically tools) decides how to handle: log, alert, or reconcile.

This directly addresses MemPalace's known gap of silently accumulating conflicting facts.

---

## 11. Agent Diary System

Each agent writes structured diary entries capturing its operational state, decisions, and reflections. Other agents can read diaries for cross-agent awareness.

```python
# observer diary entry example
await localmem_diary_write(
    agent_id="observer",
    content="Detector 07 (coherence) has flagged 3 anomalies in the last hour, "
            "all during ethical dilemma prompts. Correlates with detector 03 "
            "(linguistic shift) in 2 of 3 cases. Adding co-occurrence edge to graph.",
    tags=["coherence", "linguistic-shift", "correlation", "hourly-review"],
    references=["obs:uuid-1", "obs:uuid-2", "obs:uuid-3"]
)
```

Diary entries are also embedded and searchable via `localmem_search` — they live in the `shared:agent-diaries` room with the `agent_id` in payload for filtering.

---

## 12. File Structure

```
localmem/
├── docs/
│   ├── ARCHITECTURE.md          # This document
│   └── MCP_TOOLS.md             # Detailed tool schemas
├── src/
│   └── localmem/
│       ├── __init__.py
│       ├── config.py             # Configuration management
│       ├── models.py             # Pydantic schemas for entries, events, queries
│       ├── vector_store.py       # Qdrant wrapper (hybrid search, embedding)
│       ├── graph_store.py        # NetworkX wrapper (behavioral patterns)
│       ├── metadata_store.py     # SQLite wrapper (triples, diaries, taxonomy)
│       ├── taxonomy.py           # Wing/room management, tunnel queries
│       ├── wake_up.py            # L0–L3 layered loading logic
│       ├── contradiction.py      # Contradiction detection engine
│       ├── embedder.py           # Local embedding model management
│       ├── mcp_server.py         # FastMCP SSE server (17 tools)
│       └── cli.py                # CLI for admin/debug operations
├── manifests/
│   ├── router.yaml
│   ├── tools.yaml
│   └── observer.yaml
├── tests/
│   ├── test_vector_store.py
│   ├── test_graph_store.py
│   ├── test_metadata_store.py
│   ├── test_contradiction.py
│   └── test_mcp_server.py
├── data/                         # Runtime data (gitignored)
│   ├── qdrant/
│   ├── graph.json
│   └── localmem.db
├── pyproject.toml
└── README.md
```

---

## 13. Dependencies

```toml
[project]
name = "localmem"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "qdrant-client>=1.12.0",
    "fastembed>=0.4.0",
    "sentence-transformers>=3.0.0",
    "networkx>=3.3",
    "fastmcp>=2.0.0",
    "pydantic>=2.0.0",
    "pyyaml>=6.0",
    "aiosqlite>=0.20.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
]
```

---

## 14. Configuration

```yaml
# localmem.yaml
server:
  host: "localhost"
  port: 8781
  transport: "sse"

storage:
  base_path: "./data"
  qdrant_path: "./data/qdrant"
  sqlite_path: "./data/localmem.db"
  graph_path: "./data/graph.json"

embedding:
  model: "all-MiniLM-L6-v2"      # or "bge-large-en-v1.5" for production
  sparse_model: "Qdrant/bm25"     # via fastembed
  device: "cpu"                    # or "cuda", "mps"

loading:
  l1_top_k: 15
  l1_max_tokens: 120
  decay_rate_per_day: 0.01

graph:
  persistence_debounce_seconds: 5
  max_community_size: 50

concurrency:
  write_timeout_seconds: 30
```

---

## 15. Migration & Evolution Path

### Phase 1 (Now): Foundation
- Core stores (vector, graph, metadata)
- MCP server with 17 tools
- L0–L3 loading
- Basic contradiction detection
- Manual testing with Claude Code CLI

### Phase 2: Integration
- AG2 MCP adapter for observer's AutoGen agents
- router connection management (proxy mode)
- tools reasoning chain auto-logging
- Automated test suite

### Phase 3: Intelligence
- Importance scoring with access-based boosting and time decay
- Automatic pattern detection in graph (anomaly flagging)
- Cross-agent correlation alerts
- Dashboard via dockview (ties into existing observer dashboard spec)

### Phase 4: Scale (if needed)
- Qdrant server mode (separate process) for larger corpora
- Neo4j migration for graph if NetworkX hits limits (>100K nodes)
- Multi-instance localmem for distributed deployments

---

## 16. Open Questions

1. **Embedding model choice:** Start with MiniLM for speed or BGE-large for quality? Recommendation: MiniLM for Phase 1, migrate to BGE-large in Phase 2 when retrieval quality becomes measurable.

2. **Graph persistence format:** JSON (human-readable, larger) or pickle (faster, binary)? Recommendation: JSON for Phase 1 (debuggability), with pickle option configurable.

3. **AAAK-style compression for L1:** Worth implementing a compressed shorthand for L1 context loading? MemPalace's version lost 12% retrieval accuracy. Recommendation: Skip AAAK. Use LLM-generated summaries stored alongside verbatim originals. The summary is the "compressed view" — generated once at write time, not on every read.

4. **Agent authentication:** Should agents authenticate to the MCP server? For local-only deployment, probably unnecessary. For multi-machine setups (Phase 4), add bearer token auth.

---

*"Memory is not an instrument for exploring the past, but its theatre."*
— Walter Benjamin
