"""Tests for time-decay importance scoring and wing filtering."""

import math
import pytest
from datetime import datetime, timezone, timedelta

from localmem.config import LocalmemConfig, StorageConfig
from localmem.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path):
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            sqlite_path=str(tmp_path / "test.db"),
            qdrant_path=str(tmp_path / "qdrant"),
            graph_path=str(tmp_path / "graph.json"),
        )
    )
    s = MetadataStore(cfg)
    await s.initialize()
    return s


class TestEffectiveScore:
    def test_no_decay_when_fresh(self):
        now = datetime.now(timezone.utc).isoformat()
        score = MetadataStore._effective_score(0.5, 0, now, 0.01)
        assert score == pytest.approx(0.5, abs=0.01)

    def test_decay_over_time(self):
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        score = MetadataStore._effective_score(0.5, 0, old, 0.01)
        expected = 0.5 * math.exp(-0.01 * 100)
        assert score == pytest.approx(expected, abs=0.001)

    def test_access_boost(self):
        now = datetime.now(timezone.utc).isoformat()
        score_no_access = MetadataStore._effective_score(0.5, 0, now, 0.01)
        score_with_access = MetadataStore._effective_score(0.5, 10, now, 0.01)
        assert score_with_access > score_no_access
        assert score_with_access == pytest.approx(0.5 * 2.0, abs=0.01)

    def test_no_last_accessed_treated_as_fresh(self):
        score = MetadataStore._effective_score(0.5, 0, None, 0.01)
        assert score == 0.5  # No decay applied

    def test_higher_decay_rate_decays_faster(self):
        old = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat()
        slow = MetadataStore._effective_score(0.5, 0, old, 0.01)
        fast = MetadataStore._effective_score(0.5, 0, old, 0.05)
        assert fast < slow


class TestWingFiltering:
    async def test_wing_filter_separates_entries(self, store):
        await store.update_importance("entry-router-1", base_score=0.9, wing="router")
        await store.update_importance("entry-tools-1", base_score=0.8, wing="tools")
        await store.update_importance("entry-observer-1", base_score=0.7, wing="observer")

        router_entries = await store.get_top_entries(wing="router")
        assert len(router_entries) == 1
        assert router_entries[0]["entry_id"] == "entry-router-1"

        tools_entries = await store.get_top_entries(wing="tools")
        assert len(tools_entries) == 1
        assert tools_entries[0]["entry_id"] == "entry-tools-1"

    async def test_no_wing_returns_all(self, store):
        await store.update_importance("entry-a", base_score=0.9, wing="router")
        await store.update_importance("entry-b", base_score=0.8, wing="tools")

        all_entries = await store.get_top_entries()
        assert len(all_entries) == 2

    async def test_effective_score_in_results(self, store):
        await store.update_importance("entry-1", base_score=0.5, wing="router")

        entries = await store.get_top_entries(wing="router")
        assert "effective_score" in entries[0]
        assert entries[0]["effective_score"] > 0


class TestDecayRanking:
    async def test_old_high_score_ranks_below_fresh_lower_score(self, store):
        # Insert old entry with high base score
        await store.update_importance("old-entry", base_score=0.9, wing="shared")
        # Manually set last_accessed to 200 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        async with store._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                "UPDATE importance SET last_accessed=? WHERE entry_id=?",
                (old_ts, "old-entry"),
            )
            await db.commit()

        # Insert fresh entry with lower base score
        await store.update_importance("fresh-entry", base_score=0.5, wing="shared")

        entries = await store.get_top_entries(wing="shared")
        assert len(entries) == 2
        # Fresh entry should rank higher despite lower base_score
        assert entries[0]["entry_id"] == "fresh-entry"
