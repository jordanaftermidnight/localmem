/**
 * Zustand store for dashboard state.
 * Topic-routed WS messages update the relevant slices.
 */

import { create } from "zustand";
import type {
  AlertResponse,
  HealthResponse,
  MetricsResponse,
  RoomInfo,
} from "./types";

interface LogLine {
  level: string;
  message: string;
  timestamp: string;
}

interface DashboardState {
  connected: boolean;
  health: HealthResponse | null;
  metrics: MetricsResponse | null;
  rooms: RoomInfo[];
  alerts: AlertResponse[];
  logs: LogLine[];

  setConnected: (c: boolean) => void;
  setHealth: (h: HealthResponse) => void;
  setMetrics: (m: MetricsResponse) => void;
  setRooms: (r: RoomInfo[]) => void;
  pushAlerts: (a: AlertResponse[]) => void;
  pushLogs: (lines: LogLine[]) => void;

  selectedEntryId: string | null;
  setSelectedEntryId: (id: string | null) => void;

  entryFilter: { wing?: string; room?: string };
  setEntryFilter: (f: { wing?: string; room?: string }) => void;
}

export const useStore = create<DashboardState>((set) => ({
  connected: false,
  health: null,
  metrics: null,
  rooms: [],
  alerts: [],
  logs: [],

  setConnected: (c) => set({ connected: c }),
  setHealth: (h) => set({ health: h }),
  setMetrics: (m) => set({ metrics: m }),
  setRooms: (r) => set({ rooms: r }),
  pushAlerts: (a) =>
    set((s) => ({ alerts: [...a, ...s.alerts].slice(0, 200) })),
  pushLogs: (lines) =>
    set((s) => ({ logs: [...lines, ...s.logs].slice(0, 500) })),

  selectedEntryId: null,
  setSelectedEntryId: (id) => set({ selectedEntryId: id }),

  entryFilter: {},
  setEntryFilter: (f) => set({ entryFilter: f }),
}));
