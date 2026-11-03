"""Shared fakes: deterministic providers so the engine can be tested without models."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from zephvox.analyzers.grammar.analyzer import GrammarAnalyzer
from zephvox.analyzers.pronunciation.analyzer import PronunciationAnalyzer
from zephvox.analyzers.pronunciation.practice import PracticeAnalyzer
from zephvox.engine import audio
from zephvox.engine.capabilities import SystemCapabilities
from zephvox.engine.config import Config, EngineSettings
from zephvox.engine.engine import Engine
from zephvox.engine.models import AudioRef, Pronunciation, Segment, Transcript, Word
from zephvox.engine.registry import Registry
from zephvox.providers.base import (
    Availability,
    CaptureProvider,
    Device,
    G2PProvider,
    LLMProvider,
    PhonemeRecognizer,
    ProviderSettings,
    STTProvider,
)
from zephvox.reporters.json import JsonReporter
from zephvox.reporters.markdown import MarkdownReporter
from zephvox.reporters.terminal import TerminalReporter

# "I think we should go now." — word timings on a 3 s clip.
SENTENCE_1 = [("I", 0.0, 0.2), ("think", 0.2, 0.6), ("we", 0.6, 0.8), ("should", 0.8, 1.1), ("go", 1.1, 1.3),
              ("now.", 1.3, 1.6)]  # fmt: skip
SENTENCE_2 = [("Yesterday", 1.8, 2.3), ("he", 2.3, 2.4), ("go", 2.4, 2.6), ("to", 2.6, 2.7), ("market.", 2.7, 3.0)]

LEXICON = {
    "i": [["aɪ"]],
    "think": [["θ", "ɪ", "ŋ", "k"]],
    "we": [["w", "i"]],
    "should": [["ʃ", "ʊ", "d"]],
    "go": [["ɡ", "oʊ"]],
    "now": [["n", "aʊ"]],
    "yesterday": [["j", "ɛ", "s", "t", "ɚ", "d", "eɪ"]],
    "he": [["h", "i"]],
    "to": [["t", "u"]],
    "market": [["m", "ɑ", "ɹ", "k", "ə", "t"]],
}

# What the fake recognizer "hears" per sentence: "think" said as "sink".
HEARD = [
    ["aɪ", "s", "ɪ", "ŋ", "k", "w", "i", "ʃ", "ʊ", "d", "ɡ", "oʊ", "n", "aʊ"],
    ["j", "ɛ", "s", "t", "ɚ", "d", "eɪ", "h", "i", "ɡ", "oʊ", "t", "u", "m", "ɑ", "ɹ", "k", "ə", "t"],
]


def make_words(spec):
    return [Word(t, s, e, 0.9) for t, s, e in spec]


class FakeSTT(STTProvider):
    name = "fake-stt"
    calls = 0

    class Settings(ProviderSettings):
        verbatim: str = "partial"

    def capabilities(self) -> dict[str, Any]:
        return {"word_timestamps": True, "verbatim": self.settings.verbatim}

    def transcribe(self, samples, sample_rate):
        type(self).calls += 1
        words = make_words(SENTENCE_1) + make_words(SENTENCE_2)
        # One time-based chunk, as real STT returns it; the engine splits sentences.
        seg = Segment(" ".join(w.text for w in words), 0.0, 3.0, words)
        return Transcript([seg], provider=self.name, verbatim=self.settings.verbatim)


class FakeG2P(G2PProvider):
    name = "fake-g2p"

    def pronounce(self, words):
        return {w: Pronunciation(w, LEXICON.get(w, []), self.name) for w in words}


class FakePhonemes(PhonemeRecognizer):
    name = "fake-phonemes"
    calls = 0

    def recognize(self, samples, sample_rate):
        index = type(self).calls % len(HEARD)  # one call per sentence, in order
        type(self).calls += 1
        return list(HEARD[index])


class FakeLLM(LLMProvider):
    name = "fake-llm"
    reply: dict[str, Any] = {
        "mistakes": [
            {"index": 1, "original": "he go", "correction": "he went", "category": "verb-tense",
             "explanation": "Use the past tense with 'yesterday'.", "confidence": 0.95},
        ]
    }  # fmt: skip
    prompts: list[str] = []

    def complete_json(self, system, user, schema):
        type(self).prompts.append(user)
        return type(self).reply


class FakeCapture(CaptureProvider):
    name = "fake-capture"
    stopped: list[dict] = []

    class Settings(ProviderSettings):
        device: str | None = None

    def start(self, dest: Path):
        audio.write_wav(dest, np.zeros(16000 * 3, dtype=np.float32))
        return {"path": str(dest)}

    def stop(self, handle):
        type(self).stopped.append(handle)
        return AudioRef(Path(handle["path"]))

    def level(self, handle):
        return 0.25

    def list_devices(self):
        return [Device("mic-1", "USB Mic", True), Device("mic-2", "Laptop Mic")]


class UnavailableSTT(STTProvider):
    name = "broken-stt"

    def check_available(self):
        return Availability(False, "not installed")


@pytest.fixture(autouse=True)
def _private_data_dir(tmp_path, monkeypatch):
    """Tests never read or write the real ~/.local/share/zephvox (settings.json, sessions…)."""
    monkeypatch.setenv("ZEPHVOX_HOME", str(tmp_path / "zephvox-home"))
    # Folder lookups (and the move from the old "speakcheck" folders) stay inside the test, too.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SPEAKCHECK_HOME", raising=False)
    monkeypatch.delenv("SPEAKCHECK_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeSTT.calls = 0
    FakePhonemes.calls = 0
    FakeLLM.prompts = []
    FakeLLM.reply = {
        "mistakes": [
            {"index": 1, "original": "he go", "correction": "he went", "category": "verb-tense",
             "explanation": "Use the past tense with 'yesterday'.", "confidence": 0.95},
        ]
    }  # fmt: skip
    FakeCapture.stopped = []


@pytest.fixture
def registry() -> Registry:
    reg = Registry(entry_points_fn=None)
    for kind, cls in [
        ("stt", FakeSTT), ("stt", UnavailableSTT), ("g2p", FakeG2P), ("phonemes", FakePhonemes),
        ("llm", FakeLLM), ("capture", FakeCapture),
        ("analyzers", PronunciationAnalyzer), ("analyzers", GrammarAnalyzer), ("analyzers", PracticeAnalyzer),
        ("reporters", TerminalReporter), ("reporters", MarkdownReporter), ("reporters", JsonReporter),
    ]:  # fmt: skip
        reg.register(kind, cls.name, cls)
    return reg


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        engine=EngineSettings(data_dir=tmp_path / "data", notifications=False,
                              analyzers=["pronunciation", "grammar"]),  # no real desktop popups
        providers={
            "capture": ["fake-capture"],
            "stt": ["broken-stt", "fake-stt"],
            "g2p": ["fake-g2p"],
            "phonemes": ["fake-phonemes"],
            "llm": ["fake-llm"],
        },
    )


@pytest.fixture
def caps() -> SystemCapabilities:
    return SystemCapabilities(binaries={"ffmpeg": True, "pw-record": True, "espeak-ng": False})


@pytest.fixture
def engine(config, registry, caps) -> Engine:
    return Engine(config=config, registry=registry, caps=caps)


@pytest.fixture
def wav_file(tmp_path) -> Path:
    path = tmp_path / "clip.wav"
    audio.write_wav(path, np.zeros(16000 * 3, dtype=np.float32))
    return path
