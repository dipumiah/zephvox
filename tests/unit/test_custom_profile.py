"""Setup's Custom profile: the questions, the saved file, and how it layers into the config."""

import pytest

from zephvox.engine import profiles
from zephvox.engine.config import load_config
from zephvox.engine.errors import ZephvoxError
from zephvox.install import custom, state, wizard
from tests.unit.test_install import Recorder, install_root, machine, nvidia, setup_args  # noqa: F401  (fixture)


def interactive_setup(tmp_path, answers, hw, **overrides):
    replies = iter(answers)
    console = wizard.Console(interactive=True, ask=lambda q: next(replies, ""), out=lambda text="": None)
    runner = Recorder()
    setup = wizard.Setup(setup_args(tmp_path, yes=False, **overrides), console, runner=runner, detect=lambda r: hw)
    return setup, runner


class TestCustomFile:
    def test_layers_between_defaults_and_config_toml(self, tmp_path):
        path = tmp_path / "config.toml"
        profiles.save_custom(profiles.custom_path(path), {"provider": {"openai-compat": {"model": "qwen3:4b"},
                                                                       "kokoro": {"voice": "bm_george"}}})  # fmt: skip
        path.write_text('profile = "custom"\n[provider.kokoro]\nvoice = "bf_emma"\n')
        config = load_config(path)
        assert config.profile == "custom"
        assert config.provider_settings("openai-compat")["model"] == "qwen3:4b"  # from the custom profile
        assert config.provider_settings("kokoro")["voice"] == "bf_emma"  # config.toml wins

    def test_missing_file_means_defaults(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('profile = "custom"\n')
        assert load_config(path).provider_settings("openai-compat") == {}
        assert profiles.get("custom", path).gpu

    def test_unknown_profile_lists_custom(self):
        with pytest.raises(ZephvoxError, match="custom"):
            profiles.get("huge")


class TestChoices:
    @pytest.mark.parametrize("preset", ["large", "standard", "cpu", "cpu-light"])
    def test_enter_throughout_keeps_the_preset(self, tmp_path, preset, monkeypatch):
        monkeypatch.setenv("ZEPHVOX_HOME", str(tmp_path / "data"))  # not the real settings.json
        hw = machine([nvidia(16)])
        start = wizard.preview_config(tmp_path / "config.toml", preset, tmp_path / "none.toml")
        console = wizard.Console(interactive=True, ask=lambda q: "", out=lambda text="": None)
        chosen = custom.ask(console, hw, custom.defaults_from(start, hw))
        again = wizard.preview_config(tmp_path / "config.toml", "custom", tmp_path / "none.toml",
                                      custom.overlay_for(chosen))  # fmt: skip
        for kind in ("stt", "tts"):
            assert again.providers[kind][0] == start.providers[kind][0]
        assert again.engine.analyzers == start.engine.analyzers
        assert "grammar" not in again.engine.analyzers  # opt-in: pressing Enter never turns it on

    def test_api_and_british(self):
        hw = machine([nvidia(8)])
        answers = iter(["1", "", "2", "6", "https://api.example.com/v1", "gpt-x", "EXAMPLE_KEY", "2", "8"])
        console = wizard.Console(interactive=True, ask=lambda q: next(answers), out=lambda text="": None)
        c = custom.ask(console, hw, custom.Choices())
        overlay = custom.overlay_for(c)
        assert overlay["provider"]["openai-compat"] == {"model": "gpt-x", "base_url": "https://api.example.com/v1",
                                                        "autostart": False, "api_key_env": "EXAMPLE_KEY"}  # fmt: skip
        assert overlay["provider"]["misaki"] == {"accent": "en-gb"}
        assert overlay["provider"]["kokoro"]["voice"] == "bm_george"  # picked from the voice list
        assert "API" in custom.describe(c) and "British" in custom.describe(c)

    def test_memory_warning_and_no_gpu(self):
        lines = []
        answers = iter(["", "", "", "", ""])
        console = wizard.Console(interactive=True, ask=lambda q: next(answers), out=lines.append)
        custom.ask(console, machine([nvidia(8)]), custom.Choices())
        assert any("qwen3:14b" in line and "may not fit in 8 GB" in line for line in lines)
        lines.clear()
        answers = iter(["", "", "", ""])
        c = custom.ask(console, machine([]), custom.Choices())
        assert c.device == "cpu" and c.stt == "small.en" and any("No usable GPU" in line for line in lines)


class TestSetupWithCustom:
    def test_custom_answers_are_installed(self, tmp_path, install_root):  # noqa: F811  (fixture)
        # Custom; CPU; base.en; British; grammar off; no voice; use it; continue; don't start
        answers = ["6", "2", "1", "2", "7", "4", "y", "y", "n"]
        setup, runner = interactive_setup(tmp_path, answers, machine([nvidia(12)]))
        assert setup.run_all() == 0
        config_path = install_root.path / "config.toml"
        config = load_config(config_path)
        assert config.profile == "custom" and profiles.custom_path(config_path).exists()
        assert config.providers["stt"][0] == "faster-whisper" and config.provider_settings("faster-whisper")["model"] == "base.en"
        assert config.engine.analyzers == ["pronunciation"] and config.providers["tts"] == []
        assert config.provider_settings("misaki")["accent"] == "en-gb"
        assert state.InstallState.load(install_root.path).accelerator == "cpu"  # CPU chosen: small PyTorch build
        assert install_root.pulled == []  # grammar off: no Ollama model

        # Setup again: Custom starts from the saved choices, so Enter keeps them.
        setup, _ = interactive_setup(tmp_path, ["", "", "", "", "", "", "y", "y", "n"], machine([nvidia(12)]))
        assert setup.run_all() == 0
        assert load_config(config_path).provider_settings("faster-whisper")["model"] == "base.en"

    def test_without_a_terminal_custom_needs_saved_choices(self, tmp_path, install_root):  # noqa: F811  (fixture)
        console = wizard.Console(interactive=False, out=lambda text="": None)
        setup = wizard.Setup(setup_args(tmp_path, profile="custom"), console, runner=Recorder(),
                             detect=lambda r: machine([nvidia(12)]))  # fmt: skip
        with pytest.raises(ZephvoxError, match="custom-profile.toml"):
            setup.run_all()
        profiles.save_custom(profiles.custom_path(install_root.path / "config.toml"),
                             {"engine": {"analyzers": ["pronunciation", "grammar"]},
                              "provider": {"openai-compat": {"model": "qwen3:4b"}}})  # fmt: skip
        assert setup.run_all() == 0
        assert install_root.pulled == ["qwen3:4b"]
