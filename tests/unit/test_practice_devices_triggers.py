"""Practice scoring, capture devices/levels, TTS, shortcut drivers, GUI settings."""

import asyncio
import subprocess
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from zephvox.analyzers.base import AnalysisContext
from zephvox.analyzers.pronunciation.hints import tip
from zephvox.analyzers.pronunciation.align import align
from zephvox.analyzers.pronunciation.practice import PracticeAnalyzer, phone_status
from zephvox.engine import audio
from zephvox.engine.capabilities import SystemCapabilities
from zephvox.engine.config import Config, apply_user_settings, load_config, load_user_settings, save_user_settings
from zephvox.engine.errors import ConfigError, RecordingError, ZephvoxError
from zephvox.engine.models import PhoneSeq, Pronunciation
from zephvox.engine.practice import PracticeAttempt, PracticeStore
from zephvox.engine.text import transcript_from_text
from zephvox.providers.base import Services
from zephvox.providers.capture.pipewire import PipeWireCapture, parse_pw_dump
from zephvox.providers.capture.sounddevice import SoundDeviceCapture
from zephvox.providers.triggers import portal, pynput
from zephvox.providers.triggers.portal import IFACE, PortalSession, PortalTriggers, to_portal_trigger
from zephvox.providers.triggers.pynput import PynputTriggers, to_pynput_hotkey
from zephvox.providers.tts.espeak import EspeakTTS

THINK = {"think": Pronunciation("think", [["θ", "ɪ", "ŋ", "k"]])}


def practice(text, heard, lexicon=THINK, **settings):
    transcript = transcript_from_text(text, 1.0)
    warnings = []
    values = {"transcript": transcript, "expected_phonemes": lexicon, "heard_phonemes": [PhoneSeq(heard)]}
    ctx = AnalysisContext(values.__getitem__, lambda k: None, warnings.append)
    return PracticeAnalyzer(settings).analyze(ctx), warnings


try:
    from dbus_fast import MessageType, Variant
except ImportError:  # dbus-fast is installed on Linux only
    MessageType = Variant = None


class TestPracticeAnalyzer:
    def test_perfect_attempt_passes(self):
        (f,), _ = practice("think", ["θ", "ɪ", "ŋ", "k"])
        assert f.details["score"] == 100 and f.details["passed"] and f.details["status"] == "ok"
        assert f.category == "ok" and f.explanation == "Well done!" and f.details["tip"] == ""
        assert [p["status"] for p in f.details["phones"]] == ["ok"] * 4

    def test_th_to_s_fails_with_tip_even_if_score_is_decent(self):
        (f,), _ = practice("think", ["s", "ɪ", "ŋ", "k"])
        assert f.details["score"] == 75 and not f.details["passed"]
        assert f.details["phones"][0] == {"expected": "θ", "heard": "s", "status": "wrong"}
        assert "tongue" in f.details["tip"] and f.category == "θ→s"

    def test_no_speech_and_different_word(self):
        (f,), _ = practice("think", [])
        assert f.details["status"] == "no_speech" and f.details["score"] == 0 and "didn't hear" in f.explanation
        (f,), _ = practice("think", ["b", "æ", "d"])
        assert f.details["status"] == "different_word" and "different word" in f.explanation

    def test_phone_statuses(self):
        _, ops = align(["l", "ʌ", "n", "d", "ə", "n"], ["l", "ə", "n", "d", "n", "z"])
        assert [phone_status(o) for o in ops] == ["ok", "minor", "ok", "ok", "minor", "ok", "extra"]
        _, ops = align(["k", "æ", "t"], ["k", "æ"])
        assert phone_status(ops[-1]) == "missing"

    def test_sentence_scores_every_word_and_warns_on_unknown(self):
        lexicon = {**THINK, "so": Pronunciation("so", [["s", "oʊ"]]), "xyzzy": Pronunciation("xyzzy", [])}
        findings, warnings = practice("think so xyzzy", ["θ", "ɪ", "ŋ", "k", "s", "oʊ"], lexicon)
        assert [f.target for f in findings] == ["think", "so"]
        assert all(f.details["passed"] for f in findings)
        assert "xyzzy" in warnings[0]

    def test_tip_none_for_unknown_sound(self):
        _, ops = align(["x"], ["k"])
        assert tip(ops) == ""


