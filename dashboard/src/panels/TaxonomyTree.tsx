import { useEffect } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { panelStyle, headerStyle, theme, wingColors } from "../theme";

export function TaxonomyTree() {
  const rooms = useStore((s) => s.rooms);
  const setRooms = useStore((s) => s.setRooms);
  const filter = useStore((s) => s.entryFilter);
  const setFilter = useStore((s) => s.setEntryFilter);

  useEffect(() => {
    const refresh = () => api.taxonomy().then((r) => setRooms(r.rooms)).catch(() => {});
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [setRooms]);

  const grouped = rooms.reduce<Record<string, typeof rooms>>((acc, r) => {
    (acc[r.wing] ??= []).push(r);
    return acc;
  }, {});

  const wings = Object.keys(grouped).sort();

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>Wings & Rooms</div>
      <div
        onClick={() => setFilter({})}
        style={{ padding: 4, cursor: "pointer", color: !filter.wing ? theme.accent : theme.textDim, marginBottom: 6 }}
      >
        All wings
      </div>
      {wings.map((wing) => (
        <div key={wing} style={{ marginBottom: 10 }}>
          <div
            onClick={() => setFilter({ wing })}
            style={{
              display: "flex", justifyContent: "space-between", padding: "4px 6px",
              background: filter.wing === wing && !filter.room ? theme.panelBg : "transparent",
              borderRadius: 3, cursor: "pointer",
              color: wingColors[wing] ?? theme.text, fontWeight: 600,
            }}
          >
            <span>{wing}</span>
            <span style={{ color: theme.textDim, fontSize: 11 }}>{grouped[wing].length}</span>
          </div>
          {grouped[wing].map((r) => (
            <div
              key={`${r.wing}:${r.room}`}
              onClick={() => setFilter({ wing: r.wing, room: r.room })}
              style={{
                padding: "3px 6px 3px 18px", fontSize: 12, cursor: "pointer",
                background: filter.wing === r.wing && filter.room === r.room ? theme.panelBg : "transparent",
                borderRadius: 3,
                display: "flex", justifyContent: "space-between",
              }}
            >
              <span>{r.room}</span>
              <span style={{ color: theme.textDim, fontSize: 11, fontFamily: theme.mono }}>{r.entry_count}</span>
            </div>
          ))}
        </div>
      ))}
      {wings.length === 0 && <div style={{ color: theme.textDim }}>No rooms registered yet.</div>}
    </div>
  );
}
