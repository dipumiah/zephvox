"""Daemon, HTTP API, daemon client and daemon-aware CLI."""

import asyncio
import contextlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from zephvox import cli
from zephvox.engine.errors import ZephvoxError
from zephvox.engine.models import Finding
from zephvox.providers.base import Availability, TriggerProvider, TTSProvider
from zephvox.web import client as web_client
from zephvox.web import server as web_server
from zephvox.web.app import create_app
from zephvox.web.daemon import Daemon, EventHub, jsonable
from tests.conftest import FakeLLM


class FakeTTS(TTSProvider):
    name = "fake-tts"
    calls = 0

    def synthesize(self, text):
        type(self).calls += 1
        if text == "boom":
            raise ZephvoxError("synth failed")
        return b"RIFF" + text.encode()


class FakeTriggers(TriggerProvider):
    name = "fake-triggers"
    instance = None

    def start(self, bindings, fire):
        self.bindings, self.fire_cb, self.stopped = bindings, fire, False
        FakeTriggers.instance = self

    def stop(self):
        self.stopped = True

    def describe(self):
        return "rebind in config"


class ClosingLLM(FakeLLM):
    name = "closing-llm"
    closed = 0

    def close(self):
        type(self).closed += 1


@pytest.fixture
def daemon(engine, registry):
    registry.register("tts", "fake-tts", FakeTTS)
    registry.register("triggers", "fake-triggers", FakeTriggers)
    registry.register("llm", "closing-llm", ClosingLLM)
    engine.config.providers.update({"tts": ["fake-tts"], "triggers": ["fake-triggers"], "llm": ["closing-llm"]})
    ClosingLLM.closed = 0
    d = Daemon(engine, start_triggers=False)
    # It detects the real machine in a thread at start-up; wait for that, or it could overwrite the
    # hardware a test pins (so results would depend on the machine and on timing).
    for t in threading.enumerate():
        if t.name == "hardware":
            t.join(30)
    events = []
    engine.events.subscribe(events.append)
    d.recorded = events
    original = d.hub.publish
    d.published = []

    def publish(type, data):
        d.published.append((type, data))
        original(type, data)

    d.hub.publish = publish
    yield d
    d.close()


@pytest.fixture
def api(daemon):
    return TestClient(create_app(daemon))


def wait_for_jobs(daemon):
    daemon._worker.submit(lambda: None).result(timeout=10)


def published(daemon, type):
    return [data for t, data in daemon.published if t == type]


class TestDaemonActions:
    def test_record_stop_runs_analysis_and_releases_llm(self, daemon):
        daemon.start(analyzers=["grammar"])
        rec = daemon.stop()
        wait_for_jobs(daemon)
        session = daemon.engine.sessions.get(rec.session_id)
        result = daemon.engine.sessions.load_result(session)
        assert result.analyzers == ["grammar"] and result.by_analyzer("grammar")
        assert ClosingLLM.closed == 1 and "llm" not in daemon.engine._providers
        assert published(daemon, "analysis_done") == [{"session_id": rec.session_id}]
        assert [t for t, _ in daemon.published if t.startswith("job_")] == ["job_queued", "job_started", "job_finished"]

    def test_practice_needs_target_then_scores(self, daemon):
        with pytest.raises(ZephvoxError, match="Pick a word"):
            daemon.start(mode="practice")
        daemon.set_practice_target("think", session_id="s1")
        assert published(daemon, "practice_target")[-1] == {"target": {"text": "think", "session_id": "s1"}}
        daemon.toggle(mode="practice")
        daemon.toggle(mode="practice")
        wait_for_jobs(daemon)
        attempt = published(daemon, "practice_result")[0]["attempt"]
        assert attempt["target"] == "think" and attempt["source_session"] == "s1"
        daemon.set_practice_target(None)
        assert daemon.practice_target is None

    def test_toggle_never_crosses_modes(self, daemon):
        daemon.set_practice_target("think")
        daemon.toggle("analysis")
        with pytest.raises(ZephvoxError, match="analysis recording is in progress"):
            daemon.toggle("practice")
        assert daemon.engine.is_recording()
        daemon.cancel()

    def test_fire_actions_and_errors(self, daemon):
        daemon.fire("toggle_recording")
        assert daemon.engine.is_recording()
        daemon.fire("cancel")
        assert not daemon.engine.is_recording()
        daemon.fire("cancel")  # nothing to cancel: quietly ignored
        daemon.fire("practice_attempt")  # no target
        daemon.fire("self_destruct")
        errors = published(daemon, "action_error")
        assert [e["action"] for e in errors] == ["practice_attempt", "self_destruct"]

    def test_job_errors_are_published(self, daemon):
        def boom():
            raise ZephvoxError("model exploded")

        def crash():
            raise ValueError("bug")

        for fn in (boom, crash):
            with pytest.raises(Exception):
                daemon._submit("x", fn).result(timeout=5)
        assert [e["message"] for e in published(daemon, "job_error")] == ["model exploded", "ValueError: bug"]
        assert not daemon.busy

    def test_settings_update_switches_device(self, daemon):
        daemon.engine.provider("capture")
        out = daemon.update_settings({"capture": {"provider": "fake-capture", "device": "mic-2"}, "bogus": 1})
        assert out["capture"] == {"provider": "fake-capture", "device": "mic-2"}
        assert "capture" not in daemon.engine._providers  # rebuilt with the new device next time
        assert daemon.engine.config.provider_settings("fake-capture") == {"device": "mic-2"}
        daemon.update_settings({"analyzers": ["pronunciation"]})
        assert daemon.settings()["analyzers"] == ["pronunciation"]

    def test_triggers_started_and_dispatched(self, daemon):
        daemon._start_triggers()
        trig = FakeTriggers.instance
        assert trig.bindings["toggle_recording"] == "Ctrl+Shift+Alt+R"
        assert daemon.triggers["active"] and daemon.triggers["description"] == "rebind in config"
        trig.fire_cb("toggle_recording")
        assert daemon.engine.is_recording()
        daemon.fire("cancel")
        assert daemon.status()["triggers"]["driver"] == "fake-triggers"

    def test_triggers_unavailable(self, daemon):
        daemon.engine.config.providers["triggers"] = []
        daemon._start_triggers()
        assert not daemon.triggers["active"] and "No usable triggers" in daemon.triggers["error"]

    def test_close_cancels_recording_and_stops_triggers(self, engine, registry):
        registry.register("triggers", "fake-triggers", FakeTriggers)
        engine.config.providers["triggers"] = ["fake-triggers"]
        d = Daemon(engine, start_triggers=False)
        d._start_triggers()
        d.start()
        d.close()
        assert not engine.is_recording() and FakeTriggers.instance.stopped


