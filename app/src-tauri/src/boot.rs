//! Start-up: make sure the engine for this build is installed (building it on the first run or after
//! an update), start it, and show its page. An update that doesn't start is rolled back to the
//! previous engine.

use std::thread;
use std::time::Duration;

use serde_json::json;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;

use crate::paths::{self, Dirs};
use crate::provision;
use crate::sidecar::Sidecar;
use crate::{events, AppState};

const START_TIMEOUT: Duration = Duration::from_secs(120);

/// Start (or restart) the boot sequence in the background; ignored while one is already running.
pub fn spawn(app: AppHandle) {
    {
        let state = app.state::<AppState>();
        let mut booting = state.booting.lock().unwrap();
        if *booting {
            return;
        }
        *booting = true;
    }
    thread::spawn(move || {
        events::reset(&app);
        match run(&app) {
            Ok(url) => {
                events::done(&app);
                show_page(&app, &url);
            }
            Err(message) => events::error(&app, &message),
        }
        *app.state::<AppState>().booting.lock().unwrap() = false;
    });
}

fn show_page(app: &AppHandle, url: &str) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval(format!("window.location.replace({})", json!(url)));
        let _ = window.show();
    }
}

/// Go back to the start-up screen (used by updates).
pub fn show_boot_screen(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let target = if cfg!(windows) {
            "http://tauri.localhost/index.html"
        } else {
            "tauri://localhost/index.html"
        };
        let _ = window.eval(format!("window.location.replace({})", json!(target)));
        let _ = window.show();
    }
}

fn install(app: &AppHandle, sidecar: Sidecar) -> String {
    let url = sidecar.url.clone();
    *app.state::<AppState>().sidecar.lock().unwrap() = Some(sidecar);
    url
}

fn run(app: &AppHandle) -> Result<String, String> {
    let dirs = Dirs::new(app)?;
    let manifest = provision::load_manifest(app)?;
    let want = manifest.dir_name();
    let current = paths::read_name(&dirs.current_file());
    let failed = paths::read_name(&dirs.failed_file());

    // Already built for this version: just start it.
    if current.as_deref() == Some(&want) && paths::provisioned(&dirs.version(&want)) {
        events::step(app, "Starting zephvox");
        let sidecar = Sidecar::start(&dirs, Some(&manifest), &dirs.version(&want), START_TIMEOUT)?;
        return Ok(install(app, sidecar));
    }

    // This version already failed here: keep using the one that works rather than retrying forever.
    if failed.as_deref() == Some(&want) {
        if let Some(prev) = current
            .as_deref()
            .filter(|p| paths::provisioned(&dirs.version(p)))
        {
            events::step(app, "Starting zephvox");
            let sidecar = Sidecar::start(&dirs, None, &dirs.version(prev), START_TIMEOUT)?;
            return Ok(install(app, sidecar));
        }
    }

    let target = provision::provision(app, &dirs, &manifest, current.as_deref())?;
    events::step(app, "Starting zephvox");
    match Sidecar::start(&dirs, Some(&manifest), &target, START_TIMEOUT) {
        Ok(sidecar) => {
            paths::write_name(&dirs.current_file(), &want)?;
            let _ = std::fs::remove_file(dirs.failed_file());
            let keep: Vec<&str> = std::iter::once(want.as_str())
                .chain(current.as_deref())
                .collect();
            provision::prune(&dirs, &keep);
            Ok(install(app, sidecar))
        }
        Err(why) => rollback(app, &dirs, &want, current.as_deref(), why),
    }
}

/// The new engine didn't start. Remember that, and run the previous one if there is one.
fn rollback(
    app: &AppHandle,
    dirs: &Dirs,
    want: &str,
    previous: Option<&str>,
    why: String,
) -> Result<String, String> {
    let _ = paths::write_name(&dirs.failed_file(), want);
    let Some(prev) = previous.filter(|p| paths::provisioned(&dirs.version(p))) else {
        return Err(format!("zephvox couldn't start: {why}"));
    };
    events::log(
        app,
        &format!("The new engine failed ({why}); going back to the previous one."),
    );
    let sidecar = Sidecar::start(dirs, None, &dirs.version(prev), START_TIMEOUT).map_err(|e| {
        format!("zephvox couldn't start the new engine ({why}) or the previous one ({e})")
    })?;
    app.dialog()
        .message(format!("The update didn't start on this computer, so zephvox is running the previous version.\n\n{why}"))
        .title("zephvox")
        .show(|_| {});
    Ok(install(app, sidecar))
}
