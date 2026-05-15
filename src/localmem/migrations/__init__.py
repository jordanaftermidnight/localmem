"""LOCALMEM schema migrations.

File-based migrations live in this package as `v<NNN>_<short_name>.py` modules,
each exposing `VERSION: int`, `DESCRIPTION: str`, and async `up(db)` / `down(db)`
functions that operate on an `aiosqlite.Connection`.

The runner (see `runner.py`) discovers every such file at import time, sorts by
`VERSION`, and applies any that aren't already recorded in the `schema_version`
table. It also stores a SHA-256 of each migration file so we can detect edits
to already-applied migrations and surface them as warnings.
"""
from .runner import MigrationRunner, discover_migrations

__all__ = ["MigrationRunner", "discover_migrations"]
