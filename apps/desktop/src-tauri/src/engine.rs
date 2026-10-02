//! Newton's engine: the agentd the desktop shell starts and stops itself.
//!
//! Newton runs from its git clone. The shell finds the clone (`NEWTON_REPO`, else three
//! levels above this crate, `<repo>/apps/desktop/src-tauri`, recognised by
//! `services/agentd/pyproject.toml`) and uv (PATH, then where uv's installers put it: an
//! app opened outside a terminal gets a minimal PATH), and runs, in the clone,
//! `uv run --project <repo> --frozen newton-agentd serve --port P --data-dir D
//! --exit-on-stdin-eof`. Without a clone, uv or the clone's Python environment the
//! engine fails with a sentence that says what to install or run (scripts/setup.sh).
//!
//! Start: take `<data_dir>/agentd.lock` (one managing shell per data dir), ask
//! `GET /health` on the port; reuse a healthy agentd that uses the same data dir (one
//! started by scripts/run.sh or scripts/dev.sh), refuse a port held by anything else
//! (only after several probes agree: a busy agentd may answer late), otherwise spawn agentd with stdout and stderr in `<data_dir>/logs/agentd.log` (the
//! previous run kept as `agentd.log.1`) and stdin piped: agentd exits when that pipe
//! closes, so it never outlives the shell, even one that crashed. A watcher thread polls
//! /health until agentd answers and then notices if it exits. Every failure is also
//! written at the end of that log, the one the window points to. Quit: close stdin,
//! SIGTERM, up to 12 s, then SIGKILL. An agentd the shell didn't start keeps running.

use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use crate::agentd::{self, ConnectionError, Env, Probe};

pub const LOCK_FILE: &str = "agentd.lock";
pub const LOG_DIR: &str = "logs";
pub const LOG_FILE: &str = "agentd.log";
/// The first start after a setup imports numpy, mlx and migrates the database.
pub const START_TIMEOUT: Duration = Duration::from_secs(90);
pub const STOP_GRACE: Duration = Duration::from_secs(12);
const PROBE_TIMEOUT: Duration = Duration::from_millis(800);
const START_POLL: Duration = Duration::from_millis(250);
const RUN_POLL: Duration = Duration::from_secs(2);

/// Overrides where Newton's clone is (by default: where this shell was built from).
pub const REPO_ENV: &str = "NEWTON_REPO";
/// What makes a folder Newton's clone.
pub const REPO_MARKER: &str = "services/agentd/pyproject.toml";
/// Where uv's installers put it, tried after PATH (`~/` is the home directory).
pub const UV_FALLBACKS: &[&str] = &[
    "~/.local/bin/uv",
    "~/.cargo/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
];

/// Variables that would make agentd load code from elsewhere or behave as the retired
/// packaged app.
pub const ENV_REMOVE: &[&str] = &[
    "NEWTON_PACKAGED",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
];

/// Where Homebrew and other installers put tools agentd may look for (ollama, gh);
/// an app opened from the Finder only gets /usr/bin:/bin:/usr/sbin:/sbin.
const EXTRA_PATH: &[&str] = &["/opt/homebrew/bin", "/usr/local/bin"];
const BASE_PATH: &[&str] = &["/usr/bin", "/bin", "/usr/sbin", "/sbin"];

/// Newton's clone: `override_dir` (NEWTON_REPO) if given, else three levels above
/// `manifest_dir` (this crate: `<repo>/apps/desktop/src-tauri`).
pub fn find_repo(override_dir: Option<&Path>, manifest_dir: &Path) -> Result<PathBuf, String> {
    let repo = match override_dir {
        Some(dir) => dir.to_path_buf(),
        None => manifest_dir
            .ancestors()
            .nth(3)
            .map(Path::to_path_buf)
            .unwrap_or_default(),
    };
    if repo.join(REPO_MARKER).is_file() {
        return Ok(repo);
    }
    Err(match override_dir {
        Some(_) => format!(
            "{REPO_ENV} is set to {}, which isn't a Newton clone (it has no \
             {REPO_MARKER}): point it at the folder you cloned Newton into.",
            repo.display()
        ),
        None => format!(
            "Newton's source folder isn't at {} any more (it has no {REPO_MARKER}): run \
             Newton from its clone with scripts/run.sh, or set {REPO_ENV} to that folder.",
            repo.display()
        ),
    })
}

/// uv: the first executable `uv` on PATH (absolute entries only), then `UV_FALLBACKS`.
pub fn find_uv(
    path_var: Option<&str>,
    home: Option<&Path>,
    is_exe: &dyn Fn(&Path) -> bool,
) -> Option<PathBuf> {
    let on_path = path_var
        .unwrap_or("")
        .split(':')
        .map(Path::new)
        .filter(|dir| dir.is_absolute())
        .map(|dir| dir.join("uv"));
    let fallbacks = UV_FALLBACKS
        .iter()
        .filter_map(|p| match p.strip_prefix("~/") {
            Some(rest) => home.map(|h| h.join(rest)),
            None => Some(PathBuf::from(p)),
        });
    on_path.chain(fallbacks).find(|p| is_exe(p))
}

/// A regular file someone may execute.
pub fn is_executable(path: &Path) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        meta.is_file() && meta.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        meta.is_file()
    }
}

/// What the shell can do about the engine, decided once at start and again on restart.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Setup {
    /// The data dir or port don't resolve: `agentd_connection` reports that itself.
    Unmanaged,
    Launch(Launch),
    /// No clone, uv or Python environment: what to install or run, and where the log
    /// would be.
    Missing {
        message: String,
        log_path: PathBuf,
    },
}

