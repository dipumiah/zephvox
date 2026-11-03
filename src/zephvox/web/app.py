"""HTTP API + static React app for the daemon. Everything here is a thin layer over `Daemon`."""

from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..engine import audio
from ..engine.errors import ProviderUnavailable, RecordingError, ZephvoxError
from ..engine.models import SAMPLE_RATE
from ..providers.base import CaptureProvider
from .daemon import Daemon, jsonable

STATIC_DIR = Path(__file__).parent / "static"
CLIP_PAD_S = 0.15
KEEPALIVE_S = 15.0


class RecordBody(BaseModel):
    mode: str = "analysis"
    analyzers: list[str] | None = None


class FeedbackBody(BaseModel):
    verdict: str | None = "dismissed"


class TargetBody(BaseModel):
    text: str | None = None
    session_id: str | None = None


class ConfigChange(BaseModel):
    path: list[str]
    value: Any = None
    reset: bool = False


class ConfigBody(BaseModel):
    changes: list[ConfigChange]


class SharingBody(BaseModel):
    on: bool


class ModelBody(BaseModel):
    name: str


class ProfileBody(BaseModel):
    name: str


class RemoteBody(BaseModel):
    mode: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    sync: bool | None = None
    device_name: str | None = None


class ConnectBody(BaseModel):
    url: str
    invite: str
    device_name: str = "my computer"
    headers: dict[str, str] | None = None


class SettingsBody(BaseModel):
    capture: dict[str, Any] | None = None
    tts: dict[str, Any] | None = None
    analyzers: list[str] | None = None


def _wav_bytes(samples) -> bytes:
    buf = io.BytesIO()
    audio.write_wav(buf, samples)
    return buf.getvalue()


