"""Tests for the `localmem init` CLI command.

`init` is the bootstrap command that scaffolds a working localmem.yaml +
data directory for a fresh install. It's the only command that:
  - can run without an existing config file
  - validates writeable output paths
  - prints next-step instructions

These tests cover argument plumbing + generated YAML shape. The actual
serve/dashboard commands that consume the YAML are covered by the live
integration suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _run_init(*extra_args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke the installed `localmem` console script (via -m to avoid PATH
    issues in the test environment)."""
    return subprocess.run(
        [sys.executable, "-m", "localmem", "init", *extra_args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_init_writes_yaml_and_data_dir(tmp_path):
    r = _run_init("--data-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "localmem.yaml").exists()
    assert (tmp_path / "data").is_dir()


def test_init_default_wing_is_assistant(tmp_path):
    _run_init("--data-dir", str(tmp_path))
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert cfg["wings"] == ["assistant"]


def test_init_multiple_wings(tmp_path):
    _run_init(
        "--data-dir", str(tmp_path),
        "--wing", "router",
        "--wing", "tools",
        "--wing", "observer",
    )
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert cfg["wings"] == ["router", "tools", "observer"]


def test_init_dashboard_flag_adds_block(tmp_path):
    _run_init("--data-dir", str(tmp_path), "--dashboard")
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert cfg["dashboard"]["enabled"] is True
    assert cfg["dashboard"]["port"] == 8782


def test_init_no_dashboard_omits_block(tmp_path):
    _run_init("--data-dir", str(tmp_path))
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert "dashboard" not in cfg


def test_init_qdrant_local_default(tmp_path):
    _run_init("--data-dir", str(tmp_path))
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert cfg["storage"]["qdrant_mode"] == "local"
    assert "qdrant_url" not in cfg["storage"]


def test_init_qdrant_server_mode(tmp_path):
    _run_init(
        "--data-dir", str(tmp_path),
        "--qdrant-mode", "server",
        "--qdrant-url", "http://qdrant.example.com:6333",
    )
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert cfg["storage"]["qdrant_mode"] == "server"
    assert cfg["storage"]["qdrant_url"] == "http://qdrant.example.com:6333"


def test_init_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "localmem.yaml").write_text("existing: true\n")
    r = _run_init("--data-dir", str(tmp_path))
    assert r.returncode != 0
    assert "already exists" in r.stderr
    # Existing content untouched
    assert (tmp_path / "localmem.yaml").read_text() == "existing: true\n"


def test_init_overwrites_with_force(tmp_path):
    (tmp_path / "localmem.yaml").write_text("existing: true\n")
    r = _run_init("--data-dir", str(tmp_path), "--force")
    assert r.returncode == 0, r.stderr
    cfg = yaml.safe_load((tmp_path / "localmem.yaml").read_text())
    assert "wings" in cfg


def test_init_generated_yaml_loads_as_localmem_config(tmp_path):
    """End-to-end: generated YAML should be parseable by load_config."""
    from localmem.config import load_config
    _run_init("--data-dir", str(tmp_path), "--wing", "agent_x", "--dashboard")
    cfg = load_config(tmp_path / "localmem.yaml")
    assert cfg.wings == ["agent_x"]
    assert cfg.dashboard.enabled is True


def test_init_prints_next_steps(tmp_path):
    r = _run_init("--data-dir", str(tmp_path), "--dashboard")
    assert "localmem -c localmem.yaml serve" in r.stdout
    assert "dashboard" in r.stdout