/// The engine's setup from the environment: `manifest_dir` is this crate's folder.
pub fn plan(env: &Env, manifest_dir: &Path, is_exe: &dyn Fn(&Path) -> bool) -> Setup {
    let (Ok(data_dir), Ok(port)) = (agentd::data_dir(env), agentd::port(env)) else {
        return Setup::Unmanaged;
    };
    let log_path = data_dir.join(LOG_DIR).join(LOG_FILE);
    let missing = |message: String| Setup::Missing {
        message,
        log_path: log_path.clone(),
    };
    let override_dir = match env.get(REPO_ENV) {
        Some(value) => match agentd::expand_user(&value, env) {
            Ok(dir) => Some(dir),
            Err(e) => return missing(e.message),
        },
        None => None,
    };
    let repo = match find_repo(override_dir.as_deref(), manifest_dir) {
        Ok(repo) => repo,
        Err(message) => return missing(message),
    };
    let Some(uv) = find_uv(env.get("PATH").as_deref(), env.home.as_deref(), is_exe) else {
        return missing(format!(
            "uv isn't installed (Newton looked on PATH and in ~/.local/bin, ~/.cargo/bin, \
             /opt/homebrew/bin and /usr/local/bin): run scripts/setup.sh in {}, then \
             restart the engine.",
            repo.display()
        ));
    };
    // uv would build a missing environment itself, downloading for minutes: setup.sh
    // does that once, with progress, instead.
    if env.get("UV_PROJECT_ENVIRONMENT").is_none() && !repo.join(".venv").is_dir() {
        return missing(format!(
            "Newton's Python environment isn't set up yet: run scripts/setup.sh in {}, \
             then restart the engine.",
            repo.display()
        ));
    }
    Setup::Launch(Launch {
        uv,
        repo,
        data_dir,
        port,
    })
}

/// How to start agentd: the shared contract with `newton_agentd.main`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Launch {
    pub uv: PathBuf,
    /// Newton's clone: agentd's project, its working directory and its resources
    /// (benchmarks/, services/worker/).
    pub repo: PathBuf,
    pub data_dir: PathBuf,
    pub port: u16,
}

impl Launch {
    pub fn argv(&self) -> Vec<OsString> {
        vec![
            self.uv.clone().into(),
            "run".into(),
            "--project".into(),
            self.repo.clone().into(),
            "--frozen".into(),
            "newton-agentd".into(),
            "serve".into(),
            "--port".into(),
            self.port.to_string().into(),
            "--data-dir".into(),
            self.data_dir.clone().into(),
            "--exit-on-stdin-eof".into(),
        ]
    }

    /// Set on top of the user's environment (minus `ENV_REMOVE`).
    pub fn env(&self, path: Option<&str>) -> Vec<(&'static str, OsString)> {
        vec![
            // agentd's default already is the clone it runs from: belt and braces.
            ("NEWTON_RESOURCES_DIR", self.repo.clone().into()),
            ("PATH", child_path(path).into()),
        ]
    }

    pub fn log_path(&self) -> PathBuf {
        self.data_dir.join(LOG_DIR).join(LOG_FILE)
    }

    pub fn lock_path(&self) -> PathBuf {
        self.data_dir.join(LOCK_FILE)
    }
}

/// The shell's PATH, completed with the system directories and then Homebrew's.
pub fn child_path(current: Option<&str>) -> String {
    let mut parts: Vec<&str> = current
        .unwrap_or("")
        .split(':')
        .filter(|p| !p.is_empty())
        .collect();
    for dir in BASE_PATH.iter().chain(EXTRA_PATH) {
        if !parts.contains(dir) {
            parts.push(dir);
        }
    }
    parts.join(":")
}

/// Adds one line to the engine log (a failure before agentd could even start), so the
/// log path the window shows has the reason in it too.
fn note_in_log(log_path: &Path, message: &str) {
    if let Some(dir) = log_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(log, "newton desktop: {message}");
    }
}

/// What the start sequence does after probing the port.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    Reuse,
    Spawn,
    /// Not conclusive yet: probe again.
    Wait,
    Fail(String),
}

/// Probes that must get another program's whole answer before the port counts as
/// taken by it (an agentd being started or stopped may answer oddly once).
pub const FOREIGN_PROBES: u32 = 3;
/// Probes in a row without a whole answer before a port that accepts but stays silent
/// counts as taken (about 20 s: each waits up to `PROBE_TIMEOUT`, then `START_POLL`).
pub const SILENT_PROBES: u32 = 20;

/// Does an agentd's database (`/health` db.path) live in `data_dir`?
pub fn same_data_dir(db_path: &str, data_dir: &Path) -> bool {
    let db = Path::new(db_path);
    if db.starts_with(data_dir) {
        return true;
    }
    match (db.canonicalize(), data_dir.canonicalize()) {
        (Ok(db), Ok(dir)) => db.starts_with(dir),
        _ => false,
    }
}

pub fn decide(probe: &Probe, data_dir: &Path, port: u16) -> Decision {
    match probe {
        Probe::Free => Decision::Spawn,
        Probe::Agentd {
            db_path: Some(db), ..
        } if same_data_dir(db, data_dir) => Decision::Reuse,
        Probe::Agentd { db_path } => Decision::Fail(format!(
            "Another Newton engine already listens on port {port}{}: quit it, or set \
             NEWTON_PORT to a free port.",
            db_path
                .as_deref()
                .and_then(|p| Path::new(p).parent())
                .map(|d| format!(" with its data in {}", d.display()))
                .unwrap_or_default()
        )),
        Probe::Foreign => Decision::Fail(format!(
            "Port {port} is taken by another program: quit it, or set NEWTON_PORT to a \
             free port."
        )),
        Probe::Busy => Decision::Wait,
    }
}

fn silent_port_message(port: u16) -> String {
    format!(
        "Something listens on port {port} but hasn't answered Newton's health check for \
         {SILENT_PROBES} tries: if it's another program, quit it, or set NEWTON_PORT to a \
         free port; if it's a Newton engine that hangs, stop it. Then restart the engine."
    )
}

/// `decide` over successive probes: a silent or odd answer is only conclusive once it
/// repeats (`FOREIGN_PROBES`, `SILENT_PROBES`); until then, `Wait`.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct PortCheck {
    foreign: u32,
    silent: u32,
}

