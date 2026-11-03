//! Running child programs without a console window, streaming their output to the screen.

use std::io::{BufRead, BufReader, Read};
use std::process::{Command, ExitStatus, Stdio};
use std::thread;

use tauri::AppHandle;

use crate::events;

/// No console window flashing up on Windows, and nothing waiting on stdin.
pub fn quiet(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    #[cfg(not(windows))]
    let _ = cmd;
}

/// Run to the end; every line of stdout/stderr goes to the screen (or is read as a progress event).
pub fn run_streaming(app: &AppHandle, mut cmd: Command) -> std::io::Result<ExitStatus> {
    quiet(&mut cmd);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn()?;
    let out = child.stdout.take().expect("piped");
    let err = child.stderr.take().expect("piped");
    let (a, b) = (app.clone(), app.clone());
    let t1 = thread::spawn(move || pump(&a, out));
    let t2 = thread::spawn(move || pump(&b, err));
    let status = child.wait()?;
    let _ = t1.join();
    let _ = t2.join();
    Ok(status)
}

fn pump<R: Read>(app: &AppHandle, reader: R) {
    let mut reader = BufReader::new(reader);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        match reader.read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => break,
            Ok(_) => events::forward_line(app, &String::from_utf8_lossy(&buf)),
        }
    }
}
