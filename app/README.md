# zephvox desktop app

A [Tauri 2](https://tauri.app) shell (Rust, ~300 lines) around the Python engine and its React page. It gives
zephvox a real installer for Windows, Linux and macOS, a tray icon, and updates from the latest GitHub release.
The engine stays Python: PyTorch, Whisper, Kokoro and the scoring have no equivalent worth rewriting.

```
installer (22 MB Windows, 28 MB macOS, 32 MB .deb, ~105 MB AppImage, which carries WebKit)
 ├─ the shell: window, tray, updater, supervises the engine
 ├─ resources/engine.tar.gz   the engine source for this release (pyproject, uv.lock, src/, ...)
 ├─ resources/uv/uv           uv for this platform
 └─ resources/manifest.json   engine version, API version, tarball sha256

first start (progress shown on the start-up screen, ui/index.html)
 1. unpack the engine to <app data>/runtime/<version>-<hash>/        (checksum verified)
 2. uv installs Python + the core packages into its .venv            (lockfile, --frozen)
 3. `zephvox setup --yes --progress-json` picks the PyTorch build for the GPU, installs it, downloads
    every speech model (per-model progress) and runs a test analysis
 4. the engine starts: `python -m zephvox.cli serve --port 0 --print-ready --exit-on-stdin-close`
    and the window shows its page

later starts: just step 4.
```

Nothing in the app path is a shell script: the shell runs programs with argument lists, hides console
windows on Windows, and the engine's stdin is a pipe the shell holds, so the engine stops when the app
does (however the app ends).

## Updates

The shell checks `releases/latest/download/latest.json` at start-up and daily (tray: *Check for updates*). The
update waits until you aren't recording or analyzing, asks, shows progress on the start-up screen, installs
(signature verified) and restarts. The new installer carries the matching engine, so on its first start the
shell builds `runtime/<new version>/` next to the old one:

- the user's `config.toml`, `.env`, `.install.json` and `custom-profile.toml` are copied over;
- `uv sync` installs the same extras as before; with the uv cache on the same disk, unchanged packages are
  hard-linked, not downloaded (a PyTorch bump is the only large download);
- the new engine must start and answer `/healthz` with the version and API version this build ships. If it
  doesn't, the version is remembered as failed, the previous engine runs, and the user is told;
- the previous release folder is kept as the rollback, older ones are removed.

Models (Hugging Face cache), recordings and history (the engine's data folder) are outside `runtime/` and are
never touched. After an update the engine's start-up downloads any model the new profile needs in the
background, beside the old ones.

`zephvox update` and `zephvox uninstall` refuse inside the app (`ZEPHVOX_MANAGED=1`): the app owns them.

## Building

Needs Rust, Node 22, Python 3.12 + uv, and on Linux the [Tauri libraries](https://tauri.app/start/prerequisites/).

```sh
python app/scripts/prepare_resources.py          # engine tarball (from git HEAD!) + uv + manifest
cd app
npm install
npm run icons                                    # app icons from docs/assets/logo.svg
npx tauri dev                                    # or: npm run build
cd src-tauri && cargo test                       # the shell's own tests
```

`prepare_resources.py` archives `git HEAD`, so commit first: uncommitted engine changes are not in the app.

## Releasing

1. One-time: `npx tauri signer generate -w ~/.tauri/zephvox.key`; put the public key in
   `src-tauri/tauri.conf.json` (`plugins.updater.pubkey`) and the private key and password in the repository
   secrets `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. Lose that key and installed apps
   can't be updated.
2. Set the version everywhere with `python scripts/bump_version.py X.Y.Z` (the engine must have the same
   version as the app, so it also updates `__init__.py` and both lockfiles). Commit it.
3. Tag `vX.Y.Z` and push. `.github/workflows/release.yml` checks the tag against the files, tests, builds each
   platform, attaches the installers and `latest.json` to a draft release, and publishes it (with generated
   notes) when every platform built.

**Trying a release first:** `python scripts/bump_version.py X.Y.Z-rc.1` and tag `vX.Y.Z-rc.1`. Tags containing
`-rc` are published as *pre-releases*, which GitHub's "latest release" ignores, so neither the app's update check
nor the install scripts see them. Download the installer from the release page and try it. To test an update
from an rc build, point `plugins.updater.endpoints` at that release's own `latest.json` in a local build.

Signing: without an Apple Developer ID (notarization) and a Windows code-signing certificate, macOS Gatekeeper
and Windows SmartScreen warn on first launch. The Apple/Windows secrets in the workflow are optional; unsigned
builds work, with those warnings.

## Known limits

- **Only compiled and unit-tested on Linux so far** (`cargo test`, clippy and fmt are clean; the app itself has
  not been run, and nothing has been built on Windows or macOS). The first run on Windows (Defender, long paths), macOS (microphone / input-monitoring prompts, which attach to the
  app that started the engine) and Linux (Wayland shortcuts through the portal) needs a person to try it.
- The engine, not the shell, still records the microphone and registers the global shortcuts (pynput / portal).
  If macOS attributes those permissions to the Python binary rather than the app, the fix is to move capture and
  shortcuts into the shell; the engine would then only analyze.
- macOS needs the `app` target as well as `dmg`: only it produces the `.app.tar.gz` that `latest.json` points macOS updates at.
- Windows ships an NSIS `.exe` only: the `.msi` format can't carry a pre-release version like `0.2.0-rc.1`.
- Supported targets follow `tool.uv.environments`: Linux x86_64 and arm64, Windows x64, macOS Apple Silicon.
- AMD and Intel GPUs on Windows use the CPU profile (PyTorch has no build for them).
- No start-at-login yet.
