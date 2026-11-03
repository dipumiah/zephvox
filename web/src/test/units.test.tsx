import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { correctedSentence } from "../components/FindingCard";
import { PhoneTiles } from "../components/PhoneTiles";
import { ScoreRing } from "../components/ScoreRing";
import { Transcript, pieces } from "../components/Transcript";
import { WordInput } from "../components/WordInput";
import { AnalyzerToggles } from "../components/AnalyzerToggles";
import { initialState, reduce, type EngineState } from "../engine";
import { fmtDuration, plural, scoreColor } from "../format";
import { parseHash } from "../router";
import type { AnalysisResult, Finding } from "../types";

const finding = (over: Partial<Finding>): Finding => ({
  analyzer: "pronunciation",
  sentence: "I think we should go now.",
  target: "think",
  said: "/s ɪ ŋ k/",
  expected: "/θ ɪ ŋ k/",
  explanation: "",
  category: "θ→s",
  segment_index: 0,
  word_span: [1, 1],
  time_span: [0.2, 0.6],
  confidence: 1,
  details: {},
  ...over,
});

const grammar = finding({
  analyzer: "grammar",
  sentence: "Yesterday he go to market.",
  target: "he go",
  said: "he go",
  expected: "he went",
  category: "verb-tense",
  segment_index: 1,
  word_span: [1, 2],
});

const result: AnalysisResult = {
  session_id: "s1",
  audio_path: "",
  sentences: ["I think we should go now.", "Yesterday he go to market."],
  findings: [finding({}), grammar],
  providers: {},
  analyzers: ["pronunciation", "grammar"],
  warnings: [],
  timings: {},
  duration: 3,
  created: "2026-10-14T10:00:00",
  sentence_spans: [
    [0, 1.6],
    [1.8, 3],
  ],
};

describe("Transcript", () => {
  it("groups words of one finding into a single highlight", () => {
    expect(pieces(result.sentences[1], 1, result.findings)).toEqual([
      { text: "Yesterday", finding: null },
      { text: "he go", finding: 1 },
      { text: "to market.", finding: null },
    ]);
  });

  it("highlights mistakes, marks dismissed ones and reports clicks", async () => {
    const onSelect = vi.fn();
    render(<Transcript result={result} feedback={{ "1": "dismissed" }} selected={0} onSelect={onSelect} />);
    const word = screen.getByRole("button", { name: "pronunciation mistake: think" });
    expect(word).toHaveClass("w-pron", "selected");
    expect(screen.getByRole("button", { name: "grammar mistake: he go" })).toHaveClass("w-gram", "w-dismissed");
    await userEvent.click(word);
    expect(onSelect).toHaveBeenCalledWith(0);
  });

  it("explains an empty transcript", () => {
    render(<Transcript result={{ ...result, sentences: [] }} feedback={{}} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/No speech was recognized/)).toBeInTheDocument();
  });
});

describe("practice widgets", () => {
  it("colors each phone tile by status", () => {
    render(
      <PhoneTiles
        phones={[
          { expected: "θ", heard: "s", status: "wrong" },
          { expected: "ɪ", heard: "ɪ", status: "ok" },
          { expected: "k", heard: null, status: "missing" },
          { expected: null, heard: "ə", status: "extra" },
        ]}
      />,
    );
    expect(screen.getByLabelText("θ: wrong sound, heard s")).toHaveClass("tile", "wrong");
    expect(screen.getByLabelText("ɪ: correct")).toHaveClass("ok");
    expect(screen.getByLabelText("k: missing")).toHaveTextContent("—");
    expect(screen.getByLabelText("ə: extra sound")).toHaveTextContent("+");
  });

  it("clamps the score ring", () => {
    render(<ScoreRing score={140} />);
    expect(screen.getByRole("img", { name: "Score 100 out of 100" })).toBeInTheDocument();
  });

  it("builds the corrected sentence for grammar findings", () => {
    expect(correctedSentence(grammar)).toBe("Yesterday he went to market.");
    expect(correctedSentence({ ...grammar, said: "nope" })).toBe(grammar.sentence);
  });
});