def stop_real_ticker(daemon):
    daemon._stop.set()
    daemon._ticker.join(timeout=5)


def fake_stop(*waits):
    it = iter(waits)
    return SimpleNamespace(wait=lambda t: next(it), set=lambda: None)


class TestTicker:
    def run_ticks(self, daemon, statuses, monkeypatch):
        stop_real_ticker(daemon)
        seq = iter(statuses)
        monkeypatch.setattr(daemon.engine, "recording_status", lambda: next(seq, None))
        stops = []
        monkeypatch.setattr(daemon, "stop", lambda: stops.append(True))
        clock = iter(range(0, 1000))
        monkeypatch.setattr("zephvox.web.daemon.time.monotonic", lambda: next(clock))
        daemon._stop = fake_stop(*([False] * len(statuses) + [True]))
        daemon._tick()
        return stops

    def status(self, level, elapsed=1.0, mode="practice"):
        return {"level": level, "elapsed": elapsed, "mode": mode}

    def test_auto_stop_after_speech_then_silence(self, daemon, monkeypatch):
        statuses = [self.status(0.1), self.status(0.7), self.status(0.1), self.status(0.1), self.status(0.1)]
        assert self.run_ticks(daemon, statuses, monkeypatch) == [True]
        assert len(published(daemon, "level")) == 5

    def test_no_stop_without_speech_or_in_analysis_mode(self, daemon, monkeypatch):
        statuses = [self.status(0.1)] * 3 + [self.status(0.9, mode="analysis"), None]
        assert self.run_ticks(daemon, statuses, monkeypatch) == []

    def test_max_duration_and_stop_errors(self, daemon, monkeypatch):
        monkeypatch.setattr(daemon, "publish", lambda type, **d: daemon.published.append((type, d)))
        assert self.run_ticks(daemon, [self.status(0.1, elapsed=99)], monkeypatch) == [True]

        def failing_status():
            raise OSError("state file vanished")

        monkeypatch.setattr(daemon.engine, "recording_status", failing_status)
        daemon._stop = fake_stop(False, True)
        daemon._tick()  # survives

    def test_auto_stop_error_is_published(self, daemon, monkeypatch):
        stop_real_ticker(daemon)
        seq = iter([self.status(0.1, elapsed=99)])
        monkeypatch.setattr(daemon.engine, "recording_status", lambda: next(seq, None))

        def failing_stop():
            raise ZephvoxError("mic unplugged")

        monkeypatch.setattr(daemon, "stop", failing_stop)
        daemon._stop = fake_stop(False, True)
        daemon._tick()
        assert published(daemon, "action_error")[0]["message"] == "mic unplugged"


class TestEventHub:
    def test_fan_out_overflow_and_dead_loops(self):
        async def main():
            hub = EventHub()
            q1 = hub.subscribe(asyncio.get_running_loop())
            hub.publish("level", {"level": 0.5, "finding": Finding("g", "s", "t", "a", "b", "e", "c")})
            await asyncio.sleep(0)
            msg = json.loads(q1.get_nowait())
            assert msg["type"] == "level" and msg["data"]["finding"]["target"] == "t"
            for i in range(600):  # overflow drops the oldest
                hub._put(q1, str(i))
            assert q1.qsize() == 500 and q1.get_nowait() == "100"
            hub.unsubscribe(q1)
            return hub

        hub = asyncio.run(main())
        dead_loop = asyncio.new_event_loop()
        dead_loop.close()
        hub.subscribe(dead_loop)
        hub.publish("x", {})  # closed loop → subscriber dropped
        assert hub._subscribers == []

    def test_jsonable(self, tmp_path):
        assert jsonable({"p": tmp_path, "t": (1, 2), 3: [tmp_path]}) == {"p": str(tmp_path), "t": [1, 2],
                                                                         "3": [str(tmp_path)]}  # fmt: skip


