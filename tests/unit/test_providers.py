"""Providers that can be tested without models: LLM (HTTP), G2P (dictionaries, chain), PipeWire capture."""

import sys
import json
import signal
import subprocess

import httpx
import pytest
import respx

from zephvox.engine.capabilities import SystemCapabilities
from zephvox.engine.errors import LLMError, RecordingError
from zephvox.engine.models import Pronunciation
from zephvox.providers.base import Availability, G2PProvider, Services
from zephvox.providers.capture import pipewire
from zephvox.providers.capture.pipewire import PipeWireCapture
from zephvox.providers.g2p.chain import ChainG2P
from zephvox.providers.g2p.cmudict import CMUDictG2P
from zephvox.providers.g2p.misaki import MisakiG2P, misaki_to_phones
from zephvox.providers.llm.openai_compat import OpenAICompatLLM, parse_json_reply

BASE = "http://llm.test/v1"


def chat_reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class TestOpenAICompat:
    def llm(self, **settings):
        return OpenAICompatLLM({"base_url": BASE, **settings})

    @respx.mock
    def test_available_when_model_listed(self):
        respx.get(f"{BASE}/models").respond(json={"data": [{"id": "qwen3:8b"}]})
        assert self.llm().check_available() == Availability(True)
        assert self.llm().capabilities()["model"] == "qwen3:8b"

    @respx.mock
    def test_model_missing_or_unreachable(self):
        respx.get(f"{BASE}/models").respond(json={"data": [{"id": "llama3"}]})
        ok, reason = self.llm().check_available()
        assert not ok and "ollama pull qwen3:8b" in reason
        respx.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
        ok, reason = self.llm().check_available()
        assert not ok and "cannot reach" in reason

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("SC_TEST_KEY", raising=False)
        ok, reason = self.llm(api_key_env="SC_TEST_KEY").check_available()
        assert not ok and "SC_TEST_KEY" in reason

    @respx.mock
    def test_complete_json_request_and_parse(self, monkeypatch):
        monkeypatch.setenv("SC_TEST_KEY", "secret")
        route = respx.post(f"{BASE}/chat/completions").mock(
            return_value=chat_reply('<think>hmm</think>```json\n{"mistakes": []}\n```')
        )
        out = self.llm(api_key_env="SC_TEST_KEY").complete_json("sys", "user", {"type": "object"})
        assert out == {"mistakes": []}
        request = route.calls.last.request
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer secret"
        assert body["model"] == "qwen3:8b" and body["temperature"] == 0.0 and body["max_tokens"] == 2048
        assert body["messages"][1]["content"].endswith("/no_think")
        assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}

    @respx.mock
    def test_retries_server_errors_then_fails(self):
        route = respx.post(f"{BASE}/chat/completions").mock(
            side_effect=[httpx.Response(503), httpx.ConnectError("x"), chat_reply('{"ok": 1}')]
        )
        assert self.llm(no_think=False).complete_json("s", "u", {}) == {"ok": 1}
        assert route.call_count == 3
        assert "/no_think" not in json.loads(route.calls.last.request.content)["messages"][1]["content"]

        respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(500))
        with pytest.raises(LLMError, match="failed"):
            self.llm(max_retries=1).complete_json("s", "u", {})

    @respx.mock
    def test_client_error_is_not_retried(self):
        route = respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(400))
        with pytest.raises(LLMError):
            self.llm().complete_json("s", "u", {})
        assert route.call_count == 1

    @respx.mock
    def test_truncated_reply_is_an_error(self):
        respx.post(f"{BASE}/chat/completions").respond(
            json={"choices": [{"message": {"content": '{"mistakes": ['}, "finish_reason": "length"}]})
        with pytest.raises(LLMError, match="cut off"):
            self.llm().complete_json("s", "u", {})

    @respx.mock
    def test_unexpected_shape(self):
        respx.post(f"{BASE}/chat/completions").respond(json={"nope": []})
        with pytest.raises(LLMError, match="Unexpected response"):
            self.llm().complete_json("s", "u", {})

    def test_parse_json_reply_errors(self):
        with pytest.raises(LLMError, match="valid JSON"):
            parse_json_reply("not json")
        with pytest.raises(LLMError, match="expected an object"):
            parse_json_reply("[1, 2]")


