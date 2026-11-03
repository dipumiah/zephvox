import json

import sys
import pytest

from zephvox import cli
from zephvox.engine.engine import Engine
from zephvox.engine.models import AnalysisResult, Finding
from zephvox.reporters.base import fmt_time, group
from zephvox.reporters.json import JsonReporter
from zephvox.reporters.markdown import MarkdownReporter
from zephvox.reporters.terminal import TerminalReporter


@pytest.fixture
def result():
    return AnalysisResult(
        session_id="20261013-220000",
        audio_path="/tmp/a.wav",
        sentences=["I think so.", "He go | home."],
        findings=[
            Finding("grammar", "He go | home.", "he go", "he go", "he went", "Past tense.", "verb-tense",
                    segment_index=1),
            Finding("pronunciation", "I think so.", "think", "/s ɪ ŋ k/", "/θ ɪ ŋ k/",
                    "You said 's' instead of 'th'.", "θ→s", time_span=(62.5, 63.0), word_span=(1, 1)),
            Finding("fluency", "I think so.", "um", "um", "", "", "filler"),
        ],  # fmt: skip
        providers={"stt": "hf-whisper"},
        analyzers=["pronunciation", "grammar", "spelling"],
        warnings=["careful"],
        duration=65.0,
    )


def test_helpers(result):
    assert fmt_time(65.25) == "1:05.2"
    assert [name for name, _ in group(result)] == ["pronunciation", "grammar", "spelling", "fluency"]


def test_terminal(result):
    text = TerminalReporter().render(result)
    assert "Session 20261013-220000 · 2 sentence(s) · 1:05.0" in text
    assert "think: you said /s ɪ ŋ k/, expected /θ ɪ ŋ k/  (θ→s)" in text
    assert '"he go" → "he went"' in text
    assert "“I think so.” [1:02.5]" in text
    assert "Spelling (0)\n  No mistakes found." in text
    assert "Warnings:\n  - careful" in text


def test_markdown(result):
    text = MarkdownReporter().render(result)
    assert "## Pronunciation (1)" in text
    assert "| 1 | think | /s ɪ ŋ k/ | /θ ɪ ŋ k/ |" in text
    assert "He go \\| home." in text  # pipes escaped
    assert "## Spelling (0)\n\nNo mistakes found." in text
    assert "## Warnings" in text


def test_json(result):
    data = json.loads(JsonReporter().render(result))
    assert AnalysisResult.from_dict(data) == result


@pytest.fixture
def run_cli(engine, capsys):
    def run(*argv, ask=lambda prompt: ""):
        code = cli.main(list(argv), engine_factory=lambda config: engine, ask=ask)
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def test_cli_analyze_with_output_file(run_cli, wav_file, tmp_path):
    out_file = tmp_path / "report.md"
    code, out, err = run_cli("analyze", str(wav_file), "--format", "markdown", "-o", str(out_file))
    assert code == 0
    assert "## Pronunciation (1)" in out and out_file.read_text() == out
    assert "· transcript …" in err and "using stt provider: fake-stt" in err and "skipped" in err


def test_cli_record_and_report(run_cli):
    prompts = []
    code, out, err = run_cli("record", "--analyzers", "grammar", "-q", ask=prompts.append)
    assert code == 0 and prompts and "Grammar (1)" in out and "Pronunciation" not in out
    assert err == ""
    code, out, _ = run_cli("report", "--format", "json")
    assert code == 0 and json.loads(out)["analyzers"] == ["grammar"]


def test_cli_start_stop(run_cli):
    code, out, err = run_cli("start")
    assert code == 0 and "Run `zephvox stop`" in out and "● recording" in err
    code, out, _ = run_cli("stop", "-q")
    assert code == 0 and "Pronunciation (1)" in out


def test_cli_report_without_sessions(run_cli):
    code, _, err = run_cli("report")
    assert code == 1 and "No analyzed session" in err


def test_cli_providers_doctor_history(run_cli, wav_file):
    code, out, _ = run_cli("providers")
    assert code == 0 and "★ stt       fake-stt" in out and "✗ stt       broken-stt        — not installed" in out
    code, out, _ = run_cli("doctor")
    assert json.loads(out)["analyzers"] == ["grammar", "practice", "pronunciation"]
    run_cli("analyze", str(wav_file), "-q")
    code, out, _ = run_cli("history", "--limit", "3")
    assert "1 analyzed session(s)" in out and "1× think" in out and "θ→s (1)" in out


def test_cli_errors_exit_2(run_cli):
    code, _, err = run_cli("stop")
    assert code == 2 and "error: Not recording" in err
    code, _, err = run_cli("analyze", "x.wav", "--set", "bad")
    assert code == 2 and "Override must look like" in err


def test_cli_keyboard_interrupt_still_stops_recording(engine, capsys):
    def interrupt(prompt):
        raise KeyboardInterrupt

    code = cli.main(["record", "-q"], engine_factory=lambda c: engine, ask=interrupt)
    assert code == 130 and not engine.is_recording()


def test_cli_builds_engine_from_config(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[engine]\ndata_dir = "{(tmp_path / "d").as_posix()}"\n')
    built = []

    def factory(config):
        built.append(config)
        return Engine(config=config, caps=None)

    argv = ["report", "--config", str(cfg), "--set", "stt=faster-whisper"]
    assert cli.main(argv, engine_factory=factory) == 1
    assert built[0].providers["stt"] == ["faster-whisper"]


@pytest.mark.skipif(sys.platform != "linux", reason="writes a Linux .desktop entry")
def test_cli_desktop_entry(run_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    code, out, _ = run_cli("desktop-entry", "-q")
    entry = tmp_path / "applications" / "io.github.zephvox.desktop"
    assert code == 0 and str(entry) in out
    text = entry.read_text()
    assert "Exec=" in text and "-m zephvox.cli serve --open" in text
    assert (tmp_path / "icons" / "io.github.zephvox.svg").read_text().startswith("<svg")
