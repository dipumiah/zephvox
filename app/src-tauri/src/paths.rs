//! Where the app keeps things. Only `runtime/` is ever replaced by an update; models live in the
//! Hugging Face cache and the user's recordings in the engine's own data folder, both outside it.

use std::fs;
use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

pub struct Dirs {
    data: PathBuf,
}

impl Dirs {
    pub fn new(app: &AppHandle) -> Result<Self, String> {
        let data = app
            .path()
            .app_local_data_dir()
            .map_err(|e| format!("no data folder: {e}"))?;
        fs::create_dir_all(&data).map_err(|e| format!("can't create {}: {e}", data.display()))?;
        Ok(Self { data })
    }

    #[cfg(test)]
    pub fn at(data: PathBuf) -> Self {
        Self { data }
    }

    /// One folder per engine release inside; each is a complete install root (sources + .venv).
    pub fn runtime(&self) -> PathBuf {
        self.data.join("runtime")
    }
    pub fn version(&self, name: &str) -> PathBuf {
        self.runtime().join(name)
    }
    /// Text file naming the release folder in use.
    pub fn current_file(&self) -> PathBuf {
        self.runtime().join("current")
    }
    /// Text file naming a release that failed its health check, so it isn't retried every launch.
    pub fn failed_file(&self) -> PathBuf {
        self.runtime().join("failed")
    }
    /// Kept beside the runtime (same disk) so uv can hard-link packages instead of copying them.
    pub fn uv_cache(&self) -> PathBuf {
        self.data.join("uv-cache")
    }
    pub fn python_dir(&self) -> PathBuf {
        self.data.join("python")
    }
    pub fn bin_dir(&self) -> PathBuf {
        self.data.join("bin")
    }
    pub fn logs_dir(&self) -> PathBuf {
        self.data.join("logs")
    }
}

pub fn venv_python(version_dir: &Path) -> PathBuf {
    if cfg!(windows) {
        version_dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        version_dir.join(".venv").join("bin").join("python")
    }
}

pub fn read_name(file: &Path) -> Option<String> {
    let text = fs::read_to_string(file).ok()?;
    let name = text.trim();
    // A folder name only: never let a damaged file point outside the runtime folder.
    (!name.is_empty() && !name.contains(['/', '\\']) && name != "." && name != "..")
        .then(|| name.to_string())
}

pub fn write_name(file: &Path, name: &str) -> Result<(), String> {
    if let Some(parent) = file.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    // Written next to it and renamed, so a crash never leaves a half-written pointer.
    let tmp = file.with_extension("tmp");
    fs::write(&tmp, name).map_err(|e| e.to_string())?;
    fs::rename(&tmp, file).map_err(|e| e.to_string())
}

pub fn provisioned(version_dir: &Path) -> bool {
    version_dir.join(".provisioned").exists() && venv_python(version_dir).exists()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pointer_files_round_trip_and_reject_paths() {
        let tmp = std::env::temp_dir().join(format!("zephvox-paths-{}", std::process::id()));
        let dirs = Dirs::at(tmp.clone());
        assert_eq!(read_name(&dirs.current_file()), None);
        write_name(&dirs.current_file(), "0.1.0-abcd1234").unwrap();
        assert_eq!(
            read_name(&dirs.current_file()).as_deref(),
            Some("0.1.0-abcd1234")
        );
        write_name(&dirs.current_file(), "../evil").unwrap();
        assert_eq!(read_name(&dirs.current_file()), None);
        let _ = fs::remove_dir_all(tmp);
    }
}
