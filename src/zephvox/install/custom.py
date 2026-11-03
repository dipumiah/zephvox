"""Setup's "Custom" profile: choose each part yourself (device, speech-to-text, accent, grammar, voice).

The answers become a profile overlay saved in custom-profile.toml. Every question defaults to what
the starting config already does, so pressing Enter throughout keeps the recommended profile.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..engine.config import Config
from ..engine.hardware import Hardware
from ..providers.llm.openai_compat import OLLAMA_APPROX_GB, OpenAICompatLLM
from ..providers.stt.faster_whisper import APPROX_GB as FASTER_GB
from ..providers.stt.faster_whisper import FasterWhisperSTT
from ..providers.stt.hf_whisper import APPROX_GB as WHISPER_GB
from ..providers.stt.hf_whisper import HFWhisperSTT
from ..providers.tts.kokoro import VOICES, KokoroTTS

LOCAL_OLLAMA = "http://localhost:11434/v1"

WHISPER = [
    ("openai/whisper-large-v3-turbo", "Whisper large-v3-turbo", "fast and accurate"),
    ("openai/whisper-large-v3", "Whisper large-v3", "most accurate, slower"),
    ("distil-whisper/distil-large-v3", "Distil-Whisper large-v3", "fast, English"),
    ("openai/whisper-medium.en", "Whisper medium.en", ""),
    ("openai/whisper-small.en", "Whisper small.en", "for small GPUs"),
]
FASTER = [
    ("base.en", "Whisper base.en", "fastest, least accurate"),
    ("small.en", "Whisper small.en", "a good balance on the CPU"),
    ("medium.en", "Whisper medium.en", "slower, more accurate"),
    ("large-v3", "Whisper large-v3", "slow on the CPU"),
]
# Ollama models: (name, note, GPU memory it needs to run comfortably, GB)
GRAMMAR = [
    ("qwen3:14b", "most accurate", 12.0),
    ("qwen3:8b", "", 7.0),
    ("qwen3:4b", "for small GPUs or the CPU", 4.0),
    ("qwen3:32b", "for 24 GB+ GPUs", 22.0),
]


@dataclass
class Choices:
    device: str = "gpu"  # gpu | cpu
    stt: str = "openai/whisper-large-v3-turbo"  # a Whisper repo (gpu) or a faster-whisper size (cpu)
    accent: str = "en-us"  # en-us | en-gb
    grammar: str = "off"  # off | ollama | api (opt-in)
    llm_model: str = "qwen3:8b"
    llm_url: str = LOCAL_OLLAMA
    llm_key_env: str | None = None
    voice: str = "kokoro"  # kokoro | espeak-ng | none
    kokoro_voice: str = "af_heart"


def gpu_available(hw: Hardware) -> bool:
    gpu = hw.gpu
    return gpu is not None and (hw.accelerator != "cpu" or gpu.vendor == "apple")


def defaults_from(config: Config, hw: Hardware) -> Choices:
    """What a config already does, as answers (the starting point for the questions)."""
    settings = config.provider_settings
    stt = (config.providers.get("stt") or ["hf-whisper"])[0]
    cpu = stt == "faster-whisper" or settings("wav2vec2").get("device") == "cpu" or not gpu_available(hw)
    order = settings("chain").get("order", [])
    british = (order[:1] == ["misaki"] and settings("misaki").get("accent") == "en-gb") or (
        order[:1] == ["espeak"] and settings("espeak").get("language") == "en-gb")  # fmt: skip
    llm = {**OpenAICompatLLM.Settings().model_dump(), **settings("openai-compat")}
    tts = (config.providers.get("tts") or [None])[0]
    return Choices(
        device="cpu" if cpu else "gpu",
        stt=settings("faster-whisper").get("model", FasterWhisperSTT.Settings().model) if cpu
        else settings("hf-whisper").get("model", HFWhisperSTT.Settings().model),
        accent="en-gb" if british else "en-us",
        grammar="off" if "grammar" not in config.engine.analyzers
        else ("ollama" if llm["base_url"].rstrip("/") == LOCAL_OLLAMA else "api"),
        llm_model=llm["model"],
        llm_url=llm["base_url"],
        llm_key_env=llm.get("api_key_env"),
        voice={"kokoro": "kokoro", "espeak-ng": "espeak-ng"}.get(tts or "", "none"),
        kokoro_voice=settings("kokoro").get("voice", KokoroTTS.Settings().voice),
    )  # fmt: skip


def overlay_for(c: Choices) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    provider: dict[str, Any] = {}
    engine: dict[str, Any] = {}
    if c.device == "cpu":
        providers["stt"] = ["faster-whisper", "hf-whisper"]
        provider["faster-whisper"] = {"model": c.stt}
        provider["wav2vec2"] = {"device": "cpu"}
    else:
        providers["stt"] = ["hf-whisper", "faster-whisper"]
        provider["hf-whisper"] = {"model": c.stt}
    if c.accent == "en-gb":
        provider["chain"] = {"order": ["misaki", "deep-phonemizer"]}
        provider["misaki"] = {"accent": "en-gb"}
    if c.grammar == "off":
        engine["analyzers"] = ["pronunciation"]
    else:
        engine["analyzers"] = ["pronunciation", "grammar"]
        llm: dict[str, Any] = {"model": c.llm_model}
        if c.grammar == "api":
            llm.update(base_url=c.llm_url, autostart=False)
            if c.llm_key_env:
                llm["api_key_env"] = c.llm_key_env
        provider["openai-compat"] = llm
    if c.voice == "kokoro":
        providers["tts"] = ["kokoro", "espeak-ng"]
        provider["kokoro"] = {"voice": c.kokoro_voice}
    else:
        providers["tts"] = ["espeak-ng"] if c.voice == "espeak-ng" else []
    return {"providers": providers, "provider": provider, "engine": engine}


def describe(c: Choices) -> str:
    stt = dict((k, label) for k, label, _ in WHISPER + FASTER).get(c.stt, c.stt)
    grammar = {"off": "no grammar", "api": f"{c.llm_model} (API)"}.get(c.grammar, c.llm_model)
    voice = {"kokoro": f"Kokoro {VOICES.get(c.kokoro_voice, (c.kokoro_voice,))[0].split(' — ')[0]}",
             "espeak-ng": "espeak-ng", "none": "browser voice"}[c.voice]  # fmt: skip
    return " · ".join([c.device.upper(), stt, "British" if c.accent == "en-gb" else "American", grammar, voice])


def ask(console: Any, hw: Hardware, start: Choices) -> Choices:
    """The questions; `console` is setup's Console (choose/text/say)."""
    say = console.say
    c = replace(start)
    say("\nCustom profile (Enter keeps the default)")

    if gpu_available(hw):
        gpu = hw.gpu
        name = f"{gpu.name}, {gpu.vram_gb:.0f} GB" if gpu.vram_gb else gpu.name  # type: ignore[union-attr]
        say("\nRun the speech models on:")
        device = console.choose("  Choose", [("gpu", f"GPU ({name})"), ("cpu", "CPU")], c.device)
    else:
        say("\nNo usable GPU found: the speech models run on the CPU.")
        device = "cpu"
    if device != c.device:  # the other list's model names don't apply
        c = replace(c, stt=FasterWhisperSTT.Settings().model if device == "cpu" else HFWhisperSTT.Settings().model)
    c.device = device

    say("\nSpeech-to-text:")
    models, sizes = (FASTER, FASTER_GB) if c.device == "cpu" else (WHISPER, WHISPER_GB)
    options = [(k, f"{label:<24} {sizes.get(k, 0):>4.1f} GB  {note}".rstrip()) for k, label, note in models]
    if c.stt not in dict(options):
        options.append((c.stt, f"{c.stt} (from your config)"))
    c.stt = console.choose("  Choose", options, c.stt)

    say("\nAccent to compare against:")
    accent = console.choose("  Choose", [("en-us", "American (CMU dictionary)"),
                                         ("en-gb", "British (misaki UK dictionary)")], c.accent)  # fmt: skip
    if accent != c.accent and c.kokoro_voice[:1] in "ab":  # follow with a voice of that accent
        c.kokoro_voice = "bf_emma" if accent == "en-gb" else "af_heart"
    c.accent = accent

    say("\nGrammar checking:")
    vram = hw.gpu.vram_gb if hw.gpu and c.device == "gpu" else None
    options = []
    for model, note, needs in GRAMMAR:
        warn = f"  ! may not fit in {vram:.0f} GB" if vram and needs > vram else ""
        options.append((model, f"{model:<11} {OLLAMA_APPROX_GB.get(model, 0):>4.1f} GB  {note}{warn}".rstrip()))
    current = c.llm_model if c.grammar == "ollama" else None
    options += [("other", "Another Ollama model (type its name)"),
                ("api", "An OpenAI-compatible API (address, model, key variable)"),
                ("off", "Off (pronunciation only)")]  # fmt: skip
    if current and current not in dict(options):
        options.insert(len(GRAMMAR), (current, f"{current} (from your config)"))
    default = "off" if c.grammar == "off" else "api" if c.grammar == "api" else current or "qwen3:8b"
    pick = console.choose("  Choose", options, default)
    if pick == "off":
        c.grammar = "off"
    elif pick == "other":
        c.grammar, c.llm_url = "ollama", LOCAL_OLLAMA
        c.llm_model = console.text("  Ollama model name (e.g. llama3.1:8b):", current or "qwen3:8b")
    elif pick == "api":
        was_api, c.grammar = c.grammar == "api", "api"
        c.llm_url = console.text("  API address (e.g. https://api.openai.com/v1):",
                                 c.llm_url if c.llm_url != LOCAL_OLLAMA else "")  # fmt: skip
        c.llm_model = console.text("  Model name:", c.llm_model if was_api else "")
        c.llm_key_env = console.text("  Name of the variable in .env that holds the API key:",
                                     c.llm_key_env or "OPENAI_API_KEY") or None  # fmt: skip
    else:
        c.grammar, c.llm_model, c.llm_url = "ollama", pick, LOCAL_OLLAMA

    say("\nPractice voice:")
    heard = VOICES.get(c.kokoro_voice, (c.kokoro_voice,))[0]
    pick = console.choose("  Choose", [
        ("kokoro", f"Kokoro, {heard}   0.3 GB"),
        ("pick", "Kokoro: pick another voice"),
        ("espeak-ng", "espeak-ng (robotic, if installed on the system)"),
        ("none", "None (your browser's voice)"),
    ], "kokoro" if c.voice == "kokoro" else c.voice)  # fmt: skip
    if pick == "pick":
        voices = [(v, label) for v, (label, _) in VOICES.items()]
        c.kokoro_voice = console.choose("  Voice", voices, c.kokoro_voice if c.kokoro_voice in VOICES else "af_heart")
        pick = "kokoro"
    c.voice = pick
    return c