class TestCMUDict:
    def test_variants_and_unknown_words(self):
        g2p = CMUDictG2P()
        assert g2p.check_available().ok
        out = g2p.pronounce(["think", "either", "qwzxv"])
        assert out["think"].variants == [["θ", "ɪ", "ŋ", "k"]]
        assert ["i", "ð", "ɚ"] in out["either"].variants and ["aɪ", "ð", "ɚ"] in out["either"].variants
        assert out["qwzxv"].variants == [] and out["qwzxv"].source == ""
        assert g2p.capabilities()["variants"] is True


class TestMisaki:
    def test_shorthand_and_r_colored_vowels(self):
        assert misaki_to_phones("bˈʌɾəɹ") == ["b", "ʌ", "t", "ɚ"]
        assert misaki_to_phones("əɹˈIz") == ["ə", "ɹ", "aɪ", "z"]  # ɹ before a vowel stays
        assert misaki_to_phones("nˈɜɹs") == ["n", "ɚ", "s"]
        assert misaki_to_phones("ʧˈAnʤ") == ["tʃ", "eɪ", "n", "dʒ"]
        assert misaki_to_phones("ɡˈQ") == ["ɡ", "oʊ"]  # UK "go"

    def test_uk_dictionary(self):
        pytest.importorskip("misaki")
        g2p = MisakiG2P({"accent": "en-gb"})
        assert g2p.check_available().ok
        out = g2p.pronounce(["water", "read", "qwzxv"])
        assert out["water"].variants == [["w", "ɔ", "t", "ə"]]
        assert ["ɹ", "i", "d"] in out["read"].variants and ["ɹ", "ɛ", "d"] in out["read"].variants
        assert out["qwzxv"].variants == []
        assert not MisakiG2P({"accent": "en-au"}).check_available().ok


class DictG2P(G2PProvider):
    name = "dict"
    table: dict = {}

    def pronounce(self, words):
        return {w: Pronunciation(w, self.table.get(w.lower(), []), self.name) for w in words}  # like real drivers


class TestChain:
    def services(self, members, unavailable=()):
        def build(kind, name):
            if name not in members:
                raise KeyError(name)
            g2p = DictG2P()
            g2p.table = members[name]
            g2p.check_available = lambda: Availability(name not in unavailable, "off")
            return g2p

        return Services(SystemCapabilities(), build)

    def test_first_member_with_an_answer_wins(self):
        chain = ChainG2P({"order": ["chain", "a", "missing", "off", "b"]},
                         self.services({"a": {"x": [["a"]]}, "b": {"x": [["b"]], "y": [["y"]]}, "off": {}},
                                       unavailable={"off"}))  # fmt: skip
        assert chain.check_available().ok
        out = chain.pronounce(["x", "y", "z"])
        assert (out["x"].variants, out["y"].variants, out["z"].variants) == ([["a"]], [["y"]], [])
        caps = chain.capabilities()
        assert caps["members"] == ["dict", "dict"] and set(caps["skipped"]) == {"missing", "off"}

    def test_a_failing_member_is_skipped(self):
        def broken(words):
            raise OSError("offline")

        services, guess = self.guessing_services({"x": [["x"]]})
        guess.pronounce = broken
        chain = ChainG2P({"order": ["dict", "guess"]}, services)
        out = chain.pronounce(["x", "Ollama"])
        assert out["x"].variants == [["x"]] and out["Ollama"].variants == []
        assert chain.skipped["guess"] == "offline"

    def test_unavailable_when_no_members(self):
        chain = ChainG2P({"order": ["off"]}, self.services({"off": {}}, unavailable={"off"}))
        ok, reason = chain.check_available()
        assert not ok and "off: off" in reason

    def test_requires_build_service(self):
        with pytest.raises(RuntimeError):
            ChainG2P().check_available()

    def guessing_services(self, table):
        """A dictionary member plus a guesser that "knows" every word (marks guesses with ?)."""

        class Guess(DictG2P):
            name = "guess"

            def capabilities(self):
                return {"any_word": True}

            def pronounce(self, words):
                self.asked = getattr(self, "asked", []) + list(words)
                return {w: Pronunciation(w, [[f"{w}?"]], self.name) for w in words}

        guess = Guess()

        def build(kind, name):
            if name == "guess":
                return guess
            g2p = DictG2P()
            g2p.table = table
            return g2p

        return Services(SystemCapabilities(), build), guess

    def test_acronyms_and_compounds_use_the_dictionary_before_guessing(self):
        table = {c: [[c.upper()]] for c in "gpt"} | {"chat": [["tʃ", "æ", "t"]], "mail": [["m"]], "e": [["i"]],
                                                      "us": [["ʌ", "s"]]}  # fmt: skip
        services, guess = self.guessing_services(table)
        chain = ChainG2P({"order": ["dict", "guess"]}, services)
        out = chain.pronounce(["GPT", "ChatGPT", "e-mail", "chat", "TikTok", "US", "Ollama"])
        assert out["GPT"].variants == [["G", "P", "T"]]  # spelled out
        assert out["US"].variants == [["u?", "s?"]]  # letters this dictionary lacks are guessed one by one
        assert out["ChatGPT"].variants == [["tʃ", "æ", "t", "G", "P", "T"]] and out["ChatGPT"].source == "dict"
        assert out["e-mail"].variants == [["i", "m"]]
        assert out["TikTok"].variants == [["Tik?", "Tok?"]] and out["TikTok"].source == "guess"
        assert out["Ollama"].variants == [["Ollama?"]]
        assert "chat" not in guess.asked and "ChatGPT" not in guess.asked


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.waited = 0

    def wait(self, timeout=None):
        self.waited += 1


