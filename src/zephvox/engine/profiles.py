"""Profiles: named sets of settings sized to a machine. `zephvox setup` recommends one.

A profile is just a config layer:

    built-in defaults  <  profile  <  config.toml  <  GUI settings  <  --set flags

so `profile = "small"` in config.toml changes model choices, and anything the user sets in
config.toml still wins. Thresholds are first estimates, to be tuned on real machines.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .hardware import Hardware


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    description: str
    gpu: bool  # GPU tiers install the GPU build of PyTorch; CPU tiers the small cpu build
    overlay: dict[str, Any] = field(default_factory=dict)
    # Whether a local grammar model is suggested for this machine (grammar itself is always opt-in).
    grammar_ok: bool = True

    @property
    def llm_model(self) -> str | None:
        """The grammar model to offer if the user turns grammar checking on."""
        if not self.grammar_ok:
            return None
        return self.overlay.get("provider", {}).get("openai-compat", {}).get("model", "qwen3:8b")


def _llm(model: str) -> dict[str, Any]:
    return {"openai-compat": {"model": model}}


CPU_STT = {"stt": ["faster-whisper", "hf-whisper"]}

PROFILES: dict[str, Profile] = {p.name: p for p in [
    Profile("large", "Large GPU", "GPU with 16 GB+ (or a Mac with 32 GB+): offers the most accurate grammar model.",
            True, {"provider": _llm("qwen3:14b")}),
    Profile("standard", "Standard GPU", "GPU with 10–16 GB (or a Mac with 16 GB+).", True, {}),
    Profile("small", "Small GPU", "GPU with 6–10 GB (or a Mac with 8 GB): offers a smaller grammar model.",
            True, {"provider": _llm("qwen3:4b")}),
    Profile("cpu", "CPU", "No usable GPU, 16 GB+ RAM. Works, but each analysis takes longer.",
            False, {"providers": CPU_STT,
                    "provider": {**_llm("qwen3:4b"), "wav2vec2": {"device": "cpu"}}}),
    Profile("cpu-light", "CPU, light", "Under 16 GB RAM: smaller speech model. "
            "Grammar can be turned on with a cloud LLM.",
            False, {"providers": CPU_STT,
                    "provider": {"faster-whisper": {"model": "base.en"}, "wav2vec2": {"device": "cpu"}}},
            grammar_ok=False),
]}  # fmt: skip


# "custom": the user's own choices from `zephvox setup`, kept in custom-profile.toml next to
# config.toml (a separate file, so config.toml's own tables are never duplicated).
CUSTOM = "custom"
CUSTOM_FILE = "custom-profile.toml"


def custom_path(config_path: Path) -> Path:
    return Path(config_path).parent / CUSTOM_FILE


def load_custom(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Invalid custom profile {path}: {e}") from e


def save_custom(path: Path, overlay: dict[str, Any]) -> None:
    header = "# Your custom profile, written by `zephvox setup` (run it again to change it).\n"
    header += "# Settings in config.toml still win over these.\n\n"
    Path(path).write_text(header + dump_toml(overlay), encoding="utf-8")


def custom(overlay: dict[str, Any]) -> Profile:
    cpu = overlay.get("provider", {}).get("wav2vec2", {}).get("device") == "cpu"
    return Profile(CUSTOM, "Custom", "Your own choices from setup.", not cpu, overlay)


def get(name: str, config_path: Path | None = None) -> Profile:
    """A profile by name; "custom" reads custom-profile.toml next to `config_path`."""
    if name == CUSTOM:
        return custom(load_custom(custom_path(config_path)) if config_path else {})
    try:
        return PROFILES[name]
    except KeyError:
        raise ConfigError(f"Unknown profile {name!r}; choose one of: {', '.join([*PROFILES, CUSTOM])}") from None


def dump_toml(data: dict[str, Any], prefix: str = "") -> str:
    """Tables of strings, numbers, booleans and lists of those (all a profile holds)."""
    lines, tables = [], []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for key, value in tables:
        name = f"{prefix}.{_key(key)}" if prefix else _key(key)
        if any(not isinstance(v, dict) for v in value.values()) or not value:
            out += f"\n[{name}]\n"
        out += dump_toml(value, name)
    return out


def _key(key: str) -> str:
    return key if key and all(c.isalnum() or c in "-_" for c in key) else json.dumps(key)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def recommend(hw: Hardware) -> str:
    gpu = hw.gpu
    if gpu is not None and gpu.vendor == "apple":
        return "large" if hw.ram_gb >= 30 else "standard" if hw.ram_gb >= 15 else "small"
    if gpu is not None and gpu.vram_gb:
        # Reported sizes run a little under the label (a "16 GB" card shows 15.98).
        if gpu.vram_gb >= 15:
            return "large"
        if gpu.vram_gb >= 9.5:
            return "standard"
        if gpu.vram_gb >= 5.5:
            return "small"
    return "cpu" if hw.ram_gb >= 14 else "cpu-light"


def accelerator(profile: Profile, hw: Hardware) -> str:
    """PyTorch build for this profile on this machine."""
    return hw.accelerator if profile.gpu else "cpu"


# Approximate (download, on disk) GB of the Python packages, by OS and PyTorch build.
PACKAGES_GB = {
    ("linux", "rocm"): (6.8, 16.0),
    ("linux", "cuda"): (3.9, 8.0),
    ("linux", "cpu"): (0.5, 1.5),
    ("win32", "cuda"): (2.8, 6.0),
    ("win32", "cpu"): (0.3, 1.2),
    ("darwin", "cpu"): (0.3, 1.0),
}


def packages_gb(os_key: str, accel: str) -> tuple[float, float]:
    return PACKAGES_GB.get((os_key, accel), (1.0, 3.0))