describe("engine state", () => {
  const send = (state: EngineState, event: string, data: Record<string, unknown> = {}) =>
    reduce(state, { type: "server", event, data });

  it("follows a recording, its levels and the analysis job", () => {
    let s = send(initialState, "level", { level: 0.5 });
    expect(s).toBe(initialState); // levels without a recording are ignored
    s = send(s, "recording_started", { mode: "analysis" });
    s = send(s, "level", { level: 0.6, elapsed: 1.2 });
    expect(s.recording).toEqual({ mode: "analysis", level: 0.6, elapsed: 1.2 });
    expect(s.levels).toEqual([0.6]);
    s = send(s, "recording_stopped");
    expect(s.recording).toBeNull();
    s = send(s, "stage_started", { stage: "transcript" }); // no job yet → ignored
    s = send(s, "job_started", { job: "analysis" });
    s = send(s, "stage_started", { stage: "samples" });
    s = send(s, "stage_started", { stage: "transcript" });
    s = send(s, "stage_finished", { stage: "transcript" });
    s = send(s, "stage_started", { stage: "analyzer:grammar" });
    expect(s.job?.stages).toEqual([
      { key: "transcript", state: "done" },
      { key: "analyzer:grammar", state: "running" },
    ]);
    s = send(s, "analysis_done", { session_id: "s9" });
    s = send(s, "job_finished");
    expect(s.job).toBeNull();
    expect(s.lastAnalysis).toEqual({ sessionId: "s9", seq: 1 });
  });

  it("keeps practice state and de-duplicated notices", () => {
    let s = send(initialState, "practice_target", { target: { text: "think", session_id: null } });
    s = send(s, "practice_result", { attempt: { target: "think", score: 80 } });
    expect(s.practiceTarget?.text).toBe("think");
    expect(s.lastAttempt?.score).toBe(80);
    s = send(s, "job_error", { message: "boom" });
    s = send(s, "job_error", { message: "boom" });
    s = send(s, "warning", { message: "careful" });
    expect(s.notices.map((n) => [n.kind, n.text])).toEqual([
      ["error", "boom"],
      ["warning", "careful"],
    ]);
    s = reduce(s, { type: "dismiss_notice", id: s.notices[0].id });
    expect(s.notices).toHaveLength(1);
    s = send(s, "triggers", { driver: "portal", active: true, bindings: {} });
    expect(s.triggers?.driver).toBe("portal");
    expect(send(s, "something_new")).toBe(s);
  });

  it("restores from a status snapshot", () => {
    const s = reduce(initialState, {
      type: "status",
      status: {
        busy: false,
        recording: { mode: "practice", path: "", session_id: null, provider: "x", started: 0, meta: {}, elapsed: 3 },
        practice_target: null,
        triggers: { driver: null, active: false, error: null, description: "", bindings: {} },
        doctor: { system: { gpu: null, ram_gb: 8 }, providers: [], analyzers: [] },
      },
    });
    expect(s.recording).toEqual({ mode: "practice", level: 0, elapsed: 3 });
    expect(reduce(s, { type: "connected", value: true }).connected).toBe(true);
  });
});

describe("helpers", () => {
  it("formats and routes", () => {
    expect(fmtDuration(65.4)).toBe("1:05");
    expect(plural(1, "word")).toBe("1 word");
    expect(plural(2, "word")).toBe("2 words");
    expect(scoreColor(90)).toBe("var(--success)");
    expect(scoreColor(70)).toBe("var(--primary)");
    expect(scoreColor(10)).toBe("var(--alert)");
    const r = parseHash("#/practice?w=think&s=abc");
    expect(r.view).toBe("practice");
    expect(r.params.get("w")).toBe("think");
    expect(parseHash("#/report/2026%2001").id).toBe("2026 01");
    expect(parseHash("#/nonsense").view).toBe("home");
  });
});

describe("WordInput", () => {
  it("opens practice for a typed word, tidying the spacing", async () => {
    render(<WordInput />);
    const button = screen.getByRole("button", { name: /practice/i });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox"), "  thorough   work ");
    await userEvent.click(button);
    expect(parseHash(window.location.hash).params.get("w")).toBe("thorough work");
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("needs at least one letter", async () => {
    render(<WordInput />);
    await userEvent.type(screen.getByRole("textbox"), "123 !");
    expect(screen.getByRole("button", { name: /practice/i })).toBeDisabled();
  });
});

describe("AnalyzerToggles", () => {
  it("explains why the last check can't be turned off", async () => {
    const changes: string[][] = [];
    render(<AnalyzerToggles value={["pronunciation"]} onChange={(n) => changes.push(n)} />);
    await userEvent.click(screen.getByRole("switch", { name: /pronunciation/i }));
    expect(changes).toEqual([]);
    expect(screen.getByText("At least one check must stay on.")).toBeInTheDocument();
  });

  it("disables checks the server can't run", () => {
    render(<AnalyzerToggles value={["pronunciation", "grammar"]} available={["pronunciation"]} onChange={() => {}} />);
    expect(screen.getByRole("switch", { name: /grammar/i })).toBeDisabled();
    expect(screen.getByText(/Grammar isn't available on this server/)).toBeInTheDocument();
  });
});

describe("model downloads", () => {
  it("computes a percent and shows a banner only while something is wrong or running", async () => {
    const { percent, ModelDownloadsBanner } = await import("@/components/ModelDownloads");
    expect(percent(50, 200)).toBe(25);
    expect(percent(5, null)).toBeNull();
    expect(percent(500, 200)).toBe(100);
    expect(typeof ModelDownloadsBanner).toBe("function");
  });
});

describe("models_progress event", () => {
  it("stores the snapshot", () => {
    const snap = { running: true, error: null, items: [], done: 1, total: 2, finished: false };
    const next = reduce(initialState, { type: "server", event: "models_progress", data: snap });
    expect(next.models).toEqual(snap);
  });
});
