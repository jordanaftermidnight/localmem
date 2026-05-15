# Changelog

All notable changes to localmem.

## [0.1.0] — Initial public release

First open-source release. Everything below is shipped in v0.1.0.

### Core

- 22 MCP tools (memory CRUD, hybrid search, graph, knowledge triples,
  wake-up, intelligence, retention, health, metrics) over FastMCP/SSE on
  port 8781.
- User-configurable agent wings + reserved `shared` wing for cross-agent
  context.
- **Storage**: Qdrant (local or server mode) for hybrid dense + sparse
  search with RRF fusion; NetworkX directed multigraph with multi-hop
  traversal and Louvain community detection; SQLite WAL for temporal
  knowledge triples, agent diaries, taxonomy, and importance scoring with
  time-decay.
- **Layered loading** (L0 manifest → L1 critical context → L2 scoped
  search → L3 verbatim) for token-efficient agent wake-up.
- **Intelligence engine** with four opt-in pattern detectors (tool
  sequences, provider preferences, node clusters, cross-wing temporal
  correlations). Each detector is off by default until you point it at a
  wing/room or graph selector.

### Lifecycle

- Three-tier graceful forgetting: hot (verbatim) → warm (consolidated
  summaries grouped by wing/room/week) → cold (`jsonl.zst` archive,
  hive-partitioned).
- Pinned entries bypass every retention gate.
- Per-wing retention policy overrides; `shared` may set `max_age_days:
  null` to never archive.
- Background worker with single-flight semantics (Qdrant single-writer).
- REST trigger endpoints + cron units (launchd, systemd) + CLI
  (`pin`, `prune`, `archive`).
- Optional Ollama LLM summarizer (`consolidation.summarizer: "llm"`) with
  automatic fallback to the deterministic template summarizer.

### Schema & embeddings

- File-based schema migrations under `src/localmem/migrations/v00X_*.py`
  with `up()`/`down()` callables and hash-edit detection.
- `localmem migrate-embeddings --to <model>` re-embeds the entire
  collection offline (snapshot → dump → recreate → re-embed).

### Dashboard

- Read-only browser UI at `dashboard/` (Vite + React + dockview): 10
  panels for Health, Entries, Metrics, Alerts, Graph, Wings/Rooms,
  Triples, Diaries, Logs, Admin.
- FastAPI sidecar on port 8782 with REST + WebSocket.

### Observability

- `localmem_health` and `localmem_metrics` MCP tools with per-tool call
  counts, p50/p95/p99 latency, per-wing entry counts, retention worker
  status.
- `/metrics` Prometheus exposition endpoint (`text/plain;
  version=0.0.4`) on the dashboard sidecar.
- Structured logging (text or JSON) with optional
  `RotatingFileHandler`.

### Security

- Bearer auth on `/api/*` and `/ws` via `dashboard.auth_enabled`.
- WebSocket auth via `Sec-WebSocket-Protocol: bearer.<key>` subprotocol
  (no `?token=` in URLs).
- Constant-time API-key compare (`hmac.compare_digest`).
- DuckDB SQL allowlist on `archiver.query_sql(sql_where=...)` —
  rejects statement separators, comments, and mutating keywords.
- LLM prompt-injection defense — stored content wrapped in delimiters
  with explicit "data, not commands" instruction; control chars and
  delimiter mimicry stripped.
- `${VAR}` / `${VAR:-default}` env-var interpolation across the YAML
  config so secrets never need to live in the file on disk.
- CORS `allow_headers` scoped to `["Authorization", "Content-Type"]`.

### Cross-platform deploy

- `deploy/setup-ubuntu.sh`, `deploy/setup-macos.sh`,
  `deploy/setup-windows.ps1` — each generates a config dir, registers a
  service (systemd / launchd / Scheduled Tasks), and writes an api_key to
  a perms-restricted env file.
- `--auth` and `--qdrant-server <URL>` flags on each installer.

### Tests

- 300 tests across 22 files. Run with `pytest`.
