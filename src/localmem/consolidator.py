"""LOCALMEM consolidator — turns groups of stale low-importance entries into
single summary entries. Source entries are deleted from live stores; the
summary remains searchable. The mapping is preserved in `consolidated_sources`
so any source can be traced back to its summary.

Failure model: operations are ordered so a crash leaves the system over-counting
(safe) rather than under-counting (lossy). On the next run, `reconcile_orphans`
detects partial progress and finishes the work."""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import LocalmemConfig
from .graph_store import GraphStore
from .metadata_store import MetadataStore
from .models import Entry, EntryType, _now
from .summarizer import TemplateSummarizer
from .summarizer_llm import build_summarizer
from .vector_store import COLLECTION, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class GroupResult:
    group_key: tuple[str, str, str]   # (wing, room, week)
    entry_count: int
    summary_id: str | None = None     # None when dry_run
    skipped_reason: str | None = None


@dataclass
class WingResult:
    wing: str
    candidates: int = 0
    consolidated_groups: int = 0
    consolidated_entries: int = 0
    skipped_groups: int = 0
    groups: list[GroupResult] = field(default_factory=list)
    error: str | None = None


@dataclass
class ConsolidationReport:
    dry_run: bool
    started_at: str
    finished_at: str
    wings: list[WingResult] = field(default_factory=list)
    orphans_reconciled: int = 0


