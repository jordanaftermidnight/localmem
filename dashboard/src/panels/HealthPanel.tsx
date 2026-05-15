import { useEffect } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { panelStyle, headerStyle, theme, wingColors } from "../theme";

export function HealthPanel() {
  const health = useStore((s) => s.health);
  const setHealth = useStore((s) => s.setHealth);
  const connected = useStore((s) => s.connected);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
  }, [setHealth]);

  if (!health) {
    return <div style={panelStyle}>Loading health…</div>;
  }

  const uptime = health.uptime_seconds;
  const h = Math.floor(uptime / 3600);
  const m = Math.floor((uptime % 3600) / 60);
  const s = Math.floor(uptime % 60);

  const storeDot = (status: string) =>
    status === "ok" ? theme.green : theme.red;

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>System Health</div>

      <div style={{ display: "flex", gap: 16, marginBottom: 16 }}>
        <Stat label="Status" value={health.status} color={health.status === "healthy" ? theme.green : theme.yellow} />
        <Stat label="Uptime" value={`${h}h ${m}m ${s}s`} />
        <Stat label="WS" value={connected ? "live" : "offline"} color={connected ? theme.green : theme.red} />
      </div>

      <div style={headerStyle}>Stores</div>
      <div style={{ display: "grid", gap: 8, marginBottom: 16 }}>
        <Row label="Vector" status={health.vector_store.status} dot={storeDot(health.vector_store.status)} />
        <Row label="Metadata" status={health.metadata_store.status} dot={storeDot(health.metadata_store.status)} />
        <Row label="Graph" status={health.graph_store.status} dot={storeDot(health.graph_store.status)} detail={`${health.graph_store.nodes ?? 0} nodes / ${health.graph_store.edges ?? 0} edges`} />
      </div>

      <div style={headerStyle}>Entries per Wing</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 16 }}>
        {Object.entries(health.vector_store.entries).map(([wing, count]) => (
          <div key={wing} style={{ display: "flex", justifyContent: "space-between", padding: "4px 8px", background: theme.panelBg, borderRadius: 4 }}>
            <span style={{ color: wingColors[wing] ?? theme.text }}>{wing}</span>
            <span style={{ fontFamily: theme.mono }}>{count}</span>
          </div>
        ))}
      </div>

      <div style={headerStyle}>Embedding</div>
      <div style={{ fontFamily: theme.mono, fontSize: 12, color: theme.textDim }}>
        {health.embedding.model}<br />
        device: {health.embedding.device}<br />
        sparse: {health.embedding.sparse ? "yes" : "no"}
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: 10, textTransform: "uppercase", color: theme.textDim }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: color ?? theme.text }}>{value}</div>
    </div>
  );
}

function Row({ label, status, dot, detail }: { label: string; status: string; dot: string; detail?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ width: 8, height: 8, borderRadius: 4, background: dot }} />
      <span style={{ flex: 1 }}>{label}</span>
      <span style={{ color: theme.textDim, fontSize: 12 }}>{detail ?? status}</span>
    </div>
  );
}
