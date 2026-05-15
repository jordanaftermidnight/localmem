import { useEffect, useState } from "react";
import { api } from "../api";
import { btnStyle, headerStyle, panelStyle, theme, wingColors } from "../theme";
import type { ArchiveStatsResponse, WorkerStatusResponse } from "../types";

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function AdminPanel() {
  const [worker, setWorker] = useState<WorkerStatusResponse | null>(null);
  const [archive, setArchive] = useState<ArchiveStatsResponse | null>(null);
  const [busy, setBusy] = useState<"prune" | "archive" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    api.workerStatus().then(setWorker).catch(() => setWorker(null));
    api.archiveStats().then(setArchive).catch(() => setArchive(null));
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const triggerPrune = async () => {
    setBusy("prune");
    setError(null);
    try {
      const r = await api.pruneRun();
      setWorker(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      refresh();
    }
  };

  const triggerArchive = async () => {
    setBusy("archive");
    setError(null);
    try {
      const r = await api.archiveRun();
      setWorker(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      refresh();
    }
  };

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>Retention Admin</div>

      {error && (
        <div style={{
          background: "#3a1f1f", color: theme.red, padding: "6px 10px",
          borderRadius: 4, fontSize: 11, marginBottom: 10,
        }}>{error}</div>
      )}

      <div style={{ marginBottom: 16 }}>
        <div style={{ ...headerStyle, fontSize: 10, marginBottom: 6 }}>Worker</div>
        {worker ? (
          <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
            <tbody>
              <tr>
                <td style={{ color: theme.textDim, padding: "2px 0" }}>status</td>
                <td>
                  <span style={{
                    color: worker.running ? theme.green : theme.red,
                    fontWeight: 600,
                  }}>
                    {worker.running ? "RUNNING" : "STOPPED"}
                  </span>
                  {worker.in_flight && (
                    <span style={{ marginLeft: 8, color: theme.yellow }}>(in flight)</span>
                  )}
                </td>
              </tr>
              <tr>
                <td style={{ color: theme.textDim }}>queue size</td>
                <td>{worker.queue_size}</td>
              </tr>
              <tr>
                <td style={{ color: theme.textDim }}>dirty wings</td>
                <td>{worker.dirty_wings.length === 0 ? "—" : worker.dirty_wings.join(", ")}</td>
              </tr>
              <tr>
                <td style={{ color: theme.textDim }}>last consolidation</td>
                <td style={{ fontSize: 11 }}>{worker.last_consolidation_at ?? "never"}</td>
              </tr>
              <tr>
                <td style={{ color: theme.textDim }}>last archive</td>
                <td style={{ fontSize: 11 }}>{worker.last_archive_at ?? "never"}</td>
              </tr>
            </tbody>
          </table>
        ) : (
          <div style={{ color: theme.textDim, fontSize: 11 }}>
            Worker not available (retention disabled or server starting).
          </div>
        )}

        <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
          <button
            onClick={triggerPrune}
            disabled={busy !== null || !worker?.running}
            style={btnStyle}
          >
            {busy === "prune" ? "Running..." : "Run prune"}
          </button>
          <button
            onClick={triggerArchive}
            disabled={busy !== null || !worker?.running}
            style={btnStyle}
          >
            {busy === "archive" ? "Running..." : "Run archive"}
          </button>
          <button onClick={refresh} style={btnStyle}>Refresh</button>
        </div>
      </div>

      <div>
        <div style={{ ...headerStyle, fontSize: 10, marginBottom: 6 }}>Archive (cold tier)</div>
        {archive && archive.exists ? (
          <>
            <div style={{ fontSize: 11, color: theme.textDim, marginBottom: 6 }}>
              {archive.path}
            </div>
            <div style={{ fontSize: 12, marginBottom: 8 }}>
              {archive.total_files} files · {bytes(archive.total_bytes)}
            </div>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${theme.panelBorder}`, color: theme.textDim }}>
                  <th style={{ textAlign: "left", padding: "4px 0" }}>wing</th>
                  <th style={{ textAlign: "right" }}>files</th>
                  <th style={{ textAlign: "right" }}>size</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(archive.wings).map(([wing, stats]) => (
                  <tr key={wing}>
                    <td style={{ color: wingColors[wing] ?? theme.text, padding: "3px 0" }}>{wing}</td>
                    <td style={{ textAlign: "right" }}>{stats.files}</td>
                    <td style={{ textAlign: "right" }}>{bytes(stats.bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <div style={{ color: theme.textDim, fontSize: 11 }}>
            No archive on disk yet.
          </div>
        )}
      </div>
    </div>
  );
}
