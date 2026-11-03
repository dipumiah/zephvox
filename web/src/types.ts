// Mirrors the Python data model (src/zephvox/engine/models.py and friends).

export type Span = [number, number];

export interface Finding {
  analyzer: string;
  sentence: string;
  target: string;
  said: string;
  expected: string;
  explanation: string;
  category: string;
  segment_index: number;
  word_span: Span | null;
  time_span: Span | null;
  confidence: number;
  details: Record<string, unknown>;
}

export interface AnalysisResult {
  session_id: string;
  audio_path: string;
  sentences: string[];
  findings: Finding[];
  providers: Record<string, string>;
  analyzers: string[];
  warnings: string[];
  timings: Record<string, number>;
  duration: number;
  created: string;
  sentence_spans: Span[];
}

export type Feedback = Record<string, string>; // finding index → "dismissed"

export interface SessionSummary {
  id: string;
  created: string;
  duration: number | null;
  counts: Record<string, number>;
  preview: string;
  pending?: boolean; // recorded while the server was unreachable
  unanalyzed?: boolean; // kept, but the analysis failed (e.g. a model was missing)
  analyzing?: boolean; // its analysis is running now
  queued?: boolean; // recorded, waiting for the running analysis to finish
}

export interface SyncStatus {
  last: number | null;
  error: string | null;
  offline: boolean;
  storage_full: boolean;
}

export type PhoneStatus = "ok" | "minor" | "wrong" | "missing" | "extra";

export interface Phone {
  expected: string | null;
  heard: string | null;
  status: PhoneStatus;
}

export interface WordScore {
  word: string;
  explanation: string;
  score: number;
  passed: boolean;
  status: "ok" | "no_speech" | "different_word";
  phones: Phone[];
  tip: string;
  accepted: string[];
}

export interface PracticeAttempt {
  target: string;
  score: number;
  passed: boolean;
  words: WordScore[];
  at: string;
  audio: string | null;
  source_session: string | null;
  status: "ok" | "no_speech" | "different_word";
}

export interface PracticeStats {
  target: string;
  attempts: number;
  best: number | null;
  last: number | null;
  passed: boolean;
  history: { score: number; passed: boolean; at: string }[];
}

export interface WeakWord {
  target: string;
  mistakes: number;
  best: number | null;
  attempts: number;
}

export interface Recording {
  mode: "analysis" | "practice";
  path: string;
  session_id: string | null;
  provider: string;
  started: number;
  meta: Record<string, unknown>;
  elapsed?: number;
  level?: number | null;
}

export interface Triggers {
  driver: string | null;
  active: boolean;
  error: string | null;
  description: string;
  bindings: Record<string, string>;
}

export interface ProviderStatus {
  kind: string;
  name: string;
  available: boolean;
  reason: string;
  selected: boolean;
}

export interface Status {
  busy: boolean;
  recording: Recording | null;
  practice_target: { text: string; session_id: string | null } | null;
  triggers: Triggers;
  engine?: EngineMode;
  hardware?: HardwareInfo;
  doctor: {
    system: { gpu: { backend: string; name: string; vram_gb: number } | null; ram_gb: number };
    providers: ProviderStatus[];
    analyzers: string[];
  };
}

export interface Device {
  id: string;
  name: string;
  is_default: boolean;
}

export interface Settings {
  capture: { provider?: string; device?: string | null };
  tts: { provider?: string; voice?: string | null };
  analyzers: string[];
  shortcuts: Record<string, string>;
}

export interface ProfileInfo {
  name: string;
  title: string;
  description: string;
  gpu: boolean;
  llm_model: string | null;
  accelerator: string;
}

export interface Profiles {
  current: string | null;
  recommended: string;
  hardware: string[];
  installed_accelerator: string | null;
  config_path: string;
  profiles: ProfileInfo[];
  needs?: string[]; // after a switch: what still has to happen
  models?: string[]; // grammar models still to download
}

export interface History {
  sessions: number;
  dismissed: number;
  categories: Record<string, [string, number][]>;
  targets: Record<string, [string, number][]>;
}

