import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { AnalyzerToggles } from "@/components/AnalyzerToggles";
import { ConfigProvider, useConfig } from "@/components/config/ConfigContext";
import { Field } from "@/components/config/Field";
import { ShortcutEditor } from "@/components/config/ShortcutEditor";
import { Sharing } from "@/components/config/Sharing";
import { MissingModels, Options, Stage } from "@/components/config/Stage";
import { EngineSection } from "@/components/EngineSection";
import { ModelDownloadsPanel } from "@/components/ModelDownloads";
import { MicSelect } from "@/components/MicSelect";
import { PageHead } from "@/components/PageHead";
import { ProfileSection } from "@/components/ProfileSection";
import { SavedMark, useSaved } from "@/components/Saved";
import { SettingRow, SettingsSection } from "@/components/SettingsSection";
import { VoiceSelect } from "@/components/VoiceSelect";
import { LICENSE_NAME, SOURCE_URL } from "@/about";
import { api } from "@/api";
import { useAction, useEngine } from "@/engine";
import { setTheme, useTheme, type ThemeChoice } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { navigate, useRoute } from "@/router";
import type { ServerInfo, Status } from "@/types";

const KINDS: Record<string, string> = {
  capture: "Microphone",
  stt: "Speech to text",
  g2p: "Expected sounds",
  phonemes: "Heard sounds",
  llm: "Grammar model",
  tts: "Reference voice",
  triggers: "Shortcuts",
};

type Tab = "general" | "models" | "devices" | "sharing" | "about";

const segment = "h-8 rounded-full px-3.5 data-[state=on]:bg-card data-[state=on]:shadow-soft";

function LocalBadge({ show }: { show: boolean }) {
  return show ? <Badge variant="muted">on this computer</Badge> : null;
}

function EngineField({ k: key }: { k: string }) {
  const { info } = useConfig();
  const spec = info.engine.fields.find((f) => f.key === key);
  return spec ? <Field path={["engine", key]} spec={spec} value={info.engine.values[key]} base={info.engine.base[key]} /> : null;
}

function General({ remote, server }: { remote: boolean; server: ServerInfo | null }) {
  const { state } = useEngine();
  const run = useAction();
  const saved = useSaved();
  const theme = useTheme();
  const { info } = useConfig();
  const analyzers = (info.engine.values.analyzers as string[]).filter((a) => a !== "practice");
  const triggers = state.triggers;

  return (
    <div className="grid gap-6">
      <SettingsSection id="recording" title="Recording" aside={<LocalBadge show={remote} />}>
        <SettingRow label="Microphone" htmlFor="mic" description="Used for recordings and practice." aside={<SavedMark show={saved.is("mic")} />}>
          <MicSelect id="mic" onSaved={() => saved.flash("mic")} />
        </SettingRow>
        <SettingRow label="Reference voice" htmlFor="voice" description="The voice that says words back to you." aside={<SavedMark show={saved.is("voice")} />}>
          <VoiceSelect id="voice" onSaved={() => saved.flash("voice")} />
        </SettingRow>
        <AnalyzerToggles
          value={analyzers}
          available={remote && server ? server.analyzers : undefined}
          savedMark={<SavedMark show={saved.is("analyzers")} />}
          onChange={async (next) => {
            if (await run(() => api.saveSettings({ analyzers: next }))) saved.flash("analyzers");
          }}
        />
        <EngineField k="notifications" />
      </SettingsSection>

      <SettingsSection
        id="shortcuts"
        title="Shortcuts"
        aside={
          triggers?.active ? (
            <Badge variant="success">Active via {triggers.driver}</Badge>
          ) : triggers?.error ? (
            <Badge variant="alert">Not active</Badge>
          ) : null
        }
        description={
          triggers?.driver === "portal"
            ? "Registered with your desktop. The keys shown are the ones it really uses, including any you changed in its own settings."
            : triggers?.active
              ? triggers.description
              : undefined
        }
      >
        <ShortcutEditor />
        {triggers && !triggers.active && (
          <p className="m-0 py-4 text-sm text-muted-foreground">
            {triggers.error ? <span className="text-alert-ink">{triggers.error}. </span> : "Registering shortcuts… "}
            You can still bind <code>zephvox toggle</code> and <code>zephvox practice</code> in your system's keyboard settings.
          </p>
        )}
      </SettingsSection>

      <EngineSection />
      {!remote && <ProfileSection />}

      <SettingsSection id="appearance" title="Appearance">
        <SettingRow label="Theme" description="Light is the default. System follows your computer's setting.">
          <ToggleGroup type="single" value={theme.choice} onValueChange={(v) => v && setTheme(v as ThemeChoice)} aria-label="Theme" className="rounded-full bg-foreground/[0.05] p-1">
            <ToggleGroupItem value="light" className={segment}>
              <Sun /> Light
            </ToggleGroupItem>
            <ToggleGroupItem value="dark" className={segment}>
              <Moon /> Dark
            </ToggleGroupItem>
            <ToggleGroupItem value="system" className={segment}>
              <Monitor /> System
            </ToggleGroupItem>
          </ToggleGroup>
        </SettingRow>
      </SettingsSection>
    </div>
  );
}

