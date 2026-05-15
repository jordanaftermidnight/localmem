/**
 * LOCALMEM Dashboard TypeScript types — mirrors api/models.py Pydantic models.
 */

export interface EmbeddingInfo {
  model: string;
  device: string;
  sparse: boolean;
}

export interface HealthResponse {
  status: "healthy" | "degraded";
  uptime_seconds: number;
  vector_store: {
    status: string;
    entries: Record<string, number>;
  };
  metadata_store: {
    status: string;
    wings: string[];
  };
  graph_store: {
    status: string;
    nodes?: number;
    edges?: number;
    density?: number;
    weakly_connected_components?: number;
  };
  embedding: EmbeddingInfo;
}

export interface LatencyStats {
  avg: number;
  p50: number;
  p95: number;
  p99: number;
}

export interface ToolMetric {
  calls: number;
  errors: number;
  latency_ms: LatencyStats;
}

export interface MetricsResponse {
  uptime_seconds: number;
  total_calls: number;
  total_errors: number;
  tools: Record<string, ToolMetric>;
}

export interface RoomInfo {
  wing: string;
  room: string;
  entry_count: number;
  last_written: string | null;
}

export interface TaxonomyResponse {
  wings: string[];
  rooms: RoomInfo[];
}

export interface EntrySummary {
  id: string;
  wing: string;
  room: string;
  agent_id: string;
  entry_type: string;
  summary: string | null;
  preview: string;
  importance: number;
  tags: string[];
  created_at: string;
  pinned: boolean;
  is_summary: boolean;
}

export interface EntryDetail extends EntrySummary {
  content: string;
  refs: string[];
  metadata: Record<string, unknown>;
  updated_at: string;
}

export interface PinResponse {
  entry_id: string;
  pinned: boolean;
}

export interface WorkerStatusResponse {
  running: boolean;
  in_flight: boolean;
  queue_size: number;
  dirty_wings: string[];
  last_consolidation_at: string | null;
  last_archive_at: string | null;
}

export interface ArchiveStatsResponse {
  path: string;
  exists: boolean;
  total_files: number;
  total_bytes: number;
  wings: Record<string, { files: number; bytes: number }>;
}

export interface EntryListResponse {
  entries: EntrySummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface SearchHit {
  entry: EntrySummary;
  score: number;
  source: "dense" | "sparse" | "hybrid";
}

export interface SearchResponse {
  hits: SearchHit[];
  query: string;
}

export interface GraphStats {
  nodes: number;
  edges: number;
  density: number;
  weakly_connected_components: number;
}

export interface GraphNode {
  id: string;
  attributes: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  attributes: Record<string, unknown>;
}

export interface GraphSubgraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface TripleResponse {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  confidence: number;
  source_agent: string;
  source_entry_id: string | null;
  valid_from: string;
  valid_to: string | null;
  created_at: string;
}

export interface DiaryResponse {
  id: string;
  agent_id: string;
  timestamp: string;
  content: string;
  mood: string | null;
  tags: string[];
  references: string[];
}

export interface AlertResponse {
  id: string;
  content: string;
  summary: string | null;
  importance: number;
  tags: string[];
  metadata: Record<string, unknown>;
  created_at: string;
}

export type WSTopic =
  | "health"
  | "metrics"
  | "alerts"
  | "entries"
  | "logs"
  | "system";

export interface WSMessage {
  topic: WSTopic;
  data: Record<string, unknown>;
  timestamp?: string;
}
