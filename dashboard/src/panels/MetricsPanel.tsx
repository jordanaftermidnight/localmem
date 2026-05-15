import { useEffect } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { panelStyle, headerStyle, theme } from "../theme";

export function MetricsPanel() {
  const metrics = useStore((s) => s.metrics);
  const setMetrics = useStore((s) => s.setMetrics);

  useEffect(() => {
    api.metrics().then(setMetrics).catch(() => {});
  }, [setMetrics]);

  if (!metrics) return <div style={panelStyle}>Loading metrics…</div>;

  const tools = Object.entries(metrics.tools).sort(
    (a, b) => b[1].calls - a[1].calls
  );

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>Runtime Metrics</div>
      <div style={{ display: "flex", gap: 16, marginBottom: 12 }}>
        <Stat label="Calls" value={String(metrics.total_calls)} />
        <Stat label="Errors" value={String(metrics.total_errors)}
              color={metrics.total_errors > 0 ? theme.red : theme.green} />
        <Stat label="Uptime" value={`${Math.round(metrics.uptime_seconds)}s`} />
        <Stat label="Tools" value={String(tools.length)} />
      </div>

      <div style={headerStyle}>Per-tool Latency (ms)</div>
      <table style={{ width: "100%", fontFamily: theme.mono, fontSize: 12, borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ color: theme.textDim, textAlign: "left" }}>
            <th style={th}>Tool</th>
            <th style={th}>Calls</th>
            <th style={th}>Err</th>
            <th style={th}>p50</th>
            <th style={th}>p95</th>
            <th style={th}>p99</th>
            <th style={th}>avg</th>
          </tr>
        </thead>
        <tbody>
          {tools.map(([name, m]) => (
            <tr key={name} style={{ borderTop: `1px solid ${theme.panelBorder}` }}>
              <td style={td}>{name.replace(/^localmem_/, "")}</td>
              <td style={td}>{m.calls}</td>
              <td style={{ ...td, color: m.errors > 0 ? theme.red : theme.textDim }}>{m.errors}</td>
              <td style={td}>{m.latency_ms.p50.toFixed(1)}</td>
              <td style={td}>{m.latency_ms.p95.toFixed(1)}</td>
              <td style={td}>{m.latency_ms.p99.toFixed(1)}</td>
              <td style={td}>{m.latency_ms.avg.toFixed(1)}</td>
            </tr>
          ))}
          {tools.length === 0 && (
            <tr><td colSpan={7} style={{ ...td, color: theme.textDim }}>No calls recorded yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

const th: React.CSSProperties = { padding: "4px 8px", fontWeight: 600 };
const td: React.CSSProperties = { padding: "4px 8px" };

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: 10, textTransform: "uppercase", color: theme.textDim }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: color ?? theme.text }}>{value}</div>
    </div>
  );
}
