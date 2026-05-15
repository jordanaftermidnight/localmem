"""Dashboard WebSocket manager — topic-based multiplexing.

Clients send {"action": "subscribe", "topics": [...]} to opt into topics.
Server pushes {"topic": "...", "data": {...}, "timestamp": "..."}.

Topics: health, metrics, alerts, entries, logs, system
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

logger = logging.getLogger(__name__)

TOPICS = ("health", "metrics", "alerts", "entries", "logs", "system")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


class ConnectionManager:
    """Tracks WS clients and their topic subscriptions."""

    def __init__(self) -> None:
        self._subs: dict[WebSocket, set[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, subprotocol: str | None = None) -> None:
        await ws.accept(subprotocol=subprotocol)
        async with self._lock:
            self._subs[ws] = set(TOPICS)  # default: all topics

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._subs.pop(ws, None)

    async def subscribe(self, ws: WebSocket, topics: list[str]) -> None:
        valid = {t for t in topics if t in TOPICS}
        async with self._lock:
            if ws in self._subs:
                self._subs[ws] = valid

    async def unsubscribe(self, ws: WebSocket, topics: list[str]) -> None:
        async with self._lock:
            if ws in self._subs:
                self._subs[ws] -= set(topics)

    async def broadcast(self, topic: str, data: dict[str, Any]) -> None:
        if topic not in TOPICS:
            return
        payload = json.dumps({"topic": topic, "data": data, "timestamp": _now()})
        dead: list[WebSocket] = []
        async with self._lock:
            targets = [
                ws for ws, subs in self._subs.items() if topic in subs
            ]
        for ws in targets:
            if ws.client_state != WebSocketState.CONNECTED:
                dead.append(ws)
                continue
            try:
                await ws.send_text(payload)
            except Exception as e:
                logger.debug(f"WS send failed, dropping client: {e}")
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._subs.pop(ws, None)

    @property
    def client_count(self) -> int:
        return len(self._subs)

    def get_topics(self, ws: WebSocket) -> list[str]:
        return sorted(self._subs.get(ws, set()))