class TestAPI:
    def test_status_settings_devices(self, api):
        status = api.get("/api/status").json()
        assert status["busy"] is False and status["recording"] is None
        assert "fake-stt" in {p["name"] for p in status["doctor"]["providers"]}
        assert api.get("/api/settings").json()["shortcuts"]["cancel"] == "Ctrl+Shift+Alt+X"
        devices = api.get("/api/devices").json()
        assert devices["provider"] == "fake-capture" and devices["devices"][0]["is_default"] is True
        out = api.put("/api/settings", json={"analyzers": ["grammar"]}).json()
        assert out["analyzers"] == ["grammar"]

    def test_recording_endpoints(self, api, daemon):
        rec = api.post("/api/record/start", json={"mode": "analysis"}).json()
        assert rec["mode"] == "analysis"
        assert api.post("/api/record/start", json={}).status_code == 409
        assert api.get("/api/status").json()["recording"]["level"] == 0.25
        api.post("/api/record/cancel")
        assert api.post("/api/record/stop").status_code == 409
        api.post("/api/record/toggle", json={"analyzers": ["pronunciation"]})
        api.post("/api/record/toggle", json={})
        wait_for_jobs(daemon)
        sessions = api.get("/api/sessions").json()
        assert len(sessions) == 1 and sessions[0]["counts"] == {"pronunciation": 1}

    def test_sessions_detail_audio_feedback_delete(self, api, daemon, wav_file):
        result = daemon.engine.analyze(wav_file)
        sid = result.session_id
        daemon.engine.sessions.create()  # unanalyzed session: hidden from the list, 404 in detail
        listed = api.get("/api/sessions").json()
        assert [s["id"] for s in listed] == [sid] and listed[0]["preview"].startswith("I think")
        detail = api.get(f"/api/sessions/{sid}").json()
        assert len(detail["result"]["findings"]) == 2 and detail["feedback"] == {}
        clip = api.get(f"/api/sessions/{sid}/audio", params={"start": 0.2, "end": 0.6})
        assert clip.headers["content-type"] == "audio/wav" and len(clip.content) == 44 + int(0.7 * 16000) * 2
        assert len(api.get(f"/api/sessions/{sid}/audio").content) == 44 + 3 * 16000 * 2
        assert api.post(f"/api/sessions/{sid}/findings/0/feedback", json={}).json() == {"0": "dismissed"}
        assert api.get("/api/sessions").json()[0]["counts"] == {"grammar": 1}
        assert api.post(f"/api/sessions/{sid}/findings/9/feedback", json={}).status_code == 404
        assert api.get("/api/history").json()["dismissed"] == 1
        unanalyzed = [s.id for s in daemon.engine.sessions.list() if s.id != sid][0]
        assert api.get(f"/api/sessions/{unanalyzed}").status_code == 404
        assert api.get("/api/sessions/nope").status_code == 404
        assert api.delete(f"/api/sessions/{sid}").json() == {"deleted": sid}
        assert api.get(f"/api/sessions/{sid}").status_code == 404

    def test_a_failed_analysis_is_kept_and_can_run_again(self, api, daemon):
        # e.g. the profile's grammar model isn't downloaded and grammar is the only check
        daemon.engine.config.providers["llm"] = ["missing"]
        api.post("/api/record/toggle", json={"analyzers": ["grammar"]})
        api.post("/api/record/toggle", json={})
        wait_for_jobs(daemon)
        assert "No usable llm provider" in published(daemon, "job_error")[0]["message"]
        [listed] = api.get("/api/sessions").json()
        assert listed["unanalyzed"] is True and listed["duration"] > 0 and listed["counts"] == {}

        daemon.engine.config.providers["llm"] = ["fake-llm"]
        daemon.engine._providers.pop("llm", None)
        assert api.post(f"/api/sessions/{listed['id']}/analyze", json={"analyzers": ["grammar"]}).json() == {"queued": True}
        wait_for_jobs(daemon)
        [again] = api.get("/api/sessions").json()
        assert again["id"] == listed["id"] and "unanalyzed" not in again

    def test_upload_analysis(self, api, daemon, wav_file):
        resp = api.post("/api/analyze", files={"file": ("talk.wav", wav_file.read_bytes())},
                        data={"analyzers": "grammar"})  # fmt: skip
        assert resp.json() == {"queued": True}
        wait_for_jobs(daemon)
        assert published(daemon, "analysis_done")

    def test_practice_endpoints(self, api, daemon):
        assert api.put("/api/practice/target", json={"text": "think"}).json()["target"]["text"] == "think"
        assert api.get("/api/practice/attempts/latest/audio", params={"target": "think"}).status_code == 404
        api.post("/api/record/toggle", json={"mode": "practice"})
        api.post("/api/record/toggle", json={"mode": "practice"})
        wait_for_jobs(daemon)
        stats = api.get("/api/practice/stats", params={"target": "think"}).json()
        assert stats["attempts"] == 1
        latest = api.get("/api/practice/attempts/latest/audio", params={"target": "think"})
        assert latest.status_code == 200 and latest.content[:4] == b"RIFF"
        assert api.get("/api/practice/weak").json() == []

    def test_tts(self, api, daemon):
        FakeTTS.calls = 0
        assert api.get("/api/tts", params={"text": "think"}).content == b"RIFFthink"
        api.get("/api/tts", params={"text": "think"})
        assert FakeTTS.calls == 1  # cached
        assert api.get("/api/tts", params={"text": " "}).status_code == 400
        assert api.get("/api/tts", params={"text": "boom"}).status_code == 502
        daemon.engine.release("tts")
        daemon.engine.config.providers["tts"] = []
        assert api.get("/api/tts", params={"text": "other"}).status_code == 404

    def test_events_stream(self, daemon):
        app = create_app(daemon)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/events")

        async def main():
            checks = iter([False, False, True])

            class FakeRequest:
                async def is_disconnected(self):
                    return next(checks)

            response = await endpoint(FakeRequest())
            chunks = []
            daemon.publish("hello", n=1)
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                if len(chunks) == 1:
                    daemon.publish("hello", n=1)
            return chunks

        chunks = asyncio.run(main())
        assert chunks[0] == "retry: 2000\n\n"
        assert json.loads(chunks[1].removeprefix("data: "))["type"] == "hello"
        assert daemon.hub._subscribers == []

    def test_events_keepalive(self, daemon, monkeypatch):
        monkeypatch.setattr("zephvox.web.app.KEEPALIVE_S", 0.01)
        app = create_app(daemon)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/events")

        async def main():
            checks = iter([False, True])

            class FakeRequest:
                async def is_disconnected(self):
                    return next(checks)

            response = await endpoint(FakeRequest())
            return [c async for c in response.body_iterator]

        assert asyncio.run(main())[1] == ": keepalive\n\n"

    def test_frontend_or_placeholder(self, daemon, monkeypatch, tmp_path):
        monkeypatch.setattr("zephvox.web.app.STATIC_DIR", tmp_path)
        assert "hasn't been built" in TestClient(create_app(daemon)).get("/").text
        (tmp_path / "index.html").write_text("<h1>zephvox app</h1>")
        assert "zephvox app" in TestClient(create_app(daemon)).get("/").text


URL = "http://127.0.0.1:8765"