class TestPracticeStore:
    def test_log_stats_weak_words(self, tmp_path):
        store = PracticeStore(tmp_path)
        assert store.attempts() == [] and store.stats("think")["best"] is None
        assert store.new_audio_path().parent == tmp_path / "practice"
        store.add(PracticeAttempt("think", 60, False, at="t1"))
        store.add(PracticeAttempt("Think", 90, True, at="t2"))
        store.add(PracticeAttempt("very", 40, False))
        store.log_path.write_text(store.log_path.read_text() + "\n")  # blank lines are ignored
        stats = store.stats("think")
        assert (stats["attempts"], stats["best"], stats["last"], stats["passed"]) == (2, 90, 90, True)
        weak = store.weak_words([("think", 5), ("very", 3), ("go", 1)], limit=1)
        assert weak == [{"target": "very", "mistakes": 3, "best": 40, "attempts": 1}]


class TestSettings:
    def test_user_settings_roundtrip_and_apply(self, tmp_path):
        assert load_user_settings(tmp_path) == {}
        save_user_settings(tmp_path, {"capture": {"provider": "pipewire", "device": "mic-1"}, "analyzers": ["grammar"]})
        config = apply_user_settings(Config(), load_user_settings(tmp_path))
        assert config.provider_settings("pipewire") == {"device": "mic-1"}
        assert config.engine.analyzers == ["grammar"]
        assert apply_user_settings(Config(), {}) == Config()

    def test_load_config_applies_user_settings_and_shortcuts(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text(f'[engine]\ndata_dir = "{(tmp_path).as_posix()}"\n[shortcuts]\ncancel = "Ctrl+Shift+X"\n')
        save_user_settings(tmp_path, {"analyzers": ["pronunciation"]})
        c = load_config(path)
        assert c.shortcuts["cancel"] == "Ctrl+Shift+X" and c.shortcuts["toggle_recording"] == "Ctrl+Shift+Alt+R"
        assert c.engine.analyzers == ["pronunciation"]
        assert "tts" in c.providers and "triggers" in c.providers

    def test_bad_settings_and_shortcut_actions(self, tmp_path):
        (tmp_path / "settings.json").write_text("{nope")
        with pytest.raises(ConfigError, match="Invalid settings"):
            load_user_settings(tmp_path)
        (tmp_path / "settings.json").write_text("[1]")
        assert load_user_settings(tmp_path) == {}
        path = tmp_path / "c.toml"
        path.write_text("[shortcuts]\nexplode = \"Ctrl+E\"\n")
        with pytest.raises(ConfigError, match="Unknown shortcut action"):
            load_config(path)


PW_DUMP = [
    {"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
     "metadata": [{"key": "default.audio.sink", "value": {"name": "out"}},
                  {"key": "default.audio.source", "value": {"name": "usb-mic"}}]},
    {"type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Audio/Source", "node.name": "usb-mic",
                                                           "node.description": "Podcast Microphone"}}},
    {"type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Audio/Source", "node.name": "builtin"}}},
    {"type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Audio/Sink", "node.name": "out"}}},
    {"type": "PipeWire:Interface:Node", "info": None},
]  # fmt: skip


class TestCaptureDevicesAndLevels:
    def test_parse_pw_dump(self):
        devices = parse_pw_dump(PW_DUMP)
        assert [(d.id, d.name, d.is_default) for d in devices] == [
            ("usb-mic", "Podcast Microphone", True), ("builtin", "builtin", False)]  # fmt: skip

    def test_pipewire_list_devices_handles_failure(self, monkeypatch):
        import json as _json

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=_json.dumps(PW_DUMP).encode()))
        assert len(PipeWireCapture().list_devices()) == 2

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("pw-dump", 5)

        monkeypatch.setattr(subprocess, "run", boom)
        assert PipeWireCapture().list_devices() == []

    def test_levels(self, tmp_path):
        assert audio.level(np.zeros(10, dtype=np.float32)) == 0.0
        assert audio.level(np.array([], dtype=np.float32)) == 0.0
        assert audio.level(np.ones(10, dtype=np.float32)) == 1.0
        assert 0.4 < audio.level(np.full(10, 0.03, dtype=np.float32)) < 0.6
        wav = tmp_path / "a.wav"
        audio.write_wav(wav, np.full(16000, 0.5, dtype=np.float32))
        assert PipeWireCapture().level({"path": str(wav)}) == pytest.approx(0.9, abs=0.01)  # -6 dBFS
        header_only = tmp_path / "h.wav"
        header_only.write_bytes(b"\0" * 44)
        assert audio.tail_level(header_only) == 0.0
        assert audio.tail_level(tmp_path / "missing.wav") is None


