"""Pattern detection and cross-wing correlation engine.

Four generic detectors operate on the same storage substrate (vector store,
metadata store, behavioral graph). Each one is opt-in via
`config.intelligence.detectors.*` — none of them run unless the user names a
wing/room/node-prefix to watch.

  - tool_sequences       — frequent A→B transitions in a JSON-event stream.
  - provider_preferences — dominant/split/rotating choice patterns per category.
  - node_clusters        — Louvain communities over a typed/prefixed subgraph.
  - cross_wing           — temporal co-occurrence across two or more wings.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import LocalmemConfig
from .graph_store import GraphStore
from .metadata_store import MetadataStore
from .models import Entry, EntryType
from .vector_store import VectorStore

logger = logging.getLogger(__name__)


class IntelligenceEngine:
    """Composable pattern detectors over LOCALMEM storage.

    Every detector takes its target wing / room / node selector as an explicit
    argument (with optional defaults from config). Nothing about the engine is
    bound to a specific agent identity — callers compose detectors to fit
    whatever wing layout they configured.
    """

    def __init__(
        self,
        config: LocalmemConfig,
        vector_store: VectorStore,
        metadata_store: MetadataStore,
        graph_store: GraphStore,
    ):
        self.config = config
        self.vector_store = vector_store
        self.metadata_store = metadata_store
        self.graph_store = graph_store

    # ── Tool-sequence detector ────────────────────────────────────────────

    async def detect_tool_sequences(
        self,
        *,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Frequent A→B tool transitions in a JSON-event stream.

        Reads entries where `content` is JSON like
        `{"tool": "...", "success": true}`, orders them by `created_at`, and
        returns the most frequent adjacent pairs whose count meets
        `intelligence.pattern_min_frequency`.
        """
        cfg = self.config.intelligence.detectors.tool_sequences
        wing = wing or cfg.wing
        room = room or cfg.room
        if not wing or not room:
            return []

        entries = await self.vector_store.scroll(wing=wing, room=room, limit=500)
        if len(entries) < 2:
            return []

        tool_events = []
        for e in entries:
            try:
                data = json.loads(e.content)
                tool_events.append({
                    "tool": data.get("tool", "unknown"),
                    "success": data.get("success", True),
                    "ts": e.created_at,
                })
            except (json.JSONDecodeError, TypeError):
                continue

        if len(tool_events) < 2:
            return []

        tool_events.sort(key=lambda x: x["ts"])

        transitions: Counter[tuple[str, str]] = Counter()
        success_counts: dict[tuple[str, str], list[bool]] = defaultdict(list)
        for i in range(len(tool_events) - 1):
            pair = (tool_events[i]["tool"], tool_events[i + 1]["tool"])
            transitions[pair] += 1
            success_counts[pair].append(tool_events[i + 1]["success"])

        min_freq = self.config.intelligence.pattern_min_frequency
        sequences = []
        for (src, dst), count in transitions.most_common():
            if count < min_freq:
                break
            successes = success_counts[(src, dst)]
            success_rate = sum(1 for s in successes if s) / len(successes)
            sequences.append({
                "sequence": [src, dst],
                "frequency": count,
                "success_rate": round(success_rate, 2),
            })

        return sequences

    # ── Provider-preference detector ──────────────────────────────────────

    async def detect_provider_preferences(
        self,
        *,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Dominant/split/rotating preference patterns per category.

        Reads entries where `content` is JSON like
        `{"task_type": "...", "provider": "..."}`, groups by `task_type`, and
        classifies the distribution of `provider` choices.
        """
        cfg = self.config.intelligence.detectors.provider_preferences
        wing = wing or cfg.wing
        room = room or cfg.room
        if not wing or not room:
            return []

        entries = await self.vector_store.scroll(wing=wing, room=room, limit=500)
        if not entries:
            return []

        by_task: dict[str, list[str]] = defaultdict(list)
        for e in entries:
            try:
                data = json.loads(e.content)
                task_type = data.get("task_type", "unknown")
                provider = data.get("provider", "unknown")
                by_task[task_type].append(provider)
            except (json.JSONDecodeError, TypeError):
                continue

        min_freq = self.config.intelligence.pattern_min_frequency
        preferences = []
        for task_type, providers in by_task.items():
            if len(providers) < min_freq:
                continue

            counts = Counter(providers)
            total = len(providers)
            top_provider, top_count = counts.most_common(1)[0]
            share = top_count / total

            if share > 0.7:
                pattern = "dominant"
            elif share > 0.3:
                pattern = "split"
            else:
                pattern = "rotating"

            distribution = {p: round(c / total, 2) for p, c in counts.most_common()}
            preferences.append({
                "task_type": task_type,
                "pattern": pattern,
                "preferred_provider": top_provider,
                "share": round(share, 2),
                "total_decisions": total,
                "distribution": distribution,
            })

        preferences.sort(key=lambda x: x["total_decisions"], reverse=True)
        return preferences

    # ── Node-cluster detector ─────────────────────────────────────────────

    async def detect_node_clusters(
        self,
        *,
        node_type: str | None = None,
        node_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Louvain communities over a typed/prefixed subgraph.

        Selects nodes whose `type` attribute equals `node_type` OR whose name
        starts with `node_prefix`, runs Louvain community detection on the
        induced subgraph, and returns clusters of size >= 2 sorted by cohesion.
        """
        cfg = self.config.intelligence.detectors.node_clusters
        node_type = node_type or cfg.node_type
        node_prefix = node_prefix or cfg.node_prefix
        if not node_type and not node_prefix:
            return []

        g = self.graph_store._graph

        selected = [
            n for n, data in g.nodes(data=True)
            if (node_type and data.get("type") == node_type)
            or (node_prefix and isinstance(n, str) and n.startswith(node_prefix))
        ]
        if len(selected) < 2:
            return []

        subgraph = g.subgraph(selected)
        if subgraph.number_of_edges() == 0:
            return []

        undirected = subgraph.to_undirected()
        try:
            from networkx.algorithms.community import louvain_communities
            communities = louvain_communities(undirected)
        except Exception:
            return []

        clusters = []
        for community in communities:
            if len(community) < 2:
                continue

            sub = undirected.subgraph(community)
            n = len(community)
            max_edges = n * (n - 1) / 2
            cohesion = sub.number_of_edges() / max_edges if max_edges > 0 else 0

            names = sorted(
                node.removeprefix(node_prefix) if node_prefix and isinstance(node, str)
                else (node if isinstance(node, str) else str(node))
                for node in community
            )

            clusters.append({
                "members": names,
                "size": n,
                "cohesion": round(cohesion, 2),
                "internal_edges": sub.number_of_edges(),
            })

        clusters.sort(key=lambda x: x["cohesion"], reverse=True)
        return clusters

    # ── Cross-wing correlation detector ──────────────────────────────────

    async def detect_cross_wing_correlations(
        self,
        *,
        wings: list[str] | None = None,
        window_hours: int | None = None,
    ) -> list[dict[str, Any]]:
        """Temporal co-occurrence across two or more wings.

        Buckets recent entries into 5-minute windows by wing and returns wing
        pairs whose co-occurrence rate clears
        `intelligence.correlation_min_strength`.
        """
        cfg = self.config.intelligence.detectors.cross_wing_correlations
        wings = wings or cfg.wings
        if not wings or len(wings) < 2:
            return []

        window = window_hours or self.config.intelligence.correlation_window_hours
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()

        all_entries: list[Entry] = []
        for wing in wings:
            entries = await self.vector_store.scroll(wing=wing, limit=200)
            recent = [e for e in entries if e.created_at >= cutoff]
            all_entries.extend(recent)

        if len(all_entries) < 4:
            return []

        buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for e in all_entries:
            try:
                dt = datetime.fromisoformat(e.created_at)
                bucket_key = dt.strftime("%Y-%m-%dT%H:") + f"{(dt.minute // 5) * 5:02d}"
                buckets[bucket_key][e.wing] += 1
            except (ValueError, TypeError):
                continue

        pair_counts: Counter[tuple[str, str]] = Counter()
        wing_totals: Counter[str] = Counter()
        for bucket_key, wings_in_bucket in buckets.items():
            active_wings = sorted(wings_in_bucket.keys())
            for w in active_wings:
                wing_totals[w] += 1
            for i in range(len(active_wings)):
                for j in range(i + 1, len(active_wings)):
                    pair_counts[(active_wings[i], active_wings[j])] += 1

        total_buckets = len(buckets)
        if total_buckets == 0:
            return []

        min_strength = self.config.intelligence.correlation_min_strength
        correlations = []
        for (w1, w2), count in pair_counts.most_common():
            min_wing_freq = min(wing_totals[w1], wing_totals[w2])
            strength = count / min_wing_freq if min_wing_freq > 0 else 0

            if strength < min_strength:
                continue

            correlations.append({
                "wings": [w1, w2],
                "co_occurrences": count,
                "total_buckets": total_buckets,
                "strength": round(strength, 2),
            })

        return correlations

    # ── Orchestrator ──────────────────────────────────────────────────────

    async def run_detection(self) -> dict[str, list[dict[str, Any]]]:
        """Run every configured detector and return combined results.

        Each entry in the result is empty unless the corresponding detector
        is enabled (wing+room set, or node_type/node_prefix set, or 2+ wings
        configured for correlation).
        """
        return {
            "tool_sequences": await self.detect_tool_sequences(),
            "provider_preferences": await self.detect_provider_preferences(),
            "node_clusters": await self.detect_node_clusters(),
            "cross_wing": await self.detect_cross_wing_correlations(),
        }

    async def store_alerts(
        self, results: dict[str, list[dict[str, Any]]],
    ) -> int:
        """Persist significant patterns as entries in shared:<alert_room>.

        Each detector family produces alerts tagged with its own category so
        the wake-up loader and dashboard can filter them. Returns the number
        of alerts stored.
        """
        alert_room = self.config.intelligence.alert_room
        detectors_cfg = self.config.intelligence.detectors
        stored = 0

        tool_seq_wing = detectors_cfg.tool_sequences.wing
        for seq in results.get("tool_sequences", []):
            arrow = " -> ".join(seq["sequence"])
            tags = ["intelligence", "pattern", "tool-sequence"]
            if tool_seq_wing:
                tags.append(tool_seq_wing)
            entry = Entry(
                wing="shared",
                room=alert_room,
                agent_id="localmem",
                entry_type=EntryType.GENERIC,
                content=f"Tool sequence pattern: {arrow} (freq={seq['frequency']}, success={seq['success_rate']})",
                summary=f"Tool sequence: {arrow}",
                importance=min(seq["frequency"] * 0.1, 0.9),
                tags=tags,
                metadata=seq,
            )
            await self.vector_store.store(entry)
            await self.metadata_store.register_room("shared", alert_room)
            stored += 1

        provider_pref_wing = detectors_cfg.provider_preferences.wing
        for pref in results.get("provider_preferences", []):
            tags = ["intelligence", "pattern", "provider-preference"]
            if provider_pref_wing:
                tags.append(provider_pref_wing)
            entry = Entry(
                wing="shared",
                room=alert_room,
                agent_id="localmem",
                entry_type=EntryType.GENERIC,
                content=(
                    f"Provider preference: {pref['task_type']} -> "
                    f"{pref['preferred_provider']} ({pref['pattern']}, "
                    f"{pref['share']*100:.0f}%)"
                ),
                summary=f"{pref['pattern'].title()} preference for {pref['task_type']}",
                importance=0.5 + pref["share"] * 0.3,
                tags=tags,
                metadata=pref,
            )
            await self.vector_store.store(entry)
            await self.metadata_store.register_room("shared", alert_room)
            stored += 1

        for cluster in results.get("node_clusters", []):
            members = ", ".join(cluster["members"])
            entry = Entry(
                wing="shared",
                room=alert_room,
                agent_id="localmem",
                entry_type=EntryType.GENERIC,
                content=f"Node cluster: [{members}] cohesion={cluster['cohesion']}",
                summary=f"Node cluster ({cluster['size']} members)",
                importance=0.4 + cluster["cohesion"] * 0.5,
                tags=["intelligence", "pattern", "node-cluster"],
                metadata=cluster,
            )
            await self.vector_store.store(entry)
            await self.metadata_store.register_room("shared", alert_room)
            stored += 1

        for corr in results.get("cross_wing", []):
            pair = " <-> ".join(corr["wings"])
            entry = Entry(
                wing="shared",
                room=alert_room,
                agent_id="localmem",
                entry_type=EntryType.GENERIC,
                content=(
                    f"Cross-wing correlation: {pair} "
                    f"(strength={corr['strength']}, "
                    f"co-occurrences={corr['co_occurrences']})"
                ),
                summary=f"Cross-wing: {pair}",
                importance=0.5 + corr["strength"] * 0.4,
                tags=["intelligence", "pattern", "cross-wing"] + corr["wings"],
                metadata=corr,
            )
            await self.vector_store.store(entry)
            await self.metadata_store.register_room("shared", alert_room)
            stored += 1

        logger.info("Intelligence engine stored %d alerts", stored)
        return stored
