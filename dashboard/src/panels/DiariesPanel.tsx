import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { headerStyle, panelStyle, tabBtnStyle, theme, wingColors } from "../theme";
import type { DiaryResponse } from "../types";

export function DiariesPanel() {
  const [agent, setAgent] = useState("");
  const [entries, setEntries] = useState<DiaryResponse[]>([]);
  const [allEntries, setAllEntries] = useState<DiaryResponse[]>([]);

  useEffect(() => {
    api.diaries({ limit: 200 }).then(setAllEntries).catch(() => setAllEntries([]));
  }, []);

  useEffect(() => {
    api.diaries({ agent_id: agent || undefined, limit: 100 })
      .then(setEntries).catch(() => setEntries([]));
  }, [agent]);

  // Derive the agent filter list from the diary entries themselves — the
  // dashboard has no hardcoded knowledge of which agents exist.
  const AGENTS = useMemo(() => {
    const ids = Array.from(new Set(allEntries.map((e) => e.agent_id))).sort();
    return ["", ...ids];
  }, [allEntries]);

  const moodColor = (mood: string | null) => {
    if (!mood) return theme.textDim;
    const key = mood.toLowerCase();
    if (/curious|hopeful|focus/.test(key)) return theme.accent;
    if (/frustrated|alarm|worried/.test(key)) return theme.red;
    if (/reflect|calm|neutral/.test(key)) return theme.purple;
    return theme.yellow;
  };

  return (
    <div style={{ ...panelStyle, display: "grid", gridTemplateRows: "auto auto 1fr", gap: 8 }}>
      <div style={headerStyle}>Agent Diaries</div>

      <div style={{ display: "flex", gap: 4, fontSize: 11 }}>
        {AGENTS.map((a) => (
          <button
            key={a || "all"}
            onClick={() => setAgent(a)}
            style={tabBtnStyle(agent === a)}
          >{a || "all"}</button>
        ))}
      </div>

      <div style={{ overflow: "auto" }}>
        {entries.map((e) => (
          <div key={e.id} style={{ padding: 10, marginBottom: 8, background: theme.panelBg, borderRadius: 4 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4, fontSize: 11 }}>
              <span style={{ color: wingColors[e.agent_id] ?? theme.text, fontWeight: 600 }}>{e.agent_id}</span>
              {e.mood && <span style={{ color: moodColor(e.mood) }}>• {e.mood}</span>}
              <span style={{ marginLeft: "auto", color: theme.textDim, fontFamily: theme.mono }}>{e.timestamp.slice(0, 19)}</span>
            </div>
            <div style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{e.content}</div>
            {e.tags.length > 0 && (
              <div style={{ marginTop: 4, fontSize: 11, color: theme.textDim }}>tags: {e.tags.join(", ")}</div>
            )}
          </div>
        ))}
        {entries.length === 0 && <div style={{ color: theme.textDim }}>No diary entries.</div>}
      </div>
    </div>
  );
}
