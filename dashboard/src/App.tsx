/**
 * LOCALMEM Dashboard — dockview IDE-style layout with live panels.
 */

import { useCallback } from "react";
import {
  DockviewReact,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
} from "dockview-react";
import "dockview-react/dist/styles/dockview.css";

import { useStore } from "./store";
import { useWebSocket } from "./useWebSocket";
import {
  HealthPanel,
  EntryBrowser,
  MetricsPanel,
  AlertsPanel,
  GraphPanel,
  TaxonomyTree,
  TriplesPanel,
  DiariesPanel,
  LogsPanel,
  AdminPanel,
} from "./panels";
import { theme } from "./theme";

const components: Record<string, React.FC<IDockviewPanelProps>> = {
  health: () => <HealthPanel />,
  entries: () => <EntryBrowser />,
  metrics: () => <MetricsPanel />,
  alerts: () => <AlertsPanel />,
  graph: () => <GraphPanel />,
  taxonomy: () => <TaxonomyTree />,
  triples: () => <TriplesPanel />,
  diaries: () => <DiariesPanel />,
  logs: () => <LogsPanel />,
  admin: () => <AdminPanel />,
};

function Header() {
  const connected = useStore((s) => s.connected);
  const health = useStore((s) => s.health);

  const uptime = health?.uptime_seconds ?? 0;
  const m = Math.floor(uptime / 60);
  const s = Math.floor(uptime % 60);
  const sep = <span style={{ color: theme.panelBorder }}>|</span>;

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 12,
      padding: "6px 12px", borderBottom: `1px solid ${theme.panelBorder}`,
      background: theme.panelBg, fontSize: 12,
    }}>
      <span style={{ fontWeight: 700, color: theme.accent, letterSpacing: 1 }}>LOCALMEM</span>
      <span style={{ color: theme.textDim }}>v0.1.0</span>
      {health && (
        <>
          {sep}
          <span style={{ color: theme.textDim }}>
            {health.embedding.model} ({health.embedding.device})
          </span>
          {sep}
          <span style={{ color: theme.textDim }}>{m}m {s}s</span>
        </>
      )}
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{
          width: 8, height: 8, borderRadius: 4,
          background: connected ? theme.green : theme.red,
        }} />
        <span style={{ color: connected ? theme.green : theme.red, fontSize: 11 }}>
          {connected ? "LIVE" : "DISCONNECTED"}
        </span>
      </div>
    </div>
  );
}

export function App() {
  useWebSocket();

  const onReady = useCallback((event: DockviewReadyEvent) => {
    const p1 = event.api.addPanel({
      id: "health",
      component: "health",
      title: "Health",
    });
    event.api.addPanel({
      id: "entries",
      component: "entries",
      title: "Entries",
      position: { referencePanel: p1.id, direction: "right" },
    });
    event.api.addPanel({
      id: "alerts",
      component: "alerts",
      title: "Alerts",
      position: { referencePanel: "entries", direction: "right" },
    });

    event.api.addPanel({
      id: "taxonomy",
      component: "taxonomy",
      title: "Wings/Rooms",
      position: { referencePanel: "health", direction: "below" },
    });
    event.api.addPanel({
      id: "metrics",
      component: "metrics",
      title: "Metrics",
      position: { referencePanel: "taxonomy", direction: "right" },
    });
    event.api.addPanel({
      id: "graph",
      component: "graph",
      title: "Graph",
      position: { referencePanel: "metrics", direction: "right" },
    });
    event.api.addPanel({
      id: "triples",
      component: "triples",
      title: "Triples",
      position: { referencePanel: "graph", direction: "within" },
    });
    event.api.addPanel({
      id: "diaries",
      component: "diaries",
      title: "Diaries",
      position: { referencePanel: "graph", direction: "within" },
    });
    event.api.addPanel({
      id: "logs",
      component: "logs",
      title: "Logs",
      position: { referencePanel: "graph", direction: "within" },
    });
    event.api.addPanel({
      id: "admin",
      component: "admin",
      title: "Admin",
      position: { referencePanel: "graph", direction: "within" },
    });
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <Header />
      <div style={{ flex: 1, minHeight: 0 }}>
        <DockviewReact
          components={components}
          onReady={onReady}
          className="dockview-theme-abyss"
        />
      </div>
    </div>
  );
}
