"""The release version must agree across the Python package, the app and both lockfiles."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
bump_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump_version)


def test_pre_release_spelling():
    assert bump_version.python_version("1.2.3") == "1.2.3"
    assert bump_version.python_version("1.2.3-rc.4") == "1.2.3rc4"
    for bad in ("1.2", "v1.2.3", "1.2.3-beta.1", "1.2.3-rc1"):
        with pytest.raises(ValueError):
            bump_version.python_version(bad)


def test_the_repo_versions_agree_right_now():
    from zephvox import __version__

    assert bump_version.check(bump_version.python_version(__version__)) == []


def test_bump_then_check_in_a_copy(tmp_path, monkeypatch):
    for rel in ("pyproject.toml", "src/zephvox/__init__.py", "uv.lock", "app/src-tauri/Cargo.toml",
                "app/src-tauri/Cargo.lock", "app/src-tauri/tauri.conf.json"):  # fmt: skip
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    assert bump_version.check("9.9.9-rc.2") != []  # not bumped yet
    bump_version.bump("9.9.9-rc.2")
    assert bump_version.check("9.9.9-rc.2") == []
    assert 'version = "9.9.9rc2"' in (tmp_path / "pyproject.toml").read_text()
    assert '"version": "9.9.9-rc.2"' in (tmp_path / "app/src-tauri/tauri.conf.json").read_text()
    bump_version.bump("9.9.9")
    assert bump_version.check("9.9.9") == []
