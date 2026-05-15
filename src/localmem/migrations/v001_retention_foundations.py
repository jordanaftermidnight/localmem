"""V001 — retention foundations.

Adds the schema bits v0.3.0 introduced inline:
  - importance.pinned column (entries exempt from retention)
  - consolidated_sources table (link summaries back to source ids)
  - indexes on both

Idempotent on a fresh DB (CREATE IF NOT EXISTS guards). Idempotent on a
v0.3.0 DB that already has these via the inline migration list — the file
hash will differ, but the runner will skip applying anything new because
the version is already recorded.
"""

from __future__ import annotations

import aiosqlite

VERSION = 1
DESCRIPTION = "retention foundations: pinned + consolidated_sources"


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return any(r[1] == column for r in rows)


async def up(db: aiosqlite.Connection) -> None:
    if not await _column_exists(db, "importance", "pinned"):
        await db.execute("ALTER TABLE importance ADD COLUMN pinned INTEGER DEFAULT 0")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_importance_pinned ON importance(pinned)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS consolidated_sources (
            summary_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_text_hash TEXT,
            source_importance REAL,
            source_wing TEXT,
            source_room TEXT,
            source_created_at TEXT,
            consolidated_at TEXT NOT NULL,
            PRIMARY KEY (summary_id, source_id)
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_consolidated_summary "
        "ON consolidated_sources(summary_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_consolidated_source "
        "ON consolidated_sources(source_id)"
    )


async def down(db: aiosqlite.Connection) -> None:
    # SQLite can't drop a column on older versions without rewriting the
    # table; for a forward-only system this is acceptable. Remove the
    # auxiliary structures.
    await db.execute("DROP INDEX IF EXISTS idx_consolidated_source")
    await db.execute("DROP INDEX IF EXISTS idx_consolidated_summary")
    await db.execute("DROP TABLE IF EXISTS consolidated_sources")
    await db.execute("DROP INDEX IF EXISTS idx_importance_pinned")
    # importance.pinned column intentionally left in place — drop would
    # require recreating the table with all data, which isn't worth it.