def create_app(daemon: Daemon) -> FastAPI:
    app = FastAPI(title="zephvox", docs_url="/api/docs", openapi_url="/api/openapi.json")
    engine = daemon.engine

    def session_or_404(sid: str):
        try:
            return engine.sessions.get(sid)
        except ZephvoxError:
            raise HTTPException(404, f"No session {sid!r}") from None

    def guard(fn, *args, **kwargs):
        """Engine errors → 409 with a readable message."""
        try:
            return fn(*args, **kwargs)
        except (RecordingError, ProviderUnavailable, ZephvoxError) as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/healthz")
    def healthz():
        """What the desktop shell checks before showing the page (and after every engine update)."""
        from .. import API_VERSION, __version__

        return {"ok": True, "engine_version": __version__, "api_version": API_VERSION}

    # --- live events ----------------------------------------------------------------------

    @app.get("/api/events")
    async def events(request: Request):
        queue = daemon.hub.subscribe(asyncio.get_running_loop())

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    try:
                        message = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                        yield f"data: {message}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                daemon.hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})  # fmt: skip

    # --- status & settings ------------------------------------------------------------------

    @app.get("/api/status")
    def status():
        return jsonable({**daemon.status(), "doctor": engine.doctor()})

    @app.get("/api/settings")
    def get_settings():
        return daemon.settings()

    @app.put("/api/settings")
    def put_settings(body: SettingsBody):
        return guard(daemon.update_settings, body.model_dump(exclude_none=True))

    @app.get("/api/remote")
    def get_remote(check: bool = True):
        return daemon.remote_info(check)

    @app.put("/api/remote")
    def put_remote(body: RemoteBody):
        return guard(daemon.set_remote, body.model_dump(exclude_unset=True))

    @app.post("/api/remote/connect")
    def connect(body: ConnectBody):
        return guard(daemon.connect, body.url, body.invite, body.device_name, body.headers)

    @app.post("/api/remote/rotate")
    def rotate():
        return guard(daemon.rotate_key)

    @app.delete("/api/remote/key")
    def forget():
        return guard(daemon.forget_key)

    @app.post("/api/install")
    def install():
        return guard(daemon.start_install)

    @app.get("/api/profiles")
    def get_profiles():
        return daemon.profiles()

    @app.get("/api/profile/preview")
    def preview_profile(name: str):
        return guard(daemon.preview_profile, name)

    @app.put("/api/profile")
    def put_profile(body: ProfileBody):
        return guard(daemon.set_profile, body.name)

    # --- every option (the Settings page) ----------------------------------------------------

    @app.get("/api/config")
    def get_config():
        return jsonable(guard(daemon.config_info))

    @app.put("/api/config")
    def put_config(body: ConfigBody):
        return jsonable(guard(daemon.update_config, [c.model_dump() for c in body.changes]))

    @app.get("/api/sharing")
    def sharing():
        return jsonable(guard(daemon.sharing_info))

    @app.put("/api/sharing")
    def put_sharing(body: SharingBody):
        return jsonable(guard(daemon.set_sharing, body.on))

    @app.post("/api/sharing/{action}")
    def sharing_action(action: str, body: dict[str, Any]):
        return jsonable(guard(daemon.sharing_action, action, body))

    @app.post("/api/sharing/service/{op}")
    def sharing_service(op: str):
        if op not in ("install", "remove"):
            raise HTTPException(404, "install or remove")
        return guard(daemon.service_action, op == "install")

    @app.get("/api/models/downloads")
    def model_downloads():
        return daemon.models.snapshot()

    @app.post("/api/models/downloads")
    def start_model_downloads():
        return daemon.start_model_downloads(only_if_missing=False)

    @app.delete("/api/models/downloads")
    def cancel_model_downloads():
        return daemon.cancel_model_downloads()

    @app.post("/api/models/pull")
    def pull_model(body: ModelBody):
        return guard(daemon.pull_model, body.name)

    @app.post("/api/quit")
    def quit_app(request: Request):
        """Stop this zephvox (`zephvox quit`, and `zephvox update` before it swaps files)."""
        if request.headers.get("x-requested-with") != "zephvox":
            raise HTTPException(403, "this has to come from the zephvox command")
        if daemon.request_shutdown is None:
            raise HTTPException(409, "this zephvox wasn't started with `zephvox serve`")
        threading.Timer(0.2, daemon.request_shutdown).start()  # let this answer go out first
        return {"quitting": True}

    @app.get("/api/voices")
    def voices():
        return guard(lambda: daemon.backend.voices())

    @app.get("/api/devices")
    def devices():
        capture = guard(engine.provider, "capture")
        assert isinstance(capture, CaptureProvider)
        return {
            "provider": capture.name,
            "selected": capture.settings.model_dump().get("device"),
            "devices": jsonable(capture.list_devices()),
        }

    # --- recording (same actions as the shortcuts) ------------------------------------------

    @app.post("/api/record/start")
    def record_start(body: RecordBody):
        return jsonable(guard(daemon.start, body.mode, body.analyzers))

    @app.post("/api/record/stop")
    def record_stop():
        return jsonable(guard(daemon.stop))

    @app.post("/api/record/cancel")
    def record_cancel():
        return jsonable(guard(daemon.cancel))

    @app.post("/api/record/toggle")
    def record_toggle(body: RecordBody):
        return jsonable(guard(daemon.toggle, body.mode, body.analyzers))

    @app.post("/api/analyze")
    async def analyze_upload(file: UploadFile = File(...), analyzers: str | None = Form(None)):
        suffix = Path(file.filename or "upload").suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
        names = [a for a in (analyzers or "").split(",") if a] or None
        daemon.analyze_file(Path(tmp.name), names)
        return {"queued": True}

    # --- sessions ----------------------------------------------------------------------------

    @app.get("/api/sessions")
    def sessions():
        from ..client.sync import pending

        out = []
        recording = engine.recording_status()
        recording_id = recording and recording.get("session_id")
        for session in reversed(engine.sessions.list()):
            result = engine.sessions.load_result(session)
            if result is None:
                if session.id == recording_id or not session.audio_path.exists():
                    continue
                try:
                    created = datetime.strptime(session.id[:15], "%Y%m%d-%H%M%S").isoformat()
                except ValueError:
                    created = session.id
                seconds = _wav_seconds(session.audio_path)
                if session.id == daemon.analyzing:  # being analyzed right now
                    out.append({"id": session.id, "created": created, "duration": seconds, "counts": {},
                                "preview": "Analyzing…", "analyzing": True})  # fmt: skip
                elif session.id in daemon.waiting:  # recorded, waiting for its turn
                    out.append({"id": session.id, "created": created, "duration": seconds, "counts": {},
                                "preview": "Waiting for its turn…", "queued": True})  # fmt: skip
                elif pending(session) is not None:  # recorded while the server was unreachable
                    out.append({"id": session.id, "created": created, "duration": None, "counts": {},
                                "preview": "Waiting for the server…", "pending": True})  # fmt: skip
                else:  # the analysis failed (e.g. a model was missing): the recording is kept
                    out.append({"id": session.id, "created": created, "duration": seconds,
                                "counts": {}, "preview": "", "unanalyzed": True})  # fmt: skip
                continue
            feedback = engine.sessions.load_feedback(session)
            counts: dict[str, int] = {}
            for i, f in enumerate(result.findings):
                if feedback.get(i) != "dismissed":
                    counts[f.analyzer] = counts.get(f.analyzer, 0) + 1
            out.append({
                "id": session.id, "created": result.created, "duration": result.duration,
                "counts": counts, "preview": (result.sentences[0] if result.sentences else ""),
            })  # fmt: skip
        return out

    @app.get("/api/sessions/{sid}")
    def session_detail(sid: str):
        session = session_or_404(sid)
        result = engine.sessions.load_result(session)
        if result is None:
            raise HTTPException(404, "This session has not been analyzed yet")
        return {"result": result.to_dict(), "feedback": engine.sessions.load_feedback(session)}

    @app.delete("/api/sessions/{sid}")
    def session_delete(sid: str):
        engine.sessions.delete(session_or_404(sid))
        daemon.sync_soon()
        return {"deleted": sid}

    @app.get("/api/sessions/{sid}/audio")
    def session_audio(sid: str, start: float | None = None, end: float | None = None):
        session = session_or_404(sid)
        samples = audio.decode(session.audio_path)
        if start is not None and end is not None:
            samples = audio.slice_seconds(samples, start, end, pad=CLIP_PAD_S, sample_rate=SAMPLE_RATE)
        return Response(_wav_bytes(samples), media_type="audio/wav")

    @app.post("/api/sessions/{sid}/analyze")
    def session_reanalyze(sid: str, body: RecordBody):
        session_or_404(sid)
        daemon.reanalyze(sid, body.analyzers)
        return {"queued": True}

    @app.post("/api/sessions/{sid}/findings/{index}/feedback")
    def finding_feedback(sid: str, index: int, body: FeedbackBody):
        session = session_or_404(sid)
        result = engine.sessions.load_result(session)
        if result is None or not 0 <= index < len(result.findings):
            raise HTTPException(404, "No such finding")
        feedback = engine.sessions.set_feedback(session, index, body.verdict)
        daemon.sync_soon()
        return feedback

    # --- practice ----------------------------------------------------------------------------

    @app.put("/api/practice/target")
    def practice_target(body: TargetBody):
        daemon.set_practice_target(body.text, body.session_id)
        return {"target": daemon.practice_target}

    @app.get("/api/practice/weak")
    def practice_weak(limit: int = 20):
        return engine.weak_words(limit)

    @app.get("/api/practice/stats")
    def practice_stats(target: str):
        return engine.practice_store.stats(target)

    @app.get("/api/practice/attempts/latest/audio")
    def practice_latest_audio(target: str):
        attempts = engine.practice_store.attempts(target)
        path = Path(attempts[-1].audio) if attempts and attempts[-1].audio else None
        if path is None or not path.exists() or path.parent != engine.practice_store.audio_dir:
            raise HTTPException(404, "No attempt audio")
        return Response(path.read_bytes(), media_type="audio/wav")

    @app.get("/api/tts")
    def tts(text: str):
        if not text.strip() or len(text) > 300:
            raise HTTPException(400, "Text must be 1–300 characters")
        try:
            return Response(daemon.backend.tts(text.strip()), media_type="audio/wav")
        except ProviderUnavailable as e:
            raise HTTPException(404, str(e)) from None
        except ZephvoxError as e:
            raise HTTPException(502, str(e)) from None

    @app.get("/api/history")
    def history(limit: int = 15):
        return engine.history(limit)

    # --- the React app -----------------------------------------------------------------------

    if (STATIC_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:

        @app.get("/", response_class=HTMLResponse)
        def missing_frontend():
            return ("<h1>zephvox</h1><p>The web page hasn't been built yet. Run "
                    "<code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>.</p>"
                    "<p>The API is running: <a href='/api/docs'>/api/docs</a></p>")  # fmt: skip

    return app


def _wav_seconds(path: Path) -> float | None:
    """Length of a WAV file from its header (cheap: no decoding)."""
    try:
        with wave.open(str(path)) as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except (OSError, wave.Error, ZeroDivisionError):
        return None
