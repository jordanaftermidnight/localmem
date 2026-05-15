"""LOCALMEM embedding-model migration.

Re-embeds every entry in the live Qdrant collection with a different model
(typically MiniLM 384d → BGE-large 1024d). Because the dense vector dim
changes, we have to recreate the collection — there is no in-place "swap
the embeddings" path in Qdrant.

This is an offline migration: the caller must stop the MCP/dashboard
server first. We check by attempting to bind to its port. Qdrant local has
no cross-process concurrency control, so running this with the server up
would corrupt state.

Failure recovery: before doing anything destructive, the entire
`storage.qdrant_path` directory is rsync-copied to
`<qdrant_path>.backup-<ts>`. If migration fails part-way through, the
backup directory is left in place; restore by stopping the (already
stopped) server, swapping the dirs, and re-running with the original
model.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import LocalmemConfig
from .embedder import DENSE_DIMS, Embedder
from .vector_store import COLLECTION, create_entries_collection

logger = logging.getLogger(__name__)


@dataclass
class MigrationProgress:
    total: int = 0
    embedded: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed_ids: list[str] = field(default_factory=list)


@dataclass
class MigrationReport:
    dry_run: bool
    source_model: str
    target_model: str
    source_dim: int
    target_dim: int
    backup_path: str | None
    progress: MigrationProgress
    started_at: str
    finished_at: str
    error: str | None = None


def _server_port_open(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect((host, port))
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


class EmbeddingMigrator:
    """Migrates the live Qdrant collection from one embedding model to another.

    Steps (when --apply is set):
      1. Safety check: refuse if the dashboard/MCP port is bound by anything.
      2. Snapshot: copy storage.qdrant_path to <qdrant_path>.backup-<ts>.
      3. Dump: scroll all entries to a JSONL temp file, preserving payload.
      4. Drop and recreate the collection with the target model's dim.
      5. Re-embed and upsert in batches.
      6. Leave the backup in place for the operator to remove manually.
    """

    def __init__(
        self,
        config: LocalmemConfig,
        target_model: str,
        *,
        target_sparse_model: str | None = None,
    ):
        self.config = config
        self.target_model = target_model
        self.target_sparse_model = (
            target_sparse_model or config.embedding.sparse_model
        )

    def _safety_check(self) -> str | None:
        # Qdrant server mode supports concurrent writers — no port-bind check
        # needed; the operator is responsible for restarting consumers after
        # the collection is recreated.
        if self.config.storage.qdrant_mode == "server":
            return None
        ds = self.config.dashboard
        if _server_port_open(ds.host, ds.port):
            return (
                f"Dashboard appears to be running on {ds.host}:{ds.port}. "
                "Stop it first; Qdrant local has no cross-process concurrency."
            )
        return None

    def _snapshot(self) -> str:
        # Server mode stores data on the remote Qdrant — snapshotting is
        # the operator's responsibility (qdrant snapshot API).
        if self.config.storage.qdrant_mode == "server":
            return ""
        src = Path(self.config.storage.qdrant_path)
        if not src.exists():
            return ""
        ts = time.strftime("%Y%m%d-%H%M%S")
        dst = src.with_name(f"{src.name}.backup-{ts}")
        shutil.copytree(src, dst)
        logger.info(f"qdrant snapshot taken: {dst}")
        return str(dst)

    def _build_target_embedder(self) -> Embedder:
        # Build a config copy with the target model + load.
        target_cfg = self.config.model_copy(deep=True)
        target_cfg.embedding.model = self.target_model
        target_cfg.embedding.sparse_model = self.target_sparse_model
        embedder = Embedder(target_cfg)
        embedder.load()
        return embedder

    def _build_source_embedder(self) -> Embedder:
        embedder = Embedder(self.config)
        embedder.load()
        return embedder

    @staticmethod
    def _dim_for(model: str) -> int:
        return DENSE_DIMS.get(model, DENSE_DIMS.get(model.split("/")[-1], 384))

    async def migrate(
        self,
        *,
        dry_run: bool = False,
        batch_size: int = 500,
    ) -> MigrationReport:
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct

        progress = MigrationProgress()
        source_model = self.config.embedding.model
        source_dim = self._dim_for(source_model)
        target_dim = self._dim_for(self.target_model)

        report = MigrationReport(
            dry_run=dry_run,
            source_model=source_model,
            target_model=self.target_model,
            source_dim=source_dim,
            target_dim=target_dim,
            backup_path=None,
            progress=progress,
            started_at=_now(),
            finished_at="",
        )

        if source_model == self.target_model:
            report.error = "source and target models are identical"
            report.finished_at = _now()
            return report

        if not dry_run:
            err = self._safety_check()
            if err:
                report.error = err
                report.finished_at = _now()
                return report

        # Open client. Local mode opens the path lock (fail fast if held);
        # server mode connects over HTTP.
        if self.config.storage.qdrant_mode == "server":
            client = QdrantClient(
                url=self.config.storage.qdrant_url,
                api_key=self.config.storage.qdrant_api_key,
            )
        else:
            client = QdrantClient(path=self.config.storage.qdrant_path)
        try:
            existing = [c.name for c in client.get_collections().collections]
            if COLLECTION not in existing:
                report.error = f"collection '{COLLECTION}' not found"
                report.finished_at = _now()
                return report

            count = client.count(collection_name=COLLECTION, exact=True).count
            progress.total = count

            if dry_run:
                report.finished_at = _now()
                logger.info(
                    f"dry-run: would migrate {count} entries from "
                    f"{source_model} ({source_dim}d) to {self.target_model} ({target_dim}d)"
                )
                return report

            # Phase 1: snapshot
            report.backup_path = self._snapshot()

            # Phase 2: dump to JSONL temp file
            dump_path = (
                Path(self.config.storage.qdrant_path).parent
                / f"migration-dump-{int(time.time())}.jsonl"
            )
            with open(dump_path, "w", encoding="utf-8") as f:
                offset = None
                while True:
                    records, offset = client.scroll(
                        collection_name=COLLECTION,
                        limit=batch_size,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    for record in records:
                        f.write(
                            json.dumps({
                                "id": str(record.id),
                                "payload": record.payload,
                            }, ensure_ascii=False)
                            + "\n"
                        )
                    if offset is None:
                        break
            logger.info(f"dumped {progress.total} entries to {dump_path}")

            # Phase 3: drop + recreate with target dim using the shared
            # collection factory so VectorStore and the migrator stay in
            # sync on payload-index schema.
            client.delete_collection(collection_name=COLLECTION)
            create_entries_collection(client, dense_dim=target_dim)

            # Phase 4: re-embed in batches and upload
            target_embedder = self._build_target_embedder()
            with open(dump_path, "r", encoding="utf-8") as f:
                batch: list[PointStruct] = []
                for line in f:
                    rec = json.loads(line)
                    payload = rec.get("payload") or {}
                    content = payload.get("content", "") or ""
                    if not content:
                        progress.skipped += 1
                        continue
                    try:
                        dense = target_embedder.embed_dense(content)
                        sparse = target_embedder.embed_sparse(content)
                    except Exception as exc:
                        logger.warning(f"embed failed for {rec['id']}: {exc}")
                        progress.failed_ids.append(rec["id"])
                        continue
                    progress.embedded += 1
                    vectors: dict = {"dense": dense}
                    if sparse is not None:
                        vectors["sparse"] = sparse
                    batch.append(PointStruct(id=rec["id"], vector=vectors, payload=payload))
                    if len(batch) >= batch_size:
                        client.upsert(collection_name=COLLECTION, points=batch)
                        progress.uploaded += len(batch)
                        batch.clear()
                if batch:
                    client.upsert(collection_name=COLLECTION, points=batch)
                    progress.uploaded += len(batch)

            # Update config in-memory so a follow-up vector_store load uses
            # the new dim. (The yaml on disk is the operator's responsibility
            # to update; we log a reminder.)
            self.config.embedding.model = self.target_model
            self.config.embedding.sparse_model = self.target_sparse_model
            logger.info(
                "migration complete; remember to update localmem.yaml "
                f"embedding.model to '{self.target_model}'"
            )

        except Exception as exc:
            logger.exception("embedding migration failed")
            report.error = str(exc)
        finally:
            try:
                client.close()
            except Exception:
                pass
            report.finished_at = _now()

        return report
