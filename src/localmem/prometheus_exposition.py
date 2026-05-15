"""Prometheus text exposition for the dashboard /metrics endpoint.

Renders the in-memory MetricsCollector + health snapshot + worker status +
archive stats into Prometheus text format (v0.0.4). No external dependency:
the format is small enough to emit by hand and stay correct.

Naming follows the Prometheus convention:
    localmem_<subject>_<unit>{label="value"} <number>

Counters end in `_total`, durations are published as `_milliseconds` gauges
because that's what MetricsCollector tracks per-tool.
"""

from __future__ import annotations

from typing import Any


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _line(name: str, labels: dict[str, str] | None, value: float | int) -> str:
    if not labels:
        return f"{name} {value}"
    rendered = ",".join(
        f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items())
    )
    return f"{name}{{{rendered}}} {value}"


def build_prometheus_text(
    *,
    metrics: dict[str, Any],
    health: dict[str, Any],
    worker: dict[str, Any] | None = None,
    archive: dict[str, Any] | None = None,
) -> str:
    out: list[str] = []

    out.append("# HELP localmem_uptime_seconds Server uptime in seconds")
    out.append("# TYPE localmem_uptime_seconds gauge")
    out.append(_line("localmem_uptime_seconds", None, metrics.get("uptime_seconds", 0)))

    tools = metrics.get("tools", {}) or {}
    out.append("# HELP localmem_tool_calls_total Total MCP tool invocations")
    out.append("# TYPE localmem_tool_calls_total counter")
    if tools:
        for name, t in sorted(tools.items()):
            out.append(
                _line("localmem_tool_calls_total", {"tool": name}, t.get("calls", 0))
            )
    else:
        out.append(_line("localmem_tool_calls_total", None, 0))

    out.append("# HELP localmem_tool_errors_total Total MCP tool errors")
    out.append("# TYPE localmem_tool_errors_total counter")
    if tools:
        for name, t in sorted(tools.items()):
            out.append(
                _line("localmem_tool_errors_total", {"tool": name}, t.get("errors", 0))
            )
    else:
        out.append(_line("localmem_tool_errors_total", None, 0))

    if tools:
        for pct in ("p50", "p95", "p99"):
            metric = f"localmem_tool_latency_{pct}_milliseconds"
            out.append(f"# HELP {metric} Per-tool {pct} latency in milliseconds")
            out.append(f"# TYPE {metric} gauge")
            for name, t in sorted(tools.items()):
                value = t.get("latency_ms", {}).get(pct, 0)
                out.append(_line(metric, {"tool": name}, value))

    entries = (health.get("vector_store") or {}).get("entries") or {}
    if entries:
        out.append("# HELP localmem_entries Total entries per wing")
        out.append("# TYPE localmem_entries gauge")
        for wing, count in sorted(entries.items()):
            out.append(_line("localmem_entries", {"wing": wing}, count))

    graph = health.get("graph_store") or {}
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if nodes is not None:
        out.append("# HELP localmem_graph_nodes Behavioral pattern graph node count")
        out.append("# TYPE localmem_graph_nodes gauge")
        out.append(_line("localmem_graph_nodes", None, nodes))
    if edges is not None:
        out.append("# HELP localmem_graph_edges Behavioral pattern graph edge count")
        out.append("# TYPE localmem_graph_edges gauge")
        out.append(_line("localmem_graph_edges", None, edges))

    if worker is not None:
        out.append("# HELP localmem_worker_running Background retention worker is running")
        out.append("# TYPE localmem_worker_running gauge")
        out.append(_line(
            "localmem_worker_running", None, 1 if worker.get("running") else 0
        ))

        out.append("# HELP localmem_worker_in_flight Worker is currently processing a wing")
        out.append("# TYPE localmem_worker_in_flight gauge")
        out.append(_line(
            "localmem_worker_in_flight", None, 1 if worker.get("in_flight") else 0
        ))

        out.append("# HELP localmem_worker_queue_size Wings pending consolidation")
        out.append("# TYPE localmem_worker_queue_size gauge")
        out.append(_line(
            "localmem_worker_queue_size", None, worker.get("queue_size", 0)
        ))

    if archive is not None:
        wings = archive.get("wings") or {}
        if wings:
            out.append("# HELP localmem_archive_files Archive file count per wing")
            out.append("# TYPE localmem_archive_files gauge")
            for wing, ws in sorted(wings.items()):
                out.append(_line(
                    "localmem_archive_files", {"wing": wing}, ws.get("files", 0)
                ))

            out.append("# HELP localmem_archive_bytes Archive bytes on disk per wing")
            out.append("# TYPE localmem_archive_bytes gauge")
            for wing, ws in sorted(wings.items()):
                out.append(_line(
                    "localmem_archive_bytes", {"wing": wing}, ws.get("bytes", 0)
                ))

    return "\n".join(out) + "\n"


PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