impl PortCheck {
    pub fn decide(&mut self, probe: &Probe, data_dir: &Path, port: u16) -> Decision {
        match probe {
            Probe::Free | Probe::Agentd { .. } => *self = Self::default(),
            Probe::Foreign => {
                self.foreign += 1;
                self.silent += 1;
            }
            Probe::Busy => self.silent += 1,
        }
        match probe {
            Probe::Foreign if self.foreign >= FOREIGN_PROBES => decide(probe, data_dir, port),
            Probe::Foreign | Probe::Busy if self.silent >= SILENT_PROBES => {
                Decision::Fail(silent_port_message(port))
            }
            Probe::Foreign | Probe::Busy => Decision::Wait,
            _ => decide(probe, data_dir, port),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineState {
    /// The data dir or port don't resolve (`agentd_connection` says why).
    Unmanaged,
    Starting,
    Running {
        /// An agentd that was already up (scripts/run.sh, scripts/dev.sh, another Newton).
        reused: bool,
    },
    Failed {
        message: String,
    },
}

impl EngineState {
    /// What `agentd_connection` says instead of connecting (None: connect).
    pub fn connection_error(&self, log_path: &Path) -> Option<ConnectionError> {
        match self {
            EngineState::Unmanaged | EngineState::Running { .. } => None,
            EngineState::Starting => Some(ConnectionError::new(
                "engine_starting",
                "Newton's engine is starting.".into(),
            )),
            EngineState::Failed { message } => {
                let mut err = ConnectionError::new("engine_failed", message.clone());
                err.log_path = Some(log_path.display().to_string());
                Some(err)
            }
        }
    }
}

/// One tick of the watcher while agentd starts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartStep {
    Wait,
    Ready,
    Fail(String),
}

/// One probe while agentd starts.
pub struct StartProbe<'a> {
    /// Our child's exit (None while it runs, or when another app owns it).
    pub exited: Option<Option<i32>>,
    /// Our own child is running (not one another Newton started).
    pub ours: bool,
    pub probe: &'a Probe,
    pub elapsed: Duration,
    /// The log's last line, once our child exited.
    pub log_line: Option<&'a str>,
}

pub fn start_step(
    step: StartProbe,
    check: &mut PortCheck,
    data_dir: &Path,
    port: u16,
) -> StartStep {
    if let Some(code) = step.exited {
        return StartStep::Fail(stopped_message(true, code, step.log_line));
    }
    let decision = if step.ours {
        match (step.probe, decide(step.probe, data_dir, port)) {
            // Our child runs: no whole answer, or an odd one, is it still starting or
            // busy. Had another program held the port, our child would exit (address in
            // use) and its log would say so.
            (Probe::Foreign | Probe::Busy, _) => Decision::Wait,
            (_, decision) => decision,
        }
    } else {
        check.decide(step.probe, data_dir, port)
    };
    match decision {
        Decision::Reuse => StartStep::Ready,
        Decision::Fail(message) => StartStep::Fail(message),
        Decision::Spawn | Decision::Wait if step.elapsed >= START_TIMEOUT => {
            StartStep::Fail(format!(
                "Newton's engine didn't answer within {} s.",
                START_TIMEOUT.as_secs()
            ))
        }
        Decision::Spawn | Decision::Wait => StartStep::Wait,
    }
}

/// "Newton's engine stopped[ while starting] (exit status N)[. Its log ends with: …]"
pub fn stopped_message(starting: bool, code: Option<i32>, log_line: Option<&str>) -> String {
    let when = if starting { " while starting" } else { "" };
    let status = match code {
        Some(code) => format!("exit status {code}"),
        None => "killed by a signal".to_string(),
    };
    match log_line {
        Some(line) => {
            format!("Newton's engine stopped{when} ({status}). Its log ends with: {line}")
        }
        None => format!("Newton's engine stopped{when} ({status})."),
    }
}

/// The log's last non-empty line (for the failure sentence), from its last 4 KB.
pub fn last_log_line(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let len = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(len.saturating_sub(4096))).ok()?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail).ok()?;
    let text = String::from_utf8_lossy(&tail);
    let line = text.lines().rev().map(str::trim).find(|l| !l.is_empty())?;
    let clean: Vec<char> = line.chars().filter(|c| !c.is_control()).collect();
    let mut out: String = clean.iter().take(300).collect();
    if clean.len() > 300 {
        out.push('…');
    }
    Some(out)
}

/// `agentd.log` -> `agentd.log.1` (the run before is kept, older ones dropped).
pub fn rotate_log(path: &Path) -> std::io::Result<()> {
    if path.exists() {
        let mut old = path.as_os_str().to_owned();
        old.push(".1");
        std::fs::rename(path, PathBuf::from(old))?;
    }
    Ok(())
}

struct Inner {
    setup: Setup,
    state: EngineState,
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    lock: Option<File>,
    /// Bumped by every start and stop: a watcher of an older generation stops.
    generation: u64,
}

/// Decides the setup again (on restart: uv may have been installed meanwhile).
type Planner = Box<dyn Fn() -> Setup + Send + Sync>;

pub struct Engine {
    planner: Option<Planner>,
    inner: Mutex<Inner>,
}

fn initial_state(setup: &Setup) -> EngineState {
    match setup {
        Setup::Unmanaged => EngineState::Unmanaged,
        Setup::Launch(_) => EngineState::Starting,
        Setup::Missing { message, .. } => EngineState::Failed {
            message: message.clone(),
        },
    }
}

impl Engine {
    pub fn new(setup: Setup) -> Self {
        Self {
            planner: None,
            inner: Mutex::new(Inner {
                state: initial_state(&setup),
                setup,
                child: None,
                stdin: None,
                lock: None,
                generation: 0,
            }),
        }
    }

    /// From this process's environment and the clone this shell was built from.
    pub fn from_process() -> Self {
        let planner: Planner = Box::new(|| {
            agentd::with_process_env(|env| {
                plan(env, Path::new(env!("CARGO_MANIFEST_DIR")), &is_executable)
            })
        });
        let mut engine = Self::new(planner());
        engine.planner = Some(planner);
        engine
    }

