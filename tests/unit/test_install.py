"""Hardware detection, profiles, and the setup / update / uninstall commands (all side effects faked)."""

import io
import json
import sys
import tarfile
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from zephvox import cli
from zephvox.engine import config as config_mod
from zephvox.engine import hardware, profiles
from zephvox.engine.config import (
    choose_profile,
    load_config,
    load_user_settings,
    save_user_settings,
    write_profile,
)
from zephvox.engine.errors import ConfigError
from zephvox.engine.hardware import GPU, Hardware
from zephvox.install import download, ollama, state, update, wizard
from zephvox.providers.base import Download

ROCMINFO = """
*******
Agent 1
*******
  Name:                    AMD Ryzen 5 7600 6-Core Processor
  Marketing Name:          AMD Ryzen 5 7600 6-Core Processor
  Device Type:             CPU
*******
Agent 2
*******
  Name:                    gfx1030
  Marketing Name:          AMD Radeon RX 6800 XT
  Device Type:             GPU
      Name:                    amdgcn-amd-amdhsa--gfx1030
*******
Agent 3
*******
  Name:                    gfx1036
  Marketing Name:          AMD Ryzen 5 7600 6-Core Processor
  Device Type:             GPU
"""


def machine(gpus=(), ram=32.0, os_key="linux", disk=200.0) -> Hardware:
    return Hardware(os_key, "x86_64", "Test CPU", 8, ram, disk, list(gpus))


def nvidia(vram):
    return GPU("nvidia", "RTX", vram, "cuda")


# --- hardware ---------------------------------------------------------------------------


