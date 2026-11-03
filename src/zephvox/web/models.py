"""Background download of the speech models the config needs, with progress for the page.

Runs `python -m zephvox.install.download --progress-json` as a child process, so a download can be
cancelled by ending the process and resumed by running it again (Hugging Face keeps partial files).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..engine.config import Config
from ..install import events

Publish = Callable[..., None]


def missing_models(config: Config) -> list[Any]:
    """Downloads the config needs that aren't on disk yet (Ollama models are left to setup)."""
    from ..install.download import cache_path, plan_downloads

    out = []
    for d in plan_downloads(config):
        path = cache_path(d)
        if path is not None and not any((path / "snapshots").glob("*/*")):
            out.append(d)
    return out


class ModelDownloads:
    def __init__(self, publish: Publish, spawn: Callable[..., Any] = subprocess.Popen):
        self._publish, self._spawn = publish, spawn
        self._lock = threading.Lock()
        self._proc: Any = None
        self._cancelled = False
        self.items: dict[str, dict[str, Any]] = {}
        self.running = False
        self.error: str | None = None
        self._last_publish = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = [dict(i) for i in self.items.values()]
            running, error = self.running, self.error
        known = [i for i in items if i["total"]]
        return {
            "running": running,
            "error": error,
            "items": items,
            "done": sum((i["total"] if i["state"] == "done" else i["done"]) or 0 for i in items),
            "total": sum(i["total"] for i in known) if known and len(known) == len(items) else None,
            "finished": bool(items) and not running and all(i["state"] == "done" for i in items),
        }  # fmt: skip

    def start(self, config_path: Path | None) -> dict[str, Any]:
        with self._lock:
            already = self.running
            if not already:
                self.running, self._cancelled, self.error = True, False, None
                self.items = {}
        if already:
            return self.snapshot()
        cmd = [sys.executable, "-m", "zephvox.install.download", "--progress-json"]
        if config_path:
            cmd += ["--config", str(config_path)]
        try:
            self._proc = self._spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1)  # fmt: skip
        except OSError as e:
            with self._lock:
                self.running, self.error = False, str(e)
            self._push(force=True)
            return self.snapshot()
        threading.Thread(target=self._read, name="model-downloads", daemon=True).start()
        self._push(force=True)
        return self.snapshot()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _read(self) -> None:
        proc = self._proc
        for line in proc.stdout:
            event = events.parse(line.rstrip("\n"))
            if event and event["type"] == "download":
                with self._lock:
                    self.items[event["id"]] = {k: event.get(k) for k in ("id", "name", "label", "state", "done", "total", "error")}
                self._push(force=event["state"] != "downloading")
        code = proc.wait()
        with self._lock:
            self.running = False
            if self._cancelled:
                for i in self.items.values():
                    if i["state"] in ("queued", "downloading"):
                        i["state"] = "paused"
            elif code != 0 and not any(i["state"] == "failed" for i in self.items.values()):
                self.error = f"The model downloader stopped unexpectedly (exit code {code})."
        self._push(force=True)

    def _push(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish < 0.3:
            return
        self._last_publish = now
        self._publish("models_progress", **self.snapshot())