    fn inner(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn state(&self) -> EngineState {
        self.inner().state.clone()
    }

    fn launch(&self) -> Option<Launch> {
        match &self.inner().setup {
            Setup::Launch(launch) => Some(launch.clone()),
            _ => None,
        }
    }

    /// None: connect as usual (the token file, the port).
    pub fn connection_error(&self) -> Option<ConnectionError> {
        let inner = self.inner();
        let log_path = match &inner.setup {
            Setup::Unmanaged => return None,
            Setup::Launch(launch) => launch.log_path(),
            Setup::Missing { log_path, .. } => log_path.clone(),
        };
        inner.state.connection_error(&log_path)
    }

    /// Starts the engine in the background; a missing piece fails it at once.
    pub fn start(self: &Arc<Self>) {
        let generation = {
            let mut inner = self.inner();
            match &inner.setup {
                Setup::Unmanaged => return,
                Setup::Missing { message, log_path } => {
                    let (message, log_path) = (message.clone(), log_path.clone());
                    inner.state = EngineState::Failed {
                        message: message.clone(),
                    };
                    drop(inner);
                    return note_in_log(&log_path, &message);
                }
                Setup::Launch(_) => {}
            }
            inner.generation += 1;
            inner.state = EngineState::Starting;
            inner.generation
        };
        let engine = Arc::clone(self);
        thread::spawn(move || engine.run(generation));
    }

    /// Stops our agentd (if we started it), looks for the clone and uv again and starts
    /// again, in the background.
    pub fn restart(self: &Arc<Self>) -> Result<(), ConnectionError> {
        let replanned = self.planner.as_ref().map(|plan| plan());
        let (child, stdin, launch) = {
            let mut inner = self.inner();
            if let Some(setup) = replanned {
                inner.setup = setup;
            }
            if inner.setup == Setup::Unmanaged {
                return Err(ConnectionError::new(
                    "config",
                    "Newton can't tell where its engine should listen: check NEWTON_PORT \
                     and NEWTON_DATA_DIR."
                        .into(),
                ));
            }
            inner.generation += 1;
            inner.state = initial_state(&inner.setup);
            let launch = matches!(inner.setup, Setup::Launch(_));
            (inner.child.take(), inner.stdin.take(), launch)
        };
        let engine = Arc::clone(self);
        thread::spawn(move || {
            drop(stdin);
            if let Some(child) = child {
                terminate(child, STOP_GRACE);
            }
            if launch {
                engine.start();
            }
        });
        Ok(())
    }

    /// On quit: close agentd's stdin, SIGTERM, wait up to `STOP_GRACE`, then kill.
    pub fn shutdown(&self) {
        let (child, stdin) = {
            let mut inner = self.inner();
            inner.generation += 1;
            (inner.child.take(), inner.stdin.take())
        };
        drop(stdin);
        if let Some(child) = child {
            terminate(child, STOP_GRACE);
        }
    }

    fn current(&self, generation: u64) -> bool {
        self.inner().generation == generation
    }

    fn set_state(&self, generation: u64, state: EngineState) -> bool {
        let mut inner = self.inner();
        let current = inner.generation == generation;
        if current {
            inner.state = state;
        }
        current
    }

    /// Fails the engine and writes why at the end of its log (the log the window shows).
    fn fail(&self, generation: u64, message: String) {
        let log_path = self.launch().map(|launch| launch.log_path());
        let failed = EngineState::Failed {
            message: message.clone(),
        };
        if self.set_state(generation, failed) {
            if let Some(log_path) = log_path {
                note_in_log(&log_path, &message);
            }
        }
    }

    /// The watcher thread: lock, probe, spawn, wait for /health, then keep watching.
    fn run(self: Arc<Self>, generation: u64) {
        let Some(launch) = self.launch() else {
            return;
        };
        if let Err(message) = self.prepare(&launch) {
            return self.fail(generation, message);
        }
        let owner = self.take_lock(&launch);
        let mut check = PortCheck::default();
        let decision = loop {
            if !self.current(generation) {
                return;
            }
            let probe = agentd::probe_health(launch.port, PROBE_TIMEOUT);
            match check.decide(&probe, &launch.data_dir, launch.port) {
                // A busy agentd (or a silent program): ask again.
                Decision::Wait => thread::sleep(START_POLL),
                decision => break decision,
            }
        };
        match decision {
            Decision::Reuse => {
                self.set_state(generation, EngineState::Running { reused: true });
                return self.watch_reused(generation, &launch, owner);
            }
            Decision::Fail(message) => return self.fail(generation, message),
            Decision::Spawn if owner => {
                if let Err(message) = self.spawn(generation, &launch) {
                    return self.fail(generation, message);
                }
            }
            // Another Newton holds the lock: it is starting the engine; wait for it.
            Decision::Spawn | Decision::Wait => {}
        }
        if self.wait_ready(generation, &launch) {
            self.watch_child(generation, &launch);
        }
    }

    fn prepare(&self, launch: &Launch) -> Result<(), String> {
        let logs = launch.data_dir.join(LOG_DIR);
        std::fs::create_dir_all(&logs).map_err(|e| {
            format!(
                "Newton can't create its data folder {}: {e}",
                logs.display()
            )
        })
    }

    /// True when this shell holds (or now takes) the data dir's engine lock.
    fn take_lock(&self, launch: &Launch) -> bool {
        if self.inner().lock.is_some() {
            return true;
        }
        let Ok(file) = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(launch.lock_path())
        else {
            return false;
        };
        if file.try_lock().is_err() {
            return false;
        }
        self.inner().lock = Some(file);
        true
    }

    fn spawn(&self, generation: u64, launch: &Launch) -> Result<(), String> {
        let log_path = launch.log_path();
        let cannot_log = |e: std::io::Error| {
            format!(
                "Newton can't write its engine log {}: {e}",
                log_path.display()
            )
        };
        rotate_log(&log_path).map_err(cannot_log)?;
        let log = File::create(&log_path).map_err(cannot_log)?;
        let log_err = log.try_clone().map_err(cannot_log)?;
        let argv = launch.argv();
        let path = std::env::var("PATH").ok();
        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .envs(launch.env(path.as_deref()))
            .current_dir(&launch.repo)
            .stdin(Stdio::piped())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(log_err));
        for name in ENV_REMOVE {
            command.env_remove(name);
        }
        let mut child = command.spawn().map_err(|e| {
            format!(
                "Newton couldn't start its engine with {}: {e}",
                launch.uv.display()
            )
        })?;
        let stdin = child.stdin.take();
        let mut inner = self.inner();
        if inner.generation != generation {
            // Quit or restarted meanwhile: don't leave this one running.
            drop(inner);
            drop(stdin);
            terminate(child, STOP_GRACE);
            return Ok(());
        }
        inner.child = Some(child);
        inner.stdin = stdin;
        Ok(())
    }
    /// Our child's exit, if it has exited (None while running or when not ours).
    fn child_exit(&self) -> Option<Option<i32>> {
        let mut inner = self.inner();
        let child = inner.child.as_mut()?;
        match child.try_wait() {
            Ok(Some(status)) => {
                inner.child = None;
                inner.stdin = None;
                Some(status.code())
            }
            Ok(None) => None,
            Err(_) => Some(None),
        }
    }

