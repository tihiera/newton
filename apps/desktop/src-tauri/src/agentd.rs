//! Where agentd listens and which bearer token it expects.
//!
//! Mirrors `services/agentd/newton_agentd/config.py` (keep the two in sync):
//! - data dir: `$NEWTON_DATA_DIR` (with `~` expanded), else
//!   `~/Library/Application Support/Newton` on macOS
//!   (`$XDG_DATA_HOME/newton` or `~/.local/share/newton` elsewhere);
//! - port: `$NEWTON_PORT`, else 8765; agentd binds 127.0.0.1 only;
//! - token: `$NEWTON_API_TOKEN` if set, else `<data_dir>/api-token` (written 0600 by
//!   agentd on its first start).
//!
//! The token is handed to the webview and never logged: `Connection`'s Debug redacts it.

use std::fmt;
use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Serialize;

pub const DEFAULT_PORT: u16 = 8765;
pub const TOKEN_FILE: &str = "api-token";

#[derive(Serialize, Clone, PartialEq, Eq)]
pub struct Connection {
    pub base_url: String,
    pub token: String,
    /// Shown in the UI so the user can tell which agentd data dir is in use.
    pub data_dir: String,
}

impl fmt::Debug for Connection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Connection")
            .field("base_url", &self.base_url)
            .field("token", &"<redacted>")
            .field("data_dir", &self.data_dir)
            .finish()
    }
}

/// Serialized to the webview as `{ code, message }` (plus `log_path` for engine_failed).
#[derive(Serialize, Debug, Clone, PartialEq, Eq)]
pub struct ConnectionError {
    /// `token_missing` | `token_unreadable` | `config` | `engine_starting` |
    /// `engine_failed` (the last two from the engine the shell runs: see engine.rs)
    pub code: &'static str,
    pub message: String,
    /// engine_failed: agentd's log file, for the user to open.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub log_path: Option<String>,
}

impl ConnectionError {
    pub fn new(code: &'static str, message: String) -> Self {
        Self {
            code,
            message,
            log_path: None,
        }
    }
}

/// Environment and platform inputs, injectable for tests.
pub struct Env<'a> {
    pub var: &'a dyn Fn(&str) -> Option<String>,
    pub home: Option<PathBuf>,
    pub macos: bool,
}

impl Env<'_> {
    /// A variable that is set and non-empty (config.py treats "" as unset too).
    pub fn get(&self, name: &str) -> Option<String> {
        (self.var)(name).filter(|v| !v.is_empty())
    }

    fn home(&self) -> Result<&Path, ConnectionError> {
        self.home.as_deref().ok_or_else(|| {
            ConnectionError::new("config", "cannot resolve the home directory ($HOME)".into())
        })
    }
}

pub fn expand_user(value: &str, env: &Env) -> Result<PathBuf, ConnectionError> {
    if value == "~" {
        return Ok(env.home()?.to_path_buf());
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return Ok(env.home()?.join(rest));
    }
    Ok(PathBuf::from(value))
}

pub fn data_dir(env: &Env) -> Result<PathBuf, ConnectionError> {
    if let Some(value) = env.get("NEWTON_DATA_DIR") {
        return expand_user(&value, env);
    }
    if env.macos {
        return Ok(env
            .home()?
            .join("Library")
            .join("Application Support")
            .join("Newton"));
    }
    let base = match env.get("XDG_DATA_HOME") {
        Some(xdg) => PathBuf::from(xdg),
        None => env.home()?.join(".local").join("share"),
    };
    Ok(base.join("newton"))
}

pub fn port(env: &Env) -> Result<u16, ConnectionError> {
    match env.get("NEWTON_PORT") {
        None => Ok(DEFAULT_PORT),
        Some(raw) => raw
            .trim()
            .parse::<u16>()
            .ok()
            .filter(|p| *p != 0)
            .ok_or_else(|| {
                ConnectionError::new(
                    "config",
                    format!("NEWTON_PORT is not a valid port: {raw:?}"),
                )
            }),
    }
}

