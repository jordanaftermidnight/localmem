"""Tests for VectorStore — Qdrant hybrid search, CRUD, filtering."""

from unittest.mock import MagicMock, patch
import pytest

from conftest import FakeEmbedder

from localmem.config import LocalmemConfig, StorageConfig, EmbeddingConfig
from localmem.embedder import Embedder
from localmem.models import Entry, EntryType, SearchQuery
from localmem.vector_store import VectorStore


@pytest.fixture
async def store(tmp_path):
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=str(tmp_path),
            qdrant_path=str(tmp_path / "qdrant"),
            sqlite_path=str(tmp_path / "test.db"),
            graph_path=str(tmp_path / "graph.json"),
        ),
        embedding=EmbeddingConfig(model="test"),
    )
    embedder = FakeEmbedder()
    s = VectorStore(cfg, embedder)
    # Patch _ensure_collection to use our small dim
    from qdrant_client import QdrantClient
    from pathlib import Path

    path = Path(cfg.storage.qdrant_path)
    path.mkdir(parents=True, exist_ok=True)
    s._client = QdrantClient(path=str(path))
    s._embedder = embedder

    # Create collection with small vectors
    from qdrant_client.models import Distance, VectorParams, SparseVectorParams
    from localmem.vector_store import COLLECTION
    from qdrant_client import models

    s._client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            "dense": VectorParams(size=8, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )
    for field in ["wing", "room", "agent_id", "entry_type"]:
        s._client.create_payload_index(
            collection_name=COLLECTION,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )

    yield s


def _make_entry(**kwargs) -> Entry:
    defaults = {
        "wing": "observer",
        "room": "observations",
        "agent_id": "observer",
        "content": "Test observation content",
        "entry_type": EntryType.BEHAVIORAL_OBSERVATION,
    }
    defaults.update(kwargs)
    return Entry(**defaults)


class TestStoreCRUD:
    async def test_store_and_retrieve(self, store):
        entry = _make_entry(content="Coherence score dropped to 0.42")
        entry_id = await store.store(entry)

        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.content == "Coherence score dropped to 0.42"
        assert retrieved.wing == "observer"
        assert retrieved.room == "observations"

    async def test_retrieve_nonexistent(self, store):
        result = await store.retrieve("nonexistent-id")
        assert result is None

    async def test_delete(self, store):
        entry = _make_entry()
        entry_id = await store.store(entry)

        deleted = await store.delete(entry_id)
        assert deleted is True

        retrieved = await store.retrieve(entry_id)
        assert retrieved is None

    async def test_upsert_overwrites(self, store):
        entry = _make_entry(content="Version 1")
        await store.store(entry)

        entry.content = "Version 2"
        await store.store(entry)

        retrieved = await store.retrieve(entry.id)
        assert retrieved.content == "Version 2"

    async def test_store_preserves_metadata(self, store):
        entry = _make_entry(
            content="Test entry",
            summary="Short summary",
            importance=0.9,
            tags=["coherence", "anomaly"],
            metadata={"detector_id": "det-07"},
        )
        await store.store(entry)

        retrieved = await store.retrieve(entry.id)
        assert retrieved.summary == "Short summary"
        assert retrieved.importance == 0.9
        assert set(retrieved.tags) == {"coherence", "anomaly"}
        assert retrieved.metadata == {"detector_id": "det-07"}


class TestCount:
    async def test_count_empty(self, store):
        count = await store.count()
        assert count == 0

    async def test_count_all(self, store):
        for i in range(5):
            await store.store(_make_entry(content=f"Entry {i}"))
        assert await store.count() == 5

    async def test_count_filtered_by_wing(self, store):
        await store.store(_make_entry(wing="observer", content="observer entry"))
        await store.store(_make_entry(wing="router", content="router entry"))
        await store.store(_make_entry(wing="router", content="router entry 2"))

        assert await store.count(wing="observer") == 1
        assert await store.count(wing="router") == 2

    async def test_count_filtered_by_room(self, store):
        await store.store(_make_entry(room="observations", content="obs 1"))
        await store.store(_make_entry(room="patterns", content="pat 1"))

        assert await store.count(room="observations") == 1
        assert await store.count(room="patterns") == 1


class TestSearch:
    async def test_search_returns_results(self, store):
        await store.store(_make_entry(content="Coherence anomaly detected"))
        await store.store(_make_entry(content="Linguistic shift in response"))

        results = await store.search(
            SearchQuery(query="coherence anomaly", limit=5)
        )
        assert len(results) >= 1
        assert all(r.score >= 0 for r in results)

    async def test_search_filter_by_wing(self, store):
        await store.store(_make_entry(wing="observer", content="observer content"))
        await store.store(_make_entry(wing="router", content="router content"))

        results = await store.search(
            SearchQuery(query="content", wing="observer", limit=10)
        )
        assert all(r.entry.wing == "observer" for r in results)

    async def test_search_filter_by_room(self, store):
        await store.store(_make_entry(room="observations", content="obs data"))
        await store.store(_make_entry(room="patterns", content="pattern data"))

        results = await store.search(
            SearchQuery(query="data", room="observations", limit=10)
        )
        assert all(r.entry.room == "observations" for r in results)

    async def test_search_filter_by_entry_type(self, store):
        await store.store(
            _make_entry(
                entry_type=EntryType.BEHAVIORAL_OBSERVATION,
                content="behavioral observation",
            )
        )
        await store.store(
            _make_entry(
                entry_type=EntryType.DIARY,
                content="diary entry",
            )
        )

        results = await store.search(
            SearchQuery(
                query="entry",
                entry_type=EntryType.DIARY,
                limit=10,
            )
        )
        assert all(r.entry.entry_type == EntryType.DIARY for r in results)

    async def test_search_filter_by_tags(self, store):
        await store.store(_make_entry(tags=["coherence", "anomaly"], content="tagged entry"))
        await store.store(_make_entry(tags=["routine"], content="routine entry"))

        results = await store.search(
            SearchQuery(query="entry", tags=["coherence"], limit=10)
        )
        assert all("coherence" in r.entry.tags for r in results)

    async def test_search_empty_results(self, store):
        results = await store.search(
            SearchQuery(query="nonexistent topic", wing="nonexistent", limit=10)
        )
        assert results == []

    async def test_search_result_has_source(self, store):
        await store.store(_make_entry(content="test content for source check"))

        results = await store.search(SearchQuery(query="test content", limit=1))
        assert len(results) == 1
        assert results[0].source == "dense"  # FakeEmbedder has no sparse


class TestMultipleEntries:
    async def test_store_many_and_count(self, store):
        for i in range(20):
            await store.store(
                _make_entry(
                    wing=["observer", "router", "tools"][i % 3],
                    room=["obs", "patterns"][i % 2],
                    content=f"Entry number {i} with some unique content",
                )
            )
        assert await store.count() == 20

    async def test_search_respects_limit(self, store):
        for i in range(10):
            await store.store(_make_entry(content=f"Entry {i}"))

        results = await store.search(SearchQuery(query="Entry", limit=3))
        assert len(results) <= 3