    fn wait_ready(&self, generation: u64, launch: &Launch) -> bool {
        let started = Instant::now();
        let log_path = launch.log_path();
        let mut check = PortCheck::default();
        while self.current(generation) {
            let exited = self.child_exit();
            let ours = exited.is_none() && self.inner().child.is_some();
            let probe = if exited.is_some() {
                Probe::Free
            } else {
                agentd::probe_health(launch.port, PROBE_TIMEOUT)
            };
            let line = exited.and_then(|_| last_log_line(&log_path));
            let step = StartProbe {
                exited,
                ours,
                probe: &probe,
                elapsed: started.elapsed(),
                log_line: line.as_deref(),
            };
            match start_step(step, &mut check, &launch.data_dir, launch.port) {
                StartStep::Wait => thread::sleep(START_POLL),
                StartStep::Ready => {
                    let reused = self.inner().child.is_none();
                    self.set_state(generation, EngineState::Running { reused });
                    return true;
                }
                StartStep::Fail(message) => {
                    let child = self.inner().child.take();
                    if let Some(child) = child {
                        terminate(child, STOP_GRACE);
                    }
                    self.fail(generation, message);
                    return false;
                }
            }
        }
        false
    }

    fn watch_child(&self, generation: u64, launch: &Launch) {
        while self.current(generation) {
            if self.inner().child.is_none() {
                // Started by another Newton: watch it like a reused one.
                return self.watch_reused(generation, launch, false);
            }
            if let Some(code) = self.child_exit() {
                let line = last_log_line(&launch.log_path());
                return self.fail(generation, stopped_message(false, code, line.as_deref()));
            }
            thread::sleep(RUN_POLL);
        }
    }

    /// An agentd we didn't start: when it goes away, start ours (if we hold the lock).
    fn watch_reused(&self, generation: u64, launch: &Launch, owner: bool) {
        let mut misses = 0;
        while self.current(generation) {
            thread::sleep(RUN_POLL);
            match agentd::probe_health(launch.port, PROBE_TIMEOUT) {
                Probe::Free => misses += 1,
                _ => misses = 0,
            }
            if misses >= 3 {
                if owner || self.take_lock(launch) {
                    self.set_state(generation, EngineState::Starting);
                    if let Err(message) = self.spawn(generation, launch) {
                        return self.fail(generation, message);
                    }
                    if self.wait_ready(generation, launch) {
                        self.watch_child(generation, launch);
                    }
                    return;
                }
                misses = 0;
            }
        }
    }
}

