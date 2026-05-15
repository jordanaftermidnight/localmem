# localmem Dashboard

A read-only browser UI for inspecting the live localmem memory system.
Built on **dockview-react** + React 19 + TypeScript + Vite + Zustand, matching
the observer dashboard stack so the two can later share a shell.

**Version:** 0.5.1
**Ports:** backend `127.0.0.1:8782`, dev server `localhost:5173`
**Transport:** REST for request/response, WebSocket for live updates

## Install

```bash
# Backend (FastAPI sidecar)
pip install -e ".[dashboard]"

# Frontend
cd dashboard
npm install
```

## Run

Two processes:

```bash
# 1. Start the dashboard backend (reads the same data dir as the MCP server)
localmem dashboard

# 2. Start the dev server
cd dashboard
npm run dev              # → http://localhost:5173
```

> Qdrant local does not allow concurrent writers. Run the dashboard **either
> standalone** (no MCP server running) **or** embedded with the MCP server,
> never both as separate processes.

For production, build the UI once and serve it from any static host:

```bash
cd dashboard
npm run build            # → dashboard/dist/
```

## Configuration

`localmem.yaml`:

```yaml
dashboard:
  enabled: false              # (reserved for embedded mode)
  host: "127.0.0.1"           # localhost-only by default
  port: 8782
  auth_enabled: false         # enable when exposing beyond localhost
  api_key: "${LOCALMEM_API_KEY}"   # interpolated from env at load time
  cors_origins:
    - "http://localhost:5173"
  ws_push_interval_seconds: 2.0
```

### Authentication

When `auth_enabled: true`:

- **REST** calls require `Authorization: Bearer <key>`. The compare is
  constant-time (`hmac.compare_digest`).
- **WebSocket** upgrades present the key as a subprotocol:
  `new WebSocket(url, ['bearer.<key>'])`. The server picks the
  matching protocol if and only if the key validates. Tokens never
  appear in the URL — no leakage to access logs or browser history.

The frontend reads the key from `VITE_LOCALMEM_API_KEY` at build time,
falling back to `localStorage["localmem_api_key"]` at runtime.

The `api_key` field accepts `${VAR}` env-var interpolation, so the
secret never has to live plaintext in the YAML file. The setup scripts
(`deploy/setup-*`) generate a 24-byte random key into a perms-restricted
`prune.env` file when run with `--auth`.

### Qdrant server mode

```yaml
storage:
  qdrant_mode: "server"           # "local" (path-backed) or "server"
  qdrant_url: "http://qdrant:6333"
  qdrant_api_key: "${QDRANT_API_KEY}"   # optional
```

In server mode the dashboard `/api/health` reports
`vector_store.mode: "server"` and the URL it's pointed at. See
`docs/MIGRATIONS.md` for the implications when running an embedding
migration against a server-mode collection.

### Prometheus scraping

The endpoint is at `/metrics` (text/plain; version=0.0.4) on the
dashboard sidecar (default `127.0.0.1:8782`). It honors
`dashboard.auth_enabled`.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: localmem
    static_configs:
      - targets: ["localmem.local:8782"]
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/localmem-api-key
    scheme: http   # use https with a TLS-terminating proxy in front
    scrape_interval: 30s
