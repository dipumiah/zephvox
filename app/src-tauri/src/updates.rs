//! Updating the app from the latest GitHub release.
//!
//! The release's installer carries the matching engine inside it, so there is one thing to update.
//! On the next start the new app notices its engine differs from the installed one and builds it
//! (see boot.rs), keeping the old engine until the new one has started.
//!
//! The update waits until you aren't recording or analyzing, asks first, then shows progress on the
//! start-up screen.

use std::thread;
use std::time::Duration;

use serde_json::json;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;

use crate::{boot, events, http, AppState};

const CHECK_EVERY: Duration = Duration::from_secs(24 * 3600);
const FIRST_CHECK_AFTER: Duration = Duration::from_secs(30);

/// Check shortly after start-up, then daily.
pub fn spawn_checker(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        sleep_async(FIRST_CHECK_AFTER).await;
        loop {
            check(&app, false).await;
            sleep_async(CHECK_EVERY).await;
        }
    });
}

async fn sleep_async(d: Duration) {
    let _ = tauri::async_runtime::spawn_blocking(move || thread::sleep(d)).await;
}

/// Is the engine recording or analyzing right now?
fn engine_busy(app: &AppHandle) -> bool {
    let port = app
        .state::<AppState>()
        .sidecar
        .lock()
        .unwrap()
        .as_ref()
        .map(|s| s.port);
    let Some(port) = port else { return false };
    match http::get_json(port, "/api/status") {
        Ok(status) => status["busy"] == true || !status["recording"].is_null(),
        Err(_) => false,
    }
}

/// `manual`: asked for from the tray, so say so when there is nothing new.
pub async fn check(app: &AppHandle, manual: bool) {
    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => return notify(app, manual, format!("Can't check for updates: {e}")),
    };
    let update = match updater.check().await {
        Ok(Some(update)) => update,
        Ok(None) => return notify(app, manual, "zephvox is up to date.".into()),
        Err(e) => return notify(app, manual, format!("Can't check for updates: {e}")),
    };

    // Never interrupt a recording or an analysis: wait for a quiet moment.
    while engine_busy(app) {
        sleep_async(Duration::from_secs(20)).await;
    }

    let (tx, rx) = std::sync::mpsc::channel::<bool>();
    app.dialog()
        .message(format!(
            "zephvox {} is available (you have {}).\n\nInstall it now? zephvox will restart.",
            update.version, update.current_version
        ))
        .title("Update available")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Update and restart".into(),
            "Later".into(),
        ))
        .show(move |yes| {
            let _ = tx.send(yes);
        });
    let agreed = tauri::async_runtime::spawn_blocking(move || rx.recv().unwrap_or(false))
        .await
        .unwrap_or(false);
    if !agreed {
        return;
    }

    events::reset(app);
    boot::show_boot_screen(app);
    events::step(app, &format!("Downloading zephvox {}", update.version));
    let progress_app = app.clone();
    let mut downloaded: u64 = 0;
    let result = update
        .download_and_install(
            move |chunk, total| {
                downloaded += chunk as u64;
                events::emit(
                    &progress_app,
                    json!({"type": "download", "id": "app", "name": "zephvox", "label": "zephvox", "state": "downloading",
                           "done": downloaded, "total": total, "error": null}),
                );
            },
            || {},
        )
        .await;
    match result {
        Ok(()) => {
            events::step(app, "Restarting");
            stop_engine(app);
            app.restart();
        }
        Err(e) => {
            events::error(app, &format!("The update failed: {e}"));
        }
    }
}

fn notify(app: &AppHandle, manual: bool, text: String) {
    if manual {
        app.dialog().message(text).title("zephvox").show(|_| {});
    }
}

pub fn stop_engine(app: &AppHandle) {
    let taken = app.state::<AppState>().sidecar.lock().unwrap().take();
    if let Some(mut sidecar) = taken {
        sidecar.stop();
    }
}
