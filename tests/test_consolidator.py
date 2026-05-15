"""Tests for the consolidator and template summarizer.

These exercise the full consolidation pipeline: candidate selection,
grouping, summary generation, atomic store updates, and idempotent
reconciliation of partial-failure state."""

import pytest
from datetime import datetime, timedelta, timezone

from localmem.config import (
    ConsolidationConfig,
    LocalmemConfig,
    RetentionConfig,
    RetentionDefaults,
    StorageConfig,
    WingRetentionPolicy,
)
from localmem.consolidator import Consolidator, _iso_week
from localmem.metadata_store import MetadataStore
from localmem.models import Entry, EntryType
from localmem.summarizer import TemplateSummarizer
from localmem.vector_store import VectorStore

from conftest import FakeEmbedder


def _old_iso(days_ago: int) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _make_cfg(tmp_path, *, soft_age=30, max_age=365, floor=0.5, min_group=2) -> LocalmemConfig:
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
                soft_age_days=soft_age,
                max_age_days=max_age,
                importance_floor=floor,
            ),
            wings={
                "router": WingRetentionPolicy(),
                "observer": WingRetentionPolicy(),
                "shared": WingRetentionPolicy(max_age_days=None),
            },
            consolidation=ConsolidationConfig(min_group_size=min_group),
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
    return cfg, vs, ms


# --- Summarizer ---


class TestTemplateSummarizer:
    def test_basic_output(self):
        s = TemplateSummarizer(top_n=3)
        entries = [
            Entry(wing="observer", room="r", agent_id="a", content=f"observation {i} pattern coherence", importance=0.1 * i)
            for i in range(1, 6)
        ]
        bundle = s.summarize(entries, "observer/r 2026-W12")
        assert "Top entries by importance" in bundle["text"]
        assert bundle["entry_count"] == 5
        assert bundle["max_importance"] == pytest.approx(0.5)
        assert "pattern" in bundle["top_terms"] or "coherence" in bundle["top_terms"]

    def test_empty_raises(self):
        s = TemplateSummarizer()
        with pytest.raises(ValueError):
            s.summarize([], "x")

    def test_truncates_preview(self):
        s = TemplateSummarizer(top_n=1, preview_chars=20)
        e = Entry(wing="x", room="y", agent_id="z", content="word " * 100, importance=0.5)
        bundle = s.summarize([e], "x/y w")
        # the preview line should be the only "(0.50)" marker
        line = next(line for line in bundle["text"].splitlines() if "(0.50)" in line)
        assert "…" in line


# --- Consolidator ---


