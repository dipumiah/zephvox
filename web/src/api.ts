import type {
  ModelsProgress,
  AnalysisResult,
  ConfigChange,
  ConfigInfo,
  ModelDownload,
  SharingInfo,
  Device,
  Feedback,
  History,
  PracticeStats,
  ProfilePreview,
  Profiles,
  Recording,
  RemoteInfo,
  SessionSummary,
  Settings,
  Status,
  WeakWord,
} from "./types";

export class ApiError extends Error {}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  status: () => request<Status>("GET", "/api/status"),
  settings: () => request<Settings>("GET", "/api/settings"),
  saveSettings: (changes: Partial<Pick<Settings, "capture" | "tts" | "analyzers">>) =>
    request<Settings>("PUT", "/api/settings", changes),
  profiles: () => request<Profiles>("GET", "/api/profiles"),
  setProfile: (name: string) => request<Profiles>("PUT", "/api/profile", { name }),
  previewProfile: (name: string) => request<ProfilePreview>("GET", `/api/profile/preview?name=${encodeURIComponent(name)}`),
  devices: () => request<{ provider: string; selected: string | null; devices: Device[] }>("GET", "/api/devices"),
  voices: () =>
    request<{ provider: string; selected: string | null; voices: { id: string; label: string }[] }>("GET", "/api/voices"),

  start: (mode: "analysis" | "practice", analyzers?: string[]) =>
    request<Recording>("POST", "/api/record/start", { mode, analyzers }),
  stop: () => request<Recording>("POST", "/api/record/stop"),
  cancel: () => request<Recording>("POST", "/api/record/cancel"),
  toggle: (mode: "analysis" | "practice", analyzers?: string[]) =>
    request<Recording>("POST", "/api/record/toggle", { mode, analyzers }),
  upload: async (file: File, analyzers: string[]) => {
    const form = new FormData();
    form.append("file", file);
    form.append("analyzers", analyzers.join(","));
    const resp = await fetch("/api/analyze", { method: "POST", body: form });
    if (!resp.ok) throw new ApiError((await resp.json()).detail ?? resp.statusText);
  },

  sessions: () => request<SessionSummary[]>("GET", "/api/sessions"),
  session: (id: string) => request<{ result: AnalysisResult; feedback: Feedback }>("GET", `/api/sessions/${id}`),
  deleteSession: (id: string) => request<unknown>("DELETE", `/api/sessions/${id}`),
  reanalyze: (id: string, analyzers?: string[]) => request<unknown>("POST", `/api/sessions/${id}/analyze`, { analyzers }),
  feedback: (id: string, index: number, verdict: string | null) =>
    request<Feedback>("POST", `/api/sessions/${id}/findings/${index}/feedback`, { verdict }),

  setPracticeTarget: (text: string | null, sessionId?: string | null) =>
    request<unknown>("PUT", "/api/practice/target", { text, session_id: sessionId ?? null }),
  weakWords: () => request<WeakWord[]>("GET", "/api/practice/weak"),
  practiceStats: (target: string) =>
    request<PracticeStats>("GET", `/api/practice/stats?target=${encodeURIComponent(target)}`),
  history: () => request<History>("GET", "/api/history"),

  remote: (check = true) => request<RemoteInfo>("GET", `/api/remote?check=${check}`),
  setRemote: (changes: Partial<{ mode: "local" | "remote"; url: string; headers: Record<string, string>; sync: boolean }>) =>
    request<RemoteInfo>("PUT", "/api/remote", changes),
  connect: (url: string, invite: string, deviceName: string, headers?: Record<string, string>) =>
    request<RemoteInfo>("POST", "/api/remote/connect", { url, invite, device_name: deviceName, headers }),
  rotateKey: () => request<RemoteInfo>("POST", "/api/remote/rotate"),
  forgetKey: () => request<RemoteInfo>("DELETE", "/api/remote/key"),
  installEngine: () => request<unknown>("POST", "/api/install"),

  config: () => request<ConfigInfo>("GET", "/api/config"),
  saveConfig: (changes: ConfigChange[]) => request<ConfigInfo>("PUT", "/api/config", { changes }),
  modelDownloads: () => request<ModelsProgress>("GET", "/api/models/downloads"),
  startModelDownloads: () => request<ModelsProgress>("POST", "/api/models/downloads"),
  cancelModelDownloads: () => request<ModelsProgress>("DELETE", "/api/models/downloads"),
  pullModel: (name: string) => request<ModelDownload>("POST", "/api/models/pull", { name }),

  sharing: () => request<SharingInfo>("GET", "/api/sharing"),
  setSharing: (on: boolean) => request<SharingInfo>("PUT", "/api/sharing", { on }),
  sharingAction: (action: string, body: Record<string, unknown>) => request<SharingInfo>("POST", `/api/sharing/${action}`, body),
  sharingService: (op: "install" | "remove") => request<{ service: string }>("POST", `/api/sharing/service/${op}`),
};

export const urls = {
  clip: (sessionId: string, start?: number, end?: number) =>
    `/api/sessions/${sessionId}/audio` + (start !== undefined && end !== undefined ? `?start=${start}&end=${end}` : ""),
  tts: (text: string) => `/api/tts?text=${encodeURIComponent(text)}`,
  latestAttempt: (target: string) =>
    `/api/practice/attempts/latest/audio?target=${encodeURIComponent(target)}&t=${Date.now()}`,
};

// One shared player so starting a sound stops the previous one.
let player: HTMLAudioElement | null = null;

export async function play(url: string): Promise<void> {
  player?.pause();
  player = new Audio(url);
  await player.play();
}

/** Reference pronunciation: engine TTS, falling back to the browser's own voice. */
export async function speak(text: string): Promise<void> {
  try {
    const resp = await fetch(urls.tts(text));
    if (!resp.ok) throw new ApiError("no tts");
    const blob = await resp.blob();
    await play(URL.createObjectURL(blob));
  } catch {
    if ("speechSynthesis" in window) {
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-US";
      utterance.rate = 0.85;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utterance);
    }
  }
}
