import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { btnStyle, headerStyle, inputStyle, panelStyle, tabBtnStyle, theme, wingColors } from "../theme";
import type { EntryDetail, EntrySummary, SearchHit } from "../types";

export function EntryBrowser() {
  const filter = useStore((s) => s.entryFilter);
  const setSelected = useStore((s) => s.setSelectedEntryId);
  const selectedId = useStore((s) => s.selectedEntryId);

  const [entries, setEntries] = useState<EntrySummary[]>([]);
  const [total, setTotal] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [detail, setDetail] = useState<EntryDetail | null>(null);
  const [offset, setOffset] = useState(0);
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [hideSummaries, setHideSummaries] = useState(false);
  const limit = 25;

  const reload = useCallback(() => {
    api.entries({
      ...filter,
      pinned: pinnedOnly ? true : undefined,
      is_summary: hideSummaries ? false : undefined,
      limit,
      offset,
    }).then((r) => {
      setEntries(r.entries);
      setTotal(r.total);
    });
  }, [filter, offset, pinnedOnly, hideSummaries]);

  useEffect(() => { reload(); }, [reload]);

  useEffect(() => {
    setSelected(null);
  }, [filter, pinnedOnly, hideSummaries, setSelected]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    api.entry(selectedId).then(setDetail).catch(() => setDetail(null));
  }, [selectedId]);

  const doSearch = async () => {
    if (!searchQuery.trim()) { setHits(null); return; }
    const r = await api.search({ query: searchQuery, wing: filter.wing, room: filter.room, limit: 30 });
    setHits(r.hits);
  };

  const togglePin = async () => {
    if (!detail) return;
    const next = !detail.pinned;
    await api.pin(detail.id, next);
    setDetail({ ...detail, pinned: next });
    reload();
  };

  const items = hits ? hits.map(h => ({ ...h.entry, score: h.score })) : entries;

  return (
    <div style={{ ...panelStyle, display: "grid", gridTemplateRows: "auto auto 1fr", gap: 8 }}>
      <div style={headerStyle}>Entry Browser {filter.wing && `— ${filter.wing}${filter.room ? `/${filter.room}` : ""}`}</div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && doSearch()}
          placeholder="Search (Enter)…"
          style={inputStyle}
        />
        <button onClick={doSearch} style={btnStyle}>Search</button>
        <button onClick={() => { setHits(null); setSearchQuery(""); }} style={btnStyle}>Clear</button>
        <button
          onClick={() => setPinnedOnly(!pinnedOnly)}
          style={tabBtnStyle(pinnedOnly)}
          title="Show only pinned entries"
        >Pinned only</button>
        <button
          onClick={() => setHideSummaries(!hideSummaries)}
          style={tabBtnStyle(hideSummaries)}
          title="Hide consolidated summary entries"
        >Hide summaries</button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: detail ? "1fr 1fr" : "1fr", gap: 8, minHeight: 0 }}>
        <div style={{ overflow: "auto", border: `1px solid ${theme.panelBorder}`, borderRadius: 4 }}>
          {items.map((it) => (
            <div
              key={it.id}
              onClick={() => setSelected(it.id)}
              style={{
                padding: "8px 10px",
                borderLeft: it.pinned
                  ? `3px solid ${theme.accent}`
                  : "3px solid transparent",
                borderBottom: `1px solid ${theme.panelBorder}`,
                cursor: "pointer",
                background: it.id === selectedId ? theme.panelBg : "transparent",
              }}
            >
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 2 }}>
                <span style={{ color: wingColors[it.wing], fontSize: 11 }}>{it.wing}:{it.room}</span>
                <span style={{ color: theme.textDim, fontSize: 11 }}>{it.agent_id}</span>
                {it.is_summary && (
                  <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: theme.panelBg, color: theme.textDim }}>
                    summary
                  </span>
                )}
                <span style={{ marginLeft: "auto", fontSize: 11, color: theme.textDim }}>
                  imp {it.importance.toFixed(2)}
                </span>
              </div>
              <div style={{ fontSize: 12, color: theme.text }}>{it.summary ?? it.preview}</div>
            </div>
          ))}
          {items.length === 0 && <div style={{ padding: 12, color: theme.textDim }}>No entries.</div>}
        </div>

        {detail && (
          <div style={{ overflow: "auto", padding: 10, border: `1px solid ${theme.panelBorder}`, borderRadius: 4 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ color: wingColors[detail.wing], fontSize: 11 }}>
                {detail.wing}:{detail.room} · {detail.entry_type}
              </span>
              <button
                onClick={togglePin}
                style={tabBtnStyle(detail.pinned)}
                title={detail.pinned ? "Unpin (re-enter retention)" : "Pin (exempt from retention)"}
              >{detail.pinned ? "Unpin" : "Pin"}</button>
              {detail.is_summary && (
                <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3, background: theme.panelBg, color: theme.textDim }}>
                  consolidated summary
                </span>
              )}
            </div>
            <div style={{ fontSize: 13, marginBottom: 10, whiteSpace: "pre-wrap" }}>{detail.content}</div>
            <div style={{ fontSize: 11, color: theme.textDim }}>
              agent: {detail.agent_id}<br />
              importance: {detail.importance}<br />
              pinned: {detail.pinned ? "yes" : "no"}<br />
              tags: {detail.tags.join(", ") || "—"}<br />
              created: {detail.created_at}<br />
              id: {detail.id}
            </div>
          </div>
        )}
      </div>

      {!hits && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", color: theme.textDim, fontSize: 11 }}>
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))} style={btnStyle}>‹ prev</button>
          <span>{offset + 1}–{Math.min(offset + limit, total)} of {total}</span>
          <button disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)} style={btnStyle}>next ›</button>
        </div>
      )}
    </div>
  );
}

