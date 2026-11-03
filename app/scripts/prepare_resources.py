"""Collect what the installer bundles: the engine source tree, its manifest and the uv binary.

    python app/scripts/prepare_resources.py [--target x86_64-unknown-linux-gnu] [--no-uv]

Writes into app/src-tauri/resources/:
    engine.tar.gz   the release source tree (pyproject.toml, uv.lock, src/, config.example.toml, ...)
    manifest.json   {engine_version, api_version, tarball_sha256, uv_version}
    uv/uv[.exe]     uv for the build's platform (checksum-verified)

Run from a git checkout: the tree is `git archive HEAD` minus tests, docs and the desktop app itself.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "app" / "src-tauri" / "resources"
UV_VERSION = "0.11.29"  # keep in step with the uv the lockfile was made with
EXCLUDE = [":(exclude)tests", ":(exclude)docs", ":(exclude)app", ":(exclude).github", ":(exclude)docker",
           ":(exclude)web/node_modules", ":(exclude)plugins", ":(exclude)scripts"]  # fmt: skip
# Rust target triple → (uv release asset, archive kind)
UV_ASSETS = {
    "x86_64-unknown-linux-gnu": ("uv-x86_64-unknown-linux-gnu.tar.gz", "tar"),
    "aarch64-unknown-linux-gnu": ("uv-aarch64-unknown-linux-gnu.tar.gz", "tar"),
    "x86_64-pc-windows-msvc": ("uv-x86_64-pc-windows-msvc.zip", "zip"),
    "aarch64-apple-darwin": ("uv-aarch64-apple-darwin.tar.gz", "tar"),
}


def host_target() -> str:
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    system = platform.system()
    if system == "Windows":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-apple-darwin" if system == "Darwin" else f"{arch}-unknown-linux-gnu"


def engine_versions() -> tuple[str, int]:
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    api = re.search(r"^API_VERSION = (\d+)", (ROOT / "src/zephvox/__init__.py").read_text(encoding="utf-8"), re.M)
    if not version or not api:
        sys.exit("can't read the engine version from pyproject.toml / zephvox/__init__.py")
    return version.group(1), int(api.group(1))


def build_tarball() -> tuple[Path, str]:
    OUT.mkdir(parents=True, exist_ok=True)
    tar_bytes = subprocess.run(["git", "archive", "--format=tar", "HEAD", "--", ".", *EXCLUDE], cwd=ROOT,
                               check=True, capture_output=True).stdout  # fmt: skip
    dest = OUT / "engine.tar.gz"
    # Re-pack gzip-compressed with reproducible metadata, so the hash only changes when the content does.
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as src, tarfile.open(dest, "w:gz", format=tarfile.PAX_FORMAT) as out:
        for member in src.getmembers():
            member.mtime, member.uid, member.gid, member.uname, member.gname = 0, 0, 0, "", ""
            out.addfile(member, src.extractfile(member) if member.isfile() else None)
    return dest, hashlib.sha256(dest.read_bytes()).hexdigest()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def bundle_uv(target: str) -> None:
    if target not in UV_ASSETS:
        sys.exit(f"no uv build known for {target}; supported: {', '.join(UV_ASSETS)}")
    asset, kind = UV_ASSETS[target]
    base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{asset}"
    blob = fetch(base)
    expected = fetch(base + ".sha256").decode().split()[0]
    if hashlib.sha256(blob).hexdigest() != expected:
        sys.exit(f"checksum mismatch for {asset}")
    exe = "uv.exe" if "windows" in target else "uv"
    dest = OUT / "uv"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            (dest / exe).write_bytes(z.read(exe))
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/uv") or m.name == "uv")
            (dest / exe).write_bytes(t.extractfile(member).read())
    (dest / exe).chmod((dest / exe).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=host_target())
    parser.add_argument("--no-uv", action="store_true", help="skip downloading uv (offline checks)")
    args = parser.parse_args()
    tarball, sha = build_tarball()
    version, api = engine_versions()
    manifest = {"engine_version": version, "api_version": api, "tarball_sha256": sha, "uv_version": UV_VERSION}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if not args.no_uv:
        bundle_uv(args.target)
    print(f"engine {version} (api {api}), {tarball.stat().st_size / 1e6:.1f} MB, sha256 {sha[:12]}…")


if __name__ == "__main__":
    main()
