/**
 * Shared theme tokens + small UI primitives.
 */

// Chrome from coolors palette #0c0a3e-7b1e7a-b33f62-f9564f-f3c677.
// Deep navy base, vivid coral as the LIVE / focus accent, gold for warnings,
// coral re-used for errors (close enough to a red without breaking the
// palette's vocabulary). Off-white kept for body text — the gold tone is too
// saturated for paragraphs.
export const theme = {
  bg: "#0c0a3e",
  panelBg: "#15124f",
  panelBorder: "#2a2670",
  text: "#e6e8eb",
  textDim: "#9a93c8",
  accent: "#f9564f",
  green: "#5ad48b",
  yellow: "#f3c677",
  red: "#f9564f",
  purple: "#7b1e7a",
  rose: "#b33f62",
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

// Wing palette from coolors #b7094c-a01a58-892b64-723c70-5c4d7d-455e89-2e6f95-1780a1-0091ad.
// 9-color magenta→teal gradient — coherent spectrum, distinguishable hues for
// up to 9 user-configured wings. `shared` always uses the warm gold from the
// chrome palette so the cross-agent namespace pops against the cool wing tones.
const palette: readonly string[] = [
  "#b7094c", // magenta
  "#a01a58", // wine
  "#892b64", // mulberry
  "#723c70", // plum
  "#5c4d7d", // dusk violet
  "#455e89", // slate blue
  "#2e6f95", // steel blue
  "#1780a1", // teal blue
  "#0091ad", // cyan teal
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
      if (prop === "shared") return "#f3c677";
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
