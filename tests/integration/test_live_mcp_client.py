"""End-to-end MCP client integration tests against a live localmem server.

Exercises the same surface a real MCP client (Claude Desktop, Cursor,
Continue, custom agent) hits: SSE transport → FastMCP server → stores.

Each test opens its own SSE session via a helper to sidestep cross-task
cancel-scope issues that arise when pytest-asyncio creates a fresh event
loop per test and reuses a shared async fixture.

Run with `pytest tests/integration -v`. The default `pytest` invocation
ignores this directory because boot is slow (model download + warm-up).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from mcp import ClientSession
from mcp.client.sse import sse_client


def unwrap(result) -> dict | list:
    """MCP tool responses come back as a list of TextContent blocks. The
    server JSON-encodes the tool's return value as the first block."""
    return json.loads(result.content[0].text)


@asynccontextmanager
async def open_session(url: str):
    """Open a fresh ClientSession to the given URL. Yields after init."""
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            yield session, init


@pytest.mark.asyncio
async def test_handshake_and_protocol(live_server_url):
    async with open_session(live_server_url) as (_, init):
        assert init.serverInfo.name == "localmem"
        assert init.protocolVersion


@pytest.mark.asyncio
async def test_lists_all_22_tools(live_server_url):
    async with open_session(live_server_url) as (session, _):
        response = await session.list_tools()
        names = {t.name for t in response.tools}
        assert len(names) == 22, (
            f"expected 22 tools, got {len(names)}: {sorted(names)}"
        )
        for required in (
            "localmem_store",
            "localmem_search",
            "localmem_retrieve",
            "localmem_update",
            "localmem_delete",
            "localmem_health",
            "localmem_metrics",
            "localmem_wake",
            "localmem_kg_add",
            "localmem_kg_query",
            "localmem_graph_add",
            "localmem_graph_query",
            "localmem_graph_stats",
        ):
            assert required in names, f"missing tool: {required}"


@pytest.mark.asyncio
async def test_health_returns_ok(live_server_url):
    async with open_session(live_server_url) as (session, _):
        health = unwrap(await session.call_tool("localmem_health", {}))
        assert health["status"] == "healthy"
        assert health["vector_store"]["status"] == "ok"
        assert health["metadata_store"]["status"] == "ok"
        assert health["graph_store"]["status"] == "ok"
        assert health["embedding"]["model"] == "all-MiniLM-L6-v2"


@pytest.mark.asyncio
async def test_full_memory_round_trip(live_server_url):
    """Store an entry, search semantically, retrieve verbatim — the
    headline use case for an MCP memory server."""
    async with open_session(live_server_url) as (session, _):
        stored = unwrap(
            await session.call_tool(
                "localmem_store",
                {
                    "wing": "assistant",
                    "room": "integration-test",
                    "agent_id": "integration",
                    "content": "The mitochondria is the powerhouse of the cell.",
                    "tags": ["bio", "integration"],
                    "importance": 0.8,
                },
            )
        )
        assert stored["status"] == "stored"
        entry_id = stored["id"]

        results = unwrap(
            await session.call_tool(
                "localmem_search",
                {
                    "query": "powerhouse of the cell",
                    "wing": "assistant",
                    "limit": 3,
                },
            )
        )
        assert results, "search returned no hits for a freshly stored entry"
        top = results[0]
        assert top["id"] == entry_id
        assert top["source"] == "hybrid", "hybrid retrieval path did not fire"
        assert top["score"] > 0

        fetched = unwrap(
            await session.call_tool("localmem_retrieve", {"entry_id": entry_id})
        )
        assert fetched["content"] == "The mitochondria is the powerhouse of the cell."
        assert fetched["wing"] == "assistant"
        assert fetched["importance"] == 0.8


@pytest.mark.asyncio
async def test_update_persists_via_retrieve(live_server_url):
    """Metadata mutation must round-trip — read your writes."""
    async with open_session(live_server_url) as (session, _):
        stored = unwrap(
            await session.call_tool(
                "localmem_store",
                {
                    "wing": "assistant",
                    "room": "update-test",
                    "agent_id": "integration",
                    "content": "Original importance was 0.5",
                    "importance": 0.5,
                },
            )
        )
        entry_id = stored["id"]

        updated = unwrap(
            await session.call_tool(
                "localmem_update",
                {
                    "entry_id": entry_id,
                    "importance": 0.95,
                    "tags": ["updated", "integration"],
                },
            )
        )
        assert updated["status"] == "updated"

        refetched = unwrap(
            await session.call_tool("localmem_retrieve", {"entry_id": entry_id})
        )
        assert refetched["importance"] == 0.95
        assert "updated" in refetched["tags"]


@pytest.mark.asyncio
async def test_knowledge_triples_round_trip(live_server_url):
    """Knowledge graph: assert a triple, query it back."""
    async with open_session(live_server_url) as (session, _):
        asserted = unwrap(
            await session.call_tool(
                "localmem_kg_add",
                {
                    "subject": "mitochondria",
                    "predicate": "is_part_of",
                    "object": "cell",
                    "source_agent": "integration",
                },
            )
        )
        assert asserted["status"] == "added"
        assert asserted.get("triple_id")

        rows = unwrap(
            await session.call_tool(
                "localmem_kg_query",
                {"subject": "mitochondria", "active_only": True},
            )
        )
        assert any(
            r.get("predicate") == "is_part_of" and r.get("object") == "cell"
            for r in rows
        ), f"new triple not in query results: {rows}"


@pytest.mark.asyncio
async def test_behavioral_graph_round_trip(live_server_url):
    """Two nodes, one edge, neighbor query finds the partner."""
    async with open_session(live_server_url) as (session, _):
        await session.call_tool(
            "localmem_graph_add",
            {
                "node_id": "concept:integration-a",
                "node_attributes": {"type": "concept"},
            },
        )
        await session.call_tool(
            "localmem_graph_add",
            {
                "node_id": "concept:integration-b",
                "node_attributes": {"type": "concept"},
            },
        )
        await session.call_tool(
            "localmem_graph_add",
            {
                "source": "concept:integration-a",
                "target": "concept:integration-b",
                "edge_attributes": {"relation": "linked_to"},
            },
        )

        neighbors = unwrap(
            await session.call_tool(
                "localmem_graph_query",
                {"operation": "neighbors", "source_node": "concept:integration-a"},
            )
        )
        assert "concept:integration-b" in str(neighbors)

        stats = unwrap(await session.call_tool("localmem_graph_stats", {}))
        assert stats["nodes"] >= 2
        assert stats["edges"] >= 1


@pytest.mark.asyncio
async def test_metrics_reflect_session_activity(live_server_url):
    """Metrics counter must increment as tools fire."""
    async with open_session(live_server_url) as (session, _):
        metrics_before = unwrap(await session.call_tool("localmem_metrics", {}))
        await session.call_tool("localmem_health", {})
        await session.call_tool("localmem_health", {})
        metrics_after = unwrap(await session.call_tool("localmem_metrics", {}))

        assert metrics_after["total_calls"] > metrics_before["total_calls"]
        assert "localmem_health" in metrics_after["tools"]
        assert metrics_after["tools"]["localmem_health"]["calls"] >= 2
