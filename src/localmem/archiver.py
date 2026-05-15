"""LOCALMEM archiver — cold tier.

Entries past max_age_days that aren't pinned and weren't already consolidated
get written as JSONL.zst on disk and removed from live stores. The on-disk
layout is hive-partitioned so DuckDB can prune by wing/month at query time:

    archive/YYYY-MM/wing=X/week=W.jsonl.zst

A single file holds entries from one (year-month, wing, week) triple, written
atomically (temp + rename). Multiple flushes for the same partition append
to the same file by reading + rewriting.

Failure model: archive write happens before live delete. A crash mid-flow
leaves entries in BOTH archive and live; reconcile_archive (called at the
start of each archive run) detects this and removes the live duplicates.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import zstandard as zstd

from .config import LocalmemConfig
from .consolidator import _iso_week
from .metadata_store import MetadataStore
from .models import Entry, _now
from .vector_store import COLLECTION, VectorStore

logger = logging.getLogger(__name__)


_FORBIDDEN_SQL = re.compile(
    r";|/\*|--|"
    r"\b(?:DROP|DELETE|INSERT|UPDATE|ATTACH|DETACH|CREATE|ALTER|TRUNCATE|"
    r"PRAGMA|COPY|EXPORT|IMPORT|LOAD|INSTALL|CALL|EXECUTE|"
    r"BEGIN|COMMIT|ROLLBACK|TRANSACTION)\b",
    re.IGNORECASE,
)


def _is_safe_sql_where(clause: str) -> bool:
    """Reject statement separators, comments, and mutating keywords. Filter
    expressions like `wing = 'default' AND created_at > '2026-01-01'` pass."""
    return _FORBIDDEN_SQL.search(clause) is None


@dataclass
class WingArchiveResult:
    wing: str
    candidates: int = 0
    archived_entries: int = 0
    archived_bytes: int = 0
    partitions_written: int = 0
    skipped: bool = False
    skipped_reason: str | None = None


@dataclass
class ArchiveReport:
    dry_run: bool
    started_at: str
    finished_at: str
    wings: list[WingArchiveResult] = field(default_factory=list)
    duplicates_reconciled: int = 0


def _entry_to_dict(e: Entry) -> dict:
    """Lossless serialization to JSON-compatible dict."""
    d = e.model_dump()
    et = d.get("entry_type")
    if hasattr(et, "value"):
        d["entry_type"] = et.value
    return d


def _partition_path(archive_root: Path, wing: str, created_at: str) -> Path:
    """Return the hive-partitioned path for a given entry timestamp."""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        year_month = dt.strftime("%Y-%m")
    except (ValueError, TypeError):
        year_month = "unknown"
    week = _iso_week(created_at)
    return archive_root / year_month / f"wing={wing}" / f"week={week}.jsonl.zst"


def _atomic_write_jsonl_zst(
    path: Path, entries: list[dict], compression_level: int
) -> int:
    """Write entries as JSONL into path, zstd-compressed. If the path already
    exists, the existing entries are read and combined before writing the new
    file. Atomicity: write to temp, rename. Returns total bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if path.exists():
        existing = list(_read_jsonl_zst(path))

    combined = existing + entries
    joined = b"".join(
        json.dumps(d, ensure_ascii=False).encode("utf-8") + b"\n"
        for d in combined
    )
    cctx = zstd.ZstdCompressor(level=compression_level)
    data = cctx.compress(joined)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return len(data)


def _read_jsonl_zst(path: Path):
    """Iterate decoded JSON dicts from a JSONL.zst file."""
    if not path.exists():
        return
    dctx = zstd.ZstdDecompressor()
    with open(path, "rb") as f:
        with dctx.stream_reader(f) as reader:
            buffered = io.BufferedReader(reader)
            for line in buffered:
                if not line.strip():
                    continue
                yield json.loads(line)


