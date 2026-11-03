"""The app used to be called "speakcheck": what it left behind keeps working, and moves to the new name."""

import sys
import pytest
import respx

from zephvox import desktop
from zephvox.client.remote import ServerClient, ServerError
from zephvox.engine import config
from zephvox.server import os_service


@pytest.mark.skipif(sys.platform == "win32", reason="the data folders come from XDG_* variables, which Windows ignores")
class TestFolders:
    @pytest.fixture(autouse=True)
    def own_folders(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZEPHVOX_HOME", raising=False)  # the suite's fixture points these at a temp folder
        monkeypatch.delenv("ZEPHVOX_CONFIG", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
        self.data, self.conf = tmp_path / "data", tmp_path / "conf"

    def test_the_old_data_folder_is_moved_with_everything_in_it(self):
        old = self.data / "speakcheck"
        (old / "sessions" / "20261014-101010").mkdir(parents=True)
        (old / "sessions" / "20261014-101010" / "audio.wav").write_bytes(b"RIFF")
        (old / "settings.json").write_text('{"config": {"engine": {"practice_max_s": 6.0}}}')
        assert config.default_data_dir() == self.data / "zephvox"
        assert not old.exists()
        assert (self.data / "zephvox" / "sessions" / "20261014-101010" / "audio.wav").read_bytes() == b"RIFF"
        assert config.load_user_settings(self.data / "zephvox")["config"]["engine"]["practice_max_s"] == 6.0

    def test_an_existing_new_folder_is_never_overwritten(self):
        (self.data / "speakcheck").mkdir(parents=True)
        (self.data / "zephvox").mkdir()
        (self.data / "zephvox" / "keep").write_text("mine")
        assert config.default_data_dir() == self.data / "zephvox"
        assert (self.data / "speakcheck").is_dir() and (self.data / "zephvox" / "keep").read_text() == "mine"

    def test_a_folder_that_cant_be_moved_keeps_being_used(self, monkeypatch):
        (self.data / "speakcheck").mkdir(parents=True)

        def deny(self, target):
            raise PermissionError("no")

        monkeypatch.setattr(config.Path, "rename", deny)
        assert config.default_data_dir() == self.data / "speakcheck"  # data stays reachable

    def test_nothing_to_move_gives_the_new_folder(self):
        assert config.default_data_dir() == self.data / "zephvox" and not (self.data / "zephvox").exists()

    def test_the_old_config_folder_is_moved_too(self):
        (self.conf / "speakcheck").mkdir(parents=True)
        (self.conf / "speakcheck" / "config.toml").write_text('profile = "cpu"\n')
        assert config.user_config_dir() == self.conf / "zephvox"
        assert (self.conf / "zephvox" / "config.toml").read_text() == 'profile = "cpu"\n'

    def test_old_environment_variables_still_work_and_the_new_ones_win(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEAKCHECK_HOME", str(tmp_path / "old-home"))
        assert config.default_data_dir() == tmp_path / "old-home"
        monkeypatch.setenv("ZEPHVOX_HOME", str(tmp_path / "new-home"))
        assert config.default_data_dir() == tmp_path / "new-home"
        monkeypatch.setenv("SPEAKCHECK_CONFIG", str(tmp_path / "old.toml"))
        assert config.default_config_path() == tmp_path / "old.toml"


class TestDesktopEntries:
    def test_installing_replaces_the_old_menu_entry_and_icon(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        (tmp_path / "applications").mkdir()
        (tmp_path / "icons").mkdir()
        old = [tmp_path / "applications" / "io.github.speakcheck.desktop", tmp_path / "icons" / "io.github.speakcheck.svg"]
        for path in old:
            path.write_text("old")
        entry = desktop.install("/venv/bin/python")
        assert entry.name == "io.github.zephvox.desktop" and "Name=zephvox" in entry.read_text()
        assert "zephvox.cli" in entry.read_text()
        assert not any(p.exists() for p in old)
        old[0].write_text("old")
        desktop.remove()
        assert not entry.exists() and not old[0].exists()

    def test_only_our_own_old_command_is_removed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        env_bin = tmp_path / "env" / "bin"
        env_bin.mkdir(parents=True)
        (bin_dir / "speakcheck").symlink_to(env_bin / "speakcheck")  # the old shim: points at the old command
        desktop.install_command(str(env_bin / "python"))
        assert not (bin_dir / "speakcheck").is_symlink() and (bin_dir / "zephvox").is_symlink()
        # A different program that happens to use the old name is left alone.
        (bin_dir / "speakcheck").write_text("#!/bin/sh\necho someone else's tool\n")
        desktop.remove_command()
        assert (bin_dir / "speakcheck").exists() and not (bin_dir / "zephvox").exists()


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 0, stdout="Linger=yes", stderr="")


class TestService:
    def test_the_old_service_is_replaced_and_removed(self, tmp_path):
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        legacy = unit_dir / "speakcheck-server.service"
        legacy.write_text("[Unit]\n")
        runner = Runner()
        os_service.install(["python", "-m", "zephvox.cli", "server", "serve"], tmp_path, runner, "linux", tmp_path)
        assert not legacy.exists() and (unit_dir / "zephvox-server.service").exists()
        assert ["systemctl", "--user", "disable", "--now", "speakcheck-server"] in runner.calls
        legacy.write_text("[Unit]\n")
        assert os_service.remove(runner, "linux", tmp_path) is True
        assert not legacy.exists() and not (unit_dir / "zephvox-server.service").exists()
        assert os_service.remove(runner, "linux", tmp_path) is False


class TestServers:
    @respx.mock
    def test_servers_from_before_the_rename_are_still_recognized(self):
        respx.get("https://old.example/v1/health").respond(json={"service": "speakcheck", "version": "0.1.0"})
        respx.get("https://new.example/v1/health").respond(json={"service": "zephvox", "version": "0.1.0"})
        respx.get("https://other.example/v1/health").respond(json={"service": "nginx"})
        assert ServerClient("https://old.example").health()["service"] == "speakcheck"
        assert ServerClient("https://new.example").health()["service"] == "zephvox"
        with pytest.raises(ServerError, match="zephvox server"):
            ServerClient("https://other.example").health()
