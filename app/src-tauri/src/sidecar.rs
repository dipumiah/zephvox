//! The Python engine as a child process: `python -m zephvox.cli serve --port 0 --print-ready`.
//!
//! It tells us its port with one `ZEPHVOX_READY {json}` line. Its stdin is a pipe we hold open; when
//! we exit (however we exit) the pipe closes and the engine shuts itself down, so it can't be left
//! running behind us.

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::paths::{self, Dirs};
use crate::provision::{base_env, Manifest};
use crate::{http, proc};

const READY_PREFIX: &str = "ZEPHVOX_READY ";

pub struct Sidecar {
    child: Child,
    stdin: Option<ChildStdin>,
    pub url: String,
    pub port: u16,
    /// A daemon that was already running (started from a terminal): we use it but don't stop it.
    pub attached: bool,
}

pub fn parse_ready(line: &str) -> Option<Value> {
    let value: Value = serde_json::from_str(line.trim().strip_prefix(READY_PREFIX)?).ok()?;
    value.get("port")?.as_u64()?;
    value.get("url")?.as_str()?;
    Some(value)
}

/// Does what `/healthz` reports match the engine this app was built with?
pub fn check_health(health: &Value, manifest: &Manifest) -> Result<(), String> {
    let version = health["engine_version"].as_str().unwrap_or("?");
    let api = health["api_version"].as_u64().unwrap_or(0);
    if health["ok"] != true
        || version != manifest.engine_version
        || api != u64::from(manifest.api_version)
    {
        return Err(format!(
            "the engine reports version {version} (API {api}); this app needs {} (API {})",
            manifest.engine_version, manifest.api_version
        ));
    }
    Ok(())
}

impl Sidecar {
    /// `expect`: the engine version this build ships; None when running an older one on purpose (rollback),
    /// whose page and API are its own, so only "it answers" is checked.
    pub fn start(
        dirs: &Dirs,
        expect: Option<&Manifest>,
        version_dir: &Path,
        timeout: Duration,
    ) -> Result<Sidecar, String> {
        let python = paths::venv_python(version_dir);
        fs::create_dir_all(dirs.logs_dir()).map_err(|e| e.to_string())?;
        let log_path = dirs.logs_dir().join("engine.log");
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| e.to_string())?;

        let mut cmd = Command::new(&python);
        cmd.args([
            "-m",
            "zephvox.cli",
            "serve",
            "--port",
            "0",
            "--print-ready",
            "--exit-on-stdin-close",
        ])
        .current_dir(version_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log));
        base_env(&mut cmd, dirs);
        proc::quiet(&mut cmd);
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("can't start the engine ({}): {e}", python.display()))?;
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("piped");

        let (tx, rx) = mpsc::channel::<Value>();
        thread::spawn(move || {
            let mut sent = false;
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if !sent {
                    if let Some(ready) = parse_ready(&line) {
                        sent = tx.send(ready).is_ok();
                    }
                }
                // Keep reading to the end so the engine never blocks on a full pipe.
            }
        });

        let deadline = Instant::now() + timeout;
        let ready = loop {
            match rx.recv_timeout(Duration::from_millis(250)) {
                Ok(v) => break v,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Ok(Some(status)) = child.try_wait() {
                        return Err(format!(
                            "the engine stopped ({status}); see {}",
                            log_path.display()
                        ));
                    }
                    if Instant::now() > deadline {
                        let _ = child.kill();
                        return Err(format!(
                            "the engine didn't start in time; see {}",
                            log_path.display()
                        ));
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    let _ = child.wait();
                    return Err(format!(
                        "the engine stopped before it was ready; see {}",
                        log_path.display()
                    ));
                }
            }
        };

        let port = ready["port"].as_u64().unwrap_or(0) as u16;
        let url = ready["url"].as_str().unwrap_or_default().to_string();
        let attached = ready["attached"].as_bool().unwrap_or(false);
        let mut sidecar = Sidecar {
            child,
            stdin,
            url,
            port,
            attached,
        };
        // An attached daemon is whatever the user started: only ours must match this build.
        if !attached {
            let healthy = http::get_json(port, "/healthz").and_then(|h| match expect {
                Some(m) => check_health(&h, m),
                None if h["ok"] == true => Ok(()),
                None => Err("the engine isn't healthy".to_string()),
            });
            if let Err(e) = healthy {
                sidecar.stop();
                return Err(e);
            }
        }
        Ok(sidecar)
    }

    /// Ask it to stop by closing its stdin, give it a few seconds, then end it.
    pub fn stop(&mut self) {
        if self.attached {
            return; // not ours; `child` is the short-lived client that found it
        }
        self.stdin.take();
        let deadline = Instant::now() + Duration::from_secs(8);
        while Instant::now() < deadline {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn manifest() -> Manifest {
        Manifest {
            engine_version: "0.1.0".into(),
            api_version: 1,
            tarball_sha256: "00000000".into(),
            uv_version: "0".into(),
        }
    }

    #[test]
    fn ready_line_is_parsed() {
        let line =
            r#"ZEPHVOX_READY {"url": "http://127.0.0.1:5123", "port": 5123, "api_version": 1}"#;
        assert_eq!(parse_ready(line).unwrap()["port"], 5123);
        assert!(parse_ready("some log line").is_none());
        assert!(parse_ready("ZEPHVOX_READY {\"port\": 1}").is_none());
    }

    #[test]
    fn health_must_match_this_build() {
        let ok = json!({"ok": true, "engine_version": "0.1.0", "api_version": 1});
        assert!(check_health(&ok, &manifest()).is_ok());
        let old = json!({"ok": true, "engine_version": "0.0.9", "api_version": 1});
        assert!(check_health(&old, &manifest()).is_err());
        let api = json!({"ok": true, "engine_version": "0.1.0", "api_version": 2});
        assert!(check_health(&api, &manifest()).is_err());
    }
}
