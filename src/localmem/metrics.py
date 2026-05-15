"""LOCALMEM metrics — in-memory tool call tracking and latency percentiles."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from typing import Any


class MetricsCollector:
    """Tracks MCP tool call counts, errors, and latency distributions."""

    def __init__(self) -> None:
        self.start_time: float = time.time()
        self._calls: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._timings: dict[str, list[float]] = defaultdict(list)
        self._max_timings: int = 1000

    @asynccontextmanager
    async def track(self, name: str):
        """Async context manager that records a tool call and its latency."""
        self._calls[name] += 1
        start = time.time()
        try:
            yield
        except Exception:
            self._errors[name] += 1
            raise
        finally:
            elapsed_ms = (time.time() - start) * 1000
            timings = self._timings[name]
            timings.append(elapsed_ms)
            if len(timings) > self._max_timings:
                self._timings[name] = timings[-self._max_timings :]

    def snapshot(self) -> dict[str, Any]:
        """Return full metrics snapshot with per-tool latency percentiles."""
        uptime = time.time() - self.start_time
        tools: dict[str, Any] = {}
        for name in sorted(self._calls):
            t = sorted(self._timings.get(name, []))
            tools[name] = {
                "calls": self._calls[name],
                "errors": self._errors[name],
                "latency_ms": _latency_stats(t),
            }
        return {
            "uptime_seconds": round(uptime, 1),
            "total_calls": sum(self._calls.values()),
            "total_errors": sum(self._errors.values()),
            "tools": tools,
        }

    def reset(self) -> None:
        """Clear all counters and timings."""
        self._calls.clear()
        self._errors.clear()
        self._timings.clear()


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute percentile from a pre-sorted list."""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(sorted_values[int(k)], 3)
    return round(sorted_values[f] * (c - k) + sorted_values[c] * (k - f), 3)


def _latency_stats(sorted_values: list[float]) -> dict[str, float]:
    """Compute avg, p50, p95, p99 from sorted latency values."""
    if not sorted_values:
        return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {
        "avg": round(sum(sorted_values) / len(sorted_values), 3),
        "p50": _percentile(sorted_values, 50),
        "p95": _percentile(sorted_values, 95),
        "p99": _percentile(sorted_values, 99),
    }
