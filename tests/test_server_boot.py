"""Guard against FastMCP API drift.

The rest of the test suite exercises tools directly, bypassing the server's
construction and lifespan. That's fast but it leaves the boot path
(`create_server` → `lifespan` → `run_http_async`) untested — exactly the path
that broke when FastMCP dropped `on_event` in v3 and migrated to lifespan
context managers.

These tests don't actually serve traffic. They confirm that the symbols and
shapes the boot path depends on still exist and compose without errors.
"""

from __future__ import annotations

import inspect

import pytest
from fastmcp import FastMCP

from localmem import mcp_server


def test_create_server_returns_fastmcp_instance(tmp_path):
    cfg_path = tmp_path / "localmem.yaml"
    cfg_path.write_text(
        f'wings: ["assistant"]\n'
        f"storage:\n"
        f'  base_path: "{tmp_path}"\n'
        f'  qdrant_path: "{tmp_path}/qdrant"\n'
        f'  sqlite_path: "{tmp_path}/test.db"\n'
        f'  graph_path: "{tmp_path}/graph.json"\n'
    )
    server = mcp_server.create_server(str(cfg_path))
    assert isinstance(server, FastMCP)
    assert mcp_server._runtime_config is not None
    assert mcp_server._runtime_config.wings == ["assistant"]


def test_run_async_is_coroutine_function():
    assert inspect.iscoroutinefunction(mcp_server.run_async)


def test_fastmcp_supports_lifespan_param():
    """The lifespan kwarg is how the server initializes stores; verify it
    exists in the installed FastMCP version's constructor signature."""
    sig = inspect.signature(FastMCP.__init__)
    assert "lifespan" in sig.parameters, (
        "FastMCP no longer accepts `lifespan=` — check upgrade path before "
        "bumping fastmcp"
    )


def test_fastmcp_exposes_run_http_async():
    """`run_http_async` is the async entrypoint we use to compose with the
    CLI's outer asyncio loop; bare `run()` would crash with 'Already running
    asyncio'."""
    assert hasattr(FastMCP, "run_http_async"), (
        "FastMCP.run_http_async missing — server boot path needs rework "
        "before bumping fastmcp"
    )
    sig = inspect.signature(FastMCP.run_http_async)
    for required in ("transport", "host", "port"):
        assert required in sig.parameters, (
            f"FastMCP.run_http_async no longer accepts {required!r} — check "
            f"upgrade path"
        )


@pytest.mark.asyncio
async def test_lifespan_initializes_stores(tmp_path, monkeypatch):
    """End-to-end check of the boot pipeline without actually serving HTTP:
    enter the lifespan, confirm stores got initialized, exit cleanly."""
    cfg_path = tmp_path / "localmem.yaml"
    cfg_path.write_text(
        f'wings: ["assistant"]\n'
        f"storage:\n"
        f'  base_path: "{tmp_path}"\n'
        f'  qdrant_path: "{tmp_path}/qdrant"\n'
        f'  sqlite_path: "{tmp_path}/test.db"\n'
        f'  graph_path: "{tmp_path}/graph.json"\n'
        f"embedding:\n"
        f'  device: "cpu"\n'
    )

    init_called = False
    shutdown_called = False

    async def fake_init(cfg):
        nonlocal init_called
        init_called = True

    async def fake_shutdown():
        nonlocal shutdown_called
        shutdown_called = True

    monkeypatch.setattr(mcp_server, "initialize_stores", fake_init)
    monkeypatch.setattr(mcp_server, "shutdown_stores", fake_shutdown)

    server = mcp_server.create_server(str(cfg_path))
    async with server._lifespan_manager() if hasattr(server, "_lifespan_manager") else mcp_server._lifespan(server):
        assert init_called, "initialize_stores not called during lifespan startup"
    assert shutdown_called, "shutdown_stores not called during lifespan teardown"
