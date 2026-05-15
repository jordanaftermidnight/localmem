/**
 * LOCALMEM REST API client.
 * All endpoints return typed data matching api/models.py.
 */

import type {
  AlertResponse,
  ArchiveStatsResponse,
  DiaryResponse,
  EntryDetail,
  EntryListResponse,
  GraphStats,
  GraphSubgraph,
  HealthResponse,
  MetricsResponse,
  PinResponse,
  SearchResponse,
  TaxonomyResponse,
  TripleResponse,
  WorkerStatusResponse,
} from "./types";

const BASE = import.meta.env.VITE_API_URL ?? "";

export function getApiKey(): string | undefined {
  const fromEnv = import.meta.env.VITE_LOCALMEM_API_KEY;
  if (fromEnv) return fromEnv;
  if (typeof localStorage !== "undefined") {
    return localStorage.getItem("localmem_api_key") ?? undefined;
  }
  return undefined;
}

function getHeaders(): HeadersInit {
  const headers: HeadersInit = { "Content-Type": "application/json" };
  const apiKey = getApiKey();
  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;
  return headers;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: getHeaders() });
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

async function post<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: getHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

interface EntryListParams {
  wing?: string;
  room?: string;
  tags?: string[];
  pinned?: boolean;
  is_summary?: boolean;
  limit?: number;
  offset?: number;
}

interface SearchParams {
  query: string;
  wing?: string;
  room?: string;
  tags?: string[];
  entry_type?: string;
  limit?: number;
}

interface TripleParams {
  subject?: string;
  predicate?: string;
  object?: string;
  active_only?: boolean;
}

interface DiaryParams {
  agent_id?: string;
  limit?: number;
  after?: string;
  before?: string;
}

function qs(params: Record<string, unknown>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const item of v) p.append(k, String(item));
    } else {
      p.append(k, String(v));
    }
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  health: () => get<HealthResponse>("/api/health"),
  metrics: () => get<MetricsResponse>("/api/metrics"),
  taxonomy: () => get<TaxonomyResponse>("/api/taxonomy"),

  entries: (params: EntryListParams = {}) =>
    get<EntryListResponse>(`/api/entries${qs(params as Record<string, unknown>)}`),

  entry: (id: string) => get<EntryDetail>(`/api/entries/${id}`),

  search: (params: SearchParams) =>
    post<SearchResponse>("/api/search", params as unknown as Record<string, unknown>),

  graphStats: () => get<GraphStats>("/api/graph/stats"),

  graphSubgraph: (node?: string, depth = 2, limit = 200) =>
    get<GraphSubgraph>(
      `/api/graph/subgraph${qs({ node, depth, limit })}`
    ),

  triples: (params: TripleParams = {}) =>
    get<TripleResponse[]>(`/api/triples${qs(params as Record<string, unknown>)}`),

  tripleTimeline: (subject: string, predicate: string) =>
    get<TripleResponse[]>(
      `/api/triples/timeline/${encodeURIComponent(subject)}/${encodeURIComponent(predicate)}`
    ),

  diaries: (params: DiaryParams = {}) =>
    get<DiaryResponse[]>(`/api/diaries${qs(params as Record<string, unknown>)}`),

  alerts: (wing?: string, limit = 20) =>
    get<AlertResponse[]>(`/api/alerts${qs({ wing, limit })}`),

  pin: (id: string, pinned: boolean) =>
    post<PinResponse>(`/api/entries/${id}/pin`, { pinned }),

  workerStatus: () => get<WorkerStatusResponse>("/api/worker/status"),

  archiveStats: () => get<ArchiveStatsResponse>("/api/archive/stats"),

  pruneRun: () => post<WorkerStatusResponse>("/api/prune/run"),

  archiveRun: () => post<WorkerStatusResponse>("/api/archive/run"),
};