class TestHardware:
    def test_nvidia_smi_parsing(self):
        gpus = hardware.nvidia_gpus("NVIDIA GeForce RTX 3060, 12288\nbad line\nTesla, 16384\n")
        assert [(g.name, g.vram_gb, g.backend) for g in gpus] == [("NVIDIA GeForce RTX 3060", 12.0, "cuda"),
                                                                 ("Tesla", 16.0, "cuda")]  # fmt: skip

    def test_amd_from_sysfs_skips_integrated_gpus(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
        for card, vendor, vram in [("card0", "0x10de", 8), ("card1", "0x1002", 16), ("card2", "0x1002", 0.5)]:
            device = tmp_path / card / "device"
            device.mkdir(parents=True)
            (device / "vendor").write_text(vendor + "\n")
            (device / "mem_info_vram_total").write_text(str(int(vram * 1024**3)))
        (tmp_path / "card1-DP-1").mkdir()  # connectors are ignored
        gpus = hardware.amd_gpus(tmp_path, ROCMINFO)
        assert gpus == [GPU("amd", "AMD Radeon RX 6800 XT", 16.0, "rocm", "gfx1030")]
        assert hardware.rocm_gpu_agents(ROCMINFO) == [("AMD Radeon RX 6800 XT", "gfx1030")]

    def test_amd_without_rocminfo_or_on_arm(self, tmp_path, monkeypatch):
        device = tmp_path / "card0" / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text("0x1002")
        (device / "mem_info_vram_total").write_text(str(8 * 1024**3))
        monkeypatch.setattr(hardware.platform, "machine", lambda: "aarch64")
        assert hardware.amd_gpus(tmp_path, "") == [GPU("amd", "AMD GPU", 8.0, None, None)]

    def test_best_gpu_and_accelerator(self):
        apple = GPU("apple", "M3", 16.0, "mps")
        other = GPU("other", "Intel Arc", None, None)
        assert machine([other, nvidia(8), nvidia(24)]).gpu.vram_gb == 24
        assert machine([nvidia(8)]).accelerator == "cuda"
        assert machine([GPU("amd", "RX", 16, "rocm")]).accelerator == "rocm"
        assert machine([apple]).accelerator == "cpu"  # the macOS cpu build drives MPS
        assert machine([other]).accelerator == "cpu" and machine([other]).gpu is None

    def test_summary_and_hsa_override(self):
        lines = machine([nvidia(12), GPU("other", "Intel UHD", None, None)]).summary()
        assert any("RTX, 12 GB" in line and "Intel UHD (not usable" in line for line in lines)
        assert hardware.hsa_override("gfx1030") == "10.3.0" and hardware.hsa_override("gfx1100") == "11.0.0"
        assert hardware.hsa_override("gfx906") is None and hardware.hsa_override(None) is None

    def test_detect_runs_here(self):
        hw = hardware.detect()
        assert hw.cores >= 1 and hw.ram_gb > 0 and hw.disk_free_gb > 0


# --- profiles ---------------------------------------------------------------------------


class TestProfiles:
    @pytest.mark.parametrize(("hw", "expected"), [
        (machine([nvidia(24)]), "large"),
        (machine([nvidia(15.98)]), "large"),
        (machine([nvidia(12)]), "standard"),
        (machine([nvidia(8)]), "small"),
        (machine([nvidia(4)], ram=32), "cpu"),
        (machine([], ram=16), "cpu"),
        (machine([], ram=8), "cpu-light"),
        (machine([GPU("apple", "M2", 8, "mps")], ram=8, os_key="darwin"), "small"),
        (machine([GPU("apple", "M3", 18, "mps")], ram=18, os_key="darwin"), "standard"),
        (machine([GPU("apple", "M3 Max", 36, "mps")], ram=36, os_key="darwin"), "large"),
    ])  # fmt: skip
    def test_recommend(self, hw, expected):
        assert profiles.recommend(hw) == expected

    def test_accelerator_and_models(self):
        hw = machine([nvidia(12)])
        assert profiles.accelerator(profiles.get("standard"), hw) == "cuda"
        assert profiles.accelerator(profiles.get("cpu"), hw) == "cpu"
        assert [p.llm_model for p in profiles.PROFILES.values()] == ["qwen3:14b", "qwen3:8b", "qwen3:4b", "qwen3:4b", None]
        with pytest.raises(ConfigError, match="choose one of"):
            profiles.get("huge")
        assert profiles.packages_gb("linux", "rocm")[0] > profiles.packages_gb("darwin", "cpu")[0]

    def test_every_profile_loads(self, tmp_path):
        for name in profiles.PROFILES:
            path = tmp_path / f"{name}.toml"
            path.write_text(f'profile = "{name}"\n[engine]\ndata_dir = "{tmp_path / "data"}"\n')
            cfg = load_config(path)
            assert cfg.profile == name and cfg.source == path
        assert load_config(tmp_path / "cpu-light.toml").engine.analyzers == ["pronunciation"]
        assert load_config(tmp_path / "cpu.toml").providers["stt"][0] == "faster-whisper"

    def test_config_file_wins_over_profile(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text(f'profile = "small"\n[engine]\ndata_dir = "{tmp_path}"\n'
                        '[provider.openai-compat]\nmodel = "mine"\ntimeout = 5\n')  # fmt: skip
        cfg = load_config(path)
        assert cfg.provider_settings("openai-compat") == {"model": "mine", "timeout": 5}
        path.write_text('profile = "nope"\n')
        with pytest.raises(ConfigError, match="Unknown profile"):
            load_config(path)

    def test_merge(self):
        assert config_mod.merge({"a": {"x": 1, "y": [1]}, "b": 1}, {"a": {"y": [2]}, "c": 3}) == \
            {"a": {"x": 1, "y": [2]}, "b": 1, "c": 3}  # fmt: skip


class TestWriteProfile:
    def test_new_file_starts_from_template(self, tmp_path):
        template = tmp_path / "example.toml"
        template.write_text("# docs\n[engine]\n# analyzers = []\n")
        path = tmp_path / "config.toml"
        write_profile(path, "small", template)
        text = path.read_text()
        assert text.startswith('profile = "small"') and text.endswith("# docs\n[engine]\n# analyzers = []\n")
        assert load_config(path).profile == "small"

    def test_replaces_only_the_profile_line(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('# mine\nprofile = "large"\n\n[engine]\nanalyzers = ["grammar"]\n[x]\nprofile = "not top-level"\n')
        write_profile(path, "cpu")
        assert path.read_text() == '# mine\nprofile = "cpu"\n\n[engine]\nanalyzers = ["grammar"]\n[x]\nprofile = "not top-level"\n'

    def test_inserts_before_first_table(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[engine]\nanalyzers = ["grammar"]\n')
        write_profile(path, "standard")
        assert path.read_text().startswith('profile = "standard"')
        assert path.read_text().endswith('[engine]\nanalyzers = ["grammar"]\n')
        with pytest.raises(ConfigError):
            write_profile(path, "huge")

    def test_choosing_a_profile_with_analyzers_resets_the_gui_choice(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(f'[engine]\ndata_dir = "{tmp_path}"\n')
        save_user_settings(tmp_path, {"analyzers": ["pronunciation", "grammar"], "tts": {"voice": "x"}})
        choose_profile(path, "small")  # no analyzers in this profile: the GUI choice stays
        assert load_user_settings(tmp_path)["analyzers"] == ["pronunciation", "grammar"]
        profiles.save_custom(profiles.custom_path(path), {"engine": {"analyzers": ["pronunciation"]}})
        choose_profile(path, "custom")  # a profile that names analyzers replaces the GUI choice
        assert load_user_settings(tmp_path) == {"tts": {"voice": "x"}}
        assert load_config(path).engine.analyzers == ["pronunciation"]


# --- install pieces ---------------------------------------------------------------------


class TestState:
    def test_roundtrip_and_extras(self, tmp_path):
        assert state.InstallState.load(tmp_path) == state.InstallState()
        s = state.InstallState("cuda", state.extras_for("cuda"), "v1.0")
        s.save(tmp_path)
        assert state.InstallState.load(tmp_path) == s
        assert s.extras == ["models", "cuda", "desktop", "web", "voice"]
        (tmp_path / state.STATE_FILE).write_text("{broken")
        assert state.InstallState.load(tmp_path).accelerator is None

    def test_sync_command_release_vs_checkout(self, tmp_path):
        cmd = state.sync_command(tmp_path, ["models", "cpu"], "uv")
        assert cmd == ["uv", "sync", "--project", str(tmp_path), "--extra", "models", "--extra", "cpu",
                       "--no-dev", "--frozen"]  # fmt: skip
        (tmp_path / ".git").mkdir()
        assert state.sync_command(tmp_path, ["cpu"], "uv")[-2:] == ["--extra", "cpu"]

    def test_sync_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state, "find_uv", lambda: None)
        with pytest.raises(state.SetupError, match="uv not found"):
            state.sync(tmp_path, ["cpu"])
        monkeypatch.setattr(state, "find_uv", lambda: "uv")
        with pytest.raises(state.SetupError, match="failed"):
            state.sync(tmp_path, ["cpu"], runner=lambda cmd, check: CompletedProcess(cmd, 1))


class TestOllama:
    def test_has_model_reads_manifests(self, tmp_path):
        manifest = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "8b"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
        assert ollama.has_model("qwen3:8b", [tmp_path])
        assert not ollama.has_model("qwen3:4b", [tmp_path])
        (manifest.parent.parent.parent / "me" / "tiny").mkdir(parents=True)
        (manifest.parent.parent.parent / "me" / "tiny" / "latest").write_text("{}")
        assert ollama.has_model("me/tiny", [tmp_path])

    def test_install_command_per_os(self):
        everything = lambda name: "/bin/" + name
        nothing = lambda name: None
        assert ollama.install_command("win32", "cuda", everything)[:2] == ["winget", "install"]
        assert ollama.install_command("darwin", "cpu", everything) == ["brew", "install", "ollama"]
        assert ollama.install_command("linux", "rocm", everything)[-1] == "ollama-rocm"
        assert ollama.install_command("linux", "cpu", lambda n: "/bin/curl" if n == "curl" else None)[0] == "sh"
        assert ollama.install_command("darwin", "cpu", nothing) is None


class TestDownloads:
    def test_plan_follows_the_profile(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text(f'[engine]\ndata_dir = "{tmp_path}"\n')
        names = [(d.source, d.name) for d in download.plan_downloads(load_config(path))]
        assert names == [("huggingface", "openai/whisper-large-v3-turbo"),
                         ("huggingface", "facebook/wav2vec2-lv-60-espeak-cv-ft"),
                         ("huggingface", "dipumiah018/deep-phonemizer-en-us"),  # the chain's guesser
                         ("huggingface", "hexgrad/Kokoro-82M"), ("ollama", "qwen3:8b")]  # fmt: skip
        path.write_text(f'profile = "cpu-light"\n[engine]\ndata_dir = "{tmp_path}"\n')
        assert download.plan_downloads(load_config(path))[0] == Download("faster-whisper", "base.en", 0.15)

    def test_cache_paths(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
        assert download.cache_path(Download("huggingface", "a/b")) == tmp_path / "models--a--b"
        assert download.cache_path(Download("faster-whisper", "small.en")) == \
            tmp_path / "models--Systran--faster-whisper-small.en"  # fmt: skip
        assert download.cache_path(Download("ollama", "qwen3:8b")) is None


# --- setup wizard -----------------------------------------------------------------------


class Recorder:
    """Stands in for subprocess.run: records commands, succeeds unless told otherwise."""

    def __init__(self, fail=()):
        self.commands, self.fail = [], fail

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        code = 1 if any(f in " ".join(map(str, cmd)) for f in self.fail) else 0
        return CompletedProcess(cmd, code, stdout="", stderr="boom" if code else "")

    def ran(self, needle):
        return [c for c in self.commands if needle in " ".join(map(str, c))]


def setup_args(tmp_path, **overrides):
    args = dict(config=tmp_path / "root" / "config.toml", profile=None, accelerator=None, yes=True,
                no_download=False, no_ollama=False, no_launcher=True, no_verify=False, dry_run=False,
                installed_version=None, role="full", server_url=None, server_header=None, invite=None,
                device_name=None, grammar=False, no_grammar=False)  # fmt: skip
    args.update(overrides)
    return Namespace(**args)


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # service files and launchers land here, not in ~
    (root / "config.example.toml").write_text(f'[engine]\ndata_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setattr(wizard, "project_root", lambda: root)
    monkeypatch.setattr(state, "find_uv", lambda: "uv")
    monkeypatch.setattr(wizard.ollama, "find", lambda: "/bin/ollama")
    monkeypatch.setattr(wizard.ollama, "has_model", lambda name: False)
    monkeypatch.setattr(wizard, "downloaded", lambda d: False)
    pulled = []
    monkeypatch.setattr(wizard.ollama, "pull", lambda name, binary, runner: pulled.append(name) or True)
    return SimpleNamespace(path=root, pulled=pulled)


def run_setup(tmp_path, runner, hw, **overrides):
    console = wizard.Console(interactive=False, out=lambda text="": None)
    setup = wizard.Setup(setup_args(tmp_path, **overrides), console, runner=runner, detect=lambda root: hw)
    return setup.run_all()


class TestSetup:
    def test_full_run_on_an_nvidia_machine(self, tmp_path, install_root):
        runner = Recorder()
        assert run_setup(tmp_path, runner, machine([nvidia(12)]), installed_version="v1.2") == 0
        assert load_config(install_root.path / "config.toml").profile == "standard"  # recommended for 12 GB
        saved = state.InstallState.load(install_root.path)
        assert (saved.accelerator, saved.version) == ("cuda", "v1.2")
        (sync,) = runner.ran("uv sync")
        assert sync[sync.index("--extra") + 3] == "cuda"
        assert runner.ran("zephvox.install.download") and runner.ran("selftest.wav")
        assert install_root.pulled == []  # grammar is opt-in: no Ollama model unless asked

    def test_grammar_flag_installs_the_model_and_keeps_it_on(self, tmp_path, install_root):
        assert run_setup(tmp_path, Recorder(), machine([nvidia(12)]), no_verify=True, grammar=True) == 0
        assert install_root.pulled == ["qwen3:8b"]
        assert load_user_settings(tmp_path / "data")["analyzers"] == ["pronunciation", "grammar"]
        install_root.pulled.clear()
        assert run_setup(tmp_path, Recorder(), machine([nvidia(12)]), no_verify=True) == 0  # rerun, no flag
        assert load_user_settings(tmp_path / "data")["analyzers"] == ["pronunciation", "grammar"]

    def test_events_mode_reports_low_disk_space_as_an_error(self, tmp_path, install_root, capsys):
        console = wizard.Console(interactive=False, events=True)
        setup = wizard.Setup(setup_args(tmp_path, progress_json=True), console, runner=Recorder(),
                             detect=lambda root: machine([nvidia(12)], disk=1.0))  # fmt: skip
        assert setup.run_all() == 1
        from zephvox.install import events

        parsed = [e for line in capsys.readouterr().out.splitlines() if (e := events.parse(line))]
        assert [e for e in parsed if e["type"] == "error"][0]["message"].startswith("There isn't enough free disk space")
        assert any(e["type"] == "step" for e in parsed)

    def test_a_server_address_means_client(self, tmp_path, install_root):
        setup = wizard.Setup(setup_args(tmp_path, role=None, server_url="https://s.example"),
                             wizard.Console(interactive=False, out=lambda text="": None), runner=Recorder(),
                             detect=lambda r: machine([nvidia(12)]))  # fmt: skip
        assert setup.choose_role() == "client"

    def test_full_by_default_and_switching_a_client_to_full(self, tmp_path, install_root):
        # No role given: the full app, without a question. An old server-only install becomes full too.
        saved = state.InstallState(role="server")
        saved.save(install_root.path)
        assert run_setup(tmp_path, Recorder(), machine([nvidia(12)]), role=None) == 0
        saved = state.InstallState.load(install_root.path)
        assert saved.role == "full" and "desktop" in saved.extras
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["setup", "--role", "server"])  # no server-only install any more
        # A client install becoming full: it offers to analyze locally (default yes).
        saved.role = "client"
        saved.save(install_root.path)
        data_dir = tmp_path / "data"
        save_user_settings(data_dir, {"remote": {"mode": "remote", "url": "https://s.example"}})
        assert run_setup(tmp_path, Recorder(), machine([nvidia(12)]), role="full") == 0
        assert state.InstallState.load(install_root.path).role == "full"
        assert load_user_settings(data_dir)["remote"] == {"mode": "local", "url": "https://s.example"}

    def test_profile_flag_and_cpu_build(self, tmp_path, install_root):
        runner = Recorder()
        assert run_setup(tmp_path, runner, machine([nvidia(12)]), profile="cpu", no_download=True) == 0
        assert state.InstallState.load(install_root.path).accelerator == "cpu"
        assert not runner.ran("zephvox.install.download") and install_root.pulled == []

    def test_existing_profile_is_the_default_on_a_rerun(self, tmp_path, install_root):
        (install_root.path / "config.toml").write_text(f'profile = "small"\n[engine]\ndata_dir = "{tmp_path / "data"}"\n')
        run_setup(tmp_path, Recorder(), machine([nvidia(24)]), no_verify=True)
        assert load_config(install_root.path / "config.toml").profile == "small"

    def test_without_ollama_grammar_is_turned_off(self, tmp_path, install_root, monkeypatch):
        monkeypatch.setattr(wizard.ollama, "find", lambda: None)
        monkeypatch.setattr(wizard.ollama, "install_command", lambda *a: None)
        assert run_setup(tmp_path, Recorder(), machine([nvidia(12)]), no_verify=True, grammar=True) == 0
        assert load_user_settings(tmp_path / "data")["analyzers"] == ["pronunciation"]
        assert install_root.pulled == []

    def test_grammar_is_optional_and_its_needs_are_shown(self, tmp_path, install_root):
        shown = []
        asked = []
        console = wizard.Console(interactive=True, ask=lambda q: asked.append(q) or "n", out=shown.append)
        setup = wizard.Setup(setup_args(tmp_path, yes=False, profile="standard", no_launcher=True, grammar=True), console,
                             runner=Recorder(), detect=lambda root: machine([nvidia(12)]))  # fmt: skip
        assert setup.run_all() == 1  # the final "Continue?" answer is also "n"
        text = "\n".join(shown)
        assert "Grammar checking (optional)" in text and "Ollama" in text and "qwen3:8b" in text and "GPU memory" in text
        assert any("Check grammar too?" in q for q in asked)

    def test_grammar_is_off_by_default_and_not_asked(self, tmp_path, install_root):
        shown = []
        console = wizard.Console(interactive=False, out=shown.append)
        setup = wizard.Setup(setup_args(tmp_path, profile="standard"), console, runner=Recorder(),
                             detect=lambda root: machine([nvidia(12)]))  # fmt: skip
        assert setup.run_all() == 0
        assert "Check grammar too?" not in "\n".join(shown) and install_root.pulled == []
        assert "grammar" not in load_config(install_root.path / "config.toml").engine.analyzers

    def test_dry_run_changes_nothing(self, tmp_path, install_root):
        runner = Recorder()
        assert run_setup(tmp_path, runner, machine([nvidia(12)]), dry_run=True) == 0
        assert runner.commands == [] and not (install_root.path / "config.toml").exists()

    def test_failed_self_test_and_amd_fix(self, tmp_path, install_root):
        runner = Recorder(fail=("selftest.wav", "torch.cuda"))
        amd = GPU("amd", "RX 6800 XT", 16.0, "rocm", "gfx1030")
        console = wizard.Console(interactive=True, ask=lambda q: "y", out=lambda text="": None)
        setup = wizard.Setup(setup_args(tmp_path, yes=False, profile="large"), console, runner=runner,
                             detect=lambda root: machine([amd]))  # fmt: skip
        assert setup.run_all() == 1
        assert "HSA_OVERRIDE_GFX_VERSION=10.3.0" in (install_root.path / ".env").read_text()

    def test_console_choices(self):
        answers = iter(["9", "x", "2"])
        console = wizard.Console(interactive=True, ask=lambda q: next(answers), out=lambda text="": None)
        assert console.choose("Pick", [("a", "A"), ("b", "B")], "a") == "b"
        answers = iter(["maybe", ""])
        assert console.confirm("Sure?", default=False) is False
        assert wizard.gb(None) == "size unknown" and wizard.gb(0.15) == "150 MB" and wizard.gb(2) == "2.0 GB"


# --- update / uninstall -----------------------------------------------------------------


def source_archive(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"zephvox-1.1/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestUpdate:
    def test_latest_version(self):
        tag = update.latest_version(lambda url: json.dumps({"tag_name": "v1.1"}).encode())
        assert tag == ("v1.1", "https://github.com/dipumiah/zephvox/archive/refs/tags/v1.1.tar.gz")

        def offline(url):
            raise OSError("no network")

        assert update.latest_version(offline)[0] == "main"

    def test_update_replaces_code_and_keeps_user_files(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        (root / "src" / "old").mkdir(parents=True)
        for name in ("config.toml", ".env", "README.md"):
            (root / name).write_text("old " + name)
        state.InstallState("cuda", state.extras_for("cuda"), "v1.0").save(root)
        monkeypatch.setattr(update, "project_root", lambda: root)
        monkeypatch.setattr(state, "find_uv", lambda: "uv")
        archive = source_archive({"README.md": "new", "src/new.py": "", "config.toml": "shipped?"})

        def fetch(url):
            return json.dumps({"tag_name": "v1.1"}).encode() if "api.github" in url else archive

        runner = Recorder()
        assert update.run_update(Namespace(force=False), fetch, runner) == 0
        assert (root / "README.md").read_text() == "new" and (root / "src" / "new.py").exists()
        assert not (root / "src" / "old").exists()
        assert (root / "config.toml").read_text() == "old config.toml" and (root / ".env").read_text() == "old .env"
        assert state.InstallState.load(root).version == "v1.1"
        assert "cuda" in runner.ran("uv sync")[0]
        assert update.run_update(Namespace(force=False), fetch, runner) == 0  # now up to date
        assert len(runner.ran("uv sync")) == 1

    def test_update_stops_a_running_app_first_and_starts_it_again(self, tmp_path, monkeypatch, capsys):
        root = tmp_path / "root"
        (root / "src").mkdir(parents=True)
        state.InstallState("cpu", state.extras_for("cpu"), "v1.0").save(root)
        monkeypatch.setattr(update, "project_root", lambda: root)
        monkeypatch.setattr(state, "find_uv", lambda: "uv")
        archive = source_archive({"README.md": "new", "src/new.py": ""})
        fetch = lambda url: json.dumps({"tag_name": "v1.1"}).encode() if "api.github" in url else archive  # noqa: E731
        order = []
        monkeypatch.setattr(update, "_data_dir", lambda args: tmp_path / "data")
        monkeypatch.setattr(update, "find_daemon", lambda d: "http://127.0.0.1:8765")
        monkeypatch.setattr(update, "stop_daemon", lambda d: order.append("stop"))
        monkeypatch.setattr(update, "start_daemon", lambda r, d: order.append("start") or "http://127.0.0.1:8765")
        runner = Recorder()
        orig_replace = update.replace_tree
        monkeypatch.setattr(update, "replace_tree", lambda a, b: (order.append("replace"), orig_replace(a, b))[1])
        assert update.run_update(Namespace(force=False, no_restart=False), fetch, runner) == 0
        assert order == ["stop", "replace", "start"]  # its files change only while it is stopped
        assert "running again at http://127.0.0.1:8765" in capsys.readouterr().out

        # --no-restart leaves it stopped, and says how to start it
        state.InstallState("cpu", state.extras_for("cpu"), "v1.0").save(root)
        order.clear()
        assert update.run_update(Namespace(force=False, no_restart=True), fetch, runner) == 0
        assert order == ["stop", "replace"] and "zephvox serve" in capsys.readouterr().out

    def test_update_leaves_zephvox_running_when_the_download_fails(self, tmp_path, monkeypatch, capsys):
        root = tmp_path / "root"
        root.mkdir()
        state.InstallState("cpu", state.extras_for("cpu"), "v1.0").save(root)
        monkeypatch.setattr(update, "project_root", lambda: root)
        stopped = []
        monkeypatch.setattr(update, "find_daemon", lambda d: "http://127.0.0.1:8765")
        monkeypatch.setattr(update, "stop_daemon", lambda d: stopped.append(True))

        def offline(url):
            raise OSError("no network")

        assert update.run_update(Namespace(force=False), offline) == 2
        assert stopped == [] and "no network" in capsys.readouterr().err

    def test_update_refuses_a_git_checkout(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(update, "project_root", lambda: tmp_path)
        assert update.run_update(Namespace(force=False), lambda url: b"") == 2
        assert "git pull" in capsys.readouterr().err

    def test_uninstall_keeps_a_checkout_and_asks_about_data(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        removed = []
        monkeypatch.setattr(update, "project_root", lambda: tmp_path)
        monkeypatch.setattr("zephvox.desktop.remove_launcher", lambda: removed.append("launcher"))
        monkeypatch.setattr("zephvox.desktop.remove_command", lambda: removed.append("command"))
        data = tmp_path / "data"
        data.mkdir()
        cfg = tmp_path / "c.toml"
        cfg.write_text(f'[engine]\ndata_dir = "{data}"\n')
        assert update.run_uninstall(Namespace(yes=True, config=cfg)) == 0
        assert removed == ["launcher", "command"] and data.exists() and tmp_path.exists()


def test_selftest_clip_ships_with_the_package():
    assert wizard.SELFTEST_CLIP.exists() and wizard.SELFTEST_CLIP.stat().st_size > 10_000
    assert Path(wizard.SELFTEST_CLIP).suffix == ".wav"


def test_update_and_uninstall_refuse_inside_the_desktop_app(monkeypatch, capsys):
    monkeypatch.setenv("ZEPHVOX_MANAGED", "1")
    assert update.run_update(Namespace(force=False, no_restart=False)) == 2
    assert "desktop app" in capsys.readouterr().err
    assert update.run_uninstall(Namespace(yes=True, config=None)) == 2
    assert "uninstaller" in capsys.readouterr().err
