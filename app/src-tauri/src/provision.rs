//! Building the Python runtime: unpack the bundled engine, let uv fetch Python and the packages,
//! and on the first run let `zephvox setup` pick the right PyTorch build and download the models.
//!
//! Each engine release gets its own folder (`runtime/<version>-<hash>/`) with its own `.venv`, so a
//! failed update never touches the working one, and the old one is the rollback.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Deserialize;
use sha2::{Digest, Sha256};
use tauri::{path::BaseDirectory, AppHandle, Manager};

use crate::paths::{self, Dirs};
use crate::{events, proc};

/// Files that belong to the user, carried from one release folder to the next.
const KEEP: [&str; 4] = [
    "config.toml",
    ".env",
    ".install.json",
    "custom-profile.toml",
];

#[derive(Deserialize, Clone, Debug)]
pub struct Manifest {
    pub engine_version: String,
    pub api_version: u32,
    pub tarball_sha256: String,
    pub uv_version: String,
}

impl Manifest {
    pub fn dir_name(&self) -> String {
        let short: String = self.tarball_sha256.chars().take(8).collect();
        format!("{}-{}", self.engine_version, short)
    }
}

fn resource(app: &AppHandle, rel: &str) -> Result<PathBuf, String> {
    app.path()
        .resolve(rel, BaseDirectory::Resource)
        .map_err(|e| format!("missing bundled file {rel}: {e}"))
}