class TestClientAndCLI:
    def test_daemon_file_and_discovery(self, tmp_path):
        assert web_client.find_daemon(tmp_path) is None
        web_client.write_daemon_file(tmp_path, URL)
        with respx.mock:
            respx.get(f"{URL}/api/settings").respond(json={})
            assert web_client.find_daemon(tmp_path) == URL
            respx.get(f"{URL}/api/settings").mock(side_effect=httpx.ConnectError("down"))
            assert web_client.find_daemon(tmp_path) is None
        web_client.remove_daemon_file(tmp_path)
        assert not web_client.daemon_file(tmp_path).exists()

    @respx.mock
    def test_call_errors(self):
        respx.post(f"{URL}/api/x").respond(409, json={"detail": "Already recording"})
        with pytest.raises(web_client.DaemonError, match="Already recording"):
            web_client.call(URL, "POST", "/api/x")
        respx.post(f"{URL}/api/y").respond(500, text="oops")
        with pytest.raises(web_client.DaemonError, match="oops"):
            web_client.call(URL, "POST", "/api/y")

    @pytest.fixture
    def run(self, engine, capsys, monkeypatch):
        def go(*argv, daemon_url=URL, ask=lambda p: ""):
            monkeypatch.setattr(cli, "find_daemon", lambda data_dir: daemon_url)
            code = cli.main(list(argv), engine_factory=lambda c: engine, ask=ask)
            out = capsys.readouterr()
            return code, out.out, out.err

        return go

    @respx.mock
    def test_commands_route_to_daemon(self, run):
        status = respx.get(f"{URL}/api/status").respond(json={"recording": None})
        toggle = respx.post(f"{URL}/api/record/toggle").respond(json={})
        start = respx.post(f"{URL}/api/record/start").respond(json={})
        stop = respx.post(f"{URL}/api/record/stop").respond(json={})
        assert run("toggle", "-q")[1].startswith("Recording")
        status.respond(json={"recording": {"mode": "analysis"}})
        assert run("practice", "-q")[1].startswith("Stopped")
        assert json.loads(toggle.calls.last.request.content) == {"mode": "practice"}
        assert "Run `zephvox stop`" in run("start", "-q")[1]
        assert "report will appear" in run("stop", "-q")[1]
        assert "report will appear" in run("record", "--analyzers", "grammar", "-q")[1]
        assert json.loads(start.calls.last.request.content)["analyzers"] == ["grammar"]
        assert stop.call_count == 2
        stop.respond(409, json={"detail": "Not recording"})
        code, _, err = run("stop", "-q")
        assert code == 2 and "Not recording" in err

    def test_standalone_toggle_and_practice(self, run, engine):
        code, out, _ = run("toggle", "-q", daemon_url=None)
        assert code == 0 and "Run `zephvox toggle` again" in out and engine.is_recording()
        code, out, _ = run("toggle", "-q", daemon_url=None)
        assert code == 0 and "Pronunciation (1)" in out and not engine.is_recording()
        code, _, err = run("practice", daemon_url=None)
        assert code == 2 and "needs the daemon" in err

    def test_serve_command(self, run, monkeypatch):
        seen = {}
        monkeypatch.setattr(web_server, "serve", lambda engine, host, port, open_, shortcuts, **_: seen.update(
            host=host, port=port, open=open_, shortcuts=shortcuts))  # fmt: skip
        assert run("serve", "--port", "9000", "--open", "--no-shortcuts", "-q", daemon_url=None)[0] == 0
        assert seen == {"host": "127.0.0.1", "port": 9000, "open": True, "shortcuts": False}

    def test_server_lifecycle(self, engine, registry, monkeypatch):
        registry.register("triggers", "fake-triggers", FakeTriggers)
        engine.config.providers["triggers"] = ["fake-triggers"]
        ran, opened, daemons = {}, [], []

        import uvicorn

        import zephvox.web.app as web_app

        real_create = web_app.create_app
        monkeypatch.setattr(web_app, "create_app", lambda d: (daemons.append(d), real_create(d))[1])

        class FakeWeb:
            def __init__(self, config):
                self.config, self.should_exit = config, False

            def run(self):
                ran.update(host=self.config.host, port=self.config.port)
                assert web_client.daemon_file(engine.config.engine.data_dir).exists()
                daemons[0].request_shutdown()  # what `zephvox quit` (POST /api/quit) ends up calling
                ran["asked_to_stop"] = self.should_exit

        monkeypatch.setattr(uvicorn, "Server", FakeWeb)
        monkeypatch.setattr(web_server.threading, "Timer", lambda d, fn, args: SimpleNamespace(
            start=lambda: opened.append(args[0])))  # fmt: skip
        monkeypatch.setattr(web_server, "daemon_at", lambda url: False)
        monkeypatch.setattr(web_server, "port_free", lambda host, port: True)
        web_server.serve(engine, host="0.0.0.0", port=9001, open_browser=True, shortcuts=False)
        assert ran == {"host": "0.0.0.0", "port": 9001, "asked_to_stop": True} and opened == ["http://127.0.0.1:9001"]
        assert not web_client.daemon_file(engine.config.engine.data_dir).exists()

    def test_serve_hands_over_to_a_running_daemon(self, engine, monkeypatch, capsys):
        # A second copy (the launcher clicked again) must not touch the shortcuts: when it exited,
        # the desktop switched them off for the copy that was already running.
        import uvicorn

        opened = []
        monkeypatch.setattr(uvicorn, "Server", lambda *a, **k: pytest.fail("started a second server"))
        monkeypatch.setattr("zephvox.web.daemon.Daemon", lambda *a, **k: pytest.fail("created a second daemon"))
        monkeypatch.setattr(web_server, "daemon_at", lambda url: url == "http://127.0.0.1:8765")
        monkeypatch.setattr(web_server.webbrowser, "open", opened.append)
        web_client.write_daemon_file(engine.config.engine.data_dir, "http://127.0.0.1:8765")
        web_server.serve(engine, open_browser=True)
        assert opened == ["http://127.0.0.1:8765"] and "already running" in capsys.readouterr().err
        assert web_client.daemon_file(engine.config.engine.data_dir).exists()  # still points at the running one

    def test_serve_explains_a_port_taken_by_something_else(self, engine, monkeypatch):
        monkeypatch.setattr(web_server, "daemon_at", lambda url: False)
        monkeypatch.setattr(web_server, "port_free", lambda host, port: False)
        with pytest.raises(ZephvoxError, match="--port"):
            web_server.serve(engine, port=9002)

    def test_port_free(self):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            assert not web_server.port_free("127.0.0.1", sock.getsockname()[1])


def test_availability_helper_type():
    assert Availability(True).ok


class VoicedTTS(FakeTTS):
    name = "voiced-tts"

    class Settings(FakeTTS.Settings):
        voice: str = "v1"

    def capabilities(self):
        return {"voice": self.settings.voice, "voices": [{"id": "v1", "label": "One"}, {"id": "v2", "label": "Two"}]}

    def synthesize(self, text):
        return f"RIFF{self.settings.voice}:{text}".encode()


