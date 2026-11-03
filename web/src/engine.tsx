// Live engine state for the whole page, fed by the daemon's event stream (/api/events).
import { createContext, useContext, useEffect, useReducer, type ReactNode } from "react";
import { api } from "./api";
import type { EngineMode, InstallProgress, ModelDownload, ModelsProgress, PracticeAttempt, RemoteInfo, Status, SyncStatus, Triggers } from "./types";

export const STAGE_LABELS: Record<string, string> = {
  upload: "Sending to the server",
  samples: "Loading audio",
  transcript: "Transcribing",
  expected_phonemes: "Looking up correct sounds",
  heard_phonemes: "Listening to your sounds",
  "analyzer:pronunciation": "Checking pronunciation",
  "analyzer:grammar": "Checking grammar",
  "analyzer:practice": "Scoring",
};

export interface Stage {
  key: string;
  state: "running" | "done";
}

export interface Notice {
  id: number;
  kind: "error" | "warning" | "info";
  text: string;
}

export interface EngineState {
  connected: boolean;
  recording: { mode: "analysis" | "practice"; level: number; elapsed: number } | null;
  levels: number[];
  job: { name: string; stages: Stage[] } | null; // the one running now
  pending: number; // jobs not finished yet: the running one plus those waiting behind it
  lastAnalysis: { sessionId: string; seq: number } | null;
  analysisError: string | null; // the last analysis failed; cleared when the next one starts
  lastAttempt: PracticeAttempt | null;
  practiceTarget: { text: string; session_id: string | null } | null;
  triggers: Triggers | null;
  remote: RemoteInfo | null;
  engineMode: EngineMode | null; // where analysis runs (from status, then remote_changed)
  install: InstallProgress | null;
  sync: SyncStatus | null;
  sessionsVersion: number; // bumps when sessions change outside an analysis (sync, offline queue)
  configVersion: number; // bumps when options change (here, in another tab, or a profile switch)
  sharingVersion: number; // bumps when sharing or its accounts change
  downloads: Record<string, ModelDownload>; // grammar models being downloaded
  models: ModelsProgress | null; // speech models being fetched ahead of use
  notices: Notice[];
}

export const initialState: EngineState = {
  connected: false,
  recording: null,
  levels: [],
  job: null,
  pending: 0,
  lastAnalysis: null,
  analysisError: null,
  lastAttempt: null,
  practiceTarget: null,
  triggers: null,
  remote: null,
  engineMode: null,
  install: null,
  sync: null,
  sessionsVersion: 0,
  configVersion: 0,
  sharingVersion: 0,
  downloads: {},
  models: null,
  notices: [],
};

export type Action =
  | { type: "connected"; value: boolean }
  | { type: "status"; status: Status }
  | { type: "models"; models: ModelsProgress }
  | { type: "dismiss_notice"; id: number }
  | { type: "notice"; kind: Notice["kind"]; text: string }
  | { type: "server"; event: string; data: Record<string, any> };

const LEVEL_HISTORY = 40;
let noticeId = 0;

function addNotice(state: EngineState, kind: Notice["kind"], text: string): EngineState {
  const notices = [...state.notices.filter((n) => n.text !== text), { id: ++noticeId, kind, text }].slice(-4);
  return { ...state, notices };
}

export function reduce(state: EngineState, action: Action): EngineState {
  switch (action.type) {
    case "connected":
      return { ...state, connected: action.value };
    case "status": {
      const r = action.status.recording;
      return {
        ...state,
        recording: r ? { mode: r.mode, level: r.level ?? 0, elapsed: r.elapsed ?? 0 } : null,
        practiceTarget: action.status.practice_target,
        triggers: action.status.triggers,
        engineMode: action.status.engine ?? state.engineMode,
      };
    }
    case "dismiss_notice":
      return { ...state, notices: state.notices.filter((n) => n.id !== action.id) };
    case "models":
      return { ...state, models: action.models };
    case "notice":
      return addNotice(state, action.kind, action.text);
    case "server":
      return reduceServer(state, action.event, action.data);
  }
}