@pytest.mark.skipif(sys.platform != "linux", reason="PipeWire is Linux-only")
class TestPipeWire:
    def test_availability(self):
        assert PipeWireCapture(services=Services(SystemCapabilities(binaries={"pw-record": True}))).check_available().ok
        ok, reason = PipeWireCapture(services=Services(SystemCapabilities(binaries={"pw-record": False}))).check_available()
        assert not ok and "pw-record" in reason
        assert PipeWireCapture().capabilities()["detached"] is True

    def test_start_builds_command(self, monkeypatch, tmp_path):
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        handle = PipeWireCapture({"device": "mic-1"}).start(tmp_path / "a.wav")
        assert handle == {"pid": 4242, "path": str(tmp_path / "a.wav")}
        assert seen["cmd"][1:] == ["--rate", "16000", "--channels", "1", "--format", "s16", "--target", "mic-1",
                                   str(tmp_path / "a.wav")]  # fmt: skip
        assert seen["kw"]["start_new_session"] is True

    def test_stop_signals_and_waits(self, monkeypatch, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"\0" * 1000)
        alive = iter([True, True, False, False])
        kills = []
        monkeypatch.setattr(pipewire, "_alive", lambda pid: next(alive))
        monkeypatch.setattr(pipewire.os, "kill", lambda pid, sig: kills.append(sig))
        cap = PipeWireCapture()
        cap._proc = FakeProc()
        ref = cap.stop({"pid": 4242, "path": str(wav)})
        assert ref.path == wav and kills == [signal.SIGINT] and cap._proc is None

    def test_stop_from_other_process_kills_stuck_recorder(self, monkeypatch, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"\0" * 1000)
        kills = []
        monkeypatch.setattr(pipewire, "_alive", lambda pid: True)
        monkeypatch.setattr(pipewire.os, "kill", lambda pid, sig: kills.append(sig))
        monkeypatch.setattr(pipewire.time, "sleep", lambda s: None)
        PipeWireCapture({"stop_timeout": 0.01}).stop({"pid": 1, "path": str(wav)})
        assert kills == [signal.SIGINT, signal.SIGKILL]

    def test_empty_recording(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pipewire, "_alive", lambda pid: False)
        with pytest.raises(RecordingError, match="Nothing was recorded"):
            PipeWireCapture().stop({"pid": 1, "path": str(tmp_path / "none.wav")})

    def test_alive_helper(self):
        import os

        assert pipewire._alive(os.getpid())
        proc = subprocess.Popen(["true"])
        proc.wait()
        assert not pipewire._alive(proc.pid)

    def test_real_process_roundtrip(self, tmp_path):
        """Uses `sleep` as a stand-in recorder to exercise real signals and reaping."""
        cap = PipeWireCapture()
        cap._proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"\0" * 100)
        cap.stop({"pid": cap._proc.pid, "path": str(wav)})
        assert cap._proc is None