def test_voice_choice_changes_tts_audio(daemon, registry):
    registry.register("tts", "voiced-tts", VoicedTTS)
    daemon.engine.config.providers["tts"] = ["voiced-tts"]
    daemon.engine.release("tts")
    api = TestClient(create_app(daemon))
    assert api.get("/api/voices").json() == {"provider": "voiced-tts", "selected": "v1",
                                             "voices": [{"id": "v1", "label": "One"}, {"id": "v2", "label": "Two"}]}  # fmt: skip
    assert api.get("/api/tts", params={"text": "hi"}).content == b"RIFFv1:hi"
    assert api.put("/api/settings", json={"tts": {"provider": "voiced-tts", "voice": "v2"}}).json()["tts"]["voice"] == "v2"
    assert api.get("/api/tts", params={"text": "hi"}).content == b"RIFFv2:hi"  # not the cached v1 audio
    api.put("/api/settings", json={"tts": {"provider": "voiced-tts", "voice": None}})
    assert api.get("/api/voices").json()["selected"] == "v1"  # cleared → driver default
    daemon.engine.config.providers["tts"] = []
    daemon.engine.release("tts")
    assert api.get("/api/voices").status_code == 409


def test_reanalyze_session_in_place(api, daemon, wav_file):
    first = daemon.engine.analyze(wav_file, analyzers=["grammar"])
    assert api.post(f"/api/sessions/{first.session_id}/analyze", json={"analyzers": ["pronunciation"]}).json() == {
        "queued": True}  # fmt: skip
    wait_for_jobs(daemon)
    result = daemon.engine.sessions.load_result(daemon.engine.sessions.get(first.session_id))
    assert result.analyzers == ["pronunciation"] and len(daemon.engine.sessions.list()) == 1
    assert api.post("/api/sessions/nope/analyze", json={}).status_code == 404


class TestProfilesApi:
    @pytest.fixture
    def setup(self, daemon, tmp_path, monkeypatch):
        from zephvox.engine.hardware import GPU, Hardware
        from zephvox.web import daemon as daemon_mod

        path = tmp_path / "config.toml"
        path.write_text(f'[engine]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n')
        daemon.engine.config.source = path
        daemon.__dict__["hardware"] = Hardware("linux", "x86_64", "CPU", 8, 32.0, 100.0,
                                               [GPU("nvidia", "RTX", 12.0, "cuda")])  # fmt: skip
        monkeypatch.setattr(daemon_mod, "project_root", lambda: None)
        return path

    def test_list_and_switch(self, api, daemon, setup):
        info = api.get("/api/profiles").json()
        assert info["current"] is None and info["recommended"] == "standard"
        assert [p["name"] for p in info["profiles"]] == ["large", "standard", "small", "cpu", "cpu-light"]
        assert info["config_path"] == str(setup)
        resp = api.put("/api/profile", json={"name": "small"})
        assert resp.status_code == 200 and resp.json()["current"] == "small"
        assert 'profile = "small"' in setup.read_text()
        assert daemon.engine.config.provider_settings("openai-compat")["model"] == "qwen3:4b"
        assert any(t == "settings_changed" for t, _ in daemon.published)

    def test_unknown_profile_and_busy(self, api, daemon, setup, monkeypatch):
        assert api.put("/api/profile", json={"name": "huge"}).status_code == 409
        monkeypatch.setattr(type(daemon), "busy", property(lambda self: True))
        resp = api.put("/api/profile", json={"name": "cpu"})
        assert resp.status_code == 409 and "finish" in resp.json()["detail"]

    def test_cpu_install_needs_setup_for_a_gpu_profile(self, api, daemon, setup, monkeypatch, tmp_path):
        from zephvox.install.state import InstallState
        from zephvox.web import daemon as daemon_mod

        from zephvox.engine.hardware import GPU, Hardware

        pin_hardware(daemon, Hardware("linux", "x86_64", "Test CPU", 8, 32.0, 100.0, [GPU("nvidia", "RTX", 24.0, "cuda")]))
        InstallState("cpu", ["models", "cpu"]).save(tmp_path)
        monkeypatch.setattr(daemon_mod, "project_root", lambda: tmp_path)
        needs = api.put("/api/profile", json={"name": "large"}).json()["needs"]
        assert any("setup --profile large" in n for n in needs)
        needs = api.put("/api/profile", json={"name": "cpu"}).json()["needs"]
        assert not any("PyTorch" in n for n in needs)


def pin_hardware(daemon, hw):
    """Make the daemon see `hw`. It detects the real machine in a background thread at start-up; let that
    finish first, or it would overwrite the pinned value (and the result would depend on the machine)."""
    for t in threading.enumerate():
        if t.name == "hardware":
            t.join(30)
    daemon.__dict__["hardware"] = hw


def test_profile_preview_changes_nothing_and_status_has_hardware(daemon, monkeypatch, tmp_path):
    from zephvox.engine.hardware import GPU, Hardware
    from zephvox.web import daemon as daemon_mod

    hw = Hardware("linux", "x86_64", "Test CPU", 8, 32.0, 100.0, [GPU("amd", "RX 6800 XT", 16.0, "rocm")])
    pin_hardware(daemon, hw)
    monkeypatch.setattr(daemon_mod, "project_root", lambda: None)
    config_path = tmp_path / "config.toml"
    config_path.write_text('profile = "large"\n')
    daemon.engine.config = daemon.engine.config.model_copy(update={"source": config_path})
    preview = daemon.preview_profile("small")
    assert preview["name"] == "small" and preview["llm_model"] == "qwen3:4b" and preview["accelerator"] == "rocm"
    assert isinstance(preview["needs"], list) and "download_gb" in preview
    assert config_path.read_text() == 'profile = "large"\n'  # only a preview
    info = daemon.status()["hardware"]
    assert info["gpu"] == {"name": "RX 6800 XT", "vram_gb": 16.0, "backend": "rocm"} and info["cores"] == 8


