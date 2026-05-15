import { useCallback, useEffect, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { api } from "../api";
import { btnStyle, headerStyle, inputStyle, panelStyle, theme } from "../theme";
import type { GraphStats, GraphSubgraph } from "../types";

interface VizNode { id: string; label: string; type?: string; }
interface VizLink { source: string; target: string; rel?: string; }

export function GraphPanel() {
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [sub, setSub] = useState<GraphSubgraph | null>(null);
  const [centerNode, setCenterNode] = useState("");
  const [depth, setDepth] = useState(2);
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 400, h: 300 });

  const loadSubgraph = useCallback(() => {
    api.graphSubgraph(centerNode || undefined, depth, 300)
      .then(setSub)
      .catch(() => setSub({ nodes: [], edges: [] }));
  }, [centerNode, depth]);

  useEffect(() => {
    api.graphStats().then(setStats).catch(() => {});
    loadSubgraph();
  }, [loadSubgraph]);

  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver(([e]) => {
      setSize({ w: Math.floor(e.contentRect.width), h: Math.floor(e.contentRect.height) });
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  const data = sub ? {
    nodes: sub.nodes.map<VizNode>(n => ({
      id: n.id,
      label: String(n.attributes.name ?? n.id),
      type: String(n.attributes.type ?? "node"),
    })),
    links: sub.edges.map<VizLink>(e => ({
      source: e.source,
      target: e.target,
      rel: String(e.attributes.relation ?? ""),
    })),
  } : { nodes: [], links: [] };

  return (
    <div style={{ ...panelStyle, display: "grid", gridTemplateRows: "auto auto 1fr", gap: 8 }}>
      <div style={headerStyle}>
        Behavioral Graph {stats && `— ${stats.nodes} nodes, ${stats.edges} edges, density ${stats.density.toFixed(4)}`}
      </div>

      <div style={{ display: "flex", gap: 6, fontSize: 12 }}>
        <input
          value={centerNode}
          onChange={(e) => setCenterNode(e.target.value)}
          placeholder="center node id (empty = whole graph)"
          style={inputStyle}
        />
        <input type="number" min={1} max={5} value={depth}
               onChange={(e) => setDepth(Number(e.target.value) || 2)}
               style={{ ...inputStyle, width: 60 }} />
        <button onClick={loadSubgraph} style={btnStyle}>Reload</button>
      </div>

      <div ref={containerRef} style={{ border: `1px solid ${theme.panelBorder}`, borderRadius: 4, minHeight: 0 }}>
        {data.nodes.length === 0 ? (
          <div style={{ padding: 20, color: theme.textDim }}>No graph nodes yet.</div>
        ) : (
          <ForceGraph2D
            graphData={data}
            width={size.w}
            height={size.h}
            backgroundColor={theme.bg}
            nodeColor={() => theme.accent}
            linkColor={() => theme.textDim}
            nodeLabel={(n: VizNode) => n.label}
            nodeRelSize={4}
            cooldownTicks={80}
          />
        )}
      </div>
    </div>
  );
}

