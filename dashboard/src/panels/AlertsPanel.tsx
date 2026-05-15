import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { headerStyle, panelStyle, tabBtnStyle, theme, wingColors } from "../theme";
import type { AlertResponse } from "../types";

export function AlertsPanel() {
  const [initial, setInitial] = useState<AlertResponse[]>([]);
  const [wing, setWing] = useState<string>("");
  const pushed = useStore((s) => s.alerts);
  const rooms = useStore((s) => s.rooms);

  useEffect(() => {
    api.alerts(wing || undefined, 50).then(setInitial).catch(() => {});
  }, [wing]);

  const combined = dedupe([...pushed, ...initial]);

  const importance = (i: number) =>
    i >= 0.9 ? theme.red : i >= 0.7 ? theme.yellow : theme.accent;

  const wings = ["", ...Array.from(new Set(rooms.map(r => r.wing))).sort()];

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>Intelligence Alerts</div>
      <div style={{ display: "flex", gap: 4, marginBottom: 10, fontSize: 11 }}>
        {wings.map((w) => (
          <button
            key={w || "all"}
            onClick={() => setWing(w)}
            style={tabBtnStyle(wing === w)}
          >{w || "all"}</button>
        ))}
      </div>

      {combined.length === 0 && (
        <div style={{ color: theme.textDim }}>No alerts. Run `localmem intelligence detect`.</div>
      )}

      {combined.map((a) => (
        <div key={a.id} style={{
          padding: 8, marginBottom: 6, background: theme.panelBg, borderRadius: 4,
          borderLeft: `3px solid ${importance(a.importance)}`,
        }}>
          <div style={{ display: "flex", gap: 6, marginBottom: 3, fontSize: 11, color: theme.textDim }}>
            {a.tags.map((t) => (
              <span key={t} style={{ color: wingColors[t] ?? theme.textDim }}>{t}</span>
            ))}
            <span style={{ marginLeft: "auto" }}>{a.created_at.slice(0, 19)}</span>
          </div>
          <div style={{ fontSize: 12 }}>{a.summary ?? a.content}</div>
        </div>
      ))}
    </div>
  );
}

function dedupe(arr: AlertResponse[]): AlertResponse[] {
  const seen = new Set<string>();
  return arr.filter((a) => !seen.has(a.id) && (seen.add(a.id), true));
}