class TestShortcutFeedback:
    def setup(self, daemon):
        notes = []
        daemon.notify = lambda title, body="": notes.append((title, body))
        return notes

    def test_practice_shortcut_picks_a_word_and_reports_the_score(self, daemon, wav_file):
        notes = self.setup(daemon)
        daemon.engine.analyze(wav_file)  # "think" was mispronounced: it becomes the top weak word
        assert daemon.practice_target is None
        daemon.fire("practice_attempt")
        assert daemon.practice_target["text"] == "think"
        assert notes[-1] == ("Say “think”", "It stops by itself when you stop talking.")
        daemon.fire("practice_attempt")  # stop: scored in the background
        daemon._worker.submit(lambda: None).result(timeout=10)
        title, _ = notes[-1]
        assert title.startswith("“think”:")
        # remembered across restarts
        assert Daemon(daemon.engine, start_triggers=False).practice_target["text"] == "think"

    def test_without_any_word_it_says_so(self, daemon):
        notes = self.setup(daemon)
        daemon.fire("practice_attempt")
        assert notes == [("zephvox", "Pick a word to practice first, in zephvox's Practice page")]
        assert daemon.engine.recording_status() is None

    def test_analysis_shortcut_announces_the_report(self, daemon):
        notes = self.setup(daemon)
        daemon.fire("toggle_recording")
        assert notes[-1][0] == "Recording" and "Ctrl+Shift+Alt+R" in notes[-1][1]
        daemon.fire("toggle_recording")
        daemon._worker.submit(lambda: None).result(timeout=10)
        assert notes[-2][0] == "Analyzing your recording…"
        assert notes[-1][0] == "Your report is ready" and "pronunciation" in notes[-1][1]

    def test_clicks_in_the_page_stay_quiet(self, daemon, wav_file):
        notes = self.setup(daemon)
        daemon.engine.analyze(wav_file)
        daemon.set_practice_target("think")
        daemon.start("practice")
        daemon.stop()
        daemon._worker.submit(lambda: None).result(timeout=10)
        assert notes == []


def test_notifier_commands(tmp_path, monkeypatch):
    from zephvox.web import notify

    ran = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    notify.Notifier(runner=lambda cmd, **kw: ran.append(cmd), platform="linux")("Title", "Body")
    assert ran[-1][0] == "/usr/bin/notify-send" and ran[-1][-2:] == ["Title", "Body"]
    assert notify.Notifier(platform="darwin").command('Say "x"', "b")[2] == 'display notification "b" with title "Say \\"x\\""'
    assert "ShowBalloonTip" in notify.Notifier(platform="win32").command("T", "it's")[-1]
    notify.Notifier(enabled=False, runner=lambda cmd, **kw: ran.append(cmd), platform="linux")("x")
    assert len(ran) == 1
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    assert notify.Notifier(platform="linux").command("t", "b") is None


class TestConfigApi:
    """Every option in the Settings page: described, changed, reset and validated."""

    def test_describes_every_option_with_where_it_comes_from(self, api, daemon):
        info = api.get("/api/config").json()
        stt = info["providers"]["stt"]
        assert stt["order"] == ["broken-stt", "fake-stt"]
        broken = next(d for d in stt["drivers"] if d["name"] == "broken-stt")
        assert broken["available"] is False and broken["reason"]
        assert {f["key"] for f in info["engine"]["fields"]} >= {"analyzers", "notifications", "practice_max_s"}
        assert "data_dir" not in {f["key"] for f in info["engine"]["fields"]}  # not from the page
        assert info["shortcuts"]["values"]["cancel"] == "Ctrl+Shift+Alt+X"
        assert info["overlay"] == {} and "server" in info and "limits" in info["server"]

    def test_change_reset_and_validation(self, api, daemon):
        put = lambda *changes: api.put("/api/config", json={"changes": list(changes)})  # noqa: E731
        out = put({"path": ["engine", "practice_max_s"], "value": 6.5}, {"path": ["providers", "stt"], "value": ["fake-stt"]})
        assert out.status_code == 200
        assert daemon.engine.config.engine.practice_max_s == 6.5 and daemon.engine.config.providers["stt"] == ["fake-stt"]
        assert out.json()["overlay"] == {"engine": {"practice_max_s": 6.5}, "providers": {"stt": ["fake-stt"]}}
        # A wrong type is refused and nothing is saved.
        bad = put({"path": ["engine", "practice_max_s"], "value": "soon"})
        assert bad.status_code == 409 and daemon.engine.config.engine.practice_max_s == 6.5
        assert put({"path": ["provider", "fake-capture", "nope"], "value": 1}).status_code == 409
        assert put({"path": ["engine", "data_dir"], "value": "/tmp/x"}).status_code == 409
        assert put({"path": ["engine", "analyzers"], "value": []}).json()["detail"] == "At least one check must stay on."
        assert put({"path": ["shortcuts", "bogus"], "value": "Ctrl+B"}).status_code == 409
        # Reset: back to what the config gives without the page.
        out = put({"path": ["engine", "practice_max_s"], "reset": True}, {"path": ["providers", "stt"], "reset": True})
        assert out.json()["overlay"] == {} and daemon.engine.config.engine.practice_max_s == 10.0

    def test_changing_a_driver_option_restarts_that_driver(self, api, daemon):
        daemon.engine.provider("capture")
        api.put("/api/config", json={"changes": [{"path": ["provider", "fake-capture", "device"], "value": "mic-2"}]})
        assert "capture" not in daemon.engine._providers
        assert daemon.settings()["capture"] == {"provider": "fake-capture", "device": "mic-2"}

    def test_keys_change_in_place_when_the_driver_can(self, api, daemon, monkeypatch):
        daemon._start_triggers()
        first = FakeTriggers.instance
        rebound = []
        monkeypatch.setattr(first, "rebind", lambda b: rebound.append(b) or True, raising=False)
        monkeypatch.setattr(first, "current_bindings", lambda actions: {"cancel": "Ctrl+Alt+Z"}, raising=False)
        api.put("/api/config", json={"changes": [{"path": ["shortcuts", "cancel"], "value": "Ctrl+Alt+Z"}]})
        assert rebound == [{"cancel": "Ctrl+Alt+Z"}] and FakeTriggers.instance is first  # not restarted
        assert api.get("/api/status").json()["triggers"]["bindings"]["cancel"] == "Ctrl+Alt+Z"
        assert api.get("/api/config").json()["shortcuts"]["values"]["cancel"] == "Ctrl+Alt+Z"

    def test_new_shortcuts_are_registered_again(self, api, daemon):
        daemon._start_triggers()
        first = FakeTriggers.instance
        api.put("/api/config", json={"changes": [{"path": ["shortcuts", "cancel"], "value": "Ctrl+Alt+Q"}]})
        for _ in range(100):
            if FakeTriggers.instance is not first:
                break
            time.sleep(0.01)
        assert FakeTriggers.instance.bindings["cancel"] == "Ctrl+Alt+Q"

    def test_old_settings_keys_fold_into_the_overlay(self, daemon):
        from zephvox.engine.config import load_user_settings, save_user_settings

        data_dir = daemon.engine.config.engine.data_dir
        save_user_settings(data_dir, {"capture": {"provider": "fake-capture", "device": "mic-9"}, "practice_target": {"text": "x"}})
        daemon.update_config([{"path": ["engine", "notifications"], "value": True}])
        stored = load_user_settings(data_dir)
        assert "capture" not in stored and stored["practice_target"] == {"text": "x"}
        assert stored["config"]["provider"]["fake-capture"] == {"device": "mic-9"}
        assert daemon.notify.enabled is True

    def test_docs_come_from_the_example_config(self):
        from zephvox.engine.config import project_root
        from zephvox.engine.editable import docs_from_example

        docs = docs_from_example(project_root() / "config.example.toml")
        assert "GPU" in docs["provider.hf-whisper.device"] and "stricter" in docs["analyzer.pronunciation.threshold"]

    @pytest.mark.skipif(sys.platform == "win32", reason="the fake ollama is a /bin/sh script")
    def test_model_download_reports_progress(self, api, daemon, monkeypatch, tmp_path):
        from zephvox.install import ollama

        fake = tmp_path / "ollama"
        fake.write_text("#!/bin/sh\nprintf 'pulling 10%%\\rpulling 60%%\\rpulling 100%%\\nsuccess\\n'\n")
        fake.chmod(0o755)
        monkeypatch.setattr(ollama, "find", lambda: str(fake))
        monkeypatch.setattr(ollama, "serving", lambda binary: contextlib.nullcontext())
        assert api.post("/api/models/pull", json={"name": "qwen3:14b"}).json()["running"] is True
        for _ in range(200):
            if not daemon.downloads["qwen3:14b"]["running"]:
                break
            time.sleep(0.01)
        assert daemon.downloads["qwen3:14b"]["ok"] is True and daemon.downloads["qwen3:14b"]["percent"] == 100
        assert api.post("/api/models/pull", json={"name": "bad name; rm"}).status_code == 409
        monkeypatch.setattr(ollama, "find", lambda: None)
        assert "isn't installed" in api.post("/api/models/pull", json={"name": "qwen3:4b"}).json()["detail"]


