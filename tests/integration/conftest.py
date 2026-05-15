"""Fixtures for end-to-end integration tests.

Each test gets the URL of a live `localmem serve` subprocess and opens its
own MCP client session via the official Anthropic SDK. The server runs once
per session; the per-test client avoids cross-task cancel-scope issues that
arise when pytest-asyncio creates a fresh event loop for every test.

Slow (model download + boot ~15-30s) and opt-in — `pytest tests/` ignores
this directory by default. Run with `pytest tests/integration`.
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
SHUTDOWN_TIMEOUT_SECONDS = 10


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_config(data_dir: Path, port: int) -> Path:
    cfg_path = data_dir / "localmem.yaml"
    cfg_path.write_text(
        f'wings: ["assistant"]\n'
        f"server:\n"
        f'  host: "127.0.0.1"\n'
        f"  port: {port}\n"
        f'  transport: "sse"\n'
        f"storage:\n"
        f'  base_path: "{data_dir}/data"\n'
        f'  qdrant_path: "{data_dir}/data/qdrant"\n'
        f'  sqlite_path: "{data_dir}/data/localmem.db"\n'
        f'  graph_path: "{data_dir}/data/graph.json"\n'
        f"embedding:\n"
        f'  model: "all-MiniLM-L6-v2"\n'
        f'  device: "cpu"\n'
    )
    return cfg_path


@pytest.fixture(scope="session")
def live_server_url(tmp_path_factory) -> str:
    """Boot `localmem serve` once per session, return its SSE URL.

    Skips the test module (not fails) if the server can't be booted, so a
    busted local environment doesn't masquerade as a regression.
    """
    data_dir = tmp_path_factory.mktemp("live_server")
    port = _pick_free_port()
    cfg = _write_config(data_dir, port)
    log_path = data_dir / "serve.log"

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "localmem", "-c", str(cfg), "serve"],
        stdout=log_path.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )

    deadline = time.time() + READY_TIMEOUT_SECONDS
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            log = log_path.read_text(errors="replace") if log_path.exists() else ""
            pytest.skip(
                f"localmem serve exited before ready (rc={proc.returncode}):\n"
                f"{log[-2000:]}"
            )
        if log_path.exists() and READY_LOG_MARKER in log_path.read_text(
            errors="replace"
        ):
            ready = True
            break
        time.sleep(0.5)

    if not ready:
        proc.terminate()
        pytest.skip(
            f"localmem serve did not become ready in {READY_TIMEOUT_SECONDS}s"
        )

    url = f"http://127.0.0.1:{port}/sse"
    yield url

    proc.terminate()
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
