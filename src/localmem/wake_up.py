"""LOCALMEM wake-up — L0–L3 layered context loading."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .config import LocalmemConfig
from .metadata_store import MetadataStore
from .models import Entry, SearchQuery, WakeContext
from .vector_store import VectorStore

logger = logging.getLogger(__name__)


class WakeUp:
    """Layered context loading for agent initialization."""

    def __init__(
        self,
        config: LocalmemConfig,
        vector_store: VectorStore,
        metadata_store: MetadataStore,
        manifests_dir: str = "manifests",
    ):
        self.config = config
        self.vector_store = vector_store
        self.metadata_store = metadata_store
        self.manifests_dir = Path(manifests_dir)

    async def load_l0(self, agent_id: str) -> dict:
        """L0: Static identity manifest (~50 tokens)."""
        manifest_path = self.manifests_dir / f"{agent_id}.yaml"
        if not manifest_path.exists():
            logger.warning(f"No manifest found for agent '{agent_id}'")
            return {"agent": agent_id, "role": "unknown", "capabilities": []}
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f) or {}
        logger.debug(f"L0 loaded for {agent_id}: {len(str(manifest))} chars")
        return manifest

    async def load_l1(self, agent_id: str, manifest: dict) -> list[Entry]:
        """L1: Top-K critical context by importance (~120 tokens)."""
        top_k = self.config.loading.l1_top_k

        # Get top entries by importance score
        top_entries = await self.metadata_store.get_top_entries(
            wing=agent_id, limit=top_k
        )

        entries = []
        for item in top_entries:
            entry = await self.vector_store.retrieve(item["entry_id"])
            if entry:
                entries.append(entry)

        # If we have wake_rooms in manifest, also pull latest from those
        wake_rooms = manifest.get("wake_rooms", [])
        for room_spec in wake_rooms:
            if ":" in room_spec:
                wing, room = room_spec.split(":", 1)
            else:
                wing, room = agent_id, room_spec

            results = await self.vector_store.search(
                SearchQuery(query="*", wing=wing, room=room, limit=3)
            )
            for r in results:
                if r.entry.id not in {e.id for e in entries}:
                    entries.append(r.entry)

        # Inject intelligence alerts relevant to this agent
        alert_room = self.config.intelligence.alert_room
        alerts = await self.vector_store.scroll(
            wing="shared", room=alert_room, limit=10
        )
        seen_ids = {e.id for e in entries}
        alert_count = 0
        for alert in alerts:
            if alert_count >= 3:
                break
            if alert.id in seen_ids:
                continue
            # Include alerts tagged with this agent or cross-wing alerts
            if agent_id in alert.tags or "cross-wing" in alert.tags:
                entries.append(alert)
                seen_ids.add(alert.id)
                alert_count += 1

        logger.debug(f"L1 loaded for {agent_id}: {len(entries)} entries")
        return entries[:top_k]

    async def wake(self, agent_id: str) -> WakeContext:
        """Full wake sequence: L0 + L1."""
        manifest = await self.load_l0(agent_id)
        entries = await self.load_l1(agent_id, manifest)

        # Estimate token count (rough: 1 token ≈ 4 chars)
        manifest_tokens = len(str(manifest)) // 4
        entry_tokens = sum(
            len(e.summary or e.content[:200]) // 4 for e in entries
        )

        context = WakeContext(
            agent_id=agent_id,
            l0_manifest=manifest,
            l1_entries=entries,
            total_tokens_estimate=manifest_tokens + entry_tokens,
        )
        logger.info(
            f"Wake complete for {agent_id}: ~{context.total_tokens_estimate} tokens"
        )
        return context
