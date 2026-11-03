//! What the start-up / update screen shows, and the log of it.
//!
//! Python reports progress as `ZEPHVOX_EVENT {json}` lines (see src/zephvox/install/events.py); the
//! shell adds its own steps. Everything lands here, is kept (so a screen opened late can catch up) and
//! is sent to the page as a `boot` event.

use std::collections::{BTreeMap, VecDeque};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};

use crate::AppState;

const LOG_LINES: usize = 200;

#[derive(Default)]
pub struct BootState {
    pub step: Option<Value>,
    pub items: BTreeMap<String, Value>,
    pub logs: VecDeque<String>,
    pub error: Option<String>,
    pub done: bool,
}

impl BootState {
    pub fn snapshot(&self) -> Value {
        json!({
            "step": self.step,
            "items": self.items.values().collect::<Vec<_>>(),
            "logs": self.logs,
            "error": self.error,
            "done": self.done,
        })
    }

    /// Fold one event in; returns false for types the screen doesn't use.
    pub fn apply(&mut self, event: &Value) -> bool {
        match event.get("type").and_then(Value::as_str) {
            Some("step") => self.step = Some(event.clone()),
            Some("download") => {
                if let Some(id) = event.get("id").and_then(Value::as_str) {
                    self.items.insert(id.to_string(), event.clone());
                }
            }
            Some("log") => {
                if let Some(text) = event.get("text").and_then(Value::as_str) {
                    self.push_log(text.to_string());
                }
            }
            Some("error") => {
                self.error = event
                    .get("message")
                    .and_then(Value::as_str)
                    .map(str::to_string);
            }
            _ => return false,
        }
        true
    }

    pub fn push_log(&mut self, line: String) {
        if self.logs.len() == LOG_LINES {
            self.logs.pop_front();
        }
        self.logs.push_back(line);
    }

    /// The last few log lines, for an error message.
    pub fn tail(&self, n: usize) -> String {
        let skip = self.logs.len().saturating_sub(n);
        self.logs
            .iter()
            .skip(skip)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    }
}

pub fn emit(app: &AppHandle, event: Value) {
    let state = app.state::<AppState>();
    let known = state.boot.lock().unwrap().apply(&event);
    if known {
        let _ = app.emit("boot", event);
    }
}

pub fn step(app: &AppHandle, title: &str) {
    emit(
        app,
        json!({ "type": "step", "n": null, "total": null, "title": title }),
    );
}

pub fn log(app: &AppHandle, text: &str) {
    emit(app, json!({ "type": "log", "text": text }));
}

pub fn error(app: &AppHandle, message: &str) {
    emit(app, json!({ "type": "error", "message": message }));
}

/// The last few log lines, to put in an error message.
pub fn tail(app: &AppHandle, n: usize) -> String {
    app.state::<AppState>().boot.lock().unwrap().tail(n)
}

pub fn reset(app: &AppHandle) {
    let state = app.state::<AppState>();
    *state.boot.lock().unwrap() = BootState::default();
    let _ = app.emit("boot", json!({ "type": "reset" }));
}

pub fn done(app: &AppHandle) {
    let state = app.state::<AppState>();
    state.boot.lock().unwrap().done = true;
    let _ = app.emit("boot", json!({ "type": "done" }));
}

/// Handle one line of a child's output: an event, or ordinary text for the log.
pub fn forward_line(app: &AppHandle, line: &str) {
    let line = line.trim_end();
    if line.is_empty() {
        return;
    }
    if let Some(rest) = line.strip_prefix("ZEPHVOX_EVENT ") {
        if let Ok(event) = serde_json::from_str::<Value>(rest) {
            emit(app, event);
            return;
        }
    }
    log(app, line);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn steps_items_and_errors_are_kept() {
        let mut s = BootState::default();
        assert!(s.apply(&json!({"type": "step", "title": "Installing"})));
        assert!(s.apply(&json!({"type": "download", "id": "a", "state": "queued"})));
        assert!(s.apply(&json!({"type": "download", "id": "a", "state": "done"})));
        assert!(s.apply(&json!({"type": "error", "message": "boom"})));
        assert!(!s.apply(&json!({"type": "mystery"})));
        let snap = s.snapshot();
        assert_eq!(snap["items"].as_array().unwrap().len(), 1);
        assert_eq!(snap["items"][0]["state"], "done");
        assert_eq!(snap["error"], "boom");
    }

    #[test]
    fn log_keeps_only_the_latest_lines() {
        let mut s = BootState::default();
        for i in 0..(LOG_LINES + 5) {
            s.push_log(format!("line {i}"));
        }
        assert_eq!(s.logs.len(), LOG_LINES);
        assert_eq!(s.logs.front().unwrap(), "line 5");
        assert!(s.tail(2).ends_with(&format!("line {}", LOG_LINES + 4)));
    }
}