pub fn load_manifest(app: &AppHandle) -> Result<Manifest, String> {
    let path = resource(app, "resources/manifest.json")?;
    let text =
        fs::read_to_string(&path).map_err(|e| format!("can't read {}: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("bad manifest: {e}"))
}

/// The bundled uv, copied out once to a writable, executable place.
pub fn uv_path(app: &AppHandle, dirs: &Dirs, manifest: &Manifest) -> Result<PathBuf, String> {
    let exe = if cfg!(windows) { "uv.exe" } else { "uv" };
    let dest_dir = dirs.bin_dir().join(format!("uv-{}", manifest.uv_version));
    let dest = dest_dir.join(exe);
    if dest.exists() {
        return Ok(dest);
    }
    let bundled = resource(app, &format!("resources/uv/{exe}"))?;
    fs::create_dir_all(&dest_dir).map_err(|e| e.to_string())?;
    let partial = dest.with_extension("partial");
    fs::copy(&bundled, &partial).map_err(|e| format!("can't copy uv: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&partial, fs::Permissions::from_mode(0o755))
            .map_err(|e| e.to_string())?;
    }
    fs::rename(&partial, &dest).map_err(|e| e.to_string())?;
    Ok(dest)
}

/// Check the archive against the manifest's hash, then unpack it to `dest` (all or nothing).
pub fn unpack(archive: &Path, sha256: &str, dest: &Path) -> Result<(), String> {
    let bytes = fs::read(archive).map_err(|e| format!("can't read {}: {e}", archive.display()))?;
    if hex::encode(Sha256::digest(&bytes)) != sha256 {
        return Err("the bundled engine doesn't match its checksum: reinstall zephvox".into());
    }
    let partial = dest.with_extension("partial");
    let _ = fs::remove_dir_all(&partial);
    fs::create_dir_all(&partial).map_err(|e| e.to_string())?;
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(&bytes[..]));
    tar.set_overwrite(true);
    tar.unpack(&partial)
        .map_err(|e| format!("unpacking the engine failed: {e}"))?;
    let _ = fs::remove_dir_all(dest);
    fs::rename(&partial, dest).map_err(|e| e.to_string())
}

/// The extras the previous release was installed with (`.install.json`), so an update installs the same.
pub fn read_extras(install_json: &Path) -> Vec<String> {
    let Ok(text) = fs::read_to_string(install_json) else {
        return vec![];
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
        return vec![];
    };
    value["extras"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

pub fn uv_sync_args(project: &Path, extras: &[String]) -> Vec<String> {
    let mut args = vec![
        "sync".into(),
        "--project".into(),
        project.display().to_string(),
        "--frozen".into(),
        "--no-dev".into(),
    ];
    for extra in extras {
        args.push("--extra".into());
        args.push(extra.clone());
    }
    args
}

/// The environment every child shares: its own cache and Python (never the system's), UTF-8, managed.
pub fn base_env(cmd: &mut Command, dirs: &Dirs) {
    cmd.env("UV_CACHE_DIR", dirs.uv_cache())
        .env("UV_PYTHON_INSTALL_DIR", dirs.python_dir())
        .env("UV_PYTHON_PREFERENCE", "only-managed")
        .env("UV_NO_PROGRESS", "1")
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("ZEPHVOX_MANAGED", "1");
}

fn failure(app: &AppHandle, what: &str) -> String {
    format!("{what}\n{}", events::tail(app, 8))
}

/// Build the runtime for `manifest`. `previous` is the release folder in use now, if any.
pub fn provision(
    app: &AppHandle,
    dirs: &Dirs,
    manifest: &Manifest,
    previous: Option<&str>,
) -> Result<PathBuf, String> {
    let target = dirs.version(&manifest.dir_name());
    let prev_dir = previous.map(|p| dirs.version(p)).filter(|p| p.exists());
    let first_run = prev_dir.is_none();

    events::step(app, "Unpacking zephvox");
    fs::create_dir_all(dirs.runtime()).map_err(|e| e.to_string())?;
    unpack(
        &resource(app, "resources/engine.tar.gz")?,
        &manifest.tarball_sha256,
        &target,
    )?;
    if let Some(prev) = &prev_dir {
        for name in KEEP {
            let from = prev.join(name);
            if from.exists() {
                fs::copy(&from, target.join(name))
                    .map_err(|e| format!("can't carry over {name}: {e}"))?;
            }
        }
    }

    let uv = uv_path(app, dirs, manifest)?;
    let extras = if first_run {
        vec![]
    } else {
        read_extras(&target.join(".install.json"))
    };
    events::step(
        app,
        if first_run {
            "Installing Python and the core packages"
        } else {
            "Updating the packages"
        },
    );
    let mut sync = Command::new(&uv);
    sync.args(uv_sync_args(&target, &extras));
    base_env(&mut sync, dirs);
    let status = proc::run_streaming(app, sync).map_err(|e| format!("can't run uv: {e}"))?;
    if !status.success() {
        return Err(failure(app, "Installing the Python packages failed."));
    }

    let python = paths::venv_python(&target);
    if first_run {
        // The setup wizard picks the PyTorch build for this computer's GPU, installs it, downloads the
        // models (reporting each one's progress) and runs a test analysis. Never asks questions here.
        events::step(app, "Setting up for this computer");
        let mut setup = Command::new(&python);
        setup.args([
            "-m",
            "zephvox.cli",
            "setup",
            "--yes",
            "--progress-json",
            "--no-launcher",
            "--installed-version",
        ]);
        setup.arg(&manifest.engine_version);
        setup.current_dir(&target);
        base_env(&mut setup, dirs);
        setup.env("UV", &uv);
        let status =
            proc::run_streaming(app, setup).map_err(|e| format!("can't run setup: {e}"))?;
        if !status.success() {
            return Err(failure(app, "Setting up zephvox failed."));
        }
    } else {
        events::step(app, "Checking the update");
        let mut check = Command::new(&python);
        check.args(["-c", "import zephvox.web.server"]);
        check.current_dir(&target);
        base_env(&mut check, dirs);
        let status =
            proc::run_streaming(app, check).map_err(|e| format!("can't run python: {e}"))?;
        if !status.success() {
            return Err(failure(app, "The updated engine doesn't start."));
        }
    }
    fs::write(target.join(".provisioned"), manifest.dir_name()).map_err(|e| e.to_string())?;
    Ok(target)
}

/// Remove release folders other than the ones named (the one in use and the rollback).
pub fn prune(dirs: &Dirs, keep: &[&str]) {
    let Ok(entries) = fs::read_dir(dirs.runtime()) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if entry.path().is_dir() && !keep.contains(&name.as_str()) {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dir_name_has_version_and_short_hash() {
        let m = Manifest {
            engine_version: "1.2.3".into(),
            api_version: 1,
            tarball_sha256: "abcdef0123456789".into(),
            uv_version: "0".into(),
        };
        assert_eq!(m.dir_name(), "1.2.3-abcdef01");
    }

    #[test]
    fn sync_args_carry_the_extras() {
        let args = uv_sync_args(Path::new("/r"), &["models".into(), "cuda".into()]);
        assert_eq!(
            args,
            [
                "sync",
                "--project",
                "/r",
                "--frozen",
                "--no-dev",
                "--extra",
                "models",
                "--extra",
                "cuda"
            ]
        );
    }

    #[test]
    fn extras_come_from_install_json() {
        let tmp = std::env::temp_dir().join(format!("zephvox-extras-{}.json", std::process::id()));
        fs::write(
            &tmp,
            r#"{"accelerator":"cuda","extras":["models","cuda"],"version":"x"}"#,
        )
        .unwrap();
        assert_eq!(read_extras(&tmp), ["models", "cuda"]);
        assert!(read_extras(Path::new("/nonexistent/.install.json")).is_empty());
        let _ = fs::remove_file(tmp);
    }

    #[test]
    fn unpack_rejects_a_wrong_checksum() {
        let tmp = std::env::temp_dir().join(format!("zephvox-unpack-{}", std::process::id()));
        fs::create_dir_all(&tmp).unwrap();
        let archive = tmp.join("a.tar.gz");
        fs::write(&archive, b"not an archive").unwrap();
        let err = unpack(&archive, "deadbeef", &tmp.join("out")).unwrap_err();
        assert!(err.contains("checksum"));
        let _ = fs::remove_dir_all(tmp);
    }
}
