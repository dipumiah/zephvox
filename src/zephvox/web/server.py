"""`zephvox serve`: run the daemon + web page until Ctrl+C."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import webbrowser

from ..engine.engine import Engine
from ..engine.errors import ZephvoxError
from .client import daemon_at, find_daemon, remove_daemon_file, write_daemon_file


READY_PREFIX = "ZEPHVOX_READY "


def serve(engine: Engine, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
          shortcuts: bool = True, print_ready: bool = False, exit_on_stdin_close: bool = False) -> None:  # fmt: skip
    """Run until stopped. `port=0` picks a free one. For a desktop shell that starts us as a child:
    `print_ready` writes one `ZEPHVOX_READY {json}` line to stdout once the page is being served, and
    `exit_on_stdin_close` stops when the parent goes away (its pipe closes), so we never outlive it."""
    try:
        import uvicorn

        from .app import create_app
        from .daemon import Daemon
    except ImportError as e:
        raise ZephvoxError(f"The web GUI needs extra packages: uv sync --extra web ({e.name} missing)") from e

    if port == 0:
        port = free_port(host)
    local = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{local}:{port}"
    data_dir = engine.config.engine.data_dir

    # Already running (e.g. the app launcher clicked again): show that one. Starting a second daemon
    # would register the shortcuts again, and its exit would switch them off for the first one too.
    running = find_daemon(data_dir) or (url if daemon_at(url) else None)
    if running:
        print(f"zephvox is already running at {running}", file=sys.stderr)
        if print_ready:  # the shell attaches to it (and must not stop it on exit)
            from .. import API_VERSION, __version__

            info = {"url": running, "port": int(running.rsplit(":", 1)[-1].split("/")[0]), "attached": True,
                    "engine_version": __version__, "api_version": API_VERSION}
            print(READY_PREFIX + json.dumps(info), flush=True)
        if open_browser:
            webbrowser.open(running)
        return
    if not port_free(host, port):
        raise ZephvoxError(f"Port {port} is already in use by another program. "
                              f"Start zephvox on another one with --port.")  # fmt: skip

    daemon = Daemon(engine, start_triggers=shortcuts)
    app = create_app(daemon)
    # Fetch any missing models now rather than at the first analysis (resumes after a restart).
    threading.Thread(target=daemon.start_model_downloads, name="models-start", daemon=True).start()
    write_daemon_file(data_dir, url)
    print(f"zephvox is running at {url}  (Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    web = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    daemon.request_shutdown = lambda: setattr(web, "should_exit", True)  # `zephvox quit`
    if exit_on_stdin_close:
        threading.Thread(target=watch_stdin, args=(daemon.request_shutdown,), daemon=True).start()
    if print_ready:
        threading.Thread(target=announce_when_up, args=(web, url, port), daemon=True).start()
    try:
        web.run()
    finally:
        daemon.close()
        remove_daemon_file(data_dir)


def watch_stdin(on_close) -> None:
    """Block until stdin reaches EOF (the parent exited or closed our pipe), then call `on_close`."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass
    on_close()


def announce_when_up(web, url: str, port: int, timeout: float = 120.0) -> None:
    """Print the ready line once the server accepts requests (not before: the shell loads it at once)."""
    deadline = time.monotonic() + timeout
    while not web.started and time.monotonic() < deadline and not web.should_exit:
        time.sleep(0.05)
    if web.started:
        from .. import API_VERSION, __version__

        info = {"url": url, "port": port, "engine_version": __version__, "api_version": API_VERSION}
        print(READY_PREFIX + json.dumps(info), flush=True)


def free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True
