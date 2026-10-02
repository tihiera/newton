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
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

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

/// Serialized to the webview as `{ code, message }`.
#[derive(Serialize, Debug, Clone, PartialEq, Eq)]
pub struct ConnectionError {
    /// `token_missing` | `token_unreadable` | `config`
    pub code: &'static str,
    pub message: String,
}

impl ConnectionError {
    fn new(code: &'static str, message: String) -> Self {
        Self { code, message }
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
    fn get(&self, name: &str) -> Option<String> {
        (self.var)(name).filter(|v| !v.is_empty())
    }

    fn home(&self) -> Result<&Path, ConnectionError> {
        self.home.as_deref().ok_or_else(|| {
            ConnectionError::new("config", "cannot resolve the home directory ($HOME)".into())
        })
    }
}

fn expand_user(value: &str, env: &Env) -> Result<PathBuf, ConnectionError> {
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
        return Ok(env.home()?.join("Library").join("Application Support").join("Newton"));
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
        Some(raw) => raw.trim().parse::<u16>().ok().filter(|p| *p != 0).ok_or_else(|| {
            ConnectionError::new("config", format!("NEWTON_PORT is not a valid port: {raw:?}"))
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
            format!("agentd not started yet: no token file at {}", path.display()),
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
    let var = |name: &str| std::env::var(name).ok();
    resolve(&Env {
        var: &var,
        home: std::env::var_os("HOME").map(PathBuf::from),
        macos: cfg!(target_os = "macos"),
    })
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
        let map: HashMap<String, String> =
            vars.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        let var = move |name: &str| map.get(name).cloned();
        f(&Env { var: &var, home: Some(PathBuf::from("/Users/alice")), macos })
    }

    #[test]
    fn default_data_dir_on_macos() {
        let dir = with_env(&[], true, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/Users/alice/Library/Application Support/Newton"));
    }

    #[test]
    fn default_data_dir_elsewhere() {
        let dir = with_env(&[], false, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/Users/alice/.local/share/newton"));
        let dir = with_env(&[("XDG_DATA_HOME", "/x")], false, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/x/newton"));
    }

    #[test]
    fn data_dir_env_override_expands_tilde_and_ignores_empty() {
        let dir = with_env(&[("NEWTON_DATA_DIR", "~/nd")], true, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/Users/alice/nd"));
        let dir = with_env(&[("NEWTON_DATA_DIR", "/abs/d")], true, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/abs/d"));
        let dir = with_env(&[("NEWTON_DATA_DIR", "")], true, |env| data_dir(env).unwrap());
        assert_eq!(dir, PathBuf::from("/Users/alice/Library/Application Support/Newton"));
    }

    #[test]
    fn port_default_override_and_invalid() {
        assert_eq!(with_env(&[], true, |env| port(env).unwrap()), 8765);
        assert_eq!(with_env(&[("NEWTON_PORT", "8799")], true, |env| port(env).unwrap()), 8799);
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
        let conn = with_env(&[("NEWTON_DATA_DIR", &d), ("NEWTON_PORT", "8799")], true, |env| {
            resolve(env).unwrap()
        });
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
        let conn = with_env(&[("NEWTON_DATA_DIR", &d), ("NEWTON_API_TOKEN", "from-env")], true, |env| {
            resolve(env).unwrap()
        });
        assert_eq!(conn.token, "from-env");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn missing_or_empty_token_file_is_token_missing() {
        let dir = temp_dir();
        let d = dir.display().to_string();
        let err = with_env(&[("NEWTON_DATA_DIR", &d)], true, |env| resolve(env).unwrap_err());
        assert_eq!(err.code, "token_missing");
        assert!(err.message.starts_with("agentd not started yet: no token file at "));
        assert!(err.message.contains(&d));

        std::fs::write(dir.join(TOKEN_FILE), "  \n").unwrap();
        let err = with_env(&[("NEWTON_DATA_DIR", &d)], true, |env| resolve(env).unwrap_err());
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
            let auth = auth.map(|t| format!("Authorization: Bearer {t}\r\n")).unwrap_or_default();
            write!(s, "GET {path} HTTP/1.1\r\nHost: {addr}\r\n{auth}Connection: close\r\n\r\n").unwrap();
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
