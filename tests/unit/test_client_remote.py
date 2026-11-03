"""Client mode end to end, in process: a daemon on "this computer" analyzing on a real server app."""

import importlib.util
import os
import shutil
import stat
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zephvox import cli
from zephvox.client import credentials
from zephvox.client import remote as remote_mod
from zephvox.client.backend import LocalBackend, RemoteBackend, make_backend
from zephvox.client.remote import PracticeStream, expand_headers, is_insecure, read_pcm
from zephvox.engine import audio
from zephvox.engine.config import Config, EngineSettings, ServerSettings, load_user_settings
from zephvox.engine.engine import Engine
from zephvox.engine.errors import ProviderUnavailable, ZephvoxError
from zephvox.install import wizard
from zephvox.install.state import InstallState, extras_for
from zephvox.server.app import create_server_app
from zephvox.server.service import Server
from zephvox.web.daemon import Daemon
from tests.unit.test_install import Recorder, install_root, setup_args  # noqa: F401  (fixture and helpers)
from tests.unit.test_web import FakeTTS

URL = "http://testserver"


class WSAdapter:
    """A Starlette test WebSocket with the `websockets` sync client's methods."""

    def __init__(self, session):
        self.session = session

    def send(self, data):
        (self.session.send_bytes if isinstance(data, bytes) else self.session.send_text)(data)

    def recv(self, timeout=None):
        return self.session.receive_text()

    def close(self):
        self.session.close()


@pytest.fixture
def remote(engine, registry, caps, tmp_path, monkeypatch):
    registry.register("tts", "fake-tts", FakeTTS)
    providers = {**engine.config.providers, "tts": ["fake-tts"]}
    srv_engine = Engine(Config(engine=EngineSettings(data_dir=tmp_path / "srvdata"), providers=providers),
                        registry=registry, caps=caps)  # fmt: skip
    server = Server(srv_engine, ServerSettings(data_dir=tmp_path / "srv"))
    tc = TestClient(create_server_app(server))
    original = remote_mod.ServerClient.__init__

    def init(self, url, headers=None, key=None, http=None):
        original(self, url, headers, key, http or tc)

    def connect_ws(self):
        if self._ws is None:
            self._ws = WSAdapter(tc.websocket_connect("/v1/practice", headers=self.client._headers()).__enter__())
        return self._ws

    monkeypatch.setattr(remote_mod.ServerClient, "__init__", init)
    monkeypatch.setattr(PracticeStream, "_connect", connect_ws)
    account = server.db.add_account("alice", server.clock())
    invite = lambda: server.db.add_invite(account.id, server.clock(), 3600)  # noqa: E731
    yield SimpleNamespace(server=server, tc=tc, invite=invite, account=account)
    server.close()


@pytest.fixture
def client_daemon(engine, remote):
    engine.config.providers["stt"] = []  # nothing runs here: analysis must go to the server
    d = Daemon(engine, start_triggers=False)
    d.published = []
    original = d.hub.publish
    d.hub.publish = lambda type, data: (d.published.append((type, data)), original(type, data))
    yield d
    d.close()


def connected(daemon, remote):
    return daemon.connect(URL, remote.invite(), "laptop", {"X-Proxy": "${ZEPHVOX_TEST_TOKEN}"})


class TestConnect:
    def test_invite_key_file_and_settings(self, client_daemon, remote, engine, monkeypatch):
        monkeypatch.setenv("ZEPHVOX_TEST_TOKEN", "t0k")
        info = connected(client_daemon, remote)
        assert info["mode"] == "remote" and info["connected"] and info["account"] == "alice"
        assert info["reachable"] and info["headers"] == ["X-Proxy"] and info["device"] == "laptop"
        path = engine.config.engine.data_dir / credentials.FILE
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        creds = credentials.load(engine.config.engine.data_dir, URL + "/")
        assert creds.key.startswith("sk_") and remote.server.db.device_for_key(creds.key)
        assert credentials.load(engine.config.engine.data_dir, "http://other") is None
        stored = load_user_settings(engine.config.engine.data_dir)["remote"]
        assert stored["url"] == URL and stored["headers"] == {"X-Proxy": "${ZEPHVOX_TEST_TOKEN}"}  # not expanded
        assert isinstance(client_daemon.backend, RemoteBackend)
        assert client_daemon.backend.client.headers["X-Proxy"] == "t0k"

    def test_rotate_and_forget(self, client_daemon, remote, engine, monkeypatch):
        connected(client_daemon, remote)
        data_dir = engine.config.engine.data_dir
        old = credentials.load(data_dir).key
        client_daemon.rotate_key()
        new = credentials.load(data_dir).key
        assert new != old and remote.server.db.device_for_key(old) is None and remote.server.db.device_for_key(new)
        monkeypatch.setattr(client_daemon, "engine_installed", lambda: True)
        info = client_daemon.forget_key()
        assert not info["connected"] and info["mode"] == "local" and credentials.load(data_dir) is None
        assert remote.server.db.device_for_key(new) is None  # revoked on the server too

    def test_mode_rules(self, client_daemon, remote, monkeypatch):
        monkeypatch.setattr(client_daemon, "engine_installed", lambda: False)
        with pytest.raises(ZephvoxError, match="isn't installed"):
            client_daemon.set_remote({"mode": "local"})
        with pytest.raises(ZephvoxError, match="address"):
            client_daemon.set_remote({"mode": "remote"})
        client_daemon.set_remote({"mode": "remote", "url": URL + "/"})
        with pytest.raises(ProviderUnavailable, match="redeem an invite"):
            client_daemon.backend  # noqa: B018

    def test_bad_invite(self, client_daemon, remote):
        with pytest.raises(ZephvoxError, match="403"):
            client_daemon.connect(URL, "inv_wrong", "laptop")


