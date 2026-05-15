"""LOCALMEM retention worker — single async task that orchestrates pruning
and archiving without touching the write hot-path.

Two trigger paths feed one worker:
  - Manual / cron-driven: HTTP POST /api/prune/run signals run_now()
  - Backpressure: MCP write path calls signal_dirty(wing) when a wing's
    entry count crosses trigger_count_per_wing.

The worker maintains:
  - a 'dirty' set of wings awaiting consideration
  - a 'last_run' timestamp per wing (for cooldown)
  - an asyncio.Semaphore(1) so only one consolidation/archive runs at a time
    (Qdrant local has no cross-worker concurrency control).

The worker is stateless across process restarts — last_run is in-memory.
That's by design: cron+startup will trigger a fresh run anyway."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .archiver import ArchiveReport, Archiver
from .config import LocalmemConfig
from .consolidator import ConsolidationReport, Consolidator
from .graph_store import GraphStore
from .metadata_store import MetadataStore
from .vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class WorkerStatus:
    running: bool = False
    last_consolidation_at: str | None = None
    last_archive_at: str | None = None
    last_consolidation: ConsolidationReport | None = None
    last_archive: ArchiveReport | None = None
    in_flight: bool = False
    queue_size: int = 0
    dirty_wings: list[str] = field(default_factory=list)


class BackgroundWorker:
    def __init__(
        self,
        config: LocalmemConfig,
        vector_store: VectorStore,
        metadata_store: MetadataStore,
        graph_store: GraphStore | None = None,
        consolidator: Consolidator | None = None,
        archiver: Archiver | None = None,
    ):
        self.config = config
        self.vs = vector_store
        self.ms = metadata_store
        self.gs = graph_store
        self.consolidator = consolidator or Consolidator(
            config, vector_store, metadata_store, graph_store=graph_store
        )
        self.archiver = archiver or Archiver(config, vector_store, metadata_store)

        self._dirty: set[str] = set()
        self._last_run: dict[str, float] = {}
        self._sem = asyncio.Semaphore(1)
        self._wakeup = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._last_consolidation: ConsolidationReport | None = None
        self._last_archive: ArchiveReport | None = None
        self._in_flight: bool = False
        # When set by run_now(), the next drain ignores cooldown/threshold
        # and runs the requested prune/archive across every dirty wing.
        self._force_full_run: tuple[bool, bool] | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="localmem-worker")

    async def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

    # ── Triggers ──────────────────────────────────────────────────────

    def signal_dirty(self, wing: str) -> None:
        """Cheap call from the write path — appends to dirty set + wakes loop.
        The actual count check + consolidation runs in the worker, so this
        adds at most a set.add and event.set() to the write path."""
        self._dirty.add(wing)
        self._wakeup.set()

    async def run_now(self, *, prune: bool = True, archive: bool = True) -> None:
        """Manually trigger a full run. Used by REST trigger and tests."""
        self._dirty.update(self.config.retention.wings.keys() or self.config.all_wings())
        self._force_full_run = (prune, archive)
        self._wakeup.set()

    def status(self) -> WorkerStatus:
        return WorkerStatus(
            running=self._task is not None and not self._task.done(),
            in_flight=self._in_flight,
            queue_size=len(self._dirty),
            dirty_wings=sorted(self._dirty),
            last_consolidation_at=(
                self._last_consolidation.finished_at if self._last_consolidation else None
            ),
            last_archive_at=(
                self._last_archive.finished_at if self._last_archive else None
            ),
            last_consolidation=self._last_consolidation,
            last_archive=self._last_archive,
        )

    # ── Internals ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        logger.info("retention worker started")
        try:
            while not self._stopping.is_set():
                await self._wakeup.wait()
                self._wakeup.clear()
                if self._stopping.is_set():
                    break
                await self._drain_dirty()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("retention worker crashed")
        finally:
            logger.info("retention worker stopped")

    async def _drain_dirty(self) -> None:
        rc = self.config.retention
        if not rc.enabled:
            self._dirty.clear()
            return

        force = self._force_full_run
        if force is not None:
            try:
                self._force_full_run = None
                prune, archive = force
                async with self._sem:
                    self._in_flight = True
                    try:
                        if prune:
                            await self._run_prune(list(self._dirty) or None)
                        if archive:
                            await self._run_archive(list(self._dirty) or None)
                    finally:
                        self._in_flight = False
                        self._dirty.clear()
                return
            except Exception:
                logger.exception("forced run failed")
                self._in_flight = False
                self._force_full_run = None
                return

        # Backpressure path: per-wing trigger
        now = time.time()
        cooldown = rc.consolidation.min_interval_seconds
        trigger_count = rc.consolidation.trigger_count_per_wing

        eligible = [
            w for w in list(self._dirty)
            if now - self._last_run.get(w, 0) >= cooldown
        ]
        if not eligible:
            return

        async with self._sem:
            self._in_flight = True
            try:
                for wing in eligible:
                    try:
                        count = await self.vs.count(wing=wing)
                    except Exception as exc:
                        logger.warning(f"count check failed for {wing}: {exc}")
                        continue
                    if count < trigger_count:
                        self._dirty.discard(wing)
                        continue
                    logger.info(
                        f"backpressure consolidation triggered for {wing} "
                        f"(count={count} >= {trigger_count})"
                    )
                    try:
                        report = await self.consolidator.consolidate_all(wings=[wing])
                        self._last_consolidation = report
                    except Exception:
                        logger.exception(f"consolidation failed for {wing}")
                    self._last_run[wing] = time.time()
                    self._dirty.discard(wing)
            finally:
                self._in_flight = False

    async def _run_prune(self, wings: list[str] | None) -> None:
        try:
            self._last_consolidation = await self.consolidator.consolidate_all(
                wings=wings
            )
        except Exception:
            logger.exception("prune run failed")

    async def _run_archive(self, wings: list[str] | None) -> None:
        try:
            self._last_archive = await self.archiver.archive_all(wings=wings)
        except Exception:
            logger.exception("archive run failed")
