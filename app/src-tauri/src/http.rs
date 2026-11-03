//! A minimal HTTP GET to the local engine (no HTTP client library for two health checks).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use serde_json::Value;

pub fn get_json(port: u16, path: &str) -> Result<Value, String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream =
        TcpStream::connect_timeout(&addr, Duration::from_secs(2)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    let request = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nAccept: application/json\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    parse_response(&raw)
}

pub fn parse_response(raw: &[u8]) -> Result<Value, String> {
    let text = String::from_utf8_lossy(raw);
    let (head, body) = text.split_once("\r\n\r\n").ok_or("incomplete response")?;
    let status = head.lines().next().unwrap_or_default();
    if !status.contains(" 200") {
        return Err(format!("unexpected response: {status}"));
    }
    serde_json::from_str(body.trim()).map_err(|e| format!("bad JSON from the engine: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_json_body() {
        let raw = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"ok\":true,\"api_version\":1}";
        assert_eq!(parse_response(raw).unwrap()["api_version"], 1);
    }

    #[test]
    fn rejects_errors_and_garbage() {
        assert!(parse_response(b"HTTP/1.1 500 Internal Server Error\r\n\r\n{}").is_err());
        assert!(parse_response(b"nonsense").is_err());
        assert!(parse_response(b"HTTP/1.1 200 OK\r\n\r\nnot json").is_err());
    }
}