class TestRemoteAnalysis:
    def test_recording_is_analyzed_on_the_server(self, client_daemon, remote, engine):
        connected(client_daemon, remote)
        recorded = []
        engine.events.subscribe(recorded.append)
        client_daemon.start("analysis", ["pronunciation"])
        client_daemon.stop()
        client_daemon._worker.submit(lambda: None).result(timeout=10)  # wait for the job
        session = engine.sessions.list()[-1]
        result = engine.sessions.load_result(session)
        assert result.session_id == session.id and result.audio_path == str(session.audio_path)
        assert result.findings[0].target == "think" and result.providers["server"] == URL
        types = [e.type for e in recorded]
        assert "stage_started" in types and any(e.data.get("stage") == "upload" for e in recorded)
        assert ("analysis_done", {"session_id": session.id}) in client_daemon.published

    def test_cli_analyze_uses_the_server(self, client_daemon, remote, engine, wav_file, capsys):
        connected(client_daemon, remote)
        args = cli._parser().parse_args(["analyze", str(wav_file), "--analyzers", "pronunciation", "--format", "json"])
        assert cli.run(args, engine, input) == 0
        assert '"target": "think"' in capsys.readouterr().out

    def test_failed_job_raises(self, client_daemon, remote, engine, wav_file):
        connected(client_daemon, remote)
        remote.server.engine.config.providers["stt"] = []
        with pytest.raises(ZephvoxError, match="couldn't analyze"):
            client_daemon.backend.analyze(wav_file, ["pronunciation"])

    def test_local_mode_uses_the_engine(self, engine):
        assert isinstance(make_backend(engine), LocalBackend)


class TestRemotePractice:
    def test_streamed_while_recording(self, client_daemon, remote, engine, tmp_path):
        connected(client_daemon, remote)
        backend = client_daemon.backend
        path = tmp_path / "attempt.wav"
        with wave.open(str(path), "wb") as w:  # a recording still being written
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            backend.practice_begin("think")
            w.writeframes(np.zeros(8000, dtype="<i2").tobytes())
            w._file.flush()
            backend.practice_feed(path)
            assert backend._stream._sent == 12800  # two whole chunks went out while "speaking"
            w.writeframes(np.zeros(8000, dtype="<i2").tobytes())
        recording = SimpleNamespace(path=str(path))
        attempt = backend.practice("think", recording, "sess-1")
        assert attempt.target == "think" and attempt.audio == str(path) and attempt.source_session == "sess-1"
        assert engine.practice_store.attempts("think")[-1].score == attempt.score

    def test_broken_stream_sends_everything_at_the_end(self, client_daemon, remote, engine, wav_file):
        connected(client_daemon, remote)
        backend = client_daemon.backend
        backend.practice_begin("think")
        backend._stream._broken = True  # e.g. the connection dropped while speaking
        attempt = backend.practice("think", SimpleNamespace(path=str(wav_file)), None)
        assert attempt.target == "think"

    def test_tts_and_voices(self, client_daemon, remote):
        connected(client_daemon, remote)
        FakeTTS.calls = 0
        assert client_daemon.backend.tts("hello") == b"RIFFhello"
        client_daemon.backend.tts("hello")
        assert FakeTTS.calls == 1  # cached on both sides
        assert client_daemon.backend.voices()["provider"] == "server"


