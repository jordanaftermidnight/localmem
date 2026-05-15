"""File-based migration runner.

Discovers migration modules in this package, sorts by VERSION, applies any
that aren't already in the schema_version table. Records each application
with the file's SHA-256 so post-hoc edits to applied migrations surface as
warnings on the next initialize.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import pkgutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    file_hash: str
    module_name: str
    up: Callable[[aiosqlite.Connection], Awaitable[None]]
    down: Callable[[aiosqlite.Connection], Awaitable[None]]


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations() -> list[Migration]:
    """Scan this package for v<NNN>_<name>.py modules and return them sorted
    by version. Each module must expose VERSION, DESCRIPTION, up, down."""
    package = importlib.import_module(__name__.rsplit(".", 1)[0])
    package_path = Path(package.__file__).parent

    migrations: list[Migration] = []
    for info in pkgutil.iter_modules([str(package_path)]):
        name = info.name
        if not name.startswith("v"):
            continue
        # Expect "v001_..." form
        version_part = name[1:].split("_", 1)[0]
        try:
            version = int(version_part)
        except ValueError:
            continue
        module = importlib.import_module(f"{package.__name__}.{name}")
        try:
            description = getattr(module, "DESCRIPTION")
            up = getattr(module, "up")
            down = getattr(module, "down")
        except AttributeError as exc:
            logger.warning(f"skipping malformed migration {name}: {exc}")
            continue
        path = package_path / f"{name}.py"
        migrations.append(
            Migration(
                version=version,
                description=description,
                file_hash=_hash_file(path),
                module_name=name,
                up=up,
                down=down,
            )
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


class MigrationRunner:
    """Applies pending migrations to an aiosqlite connection."""

    def __init__(self, migrations: list[Migration] | None = None):
        self._migrations = migrations if migrations is not None else discover_migrations()

    async def ensure_table(self, db: aiosqlite.Connection) -> None:
        """Create schema_version with the columns the runner expects. The
        original (v0.3.0) shape only had (version, applied_at); we extend it
        in place with description and file_hash so edits get caught."""
        await db.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT,
                file_hash TEXT
            )"""
        )
        # Add columns to a pre-existing v0.3.0 table.
        cursor = await db.execute("PRAGMA table_info(schema_version)")
        cols = {r[1] for r in await cursor.fetchall()}
        if "description" not in cols:
            await db.execute("ALTER TABLE schema_version ADD COLUMN description TEXT")
        if "file_hash" not in cols:
            await db.execute("ALTER TABLE schema_version ADD COLUMN file_hash TEXT")

    async def applied_versions(
        self, db: aiosqlite.Connection
    ) -> dict[int, dict[str, str | None]]:
        await self.ensure_table(db)
        cursor = await db.execute(
            "SELECT version, description, file_hash, applied_at FROM schema_version"
        )
        rows = await cursor.fetchall()
        return {
            int(r[0]): {
                "description": r[1],
                "file_hash": r[2],
                "applied_at": r[3],
            }
            for r in rows
        }

    async def run(self, db: aiosqlite.Connection) -> list[int]:
        """Apply all pending migrations in version order. Returns the list of
        versions that were applied in this run."""
        applied = await self.applied_versions(db)
        applied_now: list[int] = []

        for migration in self._migrations:
            existing = applied.get(migration.version)
            if existing is not None:
                if (
                    existing["file_hash"] is not None
                    and existing["file_hash"] != migration.file_hash
                ):
                    logger.warning(
                        f"migration v{migration.version} ({migration.module_name}) "
                        f"file hash changed since it was applied — schema may be "
                        f"out of sync with the source"
                    )
                continue

            logger.info(
                f"applying migration v{migration.version}: {migration.description}"
            )
            await migration.up(db)
            now = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
            await db.execute(
                """INSERT INTO schema_version
                   (version, applied_at, description, file_hash)
                   VALUES (?, ?, ?, ?)""",
                (migration.version, now, migration.description, migration.file_hash),
            )
            applied_now.append(migration.version)

        return applied_now

    async def rollback(self, db: aiosqlite.Connection, target: int) -> list[int]:
        """Roll back to (and including) `target` is the last surviving version.
        Migrations with version > target run their down() in reverse order."""
        applied = await self.applied_versions(db)
        to_revert = [m for m in self._migrations if m.version > target and m.version in applied]
        to_revert.sort(key=lambda m: m.version, reverse=True)

        reverted: list[int] = []
        for migration in to_revert:
            logger.info(
                f"reverting migration v{migration.version}: {migration.description}"
            )
            await migration.down(db)
            await db.execute(
                "DELETE FROM schema_version WHERE version = ?", (migration.version,)
            )
            reverted.append(migration.version)
        return reverted
