# Changelog

All notable changes to localmem.

## [0.1.2] — Onboarding streamline

Direct response to v0.1.1's onboarding friction (hand-written YAML, paste
mangling, legacy `launchctl load` getting stuck in "submitted but won't
run" state, multi-Python collisions). No security or behavioral changes
to the running server.

### New

- **`localmem init` CLI command.** Scaffolds a working `localmem.yaml` +
  data directory with sensible defaults. Replaces the previous
  hand-written YAML path that broke under terminal auto-indent / paste
  mangling. Flags: `--wing` (repeatable), `--dashboard`, `--qdrant-mode
  local|server`, `--qdrant-url`, `--force`, `--data-dir`. Prints
  next-step instructions on success.

- **`deploy/setup-launchd.py --load` flag.** Generates the three macOS
  LaunchAgents AND runs the modern `bootout` → `bootstrap` → `kickstart`
  sequence in one shot. The old `launchctl load -w` path is legacy on
  Catalina+ and frequently leaves services in a "submitted but won't
  run" state (the v0.1.1 path users had to debug by hand). The new flag
  defaults to off so the write-only behavior is preserved when callers
  want to inspect plists before loading.

### Docs

- **README Quick start rewritten for the venv-first path** that actually
  works on modern macOS (PEP 668 / externally-managed Pythons). Lists
  Python 3.13 explicitly because 3.14 + Apple Silicon has a known
  sentence-transformers shutdown bug.
- **Headless / always-on section** documenting the `setup-launchd.py
  --load` one-liner for users who want the stack to survive logout +
  reboot.

### Tests

- New `tests/test_init_command.py` covering: default scaffold, multiple
  wings, dashboard toggle, Qdrant local vs server, refuse-without-force
  on existing config, force-overwrite, generated YAML round-trips
  through `load_config`, next-step prompts.

## [0.1.1] — Security hardening pass

Findings from a multi-model code/security review. Each item below is gated
by a regression test under `tests/test_security_hardening_v011.py` or
`tests/integration/test_ws_handshake.py`.

### Security

- **DuckDB SQL allowlist rewritten as AST validation.** The pre-v0.1.1
  regex blocklist could not stop UNION-SELECT data exfiltration, subquery
  shapes (`WHERE wing IN (SELECT secret FROM ...)`), DuckDB file readers
  (`read_csv_auto('/etc/passwd')`, `read_text(...)`, `load_extension(...)`),
  CHR-encoded keyword reconstruction, recursive-CTE resource exhaustion,
  or qualified-column probes. `archiver.query_sql(sql_where=...)` now
  parses the user clause with `sqlglot` (DuckDB dialect) and walks the
  AST; only the explicit allowlist of node types (boolean ops,
  comparisons, literals, schema-allowlisted bare columns, `IN`/`BETWEEN`/
  `LIKE`/`IS`) survives. Any function call, subquery, `UNION`, `WITH`,
  `CAST`, window, or qualified column rejects the clause. Lexical
  pre-checks for `;`, `--`, and `/* */` close the parse-truncation hole
  where `parse_one` silently stopped at the first statement separator.
  Requires `pip install 'localmem[analytics]'` for `sqlglot>=23`.

- **WebSocket bearer subprotocol no longer echoes the token.** Pre-v0.1.1
  accepted `Sec-WebSocket-Protocol: bearer.<token>` and echoed the same
  string back in the 101 Switching Protocols response — exposing the
  token in proxy logs, browser devtools, and service worker scope. The
  new handshake takes two subprotocol values
  (`Sec-WebSocket-Protocol: bearer, <token>`), validates `<token>` with
  `hmac.compare_digest` against the configured api_key, and accepts the
  upgrade with `subprotocol="bearer"` only. The token never appears in
  the response. **Breaking change** for any pre-v0.1.1 dashboard bundle
  or custom WS client; rebuild the frontend and update clients to send
  the new two-value subprotocol list.

- **Wing names are charset-constrained.** Wing values flow into archive
  filesystem paths, JSON payloads, WebSocket frames, and Qdrant payload
  keys. `LocalmemConfig._validate_wings` now enforces
  `^[a-z0-9][a-z0-9_-]{0,62}$` (via `re.fullmatch`, so a trailing
  newline cannot slip past `$`). Rejects `..`, `/`, `\`, whitespace,
  control characters, non-ASCII payloads (`café`, RTL overrides), and
  >63-char strings before they can be interpolated into a path.

- **`Entry.importance` is Pydantic-bounded.** Field now declares
  `Field(default=0.5, ge=0.0, le=1.0)`. The `localmem_update` MCP tool
  also enforces the bound explicitly (direct attribute assignment
  bypasses Pydantic). Prevents a hostile client from setting
  out-of-range importance to dominate retrieval rankings or evade
  retention thresholds.

### Tests

- 367 unit tests (+62 from v0.1.0). New `tests/test_security_hardening_v011.py`
  covers AST allowlist accept/reject cases, wing-name charset rules,
  importance bounds. New `tests/integration/test_ws_handshake.py`
  proves the WS server never echoes the token in the 101 response and
  rejects the pre-v0.1.1 `bearer.<token>` form.

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
