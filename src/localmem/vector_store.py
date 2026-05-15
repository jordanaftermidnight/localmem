"""LOCALMEM vector store — Qdrant local with hybrid search."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from qdrant_client import QdrantClient, models
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
    SparseVectorParams,
)

from .config import LocalmemConfig
from .embedder import Embedder
from .models import Entry, SearchQuery, SearchResult

logger = logging.getLogger(__name__)

COLLECTION = "localmem_entries"

# Payload-index schema for the entries collection. Used by both VectorStore
# and EmbeddingMigrator so a schema change only touches one place.
KEYWORD_PAYLOAD_FIELDS = ("wing", "room", "agent_id", "entry_type", "created_at")
BOOL_PAYLOAD_FIELDS = ("pinned", "is_summary")


def create_entries_collection(client, *, dense_dim: int) -> None:
    """Create the entries collection with hybrid dense+sparse vectors and
    the standard set of payload indexes. Idempotent for the bool indexes —
    Qdrant raises on duplicate creation, which we swallow."""
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            "dense": VectorParams(size=dense_dim, distance=Distance.COSINE),
        },
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )
    for field in KEYWORD_PAYLOAD_FIELDS:
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    for field in BOOL_PAYLOAD_FIELDS:
        try:
            client.create_payload_index(
                collection_name=COLLECTION,
                field_name=field,
                field_schema=models.PayloadSchemaType.BOOL,
            )
        except Exception:
            pass


class VectorStore:
    """Qdrant-backed vector store with hybrid dense+sparse search."""

    def __init__(self, config: LocalmemConfig, embedder: Embedder):
        self.config = config
        self._client: QdrantClient | None = None
        self._embedder = embedder

    async def initialize(self) -> None:
        storage = self.config.storage
        if storage.qdrant_mode == "server":
            self._client = QdrantClient(
                url=storage.qdrant_url, api_key=storage.qdrant_api_key
            )
        else:
            path = Path(storage.qdrant_path)
            path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(path))
        self._ensure_collection()

    async def close(self) -> None:
        """Release the Qdrant local lock so another writer (e.g. the embedding
        migrator) can take it. Safe to call multiple times."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _ensure_collection(self) -> None:
        collections = [c.name for c in self._client.get_collections().collections]
        if COLLECTION not in collections:
            create_entries_collection(
                self._client, dense_dim=self._embedder.dense_dim
            )
            logger.info(f"Created collection '{COLLECTION}'")
            return
        # Existing collection — backfill any missing bool indexes
        # (collections created before v0.3.0 lack pinned/is_summary).
        for field in BOOL_PAYLOAD_FIELDS:
            try:
                self._client.create_payload_index(
                    collection_name=COLLECTION,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.BOOL,
                )
            except Exception:
                pass

    async def store(self, entry: Entry, *, is_summary: bool = False) -> str:
        # Embedding is CPU-bound (model inference); push to a worker
        # thread so we don't block the event loop on stores or migrations.
        dense_vec = await asyncio.to_thread(self._embedder.embed_dense, entry.content)
        sparse_vec = await asyncio.to_thread(self._embedder.embed_sparse, entry.content)

        vectors = {"dense": dense_vec}
        if sparse_vec is not None:
            vectors["sparse"] = sparse_vec

        point = PointStruct(
            id=entry.id,
            vector=vectors,
            payload={
                "wing": entry.wing,
                "room": entry.room,
                "agent_id": entry.agent_id,
                "entry_type": entry.entry_type.value,
                "content": entry.content,
                "summary": entry.summary,
                "importance": entry.importance,
                "tags": entry.tags,
                "refs": entry.refs,
                "metadata": entry.metadata,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "pinned": False,
                "is_summary": is_summary,
            },
        )
        self._client.upsert(collection_name=COLLECTION, points=[point])
        logger.info(f"Stored entry {entry.id} in {entry.wing}:{entry.room}")
        return entry.id

    async def set_pinned(self, entry_id: str, pinned: bool) -> bool:
        try:
            self._client.set_payload(
                collection_name=COLLECTION,
                payload={"pinned": pinned},
                points=[entry_id],
            )
            return True
        except Exception as e:
            logger.warning(f"set_pinned failed for {entry_id}: {e}")
            return False

    async def search(self, query: SearchQuery) -> list[SearchResult]:
        dense_vec = await asyncio.to_thread(self._embedder.embed_dense, query.query)
        sparse_vec = await asyncio.to_thread(self._embedder.embed_sparse, query.query)

        must_conditions = []
        if query.wing:
            must_conditions.append(
                FieldCondition(key="wing", match=MatchValue(value=query.wing))
            )
        if query.room:
            must_conditions.append(
                FieldCondition(key="room", match=MatchValue(value=query.room))
            )
        if query.entry_type:
            must_conditions.append(
                FieldCondition(
                    key="entry_type", match=MatchValue(value=query.entry_type.value)
                )
            )
        if query.tags:
            for tag in query.tags:
                must_conditions.append(
                    FieldCondition(key="tags", match=MatchValue(value=tag))
                )

        qfilter = Filter(must=must_conditions) if must_conditions else None

        if sparse_vec is not None:
            results = self._client.query_points(
                collection_name=COLLECTION,
                prefetch=[
                    models.Prefetch(
                        query=dense_vec, using="dense", limit=query.limit * 2
                    ),
                    models.Prefetch(
                        query=sparse_vec, using="sparse", limit=query.limit * 2
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=qfilter,
                limit=query.limit,
                with_payload=True,
            )
        else:
            results = self._client.query_points(
                collection_name=COLLECTION,
                query=dense_vec,
                using="dense",
                query_filter=qfilter,
                limit=query.limit,
                with_payload=True,
            )

        search_results = []
        for point in results.points:
            p = point.payload
            entry = Entry(
                id=str(point.id),
                wing=p["wing"],
                room=p["room"],
                agent_id=p["agent_id"],
                entry_type=p["entry_type"],
                content=p["content"],
                summary=p.get("summary"),
                importance=p.get("importance", 0.5),
                tags=p.get("tags", []),
                refs=p.get("refs", []),
                metadata=p.get("metadata", {}),
                created_at=p.get("created_at", ""),
                updated_at=p.get("updated_at", ""),
                pinned=bool(p.get("pinned", False)),
                is_summary=bool(p.get("is_summary", False)),
            )
            search_results.append(
                SearchResult(
                    entry=entry,
                    score=point.score if point.score is not None else 0.0,
                    source="hybrid" if sparse_vec else "dense",
                )
            )
        return search_results

    async def retrieve(self, entry_id: str) -> Entry | None:
        results = self._client.retrieve(
            collection_name=COLLECTION,
            ids=[entry_id],
            with_payload=True,
        )
        if not results:
            return None
        point = results[0]
        p = point.payload
        return Entry(
            id=str(point.id),
            wing=p["wing"],
            room=p["room"],
            agent_id=p["agent_id"],
            entry_type=p["entry_type"],
            content=p["content"],
            summary=p.get("summary"),
            importance=p.get("importance", 0.5),
            tags=p.get("tags", []),
            refs=p.get("refs", []),
            metadata=p.get("metadata", {}),
            created_at=p.get("created_at", ""),
            updated_at=p.get("updated_at", ""),
            pinned=bool(p.get("pinned", False)),
            is_summary=bool(p.get("is_summary", False)),
        )

    async def delete(self, entry_id: str) -> bool:
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=models.PointIdsList(points=[entry_id]),
        )
        return True

    async def scroll(
        self,
        wing: str | None = None,
        room: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        pinned: bool | None = None,
        is_summary: bool | None = None,
        before_created_at: str | None = None,
    ) -> list[Entry]:
        """Iterate entries by filter without semantic query. before_created_at is
        ISO-8601; payload index on created_at is KEYWORD, but lexicographic
        comparison works for ISO-8601 strings."""
        conditions = []
        if wing:
            conditions.append(
                FieldCondition(key="wing", match=MatchValue(value=wing))
            )
        if room:
            conditions.append(
                FieldCondition(key="room", match=MatchValue(value=room))
            )
        if tags:
            for tag in tags:
                conditions.append(
                    FieldCondition(key="tags", match=MatchValue(value=tag))
                )
        if pinned is not None:
            conditions.append(
                FieldCondition(key="pinned", match=MatchValue(value=pinned))
            )
        if is_summary is not None:
            conditions.append(
                FieldCondition(key="is_summary", match=MatchValue(value=is_summary))
            )
        qfilter = Filter(must=conditions) if conditions else None

        records, _next_offset = self._client.scroll(
            collection_name=COLLECTION,
            scroll_filter=qfilter,
            limit=limit,
            with_payload=True,
        )

        entries = []
        for point in records:
            p = point.payload
            # Server-side range filter on KEYWORD index isn't reliable across
            # Qdrant versions, so we filter in Python. Cheap for the prune sweep
            # which only needs candidates for one wing at a time.
            if before_created_at is not None:
                created = p.get("created_at", "")
                if not created or created >= before_created_at:
                    continue
            entries.append(Entry(
                id=str(point.id),
                wing=p["wing"],
                room=p["room"],
                agent_id=p["agent_id"],
                entry_type=p["entry_type"],
                content=p["content"],
                summary=p.get("summary"),
                importance=p.get("importance", 0.5),
                tags=p.get("tags", []),
                refs=p.get("refs", []),
                metadata=p.get("metadata", {}),
                created_at=p.get("created_at", ""),
                updated_at=p.get("updated_at", ""),
                pinned=bool(p.get("pinned", False)),
                is_summary=bool(p.get("is_summary", False)),
            ))
        return entries

    async def count(
        self,
        wing: str | None = None,
        room: str | None = None,
        pinned: bool | None = None,
        is_summary: bool | None = None,
    ) -> int:
        conditions = []
        if wing:
            conditions.append(
                FieldCondition(key="wing", match=MatchValue(value=wing))
            )
        if room:
            conditions.append(
                FieldCondition(key="room", match=MatchValue(value=room))
            )
        if pinned is not None:
            conditions.append(
                FieldCondition(key="pinned", match=MatchValue(value=pinned))
            )
        if is_summary is not None:
            conditions.append(
                FieldCondition(key="is_summary", match=MatchValue(value=is_summary))
            )
        qfilter = Filter(must=conditions) if conditions else None
        result = self._client.count(
            collection_name=COLLECTION, count_filter=qfilter, exact=True
        )
        return result.count
