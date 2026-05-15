"""Tests for the embedding-model migration tool.

Uses two FakeEmbedder variants with different dense_dim values to exercise
the dim-change path. The real network/model loading is never triggered."""

import pytest

from localmem.config import LocalmemConfig, StorageConfig
from localmem.embedding_migrator import EmbeddingMigrator
from localmem.models import Entry
from localmem.vector_store import VectorStore


class FakeEmbedder384:
    dense_dim = 384

    def __init__(self, *args, **kwargs):
        pass

    def load(self):
        pass

    @property
    def resolved_device(self):
        return "cpu"

    def embed_dense(self, text):
        h = hash(text) & 0xFFFFFFFF
        return [(h >> i & 0xFF) / 255.0 for i in range(0, 384 * 8, 8)][:384]

    def embed_sparse(self, text):
        return None

    @property
    def has_sparse(self):
        return False


class FakeEmbedder1024:
    dense_dim = 1024

    def __init__(self, *args, **kwargs):
        pass

    def load(self):
        pass

    @property
    def resolved_device(self):
        return "cpu"

    def embed_dense(self, text):
        h = hash(text) & 0xFFFFFFFF
        return [(h >> (i % 32) & 0xFF) / 255.0 for i in range(1024)]

    def embed_sparse(self, text):
        return None

    @property
    def has_sparse(self):
        return False


def _patch_dims(monkeypatch):
    """Wire fake DENSE_DIMS so the migrator's _dim_for() works for our test
    model strings."""
    from localmem import embedding_migrator as em
    monkeypatch.setattr(em, "DENSE_DIMS", {
        "tiny-384": 384,
        "big-1024": 1024,
    })


def _patch_embedders(monkeypatch):
    """Replace the Embedder constructor in both vector_store and migrator
    contexts with our hash-based fakes, keyed by config.embedding.model."""
    from localmem import embedding_migrator as em
    from localmem import vector_store as vs

    def fake_embedder_factory(config):
        if config.embedding.model == "tiny-384":
            return FakeEmbedder384()
        return FakeEmbedder1024()

    monkeypatch.setattr(em, "Embedder", fake_embedder_factory)
    monkeypatch.setattr(vs, "Embedder", fake_embedder_factory)


def _make_cfg(tmp_path, model="tiny-384") -> LocalmemConfig:
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            sqlite_path=str(tmp_path / "test.db"),
            qdrant_path=str(tmp_path / "qdrant"),
            graph_path=str(tmp_path / "graph.json"),
        ),
    )
    cfg.embedding.model = model
    return cfg


@pytest.fixture
async def populated(tmp_path, monkeypatch):
    """Populates a Qdrant collection with 5 entries, then RELEASES the lock
    so the migrator (which must own the client exclusively) can run."""
    _patch_dims(monkeypatch)
    _patch_embedders(monkeypatch)

    cfg = _make_cfg(tmp_path, model="tiny-384")
    embedder = FakeEmbedder384()
    vs = VectorStore(cfg, embedder)
    await vs.initialize()

    for i in range(5):
        await vs.store(Entry(
            wing="router", room="r", agent_id="router",
            content=f"entry {i} routing decision",
            importance=0.5,
        ))
    await vs.close()
    return cfg


# --- Dry-run ---


class TestDryRun:
    async def test_reports_total_without_writing(self, populated):
        cfg = populated
        migrator = EmbeddingMigrator(cfg, target_model="big-1024")
        report = await migrator.migrate(dry_run=True)
        assert report.dry_run is True
        assert report.error is None
        assert report.progress.total == 5
        assert report.source_dim == 384
        assert report.target_dim == 1024
        assert report.backup_path is None  # no snapshot in dry-run

        # Original entries still 384d — re-open and check
        vs = VectorStore(cfg, FakeEmbedder384())
        await vs.initialize()
        count = await vs.count()
        assert count == 5
        await vs.close()

    async def test_identical_model_errors(self, populated):
        cfg = populated
        migrator = EmbeddingMigrator(cfg, target_model="tiny-384")
        report = await migrator.migrate(dry_run=True)
        assert "identical" in (report.error or "")


# --- Real migration ---


class TestApply:
    async def test_dim_change_replaces_collection(self, populated, tmp_path):
        cfg = populated
        migrator = EmbeddingMigrator(cfg, target_model="big-1024")
        report = await migrator.migrate(batch_size=2)

        assert report.error is None, report.error
        assert report.progress.embedded == 5
        assert report.progress.uploaded == 5
        assert report.backup_path is not None

        # Re-open the collection and verify the new dim is in effect
        from qdrant_client import QdrantClient
        client = QdrantClient(path=cfg.storage.qdrant_path)
        info = client.get_collection("localmem_entries")
        dense_params = info.config.params.vectors["dense"]
        assert dense_params.size == 1024
        assert client.count("localmem_entries", exact=True).count == 5
        client.close()

    async def test_payload_preserved(self, populated):
        cfg = populated
        migrator = EmbeddingMigrator(cfg, target_model="big-1024")
        await migrator.migrate(batch_size=10)

        cfg.embedding.model = "big-1024"
        new_vs = VectorStore(cfg, FakeEmbedder1024())
        await new_vs.initialize()
        entries = await new_vs.scroll(wing="router", limit=100)
        assert len(entries) == 5
        for e in entries:
            assert "routing decision" in e.content
            assert e.wing == "router"
        await new_vs.close()

    async def test_safety_check_blocks_when_port_open(self, populated, monkeypatch):
        cfg = populated
        from localmem import embedding_migrator as em
        monkeypatch.setattr(em, "_server_port_open", lambda host, port: True)

        migrator = EmbeddingMigrator(cfg, target_model="big-1024")
        report = await migrator.migrate()
        assert "running" in (report.error or "").lower()


# --- Reporting ---


class TestReporting:
    async def test_skipped_count_increments_for_empty_content(self, tmp_path, monkeypatch):
        _patch_dims(monkeypatch)
        _patch_embedders(monkeypatch)
        cfg = _make_cfg(tmp_path, model="tiny-384")
        vs = VectorStore(cfg, FakeEmbedder384())
        await vs.initialize()

        # Insert one valid entry and one with empty content
        await vs.store(Entry(wing="router", room="r", agent_id="router", content="ok"))
        await vs.store(Entry(wing="router", room="r", agent_id="router", content=""))
        await vs.close()

        migrator = EmbeddingMigrator(cfg, target_model="big-1024")
        report = await migrator.migrate(batch_size=10)
        assert report.error is None
        assert report.progress.embedded == 1
        assert report.progress.skipped == 1
