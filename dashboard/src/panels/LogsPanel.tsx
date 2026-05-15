import { useStore } from "../store";
import { panelStyle, headerStyle, theme } from "../theme";

const LEVEL_COLOR: Record<string, string> = {
  DEBUG: "#8b949e",
  INFO: "#e6e8eb",
  WARNING: "#eab308",
  ERROR: "#ef4444",
  CRITICAL: "#ef4444",
};

export function LogsPanel() {
  const logs = useStore((s) => s.logs);

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>Logs (streamed)</div>
      <div style={{ fontFamily: theme.mono, fontSize: 11, whiteSpace: "pre-wrap" }}>
        {logs.length === 0 && (
          <div style={{ color: theme.textDim }}>
            No log lines yet. Enable `logging.file` and the server will push lines via WS.
          </div>
        )}
        {logs.map((l, i) => (
          <div key={i} style={{ color: LEVEL_COLOR[l.level] ?? theme.text, marginBottom: 2 }}>
            <span style={{ color: theme.textDim }}>{l.timestamp?.slice(11, 19) ?? ""}</span>{" "}
            <span>[{l.level}]</span>{" "}
            <span>{l.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