LOCAL = "http://localhost:11434/v1"


class FakeServer:
    """Stands in for `ollama serve`: records signals, can 'crash' with output."""

    def __init__(self, crash_output=None):
        self.crash_output = crash_output
        self.terminated = self.killed = False
        self.hang = False

    def __call__(self, cmd, stdout=None, **kw):
        self.cmd, self.env = cmd, kw.get("env", {})
        if self.crash_output:
            stdout.write(self.crash_output)
        return self

    def poll(self):
        return 1 if self.crash_output else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ollama", timeout)


class TestAutostart:
    def llm(self, ollama_installed=True, **settings):
        caps = SystemCapabilities(binaries={"ollama": ollama_installed})
        return OpenAICompatLLM({"base_url": LOCAL, "start_timeout": 0.5, **settings}, Services(caps))

    @respx.mock
    def test_available_on_demand_when_not_running(self):
        respx.get(f"{LOCAL}/models").mock(side_effect=httpx.ConnectError("refused"))
        ok, reason = self.llm().check_available()
        assert ok and "starts on demand" in reason
        assert self.llm().capabilities()["autostart"] is True
        assert not self.llm(ollama_installed=False).check_available().ok
        assert not self.llm(autostart=False).check_available().ok

    @respx.mock
    def test_prepare_starts_server_and_close_stops_it(self, monkeypatch):
        server = FakeServer()
        monkeypatch.setattr(subprocess, "Popen", server)
        monkeypatch.setattr("zephvox.providers.llm.openai_compat.time.sleep", lambda s: None)
        respx.get(f"{LOCAL}/models").mock(side_effect=[
            httpx.ConnectError("not yet"), httpx.ConnectError("starting"),
            httpx.Response(200, json={"data": [{"id": "qwen3:8b"}]}),
        ])  # fmt: skip
        llm = self.llm()
        llm.prepare()
        assert server.cmd[0].endswith("ollama") and server.cmd[1:] == ["serve"]  # resolved: may be off PATH
        assert server.env["OLLAMA_HOST"] == "localhost:11434"
        llm.close()
        assert server.terminated and llm._proc is None
        llm.close()  # idempotent

    @respx.mock
    def test_prepare_reuses_a_server_it_did_not_start(self, monkeypatch):
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not start a second server"))
        respx.get(f"{LOCAL}/models").respond(json={"data": [{"id": "qwen3:8b"}]})
        llm = self.llm()
        llm.prepare()
        llm.close()  # nothing of ours to stop

    @respx.mock
    def test_prepare_failures(self, monkeypatch):
        respx.get(f"{LOCAL}/models").mock(side_effect=httpx.ConnectError("refused"))
        monkeypatch.setattr("zephvox.providers.llm.openai_compat.time.sleep", lambda s: None)
        with pytest.raises(LLMError, match="cannot reach"):
            self.llm(autostart=False).prepare()

        monkeypatch.setattr(subprocess, "Popen", FakeServer(crash_output=b"line1\nError: address in use\n"))
        with pytest.raises(LLMError, match="exited: line1 | Error: address in use"):
            self.llm().prepare()

        hung = FakeServer()
        hung.hang = True
        monkeypatch.setattr(subprocess, "Popen", hung)
        with pytest.raises(LLMError, match="did not answer"):
            self.llm(start_timeout=0).prepare()
        assert hung.terminated and hung.killed

    @respx.mock
    def test_prepare_stops_server_when_model_missing(self, monkeypatch):
        server = FakeServer()
        monkeypatch.setattr(subprocess, "Popen", server)
        respx.get(f"{LOCAL}/models").mock(side_effect=[httpx.ConnectError("x"), httpx.Response(200, json={"data": []})])
        with pytest.raises(LLMError, match="ollama pull qwen3:8b"):
            self.llm().prepare()
        assert server.terminated

    @respx.mock
    def test_running_server_without_any_models_is_unavailable(self):
        respx.get(f"{LOCAL}/models").respond(json={"data": []})
        ok, reason = self.llm().check_available()
        assert not ok and "ollama pull" in reason
        assert self.llm(check_model=False).check_available().ok