class TestSharing:
    """Share this computer: the server inside the app, and its admin, from the Settings page."""

    @pytest.fixture
    def port(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_share_admin_and_stop(self, api, daemon, port):
        daemon.engine.config.server.port = port
        out = api.put("/api/sharing", json={"on": True}).json()
        assert out["running"] and out["urls"] == [f"http://127.0.0.1:{port}"] and out["enabled"]
        assert httpx.get(f"http://127.0.0.1:{port}/v1/health").json()["service"] == "zephvox"

        added = api.post("/api/sharing/add_account", json={"name": "alice"}).json()
        code = added["invite"]["code"]
        assert code.startswith("inv_") and [a["name"] for a in added["accounts"]] == ["alice"]
        key = httpx.post(f"http://127.0.0.1:{port}/v1/devices/redeem", json={"invite": code, "device_name": "laptop"}).json()["key"]
        info = api.get("/api/sharing").json()
        [device] = info["accounts"][0]["devices"]
        assert device["name"] == "laptop" and device["revoked"] is None

        api.post("/api/sharing/set_limits", json={"account": "alice", "changes": {"max_jobs": 5}})
        api.post("/api/sharing/set_limits", json={"account": None, "changes": {"storage_mb": 100}})
        info = api.get("/api/sharing").json()
        assert info["accounts"][0]["limits"]["max_jobs"] == 5 and info["accounts"][0]["overrides"] == {"max_jobs": 5}
        assert info["defaults"]["limits"]["storage_mb"] == 100 and info["accounts"][0]["limits"]["storage_mb"] == 100
        assert api.post("/api/sharing/set_limits", json={"changes": {"bogus": 1}}).status_code == 409
        old = info["accounts"][0]["webhook_secret"]
        assert api.post("/api/sharing/rotate_secret", json={"account": "alice"}).json()["accounts"][0]["webhook_secret"] != old
        api.post("/api/sharing/revoke_device", json={"device": device["id"]})
        assert httpx.get(f"http://127.0.0.1:{port}/v1/account", headers={"Authorization": f"Bearer {key}"}).status_code == 401
        assert api.post("/api/sharing/invite", json={"account": "alice", "days": 0}).status_code == 409
        assert "needs 'account'" in api.post("/api/sharing/invite", json={}).json()["detail"]

        out = api.put("/api/sharing", json={"on": False}).json()
        assert not out["running"] and not out["enabled"]
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1)
        daemon.engine.provider("stt")  # the app's engine still works after the server stopped
        # Admin works with sharing off too (straight on the database), and accounts can be removed.
        assert api.post("/api/sharing/remove_account", json={"account": "alice"}).json()["accounts"] == []

    def test_a_taken_port_is_explained(self, api, daemon, port):
        import socket

        daemon.engine.config.server.port = port
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
            s.listen()
            assert "already in use" in api.put("/api/sharing", json={"on": True}).json()["detail"]

    def test_client_mode_cant_share(self, api, daemon):
        daemon.engine.config.remote.mode = "remote"
        assert "can't share" in api.put("/api/sharing", json={"on": True}).json()["detail"]


def test_recording_while_an_analysis_runs_queues_the_next_one(daemon, api, wav_file):
    # The first analysis is held; the user records again meanwhile. Nothing may block, and the
    # events must say which job is running and how many wait (the page shows that).
    gate = threading.Event()
    real = daemon.backend.analyze

    def slow(source, analyzers=None, **kw):
        gate.wait(5)
        return real(source, analyzers, **kw)

    daemon.backend.analyze = slow
    api.post("/api/record/toggle", json={})  # start
    api.post("/api/record/toggle", json={})  # stop → analysis 1 starts, held
    for _ in range(200):
        if any(t == "job_started" for t, _ in daemon.published):
            break
        time.sleep(0.01)
    assert api.post("/api/record/start", json={}).status_code == 200  # not blocked
    assert api.post("/api/record/stop").status_code == 200  # → analysis 2 queued behind the first
    events = [(t, d.get("pending")) for t, d in daemon.published if t.startswith("job_")]
    assert events == [("job_queued", 1), ("job_started", None), ("job_queued", 2)]
    # The lists show both: the one being analyzed, and the one waiting for its turn.
    listed = api.get("/api/sessions").json()
    assert [("analyzing" if s.get("analyzing") else "queued" if s.get("queued") else "other") for s in listed] == [
        "queued", "analyzing"]  # newest first; neither is reported as "not analyzed"  # fmt: skip
    assert not any(s.get("unanalyzed") for s in listed)
    gate.set()
    wait_for_jobs(daemon)
    after = api.get("/api/sessions").json()
    assert len(after) == 2 and not any(s.get("analyzing") or s.get("queued") or s.get("unanalyzed") for s in after)
    assert daemon.waiting == [] and daemon.analyzing is None
    assert [(t, d.get("pending")) for t, d in daemon.published if t.startswith("job_")][3:] == [
        ("job_finished", 1), ("job_started", None), ("job_finished", 0)]  # fmt: skip
    assert len([t for t, _ in daemon.published if t == "analysis_done"]) == 2


