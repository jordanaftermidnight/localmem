# localmem Lifecycle (v0.3.0)

Memory in localmem is not a flat store — it's a three-tier ladder. Entries
move down the ladder as they age and lose relevance, but they don't vanish
until the policy says they must. The principle is **graceful forgetting**:
nothing lives forever, but things are remembered until they can't be.

## The three tiers

### Tier 1 — Hot (verbatim)

Live entries in Qdrant + SQLite. Full text, full vectors (dense + sparse),
full metadata, full graph edges. Every search hits this tier.

Default lifetime: from creation until `soft_age_days` (per wing).

### Tier 2 — Warm (consolidated)

When an entry crosses `soft_age_days` AND its decayed importance is below
`importance_floor` AND it isn't pinned, it's a **consolidation candidate**.
Candidates are grouped by `(wing, room, week)` and replaced with a single
**summary entry**:

- aggregated text (template-generated; LLM-generated reserved for v0.4.0),
- one embedding over the summary,
- `consolidated_sources` table maps the summary back to its sources,
- the source entries are removed from the live stores.

Search still finds the summary; the *gist* survives even when the
*episodes* don't. The summary itself has `is_summary=true` in payload and
is exempt from re-consolidation (you can't consolidate a summary).

### Tier 3 — Cold (archived)

When an entry crosses `max_age_days`, isn't pinned, and its wing isn't
exempt, it's **archived**: written to `archive/YYYY-MM/wing=X/week=W.jsonl.zst`
and removed from live stores. Cold entries are not in Qdrant, not in
SQLite, not in graph, not in L1 wake-up context. They're queryable only
via explicit `localmem archive` calls.

`shared` wing has `max_age_days: null` by default — shared facts never go
cold (they're foundational cross-agent context; archiving them would
regress L1 wake-up for every agent).

### Pinned (always Tier 1)

Any entry with `pinned=true` is exempt from every gate above. Pin via the
CLI, REST, or dashboard pin button.

## Configuration

```yaml
retention:
  enabled: true                    # off by default — opt in

  default:
    soft_age_days: 30              # consolidation threshold
    max_age_days: 365              # archive threshold
    importance_floor: 0.1          # decayed importance below this is eligible

  wings:
    router:    { soft_age_days: 14, max_age_days: 180 }
    tools:  { soft_age_days: 30, max_age_days: 365 }
    observer:    { soft_age_days: 60, max_age_days: 720 }
    shared:
      soft_age_days: 90
      max_age_days: null           # never archive shared
      importance_floor: 0.05

  consolidation:
    group_by: ["wing", "room", "week"]
    summarizer: "template"         # "template" or "llm" (v0.4.0+)
    llm_model: "qwen2.5:3b"        # bare name, OR ollama://host:port/model
    top_n_per_summary: 5
    min_group_size: 3              # don't consolidate tiny groups
    cron_enabled: true
    cron_schedule: "0 4 * * *"
    backpressure_enabled: true
    trigger_count_per_wing: 10000  # entries-per-wing threshold for backpressure
    min_interval_seconds: 300      # cooldown between same-wing runs

  archive:
    enabled: true
    path: "./data/archive"
    format: "jsonl.zst"
    compression_level: 7
    snapshot_before_transition: true
```

`policy_for(wing)` resolves: explicit per-wing override > `default`. A
wing override must explicitly set `max_age_days: null` to opt out of
archive (only valid for `shared`).

## CLI

### Pin / unpin

```bash
localmem pin <entry-id>
localmem unpin <entry-id>
localmem list-pinned [--wing X]
```

### Prune (consolidation)

```bash
localmem prune                   # dry-run report
localmem prune --apply           # actually consolidate
localmem prune --wing router       # restrict to one wing
```

The CLI refuses `--apply` if the dashboard server is up — Qdrant local
doesn't allow concurrent writers. Use the REST trigger instead while the
server is running.

### Archive

```bash
localmem archive write              # dry-run
localmem archive write --apply
localmem archive stats              # on-disk breakdown
localmem archive sql --where "wing = 'router'" --limit 10
localmem archive search "<text>" [--wing X --since 2026-04]
localmem archive restore <entry-id> # promote cold → hot
```

`archive sql` runs DuckDB against `read_json_auto(<glob>)` over the hive-
partitioned files. Install with `pip install -e '.[analytics]'` for the
DuckDB dependency.

`archive search` decompresses matching files into a temporary in-memory
Qdrant collection, runs hybrid search, drops the collection. Slow by
design — this is the cold path.

## REST API

While the server is running, all retention operations should go through
the dashboard endpoints:

| Method | Path                          | Notes |
|--------|-------------------------------|-------|
| POST   | `/api/entries/{id}/pin`       | body: `{"pinned": true}` |
| POST   | `/api/prune/run`              | triggers consolidation pass |
| POST   | `/api/archive/run`            | triggers archive pass |
| GET    | `/api/worker/status`          | last-run timestamps + queue |
| GET    | `/api/archive/stats`          | per-wing file/byte counts |

All endpoints respect `dashboard.auth_enabled` + `api_key` (Bearer token,
header form `Authorization: Bearer <key>`). The dashboard frontend reads
`VITE_LOCALMEM_API_KEY` (build time) or `localStorage["localmem_api_key"]`
(runtime).

## Cron

See `deploy/cron/README.md` for launchd (macOS) and systemd (Linux)
installation. Both `curl POST` against `/api/prune/run` and
`/api/archive/run` so they're safe to run while the server is up — there's
no separate Qdrant writer.

Default schedule: daily 04:00 local.

## Backpressure

In addition to the cron, the server runs a `BackgroundWorker` that:

1. Receives `signal_dirty(wing)` calls from the MCP write path.
2. Dedups per-wing into a `_dirty: set[str]`.
3. Wakes when signaled.
4. For each dirty wing whose cooldown has expired:
   - Counts entries in that wing.
   - If count ≥ `trigger_count_per_wing`, runs a partial consolidation.
   - Records `last_run[wing] = now()`.
5. Drops the wing from the dirty set.

Concurrency: `asyncio.Semaphore(1)` ensures only one consolidation runs
at a time, even across wings. Writes themselves are never blocked — only
the worker.

## Failure model

Every transition is **safe-overcounting**: if the system crashes mid-flow,
the next run reconciles. Specifically:

**Consolidation order:**
1. Insert summary into Qdrant.
2. SQLite txn: insert summary row + insert source links + delete source
   importance rows.
3. Best-effort: delete source points from Qdrant.
4. Best-effort: rewire graph edges onto the summary.

Crash between steps 2 and 3 leaves source points in Qdrant. The next run
calls `reconcile_orphans()` — it finds points whose `id` is in
`consolidated_sources` and deletes them.

**Archive order:**
1. Write `jsonl.zst` to disk (atomic temp+rename).
2. Best-effort: delete source from Qdrant + SQLite.

Crash between 1 and 2 leaves entries in *both* archive and live. Next run
calls `reconcile_archive_duplicates()` — finds live entries whose ids are
on disk and deletes them.

## Recovery & restoration

- **Consolidated summaries** preserve a `consolidated_sources` row per
  source entry, so you can answer "what raw entries fed into this gist?"
  even after the originals are gone.
- **Archived entries** can be promoted back with `localmem archive
  restore <id>`. The disk copy stays — restore is idempotent and
  recoverable.

## Storage bounds

- **Hot tier**: `max_age_days × write_rate` (per wing). Bounded.
- **Warm tier**: ≈ `years × 52 weeks × wings × rooms` summary entries.
  Tiny even at scale.
- **Cold tier**: grows linearly on disk; not in RAM, not in Qdrant.

## Observability

The dashboard's **Admin** panel surfaces:

- Worker status (running, in-flight, queue size, dirty wings)
- Last consolidation / archive timestamps
- Trigger buttons for manual prune / archive
- Per-wing archive stats (file count, bytes)

Logs go through the standard `LoggingConfig` — set `format: json` for
machine parsing.
