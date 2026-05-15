"""Tests for MetricsCollector — counters, timings, percentiles."""

import asyncio
import pytest

from localmem.metrics import MetricsCollector, _percentile, _latency_stats


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([5.0], 50) == 5.0
        assert _percentile([5.0], 99) == 5.0

    def test_even_distribution(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(values, 50) == 3.0

    def test_p95(self):
        values = list(range(1, 101))
        p95 = _percentile([float(v) for v in values], 95)
        assert 95 <= p95 <= 96

    def test_p99(self):
        values = list(range(1, 101))
        p99 = _percentile([float(v) for v in values], 99)
        assert 99 <= p99 <= 100


class TestLatencyStats:
    def test_empty(self):
        stats = _latency_stats([])
        assert stats == {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}

    def test_basic(self):
        stats = _latency_stats([1.0, 2.0, 3.0, 4.0, 5.0])
        assert stats["avg"] == 3.0
        assert stats["p50"] == 3.0


class TestMetricsCollector:
    async def test_track_increments_counter(self):
        m = MetricsCollector()
        async with m.track("test_tool"):
            pass
        assert m._calls["test_tool"] == 1

    async def test_track_records_timing(self):
        m = MetricsCollector()
        async with m.track("test_tool"):
            await asyncio.sleep(0.01)
        assert len(m._timings["test_tool"]) == 1
        assert m._timings["test_tool"][0] > 0

    async def test_track_counts_errors(self):
        m = MetricsCollector()
        with pytest.raises(ValueError):
            async with m.track("bad_tool"):
                raise ValueError("boom")
        assert m._errors["bad_tool"] == 1
        assert m._calls["bad_tool"] == 1

    async def test_snapshot_structure(self):
        m = MetricsCollector()
        async with m.track("tool_a"):
            pass
        async with m.track("tool_b"):
            pass
        snap = m.snapshot()
        assert "uptime_seconds" in snap
        assert snap["total_calls"] == 2
        assert snap["total_errors"] == 0
        assert "tool_a" in snap["tools"]
        assert "tool_b" in snap["tools"]
        assert "latency_ms" in snap["tools"]["tool_a"]
        assert "avg" in snap["tools"]["tool_a"]["latency_ms"]

    async def test_rolling_window(self):
        m = MetricsCollector()
        m._max_timings = 5
        for _ in range(10):
            async with m.track("flood"):
                pass
        assert len(m._timings["flood"]) == 5
        assert m._calls["flood"] == 10

    async def test_reset(self):
        m = MetricsCollector()
        async with m.track("tool"):
            pass
        m.reset()
        assert m._calls["tool"] == 0
        assert m._timings["tool"] == []
        snap = m.snapshot()
        assert snap["total_calls"] == 0

    async def test_multiple_tools(self):
        m = MetricsCollector()
        for _ in range(3):
            async with m.track("store"):
                pass
        for _ in range(2):
            async with m.track("search"):
                pass
        snap = m.snapshot()
        assert snap["tools"]["store"]["calls"] == 3
        assert snap["tools"]["search"]["calls"] == 2
