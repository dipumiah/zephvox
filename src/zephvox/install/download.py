"""Fetch the speech models the configured drivers will use, ahead of the first analysis.

Run as `python -m zephvox.install.download --config config.toml`: setup starts it in a fresh
process, after installing the packages it imports.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..engine.config import Config, load_config
from ..engine.registry import Registry
from ..providers.base import Download
from ..providers.g2p.chain import ChainG2P

CHAIN_ORDER = ChainG2P.Settings().order

# The stages whose drivers download models.
MODEL_KINDS = ("stt", "phonemes", "g2p", "tts", "llm")


def plan_downloads(config: Config, registry: Registry | None = None) -> list[Download]:
    """What the first-choice driver of each stage will download. Needs no model packages."""
    registry = registry or Registry()
    found = []
    for kind in MODEL_KINDS:
        names = [n for n in config.providers.get(kind, []) if n in registry.names(kind)]
        if not names:
            continue
        members = [names[0]]
        if kind == "g2p" and names[0] == "chain":  # a chain uses every member
            members = [n for n in config.provider_settings("chain").get("order", CHAIN_ORDER) if n in registry.names(kind)]
        for name in members:
            found += registry.get(kind, name)(config.provider_settings(name)).downloads()
    return found


def hf_cache_dir() -> Path:
    if env := os.environ.get("HF_HUB_CACHE"):
        return Path(env)
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def repo_id(download: Download) -> str | None:
    """The Hugging Face repo a download lives in (faster-whisper names map to Systran's)."""
    if download.source == "huggingface":
        return download.name
    if download.source == "faster-whisper":
        return download.name if "/" in download.name else f"Systran/faster-whisper-{download.name}"
    return None


def cache_path(download: Download) -> Path | None:
    repo = repo_id(download)
    return hf_cache_dir() / f"models--{repo.replace('/', '--')}" if repo else None


# What faster-whisper's own download_model() fetches, so its cache entry is the same one.
FASTER_WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

ProgressFn = Callable[[int, int | None], None]  # (bytes done, bytes total or None)


def item_id(download: Download) -> str:
    return f"{download.source}:{download.name}"


def progress_tqdm(report: ProgressFn, min_interval: float = 0.25) -> type:
    """A tqdm class for huggingface_hub that adds up the byte counts of every file's bar."""
    from tqdm.auto import tqdm

    bars: dict[int, list[int | None]] = {}  # bar id → [done, total]
    lock = threading.Lock()
    last = [0.0]

    def push(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last[0] < min_interval:
            return
        last[0] = now
        with lock:
            done = sum(b[0] or 0 for b in bars.values())
            totals = [b[1] for b in bars.values()]
        report(done, sum(t or 0 for t in totals) if totals and all(t is not None for t in totals) else None)

    class ProgressTqdm(tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["disable"] = False
            super().__init__(*args, **{**kwargs, "file": open(os.devnull, "w")})  # noqa: SIM115 (never drawn)
            if self.unit == "B":
                with lock:
                    bars[id(self)] = [self.n, self.total]

        def update(self, n: float | None = 1) -> bool | None:
            result = super().update(n)
            if id(self) in bars:
                with lock:
                    bars[id(self)] = [self.n, self.total]
                push()
            return result

        def close(self) -> None:
            super().close()
            if id(self) in bars:
                push(force=True)

    return ProgressTqdm


def fetch(download: Download, progress: ProgressFn | None = None) -> None:
    kwargs: dict[str, Any] = {"tqdm_class": progress_tqdm(progress)} if progress else {}
    if download.source == "huggingface":
        from huggingface_hub import snapshot_download

        snapshot_download(download.name, revision=download.revision,
                          allow_patterns=list(download.patterns) if download.patterns else None, **kwargs)  # fmt: skip
    elif download.source == "faster-whisper":
        if progress:
            from huggingface_hub import snapshot_download

            snapshot_download(repo_id(download), allow_patterns=FASTER_WHISPER_FILES, **kwargs)
        else:
            from faster_whisper import download_model

            download_model(download.name)


def run_json(downloads: list[Download], fetcher: Callable[..., None] = fetch) -> int:
    """Download everything, reporting each item's progress as events. Failures don't stop the others."""
    from . import events

    items = [d for d in downloads if d.source != "ollama"]  # setup pulls those with the ollama program

    def total_bytes(d: Download) -> int | None:
        return int(d.approx_gb * 1e9) if d.approx_gb else None

    for d in items:
        events.emit("download", id=item_id(d), name=d.name, label=d.name, state="queued",
                    done=0, total=total_bytes(d), error=None)  # fmt: skip
    failed = 0
    for d in items:
        common = {"id": item_id(d), "name": d.name, "label": d.name}

        def report(done: int, total: int | None, d=d, common=common) -> None:
            events.emit("download", **common, state="downloading", done=done, total=total or total_bytes(d), error=None)

        events.emit("download", **common, state="downloading", done=0, total=total_bytes(d), error=None)
        try:
            fetcher(d, report)
        except Exception as e:  # keep going: the driver retries this one on first use
            failed += 1
            events.emit("download", **common, state="failed", done=None, total=total_bytes(d), error=str(e))
            continue
        size = total_bytes(d)
        events.emit("download", **common, state="done", done=size, total=size, error=None)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zephvox.install.download")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--progress-json", action="store_true", help="report progress as ZEPHVOX_EVENT lines")
    args = parser.parse_args(argv)
    if args.progress_json:
        return run_json(plan_downloads(load_config(args.config)))
    for download in plan_downloads(load_config(args.config)):
        if download.source == "ollama":
            continue  # setup pulls it with the ollama program
        size = f" (~{download.approx_gb:g} GB)" if download.approx_gb else ""
        print(f"Downloading {download.name}{size}", flush=True)
        try:
            fetch(download)
        except Exception as e:  # report and keep going: the driver retries on first use
            print(f"  failed: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