def _iso_week(ts: str) -> str:
    """Return ISO week label like '2026-W12' from an ISO-8601 timestamp.
    Falls back to the year if parsing fails — this keeps consolidation
    progressing even on malformed timestamps."""
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"
    except (ValueError, TypeError):
        return ts[:4] if len(ts) >= 4 else "unknown"


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class Consolidator:
    def __init__(
        self,
        config: LocalmemConfig,
        vector_store: VectorStore,
        metadata_store: MetadataStore,
        graph_store: GraphStore | None = None,
        summarizer: TemplateSummarizer | None = None,
    ):
        self.config = config
        self.vs = vector_store
        self.ms = metadata_store
        self.gs = graph_store
        self.summarizer = summarizer or build_summarizer(config)

    async def reconcile_orphans(self) -> int:
        """Find entries listed in consolidated_sources that still have a Qdrant
        point. Such entries are remnants of a partially-failed consolidation —
        delete them from Qdrant. Returns count of points deleted."""
        async with self.ms._connect() as db:
            await self.ms._setup(db)
            cursor = await db.execute(
                "SELECT DISTINCT source_id FROM consolidated_sources"
            )
            rows = await cursor.fetchall()

        source_ids = [r["source_id"] for r in rows]
        if not source_ids:
            return 0

        # Qdrant retrieve only returns points that exist; the rest are silently
        # absent. We then delete the ones still around.
        try:
            found = self.vs._client.retrieve(
                collection_name=COLLECTION,
                ids=source_ids,
                with_payload=False,
            )
        except Exception as e:
            logger.warning(f"reconcile_orphans retrieve failed: {e}")
            return 0

        ghost_ids = [str(p.id) for p in found]
        for gid in ghost_ids:
            try:
                await self.vs.delete(gid)
            except Exception as e:
                logger.warning(f"reconcile_orphans delete {gid} failed: {e}")
        if ghost_ids:
            logger.info(f"reconciled {len(ghost_ids)} orphan source points")
        return len(ghost_ids)

    async def _gather_candidates(
        self, wing: str, cutoff_iso: str, importance_floor: float
    ) -> list[Entry]:
        """Pull entries older than cutoff from the wing, excluding pinned and
        already-summary entries, then filter to importance < floor."""
        # Scroll in batches; importance check is a per-entry SQL lookup.
        batch_size = 500
        candidates: list[Entry] = []
        # Single scroll call captures all candidates that pass before_created_at
        # — for very large wings we'd need pagination, but Qdrant local scroll
        # with limit=10_000 is fine for normal volumes.
        entries = await self.vs.scroll(
            wing=wing,
            limit=10_000,
            pinned=False,
            is_summary=False,
            before_created_at=cutoff_iso,
        )

        # Importance filter — uses MetadataStore time-decayed effective score
        # rather than the raw payload importance (which doesn't decay).
        if not entries:
            return []
        ids = [e.id for e in entries]
        scored = await self._effective_scores(ids)
        for e in entries:
            eff = scored.get(e.id)
            # Entries without an importance row default to base 0.5 with no
            # access history — fall back to the payload importance value.
            score = eff if eff is not None else e.importance
            if score < importance_floor:
                candidates.append(e)
            if len(candidates) >= batch_size * 20:
                break  # safety bound
        return candidates

    async def _effective_scores(self, entry_ids: list[str]) -> dict[str, float]:
        """Batch fetch effective scores. Returns {id: score} only for entries
        that have an importance row."""
        if not entry_ids:
            return {}
        placeholders = ",".join(["?"] * len(entry_ids))
        async with self.ms._connect() as db:
            await self.ms._setup(db)
            cursor = await db.execute(
                f"""SELECT entry_id, base_score, access_count, last_accessed, decay_rate
                    FROM importance WHERE entry_id IN ({placeholders})""",
                entry_ids,
            )
            rows = await cursor.fetchall()
        scores: dict[str, float] = {}
        for r in rows:
            scores[r["entry_id"]] = MetadataStore._effective_score(
                r["base_score"],
                r["access_count"],
                r["last_accessed"],
                r["decay_rate"],
            )
        return scores

    def _group_entries(
        self, entries: Iterable[Entry]
    ) -> dict[tuple[str, str, str], list[Entry]]:
        groups: dict[tuple[str, str, str], list[Entry]] = defaultdict(list)
        for e in entries:
            groups[(e.wing, e.room, _iso_week(e.created_at))].append(e)
        return groups

    async def _consolidate_group(
        self,
        group_key: tuple[str, str, str],
        entries: list[Entry],
        *,
        dry_run: bool,
    ) -> GroupResult:
        wing, room, week = group_key
        group_label = f"{wing}/{room} {week}"

        if dry_run:
            return GroupResult(
                group_key=group_key,
                entry_count=len(entries),
                summary_id=None,
            )

        bundle = self.summarizer.summarize(entries, group_label)

        summary = Entry(
            wing=wing,
            room=room,
            agent_id="localmem-consolidator",
            entry_type=EntryType.GENERIC,
            content=bundle["text"],
            summary=f"Consolidated {len(entries)} entries from {group_label}",
            importance=bundle["max_importance"],
            tags=["consolidated", f"week-{week}"],
            metadata={
                "source_count": len(entries),
                "date_range": bundle["date_range"],
                "top_terms": bundle["top_terms"],
                "group_key": f"{wing}/{room}/{week}",
            },
        )

        # Step 1: Insert summary into Qdrant. After this point the summary is
        # discoverable by search; if we crash, reconcile_orphans on next run
        # will clean up any source points still around.
        await self.vs.store(summary, is_summary=True)

        # Step 2: Persist source linkage — so future searches can answer
        # "what sources fed into this summary?" even after Qdrant deletes.
        sources_meta = [
            {
                "source_id": e.id,
                "source_text_hash": _content_hash(e.content),
                "source_importance": e.importance,
                "source_wing": e.wing,
                "source_room": e.room,
                "source_created_at": e.created_at,
            }
            for e in entries
        ]
        await self.ms.add_consolidated_sources(summary.id, sources_meta)
        for e in entries:
            await self.ms.remove_importance(e.id)

        # Step 3: Best-effort delete of source points. Failures here are
        # picked up by reconcile_orphans on the next run.
        for e in entries:
            try:
                await self.vs.delete(e.id)
            except Exception as exc:
                logger.warning(f"failed to delete source point {e.id}: {exc}")

        # Step 4: Best-effort graph rewire — copy edges from source nodes onto
        # the summary node so behavioral patterns survive consolidation. We
        # skip the rewire if the graph store isn't attached.
        if self.gs is not None:
            await self._rewire_graph(summary.id, [e.id for e in entries])

        return GroupResult(
            group_key=group_key,
            entry_count=len(entries),
            summary_id=summary.id,
        )

    async def _rewire_graph(self, summary_id: str, source_ids: list[str]) -> None:
        """Add a summary node and copy all edges from source nodes onto it.
        Source nodes are then removed. Failures are logged but non-fatal."""
        try:
            g = self.gs._graph
            if not any(sid in g for sid in source_ids):
                return  # nothing to rewire
            await self.gs.add_node(summary_id, {"type": "summary"})
            for sid in source_ids:
                if sid not in g:
                    continue
                for neighbor in list(g.successors(sid)):
                    edge_data = g.get_edge_data(sid, neighbor) or {}
                    await self.gs.add_edge(summary_id, neighbor, dict(edge_data))
                for predecessor in list(g.predecessors(sid)):
                    edge_data = g.get_edge_data(predecessor, sid) or {}
                    await self.gs.add_edge(predecessor, summary_id, dict(edge_data))
                await self.gs.remove_node(sid)
        except Exception as exc:
            logger.warning(f"graph rewire failed for summary {summary_id}: {exc}")

    async def consolidate_wing(
        self, wing: str, *, dry_run: bool = False
    ) -> WingResult:
        result = WingResult(wing=wing)
        rc = self.config.retention
        if not rc.enabled:
            result.error = "retention disabled"
            return result

        policy = rc.policy_for(wing)
        soft_age = policy["soft_age_days"]
        if soft_age is None:
            result.error = "no soft_age_days configured"
            return result

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=soft_age)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

        try:
            candidates = await self._gather_candidates(
                wing, cutoff_iso, policy["importance_floor"]
            )
        except Exception as exc:
            logger.exception("candidate gather failed")
            result.error = f"candidate gather failed: {exc}"
            return result

        result.candidates = len(candidates)
        if not candidates:
            return result

        groups = self._group_entries(candidates)
        min_size = rc.consolidation.min_group_size

        for group_key, entries in groups.items():
            if len(entries) < min_size:
                result.skipped_groups += 1
                result.groups.append(
                    GroupResult(
                        group_key=group_key,
                        entry_count=len(entries),
                        skipped_reason=f"below min_group_size={min_size}",
                    )
                )
                continue
            try:
                gr = await self._consolidate_group(group_key, entries, dry_run=dry_run)
                result.groups.append(gr)
                if gr.summary_id is not None:
                    result.consolidated_groups += 1
                    result.consolidated_entries += gr.entry_count
            except Exception as exc:
                logger.exception(f"consolidation failed for {group_key}")
                result.groups.append(
                    GroupResult(
                        group_key=group_key,
                        entry_count=len(entries),
                        skipped_reason=f"error: {exc}",
                    )
                )
        return result

    async def consolidate_all(
        self, *, dry_run: bool = False, wings: list[str] | None = None
    ) -> ConsolidationReport:
        report = ConsolidationReport(
            dry_run=dry_run,
            started_at=_now(),
            finished_at="",
        )
        if not dry_run:
            report.orphans_reconciled = await self.reconcile_orphans()

        target_wings = wings or list(self.config.retention.wings.keys())
        if not target_wings:
            target_wings = self.config.all_wings()

        for wing in target_wings:
            wr = await self.consolidate_wing(wing, dry_run=dry_run)
            report.wings.append(wr)

        report.finished_at = _now()
        return report
