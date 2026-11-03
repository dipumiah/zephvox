//! zephvox desktop app: a window around the engine's page, a tray icon, updates, and the Python
//! engine it builds and supervises. See app/README.md.

mod boot;
mod events;
mod http;
mod paths;
mod proc;
mod provision;
mod sidecar;
mod tray;
mod updates;

use std::sync::Mutex;

use serde_json::Value;
use tauri::{AppHandle, State, WindowEvent};

#[derive(Default)]
pub struct AppState {
    pub boot: Mutex<events::BootState>,
    pub sidecar: Mutex<Option<sidecar::Sidecar>>,
    pub booting: Mutex<bool>,
}

/// What the start-up screen shows so far (it may open after things have already happened).
#[tauri::command]
fn boot_snapshot(state: State<AppState>) -> Value {
    state.boot.lock().unwrap().snapshot()
}

/// "Try again" on the start-up screen.
#[tauri::command]
fn boot_retry(app: AppHandle) {
    boot::spawn(app);
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            tray::show_main(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![boot_snapshot, boot_retry])
        .setup(|app| {
            tray::create(app.handle())?;
            boot::spawn(app.handle().clone());
            updates::spawn_checker(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides it: the engine keeps running for the shortcuts. Quit is in the tray.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the zephvox app");

    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            updates::stop_engine(handle);
        }
    });
}
