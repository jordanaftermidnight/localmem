"""Tests for the BackgroundWorker.

These mock the consolidator/archiver to keep the focus on orchestration:
queue dedup, cooldown, semaphore concurrency. Real consolidation behavior is
covered by test_consolidator.py."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from localmem.archiver import ArchiveReport
from localmem.config import (
    ConsolidationConfig,
    LocalmemConfig,
    RetentionConfig,
    RetentionDefaults,
    StorageConfig,
    WingRetentionPolicy,
)
from localmem.consolidator import ConsolidationReport, WingResult
from localmem.metadata_store import MetadataStore
from localmem.models import Entry
from localmem.vector_store import VectorStore
from localmem.worker import BackgroundWorker

from conftest import FakeEmbedder


def _make_cfg(tmp_path, *, trigger_count=10, cooldown_s=0) -> LocalmemConfig:
    return LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            sqlite_path=str(tmp_path / "test.db"),
            qdrant_path=str(tmp_path / "qdrant"),
            graph_path=str(tmp_path / "graph.json"),
        ),
        retention=RetentionConfig(
            enabled=True,
            default=RetentionDefaults(soft_age_days=30, max_age_days=365, importance_floor=0.5),
            wings={
                "router": WingRetentionPolicy(),
                "observer": WingRetentionPolicy(),
            },
            consolidation=ConsolidationConfig(
                trigger_count_per_wing=trigger_count,
                min_interval_seconds=cooldown_s,
            ),
        ),
    )


@pytest.fixture
async def worker_setup(tmp_path):
    cfg = _make_cfg(tmp_path)
    embedder = FakeEmbedder()
    vs = VectorStore(cfg, embedder)
    await vs.initialize()
    ms = MetadataStore(cfg)
    await ms.initialize()

    consolidator = AsyncMock()
    consolidator.consolidate_all = AsyncMock(
        return_value=ConsolidationReport(
            dry_run=False,
            started_at="2026-04-23T00:00:00Z",
            finished_at="2026-04-23T00:00:01Z",
            wings=[WingResult(wing="router", consolidated_groups=1, consolidated_entries=4)],
        )
    )
    archiver = AsyncMock()
    archiver.archive_all = AsyncMock(
        return_value=ArchiveReport(
            dry_run=False,
            started_at="2026-04-23T00:00:00Z",
            finished_at="2026-04-23T00:00:01Z",
        )
    )

    worker = BackgroundWorker(
        cfg, vs, ms, consolidator=consolidator, archiver=archiver
    )
    yield cfg, vs, ms, worker, consolidator, archiver
    await worker.stop()


# --- Lifecycle ---


class TestLifecycle:
    async def test_start_stop_idempotent(self, worker_setup):
        _, _, _, worker, _, _ = worker_setup
        worker.start()
        worker.start()  # second start should no-op
        assert worker.status().running is True
        await worker.stop()
        assert worker.status().running is False
        await worker.stop()  # second stop also no-op


# --- Backpressure path ---


class TestBackpressure:
    async def test_signal_below_threshold_doesnt_consolidate(self, worker_setup):
        cfg, vs, ms, worker, consolidator, _ = worker_setup
        # 0 entries < trigger_count (10), so no consolidation
        worker.start()
        worker.signal_dirty("router")
        await asyncio.sleep(0.1)
        consolidator.consolidate_all.assert_not_called()

    async def test_signal_at_threshold_triggers_consolidation(self, worker_setup):
        cfg, vs, ms, worker, consolidator, _ = worker_setup
        # Insert 10 entries to hit trigger_count
        for i in range(10):
            await vs.store(Entry(
                wing="router", room="r", agent_id="router",
                content=f"e{i}", importance=0.5,
            ))
        worker.start()
        worker.signal_dirty("router")
        await asyncio.sleep(0.2)
        assert consolidator.consolidate_all.call_count >= 1

    async def test_dedup_same_wing_signals(self, worker_setup):
        cfg, vs, ms, worker, consolidator, _ = worker_setup
        for i in range(10):
            await vs.store(Entry(wing="router", room="r", agent_id="router", content=f"e{i}"))
        worker.start()
        # Three signals in quick succession should result in one consolidation
        # (the worker has cooldown=0 in this fixture, so subsequent ones can
        # fire — but the wing gets cleared from dirty after each pass)
        worker.signal_dirty("router")
        worker.signal_dirty("router")
        worker.signal_dirty("router")
        await asyncio.sleep(0.2)
        # Either 1 or a small number — the key is that 3 signals don't produce 3 calls
        assert consolidator.consolidate_all.call_count <= 2

    async def test_disabled_retention_drains_queue_silently(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        cfg.retention.enabled = False  # type: ignore[assignment]
        embedder = FakeEmbedder()
        vs = VectorStore(cfg, embedder)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        consolidator = AsyncMock()
        archiver = AsyncMock()
        worker = BackgroundWorker(cfg, vs, ms, consolidator=consolidator, archiver=archiver)
        worker.start()
        worker.signal_dirty("router")
        await asyncio.sleep(0.1)
        consolidator.consolidate_all.assert_not_called()
        await worker.stop()


# --- Forced run (REST trigger) ---


class TestForcedRun:
    async def test_run_now_prune_only(self, worker_setup):
        cfg, vs, ms, worker, consolidator, archiver = worker_setup
        worker.start()
        await worker.run_now(prune=True, archive=False)
        await asyncio.sleep(0.2)
        consolidator.consolidate_all.assert_called()
        archiver.archive_all.assert_not_called()

    async def test_run_now_archive_only(self, worker_setup):
        cfg, vs, ms, worker, consolidator, archiver = worker_setup
        worker.start()
        await worker.run_now(prune=False, archive=True)
        await asyncio.sleep(0.2)
        consolidator.consolidate_all.assert_not_called()
        archiver.archive_all.assert_called()

    async def test_run_now_both(self, worker_setup):
        cfg, vs, ms, worker, consolidator, archiver = worker_setup
        worker.start()
        await worker.run_now()
        await asyncio.sleep(0.2)
        consolidator.consolidate_all.assert_called()
        archiver.archive_all.assert_called()

    async def test_status_after_run(self, worker_setup):
        cfg, vs, ms, worker, consolidator, archiver = worker_setup
        worker.start()
        await worker.run_now()
        await asyncio.sleep(0.2)
        s = worker.status()
        assert s.last_consolidation_at is not None
        assert s.last_archive_at is not None


# --- Concurrency ---


class TestConcurrency:
    async def test_semaphore_serializes_runs(self, worker_setup):
        """Only one consolidation runs at a time even when multiple wings
        are signaled simultaneously."""
        cfg, vs, ms, worker, consolidator, archiver = worker_setup

        # Track concurrent in-flight calls
        concurrent = 0
        max_concurrent = 0

        async def slow_consolidate(*args, **kwargs):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1
            return ConsolidationReport(
                dry_run=False, started_at="x", finished_at="y", wings=[],
            )

        consolidator.consolidate_all = AsyncMock(side_effect=slow_consolidate)

        # Insert enough entries in both wings to trip the trigger
        for wing in ("router", "observer"):
            for i in range(10):
                await vs.store(Entry(wing=wing, room="r", agent_id=wing, content=f"e{i}"))

        worker.start()
        worker.signal_dirty("router")
        worker.signal_dirty("observer")
        await asyncio.sleep(0.3)

        assert max_concurrent <= 1