pub fn read_token(path: &Path) -> Result<String, ConnectionError> {
    match std::fs::read_to_string(path) {
        Ok(text) => {
            let token = text.trim();
            if token.is_empty() {
                Err(ConnectionError::new(
                    "token_missing",
                    format!("agentd token file is empty: {}", path.display()),
                ))
            } else {
                Ok(token.to_string())
            }
        }
        Err(e) if e.kind() == ErrorKind::NotFound => Err(ConnectionError::new(
            "token_missing",
            format!(
                "agentd not started yet: no token file at {}",
                path.display()
            ),
        )),
        // The io::Error never contains the file's contents.
        Err(e) => Err(ConnectionError::new(
            "token_unreadable",
            format!("cannot read agentd token file {}: {e}", path.display()),
        )),
    }
}

pub fn resolve(env: &Env) -> Result<Connection, ConnectionError> {
    let dir = data_dir(env)?;
    let port = port(env)?;
    let token = match env.get("NEWTON_API_TOKEN") {
        Some(token) => token,
        None => read_token(&dir.join(TOKEN_FILE))?,
    };
    Ok(Connection {
        base_url: format!("http://127.0.0.1:{port}"),
        token,
        data_dir: dir.display().to_string(),
    })
}

/// Resolve from this process's real environment.
pub fn resolve_from_process() -> Result<Connection, ConnectionError> {
    with_process_env(resolve)
}

/// Runs `f` with this process's real environment.
pub fn with_process_env<T>(f: impl FnOnce(&Env) -> T) -> T {
    let var = |name: &str| std::env::var(name).ok();
    f(&Env {
        var: &var,
        home: std::env::var_os("HOME").map(PathBuf::from),
        macos: cfg!(target_os = "macos"),
    })
}

/// Who answers `GET /health` on 127.0.0.1:<port>.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Probe {
    /// Nothing listens there.
    Free,
    /// An agentd; `db_path` (from its health) tells which data dir it uses.
    Agentd { db_path: Option<String> },
    /// Something else holds the port: it sent a whole answer, and not agentd's.
    Foreign,
    /// Something accepted the connection but sent no whole answer in time: a busy
    /// agentd (its event loop held up by sync work) or a program that never answers.
    /// Not conclusive on its own: the engine probes again (see engine.rs `PortCheck`).
    Busy,
}

/// One `GET /health` with short timeouts (loopback only, no token needed).
pub fn probe_health(port: u16, timeout: Duration) -> Probe {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, timeout) else {
        return Probe::Free;
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let request =
        format!("GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return Probe::Busy;
    }
    let mut raw = Vec::new();
    // `Connection: close`: a whole answer ends with the peer closing. A read that timed
    // out (or failed) got at most part of one.
    let complete = stream.take(1 << 20).read_to_end(&mut raw).is_ok();
    probe_from(&String::from_utf8_lossy(&raw), complete)
}

/// What a read of `raw` means: a partial answer that isn't (yet) agentd's is `Busy`.
pub fn probe_from(raw: &str, complete: bool) -> Probe {
    match parse_health(raw) {
        Probe::Foreign if !complete => Probe::Busy,
        probe => probe,
    }
}

