"""Thin developer CLI over the engine API."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from .engine.config import Config, config_dirs, default_config_path, load_config, load_env
from .engine.engine import Engine
from .engine.errors import ZephvoxError
from .engine.events import Event
from .engine.models import AnalysisResult
from .engine.profiles import PROFILES
from .server import cli as server_cli
from .web.client import DaemonError, call, find_daemon

EngineFactory = Callable[[Config], Engine]


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="config file (default: config.toml in the project root)")
    common.add_argument("--set", action="append", default=[], metavar="KIND=NAME",
                        help="choose a provider, e.g. --set stt=faster-whisper (repeatable)")  # fmt: skip
    common.add_argument("-q", "--quiet", action="store_true", help="no progress output")

    analysis = argparse.ArgumentParser(add_help=False)
    analysis.add_argument("--analyzers", help="comma-separated, e.g. pronunciation,grammar")
    analysis.add_argument("--format", default="terminal", help="reporter: terminal, markdown, json")
    analysis.add_argument("-o", "--output", type=Path, help="also write the report to this file")

    p = argparse.ArgumentParser(prog="zephvox", description="Find mistakes in your spoken English.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("record", parents=[common, analysis], help="record until Enter, then analyze")
    sub.add_parser("start", parents=[common], help="start recording in the background")
    sub.add_parser("stop", parents=[common, analysis], help="stop the background recording and analyze it")
    a = sub.add_parser("analyze", parents=[common, analysis], help="analyze an audio file")
    a.add_argument("file", type=Path)
    r = sub.add_parser("report", parents=[common, analysis], help="show a saved session's report")
    r.add_argument("session", nargs="?", help="session id (default: latest)")
    sub.add_parser("toggle", parents=[common], help="start, or stop and analyze (bind this to a shortcut)")
    sub.add_parser("practice", parents=[common], help="record a practice attempt for the word chosen in the page")
    s = sub.add_parser("serve", parents=[common], help="run the daemon + web page")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices on your network")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the page in your browser")
    s.add_argument("--no-shortcuts", action="store_true", help="don't register global shortcuts")
    sub.add_parser("desktop-entry", parents=[common],
                   help="add zephvox to the app menu (Linux: also names its global shortcuts)")  # fmt: skip
    st = sub.add_parser("setup", parents=[common], help="detect this machine, choose a profile, install models")
    st.add_argument("--profile", choices=[*PROFILES, "custom"],
                    help="skip the question and use this profile (custom: ask each part, or reuse saved choices)")  # fmt: skip
    st.add_argument("--accelerator", choices=["cpu", "cuda", "rocm"], help="PyTorch build (default: detected)")
    st.add_argument("-y", "--yes", action="store_true", help="accept every default without asking")
    st.add_argument("--no-download", action="store_true", help="download models on first use instead")
    st.add_argument("--no-grammar", action="store_true", help="don't offer grammar checking (pronunciation only)")
    st.add_argument("--no-ollama", action="store_true", help="don't install Ollama (grammar stays off)")
    st.add_argument("--no-launcher", action="store_true", help="don't add an app-menu entry")
    st.add_argument("--no-verify", action="store_true", help="skip the test analysis")
    st.add_argument("--dry-run", action="store_true", help="show what would be installed, change nothing")
    st.add_argument("--role", choices=["full", "client"],
                    help="full (the default): record and analyze here; client: record here, analyze on a server")  # fmt: skip
    st.add_argument("--server-url", help="client: the server's address, e.g. https://speak.example.com")
    st.add_argument("--server-header", action="append", metavar="NAME=VALUE",
                    help="client: an extra header for every request (repeatable; ${VAR} reads .env)")  # fmt: skip
    st.add_argument("--invite", help="client: the invite code from the server's admin")
    st.add_argument("--device-name", help="client: how this device is listed on the server (default: hostname)")
    st.add_argument("--installed-version", help=argparse.SUPPRESS)  # passed by the install script
    up = sub.add_parser("update", parents=[common], help="install the latest release")
    up.add_argument("--force", action="store_true", help="reinstall even if up to date")
    up.add_argument("--no-restart", action="store_true", help="don't start zephvox again afterwards if it was running")
    sub.add_parser("quit", parents=[common], help="stop the running zephvox")
    un = sub.add_parser("uninstall", parents=[common], help="remove zephvox (asks about models and recordings)")
    un.add_argument("-y", "--yes", action="store_true", help="remove without asking (keeps models and recordings)")
    sub.add_parser("providers", parents=[common], help="list providers and whether they can run here")
    sub.add_parser("doctor", parents=[common], help="system + plugin diagnostics as JSON")
    h = sub.add_parser("history", parents=[common], help="your most frequent mistakes")
    h.add_argument("--limit", type=int, default=10)
    server_cli.add_parser(sub, common)
    return p


def _progress(event: Event) -> None:
    d = event.data
    if event.type == "stage_started":
        print(f"· {d['stage']} …", file=sys.stderr)
    elif event.type == "stage_finished" and "seconds" in d:
        print(f"  done in {d['seconds']:.1f}s", file=sys.stderr)
    elif event.type == "provider_selected":
        print(f"· using {d['kind']} provider: {d['name']}", file=sys.stderr)
    elif event.type == "provider_fallback":
        print(f"  ({d['kind']}: skipped {'; '.join(d['skipped'])})", file=sys.stderr)
    elif event.type == "recording_started":
        print(f"● recording ({d['mode']})", file=sys.stderr)


def _emit_report(engine: Engine, result: AnalysisResult, args: argparse.Namespace) -> None:
    reporter = engine.registry.get("reporters", args.format)()
    text = reporter.render(result)
    print(text, end="")
    if args.output:
        args.output.write_text(text, encoding="utf-8")


def _analyzers(args: argparse.Namespace) -> list[str] | None:
    return [a.strip() for a in args.analyzers.split(",") if a.strip()] if args.analyzers else None


DAEMON_COMMANDS = {"start", "stop", "record", "toggle", "practice"}


def _via_daemon(url: str, args: argparse.Namespace, ask: Callable[[str], str]) -> int:
    """Recording commands are forwarded to a running daemon, which owns the microphone."""
    cmd = args.command
    analyzers = _analyzers(args) if getattr(args, "analyzers", None) else None
    try:
        if cmd in ("toggle", "practice"):
            was_recording = call(url, "GET", "/api/status")["recording"] is not None
            mode = "practice" if cmd == "practice" else "analysis"
            call(url, "POST", "/api/record/toggle", json={"mode": mode})
            print("Stopped; analyzing in zephvox." if was_recording else "Recording… run it again to stop.")
        elif cmd == "start":
            call(url, "POST", "/api/record/start", json={"mode": "analysis", "analyzers": analyzers})
            print("Recording in the zephvox daemon. Run `zephvox stop` when done.")
        elif cmd == "stop":
            call(url, "POST", "/api/record/stop")
            print(f"Analyzing — the report will appear at {url}")
        elif cmd == "record":
            call(url, "POST", "/api/record/start", json={"mode": "analysis", "analyzers": analyzers})
            try:
                ask("Speak now. Press Enter to stop… ")
            finally:
                call(url, "POST", "/api/record/stop")
            print(f"Analyzing — the report will appear at {url}")
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _analyze(engine: Engine, source, analyzers: list[str] | None):
    """On this computer, or on the server in client mode."""
    from .client.backend import make_backend

    backend = make_backend(engine)
    try:
        return backend.analyze(source, analyzers)
    finally:
        backend.close()


def run(args: argparse.Namespace, engine: Engine, ask: Callable[[str], str]) -> int:
    cmd = args.command
    if cmd in DAEMON_COMMANDS:
        url = find_daemon(engine.config.engine.data_dir)
        if url:
            return _via_daemon(url, args, ask)
        if cmd == "practice":
            print("error: practice needs the daemon: run `zephvox serve` and pick a word in the page",
                  file=sys.stderr)  # fmt: skip
            return 2
        if cmd == "toggle":
            if not engine.is_recording():
                recording = engine.start_recording()
                print(f"Recording into session {recording.session_id}. Run `zephvox toggle` again to stop.")
                return 0
            cmd = "stop"
            args.analyzers, args.format, args.output = None, "terminal", None
    if cmd == "desktop-entry":
        from .desktop import install_launcher

        print(f"Installed {install_launcher()}")
    elif cmd == "serve":
        from .web.server import serve

        serve(engine, args.host, args.port, args.open, shortcuts=not args.no_shortcuts)
    elif cmd == "record":
        engine.start_recording()
        try:
            ask("Speak now. Press Enter to stop… ")
        finally:
            recording = engine.stop_recording()
        _emit_report(engine, _analyze(engine, recording, _analyzers(args)), args)
    elif cmd == "start":
        recording = engine.start_recording()
        print(f"Recording into session {recording.session_id}. Run `zephvox stop` when done.")
    elif cmd == "stop":
        recording = engine.stop_recording()
        _emit_report(engine, _analyze(engine, recording, _analyzers(args)), args)
    elif cmd == "analyze":
        _emit_report(engine, _analyze(engine, args.file, _analyzers(args)), args)
    elif cmd == "report":
        sessions = engine.sessions.list()
        session = engine.sessions.get(args.session) if args.session else (sessions[-1] if sessions else None)
        result = engine.sessions.load_result(session) if session else None
        if result is None:
            print("No analyzed session found.", file=sys.stderr)
            return 1
        _emit_report(engine, result, args)
    elif cmd == "providers":
        for s in engine.list_providers():
            mark = "★" if s.selected else ("✓" if s.available else "✗")
            reason = f"  — {s.reason}" if s.reason else ""
            print(f"{mark} {s.kind:<9} {s.name:<16}{reason}")
        print("\n★ selected  ✓ available  ✗ unavailable")
        if hints := engine.caps.setup_hints():
            print("\nTo get more working:")
            for hint in hints:
                print(f"  · {hint}")
    elif cmd == "doctor":
        report = engine.doctor()
        remote = engine.config.remote
        report["remote"] = {"mode": remote.mode, "url": remote.url, "sync": remote.syncing}
        if remote.url:
            from .client.backend import client_for
            from .client.remote import ServerError

            client = client_for(engine)
            try:
                report["remote"]["server"] = client.health()
                if client.key:
                    report["remote"]["account"] = client.account()["name"]
            except ServerError as e:
                report["remote"]["error"] = str(e)
            finally:
                client.close()
        report["config"] = {
            "file": str(args.config or default_config_path()),
            "env_files": [str(d / ".env") for d in config_dirs() if (d / ".env").exists()],
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif cmd == "history":
        h = engine.history(args.limit)
        print(f"{h['sessions']} analyzed session(s)")
        for analyzer, items in h["targets"].items():
            print(f"\n{analyzer.capitalize()} — most frequent:")
            for target, count in items:
                print(f"  {count:>3}× {target}")
            print("  by type: " + ", ".join(f"{c} ({n})" for c, n in h["categories"][analyzer]))
    return 0


def main(argv: list[str] | None = None, engine_factory: EngineFactory = Engine,
         ask: Callable[[str], str] = input) -> int:  # fmt: skip
    for stream in (sys.stdout, sys.stderr):  # Windows defaults to cp1252, which can't print IPA
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    load_env()  # before anything imports torch: GPU workarounds are read at import time
    if args.command == "setup":  # these work before (or after) the engine can run
        from .install.wizard import run_setup

        return run_setup(args, ask)
    if args.command in ("update", "uninstall"):
        from .install import update

        return update.run_update(args) if args.command == "update" else update.run_uninstall(args, ask)
    try:
        config = load_config(args.config).with_overrides(args.set)
        if args.command == "server":
            return server_cli.run(args, config, ask)
        if args.command == "quit":
            from .web.client import DaemonError, stop_daemon

            try:
                url = stop_daemon(config.engine.data_dir)
            except DaemonError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print("zephvox stopped." if url else "zephvox isn't running.")
            return 0
        engine = engine_factory(config)
        if not args.quiet:
            engine.events.subscribe(_progress)
        try:
            return run(args, engine, ask)
        finally:
            engine.close()
    except ZephvoxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