class FakeStream:
    def __init__(self, callback, **kw):
        self.callback, self.kw = callback, kw
        self.started = self.stopped = self.closed = False

    def start(self):
        self.started = True
        self.callback(np.full((1600, 1), 8000, dtype=np.int16), 1600, None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeSD:
    default = SimpleNamespace(device=(1, 0))

    def __init__(self, devices=None, silent=False):
        self.devices = devices if devices is not None else [
            {"name": "Speakers", "max_input_channels": 0}, {"name": "USB Mic", "max_input_channels": 1}]
        self.silent = silent
        self.streams = []

    def query_devices(self):
        return self.devices

    def InputStream(self, **kw):  # noqa: N802 - mirrors sounddevice
        stream = FakeStream(**kw)
        if self.silent:
            stream.start = lambda: None
        self.streams.append(stream)
        return stream


class TestSoundDevice:
    def test_devices_and_availability(self):
        cap = SoundDeviceCapture(sd=FakeSD())
        assert cap.check_available().ok and cap.capabilities()["detached"] is False
        assert [(d.id, d.is_default) for d in cap.list_devices()] == [("USB Mic", True)]
        ok, reason = SoundDeviceCapture(sd=FakeSD(devices=[])).check_available()
        assert not ok and "no input" in reason

        class Broken:
            def query_devices(self):
                raise OSError("PortAudio library not found")

        ok, reason = SoundDeviceCapture(sd=Broken()).check_available()
        assert not ok and "PortAudio" in reason

    def test_record_level_stop(self, tmp_path):
        sd = FakeSD()
        cap = SoundDeviceCapture({"device": "USB Mic"}, sd=sd)
        handle = cap.start(tmp_path / "a.wav")
        assert sd.streams[0].kw["device"] == "USB Mic" and sd.streams[0].kw["samplerate"] == 16000
        assert cap.level(handle) > 0.5
        ref = cap.stop(handle)
        with wave.open(str(ref.path)) as w:
            assert w.getnframes() == 1600 and w.getframerate() == 16000
        assert sd.streams[0].closed and cap.level(handle) is None
        with pytest.raises(RecordingError, match="another process"):
            cap.stop(handle)

    def test_silent_recording_and_close(self, tmp_path):
        cap = SoundDeviceCapture(sd=FakeSD(silent=True))
        handle = cap.start(tmp_path / "a.wav")
        with pytest.raises(RecordingError, match="Nothing was recorded"):
            cap.stop(handle)
        cap.start(tmp_path / "b.wav")
        cap.close()  # stops and swallows the empty-recording error
        assert cap._active == {}


class TestEspeakTTS:
    def test_synthesize(self, monkeypatch):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout=b"RIFFdata", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        tts = EspeakTTS({"speed": 120}, Services(SystemCapabilities(binaries={"espeak-ng": True})))
        assert tts.check_available().ok and tts.capabilities()["voice"] == "en-us"
        assert tts.synthesize("think") == b"RIFFdata"
        assert seen["cmd"][0].endswith("espeak-ng")  # full path when found (Windows: not on PATH)
        assert seen["cmd"][1:] == ["-v", "en-us", "-s", "120", "--stdout", "think"]

    def test_failure_and_unavailable(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad"))
        with pytest.raises(ZephvoxError, match="espeak-ng failed: bad"):
            EspeakTTS().synthesize("x")
        assert not EspeakTTS(services=Services(SystemCapabilities(binaries={"espeak-ng": False}))).check_available().ok


class TestKeyCombos:
    @pytest.mark.parametrize("combo, portal_form, pynput_form", [
        ("Ctrl+Alt+R", "CTRL+ALT+r", "<ctrl>+<alt>+r"),
        ("Ctrl+Shift+Alt+P", "CTRL+SHIFT+ALT+p", "<ctrl>+<shift>+<alt>+p"),
        ("Super+Shift+F5", "LOGO+SHIFT+F5", "<cmd>+<shift>+<f5>"),
        ("Ctrl + Space", "CTRL+Space", "<ctrl>+<space>"),
    ])  # fmt: skip
    def test_conversion(self, combo, portal_form, pynput_form):
        assert to_portal_trigger(combo) == portal_form
        assert to_pynput_hotkey(combo) == pynput_form

    @pytest.mark.parametrize("bad", ["", "Hyper+R"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            to_portal_trigger(bad)
        with pytest.raises(ValueError):
            to_pynput_hotkey(bad)


class FakeBus:
    """Answers portal requests the way xdg-desktop-portal does: reply now, Response signal right after."""

    unique_name = ":1.42"

    def __init__(self, response_code=0, register_fails=True, kde_active=None):
        self.code = response_code
        self.register_fails = register_fails
        self.kde_active = list(kde_active) if kde_active is not None else None  # answers to isActive, in order
        self.calls = []
        self.handler = None
        self.kde_keys = {}

    def add_message_handler(self, handler):
        self.handler = handler

    async def call(self, msg):
        self.calls.append(msg)
        if msg.member == "setForeignShortcut":
            self.kde_keys[msg.body[0][1]] = msg.body[1]
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])
        if msg.member == "shortcut":
            if self.kde_active is None:
                return SimpleNamespace(message_type=MessageType.ERROR, error_name="ServiceUnknown", body=[])
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[self.kde_keys.get(msg.body[0][1], [])])
        if msg.member == "isActive":
            if not self.kde_active:  # not KDE (or nothing left to say)
                return SimpleNamespace(message_type=MessageType.ERROR, error_name="ServiceUnknown", body=[])
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[self.kde_active.pop(0)])
        if msg.member == "Register" and self.register_fails:
            return SimpleNamespace(message_type=MessageType.ERROR, error_name="NotSupported", body=[])
        if msg.member in ("CreateSession", "BindShortcuts"):
            options = msg.body[0] if msg.member == "CreateSession" else msg.body[3]
            path = f"/org/freedesktop/portal/desktop/request/1_42/{options['handle_token'].value}"
            results = {"session_handle": Variant("o", "/org/freedesktop/portal/desktop/session/1_42/zephvox")}
            signal = SimpleNamespace(member="Response", path=path, interface="org.freedesktop.portal.Request",
                                     body=[self.code, results])  # fmt: skip
            asyncio.get_running_loop().call_soon(self.handler, signal)
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])