/// An HTTP response to `GET /health`: agentd's is a 200 with JSON carrying `status` and
/// `db.path`.
pub fn parse_health(raw: &str) -> Probe {
    let Some((head, body)) = raw.split_once("\r\n\r\n") else {
        return Probe::Foreign;
    };
    let ok = head.lines().next().is_some_and(|status| {
        status.starts_with("HTTP/1.1 200") || status.starts_with("HTTP/1.0 200")
    });
    let Ok(json) = serde_json::from_str::<serde_json::Value>(body.trim()) else {
        return Probe::Foreign;
    };
    if !ok || json.get("status").and_then(|s| s.as_str()).is_none() || json.get("db").is_none() {
        return Probe::Foreign;
    }
    Probe::Agentd {
        db_path: json
            .pointer("/db/path")
            .and_then(|p| p.as_str())
            .map(str::to_string),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn temp_dir() -> PathBuf {
        static N: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "newton-desktop-test-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn with_env<T>(vars: &[(&str, &str)], macos: bool, f: impl FnOnce(&Env) -> T) -> T {
        let map: HashMap<String, String> = vars
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        let var = move |name: &str| map.get(name).cloned();
        f(&Env {
            var: &var,
            home: Some(PathBuf::from("/Users/alice")),
            macos,
        })
    }

    #[test]
    fn default_data_dir_on_macos() {
        let dir = with_env(&[], true, |env| data_dir(env).unwrap());
        assert_eq!(
            dir,
            PathBuf::from("/Users/alice/Library/Application Support/Newton")
        );
    }

    #[test]
    fn default_data_dir_elsewhere() {
        let dir = with_env(&[], false, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/Users/alice/.local/share/newton"));
        let dir = with_env(&[("XDG_DATA_HOME", "/x")], false, |env| {
            data_dir(env).unwrap()
        });
        assert_eq!(dir, PathBuf::from("/x/newton"));
    }

    #[test]
    fn data_dir_env_override_expands_tilde_and_ignores_empty() {
        let dir = with_env(&[("NEWTON_DATA_DIR", "~/nd")], true, |env| {
            data_dir(env).unwrap()
        });
        assert_eq!(dir, PathBuf::from("/Users/alice/nd"));
        let dir = with_env(&[("NEWTON_DATA_DIR", "/abs/d")], true, |env| {
            data_dir(env).unwrap()
        });
        assert_eq!(dir, PathBuf::from("/abs/d"));
        let dir = with_env(&[("NEWTON_DATA_DIR", "")], true, |env| {
            data_dir(env).unwrap()
        });
        assert_eq!(
            dir,
            PathBuf::from("/Users/alice/Library/Application Support/Newton")
        );
    }

    #[test]
    fn port_default_override_and_invalid() {
        assert_eq!(with_env(&[], true, |env| port(env).unwrap()), 8765);
        assert_eq!(
            with_env(&[("NEWTON_PORT", "8799")], true, |env| port(env).unwrap()),
            8799
        );
        for bad in ["abc", "0", "70000", "-1"] {
            let err = with_env(&[("NEWTON_PORT", bad)], true, |env| port(env).unwrap_err());
            assert_eq!(err.code, "config", "{bad}");
        }
    }

    #[test]
    fn resolve_reads_trimmed_token_and_builds_loopback_url() {
        let dir = temp_dir();
        std::fs::write(dir.join(TOKEN_FILE), "s3cret-token\n").unwrap();
        let d = dir.display().to_string();
        let conn = with_env(
            &[("NEWTON_DATA_DIR", &d), ("NEWTON_PORT", "8799")],
            true,
            |env| resolve(env).unwrap(),
        );
        assert_eq!(conn.base_url, "http://127.0.0.1:8799");
        assert_eq!(conn.token, "s3cret-token");
        assert_eq!(conn.data_dir, d);
        // The token never shows up in debug output (and so never in a log line).
        assert!(!format!("{conn:?}").contains("s3cret"));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn env_token_wins_like_config_py() {
        let dir = temp_dir();
        let d = dir.display().to_string();
        let conn = with_env(
            &[("NEWTON_DATA_DIR", &d), ("NEWTON_API_TOKEN", "from-env")],
            true,
            |env| resolve(env).unwrap(),
        );
        assert_eq!(conn.token, "from-env");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn parse_health_tells_agentd_from_other_programs() {
        let agentd = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n\
            {\"status\":\"ok\",\"db\":{\"ok\":true,\"path\":\"/d/newton.db\"}}";
        assert_eq!(
            parse_health(agentd),
            Probe::Agentd {
                db_path: Some("/d/newton.db".into())
            }
        );
        let other = "HTTP/1.1 200 OK\r\n\r\n<html>hi</html>";
        assert_eq!(parse_health(other), Probe::Foreign);
        let not_found = "HTTP/1.1 404 Not Found\r\n\r\n{\"status\":\"x\",\"db\":{}}";
        assert_eq!(parse_health(not_found), Probe::Foreign);
        assert_eq!(parse_health(""), Probe::Foreign);
    }

    #[test]
    fn a_partial_answer_is_busy_not_foreign() {
        let head = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"status\":";
        assert_eq!(probe_from(head, false), Probe::Busy);
        assert_eq!(probe_from("", false), Probe::Busy);
        assert_eq!(probe_from(head, true), Probe::Foreign, "closed mid-answer");
        let whole = "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"ok\",\"db\":{\"path\":\"/d/n.db\"}}";
        assert!(matches!(probe_from(whole, false), Probe::Agentd { .. }));
    }

    /// A listener that accepts and never answers (a busy agentd): Busy, not Foreign.
    #[test]
    fn probe_of_a_silent_listener_is_busy() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        // Accepted by the kernel's backlog; nobody reads or writes.
        assert_eq!(probe_health(port, Duration::from_millis(200)), Probe::Busy);
        drop(listener);
    }

    /// A program that answers something else in full: Foreign.
    #[test]
    fn probe_of_another_http_server_is_foreign() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 512];
            let _ = s.read(&mut buf);
            let _ = s.write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n<html>hi</html>");
        });
        assert_eq!(probe_health(port, Duration::from_secs(2)), Probe::Foreign);
        server.join().unwrap();
    }

    #[test]
    fn probe_of_a_closed_port_is_free() {
        // Bind then drop: the port is (almost certainly) closed right after.
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        assert_eq!(probe_health(port, Duration::from_millis(300)), Probe::Free);
    }

    #[test]
    fn engine_failed_serializes_its_log_path_and_others_omit_it() {
        let mut err = ConnectionError::new("engine_failed", "stopped".into());
        assert_eq!(
            serde_json::to_string(&err).unwrap(),
            r#"{"code":"engine_failed","message":"stopped"}"#
        );
        err.log_path = Some("/d/logs/agentd.log".into());
        assert!(serde_json::to_string(&err)
            .unwrap()
            .ends_with(r#""log_path":"/d/logs/agentd.log"}"#));
    }

    #[test]
    fn missing_or_empty_token_file_is_token_missing() {
        let dir = temp_dir();
        let d = dir.display().to_string();
        let err = with_env(&[("NEWTON_DATA_DIR", &d)], true, |env| {
            resolve(env).unwrap_err()
        });
        assert_eq!(err.code, "token_missing");
        assert!(err
            .message
            .starts_with("agentd not started yet: no token file at "));
        assert!(err.message.contains(&d));

        std::fs::write(dir.join(TOKEN_FILE), "  \n").unwrap();
        let err = with_env(&[("NEWTON_DATA_DIR", &d)], true, |env| {
            resolve(env).unwrap_err()
        });
        assert_eq!(err.code, "token_missing");
        std::fs::remove_dir_all(dir).unwrap();
    }

    /// Against a running agentd: resolve the connection from the real environment
    /// (NEWTON_DATA_DIR / NEWTON_PORT), then GET /health and an authenticated /hosts.
    /// Run: `NEWTON_DATA_DIR=… NEWTON_PORT=… cargo test -- --ignored live_agentd`
    #[test]
    #[ignore]
    fn live_agentd() {
        use std::io::{Read, Write};
        let conn = resolve_from_process().expect("resolve connection");
        let addr = conn.base_url.trim_start_matches("http://").to_string();
        let get = |path: &str, auth: Option<&str>| -> String {
            let mut s = std::net::TcpStream::connect(&addr).expect("connect to agentd");
            let auth = auth
                .map(|t| format!("Authorization: Bearer {t}\r\n"))
                .unwrap_or_default();
            write!(
                s,
                "GET {path} HTTP/1.1\r\nHost: {addr}\r\n{auth}Connection: close\r\n\r\n"
            )
            .unwrap();
            let mut out = String::new();
            s.read_to_string(&mut out).unwrap();
            out
        };
        let health = get("/health", None);
        assert!(health.starts_with("HTTP/1.1 200"), "{health}");
        assert!(health.contains("\"status\":\"ok\""), "{health}");
        let hosts = get("/hosts", Some(&conn.token));
        assert!(hosts.starts_with("HTTP/1.1 200"), "{hosts}");
        assert!(hosts.contains("\"kind\":\"local\""), "{hosts}");
        let denied = get("/hosts", Some("wrong-token"));
        assert!(denied.starts_with("HTTP/1.1 401"), "{denied}");
    }
}