export interface HardwareInfo {
  cpu: string;
  cores: number;
  ram_gb: number;
  gpu: { name: string; vram_gb: number | null; backend: string | null } | null;
  accelerator: string;
  notes: string[];
}

export interface ServerInfo {
  version: string;
  analyzers: string[]; // can run now
  installed: string[];
  tts: boolean;
  source: string;
}

export interface ProfilePreview {
  name: string;
  llm_model: string | null;
  accelerator: string;
  needs: string[];
  download_gb: number;
  models?: string[]; // grammar models it uses that aren't downloaded
}

export interface EngineMode {
  mode: "local" | "remote";
  url: string | null;
}

export interface InstallProgress {
  running: boolean;
  ok: boolean | null;
  log: string[];
}

export interface RemoteInfo {
  mode: "local" | "remote";
  url: string | null;
  headers: string[];
  sync: boolean;
  connected: boolean;
  device: string | null;
  account: string | null;
  insecure: boolean;
  engine_installed: boolean;
  install: InstallProgress;
  sync_status: SyncStatus | null;
  reachable: boolean | null;
  error: string | null;
  server: ServerInfo | null;
}

// --- every option (GET /api/config) -------------------------------------------------------

export type FieldType = "bool" | "int" | "float" | "str" | "list" | "dict" | "choice";

export interface FieldSpec {
  key: string;
  type: FieldType;
  default: unknown;
  nullable?: boolean;
  choices?: string[];
}

export interface OptionGroup {
  fields: FieldSpec[];
  values: Record<string, unknown>;
  base: Record<string, unknown>;
}

export interface DriverInfo extends OptionGroup {
  name: string;
  available: boolean;
  reason: string;
}

export interface StageInfo {
  order: string[];
  base_order: string[];
  drivers: DriverInfo[];
}

export interface ModelItem {
  id: string;
  name: string;
  label: string;
  state: "queued" | "downloading" | "verifying" | "done" | "failed" | "paused";
  done: number | null; // bytes
  total: number | null;
  error: string | null;
}

/** Speech models being fetched in the background (all at once, ahead of first use). */
export interface ModelsProgress {
  running: boolean;
  error: string | null;
  items: ModelItem[];
  done: number;
  total: number | null;
  finished: boolean;
}

export interface ModelDownload {
  name: string;
  running: boolean;
  ok: boolean | null;
  percent: number | null;
  line: string;
}

export interface ConfigInfo {
  profile: string | null;
  source: string | null;
  engine: OptionGroup & { data_dir: string };
  providers: Record<string, StageInfo>;
  analyzers: Record<string, OptionGroup>;
  shortcuts: { values: Record<string, string>; base: Record<string, string> };
  server: OptionGroup & { limits: OptionGroup };
  overlay: Record<string, any>;
  in_file: string[];
  docs: Record<string, string>;
  suggestions: Record<string, { value: string; label: string }[]>;
  missing_models: string[];
  downloads: Record<string, ModelDownload>;
}

export interface ConfigChange {
  path: string[];
  value?: unknown;
  reset?: boolean;
}

// --- sharing this computer (GET /api/sharing) ---------------------------------------------

export interface Limits {
  max_audio_minutes: number;
  max_jobs: number;
  storage_mb: number;
  requests_per_minute: number;
}

export interface SharedAccount {
  id: string;
  name: string;
  created: number;
  limits: Limits;
  overrides: Partial<Limits>;
  usage: { active_jobs: number; storage_mb: number };
  webhook_secret: string;
  devices: { id: string; name: string; created: number; last_seen: number | null; revoked: number | null }[];
}

export interface SharingInfo {
  running: boolean;
  enabled: boolean;
  host: string | null;
  port: number | null;
  host_setting: string;
  port_setting: number;
  urls: string[];
  error: string | null;
  remote: boolean;
  accounts: SharedAccount[];
  defaults: { limits: Limits; overrides: Partial<Limits>; config: Limits };
  jobs: { id: string; account: string; status: string; created: number; duration: number | null; error: string | null }[];
  invite?: { account: string; code: string; expires: number };
}