@pytest.mark.skipif(MessageType is None, reason="the shortcut portal is Linux-only (dbus-fast)")
class TestPortal:
    def test_setup_binds_and_dispatches_activations(self):
        bus, fired = FakeBus(), []
        session = PortalSession(bus, fired.append, response_timeout=2)
        asyncio.run(session.setup({"toggle_recording": "Ctrl+Alt+R"}, "io.github.zephvox"))
        members = [m.member for m in bus.calls]
        assert members == ["AddMatch", "AddMatch", "Register", "CreateSession", "BindShortcuts"]
        shortcut = bus.calls[-1].body[1][0]
        assert shortcut[0] == "toggle_recording" and shortcut[1]["preferred_trigger"].value == "CTRL+ALT+r"
        handle = session.session_handle
        bus.handler(SimpleNamespace(member="Activated", interface=IFACE, path="/", body=[handle, "toggle_recording", 0, {}]))
        bus.handler(SimpleNamespace(member="Activated", interface=IFACE, path="/", body=["/other", "cancel", 0, {}]))
        bus.handler(SimpleNamespace(member="Response", interface="x", path="/unknown", body=[0, {}]))
        assert fired == ["toggle_recording"]
        asyncio.run(session.close())
        assert bus.calls[-1].member == "Close" and session.session_handle is None
        asyncio.run(session.close())  # no session: no call

    def test_rebinds_when_kde_switched_the_shortcuts_off(self):
        # KDE switches an app's shortcuts off when any of its sessions closes (e.g. a second copy exiting).
        bus, rebinds = FakeBus(kde_active=[True, False, True]), []
        session = PortalSession(bus, lambda a: None, response_timeout=2)

        async def run():
            await session.setup({"cancel": "Ctrl+Alt+X"}, "io.github.zephvox")
            await session.watch({"cancel": "Ctrl+Alt+X"}, "io.github.zephvox", interval=0, on_rebind=lambda: rebinds.append(1))

        asyncio.run(run())  # returns once isActive stops answering, like on a desktop that isn't KDE
        members = [m.member for m in bus.calls]
        assert members.count("CreateSession") == 2 and "Close" in members and rebinds == [1]
        assert next(m for m in bus.calls if m.member == "isActive").path == "/component/io_github_zephvox"

    def test_old_kde_component_is_dropped(self):
        bus = FakeBus()
        session = PortalSession(bus, lambda a: None, response_timeout=2)
        asyncio.run(session.drop_kde_component("io.github.speakcheck", ["toggle_recording", "cancel"]))
        calls = [(m.member, m.body) for m in bus.calls]
        assert calls == [("unregister", ["io.github.speakcheck", "toggle_recording"]),
                         ("unregister", ["io.github.speakcheck", "cancel"])]  # fmt: skip

    def test_qt_key_codes(self):
        assert portal.to_qt_key("Ctrl+Shift+Alt+X") == 234881112  # what KDE stored on a real desktop
        for combo in ("Ctrl+Shift+Alt+R", "Super+F5", "Ctrl+Space", "Alt+7"):
            assert portal.from_qt_key(portal.to_qt_key(combo)) == combo
        with pytest.raises(ValueError):
            portal.to_qt_key("Ctrl+Mystery")

    def test_keys_are_set_and_read_through_kde(self):
        bus = FakeBus(kde_active=[True])
        session = PortalSession(bus, lambda a: None, response_timeout=2)

        async def run():
            assert await session.set_kde_bindings("io.github.zephvox", {"cancel": "Ctrl+Shift+Alt+Q"})
            return await session.kde_bindings("io.github.zephvox", ["cancel", "toggle_recording"])

        assert asyncio.run(run()) == {"cancel": "Ctrl+Shift+Alt+Q"}
        call = next(m for m in bus.calls if m.member == "setForeignShortcut")
        assert call.body[0] == ["io.github.zephvox", "cancel", "zephvox", "zephvox: cancel recording"]
        assert asyncio.run(PortalSession(FakeBus(), lambda a: None).kde_bindings("app", ["cancel"])) is None  # not KDE

    def test_watch_returns_at_once_off_kde(self):
        bus = FakeBus()
        session = PortalSession(bus, lambda a: None, response_timeout=2)
        asyncio.run(session.watch({}, "app", interval=0))
        assert [m.member for m in bus.calls] == ["isActive"]

    def test_declined_dialog_raises(self):
        session = PortalSession(FakeBus(response_code=1, register_fails=False), lambda a: None, response_timeout=2)
        with pytest.raises(ZephvoxError, match="declined"):
            asyncio.run(session.setup({"cancel": "Ctrl+Alt+X"}, "app"))

    def test_availability(self, monkeypatch):
        monkeypatch.setattr(portal.sys, "platform", "darwin")
        assert "Linux only" in PortalTriggers().check_available().reason
        monkeypatch.setattr(portal.sys, "platform", "linux")
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        assert "D-Bus" in PortalTriggers().check_available().reason
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/x")

        async def version(n):
            return n

        monkeypatch.setattr(portal, "_portal_version", lambda: version(2))
        assert PortalTriggers().check_available().ok
        monkeypatch.setattr(portal, "_portal_version", lambda: version(0))
        assert "no GlobalShortcuts" in PortalTriggers().check_available().reason

        async def broken():
            raise ConnectionError

        monkeypatch.setattr(portal, "_portal_version", broken)
        assert "cannot reach" in PortalTriggers().check_available().reason
        assert "System Settings" in PortalTriggers().describe()
        PortalTriggers().stop()  # never started: no-op