class TestQuit:
    """`zephvox quit`, and what `zephvox update` relies on."""

    def test_the_endpoint_only_obeys_the_command_line(self, api, daemon, monkeypatch):
        stopped = []
        daemon.request_shutdown = lambda: stopped.append(True)
        monkeypatch.setattr("zephvox.web.app.threading.Timer", lambda d, fn: SimpleNamespace(start=fn))
        # Another website can't add a custom header without a preflight the daemon never answers.
        assert api.post("/api/quit").status_code == 403 and stopped == []
        assert api.post("/api/quit", headers={"X-Requested-With": "zephvox"}).json() == {"quitting": True}
        assert stopped == [True]
        daemon.request_shutdown = None
        assert api.post("/api/quit", headers={"X-Requested-With": "zephvox"}).status_code == 409

    @respx.mock
    def test_stop_waits_until_the_daemon_is_gone(self, tmp_path):
        web_client.write_daemon_file(tmp_path, URL)
        respx.get(f"{URL}/api/settings").mock(side_effect=[httpx.Response(200, json={}),   # found
                                                           httpx.Response(200, json={}),   # still up
                                                           httpx.ConnectError("gone")])  # fmt: skip
        quit_call = respx.post(f"{URL}/api/quit").respond(json={"quitting": True})
        assert web_client.stop_daemon(tmp_path, sleep=lambda s: None) == URL
        assert quit_call.calls.last.request.headers["x-requested-with"] == "zephvox"

    @respx.mock
    def test_an_older_daemon_is_ended_by_its_process_id(self, tmp_path):
        web_client.write_daemon_file(tmp_path, URL)
        respx.get(f"{URL}/api/settings").mock(side_effect=[httpx.Response(200, json={}), httpx.ConnectError("gone")])
        respx.post(f"{URL}/api/quit").respond(404, json={"detail": "Not Found"})  # from before quit existed
        respx.get(f"{URL}/api/status").respond(json={"pid": 4242})
        ended = []
        assert web_client.stop_daemon(tmp_path, terminate=ended.append, sleep=lambda s: None) == URL
        assert ended == [4242]

    @respx.mock
    def test_nothing_running_and_a_daemon_that_wont_stop(self, tmp_path):
        assert web_client.stop_daemon(tmp_path) is None
        web_client.write_daemon_file(tmp_path, URL)
        respx.get(f"{URL}/api/settings").respond(json={})  # always up
        respx.post(f"{URL}/api/quit").respond(json={"quitting": True})
        with pytest.raises(web_client.DaemonError, match="didn't stop"):
            web_client.stop_daemon(tmp_path, wait=0.05, sleep=lambda s: None)

    def test_start_detaches_and_waits_for_it(self, tmp_path, monkeypatch):
        launched = []
        answers = iter([None, None, URL])
        monkeypatch.setattr(web_client, "find_daemon", lambda d, t=None: next(answers))
        url = web_client.start_daemon(tmp_path, tmp_path / "data", popen=lambda cmd, **kw: launched.append((cmd, kw)),
                                      sleep=lambda s: None)  # fmt: skip
        assert url == URL and launched[0][0][1:] == ["-m", "zephvox.cli", "serve"]
        assert Path(launched[0][1]["cwd"]).samefile(tmp_path)
        if sys.platform != "win32":  # Windows detaches with creation flags instead
            assert launched[0][1].get("start_new_session") is True

    def test_the_command(self, tmp_path, monkeypatch, capsys):
        engine_config = tmp_path / "c.toml"
        engine_config.write_text(f'[engine]\ndata_dir = "{(tmp_path).as_posix()}"\n')
        calls = []
        monkeypatch.setattr("zephvox.web.client.stop_daemon", lambda d: calls.append(d) or None)
        assert cli.main(["quit", "--config", str(engine_config)]) == 0
        assert "isn't running" in capsys.readouterr().out and calls == [tmp_path]
        monkeypatch.setattr("zephvox.web.client.stop_daemon", lambda d: URL)
        assert cli.main(["quit", "--config", str(engine_config)]) == 0
        assert "stopped" in capsys.readouterr().out


# --- desktop shell contract ---------------------------------------------------------------


def test_healthz_reports_versions(api):
    from zephvox import API_VERSION, __version__

    assert api.get("/healthz").json() == {"ok": True, "engine_version": __version__, "api_version": API_VERSION}


def test_sidecar_prints_ready_line_and_exits_when_stdin_closes(tmp_path):
    import json
    import subprocess
    import sys

    from zephvox.web.server import READY_PREFIX

    env = {**__import__("os").environ, "ZEPHVOX_HOME": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "zephvox.cli", "serve", "--port", "0", "--no-shortcuts", "--print-ready",
         "--exit-on-stdin-close"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)  # fmt: skip
    try:
        line = proc.stdout.readline()
        assert line.startswith(READY_PREFIX)
        info = json.loads(line[len(READY_PREFIX):])
        assert info["port"] > 0 and info["url"].endswith(str(info["port"]))
        proc.stdin.close()  # the parent went away
        assert proc.wait(timeout=20) == 0
    finally:
        proc.kill()


def test_model_download_endpoints(api, daemon):
    assert api.get("/api/models/downloads").json()["running"] is False
    daemon.models.start = lambda path: {"running": True}  # no real downloader in tests
    assert api.post("/api/models/downloads").json() == {"running": True}
    assert api.delete("/api/models/downloads").status_code == 200
