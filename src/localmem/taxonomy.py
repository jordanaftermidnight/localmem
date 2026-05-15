"""LOCALMEM taxonomy — wing/room management and tunnel queries."""

from __future__ import annotations

import logging

import aiosqlite

from .models import _now

logger = logging.getLogger(__name__)


class Taxonomy:
    """Manages the wing/room spatial hierarchy.

    Wings = agent namespaces (user-configured + the reserved "shared" wing).
    Rooms = functional domain within a wing.
    Tunnels = cross-wing queries by room name.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path

    def _connect(self) -> aiosqlite.Connection:
        """Return an aiosqlite connection context manager (do NOT await)."""
        return aiosqlite.connect(self._db_path)

    async def _setup(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row

    async def register_room(self, wing: str, room: str) -> None:
        """Register or increment entry count for a wing:room pair."""
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
        """List all registered wings."""
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                "SELECT DISTINCT wing FROM taxonomy ORDER BY wing"
            )
            rows = await cursor.fetchall()
            return [r["wing"] for r in rows]

    async def list_rooms(self, wing: str | None = None) -> list[dict]:
        """List rooms, optionally filtered by wing."""
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

    async def find_tunnel_wings(self, room: str) -> list[str]:
        """Find all wings that contain a given room name (tunnel query)."""
        async with self._connect() as db:
            await self._setup(db)
            cursor = await db.execute(
                "SELECT wing FROM taxonomy WHERE room=? ORDER BY wing",
                (room,),
            )
            rows = await cursor.fetchall()
            return [r["wing"] for r in rows]