class FakeKeyboard:
    class GlobalHotKeys:
        last = None

        def __init__(self, hotkeys):
            self.hotkeys = hotkeys
            self.running = False
            FakeKeyboard.GlobalHotKeys.last = self

        def start(self):
            self.running = True

        def stop(self):
            self.running = False


class TestPynput:
    def test_start_fire_stop(self):
        fired = []
        trig = PynputTriggers(keyboard=FakeKeyboard)
        trig.start({"toggle_recording": "Ctrl+Alt+R", "cancel": "Ctrl+Alt+X"}, fired.append)
        listener = FakeKeyboard.GlobalHotKeys.last
        listener.hotkeys["<ctrl>+<alt>+r"]()
        assert fired == ["toggle_recording"] and listener.running
        trig.close()
        assert not listener.running
        trig.stop()  # idempotent

    def test_availability(self, monkeypatch):
        monkeypatch.setattr(pynput.sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert "Wayland" in PynputTriggers(keyboard=FakeKeyboard).check_available().reason
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        assert "X11" in PynputTriggers(keyboard=FakeKeyboard).check_available().reason
        monkeypatch.setenv("DISPLAY", ":0")
        assert PynputTriggers(keyboard=FakeKeyboard).check_available().ok
        monkeypatch.setattr(pynput.sys, "platform", "darwin")
        assert "Accessibility" in PynputTriggers().describe()
        monkeypatch.setattr(pynput.sys, "platform", "win32")
        assert "config" in PynputTriggers().describe()
