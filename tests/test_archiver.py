"""Tests for the archive (cold tier) layer.

Covers: write/round-trip, hive partitioning, restore, duplicate reconciliation,
DuckDB SQL queries, semantic search, and policy gating (max_age=null skips)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from localmem.archiver import (
    Archiver,
    _atomic_write_jsonl_zst,
    _read_jsonl_zst,
    _partition_path,
)
from localmem.config import (
    ArchiveConfig,
    ConsolidationConfig,
    LocalmemConfig,
    RetentionConfig,
    RetentionDefaults,
    StorageConfig,
    WingRetentionPolicy,
)
from localmem.metadata_store import MetadataStore
from localmem.models import Entry
from localmem.vector_store import VectorStore

from conftest import FakeEmbedder


def _old_iso(days_ago: int) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _make_cfg(tmp_path, *, max_age=10, archive_enabled=True) -> LocalmemConfig:
    return LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            sqlite_path=str(tmp_path / "test.db"),
            qdrant_path=str(tmp_path / "qdrant"),
            graph_path=str(tmp_path / "graph.json"),
        ),
        retention=RetentionConfig(
            enabled=True,
            default=RetentionDefaults(
                soft_age_days=5,
                max_age_days=max_age,
                importance_floor=0.5,
            ),
            wings={
                "router": WingRetentionPolicy(),
                "shared": WingRetentionPolicy(max_age_days=None),
            },
            consolidation=ConsolidationConfig(min_group_size=2),
            archive=ArchiveConfig(
                enabled=archive_enabled,
                path=str(tmp_path / "archive"),
                compression_level=3,
            ),
        ),
    )


@pytest.fixture
async def stores(tmp_path):
    cfg = _make_cfg(tmp_path)
    embedder = FakeEmbedder()
    vs = VectorStore(cfg, embedder)
    await vs.initialize()
    ms = MetadataStore(cfg)
    await ms.initialize()
    return cfg, vs, ms, embedder


# --- Lower-level helpers ---


class TestSerialization:
    def test_jsonl_zst_round_trip(self, tmp_path):
        path = tmp_path / "test.jsonl.zst"
        rows = [
            {"id": "a", "x": 1},
            {"id": "b", "x": 2, "nested": {"y": [1, 2, 3]}},
        ]
        _atomic_write_jsonl_zst(path, rows, compression_level=3)
        assert path.exists()
        loaded = list(_read_jsonl_zst(path))
        assert loaded == rows

    def test_jsonl_zst_append_combines(self, tmp_path):
        path = tmp_path / "test.jsonl.zst"
        _atomic_write_jsonl_zst(path, [{"id": "a"}], 3)
        _atomic_write_jsonl_zst(path, [{"id": "b"}, {"id": "c"}], 3)
        loaded = list(_read_jsonl_zst(path))
        assert {r["id"] for r in loaded} == {"a", "b", "c"}

    def test_partition_path_format(self):
        root = Path("/tmp/archive")
        p = _partition_path(root, "router", "2026-04-15T12:00:00Z")
        assert "wing=router" in str(p)
        assert "2026-04" in str(p)
        assert p.suffix == ".zst"


# --- Archiver write path ---


class TestArchiveWrite:
    async def test_dry_run_doesnt_touch_disk_or_stores(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        for i in range(3):
            await vs.store(Entry(
                wing="router", room="r", agent_id="router",
                content=f"old {i}", importance=0.1,
                created_at=_old_iso(30),
            ))

        report = await archiver.archive_all(dry_run=True, wings=["router"])
        wr = report.wings[0]
        assert wr.candidates == 3
        assert wr.archived_entries == 3
        assert wr.partitions_written >= 1
        # But nothing on disk
        assert not Path(cfg.retention.archive.path).exists()
        # And entries still live
        assert await vs.count(wing="router") == 3

    async def test_real_write_archives_and_removes(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        ids = []
        for i in range(3):
            e = Entry(
                wing="router", room="r", agent_id="router",
                content=f"archive me {i}", importance=0.05,
                created_at=_old_iso(30),
            )
            await vs.store(e)
            ids.append(e.id)

        await archiver.archive_all(wings=["router"])

        # Live store should be empty for those entries
        for eid in ids:
            assert await vs.retrieve(eid) is None

        # Archive root exists with at least one file
        files = list(Path(cfg.retention.archive.path).rglob("*.jsonl.zst"))
        assert len(files) >= 1

        # Recover the archived entries from disk and verify they're all there
        all_archived = []
        for f in files:
            all_archived.extend(_read_jsonl_zst(f))
        archived_ids = {r["id"] for r in all_archived}
        assert archived_ids == set(ids)

    async def test_pinned_skipped(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        e = Entry(
            wing="router", room="r", agent_id="router",
            content="don't archive me", importance=0.05,
            created_at=_old_iso(30),
        )
        await vs.store(e)
        await vs.set_pinned(e.id, True)
        await ms.set_pinned(e.id, True, wing="router")

        await archiver.archive_all(wings=["router"])
        assert await vs.retrieve(e.id) is not None  # still live

    async def test_recent_skipped(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        e = Entry(
            wing="router", room="r", agent_id="router",
            content="fresh", importance=0.05,
            created_at=_old_iso(2),
        )
        await vs.store(e)

        report = await archiver.archive_all(wings=["router"])
        assert report.wings[0].candidates == 0
        assert await vs.retrieve(e.id) is not None

    async def test_shared_wing_never_archives(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        e = Entry(
            wing="shared", room="r", agent_id="x",
            content="cross-wing fact", importance=0.05,
            created_at=_old_iso(1000),  # very old
        )
        await vs.store(e)

        report = await archiver.archive_all(wings=["shared"])
        wr = report.wings[0]
        assert wr.skipped is True
        assert "max_age_days=null" in (wr.skipped_reason or "")
        assert await vs.retrieve(e.id) is not None

    async def test_archive_disabled_skips(self, tmp_path):
        cfg = _make_cfg(tmp_path, archive_enabled=False)
        embedder = FakeEmbedder()
        vs = VectorStore(cfg, embedder)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        archiver = Archiver(cfg, vs, ms)

        await vs.store(Entry(
            wing="router", room="r", agent_id="router",
            content="old", importance=0.05,
            created_at=_old_iso(30),
        ))

        report = await archiver.archive_all(wings=["router"])
        assert report.wings[0].skipped is True


# --- Reconciliation, restore, queries ---


class TestArchiveOps:
    async def test_reconcile_duplicates(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        e = Entry(
            wing="router", room="r", agent_id="router",
            content="ghost", importance=0.05,
            created_at=_old_iso(30),
        )
        await vs.store(e)
        # Manually write to archive — simulating partial-failure where live
        # delete didn't run.
        path = _partition_path(Path(cfg.retention.archive.path), "router", e.created_at)
        from localmem.archiver import _entry_to_dict
        _atomic_write_jsonl_zst(path, [_entry_to_dict(e)], 3)

        cleaned = await archiver.reconcile_archive_duplicates()
        assert cleaned == 1
        assert await vs.retrieve(e.id) is None

    async def test_restore(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        e = Entry(
            wing="router", room="r", agent_id="router",
            content="keep me discoverable", importance=0.05,
            tags=["important"],
            created_at=_old_iso(30),
        )
        await vs.store(e)
        await archiver.archive_all(wings=["router"])
        assert await vs.retrieve(e.id) is None

        ok = await archiver.restore(e.id)
        assert ok is True
        restored = await vs.retrieve(e.id)
        assert restored is not None
        assert restored.content == "keep me discoverable"
        assert "important" in restored.tags

    async def test_restore_unknown_returns_false(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)
        assert await archiver.restore("nope") is False

    async def test_stats(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        # Empty
        st = archiver.stats()
        assert st["exists"] is False or st["total_files"] == 0

        # Populate
        for i in range(2):
            await vs.store(Entry(
                wing="router", room="r", agent_id="router",
                content=f"x {i}", importance=0.05,
                created_at=_old_iso(30),
            ))
        await archiver.archive_all(wings=["router"])

        st = archiver.stats()
        assert st["exists"] is True
        assert st["total_files"] >= 1
        assert st["total_bytes"] > 0
        assert "router" in st["wings"]

    async def test_query_sql(self, stores):
        cfg, vs, ms, _ = stores
        archiver = Archiver(cfg, vs, ms)

        for i in range(3):
            await vs.store(Entry(
                wing="router", room="r", agent_id="router",
                content=f"sql test {i}", importance=0.05,
                created_at=_old_iso(30),
            ))
        await archiver.archive_all(wings=["router"])

        rows = archiver.query_sql(sql_where="wing = 'router'", limit=10)
        assert len(rows) == 3
        contents = {r["content"] for r in rows}
        assert all("sql test" in c for c in contents)

    async def test_search_semantic(self, stores):
        cfg, vs, ms, embedder = stores
        archiver = Archiver(cfg, vs, ms)

        for i, kw in enumerate(["routing", "coherence", "anomaly"]):
            await vs.store(Entry(
                wing="router", room="r", agent_id="router",
                content=f"entry about {kw} item {i}", importance=0.05,
                created_at=_old_iso(30),
            ))
        await archiver.archive_all(wings=["router"])

        results = archiver.search_semantic("routing", embedder, limit=5)
        # FakeEmbedder hashes content; cosine ranking is somewhat arbitrary
        # but should at least return results.
        assert len(results) >= 1
        assert all("score" in r for r in results)
