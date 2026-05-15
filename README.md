# localmem

**Local-first multi-agent memory MCP server.** Persistent storage for LLM
agents — hybrid (dense + sparse) vector search, behavioral pattern graphs,
temporal knowledge triples, layered wake-up context, lifecycle management,
and a read-only browser dashboard. All on-device, no cloud dependencies.

Exposes its functionality over the Model Context Protocol (SSE transport), so
any MCP-capable client — Claude Code, Cursor, Continue, custom agents — can
read and write memory through a single shared server.

## Why

Most "memory" for LLM agents is either a flat key-value store or a
single-agent knowledge graph. Real agent systems need more:

- **Per-agent namespaces.** Each agent's notes, decisions, and observations
  stay in its own wing. A reserved `shared` wing carries cross-agent context.
- **Hybrid retrieval.** Dense embeddings catch semantic matches, sparse BM25
  catches exact terms, RRF fuses both. Either signal alone misses too much.
- **Behavioral graphs.** Some relationships live in entries; others live in
  the connections between them — co-occurrence, sequence, community structure.
- **Temporal knowledge.** Facts change. Knowledge triples track validity
  windows and surface contradictions automatically.
- **Graceful forgetting.** Three-tier lifecycle (hot → warm summaries →
  cold compressed archive) keeps the working set fast without losing history.
- **Token-aware loading.** Layered wake-up context (L0 manifest → L1 critical
  → L2 scoped search → L3 verbatim) gives an agent ~170 tokens of high-signal
  context without pulling the whole store.

## Quick start

```bash
git clone https://github.com/jordanaftermidnight/localmem.git
cd localmem
pip install -e ".[dev]"

# 1. Edit localmem.yaml — at minimum list your agent wings:
#      wings:
#        - my_assistant
#
# 2. Start the MCP server:
localmem serve                  # SSE on http://localhost:8781

# 3. (Optional) Start the read-only dashboard:
pip install -e ".[dashboard]"
localmem dashboard              # REST + WS on http://localhost:8782
( cd dashboard && npm install && npm run dev )   # UI on http://localhost:5173
```

Connect any MCP client to `http://localhost:8781/sse` and the 22 tools below
become available.

## MCP tools

| Group | Tool | Purpose |
| --- | --- | --- |
| Memory (6) | `localmem_store`, `localmem_search`, `localmem_retrieve`, `localmem_update`, `localmem_pin`, `localmem_unpin` | Entry CRUD + hybrid search |
| Graph (5) | `localmem_graph_add_node`, `localmem_graph_add_edge`, `localmem_graph_query`, `localmem_graph_neighbors`, `localmem_graph_communities` | Behavioral pattern graph |
| Knowledge (3) | `localmem_triple_assert`, `localmem_triple_query`, `localmem_triple_contradictions` | Temporal triples with contradiction detection |
| System (3) | `localmem_wake`, `localmem_health`, `localmem_metrics` | Layered wake-up + observability |
| Intelligence (3) | `localmem_intel_detect`, `localmem_intel_alerts`, `localmem_intel_report` | Pattern detection (opt-in via config) |
| Operations (2) | `localmem_prune`, `localmem_archive` | Retention triggers |

## Storage stack

- **[Qdrant](https://qdrant.tech/)** — embedded by default (path-backed,
  single-writer). Switch to a remote Qdrant via `storage.qdrant_mode: server`
  + `storage.qdrant_url` to unblock live embedding migrations and
  multi-process writers.
- **[NetworkX](https://networkx.org/)** — in-process directed multigraph with
  multi-hop traversal and Louvain community detection.
- **SQLite** (WAL) — temporal triples, agent diaries, wing/room taxonomy,
  importance scoring with time-decay.

## Configuration

`localmem.yaml` at the repo root is the single source of truth. The shipped
defaults run locally with zero edits — set `wings:` to name your agents and
you're done. See inline comments for every section. Highlights:

- `wings: [list]` — your agent namespaces. `shared` is implicit.
- `embedding.model` — `all-MiniLM-L6-v2` (384d, fast) or BGE-large (1024d,
  quality). Switch live with `localmem migrate-embeddings --to <model>`.
- `retention.enabled: true` — opt in to the three-tier lifecycle.
- `dashboard.auth_enabled: true` + bearer key for remote dashboard access.
- `intelligence.detectors.*` — each pattern detector is off until you point
  it at a specific wing/room (or node selector for the graph cluster
  detector). Nothing runs you didn't ask for.

Any string value supports `${VAR}` or `${VAR:-default}` env-var
interpolation, so secrets stay out of YAML on disk.

## Dashboard

A read-only browser UI under `dashboard/` (Vite + React + dockview). 10
panels: Health, Entries, Metrics, Alerts, Graph, Wings/Rooms, Triples,
Diaries, Logs, Admin. Pin/unpin and lifecycle triggers live in Admin.
Localhost-only by default; flip on bearer auth to expose it remotely.

## Observability

- `localmem health` and `localmem_health` MCP tool — per-wing entry counts,
  store connectivity, embedding device, retention worker status.
- `localmem_metrics` MCP tool — per-tool call counts, p50/p95/p99 latency,
  error counts (rolling window).
- `/metrics` Prometheus exposition endpoint on the dashboard sidecar (`text/plain;
  version=0.0.4`). See [docs/DASHBOARD.md](docs/DASHBOARD.md) for the metric
  reference and example scrape config.
- Structured logging (text or JSON) with optional `RotatingFileHandler`. See
  [docs/LOGGING.md](docs/LOGGING.md) for Loki + Promtail and ELK + Filebeat
  shipping configs.

## Deployment

`deploy/` contains installer scripts for the three major platforms — each
generates a config dir, sets up a service (systemd / launchd / Scheduled
Tasks), and writes an `api_key` to a perms-restricted env file:

```bash
deploy/setup-ubuntu.sh  --auth --qdrant-server http://qdrant:6333
deploy/setup-macos.sh   --auth
deploy/setup-windows.ps1 -Auth
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full specification
- [`docs/DASHBOARD.md`](docs/DASHBOARD.md) — dashboard panels, auth, metrics
- [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md) — retention / consolidation / archive
- [`docs/MIGRATIONS.md`](docs/MIGRATIONS.md) — schema and embedding migrations
- [`docs/LOGGING.md`](docs/LOGGING.md) — log shipping recipes

## Project layout

```
localmem/
├── src/localmem/         # Package source
├── dashboard/            # React + dockview frontend
├── deploy/               # Installers + service units
├── docs/                 # Architecture, dashboard, lifecycle, migrations, logging
├── manifests/            # Per-agent wake-up manifests
├── tests/                # 300+ tests
├── localmem.yaml         # Default configuration
└── pyproject.toml
```

## Known issues

- **Python 3.14 + Apple Silicon + sentence-transformers**: the `loky`
  process pool used by sentence-transformers can crash silently at shutdown
  on Python 3.14 / arm64 macOS. The unit-test suite uses a hash-based
  embedder and is unaffected. For production runtime, prefer Python 3.12
  until the upstream issue is resolved, or use the `fastembed`-only path
  by setting `embedding.model: "Qdrant/bm25"` (sparse-only retrieval).

## License

MIT. See [LICENSE](LICENSE).