class TestInstallFromGui:
    def test_install_streams_output_and_switches_to_local(self, client_daemon, remote, monkeypatch):
        connected(client_daemon, remote)
        seen = {}

        class FakeProc:
            def __init__(self, command, **kw):
                seen["command"] = command
                self.stdout = iter(["[1/7] Your machine\n", "Done.\n"])

            def wait(self):
                return 0

        monkeypatch.setattr(client_daemon, "engine_installed", lambda: True)
        monkeypatch.setattr("zephvox.install.ollama.find", lambda: None)
        client_daemon.start_install(runner=FakeProc)
        for _ in range(200):
            if not client_daemon.install["running"]:
                break
            __import__("time").sleep(0.01)
        assert client_daemon.install["ok"] and "--role" in seen["command"] and "--no-ollama" in seen["command"]
        assert ("install_output", {"line": "Done."}) in client_daemon.published
        assert client_daemon.engine.config.remote.mode == "local"


class TestHelpers:
    def test_headers_and_urls(self):
        assert expand_headers({"A": "Bearer ${TOK}", "B": "x"}, {"TOK": "abc"}) == {"A": "Bearer abc", "B": "x"}
        assert is_insecure("http://speak.example.com") and not is_insecure("https://speak.example.com")
        assert not is_insecure("http://127.0.0.1:8770")

    def test_read_pcm(self, tmp_path):
        path = tmp_path / "a.wav"
        audio.write_wav(path, np.zeros(1600, dtype=np.float32))
        assert len(read_pcm(path)) == 3200
        if not (importlib.util.find_spec("av") or shutil.which("ffmpeg")):
            return  # resampling needs the models extra (av) or ffmpeg, not installed on every machine
        other = tmp_path / "24k.wav"
        with wave.open(str(other), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(np.zeros(2400, dtype="<i2").tobytes())
        assert read_pcm(other) == b""  # not streamable as is
        assert len(remote_mod.pcm_16k(other)) == 3200  # converted to 16 kHz instead
        (tmp_path / "b.wav").write_bytes(b"junk")
        assert read_pcm(tmp_path / "b.wav") == b"" and read_pcm(tmp_path / "missing.wav") == b""

    def test_extras_per_role(self):
        assert extras_for("cuda", "client") == ["desktop", "web"]
        assert extras_for("rocm") == ["models", "rocm", "desktop", "web", "voice"]


class TestClientSetup:
    def test_client_role(self, install_root, remote, tmp_path, monkeypatch):  # noqa: F811  (fixture)
        runner = Recorder()
        code = remote.invite()
        args = setup_args(tmp_path, role="client", server_url=URL, invite=code, device_name="pc",
                          server_header=["X-Org=acme"])  # fmt: skip
        setup = wizard.Setup(args, wizard.Console(interactive=False, out=lambda *a: None), runner=runner,
                             detect=lambda root: None)  # fmt: skip
        assert setup.run_all() == 0
        state = InstallState.load(install_root.path)
        assert state.role == "client" and state.extras == ["desktop", "web"]
        assert runner.ran("--extra desktop") and not runner.ran("--extra models")
        data_dir = tmp_path / "data"
        settings = load_user_settings(data_dir)["remote"]
        assert settings == {"mode": "remote", "url": URL, "headers": {"X-Org": "acme"}, "device_name": "pc"}
        assert credentials.load(data_dir, URL).account == "alice"

    def test_client_role_needs_an_invite(self, install_root, remote, tmp_path):  # noqa: F811  (fixture)
        args = setup_args(tmp_path, role="client", server_url=URL)
        setup = wizard.Setup(args, wizard.Console(interactive=False, out=lambda *a: None), runner=Recorder(),
                             detect=lambda root: None)  # fmt: skip
        with pytest.raises(ZephvoxError, match="invite code is needed"):
            setup.run_all()


class TestServerChecks:
    def test_checks_the_server_cant_run_are_skipped(self, client_daemon, remote, engine, wav_file):
        connected(client_daemon, remote)
        remote.server.engine.config.providers["llm"] = []  # this server has no language model
        remote.server._runnable = None  # (the server re-checks once a minute)
        health = remote.tc.get("/v1/health").json()
        assert "grammar" in health["installed"] and "grammar" not in health["analyzers"]
        warnings = []
        engine.events.subscribe(lambda e: e.type == "warning" and warnings.append(e.data["message"]))
        result = client_daemon.backend.analyze(wav_file, ["pronunciation", "grammar"])
        assert result.analyzers == ["pronunciation"]
        assert warnings[0] == "Grammar isn't available on this server right now, so it was skipped."
        with pytest.raises(ZephvoxError, match="can't run any"):
            client_daemon.backend.analyze(wav_file, ["grammar"])
