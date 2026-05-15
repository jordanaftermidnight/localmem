"""LOCALMEM metadata store — SQLite for triples, diaries, taxonomy."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .config import LocalmemConfig
from .contradiction import detect_and_resolve
from .models import ContradictionEvent, DiaryEntry, Triple, _now

logger = logging.getLogger(__name__)

SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_agent TEXT NOT NULL,
    source_entry_id TEXT,
    created_at TEXT NOT NULL,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);

CREATE TABLE IF NOT EXISTS diary (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    mood TEXT,
    tags TEXT,
    references_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_diary_agent ON diary(agent_id, timestamp);

CREATE TABLE IF NOT EXISTS taxonomy (
    wing TEXT NOT NULL,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    entry_count INTEGER DEFAULT 0,
    last_written TEXT,
    PRIMARY KEY (wing, room)
);

CREATE TABLE IF NOT EXISTS importance (
    entry_id TEXT PRIMARY KEY,
    wing TEXT,
    base_score REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    decay_rate REAL DEFAULT 0.01
);
CREATE INDEX IF NOT EXISTS idx_importance_wing ON importance(wing);
"""


class MetadataStore:
    """SQLite-backed metadata store with WAL mode for concurrent reads."""

    def __init__(self, config: LocalmemConfig):
        self.config = config
        self._db_path = config.storage.sqlite_path

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(SCHEMA_BASE)
            await self._migrate(db)
            await db.commit()
        logger.info(f"Metadata store initialized at {self._db_path}")

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        from .migrations import MigrationRunner
        runner = MigrationRunner()
        await runner.run(db)

    def _connect(self) -> aiosqlite.Connection:
        """Return an aiosqlite connection context manager (do NOT await)."""
        return aiosqlite.connect(self._db_path)

    async def _setup(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row

    # --- Triples ---

    async def add_triple(self, triple: Triple) -> ContradictionEvent | None:
        async with self._connect() as db:
            await self._setup(db)
            contradiction = await detect_and_resolve(db, triple)

            await db.execute(
                """INSERT OR REPLACE INTO triples
                   (id, subject, predicate, object, confidence, valid_from,
                    valid_to, source_agent, source_entry_id, created_at, superseded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    triple.id,
                    triple.subject,
                    triple.predicate,
                    triple.object,
                    triple.confidence,
                    triple.valid_from,
                    triple.valid_to,
                    triple.source_agent,
                    triple.source_entry_id,
                    triple.created_at,
                    triple.superseded_by,
                ),
            )
            await db.commit()
            return contradiction

    async def query_triples(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        obj: str | None = None,
        active_only: bool = True,
    ) -> list[Triple]:
        conditions = []
        params = []
        if subject:
            conditions.append("subject=?")
            params.append(subject)
        if predicate:
            conditions.append("predicate=?")
            params.append(predicate)
        if obj:
            conditions.append("object=?")
            params.append(obj)
        if active_only:
            conditions.append("valid_to IS NULL")

        where = " AND ".join(conditions) if conditions else "1=1"
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                f"SELECT * FROM triples WHERE {where} ORDER BY valid_from DESC",
                params,
            )
            rows = await cursor.fetchall()
            return [
                Triple(
                    id=r["id"],
                    subject=r["subject"],
                    predicate=r["predicate"],
                    object=r["object"],
                    confidence=r["confidence"],
                    valid_from=r["valid_from"],
                    valid_to=r["valid_to"],
                    source_agent=r["source_agent"],
                    source_entry_id=r["source_entry_id"],
                    created_at=r["created_at"],
                    superseded_by=r["superseded_by"],
                )
                for r in rows
            ]

    async def triple_timeline(self, subject: str, predicate: str) -> list[Triple]:
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                "SELECT * FROM triples WHERE subject=? AND predicate=? ORDER BY valid_from ASC",
                (subject, predicate),
            )
            rows = await cursor.fetchall()
            return [
                Triple(
                    id=r["id"],
                    subject=r["subject"],
                    predicate=r["predicate"],
                    object=r["object"],
                    confidence=r["confidence"],
                    valid_from=r["valid_from"],
                    valid_to=r["valid_to"],
                    source_agent=r["source_agent"],
                    source_entry_id=r["source_entry_id"],
                    created_at=r["created_at"],
                    superseded_by=r["superseded_by"],
                )
                for r in rows
            ]

    # --- Diary ---

    async def write_diary(self, entry: DiaryEntry) -> str:
        async with self._connect() as db:
            await self._setup(db)
            await db.execute(
                """INSERT INTO diary (id, agent_id, timestamp, content, mood, tags, references_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.id,
                    entry.agent_id,
                    entry.timestamp,
                    entry.content,
                    entry.mood,
                    json.dumps(entry.tags),
                    json.dumps(entry.references),
                ),
            )
            await db.commit()
        return entry.id

    async def read_diary(
        self,
        agent_id: str | None = None,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
    ) -> list[DiaryEntry]:
        conditions = []
        params = []
        if agent_id:
            conditions.append("agent_id=?")
            params.append(agent_id)
        if after:
            conditions.append("timestamp>?")
            params.append(after)
        if before:
            conditions.append("timestamp<?")
            params.append(before)

        where = " AND ".join(conditions) if conditions else "1=1"
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                f"SELECT * FROM diary WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                params + [limit],
            )
            rows = await cursor.fetchall()
            return [
                DiaryEntry(
                    id=r["id"],
                    agent_id=r["agent_id"],
                    timestamp=r["timestamp"],
                    content=r["content"],
                    mood=r["mood"],
                    tags=json.loads(r["tags"]) if r["tags"] else [],
                    references=json.loads(r["references_json"])
                    if r["references_json"]
                    else [],
                )
                for r in rows
            ]

    # --- Taxonomy ---

    async def register_room(self, wing: str, room: str) -> None:
        async with self._connect() as db:
            await self._setup(db)
            await db.execute(
                """INSERT INTO taxonomy (wing, room, created_at, entry_count, last_written)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(wing, room) DO UPDATE SET
                   entry_count = entry_count + 1, last_written = ?""",
                (wing, room, _now(), _now(), _now()),
            )
            await db.commit()

    async def list_wings(self) -> list[str]:
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                "SELECT DISTINCT wing FROM taxonomy ORDER BY wing"
            )
            rows = await cursor.fetchall()
            return [r["wing"] for r in rows]

    async def list_rooms(self, wing: str | None = None) -> list[dict]:
        async with self._connect() as db:
            await self._setup(db)
            if wing:
                cursor = await db.execute(
                    "SELECT * FROM taxonomy WHERE wing=? ORDER BY room",
                    (wing,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM taxonomy ORDER BY wing, room"
                )
            rows = await cursor.fetchall()
            return [
                {
                    "wing": r["wing"],
                    "room": r["room"],
                    "entry_count": r["entry_count"],
                    "last_written": r["last_written"],
                }
                for r in rows
            ]

    # --- Importance ---

    async def update_importance(
        self, entry_id: str, base_score: float | None = None, wing: str | None = None,
    ) -> None:
        async with self._connect() as db:
            await self._setup(db)
            now = _now()
            if base_score is not None:
                await db.execute(
                    """INSERT INTO importance (entry_id, wing, base_score, access_count, last_accessed)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(entry_id) DO UPDATE SET
                       base_score=?, access_count=access_count+1, last_accessed=?,
                       wing=COALESCE(?, wing)""",
                    (entry_id, wing, base_score, now, base_score, now, wing),
                )
            else:
                await db.execute(
                    """INSERT INTO importance (entry_id, wing, access_count, last_accessed)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(entry_id) DO UPDATE SET
                       access_count=access_count+1, last_accessed=?,
                       wing=COALESCE(?, wing)""",
                    (entry_id, wing, now, now, wing),
                )
            await db.commit()

    @staticmethod
    def _effective_score(
        base_score: float,
        access_count: int,
        last_accessed: str | None,
        decay_rate: float,
    ) -> float:
        """Compute importance with time decay: base * access_boost * decay_factor."""
        access_boost = 1.0 + access_count * 0.1
        if last_accessed:
            try:
                last_dt = datetime.fromisoformat(last_accessed)
                age_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
                decay_factor = math.exp(-decay_rate * max(age_days, 0))
            except (ValueError, TypeError):
                decay_factor = 1.0
        else:
            decay_factor = 1.0  # No access history — treat as fresh
        return min(base_score * access_boost * decay_factor, 1.0)

    async def get_top_entries(
        self, wing: str | None = None, limit: int = 15
    ) -> list[dict]:
        async with self._connect() as db:
            await self._setup(db)
            if wing:
                query = """
                    SELECT entry_id, base_score, access_count, last_accessed, decay_rate, pinned
                    FROM importance WHERE wing = ?
                """
                cursor = await db.execute(query, (wing,))
            else:
                query = """
                    SELECT entry_id, base_score, access_count, last_accessed, decay_rate, pinned
                    FROM importance
                """
                cursor = await db.execute(query)
            rows = await cursor.fetchall()

            scored = []
            for r in rows:
                eff = self._effective_score(
                    r["base_score"],
                    r["access_count"],
                    r["last_accessed"],
                    r["decay_rate"],
                )
                scored.append({
                    "entry_id": r["entry_id"],
                    "score": r["base_score"],
                    "access_count": r["access_count"],
                    "last_accessed": r["last_accessed"],
                    "effective_score": round(eff, 6),
                    "pinned": bool(r["pinned"]),
                })
            scored.sort(key=lambda x: x["effective_score"], reverse=True)
            return scored[:limit]

    # --- Pinning ---

    async def set_pinned(
        self, entry_id: str, pinned: bool, wing: str | None = None
    ) -> None:
        async with self._connect() as db:
            await self._setup(db)
            now = _now()
            await db.execute(
                """INSERT INTO importance (entry_id, wing, pinned, last_accessed)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(entry_id) DO UPDATE SET
                   pinned = excluded.pinned,
                   wing = COALESCE(excluded.wing, wing)""",
                (entry_id, wing, 1 if pinned else 0, now),
            )
            await db.commit()

    async def is_pinned(self, entry_id: str) -> bool:
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                "SELECT pinned FROM importance WHERE entry_id = ?",
                (entry_id,),
            )
            row = await cursor.fetchone()
            return bool(row["pinned"]) if row else False

    async def list_pinned(
        self, wing: str | None = None, limit: int = 100
    ) -> list[dict]:
        async with self._connect() as db:
            await self._setup(db)
            if wing:
                cursor = await db.execute(
                    """SELECT entry_id, wing, base_score, last_accessed
                       FROM importance WHERE pinned = 1 AND wing = ?
                       ORDER BY last_accessed DESC LIMIT ?""",
                    (wing, limit),
                )
            else:
                cursor = await db.execute(
                    """SELECT entry_id, wing, base_score, last_accessed
                       FROM importance WHERE pinned = 1
                       ORDER BY last_accessed DESC LIMIT ?""",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [
                {
                    "entry_id": r["entry_id"],
                    "wing": r["wing"],
                    "base_score": r["base_score"],
                    "last_accessed": r["last_accessed"],
                }
                for r in rows
            ]

    # --- Consolidated sources (links a summary entry to its source entries) ---

    async def add_consolidated_sources(
        self, summary_id: str, sources: list[dict]
    ) -> int:
        if not sources:
            return 0
        now = _now()
        rows = [
            (
                summary_id,
                s["source_id"],
                s.get("source_text_hash"),
                s.get("source_importance"),
                s.get("source_wing"),
                s.get("source_room"),
                s.get("source_created_at"),
                now,
            )
            for s in sources
        ]
        async with self._connect() as db:
            await self._setup(db)
            await db.executemany(
                """INSERT OR REPLACE INTO consolidated_sources
                   (summary_id, source_id, source_text_hash, source_importance,
                    source_wing, source_room, source_created_at, consolidated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await db.commit()
        return len(rows)

    async def get_consolidated_sources(self, summary_id: str) -> list[dict]:
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                """SELECT source_id, source_text_hash, source_importance,
                          source_wing, source_room, source_created_at, consolidated_at
                   FROM consolidated_sources WHERE summary_id = ?""",
                (summary_id,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "source_id": r["source_id"],
                    "source_text_hash": r["source_text_hash"],
                    "source_importance": r["source_importance"],
                    "source_wing": r["source_wing"],
                    "source_room": r["source_room"],
                    "source_created_at": r["source_created_at"],
                    "consolidated_at": r["consolidated_at"],
                }
                for r in rows
            ]

    async def get_summary_for_source(self, source_id: str) -> str | None:
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                "SELECT summary_id FROM consolidated_sources WHERE source_id = ?",
                (source_id,),
            )
            row = await cursor.fetchone()
            return row["summary_id"] if row else None

    async def remove_importance(self, entry_id: str) -> None:
        async with self._connect() as db:
            await self._setup(db)
            await db.execute(
                "DELETE FROM importance WHERE entry_id = ?", (entry_id,)
            )
            await db.commit()
