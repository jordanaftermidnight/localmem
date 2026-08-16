import { useEffect, useState } from "react";
import { api } from "../api";
import { headerStyle, inputStyle, panelStyle, theme } from "../theme";
import type { TripleResponse } from "../types";

export function TriplesPanel() {
  const [subject, setSubject] = useState("");
  const [predicate, setPredicate] = useState("");
  const [activeOnly, setActiveOnly] = useState(true);
  const [triples, setTriples] = useState<TripleResponse[]>([]);
  const [timelineFor, setTimelineFor] = useState<{ subject: string; predicate: string } | null>(null);
  const [timeline, setTimeline] = useState<TripleResponse[]>([]);

  useEffect(() => {
    api.triples({
      subject: subject || undefined,
      predicate: predicate || undefined,
      active_only: activeOnly,
    }).then(setTriples).catch(() => setTriples([]));
  }, [subject, predicate, activeOnly]);

  useEffect(() => {
    if (!timelineFor) return;
    api.tripleTimeline(timelineFor.subject, timelineFor.predicate)
      .then(setTimeline).catch(() => setTimeline([]));
  }, [timelineFor]);

  return (
    <div style={{ ...panelStyle, display: "grid", gridTemplateRows: "auto auto 1fr", gap: 8 }}>
      <div style={headerStyle}>Knowledge Triples</div>

      <div style={{ display: "flex", gap: 6, fontSize: 12 }}>
        <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="subject" style={inputStyle} />
        <input value={predicate} onChange={(e) => setPredicate(e.target.value)} placeholder="predicate" style={inputStyle} />
        <label style={{ display: "flex", gap: 4, alignItems: "center", color: theme.textDim, fontSize: 11 }}>
          <input type="checkbox" checked={activeOnly} onChange={(e) => setActiveOnly(e.target.checked)} />
          active only
        </label>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: timelineFor ? "1fr 1fr" : "1fr", gap: 8, minHeight: 0 }}>
        <div style={{ overflow: "auto", border: `1px solid ${theme.panelBorder}`, borderRadius: 4 }}>
          {triples.map((t) => (
            <div
              key={t.id}
              onClick={() => setTimelineFor({ subject: t.subject, predicate: t.predicate })}
              style={{ padding: "6px 8px", borderBottom: `1px solid ${theme.panelBorder}`, cursor: "pointer", fontSize: 12, fontFamily: theme.mono }}
            >
              <span style={{ color: theme.accent }}>{t.subject}</span>{" "}
              <span style={{ color: theme.textDim }}>{t.predicate}</span>{" "}
              <span style={{ color: theme.text }}>{t.object}</span>
              <div style={{ fontSize: 10, color: theme.textDim, marginTop: 2 }}>
                conf {t.confidence.toFixed(2)} · by {t.source_agent}{t.valid_to && ` · ended ${t.valid_to.slice(0,10)}`}
              </div>
            </div>
          ))}
          {triples.length === 0 && <div style={{ padding: 10, color: theme.textDim }}>No triples.</div>}
        </div>

        {timelineFor && (
          <div style={{ overflow: "auto", border: `1px solid ${theme.panelBorder}`, borderRadius: 4, padding: 8 }}>
            <div style={{ fontSize: 11, color: theme.textDim, marginBottom: 6 }}>
              Timeline: {timelineFor.subject} {timelineFor.predicate}
              <button onClick={() => setTimelineFor(null)} style={{ ...smallBtnStyle, marginLeft: 8, fontSize: 10 }}>close</button>
            </div>
            {timeline.map((t) => (
              <div key={t.id} style={{ padding: 4, fontSize: 11, fontFamily: theme.mono, borderLeft: `2px solid ${t.valid_to ? theme.textDim : theme.green}`, marginBottom: 4, paddingLeft: 8 }}>
                {t.valid_from.slice(0,19)} → {t.valid_to?.slice(0,19) ?? "current"}<br />
                <span style={{ color: theme.text }}>{t.object}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

const smallBtnStyle: React.CSSProperties = {
  background: theme.panelBg, color: theme.text, border: `1px solid ${theme.panelBorder}`,
  padding: "2px 6px", borderRadius: 3, cursor: "pointer",
};