function ModelsAndChecks() {
  const { info } = useConfig();
  const a = info.analyzers;
  return (
    <div className="grid gap-6">
      <p className="m-0 text-sm text-muted-foreground">
        Everything the profile and <code>config.toml</code> set, changeable here. A change applies to the next recording; “Reset” puts back
        what the profile and <code>config.toml</code> give.
      </p>
      <SettingsSection id="downloads" title="Downloads" description="Speech models are fetched in the background so they're ready before you need them.">
        <ModelDownloadsPanel />
      </SettingsSection>
      <Stage id="stt" kind="stt" title="Speech to text" description="Turns your recording into words, with a time for each word." />
      <Stage id="g2p" kind="g2p" title="Expected sounds" description="How each word should sound: dictionaries first, a model for words they don't have." />
      <Stage id="phonemes" kind="phonemes" title="Heard sounds" description="Which sounds you actually made, straight from the audio." />
      <Stage id="llm" kind="llm" title="Grammar" description="The language model that reads your sentences for mistakes.">
        <MissingModels names={info.missing_models} />
      </Stage>
      {a.grammar && (
        <SettingsSection id="grammar-check" title="Grammar check" description="How sure the model must be before a mistake is shown.">
          <Options path={["analyzer", "grammar"]} group={a.grammar} />
        </SettingsSection>
      )}
      {a.pronunciation && (
        <SettingsSection id="pronunciation-check" title="Pronunciation check" description="When a sound counts as a mistake.">
          <Options path={["analyzer", "pronunciation"]} group={a.pronunciation} />
          <EngineField k="sentence_split" />
        </SettingsSection>
      )}
      {a.practice && (
        <SettingsSection id="practice-check" title="Practice" description="Scoring and timing of practice attempts.">
          <Options path={["analyzer", "practice"]} group={a.practice} />
          <EngineField k="practice_auto_stop_s" />
          <EngineField k="practice_max_s" />
        </SettingsSection>
      )}
      <Stage id="tts" kind="tts" title="Reference voice" description="Says words and sentences back to you. The voice itself is chosen under General." />
    </div>
  );
}

