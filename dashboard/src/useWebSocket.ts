/**
 * WebSocket hook — topic-routed messages into the Zustand store.
 * Auto-reconnects with exponential backoff.
 */

import { useEffect, useRef } from "react";
import { getApiKey } from "./api";
import { useStore } from "./store";
import type {
  AlertResponse,
  HealthResponse,
  MetricsResponse,
  WSMessage,
} from "./types";

const WS_URL =
  import.meta.env.VITE_WS_URL ??
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

function route(msg: WSMessage) {
  const s = useStore.getState();
  switch (msg.topic) {
    case "health":
      s.setHealth(msg.data as unknown as HealthResponse);
      break;
    case "metrics":
      s.setMetrics(msg.data as unknown as MetricsResponse);
      break;
    case "alerts": {
      const newAlerts = (msg.data as { new?: AlertResponse[] }).new ?? [];
      if (newAlerts.length) s.pushAlerts(newAlerts);
      break;
    }
    case "logs": {
      const lines = (msg.data as { lines?: { level: string; message: string; timestamp: string }[] }).lines ?? [];
      if (lines.length) s.pushLogs(lines);
      break;
    }
    case "system":
    case "entries":
      break;
  }
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectDelay = useRef(RECONNECT_BASE_MS);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    function connect() {
      if (wsRef.current?.readyState === WebSocket.OPEN) return;

      const token = getApiKey();
      // Auth via the Sec-WebSocket-Protocol subprotocol list. The server
      // sees ["bearer", <token>], accepts the handshake with subprotocol
      // "bearer" only, and never echoes the token back. Sending the token
      // as a separate subprotocol value (rather than concatenated as
      // `bearer.<token>`) keeps it out of the 101 Switching Protocols
      // response that proxies and devtools can see.
      const ws = token
        ? new WebSocket(WS_URL, ["bearer", token])
        : new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        useStore.getState().setConnected(true);
        reconnectDelay.current = RECONNECT_BASE_MS;
      };

      ws.onmessage = (event) => {
        try {
          const msg: WSMessage = JSON.parse(event.data);
          route(msg);
        } catch {
          // ignore malformed
        }
      };

      ws.onclose = () => {
        useStore.getState().setConnected(false);
        scheduleReconnect();
      };

      ws.onerror = () => ws.close();
    }

    function scheduleReconnect() {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectTimer.current = setTimeout(() => {
        reconnectDelay.current = Math.min(
          reconnectDelay.current * 2,
          RECONNECT_MAX_MS
        );
        connect();
      }, reconnectDelay.current);
    }

    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, []);
}
