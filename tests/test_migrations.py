"""Tests for the file-based migration runner.

Covers: discovery, fresh-DB application, idempotent re-run, hash-change
warning on edited applied migrations, rollback semantics, version ordering."""

import importlib
import logging

import aiosqlite
import pytest

from localmem.migrations import MigrationRunner, discover_migrations
from localmem.migrations.runner import Migration


# --- Discovery ---


class TestDiscovery:
    def test_finds_v001(self):
        migs = discover_migrations()
        versions = [m.version for m in migs]
        assert 1 in versions

    def test_returned_in_version_order(self):
        migs = discover_migrations()
        versions = [m.version for m in migs]
        assert versions == sorted(versions)

    def test_each_has_callables(self):
        migs = discover_migrations()
        for m in migs:
            assert callable(m.up)
            assert callable(m.down)
            assert m.description
            assert m.file_hash
            assert len(m.file_hash) == 64  # SHA-256 hex

    def test_v001_module_name(self):
        migs = discover_migrations()
        v1 = next(m for m in migs if m.version == 1)
        assert v1.module_name.startswith("v001")


# --- Apply on a fresh DB ---


@pytest.fixture
async def fresh_db(tmp_path):
    path = tmp_path / "mig.db"
    async with aiosqlite.connect(path) as db:
        # Set up baseline schema (importance table that v001 mutates)
        await db.execute(
            """CREATE TABLE importance (
                entry_id TEXT PRIMARY KEY,
                wing TEXT,
                base_score REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT,
                decay_rate REAL DEFAULT 0.01
            )"""
        )
        await db.commit()
    return path


class TestApply:
    async def test_fresh_db_applies_all(self, fresh_db):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            applied = await runner.run(db)
            await db.commit()
            assert 1 in applied

            applied_map = await runner.applied_versions(db)
            assert 1 in applied_map
            assert applied_map[1]["file_hash"] is not None
            assert applied_map[1]["description"] == (
                "retention foundations: pinned + consolidated_sources"
            )

    async def test_idempotent_rerun(self, fresh_db):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            await runner.run(db)
            await db.commit()
            second = await runner.run(db)
            await db.commit()
            assert second == []  # nothing new to apply

    async def test_creates_consolidated_sources(self, fresh_db):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            await runner.run(db)
            await db.commit()
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='consolidated_sources'"
            )
            assert await cursor.fetchone() is not None

    async def test_adds_pinned_column(self, fresh_db):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            await runner.run(db)
            await db.commit()
            cursor = await db.execute("PRAGMA table_info(importance)")
            cols = [r[1] for r in await cursor.fetchall()]
            assert "pinned" in cols


# --- Hash mismatch detection ---


class TestHashWatch:
    async def test_warns_on_modified_migration(self, fresh_db, caplog):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            await runner.run(db)
            await db.commit()

        # Build a fake "modified" migration with the same version + same up()
        # but a different file_hash. The runner should warn but not re-apply.
        original_migs = discover_migrations()
        modified = []
        for m in original_migs:
            if m.version == 1:
                modified.append(Migration(
                    version=m.version,
                    description=m.description,
                    file_hash="0" * 64,  # different hash
                    module_name=m.module_name,
                    up=m.up,
                    down=m.down,
                ))
            else:
                modified.append(m)

        async with aiosqlite.connect(fresh_db) as db:
            runner2 = MigrationRunner(migrations=modified)
            with caplog.at_level(logging.WARNING):
                applied = await runner2.run(db)
                await db.commit()

            assert applied == []
            assert any("file hash changed" in rec.message for rec in caplog.records)


# --- Rollback ---


class TestRollback:
    async def test_rollback_to_zero_runs_down(self, fresh_db):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            await runner.run(db)
            await db.commit()

            reverted = await runner.rollback(db, target=0)
            await db.commit()
            assert 1 in reverted

            # consolidated_sources gone
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='consolidated_sources'"
            )
            assert await cursor.fetchone() is None

    async def test_rollback_above_max_no_op(self, fresh_db):
        async with aiosqlite.connect(fresh_db) as db:
            runner = MigrationRunner()
            await runner.run(db)
            await db.commit()
            reverted = await runner.rollback(db, target=99)
            await db.commit()
            assert reverted == []