function Devices({ remote }: { remote: boolean }) {
  return (
    <div className="grid gap-6">
      {remote && <p className="m-0 text-sm text-muted-foreground">Analysis runs on the server; these drivers are this computer's own.</p>}
      <Stage id="capture" kind="capture" title="Microphone driver" description="How zephvox records. The microphone itself is chosen under General." />
      <Stage id="triggers" kind="triggers" title="Shortcut driver" description="How the global shortcuts reach zephvox while another app is in front." />
    </div>
  );
}

function About({ remote }: { remote: boolean }) {
  const run = useAction();
  const [status, setStatus] = useState<Status | null>(null);
  const { info } = useConfig();
  useEffect(() => {
    run(api.status).then((s) => s && setStatus(s));
  }, []);
  const hw = status?.hardware;
  const kinds = remote ? ["capture", "triggers"] : Object.keys(KINDS);
  return (
    <div className="grid gap-6">
      {!remote && (
        <SettingsSection id="hardware" title="This computer">
          <dl className="m-0 grid grid-cols-[9rem_minmax(0,1fr)] gap-x-4 gap-y-2 py-4 text-sm">
            <dt className="text-muted-foreground">Graphics card</dt>
            <dd className="m-0">
              {hw
                ? hw.gpu
                  ? `${hw.gpu.name}${hw.gpu.vram_gb ? `, ${hw.gpu.vram_gb.toFixed(0)} GB` : ""}` +
                    (hw.gpu.backend ? ` (${hw.gpu.backend.toUpperCase()})` : " (not usable for the models here)")
                  : "None: models run on the processor"
                : "…"}
            </dd>
            <dt className="text-muted-foreground">Processor</dt>
            <dd className="m-0">{hw ? `${hw.cpu} (${hw.cores} threads)` : "…"}</dd>
            <dt className="text-muted-foreground">Memory</dt>
            <dd className="m-0">{hw ? `${hw.ram_gb.toFixed(0)} GB` : "…"}</dd>
            <dt className="text-muted-foreground">Settings file</dt>
            <dd className="m-0 break-all">{info.source ?? "none (built-in defaults)"}</dd>
            <dt className="text-muted-foreground">Recordings</dt>
            <dd className="m-0 break-all">{info.engine.data_dir}</dd>
          </dl>
          {hw?.notes.map((n) => (
            <p key={n} className="m-0 pb-3 text-sm text-alert-ink">
              {n}
            </p>
          ))}
        </SettingsSection>
      )}
      <SettingsSection id="drivers" title="Drivers in use" description="★ in use · ✗ can't run here (hover for why). Change them under Models & checks or Devices.">
        {status ? (
          <dl className="m-0 grid grid-cols-[9rem_minmax(0,1fr)] gap-x-4 gap-y-2.5 py-4 text-sm">
            {kinds.map((kind) => (
              <div key={kind} className="contents">
                <dt className="text-muted-foreground">{KINDS[kind]}</dt>
                <dd className="m-0 flex flex-wrap gap-1.5">
                  {status.doctor.providers
                    .filter((p) => p.kind === kind)
                    .map((d) => (
                      <Badge key={d.name} variant={d.selected ? "sky" : d.available ? "muted" : "alert"} title={d.reason || (d.selected ? "in use" : "available")}>
                        {d.selected ? "★ " : d.available ? "" : "✗ "}
                        {d.name}
                      </Badge>
                    ))}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="py-4 text-sm text-muted-foreground">Checking drivers…</p>
        )}
      </SettingsSection>
      <p className="m-0 text-xs text-muted-foreground">
        zephvox is free software under the {LICENSE_NAME} license:{" "}
        <a href={SOURCE_URL} target="_blank" rel="noreferrer" className="text-link underline underline-offset-2">
          source code
        </a>
        .
      </p>
    </div>
  );
}

export function Settings() {
  const { state } = useEngine();
  const run = useAction();
  const route = useRoute();
  const [server, setServer] = useState<ServerInfo | null>(null);
  const remote = state.engineMode?.mode === "remote";

  // In client mode, offer only the checks the server can run.
  useEffect(() => {
    if (remote) run(() => api.remote(true)).then((r) => r && setServer(r.server));
    else setServer(null);
  }, [remote]);

  const tabs: [Tab, string][] = [
    ["general", "General"],
    ...(remote ? [] : ([["models", "Models & checks"]] as [Tab, string][])),
    ["devices", "Devices"],
    ...(remote ? [] : ([["sharing", "Sharing"]] as [Tab, string][])),
    ["about", "Diagnostics"],
  ];
  const asked = route.params.get("tab") as Tab | null;
  const tab: Tab = tabs.some(([t]) => t === asked) ? asked! : "general";

  return (
    <>
      <PageHead title="Settings">
        {remote ? "This computer records; a zephvox server does the analysis." : "Everything zephvox does, and how."} Changes are saved as you
        make them.
      </PageHead>

      <nav aria-label="Settings" className="mb-6 flex flex-wrap gap-2" role="tablist">
        {tabs.map(([t, label]) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            onClick={() => navigate("status", undefined, t === "general" ? {} : { tab: t })}
            className={cn(
              "h-9 rounded-full border px-4 text-sm font-semibold transition-colors",
              tab === t ? "border-foreground bg-foreground text-background" : "border-border bg-card text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </nav>

      <ConfigProvider fallback={<p className="text-muted-foreground">Loading settings…</p>}>
        {() => (
          <div role="tabpanel">
            {tab === "general" && <General remote={remote} server={server} />}
            {tab === "models" && <ModelsAndChecks />}
            {tab === "devices" && <Devices remote={remote} />}
            {tab === "sharing" && <Sharing />}
            {tab === "about" && <About remote={remote} />}
          </div>
        )}
      </ConfigProvider>
    </>
  );
}
