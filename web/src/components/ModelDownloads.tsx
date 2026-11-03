import { Check, CircleAlert, Download, Pause, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/api";
import { useAction, useEngine } from "@/engine";
import { fmtBytes } from "@/format";
import type { ModelItem, ModelsProgress } from "@/types";

const STATE_LABEL: Record<ModelItem["state"], string> = {
  queued: "Waiting",
  downloading: "Downloading",
  verifying: "Checking",
  done: "Ready",
  failed: "Failed",
  paused: "Paused",
};

export function percent(done: number | null | undefined, total: number | null | undefined): number | null {
  if (!total || done == null) return null;
  return Math.min(100, Math.floor((done / total) * 100));
}

function Bar({ value, label }: { value: number | null; label: string }) {
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value ?? undefined}
      className="h-2 w-full overflow-hidden rounded-full bg-foreground/10"
    >
      <div
        className={value == null ? "h-full w-1/3 animate-pulse rounded-full bg-primary" : "h-full rounded-full bg-primary transition-[width]"}
        style={value == null ? undefined : { width: `${value}%` }}
      />
    </div>
  );
}

function Row({ item }: { item: ModelItem }) {
  const p = item.state === "done" ? 100 : percent(item.done, item.total);
  return (
    <li className="grid gap-1.5 py-3">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="min-w-0 truncate font-semibold">{item.label}</span>
        <span className="flex shrink-0 items-center gap-1.5 text-muted-foreground">
          {item.state === "done" && <Check className="size-4 text-success" aria-hidden />}
          {item.state === "failed" && <CircleAlert className="size-4 text-alert" aria-hidden />}
          {STATE_LABEL[item.state]}
          {item.state === "downloading" && p != null && ` ${p}%`}
        </span>
      </div>
      {item.state !== "done" && item.state !== "failed" && <Bar value={p} label={item.label} />}
      <div className="text-xs text-muted-foreground">
        {item.state === "failed"
          ? (item.error ?? "Download failed")
          : item.state === "done"
            ? fmtBytes(item.total)
            : `${fmtBytes(item.done)} of ${fmtBytes(item.total)}`}
      </div>
    </li>
  );
}

function useControls() {
  const run = useAction();
  const { dispatch } = useEngine();
  const apply = (fn: () => Promise<ModelsProgress>) => run(fn).then((models) => models && dispatch({ type: "models", models }));
  return { pause: () => apply(api.cancelModelDownloads), resume: () => apply(api.startModelDownloads) };
}

/** Every model with its own progress bar, and pause / resume. */
export function ModelDownloadsPanel() {
  const { state } = useEngine();
  const { pause, resume } = useControls();
  const m = state.models;
  if (!m || (m.items.length === 0 && !m.error)) {
    return <p className="m-0 py-4 text-sm text-muted-foreground">All the speech models for this profile are downloaded.</p>;
  }
  const overall = m.finished ? 100 : percent(m.done, m.total);
  return (
    <div className="py-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">
          {m.finished ? "All models downloaded" : m.running ? `Downloading ${fmtBytes(m.done)} of ${fmtBytes(m.total)}` : "Not downloading"}
        </span>
        {!m.finished &&
          (m.running ? (
            <Button size="sm" variant="outline" onClick={pause}>
              <Pause /> Pause
            </Button>
          ) : (
            <Button size="sm" onClick={resume}>
              <Play /> {m.items.some((i) => i.state === "failed") ? "Retry" : "Resume"}
            </Button>
          ))}
      </div>
      <Bar value={overall} label="All models" />
      {m.error && <p className="mt-2 mb-0 text-sm text-alert">{m.error}</p>}
      <ul className="m-0 mt-2 list-none divide-y divide-border p-0">
        {m.items.map((item) => (
          <Row key={item.id} item={item} />
        ))}
      </ul>
    </div>
  );
}

/** A slim strip above every page while models download in the background (or after one failed). */
export function ModelDownloadsBanner({ onOpen }: { onOpen: () => void }) {
  const { state } = useEngine();
  const { resume } = useControls();
  const m = state.models;
  if (!m || m.finished || (!m.running && !m.error && !m.items.some((i) => i.state === "failed"))) return null;
  const failed = !m.running && (m.error || m.items.some((i) => i.state === "failed"));
  const p = percent(m.done, m.total);
  return (
    <div role="status" className="surface mb-5 grid gap-2 p-3 text-sm sm:px-4">
      <div className="flex items-center justify-between gap-3">
        <span className="flex min-w-0 items-center gap-2 font-semibold">
          {failed ? <CircleAlert className="size-4 shrink-0 text-alert" aria-hidden /> : <Download className="size-4 shrink-0" aria-hidden />}
          <span className="truncate">
            {failed ? "A model didn't finish downloading" : `Downloading speech models${p != null ? ` · ${p}%` : ""}`}
          </span>
        </span>
        <span className="flex shrink-0 gap-2">
          {failed && (
            <Button size="xs" onClick={resume}>
              Retry
            </Button>
          )}
          <Button size="xs" variant="outline" onClick={onOpen}>
            Details
          </Button>
        </span>
      </div>
      {!failed && <Bar value={p} label="Downloading speech models" />}
      {!failed && (
        <span className="text-xs text-muted-foreground">
          {fmtBytes(m.done)} of {fmtBytes(m.total)}. You can keep using zephvox; recording works once the speech models are ready.
        </span>
      )}
    </div>
  );
}