function reduceServer(state: EngineState, event: string, d: Record<string, any>): EngineState {
  switch (event) {
    case "recording_started":
      return {
        ...state,
        recording: { mode: d.mode, level: 0, elapsed: 0 },
        levels: [],
        analysisError: d.mode === "analysis" ? null : state.analysisError,
      };
    case "level":
      if (!state.recording) return state;
      return {
        ...state,
        recording: { ...state.recording, level: d.level ?? 0, elapsed: d.elapsed ?? 0 },
        levels: [...state.levels, d.level ?? 0].slice(-LEVEL_HISTORY),
      };
    case "recording_stopped":
    case "recording_cancelled":
      return { ...state, recording: null };
    case "job_queued":
      return { ...state, pending: d.pending ?? state.pending + 1 };
    case "job_started":
      return { ...state, job: { name: d.job, stages: [] }, analysisError: d.job === "analysis" ? null : state.analysisError };
    case "stage_started": {
      if (!state.job || d.stage === "samples") return state;
      const stages = state.job.stages.filter((s) => s.key !== d.stage);
      return { ...state, job: { ...state.job, stages: [...stages, { key: d.stage, state: "running" }] } };
    }
    case "stage_finished": {
      if (!state.job) return state;
      const stages = state.job.stages.map((s) => (s.key === d.stage ? { ...s, state: "done" as const } : s));
      return { ...state, job: { ...state.job, stages } };
    }
    case "job_finished":
      return { ...state, job: null, pending: d.pending ?? Math.max(0, state.pending - 1) };
    case "analysis_done":
      return { ...state, lastAnalysis: { sessionId: d.session_id, seq: (state.lastAnalysis?.seq ?? 0) + 1 } };
    case "practice_result":
      return { ...state, lastAttempt: d.attempt };
    case "practice_target":
      return { ...state, practiceTarget: d.target };
    case "triggers":
      return { ...state, triggers: d as Triggers };
    case "remote_changed":
      return { ...state, remote: d.remote as RemoteInfo, engineMode: { mode: d.remote.mode, url: d.remote.url } };
    case "install":
      return { ...state, install: d as InstallProgress };
    case "sync":
      return { ...state, sync: d as SyncStatus };
    case "sessions_changed":
      return { ...state, sessionsVersion: state.sessionsVersion + 1 };
    case "config_changed":
      return { ...state, configVersion: state.configVersion + 1 };
    case "sharing_changed":
      return { ...state, sharingVersion: state.sharingVersion + 1 };
    case "models_progress":
      return { ...state, models: d as ModelsProgress };
    case "model_download":
      return { ...state, downloads: { ...state.downloads, [d.name]: d as ModelDownload } };
    case "install_output": {
      const log = [...(state.install?.log ?? []), d.line as string].slice(-200);
      return { ...state, install: { running: true, ok: null, ...state.install, log } };
    }
    case "job_error":
      // A failed analysis gets its own card on Home and in the sky; other jobs get a notice.
      return d.job === "analysis" ? { ...state, analysisError: d.message } : addNotice(state, "error", d.message);
    case "action_error":
      return addNotice(state, "error", d.message);
    case "warning":
      return addNotice(state, "warning", d.message);
    default:
      return state;
  }
}

type Ctx = { state: EngineState; dispatch: (a: Action) => void };
const EngineContext = createContext<Ctx | null>(null);

export function EngineProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reduce, initialState);

  useEffect(() => {
    const source = new EventSource("/api/events");
    const refresh = () => {
      api
        .status()
        .then((status) => dispatch({ type: "status", status }))
        .catch(() => undefined);
      api
        .modelDownloads()
        .then((models) => dispatch({ type: "models", models }))
        .catch(() => undefined);
    };
    source.onopen = () => {
      dispatch({ type: "connected", value: true });
      refresh(); // catch up on anything that happened while disconnected
    };
    source.onerror = () => dispatch({ type: "connected", value: false });
    source.onmessage = (msg) => {
      const { type, data } = JSON.parse(msg.data);
      dispatch({ type: "server", event: type, data });
    };
    return () => source.close();
  }, []);

  return <EngineContext.Provider value={{ state, dispatch }}>{children}</EngineContext.Provider>;
}

export function useEngine(): Ctx {
  const ctx = useContext(EngineContext);
  if (!ctx) throw new Error("useEngine must be used inside <EngineProvider>");
  return ctx;
}

/** Run an API call; failures become a notice instead of an unhandled rejection. */
export function useAction() {
  const { dispatch } = useEngine();
  return async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    try {
      return await fn();
    } catch (e) {
      dispatch({ type: "notice", kind: "error", text: e instanceof Error ? e.message : String(e) });
      return undefined;
    }
  };
}
