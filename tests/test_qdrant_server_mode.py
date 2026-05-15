"""Tests for the qdrant_mode local|server config + VectorStore + migrator branching.

These don't spin up a real Qdrant server; they verify config validation and
that the right QdrantClient constructor path is taken under each mode.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import FakeEmbedder

from localmem.config import (
    EmbeddingConfig,
    LocalmemConfig,
    StorageConfig,
)
from localmem.embedding_migrator import EmbeddingMigrator
from localmem.vector_store import VectorStore


class TestStorageConfigValidation:
    def test_local_is_default(self):
        cfg = StorageConfig()
        assert cfg.qdrant_mode == "local"
        assert cfg.qdrant_url is None

    def test_local_explicit_no_url_required(self):
        cfg = StorageConfig(qdrant_mode="local")
        assert cfg.qdrant_mode == "local"

    def test_server_requires_url(self):
        with pytest.raises(ValueError, match="qdrant_url"):
            StorageConfig(qdrant_mode="server")

    def test_server_with_url_ok(self):
        cfg = StorageConfig(qdrant_mode="server", qdrant_url="http://qdrant:6333")
        assert cfg.qdrant_mode == "server"
        assert cfg.qdrant_url == "http://qdrant:6333"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="must be 'local' or 'server'"):
            StorageConfig(qdrant_mode="hybrid")

    def test_server_with_api_key(self):
        cfg = StorageConfig(
            qdrant_mode="server",
            qdrant_url="http://qdrant:6333",
            qdrant_api_key="abc",
        )
        assert cfg.qdrant_api_key == "abc"


class TestVectorStoreServerMode:
    @pytest.mark.asyncio
    async def test_server_mode_uses_url_constructor(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                qdrant_mode="server",
                qdrant_url="http://qdrant:6333",
                qdrant_api_key="key",
                sqlite_path=str(tmp_path / "m.db"),
                graph_path=str(tmp_path / "g.json"),
            ),
            embedding=EmbeddingConfig(model="test"),
        )
        vs = VectorStore(cfg, FakeEmbedder())
        with patch("localmem.vector_store.QdrantClient") as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.get_collections.return_value.collections = []
            await vs.initialize()
            # URL ctor was used, not path
            mock_client.assert_called_once_with(
                url="http://qdrant:6333", api_key="key"
            )

    @pytest.mark.asyncio
    async def test_local_mode_uses_path_constructor(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                qdrant_mode="local",
                qdrant_path=str(tmp_path / "qdrant"),
                sqlite_path=str(tmp_path / "m.db"),
                graph_path=str(tmp_path / "g.json"),
            ),
            embedding=EmbeddingConfig(model="test"),
        )
        vs = VectorStore(cfg, FakeEmbedder())
        with patch("localmem.vector_store.QdrantClient") as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.get_collections.return_value.collections = []
            await vs.initialize()
            args, kwargs = mock_client.call_args
            assert kwargs.get("path", args[0] if args else None) == str(
                tmp_path / "qdrant"
            )
            assert "url" not in kwargs


class TestMigratorServerMode:
    def test_safety_check_skipped_in_server_mode(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                qdrant_mode="server",
                qdrant_url="http://qdrant:6333",
                sqlite_path=str(tmp_path / "m.db"),
                graph_path=str(tmp_path / "g.json"),
            ),
            embedding=EmbeddingConfig(model="all-MiniLM-L6-v2"),
        )
        m = EmbeddingMigrator(cfg, target_model="BAAI/bge-large-en-v1.5")
        # Even if the dashboard port is bound, server mode skips the check.
        with patch("localmem.embedding_migrator._server_port_open", return_value=True):
            assert m._safety_check() is None

    def test_safety_check_enforced_in_local_mode(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                qdrant_mode="local",
                qdrant_path=str(tmp_path / "qdrant"),
                sqlite_path=str(tmp_path / "m.db"),
                graph_path=str(tmp_path / "g.json"),
            ),
            embedding=EmbeddingConfig(model="all-MiniLM-L6-v2"),
        )
        m = EmbeddingMigrator(cfg, target_model="BAAI/bge-large-en-v1.5")
        with patch("localmem.embedding_migrator._server_port_open", return_value=True):
            err = m._safety_check()
            assert err is not None
            assert "Dashboard appears to be running" in err

    def test_snapshot_skipped_in_server_mode(self, tmp_path):
        cfg = LocalmemConfig(
            storage=StorageConfig(
                qdrant_mode="server",
                qdrant_url="http://qdrant:6333",
                sqlite_path=str(tmp_path / "m.db"),
                graph_path=str(tmp_path / "g.json"),
            ),
            embedding=EmbeddingConfig(model="all-MiniLM-L6-v2"),
        )
        m = EmbeddingMigrator(cfg, target_model="BAAI/bge-large-en-v1.5")
        assert m._snapshot() == ""
