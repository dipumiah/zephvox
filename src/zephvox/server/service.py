"""The server's moving parts in one object: database, job runner, webhooks, practice threads, TTS."""

from __future__ import annotations

import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from ..engine import audio
from ..engine.config import Config, ServerLimits, ServerSettings
from ..engine.engine import Engine
from ..engine.errors import ProviderUnavailable, ZephvoxError
from ..engine.text import normalize_word
from ..providers.base import TTSProvider
from . import limits
from .db import Account, Database, Job
from .jobs import JobRunner
from .webhooks import Deliverer

SOURCE_URL = "https://github.com/dipumiah/zephvox"  # AGPL: users of a hosted copy can get its source
TTS_CACHE = 256
RUNNABLE_TTL_S = 60.0


def zephvox_version() -> str:
    try:
        return version("zephvox")
    except PackageNotFoundError:
        return "unknown"


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def server_dir(config: Config) -> Path:
    configured = config.server.data_dir
    return Path(configured).expanduser() if configured else config.engine.data_dir / "server"


class Server:
    def __init__(self, engine: Engine, settings: ServerSettings | None = None, clock: Callable[[], float] = time.time,
                 start: bool = True, http: httpx.Client | None = None, owns_engine: bool = True):  # fmt: skip
        self.engine = engine
        self.owns_engine = owns_engine  # False inside the app ("Share this computer"): the app keeps using it
        self.settings = settings or engine.config.server
        self.clock = clock
        self.root = Path(self.settings.data_dir).expanduser() if self.settings.data_dir else server_dir(engine.config)
        self.db = Database(self.root / "server.db")
        self.rate = limits.RateLimiter()
        self.jobs = JobRunner(engine, self.db, self.root / "jobs", clock, self.settings.job_ttl_hours * 3600,
                              on_finished=self._job_finished)  # fmt: skip
        self.webhooks = Deliverer(self.db, self._webhook_body, self._webhook_secret, clock,
                                  self.settings.webhook_allow_private, client=http)  # fmt: skip
        self.practice_pool = ThreadPoolExecutor(self.settings.practice_workers, thread_name_prefix="practice")
        self._tts: dict[str, TTSProvider] = {}
        self._tts_audio: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._tts_lock = threading.Lock()
        self._runnable: tuple[float, list[str]] | None = None
        if start:
            self.jobs.start()
            self.webhooks.start()

    def close(self) -> None:
        self.jobs.stop()
        self.webhooks.stop()
        self.practice_pool.shutdown(wait=False, cancel_futures=True)
        if self.owns_engine:
            self.engine.close()
        self.db.close()

    # --- accounts -----------------------------------------------------------------------

    def limits(self, account: Account | None = None) -> ServerLimits:
        return limits.effective(self.settings.limits, self.db, account)

    def account_view(self, account: Account) -> dict[str, Any]:
        return {
            "id": account.id,
            "name": account.name,
            "limits": self.limits(account).model_dump(),
            "usage": {"active_jobs": self.db.active_jobs(account.id),
                      "storage_mb": round(self.db.storage_bytes(account.id) / (1024 * 1024), 1)},
            "webhook_secret": account.webhook_secret,
        }

    # --- jobs ---------------------------------------------------------------------------

    def job_view(self, job: Job, result: bool = True) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": job.id,
            "status": job.status,
            "created": iso(job.created),
            "started": iso(job.started),
            "finished": iso(job.finished),
            "expires_at": iso(job.expires),
            "analyzers": job.analyzers,
            "filename": job.filename,
            "duration": job.duration,
            "webhook_url": job.webhook_url,
            "error": job.error,
        }
        if result and job.status == "done":
            view["result"] = self.jobs.result(job.id)
        return view

    def _job_finished(self, job: Job) -> None:
        if job.webhook_url:
            self.db.add_delivery(job.id, "job.completed" if job.status == "done" else "job.failed", self.clock())
            self.webhooks.notify()

    def _webhook_body(self, job_id: str, event: str) -> dict[str, Any] | None:
        job = self.db.job(job_id)
        return None if job is None else {"event": event, "job": self.job_view(job)}

    def _webhook_secret(self, job_id: str) -> str | None:
        job = self.db.job(job_id)
        account = self.db.account(job.account_id) if job else None
        return account.webhook_secret if account else None

    # --- practice -----------------------------------------------------------------------

    def warm_practice(self, target: str) -> None:
        """When an attempt starts: look up the expected sounds and load the models, so only the
        recognition itself is left when the speaker stops."""
        words = [w for w in (normalize_word(t) for t in target.split()) if w]
        self.engine.provider("g2p").pronounce(words)
        self.engine.provider("phonemes")

    def score_practice(self, target: str, samples: np.ndarray) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="zephvox-practice-") as tmp:
            path = Path(tmp) / "attempt.wav"
            audio.write_wav(path, samples)
            attempt = self.engine.score_practice(target, path)
        data = attempt.to_dict()
        data["audio"] = None
        return data

    # --- reference voices ---------------------------------------------------------------

    def tts(self, text: str, voice: str | None) -> bytes:
        key = (text, voice or "")
        with self._tts_lock:
            if key in self._tts_audio:
                self._tts_audio.move_to_end(key)
                return self._tts_audio[key]
        wav = self._tts_provider(voice).synthesize(text)
        with self._tts_lock:
            self._tts_audio[key] = wav
            while len(self._tts_audio) > TTS_CACHE:
                self._tts_audio.popitem(last=False)
        return wav

    def _tts_provider(self, voice: str | None) -> TTSProvider:
        with self._tts_lock:
            if (voice or "") in self._tts:
                return self._tts[voice or ""]
        reasons = []
        for name in self.engine.config.providers.get("tts", []):
            try:
                provider = self.engine.build_provider("tts", name, {"voice": voice} if voice else None)
            except Exception as e:  # unknown plugin or a setting this driver doesn't take
                reasons.append(f"{name}: {e}")
                continue
            ok, reason = provider.check_available()
            if ok:
                with self._tts_lock:
                    self._tts[voice or ""] = provider  # type: ignore[assignment]
                return provider  # type: ignore[return-value]
            reasons.append(f"{name}: {reason}")
        raise ProviderUnavailable("tts", reasons)

    # --- health -------------------------------------------------------------------------

    def installed_analyzers(self) -> list[str]:
        practice = self.engine.config.engine.practice_analyzer
        return [a for a in self.engine.registry.names("analyzers") if a != practice]

    def runnable_analyzers(self) -> list[str]:
        """Analyzers whose providers can run right now (e.g. no grammar without a language model).
        Checked at most once a minute."""
        from ..engine.planner import plan

        now = self.clock()
        if self._runnable is not None and now - self._runnable[0] < RUNNABLE_TTL_S:
            return self._runnable[1]
        out = []
        for name in self.installed_analyzers():
            cls = self.engine.registry.get("analyzers", name)
            kinds = plan([(cls.needs, cls.uses)], self.engine.artifacts).provider_kinds
            if all(self._kind_available(kind) for kind in kinds):
                out.append(name)
        self._runnable = (now, out)
        return out

    def _kind_available(self, kind: str) -> bool:
        for name in self.engine.config.providers.get(kind, []):
            try:
                if self.engine.build_provider(kind, name).check_available().ok:
                    return True
            except Exception:  # an unknown or broken plugin: try the next one
                continue
        return False

    def health(self) -> dict[str, Any]:
        analyzers = self.runnable_analyzers()
        try:
            tts = bool(self.engine.config.providers.get("tts")) and self._tts_provider(None) is not None
        except ZephvoxError:
            tts = False
        return {
            "service": "zephvox",
            "version": zephvox_version(),
            "analyzers": analyzers,  # can run now
            "installed": self.installed_analyzers(),
            "default_analyzers": self.engine.config.engine.analyzers,
            "practice": True,
            "tts": tts,
            "source": SOURCE_URL,
        }
