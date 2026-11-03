"""Set the release version everywhere it lives, or check that it agrees with a tag.

    python scripts/bump_version.py 0.2.0          # a release
    python scripts/bump_version.py 0.2.0-rc.1     # a pre-release (Python spells it 0.2.0rc1)
    python scripts/bump_version.py --check v0.2.0-rc.1   # used by the release workflow

The desktop app refuses an engine whose version differs from the one it was built with, so the
Python package, the app and both lockfiles must all say the same thing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")


def python_version(semver: str) -> str:
    """0.2.0-rc.1 → 0.2.0rc1 (PEP 440); plain versions are the same in both."""
    m = SEMVER.match(semver)
    if not m:
        raise ValueError(f"{semver!r} isn't X.Y.Z or X.Y.Z-rc.N")
    major, minor, patch, rc = m.groups()
    return f"{major}.{minor}.{patch}" + (f"rc{rc}" if rc else "")


def semver_from_python(version: str) -> str:
    """0.2.0rc1 → 0.2.0-rc.1: the inverse of python_version()."""
    m = re.match(r"^(\d+\.\d+\.\d+)(?:rc(\d+))?$", version)
    if not m:
        raise ValueError(f"{version!r} isn't a version this project uses")
    return m.group(1) + (f"-rc.{m.group(2)}" if m.group(2) else "")


# file → (pattern with the version as group 2, how often it must match)
def targets(semver: str) -> list[tuple[Path, str, str, int]]:
    py = python_version(semver)
    return [
        (ROOT / "pyproject.toml", r'(?m)^(version = ")([^"]+)(")', py, 1),
        (ROOT / "src/zephvox/__init__.py", r'(?m)^(__version__ = ")([^"]+)(")', py, 1),
        (ROOT / "uv.lock", r'(?m)(^name = "zephvox"\nversion = ")([^"]+)(")', py, 1),
        (ROOT / "app/src-tauri/Cargo.toml", r'(?m)^(version = ")([^"]+)(")', semver, 1),
        (ROOT / "app/src-tauri/Cargo.lock", r'(?m)(^name = "zephvox-app"\nversion = ")([^"]+)(")', semver, 1),
        (ROOT / "app/src-tauri/tauri.conf.json", r'(?m)^(  "version": ")([^"]+)(")', semver, 1),
    ]


def current(path: Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"{path.relative_to(ROOT)}: no version found")
    return m.group(2)


def check(semver: str) -> list[str]:
    """What disagrees with `semver`, as messages (empty: all good)."""
    problems = []
    for path, pattern, want, _ in targets(semver):
        have = current(path, pattern)
        if have != want:
            problems.append(f"{path.relative_to(ROOT)} says {have}, expected {want}")
    return problems


def bump(semver: str) -> None:
    for path, pattern, want, count in targets(semver):
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(pattern, lambda m: m.group(1) + want + m.group(3), text, count=count)
        if n != count:
            raise SystemExit(f"{path.relative_to(ROOT)}: expected {count} version, found {n}")
        path.write_text(new, encoding="utf-8")
    json.loads((ROOT / "app/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))  # still valid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="X.Y.Z or X.Y.Z-rc.N; with --check, a tag like vX.Y.Z")
    parser.add_argument("--check", action="store_true", help="verify the files already match this tag")
    args = parser.parse_args()
    semver = args.version.removeprefix("v")
    try:
        python_version(semver)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.check:
        problems = check(semver)
        for p in problems:
            print(p, file=sys.stderr)
        print(f"all versions are {semver}" if not problems else "versions disagree with the tag")
        return 1 if problems else 0
    bump(semver)
    print(f"Set {semver} (Python: {python_version(semver)}). Commit, then tag v{semver}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
