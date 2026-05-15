/**
 * Shared theme tokens + small UI primitives.
 */

export const theme = {
  bg: "#0b0d10",
  panelBg: "#141820",
  panelBorder: "#21262d",
  text: "#e6e8eb",
  textDim: "#8b949e",
  accent: "#58a6ff",
  green: "#22c55e",
  yellow: "#eab308",
  red: "#ef4444",
  purple: "#a78bfa",
  mono: "'SF Mono', Menlo, Monaco, 'Courier New', monospace",
};

export const panelStyle: React.CSSProperties = {
  padding: 12,
  height: "100%",
  // Without border-box, `height: 100%` + `padding: 12` makes each panel 24px
  // taller than its dockview frame, pushing footer rows (e.g. EntryBrowser's
  // prev/next pagination) under the viewport edge.
  boxSizing: "border-box",
  overflow: "auto",
  background: theme.bg,
  color: theme.text,
  fontSize: 13,
};

export const headerStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: 0.5,
  color: theme.textDim,
  marginBottom: 8,
  paddingBottom: 4,
  borderBottom: `1px solid ${theme.panelBorder}`,
};

// Wing palette. `shared` is always amber to stand out as the cross-agent
// namespace; everything else gets a deterministic color from `palette` based
// on a stable hash of the wing name. This lets the dashboard render any
// user-configured wing without a hardcoded mapping.
const palette: readonly string[] = [
  "#58a6ff", // blue
  "#a78bfa", // violet
  "#22c55e", // green
  "#f97316", // orange
  "#ec4899", // pink
  "#14b8a6", // teal
  "#fbbf24", // amber-2
  "#06b6d4", // cyan
];

function hashWing(wing: string): number {
  let h = 0;
  for (let i = 0; i < wing.length; i++) {
    h = ((h << 5) - h + wing.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

// Proxy preserves the legacy `wingColors[wing]` lookup pattern but resolves
// the color dynamically — no need to enumerate wings at build time.
export const wingColors = new Proxy<Record<string, string>>(
  {},
  {
    get(_target, prop: string | symbol): string | undefined {
      if (typeof prop !== "string") return undefined;
      if (prop === "shared") return "#eab308";
      return palette[hashWing(prop) % palette.length];
    },
  },
);

export const inputStyle: React.CSSProperties = {
  flex: 1,
  background: theme.panelBg,
  color: theme.text,
  border: `1px solid ${theme.panelBorder}`,
  padding: "4px 8px",
  borderRadius: 4,
  fontSize: 12,
};

export const btnStyle: React.CSSProperties = {
  background: theme.panelBg,
  color: theme.text,
  border: `1px solid ${theme.panelBorder}`,
  padding: "4px 10px",
  borderRadius: 4,
  fontSize: 12,
  cursor: "pointer",
};

export const tabBtnStyle = (active: boolean): React.CSSProperties => ({
  padding: "3px 8px",
  fontSize: 11,
  borderRadius: 4,
  cursor: "pointer",
  background: active ? theme.accent : theme.panelBg,
  color: active ? theme.bg : theme.text,
  border: `1px solid ${theme.panelBorder}`,
});
