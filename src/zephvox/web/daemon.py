"""The long-running daemon behind `zephvox serve`.

Owns one Engine (so models stay loaded), runs engine jobs on a single worker thread, turns
shortcut presses and API calls into the same actions, and broadcasts every engine event to
connected pages.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..engine import hardware, profiles
from ..engine.config import (
    ACTIONS,
    PROVIDER_KINDS,
    Config,
    apply_user_settings,
    choose_profile,
    default_config_path,
    load_config,
    load_user_settings,
    project_root,
    save_user_settings,
)
from ..engine.engine import Engine, Recording
from ..engine.errors import ProviderUnavailable, RecordingError, ZephvoxError
from ..engine.events import Event
from ..engine.session import Session
from .models import ModelDownloads, missing_models
from .notify import Notifier

# Practice auto-stop: a level above SPEECH means the user started talking; staying below
# SILENCE for `practice_auto_stop_s` afterwards ends the attempt.
SPEECH_LEVEL = 0.45
SILENCE_LEVEL = 0.30
LEVEL_INTERVAL_S = 0.12


def jsonable(value: Any) -> Any:
    """Event payloads → JSON-safe data (dataclasses, paths, tuples)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


class EventHub:
    """Fan engine events (from any thread) out to every connected page's asyncio queue."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(lp, q) for lp, q in self._subscribers if q is not queue]

    def publish(self, type: str, data: dict[str, Any]) -> None:
        message = json.dumps({"type": type, "data": jsonable(data)}, ensure_ascii=False)
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._put, queue, message)
            except RuntimeError:  # that page's loop is gone
                self.unsubscribe(queue)

    @staticmethod
    def _put(queue: asyncio.Queue, message: str) -> None:
        if queue.full():
            queue.get_nowait()  # a stalled page loses the oldest events, never blocks the engine
        queue.put_nowait(message)


def installed_accelerator(root: Path | None) -> str | None:
    """The PyTorch build setup installed (from .install.json)."""
    from ..install.state import InstallState

    return InstallState.load(root).accelerator if root else None


class Daemon:
    def __init__(self, engine: Engine, start_triggers: bool = True):
        self.engine = engine
        self.hub = EventHub()
        engine.events.subscribe(self._forward)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine")
        self._jobs = 0
        self._jobs_lock = threading.Lock()
        # {"text": ..., "session_id": ...}; remembered across restarts in settings.json
        self.practice_target: dict[str, Any] | None = load_user_settings(engine.config.engine.data_dir).get("practice_target")
        self.notify = Notifier(engine.config.engine.notifications)
        # Without a config file (tests, embedding) the config as given is the base for page changes.
        self._initial_config = engine.config
        self.downloads: dict[str, dict[str, Any]] = {}  # grammar models being pulled, by name
        self.models = ModelDownloads(self.publish)  # speech models fetched in the background
        self._sharing: Any = None  # the server inside the app ("Share this computer"), while on
        self._admin_db: Any = None
        if start_triggers and engine.config.server.share:  # turned on earlier: serve again
            threading.Thread(target=self._start_sharing_quietly, name="sharing-start", daemon=True).start()
        self._shortcut_pending: set[str] = set()  # "analysis" / "practice": announce the result
        self.request_shutdown: Callable[[], None] | None = None  # set by `serve`: stops the web server
        self.analyzing: str | None = None  # session being analyzed right now
        self.waiting: list[str] = []  # sessions queued behind it, oldest first
        self._backend: Any = None  # LocalBackend | RemoteBackend, built on first use
        self._backend_lock = threading.Lock()
        self._sync: Any = None  # SyncWorker while a server is connected
        self.install = {"running": False, "ok": None, "log": []}
        self.analyzers_for_next: list[str] | None = None
        self.triggers: dict[str, Any] = {"driver": None, "active": False, "error": None, "description": ""}
        self._trigger_provider = None
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, name="level-ticker", daemon=True)
        self._ticker.start()
        if start_triggers:
            threading.Thread(target=self._start_triggers, name="triggers", daemon=True).start()
        # Hardware detection runs programs (rocminfo, nvidia-smi): do it now, not when the page asks.
        threading.Thread(target=lambda: self.hardware, name="hardware", daemon=True).start()
        self._restart_sync()

    # --- speech models: fetched ahead of use, with progress --------------------------------------

    def start_model_downloads(self, only_if_missing: bool = True) -> dict[str, Any]:
        """Download the models the config needs (resumes partial ones). Not in client mode: the server has them."""
        config = self.engine.config
        if only_if_missing and (config.remote.mode == "remote" or not missing_models(config)):
            return self.models.snapshot()
        return self.models.start(config.source)

    def cancel_model_downloads(self) -> dict[str, Any]:
        self.models.cancel()
        return self.models.snapshot()

    # --- events -------------------------------------------------------------------------

    def _forward(self, event: Event) -> None:
        self.hub.publish(event.type, event.data)

    def publish(self, type: str, **data: Any) -> None:
        self.hub.publish(type, data)

    @property
    def busy(self) -> bool:
        return self._jobs > 0

    def _submit(self, name: str, fn: Callable[[], Any]) -> Future:
        with self._jobs_lock:
            self._jobs += 1
            pending = self._jobs
        # Jobs run one at a time: "queued" now, "started" when this one actually begins.
        self.publish("job_queued", job=name, pending=pending)

        def run():
            self.publish("job_started", job=name)
            try:
                return fn()
            except ZephvoxError as e:
                self.publish("job_error", job=name, message=str(e))
                raise
            except Exception as e:  # never kill the worker silently
                self.publish("job_error", job=name, message=f"{type(e).__name__}: {e}")
                raise
            finally:
                with self._jobs_lock:
                    self._jobs -= 1
                    pending = self._jobs
                self.publish("job_finished", job=name, pending=pending)

        return self._worker.submit(run)

    # --- where analysis runs ----------------------------------------------------------------

    @property
    def backend(self) -> Any:
        """This computer's engine, or the server in client mode (see `client/backend.py`)."""
        from ..client.backend import make_backend

        with self._backend_lock:
            if self._backend is None:
                self._backend = make_backend(self.engine)
            return self._backend

    def _reset_backend(self) -> None:
        with self._backend_lock:
            old, self._backend = self._backend, None
        if old is not None:
            old.close()
        self._restart_sync()

    # --- background sync: history backup and the offline queue -------------------------------

    def _restart_sync(self) -> None:
        from ..client import credentials
        from ..client.backend import client_for
        from ..client.sync import SyncWorker

        old, self._sync = self._sync, None
        if old is not None:
            old.stop()
        remote = self.engine.config.remote
        if not remote.url or credentials.load(self.engine.config.engine.data_dir, remote.url) is None:
            return
        if remote.mode != "remote" and not remote.syncing:
            return
        self._sync = SyncWorker(self.engine, lambda: client_for(self.engine), history=remote.syncing,
                                analyze_pending=self._analyze_pending if remote.mode == "remote" else None,
                                publish=self.publish)  # fmt: skip
        self._sync.start()

    def sync_soon(self) -> None:
        if self._sync is not None:
            self._sync.trigger()

    def _analyze_pending(self, session: Session, analyzers: list[str] | None) -> None:
        """A recording queued while the server was unreachable (runs on the sync thread)."""
        result = self.backend.analyze(session, analyzers)
        self.publish("analysis_done", session_id=result.session_id, queued=True)

    # --- actions (shared by shortcuts, API and CLI) ---------------------------------------

    def start(self, mode: str = "analysis", analyzers: list[str] | None = None) -> Recording:
        meta: dict[str, Any] = {}
        if mode == "practice":
            if not self.practice_target:
                self._default_target()
            meta = dict(self.practice_target)  # type: ignore[arg-type]
            backend = self.backend  # fail now (e.g. not connected) rather than after recording
        else:
            self.analyzers_for_next = analyzers
        recording = self.engine.start_recording(mode=mode, meta=meta)
        if mode == "practice" and backend.remote:
            # Open the stream while the user starts talking; audio follows from `_tick`.
            threading.Thread(target=backend.practice_begin, args=(meta["text"],), daemon=True).start()
        return recording

    def stop(self) -> Recording:
        recording = self.engine.stop_recording()
        if recording.mode == "practice":
            meta = recording.meta
            self._submit("practice", lambda: self._practice(meta["text"], recording, meta.get("session_id")))
        else:
            analyzers = self.analyzers_for_next
            self._enqueue(recording.session_id)
            self._submit("analysis", lambda: self._analyze(recording, analyzers))
        return recording

    def _practice(self, target: str, recording: Recording, session_id: str | None) -> Any:
        try:
            attempt = self.backend.practice(target, recording, session_id)
        except ZephvoxError as e:
            if "practice" in self._shortcut_pending:
                self._shortcut_pending.discard("practice")
                self.notify(f"Couldn't score “{target}”", str(e))
            raise
        self._announce_practice(attempt)
        return attempt

    def _analyze(self, recording: Recording | Session | Path, analyzers: list[str] | None):
        from ..client.remote import QueuedOffline

        sid = getattr(recording, "session_id", None) or getattr(recording, "id", None)
        with self._jobs_lock:
            if sid in self.waiting:
                self.waiting.remove(sid)
            self.analyzing = sid
        self.publish("sessions_changed")  # it moves from "waiting" to "analyzing" in the lists
        try:
            result = self.backend.analyze(recording, analyzers)
        except QueuedOffline as e:
            self.publish("warning", message=str(e).split(" (")[0])
            self.publish("sessions_changed")
            self.sync_soon()
            return None
        except ZephvoxError as e:
            if "analysis" in self._shortcut_pending:
                self._shortcut_pending.discard("analysis")
                self.notify("Couldn't analyze your recording", str(e))
            self.publish("sessions_changed")  # it's kept, and listed as not analyzed
            raise
        finally:
            self.analyzing = None
        self.publish("analysis_done", session_id=result.session_id)
        self._announce_analysis(result)
        self.sync_soon()
        return result

    def _enqueue(self, session_id: str | None) -> None:
        if session_id:
            with self._jobs_lock:
                if session_id not in self.waiting and session_id != self.analyzing:
                    self.waiting.append(session_id)

    def cancel(self) -> Recording:
        recording = self.engine.cancel_recording()
        if recording.mode == "practice" and self._backend is not None:
            self._backend.practice_cancel()
        return recording

    def toggle(self, mode: str = "analysis", analyzers: list[str] | None = None) -> Recording:
        status = self.engine.recording_status()
        if status is None:
            return self.start(mode, analyzers)
        if status["mode"] != mode:
            # A practice shortcut must never end (and analyze) a normal recording, or vice versa.
            raise RecordingError(f"A {status['mode']} recording is in progress; stop or cancel it first")
        return self.stop()

    def fire(self, action: str) -> None:
        """Run a shortcut action; errors become events instead of exceptions (no caller to catch them).
        The page may not be visible, so what happens is also shown as a desktop notification."""
        print(f"· shortcut: {action}", file=sys.stderr, flush=True)
        keys = self.engine.config.shortcuts.get(action, "")
        try:
            was_recording = self.engine.is_recording()
            if action == "toggle_recording":
                self.toggle("analysis")
                if was_recording:
                    self._shortcut_pending.add("analysis")
                    self.notify("Analyzing your recording…", "You'll get a note when it's ready.")
                else:
                    self.notify("Recording", f"Speak now. Press {keys} again to stop and analyze.")
            elif action == "practice_attempt":
                self.toggle("practice")
                if not was_recording:
                    self._shortcut_pending.add("practice")
                    word = (self.practice_target or {}).get("text", "")
                    self.notify(f"Say “{word}”", "It stops by itself when you stop talking.")
            elif action == "cancel":
                if was_recording:
                    self.cancel()
                    self.notify("Recording cancelled")
            else:
                raise ZephvoxError(f"Unknown action {action!r}")
        except ZephvoxError as e:
            self.publish("action_error", action=action, message=str(e))
            self.notify("zephvox", str(e))

    def _announce_practice(self, attempt: Any) -> None:
        if "practice" not in self._shortcut_pending:
            return
        self._shortcut_pending.discard("practice")
        if attempt.status == "no_speech":
            self.notify(f"“{attempt.target}”: nothing heard", "Try again a little louder or closer to the microphone.")
        elif attempt.status == "different_word":
            self.notify(f"“{attempt.target}”: that sounded like another word", "Listen to the example and try again.")
        elif attempt.passed:
            self.notify(f"“{attempt.target}”: {attempt.score} ✓", "Passed: great pronunciation!")
        else:
            issue = next((w for w in attempt.words if not w.get("passed")), None)
            hint = (issue or {}).get("tip") or (issue or {}).get("explanation") or "Try again."
            self.notify(f"“{attempt.target}”: {attempt.score}", hint)

    def _announce_analysis(self, result: Any) -> None:
        if "analysis" not in self._shortcut_pending:
            return
        self._shortcut_pending.discard("analysis")
        counts = {name: len(result.by_analyzer(name)) for name in result.analyzers}
        found = ", ".join(f"{n} {name}" for name, n in counts.items()) or "nothing to check"
        self.notify("Your report is ready", f"Found {found}. Open zephvox to see it.")

    def reanalyze(self, session_id: str, analyzers: list[str] | None) -> Future:
        """Analyze an existing session again, in place (e.g. after tuning thresholds)."""
        session = self.engine.sessions.get(session_id)
        self._enqueue(session.id)
        return self._submit("analysis", lambda: self._analyze(session, analyzers))

    def analyze_file(self, path: Path, analyzers: list[str] | None) -> Future:
        return self._submit("analysis", lambda: self._analyze_path(path, analyzers))

    def _analyze_path(self, path: Path, analyzers: list[str] | None):
        try:
            return self._analyze(path, analyzers)
        finally:
            path.unlink(missing_ok=True)

    def set_practice_target(self, text: str | None, session_id: str | None = None) -> None:
        self.practice_target = {"text": text, "session_id": session_id} if text else None
        data_dir = self.engine.config.engine.data_dir
        stored = load_user_settings(data_dir)
        stored["practice_target"] = self.practice_target
        save_user_settings(data_dir, stored)
        self.publish("practice_target", target=self.practice_target)

    def _default_target(self) -> None:
        """No word chosen yet: practice the top one from "Words to work on"."""
        weak = self.engine.weak_words(1)
        if not weak:
            raise RecordingError("Pick a word to practice first, in zephvox's Practice page")
        self.set_practice_target(weak[0]["target"])

    # --- settings -------------------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        from ..engine.editable import overlay_of

        overlay = overlay_of(load_user_settings(self.engine.config.engine.data_dir)).get("provider", {})

        def chosen(kind: str, field: str) -> dict[str, Any]:
            """The driver of `kind` whose `field` was picked in the page, e.g. {provider, device}."""
            for name in self.engine.registry.names(kind):
                if field in overlay.get(name, {}):
                    return {"provider": name, field: overlay[name][field]}
            return {}

        return {
            "capture": chosen("capture", "device"),
            "tts": chosen("tts", "voice"),
            "analyzers": self.engine.config.engine.analyzers,
            "shortcuts": self.engine.config.shortcuts,
        }

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """The microphone, voice and checks (the page's first controls); each is an option change."""
        edits: list[dict[str, Any]] = []
        for key, field in (("capture", "device"), ("tts", "voice")):
            chosen = changes.get(key)
            if isinstance(chosen, dict) and chosen.get("provider"):
                path = ["provider", chosen["provider"], field]
                edits.append({"path": path, "reset": True} if chosen.get(field) is None
                             else {"path": path, "value": chosen[field]})  # fmt: skip
        if changes.get("analyzers"):
            edits.append({"path": ["engine", "analyzers"], "value": list(changes["analyzers"])})
        if edits:
            self.update_config(edits)
        return self.settings()

    # --- every option (the Settings page) ---------------------------------------------------

    def _base_config(self) -> Config:
        """The config without the page's changes: defaults, profile, config.toml and `--set`."""
        current = self.engine.config
        source = current.source
        if source is None:
            return self._initial_config
        base = load_config(source, page=False) if Path(source).exists() else Config(source=source)
        return base.with_overrides(current.overrides) if current.overrides else base

    def _reconfigure(self) -> None:
        """Rebuild the config (base + the page's changes) and let changed drivers start fresh."""
        old = self.engine.config
        base = self._base_config()
        new = apply_user_settings(base, load_user_settings(base.engine.data_dir))
        self.engine.config = new
        for kind in PROVIDER_KINDS:
            names = self.engine.registry.names(kind)
            changed = old.providers.get(kind) != new.providers.get(kind) or any(
                old.provider_settings(n) != new.provider_settings(n) for n in names)  # fmt: skip
            if not changed:
                continue
            if kind == "triggers" or (kind == "capture" and self.engine.is_recording()):
                continue  # shortcuts restart below; a recording keeps its microphone
            self.engine.release(kind)
        self.notify.enabled = new.engine.notifications
        if self._sharing is not None and self._sharing.running and (
                (old.server.host, old.server.port) != (new.server.host, new.server.port)):  # fmt: skip
            self._sharing.stop()
            self._sharing = None
            threading.Thread(target=self._start_sharing_quietly, name="sharing-restart", daemon=True).start()
        changed = {a: k for a, k in new.shortcuts.items() if old.shortcuts.get(a) != k}
        if old.providers.get("triggers") != new.providers.get("triggers"):
            self._restart_triggers()
        elif changed:
            provider = self._trigger_provider
            if provider is not None and provider.rebind(changed):  # e.g. KDE: set in place
                self.publish("triggers", **self.triggers, bindings=self.bindings())
            else:
                self._restart_triggers()

    def bindings(self) -> dict[str, str]:
        """The shortcut keys really in use: the desktop's, when it may have changed them, else the config's."""
        configured = dict(self.engine.config.shortcuts)
        provider = self._trigger_provider
        actual = provider.current_bindings(list(configured)) if provider is not None else None
        return {**configured, **(actual or {})}

    def _restart_triggers(self) -> None:
        old, self._trigger_provider = self._trigger_provider, None
        if old is not None:
            try:
                old.stop()
            except Exception:  # replaced anyway
                pass
        self.engine.release("triggers")
        threading.Thread(target=self._start_triggers, name="triggers", daemon=True).start()

    def _validate_drivers(self, config: Config, touched: set[tuple[str, str]]) -> None:
        """Driver and check options are free-form in the config; check the changed ones against their fields."""
        from pydantic import ValidationError

        registry = self.engine.registry
        for name, values in config.provider.items():
            if ("provider", name) not in touched:
                continue
            kind = next((k for k in PROVIDER_KINDS if name in registry.names(k)), None)
            if kind is None:
                continue
            try:
                registry.get(kind, name).Settings(**values)
            except ValidationError as e:
                raise ZephvoxError(f"{name}: {e.errors()[0]['loc'][0]} {e.errors()[0]['msg'].lower()}") from e
        for name, values in config.analyzer.items():
            if ("analyzer", name) not in touched or name not in registry.names("analyzers"):
                continue
            try:
                registry.get("analyzers", name).Settings(**values)
            except ValidationError as e:
                raise ZephvoxError(f"{name}: {e.errors()[0]['loc'][0]} {e.errors()[0]['msg'].lower()}") from e

    def update_config(self, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """Change options (`[{path, value}]` or `[{path, reset: true}]`); checked before anything is saved."""
        from ..engine import editable

        data_dir = self.engine.config.engine.data_dir
        stored = editable.apply_changes(load_user_settings(data_dir), changes)
        candidate = apply_user_settings(self._base_config(), stored)  # raises on a bad value
        self._validate_drivers(candidate, {tuple(c["path"][:2]) for c in changes if len(c.get("path") or ()) >= 2})
        if not candidate.engine.analyzers:
            raise ZephvoxError("At least one check must stay on.")
        save_user_settings(data_dir, stored)
        self._reconfigure()
        self.publish("settings_changed", settings=self.settings())
        self.publish("config_changed")
        return self.config_info()

    MODEL_NAME = re.compile(r"^[\w][\w.:/-]{0,127}$")
    ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def pull_model(self, name: str) -> dict[str, Any]:
        """Download an Ollama model in the background (`ollama pull`), with progress as events."""
        from ..install import ollama

        if not self.MODEL_NAME.match(name):
            raise ZephvoxError(f"{name!r} isn't a model name")
        current = self.downloads.get(name)
        if current and current["running"]:
            return current
        binary = ollama.find()
        if binary is None:
            raise ZephvoxError("Ollama isn't installed. Install it from https://ollama.com, then try again.")
        state: dict[str, Any] = {"name": name, "running": True, "ok": None, "percent": None, "line": "Starting…"}
        self.downloads[name] = state
        self.publish("model_download", **state)

        def run() -> None:
            ok = False
            try:
                with ollama.serving(binary):
                    proc = subprocess.Popen([binary, "pull", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    assert proc.stdout is not None
                    buffer, last = b"", 0.0
                    while chunk := proc.stdout.read1(4096):
                        buffer += chunk
                        *lines, buffer = re.split(rb"[\r\n]", buffer)
                        for raw in lines:
                            line = self.ANSI.sub("", raw.decode(errors="replace")).strip()
                            if not line:
                                continue
                            state["line"] = line
                            if m := re.search(r"(\d{1,3})%", line):
                                state["percent"] = int(m.group(1))
                        if time.monotonic() - last > 0.5:
                            last = time.monotonic()
                            self.publish("model_download", **state)
                    ok = proc.wait() == 0
            except (RuntimeError, OSError) as e:
                state["line"] = str(e)
            state.update(running=False, ok=ok, percent=100 if ok else state["percent"])
            if ok:
                state["line"] = "Downloaded"
                self.engine.release("llm")
            self.publish("model_download", **state)

        threading.Thread(target=run, name=f"pull-{name}", daemon=True).start()
        return state

    # --- sharing this computer (the server inside the app) and its admin -------------------------

    def _db(self) -> Any:
        """The server's database: the running server's, or one opened just for admin."""
        if self._sharing is not None and self._sharing.server is not None:
            return self._sharing.server.db
        if self._admin_db is None:
            from ..server.db import Database
            from ..server.service import server_dir

            self._admin_db = Database(server_dir(self.engine.config) / "server.db")
        return self._admin_db

    def _start_sharing_quietly(self) -> None:
        try:
            self.start_sharing()
        except ZephvoxError as e:
            self.publish("warning", message=f"Couldn't share this computer: {e}")

    def start_sharing(self) -> dict[str, Any]:
        from ..server.embedded import EmbeddedServer

        if self.engine.config.remote.mode == "remote":
            raise ZephvoxError("This computer uses a server for analysis; it can't share its own engine.")
        if self._admin_db is not None:
            self._admin_db.close()
            self._admin_db = None
        if self._sharing is None:
            self._sharing = EmbeddedServer(self.engine)
        settings = self.engine.config.server
        self._sharing.start(settings.host, settings.port)
        self.publish("sharing_changed")
        return self.sharing_info()

    def stop_sharing(self) -> dict[str, Any]:
        if self._sharing is not None:
            self._sharing.stop()
            self._sharing = None
        self.publish("sharing_changed")
        return self.sharing_info()

    def set_sharing(self, on: bool) -> dict[str, Any]:
        """The Share switch: start or stop now, and remember it for the next start of the app."""
        self.start_sharing() if on else self.stop_sharing()
        self.update_config([{"path": ["server", "share"], "value": True} if on else {"path": ["server", "share"], "reset": True}])
        return self.sharing_info()

    def sharing_info(self) -> dict[str, Any]:
        from ..server import admin
        from ..server.embedded import EmbeddedServer

        state = self._sharing.info() if self._sharing is not None else EmbeddedServer(self.engine).info()
        settings = self.engine.config.server
        return {**state, "enabled": settings.share, "host_setting": settings.host, "port_setting": settings.port,
                "remote": self.engine.config.remote.mode == "remote", **admin.overview(self._db(), self.engine.config)}  # fmt: skip

    def sharing_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """Accounts, invites, devices, limits, jobs and webhook secrets (as `zephvox server …`)."""
        try:
            return self._sharing_action(action, body)
        except KeyError as e:
            raise ZephvoxError(f"{action} needs {e.args[0]!r}") from None

    def _sharing_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from ..server import admin

        db, now, out = self._db(), time.time(), {}
        if action == "add_account":
            account = admin.add_account(db, str(body.get("name", "")), now)
            out["invite"] = admin.invite(db, account.id, now, 7 * 86400)
        elif action == "remove_account":
            admin.remove_account(db, self.engine.config, str(body["account"]))
        elif action == "invite":
            days = float(body.get("days", 7))
            if not 0 < days <= 365:
                raise ZephvoxError("an invite can be valid for 1 to 365 days")
            out["invite"] = admin.invite(db, str(body["account"]), now, days * 86400)
        elif action == "revoke_device":
            admin.revoke_device(db, str(body["device"]), now)
        elif action == "set_limits":
            admin.set_limits(db, body.get("account"), dict(body.get("changes") or {}))
        elif action == "cancel_job":
            admin.cancel_job(db, str(body["job"]), now)
        elif action == "rotate_secret":
            admin.rotate_webhook_secret(db, str(body["account"]))
        else:
            raise ZephvoxError(f"unknown action {action!r}")
        self.publish("sharing_changed")
        return {**self.sharing_info(), **out}

    def service_action(self, install: bool) -> dict[str, Any]:
        """The server as a background service, without the app (`zephvox server install-service`)."""
        from ..server import os_service

        if not install:
            removed = os_service.remove()
            return {"service": "removed" if removed else "none"}
        config = self.engine.config
        cmd = os_service.command(sys.executable, config.source if config.source and config.source.exists() else None,
                                 config.server.host, config.server.port)  # fmt: skip
        try:
            where = os_service.install(cmd, config.source.parent if config.source else Path.cwd())
        except (OSError, subprocess.CalledProcessError) as e:
            raise ZephvoxError(f"couldn't install the service: {e}") from e
        return {"service": str(where)}

    def config_info(self) -> dict[str, Any]:
        from ..engine import editable

        config = self.engine.config
        availability = {(s.kind, s.name): (s.available, s.reason) for s in self.engine.list_providers()}
        root = project_root()
        info = editable.describe(
            config, self._base_config(), editable.overlay_of(load_user_settings(config.engine.data_dir)),
            editable.read_file(config.source), self.engine.registry, availability,
            root / "config.example.toml" if root else None,
        )  # fmt: skip
        info["shortcuts"]["values"] = self.bindings()  # what really works, even if changed in the desktop's settings
        return {"missing_models": self.missing_models(), "downloads": self.downloads, **info}

    # --- client mode: the server, this device's key, installing the engine here ------------------

    def engine_installed(self) -> bool:
        """Can analysis run on this computer (models extra + PyTorch installed)?"""
        import importlib.util

        return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))

    def remote_info(self, check: bool = True) -> dict[str, Any]:
        from ..client import credentials
        from ..client.backend import client_for
        from ..client.remote import ServerError, is_insecure

        remote = self.engine.config.remote
        data_dir = self.engine.config.engine.data_dir
        creds = credentials.load(data_dir, remote.url) if remote.url else None
        info: dict[str, Any] = {
            "mode": remote.mode,
            "url": remote.url,
            "headers": sorted(remote.headers),  # names only: values may be secrets
            "sync": remote.syncing,
            "connected": creds is not None,
            "device": creds.device_name if creds else None,
            "account": creds.account if creds else None,
            "insecure": bool(remote.url) and is_insecure(remote.url),
            "engine_installed": self.engine_installed(),
            "install": {k: (v[-200:] if k == "log" else v) for k, v in self.install.items()},
            "sync_status": dict(self._sync.status) if self._sync is not None else None,
            "reachable": None,
            "error": None,
            "server": None,
        }
        if check and remote.url:
            client = client_for(self.engine)
            try:
                health = client.health()
                info["server"] = {k: health.get(k) for k in ("version", "analyzers", "installed", "tts", "source")}
                info["reachable"] = True
                if creds:
                    account = client.account()
                    info["account"] = account["name"]
            except ServerError as e:
                info["reachable"] = e.status is not None  # it answered, but said no
                info["error"] = str(e)
            finally:
                client.close()
        return info

    def _save_remote(self, changes: dict[str, Any]) -> None:
        data_dir = self.engine.config.engine.data_dir
        stored = load_user_settings(data_dir)
        remote = {**(stored.get("remote") or {}), **changes}
        stored["remote"] = remote
        save_user_settings(data_dir, stored)
        self.engine.config = apply_user_settings(self.engine.config, stored)
        self._reset_backend()

    def set_remote(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Mode, server address, headers or sync. Local mode needs the engine installed here."""
        if self.busy or self.engine.is_recording():
            raise ZephvoxError("finish the current recording or analysis first")
        allowed = {k: v for k, v in changes.items() if k in ("mode", "url", "headers", "sync", "device_name")}
        if allowed.get("mode") == "local" and not self.engine_installed():
            raise ZephvoxError("the engine isn't installed on this computer yet: install it first")
        if allowed.get("mode") == "remote" and not (allowed.get("url") or self.engine.config.remote.url):
            raise ZephvoxError("enter the server's address first")
        if "url" in allowed:
            allowed["url"] = (allowed["url"] or "").strip().rstrip("/") or None
        self._save_remote(allowed)
        info = self.remote_info()
        self.publish("remote_changed", remote=info)
        return info

    def connect(self, url: str, invite: str, device_name: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Redeem an invite: this device gets its own key, and analysis moves to the server."""
        from ..client import credentials
        from ..client.remote import ServerClient

        url = url.strip().rstrip("/")
        headers = self.engine.config.remote.headers if headers is None else headers
        client = ServerClient(url, headers)
        try:
            client.health()
            data = client.redeem(invite, device_name)
        finally:
            client.close()
        data_dir = self.engine.config.engine.data_dir
        credentials.save(data_dir, credentials.Credentials(url, data["key"], data["device"]["id"],
                                                           data["device"]["name"], data.get("account") or ""))  # fmt: skip
        self._save_remote({"mode": "remote", "url": url, "headers": headers, "device_name": device_name})
        info = self.remote_info()
        self.publish("remote_changed", remote=info)
        return info

    def rotate_key(self) -> dict[str, Any]:
        """A new key for this device; the old one stops working on the server at once."""
        from ..client import credentials
        from ..client.backend import client_for

        data_dir = self.engine.config.engine.data_dir
        creds = credentials.load(data_dir, self.engine.config.remote.url)
        if creds is None:
            raise ZephvoxError("this device isn't connected to a server")
        client = client_for(self.engine)
        try:
            creds.key = client.rotate()
        finally:
            client.close()
        credentials.save(data_dir, creds)
        self._reset_backend()
        return self.remote_info(check=False)

    def forget_key(self) -> dict[str, Any]:
        """Disconnect: revoke this device on the server (if reachable) and delete the local key."""
        from ..client import credentials
        from ..client.backend import client_for
        from ..client.remote import ServerError

        data_dir = self.engine.config.engine.data_dir
        creds = credentials.load(data_dir)
        if creds is not None and creds.device_id:
            client = client_for(self.engine, creds.url)
            try:
                client.revoke(creds.device_id)
            except ServerError:
                pass  # the key is deleted here either way
            finally:
                client.close()
        credentials.clear(data_dir)
        changes: dict[str, Any] = {"mode": "local"} if self.engine_installed() else {}
        self._save_remote(changes)
        info = self.remote_info(check=False)
        self.publish("remote_changed", remote=info)
        return info

    def start_install(self, then_local: bool = True, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Install the engine on this computer (`zephvox setup --role full`) in the background."""
        import subprocess
        import sys

        from ..install.ollama import find as find_ollama

        if self.install["running"]:
            raise ZephvoxError("the engine is already being installed")
        command = [sys.executable, "-m", "zephvox.cli", "setup", "--role", "full", "--yes", "--no-launcher"]
        if self.engine.config.source:
            command += ["--config", str(self.engine.config.source)]
        if find_ollama() is None:
            command.append("--no-ollama")  # installing Ollama may need a password: do that in a terminal
        self.install.update(running=True, ok=None, log=[])
        self.publish("install", **self.install)
        popen = runner or subprocess.Popen

        def run() -> None:
            ok = False
            try:
                proc = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    line = line.rstrip()
                    self.install["log"].append(line)
                    self.publish("install_output", line=line)
                ok = proc.wait() == 0
            except OSError as e:
                self.install["log"].append(f"error: {e}")
            import importlib

            importlib.invalidate_caches()  # the new packages must be found without a restart
            if ok and then_local and self.engine_installed():
                path = self._config_path()
                if path.exists():  # setup wrote the chosen profile there
                    self._reconfigure()
                self._save_remote({"mode": "local"})
            self.install.update(running=False, ok=ok)
            self.publish("install", **{**self.install, "log": self.install["log"][-20:]})
            self.publish("remote_changed", remote=self.remote_info(check=False))

        threading.Thread(target=run, name="install", daemon=True).start()
        return self.install

    # --- profiles -------------------------------------------------------------------------

    MODEL_KINDS = ("stt", "phonemes", "g2p", "tts", "llm")

    @functools.cached_property
    def hardware(self) -> hardware.Hardware:
        return hardware.detect()

    def _config_path(self) -> Path:
        return self.engine.config.source or default_config_path()

    def profiles(self) -> dict[str, Any]:
        from ..install.state import InstallState

        root = project_root()
        hw = self.hardware
        listed = [
            {"name": p.name, "title": p.title, "description": p.description, "gpu": p.gpu,
             "llm_model": p.llm_model, "accelerator": profiles.accelerator(p, hw)}
            for p in profiles.PROFILES.values()
        ]  # fmt: skip
        path = self._config_path()
        if profiles.custom_path(path).exists():  # made with setup's Custom questions
            from ..install import custom
            from ..install.wizard import preview_config

            mine = profiles.get(profiles.CUSTOM, path)
            config = preview_config(path, profiles.CUSTOM, root / "config.example.toml" if root else path)
            listed.append({"name": mine.name, "title": "Custom", "gpu": mine.gpu, "llm_model": mine.llm_model,
                           "description": custom.describe(custom.defaults_from(config, hw)) + ". Change it with "
                           "`zephvox setup`.", "accelerator": profiles.accelerator(mine, hw)})  # fmt: skip
        return {
            "current": self.engine.config.profile,
            "recommended": profiles.recommend(hw),
            "hardware": hw.summary(),
            "installed_accelerator": InstallState.load(root).accelerator if root else None,
            "config_path": str(path),
            "profiles": listed,
        }

    def _profile_needs(self, profile: profiles.Profile, config: Any) -> tuple[list[str], float]:
        """What switching to `profile` (giving `config`) still needs here, and the download size."""
        from ..install import ollama
        from ..install.download import plan_downloads
        from ..install.wizard import downloaded

        needs, size = [], 0.0
        root = project_root()
        installed = installed_accelerator(root)
        accelerator = profiles.accelerator(profile, self.hardware)
        # A GPU build also runs on the CPU; only the cpu build can't serve a GPU profile.
        if installed == "cpu" and accelerator != "cpu":
            needs.append(f"This profile needs PyTorch for {accelerator}: run `zephvox setup --profile {profile.name}`.")
        for d in plan_downloads(config):
            if d.source == "ollama":
                if "grammar" in config.engine.analyzers and not ollama.has_model(d.name):
                    needs.append(f"Grammar needs its model: run `ollama pull {d.name}`"
                                 + (f" (~{d.approx_gb:g} GB)." if d.approx_gb else "."))  # fmt: skip
                    size += d.approx_gb or 0
            elif not downloaded(d):
                needs.append(f"{d.name} downloads on first use" + (f" (~{d.approx_gb:g} GB)." if d.approx_gb else "."))
                size += d.approx_gb or 0
        return needs, round(size, 1)

    def missing_models(self, config: Config | None = None) -> list[str]:
        """Grammar models the config uses that aren't downloaded (when grammar is on and runs on Ollama)."""
        from ..install import ollama
        from ..install.download import plan_downloads

        config = config or self.engine.config
        if "grammar" not in config.engine.analyzers:
            return []
        return [d.name for d in plan_downloads(config) if d.source == "ollama" and not ollama.has_model(d.name)]

    def preview_profile(self, name: str) -> dict[str, Any]:
        """What switching to a profile would change, without switching (for the page's confirmation)."""
        from ..install.wizard import preview_config

        path = self._config_path()
        profile = profiles.get(name, path)
        root = project_root()
        config = preview_config(path, name, root / "config.example.toml" if root else path)
        needs, size = self._profile_needs(profile, config)
        return {"name": name, "llm_model": profile.llm_model if name != profiles.CUSTOM else
                (config.provider_settings("openai-compat").get("model", "qwen3:8b")
                 if "grammar" in config.engine.analyzers else None),
                "accelerator": profiles.accelerator(profile, self.hardware), "needs": needs, "download_gb": size,
                "models": self.missing_models(config)}  # fmt: skip

    def set_profile(self, name: str) -> dict[str, Any]:
        """Switch profile now. Models load on next use; what can't happen here is listed in `needs`."""
        profile = profiles.get(name, self._config_path())
        if self.busy or self.engine.is_recording():
            raise ZephvoxError("finish the current recording or analysis first")
        root = project_root()
        path = self._config_path()
        choose_profile(path, name, template=root / "config.example.toml" if root else None)
        self._reconfigure()
        for kind in self.MODEL_KINDS:
            self.engine.release(kind)
        info = self.profiles()
        needs, _ = self._profile_needs(profile, self.engine.config)
        self.publish("settings_changed", settings=self.settings())
        return {**info, "needs": needs, "models": self.missing_models()}

    def hardware_info(self) -> dict[str, Any]:
        """This computer, detected the same way as setup (so names and sizes match)."""
        hw = self.hardware
        gpu = hw.gpu
        return {"cpu": hw.cpu, "cores": hw.cores, "ram_gb": hw.ram_gb,
                "gpu": None if gpu is None else {"name": gpu.name, "vram_gb": gpu.vram_gb, "backend": gpu.backend},
                "accelerator": hw.accelerator, "notes": hw.notes}  # fmt: skip

    # --- background: levels, auto-stop, shortcuts -------------------------------------------

    def _tick(self) -> None:
        heard_speech, quiet_since = False, None
        while not self._stop.wait(LEVEL_INTERVAL_S):
            try:
                status = self.engine.recording_status()
            except (ZephvoxError, OSError, ValueError):
                status = None
            if status is None:
                heard_speech, quiet_since = False, None
                continue
            self.publish("level", level=status["level"], elapsed=status["elapsed"], mode=status["mode"])
            if status["mode"] != "practice":
                continue
            if self._backend is not None and self._backend.remote:
                self._backend.practice_feed(Path(status["path"]))  # stream while the user speaks
            level = status["level"] or 0.0
            cfg = self.engine.config.engine
            if level >= SPEECH_LEVEL:
                heard_speech, quiet_since = True, None
            elif heard_speech and level < SILENCE_LEVEL:
                quiet_since = quiet_since or time.monotonic()
            too_long = status["elapsed"] >= cfg.practice_max_s
            silent_long = (cfg.practice_auto_stop_s > 0 and quiet_since is not None
                           and time.monotonic() - quiet_since >= cfg.practice_auto_stop_s)  # fmt: skip
            if too_long or silent_long:
                heard_speech, quiet_since = False, None
                try:
                    self.stop()
                except ZephvoxError as e:
                    self.publish("action_error", action="auto_stop", message=str(e))

    def _start_triggers(self) -> None:
        bindings = {a: k for a, k in self.engine.config.shortcuts.items() if a in ACTIONS and k}
        try:
            provider = self.engine.provider("triggers")
            self.triggers.update(driver=provider.name, description=provider.describe())
            provider.start(bindings, self.fire)
            self._trigger_provider = provider
            self.triggers.update(active=True, error=None)
        except (ProviderUnavailable, ZephvoxError) as e:
            self.triggers.update(active=False, error=str(e))
        self.publish("triggers", **self.triggers, bindings=self.bindings())

    # --- lifecycle --------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "recording": self.engine.recording_status(),
            "practice_target": self.practice_target,
            "triggers": {**self.triggers, "bindings": self.bindings()},
            "engine": {"mode": self.engine.config.remote.mode, "url": self.engine.config.remote.url},
            "hardware": self.hardware_info(),
            "pid": os.getpid(),
        }

    def close(self) -> None:
        self._stop.set()
        self.models.cancel()
        if self._sharing is not None:
            self._sharing.stop()
            self._sharing = None
        if self._admin_db is not None:
            self._admin_db.close()
            self._admin_db = None
        if self.engine.is_recording():
            try:
                self.engine.cancel_recording()
            except ZephvoxError:
                pass
        if self._trigger_provider is not None:
            try:
                self._trigger_provider.stop()
            except Exception:  # shutting down anyway
                pass
        self._worker.shutdown(wait=True, cancel_futures=True)
        self._reset_backend()
        if self._sync is not None:
            self._sync.stop()
            self._sync = None
        self.engine.close()