```

Metric reference:

| Name | Type | Labels | Source |
|---|---|---|---|
| `localmem_uptime_seconds` | gauge | — | server start_time |
| `localmem_tool_calls_total` | counter | `tool` | MetricsCollector |
| `localmem_tool_errors_total` | counter | `tool` | MetricsCollector |
| `localmem_tool_latency_p50_milliseconds` | gauge | `tool` | MetricsCollector |
| `localmem_tool_latency_p95_milliseconds` | gauge | `tool` | MetricsCollector |
| `localmem_tool_latency_p99_milliseconds` | gauge | `tool` | MetricsCollector |
| `localmem_entries` | gauge | `wing` | per-wing Qdrant count |
| `localmem_graph_nodes` | gauge | — | NetworkX |
| `localmem_graph_edges` | gauge | — | NetworkX |
| `localmem_worker_running` | gauge | — | BackgroundWorker |
| `localmem_worker_in_flight` | gauge | — | BackgroundWorker |
| `localmem_worker_queue_size` | gauge | — | BackgroundWorker |
| `localmem_archive_files` | gauge | `wing` | Archiver.stats() |
| `localmem_archive_bytes` | gauge | `wing` | Archiver.stats() |

## Panels

| Panel | Data source | Description |
|---|---|---|
| **Health** | `/api/health` | Store connectivity, uptime, per-wing entry counts, embedding device |
| **Entries** | `/api/entries`, `/api/search` | Paginated browser with full-text search and detail drawer |
| **Metrics** | `/api/metrics` | Per-tool call counts + p50/p95/p99 latencies |
| **Alerts** | `/api/alerts` + WS `alerts` topic | Intelligence alerts stream, wing filter |
| **Graph** | `/api/graph/stats`, `/api/graph/subgraph` | Force-directed behavioral pattern graph |
| **Wings/Rooms** | `/api/taxonomy` | Click a room to filter the Entries panel |
| **Triples** | `/api/triples`, `/api/triples/timeline/{s}/{p}` | Knowledge triples with temporal timeline |
| **Diaries** | `/api/diaries` | Per-agent journal with mood and tags |
| **Logs** | WS `logs` topic | Live log tail (requires `logging.file` set) |

## REST endpoints

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/health` | health summary (now includes qdrant mode + retention) |
| GET  | `/api/metrics` | runtime metrics snapshot (JSON) |
| GET  | `/metrics` | Prometheus exposition (text/plain) |
| POST | `/api/entries/{id}/pin` | pin/unpin an entry |
| POST | `/api/prune/run` | trigger consolidation pass |
| POST | `/api/archive/run` | trigger archive pass |
| GET  | `/api/worker/status` | retention worker status |
| GET  | `/api/archive/stats` | per-wing archive file/byte counts |
| GET  | `/api/taxonomy` | wings + rooms with counts |
| GET  | `/api/entries?wing=&room=&tag=&limit=&offset=` | paginated entry list |
| GET  | `/api/entries/{id}` | full entry |
| POST | `/api/search` | hybrid search |
| GET  | `/api/graph/stats` | node/edge counts, density |
| GET  | `/api/graph/subgraph?node=&depth=&limit=` | subgraph extraction |
| GET  | `/api/triples?subject=&predicate=&object=&active_only=` | triple query |
| GET  | `/api/triples/timeline/{subject}/{predicate}` | temporal history |
| GET  | `/api/diaries?agent_id=&limit=&after=&before=` | diary read |
| GET  | `/api/alerts?wing=&limit=` | intelligence alerts |
| WS   | `/ws` | topic multiplexing: health, metrics, alerts, entries, logs, system |

## WebSocket protocol

Server pushes `{"topic": "...", "data": {...}, "timestamp": "..."}`.
Clients subscribe with `{"action": "subscribe", "topics": ["health", "metrics"]}`.
Default on connect: all topics.

## Development

```bash
# backend
python -m pytest tests/test_api.py          # 22 API tests
python -m pytest                             # full suite (170 tests)

# frontend
cd dashboard
npm run lint
npm run build
```

## Roadmap

- **Shipped through v0.5.1** — auth, /metrics, Qdrant server mode,
  log-shipping docs, hardening pass (constant-time compare, WS subprotocol
  auth, SQL allowlist, prompt-injection defense, env-var interpolation,
  cross-platform setup).
- **v0.6** — zero-downtime embedding migration (side-collection +
  alias swap), OpenTelemetry tracing, Prometheus Histogram type for
  latency, frontend code-splitting.
- **v1.0** — system integration shell (observer + localmem + tools + router
  panels in one dockview), cluster deployment (requires SQLite →
  Postgres or Litestream replication).
