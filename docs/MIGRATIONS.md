# localmem Migrations

Three kinds of migration live under this umbrella:

1. **Schema migrations** — versioned changes to the SQLite metadata store.
2. **Embedding-model migration** — re-embedding the entire Qdrant collection
   with a different dense model (e.g. MiniLM 384d → BGE-large 1024d).
3. **Upgrade guide** — moving an existing install across localmem
   releases (config-file changes, breaking changes, manual steps).

## Upgrade guide

### v0.4.0 → v0.5.0 → v0.5.1

These are additive releases with two mechanical steps and one breaking
change for custom WS clients.

**Mechanical steps:**

1. **Upgrade.** `pip install --upgrade 'localmem[dashboard]'`
   (or `pip install -e '.[dashboard]'` from a source checkout). The
   `[analytics]` extras remain optional but now also pull `sqlglot`
   for the AST-validated archive SQL query path.
2. **Add the new `storage` keys** (or run the updated setup script,
   which writes them automatically):

   ```yaml
   storage:
     qdrant_mode: "local"     # default; no behavior change
     qdrant_url: ""           # only required when qdrant_mode = "server"
     qdrant_api_key: ""       # optional, server mode only
   ```

   The Pydantic validator defaults `qdrant_mode` to `"local"` if the
   field is missing, so a v0.4.0 config still parses — but adding the
   key explicitly avoids surprises later.

3. **For remote dashboards: enable auth.** The setup scripts gained
   `--auth`, which generates a 24-byte random key, writes it to
   `${CONFIG_DIR}/prune.env` (mode 0600/0640), and patches the
   YAML to read it via `${LOCALMEM_API_KEY}` interpolation.

   ```yaml
   dashboard:
     auth_enabled: true
     api_key: "${LOCALMEM_API_KEY}"
   ```

   Existing inline `api_key: "..."` strings continue to work.

**Breaking change (v0.5.1):**

- WebSocket auth moved from the `?token=<key>` query parameter to the
  `Sec-WebSocket-Protocol: bearer.<key>` subprotocol. The bundled
  dashboard updates automatically. Custom clients need:

  ```javascript
  new WebSocket(url, [`bearer.${apiKey}`])
  ```

  v0.5.0 clients sending `?token=` will be rejected with code 1008.

**Schema migrations** run automatically on first start under v0.5.x —
the v001 migration was already shipped in v0.3.0/0.4.0 and is detected
as already applied.

**Embedding model:** unchanged. If you want to switch models, follow
the embedding-model migration section below.

## Schema migrations (file-based)

## Schema migrations (file-based)

### Layout

```
src/localmem/migrations/
├── __init__.py
├── runner.py
├── v001_retention_foundations.py
└── ...                          # add v002_*.py, v003_*.py as needed
```

Each `v<NNN>_<name>.py` exposes:

```python
VERSION = 2
DESCRIPTION = "short, descriptive label for the change"

async def up(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE ...")

async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE ...")
```

### Runner behavior

`MetadataStore.initialize()` calls `MigrationRunner.run(db)` automatically.
The runner:

1. Ensures `schema_version` exists (with `version`, `applied_at`,
   `description`, `file_hash` columns — extends an older v0.3.0 table in
   place).
2. Discovers all `v<NNN>_*` modules in the package, sorts by `VERSION`.
3. For each migration not already in `schema_version`:
   - Calls `up(db)`.
   - Records `(version, applied_at, description, file_hash)`.
4. For migrations *already* applied: compares stored `file_hash` against
   the current file's hash. If they differ, logs a warning. The migration
   is **not** re-applied.

### Authoring a new migration

```bash
# Pick the next version number.
ls src/localmem/migrations/ | sort
# e.g. last is v003, so the new one is v004.
```

Create `v004_short_label.py` with `VERSION = 4`. SQLite has no
`ALTER TABLE ADD COLUMN IF NOT EXISTS`, so guard with `PRAGMA table_info`
when adding columns:

```python
async def _column_exists(db, table, column):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return any(r[1] == column for r in await cursor.fetchall())

async def up(db):
    if not await _column_exists(db, "entries", "new_col"):
        await db.execute("ALTER TABLE entries ADD COLUMN new_col TEXT")
```

`down()` is best-effort. SQLite can't drop columns on older versions
without rewriting the table, which is rarely worth it for a forward-only
deployment. Drop indexes and ancillary tables; leave columns.

### Rollback

`MigrationRunner.rollback(db, target=N)` runs `down()` for every applied
migration with version > N, in reverse order. There's no CLI surface for
rollback yet — operators use it manually if they need to undo something.

## Embedding-model migration

### When you need it

You want to switch to a higher-quality dense embedder (most commonly
MiniLM 384d → BGE-large 1024d). Vector dim changes; the existing Qdrant
collection cannot be reused. Every entry must be re-embedded under the
new model.

### How it works

```bash
# Dry run — counts and config diff only
localmem migrate-embeddings --to BAAI/bge-large-en-v1.5

# Actually run it
localmem migrate-embeddings --to BAAI/bge-large-en-v1.5 --apply
```

Pre-conditions:

- The dashboard / MCP server must be **stopped**. Qdrant local does not
  permit cross-process writers; the migrator owns the lock for the
  duration of the run.
- Sufficient disk space for a full snapshot of `data/qdrant/`.

What runs (with `--apply`):

1. **Safety check.** Refuses if `${dashboard.host}:${dashboard.port}` is
   bound by anything (assumed to be a running server).
2. **Snapshot.** `data/qdrant/` is copied to
   `data/qdrant.backup-<timestamp>`. If anything goes wrong, swap the
   directories and re-run with the original `--to`.
3. **Dump.** All entries are scrolled to a JSONL temp file
   (`migration-dump-<unix-ts>.jsonl` under the qdrant parent dir),
   preserving the full payload. The vectors themselves are *not* dumped —
   they're discarded and re-computed.
4. **Recreate.** The collection is dropped and recreated with the target
   model's dim, plus the same set of payload indexes the original had.
5. **Re-embed and upload.** Each dumped entry is re-embedded with the
   target model and upserted in batches (`--batch-size`, default 500).

After the run, the `localmem.yaml` file still points at the old model.
Update `embedding.model:` to the new value before restarting the server.

### Recovery

Failures part-way through leave:

- The backup directory in place (move or delete manually after success).
- The dump JSONL in place (safe to delete if the migration completed
  uploading; otherwise it's the source of truth for a retry).

To recover:

```bash
# Stop any running server.
mv data/qdrant data/qdrant.failed
mv data/qdrant.backup-<ts> data/qdrant
# Then either retry with the corrected model name, or accept the
# old config and restart.
```

### Why offline

Live re-embedding is harder than it looks: the old and new vectors have
different dims, can't co-exist in the same collection, and Qdrant local
doesn't support side-by-side collections under a single client.

In v0.5.0 we added **Qdrant server mode** (`storage.qdrant_mode: "server"`
+ `storage.qdrant_url`). When the migrator runs in server mode it skips
the port-bind safety check and the local snapshot — the operator owns
the Qdrant snapshot via the Qdrant snapshot API. Concurrent writers are
not blocked, but the collection is still dropped and recreated, so any
live reader will see a brief gap and need to handle "collection not
found" gracefully. A true zero-downtime path (write to a side collection,
then alias-swap) is a v0.6 follow-up.

In local mode you still need a maintenance window proportional to the
corpus size: re-embedding rate is dominated by your embedder's throughput.
