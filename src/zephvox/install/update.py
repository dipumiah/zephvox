"""`zephvox update` and `zephvox uninstall` for installs made by the install script."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ..engine.config import default_config_path, load_config, project_root
from ..engine.errors import ZephvoxError
from ..web.client import DaemonError, find_daemon, start_daemon, stop_daemon
from . import ollama
from .download import cache_path, plan_downloads
from .state import STATE_FILE, InstallState, Runner, SetupError, is_checkout, sync
from .wizard import Console

REPO = "dipoahmed018/zephvox"
# Never replaced by an update: the user's files and the environment.
PRESERVE = {"config.toml", ".env", ".venv", STATE_FILE, ".git"}

Fetch = Callable[[str], bytes]


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "zephvox-updater"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return resp.read()


def latest_version(fetch: Fetch = http_get) -> tuple[str, str]:
    """(version, source archive URL) of the latest release, or of `main` if there is none yet."""
    try:
        tag = json.loads(fetch(f"https://api.github.com/repos/{REPO}/releases/latest"))["tag_name"]
        return tag, f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz"
    except (OSError, ValueError, KeyError):
        return "main", f"https://github.com/{REPO}/archive/refs/heads/main.tar.gz"


def extract(archive: bytes, dest: Path) -> Path:
    """Unpack a GitHub source archive; returns its single top-level folder."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    (top,) = [p for p in dest.iterdir() if p.is_dir()]
    return top


def replace_tree(new: Path, root: Path) -> None:
    """Swap every top-level item of `root` for the new release's, keeping PRESERVE."""
    for item in new.iterdir():
        if item.name in PRESERVE:
            continue
        target = root / item.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        shutil.move(str(item), target)


def _launcher() -> Path:
    """The environment's `zephvox` launcher (on Windows Scripts\\zephvox.exe, the process running the update)."""
    return Path(sys.executable).parent / "zephvox.exe"


def free_launcher() -> Path | None:
    """Windows can't overwrite a running exe but can rename it: move the launcher aside so `uv sync`
    can write a fresh one. Returns where it went (None if there was nothing to move)."""
    if os.name != "nt":
        return None  # elsewhere a running file can be replaced freely
    exe = _launcher()
    if not exe.exists():
        return None
    old = exe.with_name(exe.name + ".old")
    old.unlink(missing_ok=True)  # left by an earlier update, no longer running
    exe.rename(old)
    return old


def run_update(args: argparse.Namespace, fetch: Fetch = http_get, runner: Runner = subprocess.run) -> int:
    root = project_root()
    try:
        if root is None:
            raise SetupError("no install folder found")
        if is_checkout(root):
            raise SetupError("this is a git checkout: update it with `git pull`, then `uv run zephvox setup`")
        state = InstallState.load(root)
        version, url = latest_version(fetch)
        if version == state.version and version != "main" and not args.force:
            print(f"zephvox {version} is up to date.")
            return 0
        print(f"Updating {state.version or 'zephvox'} → {version} …")
        with tempfile.TemporaryDirectory() as tmp:
            new_tree = extract(fetch(url), Path(tmp))  # downloaded first: a failure leaves zephvox running
            data_dir = _data_dir(args)
            running = find_daemon(data_dir) if data_dir else None
            if running:
                # Its files are about to change (on Windows they can't be replaced while it runs), and a
                # running copy keeps the old code: so it is stopped now and started again below.
                print("zephvox is running: stopping it for the update …")
                stop_daemon(data_dir)
            replace_tree(new_tree, root)
        aside = free_launcher()
        try:
            sync(root, state.extras or ["models", state.accelerator or "cpu", "desktop", "web", "voice"], runner)
        except BaseException:
            if aside is not None and not _launcher().exists():
                aside.rename(_launcher())  # the update failed: keep the old command working
            raise
        state.version = version
        state.save(root)
    except (ZephvoxError, OSError, tarfile.TarError, DaemonError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"Updated to {version}.")
    if running and not getattr(args, "no_restart", False):
        print("Starting zephvox again …")
        started = start_daemon(root, data_dir)
        print(f"zephvox is running again at {started}." if started else
              "It didn't come back up: run `zephvox serve` to see why.")  # fmt: skip
    elif running:
        print("Start it again with `zephvox serve`.")
    print("Run `zephvox setup` if you want to review the profile (it also fetches anything a new release needs).")
    return 0


def _data_dir(args: argparse.Namespace) -> Path | None:
    """Where the daemon's files live, to find out whether it is running (None if the config can't be read)."""
    try:
        return load_config(getattr(args, "config", None) or default_config_path()).engine.data_dir
    except ZephvoxError:
        return None


def run_uninstall(args: argparse.Namespace, ask: Callable[[str], str] = input, runner: Runner = subprocess.run) -> int:
    console = Console(interactive=sys.stdin.isatty() and not args.yes, ask=ask)
    say = console.say
    root = project_root()
    if root is None:
        print("error: no install folder found", file=sys.stderr)
        return 2
    if not console.confirm(f"Remove zephvox from {root}?", default=args.yes):
        return 1
    from ..desktop import remove_command, remove_launcher

    remove_launcher()
    remove_command()
    say("Removed the app-menu entry and the `zephvox` command.")
    try:
        config = load_config(args.config or default_config_path())
    except ZephvoxError:
        config = None
    if config is not None:
        downloads = plan_downloads(config)
        if downloads and console.confirm("Also delete the downloaded models?", default=False):
            for d in downloads:
                if (path := cache_path(d)) is not None:
                    shutil.rmtree(path, ignore_errors=True)
                elif d.source == "ollama" and (binary := ollama.find()):
                    with ollama.serving(binary):
                        runner([binary, "rm", d.name], check=False)
            say("Deleted the models.")
        data = config.engine.data_dir
        if data.exists() and console.confirm(f"Delete your recordings and history ({data})?", default=False):
            shutil.rmtree(data, ignore_errors=True)
    if is_checkout(root):
        say(f"{root} is a git checkout: leaving the folder in place.")
        return 0
    say(f"Removing {root} …")
    if os.name == "nt":  # files of the running Python can't be deleted until it exits
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 > nul & rmdir /s /q "{root}"', shell=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)  # fmt: skip
    else:
        shutil.rmtree(root, ignore_errors=True)
    say("zephvox is uninstalled.")
    return 0