class TestConsolidatorBasics:
    def test_iso_week(self):
        # 2026-04-01 is W14
        assert _iso_week("2026-04-01T00:00:00Z").startswith("2026-W")
        assert _iso_week("invalid") in ("inva", "unknown")

    async def test_dry_run_reports_without_writing(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        for i in range(3):
            await vs.store(Entry(
                wing="router", room="r1", agent_id="a", content=f"old entry {i} routing latency",
                importance=0.05, created_at=_old_iso(60),
            ))

        report = await consolidator.consolidate_all(dry_run=True, wings=["router"])

        # Verify nothing got written
        all_entries = await vs.scroll(wing="router", limit=100)
        assert len(all_entries) == 3
        sources = await ms.get_consolidated_sources("anything")
        assert sources == []

        wing_result = report.wings[0]
        assert wing_result.candidates == 3
        assert wing_result.consolidated_groups == 0  # dry-run
        assert wing_result.groups[0].summary_id is None

    async def test_real_run_creates_summary(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        old_ids = []
        for i in range(4):
            e = Entry(
                wing="router", room="routing", agent_id="router",
                content=f"old routing decision {i} provider anthropic",
                importance=0.05,
                created_at=_old_iso(45),
            )
            await vs.store(e)
            old_ids.append(e.id)

        report = await consolidator.consolidate_all(wings=["router"])

        wing_result = report.wings[0]
        assert wing_result.consolidated_groups >= 1
        assert wing_result.consolidated_entries == 4

        # Source entries should be gone from Qdrant
        for sid in old_ids:
            assert await vs.retrieve(sid) is None

        # Summary should exist with is_summary=True
        summaries = await vs.scroll(wing="router", is_summary=True, limit=10)
        assert len(summaries) >= 1
        summary = summaries[0]
        assert summary.is_summary is True
        assert summary.metadata["source_count"] == 4

        # consolidated_sources table should contain links
        sources = await ms.get_consolidated_sources(summary.id)
        assert {s["source_id"] for s in sources} == set(old_ids)

        # Reverse lookup works
        for sid in old_ids:
            assert await ms.get_summary_for_source(sid) == summary.id

    async def test_pinned_entries_skipped(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        for i in range(3):
            e = Entry(
                wing="observer", room="patterns", agent_id="observer",
                content=f"pinned content {i}",
                importance=0.05,
                created_at=_old_iso(60),
            )
            await vs.store(e)
            await vs.set_pinned(e.id, True)
            await ms.set_pinned(e.id, True, wing="observer")

        report = await consolidator.consolidate_all(wings=["observer"])
        assert report.wings[0].candidates == 0
        assert report.wings[0].consolidated_groups == 0

        all_observer = await vs.scroll(wing="observer", limit=100)
        assert len(all_observer) == 3
        assert all(e.pinned for e in all_observer)

    async def test_high_importance_entries_skipped(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        for i in range(3):
            e = Entry(
                wing="router", room="r", agent_id="router",
                content=f"high-value decision {i}",
                importance=0.9,
                created_at=_old_iso(60),
            )
            await vs.store(e)

        report = await consolidator.consolidate_all(wings=["router"])
        assert report.wings[0].candidates == 0

        all_router = await vs.scroll(wing="router", limit=100)
        assert len(all_router) == 3

    async def test_recent_entries_skipped(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        for i in range(3):
            e = Entry(
                wing="router", room="r", agent_id="router",
                content=f"fresh entry {i}",
                importance=0.05,
                created_at=_old_iso(5),  # only 5 days old, soft is 30
            )
            await vs.store(e)

        report = await consolidator.consolidate_all(wings=["router"])
        assert report.wings[0].candidates == 0

    async def test_min_group_size_skips_tiny_groups(self, tmp_path):
        cfg = _make_cfg(tmp_path, min_group=5)
        embedder = FakeEmbedder()
        vs = VectorStore(cfg, embedder)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        consolidator = Consolidator(cfg, vs, ms)

        # 3 entries, min_group is 5 — they should be skipped
        for i in range(3):
            await vs.store(Entry(
                wing="router", room="r", agent_id="router",
                content=f"old {i}",
                importance=0.05,
                created_at=_old_iso(60),
            ))

        report = await consolidator.consolidate_all(wings=["router"])
        assert report.wings[0].consolidated_groups == 0
        assert report.wings[0].skipped_groups >= 1

    async def test_groups_by_room_and_week(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        # Different rooms — should produce separate summaries
        for room in ("r1", "r2"):
            for i in range(3):
                await vs.store(Entry(
                    wing="router", room=room, agent_id="router",
                    content=f"old entry in {room} num {i}",
                    importance=0.05,
                    created_at=_old_iso(60),
                ))

        report = await consolidator.consolidate_all(wings=["router"])
        assert report.wings[0].consolidated_groups == 2

        summaries = await vs.scroll(wing="router", is_summary=True, limit=10)
        rooms = {s.room for s in summaries}
        assert rooms == {"r1", "r2"}


class TestReconciliation:
    async def test_reconcile_orphans_detects_ghost_points(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        # Insert an entry, then add a fake consolidated_sources row pointing
        # at it — simulating a partial-failure state where the summary was
        # written but the source delete didn't run.
        e = Entry(
            wing="router", room="r", agent_id="router",
            content="ghost", importance=0.5, created_at=_old_iso(60),
        )
        await vs.store(e)
        await ms.add_consolidated_sources("fake-summary", [{"source_id": e.id}])

        before = await vs.retrieve(e.id)
        assert before is not None

        cleaned = await consolidator.reconcile_orphans()
        assert cleaned == 1

        after = await vs.retrieve(e.id)
        assert after is None

    async def test_reconcile_orphans_idempotent(self, stores):
        cfg, vs, ms = stores
        consolidator = Consolidator(cfg, vs, ms)

        # Nothing to reconcile
        assert await consolidator.reconcile_orphans() == 0
        assert await consolidator.reconcile_orphans() == 0