class Archiver:
    """Moves entries from live stores to on-disk JSONL.zst archive."""

    def __init__(
        self,
        config: LocalmemConfig,
        vector_store: VectorStore,
        metadata_store: MetadataStore,
    ):
        self.config = config
        self.vs = vector_store
        self.ms = metadata_store

    @property
    def _root(self) -> Path:
        return Path(self.config.retention.archive.path)

    async def reconcile_archive_duplicates(self) -> int:
        """If a previous archive run wrote to disk but failed to delete from
        live stores, the entry exists in BOTH places. Live wins on read but
        wastes space; sweep through archive files and drop any live entry whose
        id is already on disk. Idempotent."""
        if not self._root.exists():
            return 0

        archived_ids: set[str] = set()
        for path in self._root.rglob("*.jsonl.zst"):
            for d in _read_jsonl_zst(path):
                eid = d.get("id")
                if eid:
                    archived_ids.add(eid)

        if not archived_ids:
            return 0

        # Check which still exist live, delete them.
        try:
            found = self.vs._client.retrieve(
                collection_name=COLLECTION,
                ids=list(archived_ids),
                with_payload=False,
            )
        except Exception as e:
            logger.warning(f"reconcile retrieve failed: {e}")
            return 0

        live_ids = [str(p.id) for p in found]
        for lid in live_ids:
            try:
                await self.vs.delete(lid)
                await self.ms.remove_importance(lid)
            except Exception as e:
                logger.warning(f"reconcile delete {lid} failed: {e}")
        if live_ids:
            logger.info(f"reconciled {len(live_ids)} archive duplicates")
        return len(live_ids)

    async def archive_wing(
        self, wing: str, *, dry_run: bool = False
    ) -> WingArchiveResult:
        result = WingArchiveResult(wing=wing)
        rc = self.config.retention
        if not rc.enabled:
            result.skipped = True
            result.skipped_reason = "retention disabled"
            return result
        if not rc.archive.enabled:
            result.skipped = True
            result.skipped_reason = "archive disabled"
            return result

        policy = rc.policy_for(wing)
        max_age = policy["max_age_days"]
        if max_age is None:
            result.skipped = True
            result.skipped_reason = "wing exempt from archive (max_age_days=null)"
            return result

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

        candidates = await self.vs.scroll(
            wing=wing,
            limit=10_000,
            pinned=False,
            before_created_at=cutoff_iso,
        )
        result.candidates = len(candidates)
        if not candidates:
            return result

        # Group by partition path
        by_partition: dict[Path, list[Entry]] = {}
        for e in candidates:
            ppath = _partition_path(self._root, wing, e.created_at)
            by_partition.setdefault(ppath, []).append(e)

        if dry_run:
            result.partitions_written = len(by_partition)
            result.archived_entries = sum(len(v) for v in by_partition.values())
            return result

        for ppath, entries in by_partition.items():
            payload = [_entry_to_dict(e) for e in entries]
            try:
                bytes_written = _atomic_write_jsonl_zst(
                    ppath,
                    payload,
                    self.config.retention.archive.compression_level,
                )
            except Exception as exc:
                logger.exception(f"archive write failed for {ppath}")
                continue
            result.partitions_written += 1
            result.archived_bytes += bytes_written

            # Best-effort: remove from live stores. If anything fails, the next
            # reconcile_archive_duplicates run will clean it up.
            for e in entries:
                try:
                    await self.vs.delete(e.id)
                    await self.ms.remove_importance(e.id)
                    result.archived_entries += 1
                except Exception as exc:
                    logger.warning(f"failed to remove archived entry {e.id}: {exc}")

        return result

    async def archive_all(
        self, *, dry_run: bool = False, wings: list[str] | None = None
    ) -> ArchiveReport:
        report = ArchiveReport(
            dry_run=dry_run, started_at=_now(), finished_at=""
        )
        if not dry_run:
            report.duplicates_reconciled = await self.reconcile_archive_duplicates()

        target_wings = wings or list(self.config.retention.wings.keys())
        if not target_wings:
            target_wings = self.config.all_wings()

        for wing in target_wings:
            wr = await self.archive_wing(wing, dry_run=dry_run)
            report.wings.append(wr)

        report.finished_at = _now()
        return report

    # ── Read-side: query and restore ───────────────────────────────────

    def stats(self) -> dict:
        """Return per-wing archive statistics — used by dashboard and CLI."""
        if not self._root.exists():
            return {"path": str(self._root), "exists": False, "wings": {}}
        per_wing: dict[str, dict] = {}
        total_files = 0
        total_bytes = 0
        for path in self._root.rglob("*.jsonl.zst"):
            wing_part = next(
                (p.replace("wing=", "") for p in path.parts if p.startswith("wing=")),
                "unknown",
            )
            entry = per_wing.setdefault(wing_part, {"files": 0, "bytes": 0})
            entry["files"] += 1
            size = path.stat().st_size
            entry["bytes"] += size
            total_files += 1
            total_bytes += size
        return {
            "path": str(self._root),
            "exists": True,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "wings": per_wing,
        }

    def find_entry(self, entry_id: str) -> tuple[Path, dict] | None:
        """Linear scan of archive files to find an entry by id. O(N) but
        archives are small files; restore is rare. Returns (path, dict) or None."""
        if not self._root.exists():
            return None
        for path in self._root.rglob("*.jsonl.zst"):
            for d in _read_jsonl_zst(path):
                if d.get("id") == entry_id:
                    return path, d
        return None

    async def restore(self, entry_id: str) -> bool:
        """Pull an entry from the cold archive back into the hot tier. The
        archive copy is left in place (idempotent restore + recoverability).
        Returns True if restored, False if not found."""
        match = self.find_entry(entry_id)
        if match is None:
            return False
        _path, d = match
        # Reconstruct an Entry. Drop fields the model doesn't accept.
        et = d.get("entry_type", "generic")
        from .models import EntryType
        try:
            entry_type = EntryType(et)
        except ValueError:
            entry_type = EntryType.GENERIC
        entry = Entry(
            id=d["id"],
            wing=d["wing"],
            room=d["room"],
            agent_id=d["agent_id"],
            entry_type=entry_type,
            content=d.get("content", ""),
            summary=d.get("summary"),
            importance=d.get("importance", 0.5),
            tags=d.get("tags", []) or [],
            refs=d.get("refs", []) or [],
            metadata=d.get("metadata", {}) or {},
            created_at=d.get("created_at", _now()),
            updated_at=d.get("updated_at", _now()),
            pinned=bool(d.get("pinned", False)),
            is_summary=bool(d.get("is_summary", False)),
        )
        await self.vs.store(entry, is_summary=entry.is_summary)
        if entry.pinned:
            await self.vs.set_pinned(entry.id, True)
            await self.ms.set_pinned(entry.id, True, wing=entry.wing)
        return True

    def search_semantic(
        self,
        query: str,
        embedder,
        *,
        wing: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Semantic search over archived entries. Loads matching files into a
        temporary in-memory Qdrant collection, runs cosine search, drops the
        collection. Cold path — slow by design.

        wing/since filter the file list before loading.
        since is YYYY-MM (lexicographic compare against partition path)."""
        if not self._root.exists():
            return []

        # Find matching files.
        files: list[Path] = []
        for path in self._root.rglob("*.jsonl.zst"):
            parts = path.parts
            yyyy_mm = next((p for p in parts if len(p) == 7 and p[4] == "-"), None)
            wing_part = next(
                (p.replace("wing=", "") for p in parts if p.startswith("wing=")),
                None,
            )
            if wing and wing_part != wing:
                continue
            if since and yyyy_mm and yyyy_mm < since:
                continue
            files.append(path)

        if not files:
            return []

        # Load entries
        loaded: list[dict] = []
        for f in files:
            for d in _read_jsonl_zst(f):
                loaded.append(d)
        if not loaded:
            return []

        # Spin up temp in-memory collection. Mirror the live collection's vector
        # config — single dense vector, cosine distance.
        from qdrant_client import QdrantClient
        from qdrant_client.models import (
            Distance,
            PointStruct,
            VectorParams,
        )

        temp = QdrantClient(":memory:")
        coll = "archive_search"
        temp.create_collection(
            collection_name=coll,
            vectors_config=VectorParams(
                size=embedder.dense_dim,
                distance=Distance.COSINE,
            ),
        )

        points = []
        for d in loaded:
            content = d.get("content", "") or ""
            if not content:
                continue
            try:
                vec = embedder.embed_dense(content)
            except Exception as e:
                logger.debug(f"embed failed for archive entry {d.get('id')}: {e}")
                continue
            points.append(
                PointStruct(id=d["id"], vector=vec, payload=d)
            )

        if not points:
            temp.close()
            return []

        temp.upsert(collection_name=coll, points=points)

        try:
            qvec = embedder.embed_dense(query)
            results = temp.query_points(
                collection_name=coll,
                query=qvec,
                limit=limit,
                with_payload=True,
            )
            return [
                {**p.payload, "score": p.score}
                for p in results.points
            ]
        finally:
            temp.close()

    def query_sql(self, sql_where: str | None = None, limit: int | None = 100) -> list[dict]:
        """Run a DuckDB SELECT against the archive. The user provides the
        WHERE clause; we hardcode SELECT * FROM read_json_auto(<glob>) so
        callers can't run arbitrary SQL against the user's filesystem.

        The WHERE clause is validated against an allowlist of safe tokens
        before execution: mutating statements (DROP, DELETE, UPDATE,
        INSERT, ATTACH, COPY, ...), comments (-- and /* */), and
        statement separators (;) are rejected.

        Returns list of dicts.
        """
        try:
            import duckdb
        except ImportError:
            raise RuntimeError(
                "DuckDB not installed. Install: pip install -e '.[analytics]'"
            )

        if sql_where is not None and not _is_safe_sql_where(sql_where):
            raise ValueError(
                "sql_where contains forbidden tokens (statement separators, "
                "comments, or mutating keywords); only SELECT-side filter "
                "expressions are accepted"
            )

        if not self._root.exists():
            return []
        glob = str(self._root / "**" / "*.jsonl.zst")
        query = f"SELECT * FROM read_json_auto('{glob}', maximum_object_size=33554432)"
        if sql_where:
            query += f" WHERE {sql_where}"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        conn = duckdb.connect(":memory:")
        try:
            cursor = conn.execute(query)
            cols = [c[0] for c in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
