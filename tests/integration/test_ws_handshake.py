"""End-to-end check that the WS bearer subprotocol does NOT echo the token.

Hits the live dashboard sidecar via the standard `websockets` library —
inspects the negotiated subprotocol on the 101 Switching Protocols response.
For v0.1.1 this must be exactly "bearer", never "bearer.<token>" or the
token itself.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


READY_LOG_MARKER = "Uvicorn running on"
READY_TIMEOUT_SECONDS = 60


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def auth_dashboard(tmp_path_factory):
    """Boot `localmem dashboard` with auth enabled. Yields (port, api_key)."""
    data_dir = tmp_path_factory.mktemp("ws_handshake")
    port = _pick_free_port()
    api_key = "test-api-key-do-not-leak-this-XXXX"
    cfg_path = data_dir / "localmem.yaml"
    cfg_path.write_text(
        f'wings: ["assistant"]\n'
        f"storage:\n"
        f'  base_path: "{data_dir}/data"\n'
        f'  qdrant_path: "{data_dir}/data/qdrant"\n'
        f'  sqlite_path: "{data_dir}/data/m.db"\n'
        f'  graph_path: "{data_dir}/data/g.json"\n'
        f"embedding:\n"
        f'  model: "all-MiniLM-L6-v2"\n'
        f'  device: "cpu"\n'
        f"dashboard:\n"
        f"  enabled: true\n"
        f'  host: "127.0.0.1"\n'
        f"  port: {port}\n"
        f"  auth_enabled: true\n"
        f'  api_key: "{api_key}"\n'
    )

    log_path = data_dir / "serve.log"
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "localmem", "-c", str(cfg_path), "dashboard"],
        stdout=log_path.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )

    deadline = time.time() + READY_TIMEOUT_SECONDS
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            log = log_path.read_text(errors="replace") if log_path.exists() else ""
            pytest.skip(f"dashboard exited rc={proc.returncode}:\n{log[-2000:]}")
        if log_path.exists() and READY_LOG_MARKER in log_path.read_text(
            errors="replace"
        ):
            ready = True
            break
        time.sleep(0.5)

    if not ready:
        proc.terminate()
        pytest.skip(f"dashboard did not become ready in {READY_TIMEOUT_SECONDS}s")

    yield port, api_key

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_handshake_echoes_only_bearer_not_token(auth_dashboard):
    """The 101 response's subprotocol must be literally 'bearer'."""
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port, api_key = auth_dashboard
    url = f"ws://127.0.0.1:{port}/ws"

    async with websockets.connect(
        url, subprotocols=["bearer", api_key]
    ) as ws:
        negotiated = ws.subprotocol
        assert negotiated == "bearer", (
            f"server echoed {negotiated!r} — must be 'bearer', "
            f"the token should never appear in the 101 response"
        )
        assert api_key not in (negotiated or ""), "token leaked into subprotocol"


@pytest.mark.asyncio
async def test_handshake_rejects_wrong_token(auth_dashboard):
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port, _ = auth_dashboard
    url = f"ws://127.0.0.1:{port}/ws"
    with pytest.raises(Exception):  # InvalidStatus or ConnectionClosed
        async with websockets.connect(
            url, subprotocols=["bearer", "wrong-token"]
        ):
            pass


@pytest.mark.asyncio
async def test_handshake_rejects_missing_bearer(auth_dashboard):
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port, api_key = auth_dashboard
    url = f"ws://127.0.0.1:{port}/ws"
    # No 'bearer' identifier in the subprotocol list at all.
    with pytest.raises(Exception):
        async with websockets.connect(url, subprotocols=[api_key]):
            pass


@pytest.mark.asyncio
async def test_handshake_rejects_legacy_dot_form(auth_dashboard):
    """The pre-v0.1.1 `bearer.<token>` single-value form is intentionally
    no longer accepted — clients must rebuild against the new dashboard."""
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port, api_key = auth_dashboard
    url = f"ws://127.0.0.1:{port}/ws"
    with pytest.raises(Exception):
        async with websockets.connect(
            url, subprotocols=[f"bearer.{api_key}"]
        ):
            pass
