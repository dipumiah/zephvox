"""Machine-readable progress: the event lines the desktop app reads."""

import io
import json
from contextlib import redirect_stdout

import pytest

from zephvox.install import download, events, wizard
from zephvox.providers.base import Download


def lines_of(fn):
    out = io.StringIO()
    with redirect_stdout(out):
        result = fn()
    return result, [e for line in out.getvalue().splitlines() if (e := events.parse(line))]


def test_parse_ignores_ordinary_text_and_bad_json():
    assert events.parse("Downloading foo") is None
    assert events.parse(events.PREFIX + "{not json") is None
    assert events.parse(events.PREFIX + json.dumps({"type": "log", "text": "x"})) == {"type": "log", "text": "x"}


def test_run_json_reports_queue_progress_done_and_failures():
    items = [Download("huggingface", "a/one", 1.0), Download("faster-whisper", "base.en", 0.1),
             Download("ollama", "qwen3:8b", 5.0)]  # fmt: skip

    def fetcher(d, report):
        if d.name == "base.en":
            raise OSError("offline")
        report(5, 10)

    code, evs = lines_of(lambda: download.run_json(items, fetcher))
    assert code == 1  # one failed, the other still ran
    by_id = {}
    for e in evs:
        by_id.setdefault(e["id"], []).append(e["state"])
    assert "ollama:qwen3:8b" not in by_id  # setup pulls Ollama models itself
    assert by_id["huggingface:a/one"] == ["queued", "downloading", "downloading", "done"]
    assert by_id["faster-whisper:base.en"] == ["queued", "downloading", "failed"]
    progress = [e for e in evs if e["id"] == "huggingface:a/one" and e["state"] == "downloading"][-1]
    assert (progress["done"], progress["total"]) == (5, 10)
    failed = [e for e in evs if e["state"] == "failed"][0]
    assert failed["error"] == "offline"


def test_progress_tqdm_adds_up_file_bars():
    pytest.importorskip("tqdm")  # comes with the model packages
    seen = []
    cls = download.progress_tqdm(lambda done, total: seen.append((done, total)), min_interval=0)
    a = cls(total=100, unit="B")
    b = cls(total=50, unit="B")
    other = cls(total=3)  # not bytes: ignored
    a.update(40)
    b.update(50)
    other.update(1)
    a.close(), b.close(), other.close()
    assert seen[-1] == (40 + 50, 150)


def test_console_events_mode_emits_steps_and_logs():
    console = wizard.Console(interactive=True, events=True)  # interactive flag is ignored by run_setup
    _, evs = lines_of(lambda: (console.step("Installing"), console.say("hello")))
    assert evs[0]["type"] == "step" and evs[0]["title"] == "Installing" and evs[0]["n"] == 1
    assert evs[1] == {"type": "log", "text": "hello"}


# --- the daemon's background model downloads -------------------------------------------------


class FakeProc:
    def __init__(self, lines, code=0, hold=False):
        self.code = code
        self.terminated = False
        self._stop = __import__("threading").Event()
        self._hold = hold

        def output():
            yield from lines
            if hold:  # a downloader that keeps running until it is terminated
                self._stop.wait(5)

        self.stdout = output()

    def poll(self):
        return None

    def wait(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self._stop.set()


def event_line(**kw):
    return events.PREFIX + json.dumps({"type": "download", "error": None, **kw}) + "\n"


def test_model_downloads_track_items_and_publish():
    from zephvox.web.models import ModelDownloads

    published = []
    lines = [event_line(id="a", name="a", label="a", state="queued", done=0, total=100),
             event_line(id="a", name="a", label="a", state="downloading", done=40, total=100),
             "some log text\n",
             event_line(id="a", name="a", label="a", state="done", done=100, total=100)]  # fmt: skip
    m = ModelDownloads(lambda type, **d: published.append((type, d)), spawn=lambda *a, **k: FakeProc(lines))
    m.start(None)
    for _ in range(100):
        if not m.snapshot()["running"]:
            break
        import time

        time.sleep(0.01)
    snap = m.snapshot()
    assert snap["finished"] and snap["done"] == 100 and snap["total"] == 100 and snap["error"] is None
    assert published[-1][0] == "models_progress" and published[-1][1]["finished"]


def test_cancel_marks_unfinished_items_paused():
    from zephvox.web.models import ModelDownloads

    proc = FakeProc([event_line(id="a", name="a", label="a", state="downloading", done=1, total=9)], code=-15, hold=True)
    m = ModelDownloads(lambda *a, **k: None, spawn=lambda *a, **k: proc)
    m.cancel()  # nothing running yet: harmless
    m.start(None)
    m.cancel()
    assert proc.terminated
    import time

    for _ in range(100):
        if not m.snapshot()["running"]:
            break
        time.sleep(0.01)
    snap = m.snapshot()
    assert snap["items"][0]["state"] == "paused" and snap["error"] is None


def test_a_crashing_downloader_is_reported():
    from zephvox.web.models import ModelDownloads

    m = ModelDownloads(lambda *a, **k: None, spawn=lambda *a, **k: FakeProc([], code=1))
    m.start(None)
    import time

    for _ in range(100):
        if not m.snapshot()["running"]:
            break
        time.sleep(0.01)
    assert "stopped unexpectedly" in m.snapshot()["error"]