/// SIGTERM, then SIGKILL after `grace`.
pub fn terminate(mut child: Child, grace: Duration) {
    if let Ok(Some(_)) = child.try_wait() {
        return;
    }
    #[cfg(unix)]
    {
        if let Ok(pid) = libc::pid_t::try_from(child.id()) {
            // SAFETY: kill(2) on our own child's pid; no memory is touched.
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
        }
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            if let Ok(Some(_)) = child.try_wait() {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    #[cfg(not(unix))]
    let _ = grace;
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn temp_dir() -> PathBuf {
        static N: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "newton-engine-test-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn launch() -> Launch {
        Launch {
            uv: "/Users/alice/.local/bin/uv".into(),
            repo: "/Users/alice/src/newton".into(),
            data_dir: "/Users/alice/Library/Application Support/Newton".into(),
            port: 8765,
        }
    }

    /// A fake clone: `<dir>/services/agentd/pyproject.toml`, `.venv` if asked, and the
    /// crate folder three levels down.
    fn fake_repo(venv: bool) -> (PathBuf, PathBuf) {
        let repo = temp_dir();
        std::fs::create_dir_all(repo.join("services/agentd")).unwrap();
        std::fs::write(repo.join(REPO_MARKER), "").unwrap();
        if venv {
            std::fs::create_dir_all(repo.join(".venv")).unwrap();
        }
        let manifest = repo.join("apps/desktop/src-tauri");
        std::fs::create_dir_all(&manifest).unwrap();
        (repo, manifest)
    }

    fn plan_with(vars: &[(&str, &str)], manifest: &Path, exes: &[&str]) -> Setup {
        let map: HashMap<String, String> = vars
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        let var = move |name: &str| map.get(name).cloned();
        let exes: HashSet<PathBuf> = exes.iter().map(PathBuf::from).collect();
        plan(
            &Env {
                var: &var,
                home: Some(PathBuf::from("/Users/alice")),
                macos: true,
            },
            manifest,
            &|p: &Path| exes.contains(p),
        )
    }

    #[test]
    fn argv_follows_the_contract() {
        let argv: Vec<String> = launch()
            .argv()
            .into_iter()
            .map(|a| a.into_string().unwrap())
            .collect();
        assert_eq!(
            argv,
            [
                "/Users/alice/.local/bin/uv",
                "run",
                "--project",
                "/Users/alice/src/newton",
                "--frozen",
                "newton-agentd",
                "serve",
                "--port",
                "8765",
                "--data-dir",
                "/Users/alice/Library/Application Support/Newton",
                "--exit-on-stdin-eof",
            ]
        );
    }

    /// agentd's CLI really has the flag the argv passes.
    #[test]
    fn agentd_main_has_exit_on_stdin_eof() {
        let repo = find_repo(None, Path::new(env!("CARGO_MANIFEST_DIR"))).unwrap();
        let main =
            std::fs::read_to_string(repo.join("services/agentd/newton_agentd/main.py")).unwrap();
        assert!(main.contains("\"--exit-on-stdin-eof\""));
    }

    #[test]
    fn env_follows_the_contract() {
        let env: HashMap<_, _> = launch().env(Some("/usr/bin:/bin")).into_iter().collect();
        assert_eq!(env.len(), 2, "only these on top of the user's environment");
        assert_eq!(env["NEWTON_RESOURCES_DIR"], "/Users/alice/src/newton");
        assert_eq!(
            env["PATH"],
            "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        );
        assert!(!env.contains_key("NEWTON_PACKAGED"));
        assert!(ENV_REMOVE.contains(&"NEWTON_PACKAGED"));
        assert!(ENV_REMOVE.contains(&"PYTHONPATH") && ENV_REMOVE.contains(&"VIRTUAL_ENV"));
        assert_eq!(
            launch().log_path(),
            PathBuf::from("/Users/alice/Library/Application Support/Newton/logs/agentd.log")
        );
    }

    #[test]
    fn child_path_keeps_the_users_order_and_adds_what_is_missing() {
        assert_eq!(
            child_path(Some("/opt/homebrew/bin:/usr/bin:/custom")),
            "/opt/homebrew/bin:/usr/bin:/custom:/bin:/usr/sbin:/sbin:/usr/local/bin"
        );
        assert_eq!(
            child_path(None),
            "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        );
    }

    #[test]
    fn this_crate_lives_in_a_clone() {
        let repo = find_repo(None, Path::new(env!("CARGO_MANIFEST_DIR"))).unwrap();
        assert!(repo.join("apps/desktop/src-tauri/Cargo.toml").is_file());
    }

    #[test]
    fn find_repo_goes_three_levels_up_or_takes_the_override() {
        let (repo, manifest) = fake_repo(false);
        assert_eq!(find_repo(None, &manifest), Ok(repo.clone()));
        assert_eq!(
            find_repo(Some(&repo), Path::new("/nowhere")),
            Ok(repo.clone())
        );
        let err = find_repo(None, &repo.join("apps")).unwrap_err();
        assert!(
            err.contains("scripts/run.sh") && err.contains(REPO_ENV),
            "{err}"
        );
        let err = find_repo(Some(&manifest), &manifest).unwrap_err();
        assert!(err.starts_with("NEWTON_REPO is set to "), "{err}");
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn find_uv_tries_path_then_the_installers_folders() {
        let only =
            |exes: &'static [&'static str]| move |p: &Path| exes.iter().any(|e| Path::new(e) == p);
        let home = Some(Path::new("/Users/alice"));
        assert_eq!(
            find_uv(
                Some("/a:/b"),
                home,
                &only(&["/b/uv", "/Users/alice/.local/bin/uv"])
            ),
            Some(PathBuf::from("/b/uv"))
        );
        // A Finder-launched app: /usr/bin:/bin only.
        assert_eq!(
            find_uv(
                Some("/usr/bin:/bin"),
                home,
                &only(&["/Users/alice/.cargo/bin/uv", "/opt/homebrew/bin/uv"])
            ),
            Some(PathBuf::from("/Users/alice/.cargo/bin/uv"))
        );
        // Relative PATH entries are never searched.
        assert_eq!(
            find_uv(Some(".:bin"), None, &|_: &Path| true),
            Some(PathBuf::from("/opt/homebrew/bin/uv"))
        );
        assert_eq!(find_uv(None, home, &|_: &Path| false), None);
    }

    #[test]
    fn is_executable_needs_an_x_bit() {
        let dir = temp_dir();
        let file = dir.join("uv");
        std::fs::write(&file, "#!/bin/sh\n").unwrap();
        assert!(!is_executable(&file));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o755)).unwrap();
            assert!(is_executable(&file));
        }
        assert!(!is_executable(&dir), "a folder");
        assert!(!is_executable(&dir.join("missing")));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn plan_launches_from_the_clone_with_uv() {
        let (repo, manifest) = fake_repo(true);
        let setup = plan_with(
            &[("PATH", "/usr/bin:/bin"), ("NEWTON_PORT", "8799")],
            &manifest,
            &["/Users/alice/.local/bin/uv"],
        );
        assert_eq!(
            setup,
            Setup::Launch(Launch {
                uv: "/Users/alice/.local/bin/uv".into(),
                repo: repo.clone(),
                data_dir: "/Users/alice/Library/Application Support/Newton".into(),
                port: 8799,
            })
        );
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn plan_says_what_to_install_or_run() {
        let log = PathBuf::from("/Users/alice/Library/Application Support/Newton/logs/agentd.log");
        let (repo, manifest) = fake_repo(false);
        let uv = ["/opt/homebrew/bin/uv"];
        // No uv anywhere.
        let Setup::Missing { message, log_path } = plan_with(&[], &manifest, &[]) else {
            panic!("no uv must not launch");
        };
        assert!(message.starts_with("uv isn't installed"), "{message}");
        assert!(message.contains("run scripts/setup.sh"), "{message}");
        assert_eq!(log_path, log);
        // uv but no .venv yet.
        let Setup::Missing { message, .. } = plan_with(&[], &manifest, &uv) else {
            panic!("no environment must not launch");
        };
        assert!(
            message.contains("Python environment isn't set up") && message.contains("setup.sh"),
            "{message}"
        );
        // ...unless uv is told where the environment is.
        assert!(matches!(
            plan_with(&[("UV_PROJECT_ENVIRONMENT", "/venvs/n")], &manifest, &uv),
            Setup::Launch(_)
        ));
        // A shell built from a clone that moved, and a wrong NEWTON_REPO.
        let Setup::Missing { message, .. } = plan_with(&[], Path::new("/gone/a/b/c"), &uv) else {
            panic!("no clone must not launch");
        };
        assert!(
            message.contains("/gone") && message.contains("NEWTON_REPO"),
            "{message}"
        );
        let Setup::Missing { message, .. } =
            plan_with(&[("NEWTON_REPO", "~/elsewhere")], &manifest, &uv)
        else {
            panic!("a wrong NEWTON_REPO must not launch");
        };
        assert!(message.contains("/Users/alice/elsewhere"), "{message}");
        // A bad port: agentd_connection reports it, the engine stays out of the way.
        assert_eq!(
            plan_with(&[("NEWTON_PORT", "x")], &manifest, &uv),
            Setup::Unmanaged
        );
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn a_missing_piece_fails_with_the_log_path_and_restart_checks_again() {
        let dir = temp_dir();
        let log_path = dir.join(LOG_DIR).join(LOG_FILE);
        let engine = Arc::new(Engine::new(Setup::Missing {
            message: "uv isn't installed: run scripts/setup.sh".into(),
            log_path: log_path.clone(),
        }));
        let err = engine.connection_error().unwrap();
        assert_eq!(err.code, "engine_failed");
        assert_eq!(err.message, "uv isn't installed: run scripts/setup.sh");
        assert_eq!(err.log_path, Some(log_path.display().to_string()));
        engine.start();
        assert!(std::fs::read_to_string(&log_path)
            .unwrap()
            .contains("uv isn't installed"));
        engine.restart().unwrap();
        assert_eq!(engine.connection_error().unwrap().code, "engine_failed");
        engine.shutdown();
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn decide_reuses_only_an_agentd_with_the_same_data_dir() {
        let data = Path::new("/d/Newton");
        let ours = Probe::Agentd {
            db_path: Some("/d/Newton/newton.db".into()),
        };
        let theirs = Probe::Agentd {
            db_path: Some("/repo/.data/newton.db".into()),
        };
        assert_eq!(decide(&Probe::Free, data, 8765), Decision::Spawn);
        assert_eq!(decide(&ours, data, 8765), Decision::Reuse);
        let Decision::Fail(msg) = decide(&theirs, data, 8765) else {
            panic!("another data dir must not be reused");
        };
        assert!(
            msg.contains("port 8765") && msg.contains("/repo/.data"),
            "{msg}"
        );
        let Decision::Fail(msg) = decide(&Probe::Foreign, data, 8765) else {
            panic!("a foreign program must not be reused");
        };
        assert!(
            msg.starts_with("Port 8765 is taken by another program"),
            "{msg}"
        );
        assert_eq!(decide(&Probe::Busy, data, 8765), Decision::Wait);
    }

    /// A listener that accepts and never answers stands in for a busy agentd; a
    /// `sleep` child stands in for the one we just spawned: it keeps running.
    #[cfg(unix)]
    #[test]
    fn a_slow_health_reply_does_not_kill_our_starting_child() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut l = launch();
        l.data_dir = temp_dir();
        l.port = listener.local_addr().unwrap().port();
        let engine = Arc::new(Engine::new(Setup::Launch(l.clone())));
        let child = Command::new("/bin/sleep")
            .arg("30")
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let generation = {
            let mut inner = engine.inner();
            inner.generation += 1;
            inner.child = Some(child);
            inner.generation
        };
        let watcher = {
            let (engine, l) = (Arc::clone(&engine), l.clone());
            thread::spawn(move || engine.wait_ready(generation, &l))
        };
        thread::sleep(Duration::from_millis(2500));
        assert_eq!(engine.state(), EngineState::Starting);
        assert!(engine.inner().child.is_some(), "our agentd still runs");
        engine.shutdown();
        assert!(!watcher.join().unwrap());
        drop(listener);
        std::fs::remove_dir_all(&l.data_dir).unwrap();
    }

    /// A port held by another HTTP server: the engine fails after FOREIGN_PROBES and
    /// writes why at the end of the log the window points to.
    #[test]
    fn a_port_conflict_fails_and_says_why_in_the_log() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut l = launch();
        l.data_dir = temp_dir();
        l.port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let mut buf = [0u8; 512];
                let _ = stream.read(&mut buf);
                let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhi");
            }
        });
        std::fs::create_dir_all(l.data_dir.join(LOG_DIR)).unwrap();
        std::fs::write(l.log_path(), "an earlier run's last words\n").unwrap();
        let engine = Arc::new(Engine::new(Setup::Launch(l.clone())));
        engine.start();
        let started = Instant::now();
        let message = loop {
            if let EngineState::Failed { message } = engine.state() {
                break message;
            }
            assert!(
                started.elapsed() < Duration::from_secs(10),
                "still starting"
            );
            thread::sleep(Duration::from_millis(50));
        };
        assert!(message.starts_with("Port "), "{message}");
        assert_eq!(
            last_log_line(&l.log_path()),
            Some(format!("newton desktop: {message}"))
        );
        let err = engine.connection_error().unwrap();
        assert_eq!(err.log_path, Some(l.log_path().display().to_string()));
        engine.shutdown();
        std::fs::remove_dir_all(&l.data_dir).unwrap();
    }

    #[test]
    fn start_steps() {
        let data = Path::new("/d/Newton");
        let ours = Probe::Agentd {
            db_path: Some("/d/Newton/newton.db".into()),
        };
        let s = Duration::from_secs(1);
        let step = |exited, mine, probe: &Probe, elapsed, line| {
            start_step(
                StartProbe {
                    exited,
                    ours: mine,
                    probe,
                    elapsed,
                    log_line: line,
                },
                &mut PortCheck::default(),
                data,
                1,
            )
        };
        assert_eq!(step(None, true, &Probe::Free, s, None), StartStep::Wait);
        assert_eq!(step(None, true, &ours, s, None), StartStep::Ready);
        assert_eq!(step(None, false, &ours, s, None), StartStep::Ready);
        assert!(matches!(
            step(None, true, &Probe::Free, START_TIMEOUT, None),
            StartStep::Fail(m) if m.contains("didn't answer within")
        ));
        assert_eq!(
            step(Some(Some(1)), false, &ours, s, Some("ImportError: x")),
            StartStep::Fail(
                "Newton's engine stopped while starting (exit status 1). \
                 Its log ends with: ImportError: x"
                    .into()
            )
        );
        // Another agentd with its own data dir answered in full: conclusive.
        let theirs = Probe::Agentd {
            db_path: Some("/elsewhere/newton.db".into()),
        };
        assert!(matches!(
            step(None, true, &theirs, s, None),
            StartStep::Fail(m) if m.contains("Another Newton engine")
        ));
    }

    /// Our child runs and its /health is slow (a busy event loop) or odd: keep waiting,
    /// never "port taken", until it exits or START_TIMEOUT.
    #[test]
    fn our_own_starting_child_is_never_mistaken_for_another_program() {
        let data = Path::new("/d/Newton");
        let mut check = PortCheck::default();
        for probe in [Probe::Busy, Probe::Foreign].iter().cycle().take(60) {
            let step = StartProbe {
                exited: None,
                ours: true,
                probe,
                elapsed: Duration::from_secs(5),
                log_line: None,
            };
            assert_eq!(
                start_step(step, &mut check, data, 8765),
                StartStep::Wait,
                "{probe:?}"
            );
        }
        let step = StartProbe {
            exited: None,
            ours: true,
            probe: &Probe::Busy,
            elapsed: START_TIMEOUT,
            log_line: None,
        };
        assert!(matches!(
            start_step(step, &mut check, data, 8765),
            StartStep::Fail(m) if m.contains("didn't answer within")
        ));
    }

    #[test]
    fn port_check_needs_repeated_evidence_before_calling_the_port_taken() {
        let data = Path::new("/d/Newton");
        let ours = Probe::Agentd {
            db_path: Some("/d/Newton/newton.db".into()),
        };
        // A busy agentd: silent probes wait, then it answers and is reused.
        let mut check = PortCheck::default();
        for _ in 0..SILENT_PROBES - 1 {
            assert_eq!(check.decide(&Probe::Busy, data, 8765), Decision::Wait);
        }
        assert_eq!(check.decide(&ours, data, 8765), Decision::Reuse);
        // ...and the streak starts over after it answered.
        assert_eq!(check.decide(&Probe::Busy, data, 8765), Decision::Wait);
        // A program that never answers: conclusive after SILENT_PROBES.
        let mut check = PortCheck::default();
        for _ in 0..SILENT_PROBES - 1 {
            assert_eq!(check.decide(&Probe::Busy, data, 8765), Decision::Wait);
        }
        assert!(matches!(
            check.decide(&Probe::Busy, data, 8765),
            Decision::Fail(m) if m.contains("port 8765") && m.contains("NEWTON_PORT")
        ));
        // Another program's whole answer: conclusive after FOREIGN_PROBES.
        let mut check = PortCheck::default();
        for _ in 0..FOREIGN_PROBES - 1 {
            assert_eq!(check.decide(&Probe::Foreign, data, 8765), Decision::Wait);
            assert_eq!(check.decide(&Probe::Busy, data, 8765), Decision::Wait);
        }
        assert!(matches!(
            check.decide(&Probe::Foreign, data, 8765),
            Decision::Fail(m) if m.starts_with("Port 8765 is taken by another program")
        ));
        // A free port is decided at once.
        assert_eq!(
            PortCheck::default().decide(&Probe::Free, data, 8765),
            Decision::Spawn
        );
    }

    #[test]
    fn state_to_connection_error() {
        let log = Path::new("/d/logs/agentd.log");
        assert_eq!(EngineState::Unmanaged.connection_error(log), None);
        assert_eq!(
            EngineState::Running { reused: false }.connection_error(log),
            None
        );
        assert_eq!(
            EngineState::Starting.connection_error(log).unwrap().code,
            "engine_starting"
        );
        let failed = EngineState::Failed {
            message: "boom".into(),
        }
        .connection_error(log)
        .unwrap();
        assert_eq!(failed.code, "engine_failed");
        assert_eq!(failed.message, "boom");
        assert_eq!(failed.log_path.as_deref(), Some("/d/logs/agentd.log"));
    }

    #[test]
    fn unmanaged_engine_never_blocks_the_connection() {
        let engine = Arc::new(Engine::new(Setup::Unmanaged));
        assert_eq!(engine.state(), EngineState::Unmanaged);
        assert_eq!(engine.connection_error(), None);
        engine.start();
        assert_eq!(engine.state(), EngineState::Unmanaged);
        assert_eq!(engine.restart().unwrap_err().code, "config");
        engine.shutdown();
    }

    #[test]
    fn log_rotation_and_last_line() {
        let dir = temp_dir();
        let log = dir.join(LOG_FILE);
        rotate_log(&log).unwrap();
        std::fs::write(&log, "first run\n").unwrap();
        rotate_log(&log).unwrap();
        assert!(!log.exists());
        assert_eq!(
            std::fs::read_to_string(dir.join("agentd.log.1")).unwrap(),
            "first run\n"
        );
        std::fs::write(&log, "a\nTraceback\nModuleNotFoundError: numpy\n\n").unwrap();
        assert_eq!(
            last_log_line(&log).as_deref(),
            Some("ModuleNotFoundError: numpy")
        );
        assert_eq!(last_log_line(&dir.join("missing")), None);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn lock_is_exclusive_per_data_dir() {
        let dir = temp_dir();
        let mut l = launch();
        l.data_dir = dir.clone();
        let a = Engine::new(Setup::Launch(l.clone()));
        let b = Engine::new(Setup::Launch(l.clone()));
        assert!(a.take_lock(&l));
        assert!(a.take_lock(&l), "re-taking our own lock");
        assert!(!b.take_lock(&l));
        drop(a);
        assert!(b.take_lock(&l), "released when the holder goes away");
        std::fs::remove_dir_all(dir).unwrap();
    }

    /// The whole cycle against this clone with the real uv (its .venv set up): spawn,
    /// wait for /health, a second shell reuses it, restart, quit.
    /// Run: `cargo test -- --ignored live_engine`
    #[test]
    #[ignore]
    fn live_engine() {
        let repo = find_repo(None, Path::new(env!("CARGO_MANIFEST_DIR"))).unwrap();
        let home = std::env::var_os("HOME").map(PathBuf::from);
        let path = std::env::var("PATH").ok();
        let uv = find_uv(path.as_deref(), home.as_deref(), &is_executable).expect("uv");
        let data_dir = temp_dir();
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let engine = Arc::new(Engine::new(Setup::Launch(Launch {
            uv,
            repo,
            data_dir: data_dir.clone(),
            port,
        })));
        let wait_running = |engine: &Arc<Engine>| {
            let started = Instant::now();
            loop {
                match engine.state() {
                    EngineState::Running { reused } => return (reused, started.elapsed()),
                    EngineState::Failed { message } => panic!("{message}"),
                    _ => thread::sleep(Duration::from_millis(100)),
                }
                assert!(started.elapsed() < START_TIMEOUT, "still starting");
            }
        };
        engine.start();
        assert_eq!(
            engine.connection_error().map(|e| e.code),
            match engine.state() {
                EngineState::Starting => Some("engine_starting"),
                _ => None,
            }
        );
        let (reused, took) = wait_running(&engine);
        assert!(!reused);
        eprintln!("engine ready in {took:?}");
        assert!(data_dir.join("api-token").exists());
        assert!(data_dir.join(LOG_DIR).join(LOG_FILE).exists());
        // A second app on the same data dir reuses it and doesn't spawn its own.
        let other = Arc::new(Engine::new(Setup::Launch(engine.launch().unwrap())));
        other.start();
        assert!(wait_running(&other).0, "reused");
        other.shutdown();

        engine.restart().unwrap();
        let (reused, took) = wait_running(&engine);
        assert!(!reused);
        eprintln!("restarted in {took:?}");
        assert!(data_dir.join(LOG_DIR).join("agentd.log.1").exists());
        let started = Instant::now();
        engine.shutdown();
        eprintln!("stopped in {:?}", started.elapsed());
        assert_eq!(
            agentd::probe_health(port, PROBE_TIMEOUT),
            Probe::Free,
            "agentd stopped"
        );
        std::fs::remove_dir_all(data_dir).unwrap();
    }

    /// A real child standing in for agentd: SIGTERM stops it well before the grace.
    #[cfg(unix)]
    #[test]
    fn terminate_stops_a_child_with_sigterm() {
        let child = Command::new("/bin/sleep")
            .arg("30")
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let started = Instant::now();
        terminate(child, Duration::from_secs(5));
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
