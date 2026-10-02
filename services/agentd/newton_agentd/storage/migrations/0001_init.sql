-- Initial schema. JSON columns hold UTF-8 JSON text. Timestamps are unix seconds.

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_entity ON events (entity_type, entity_id);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE hosts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('local', 'ssh', 'brev')),
    ssh_target TEXT,
    ssh_user TEXT,
    ssh_port INTEGER,
    remote_port INTEGER NOT NULL DEFAULT 8777,
    python TEXT NOT NULL DEFAULT 'python3',
    use_venv INTEGER NOT NULL DEFAULT 1,
    install_deps INTEGER NOT NULL DEFAULT 1,
    token_ref TEXT NOT NULL,
    max_parallel_jobs INTEGER NOT NULL DEFAULT 2,
    hardware TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_error TEXT,
    last_checked_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE research_items (
    id TEXT PRIMARY KEY,
    goal_id TEXT REFERENCES goals (id),
    kind TEXT NOT NULL DEFAULT 'paper',
    title TEXT NOT NULL,
    source TEXT,
    external_id TEXT,
    state TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE experiments (
    id TEXT PRIMARY KEY,
    goal_id TEXT REFERENCES goals (id),
    research_item_id TEXT REFERENCES research_items (id),
    host_id TEXT NOT NULL REFERENCES hosts (id),
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    state TEXT NOT NULL,
    evaluation TEXT,
    evidence TEXT,
    report_path TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_experiments_state ON experiments (state);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT REFERENCES experiments (id),
    host_id TEXT NOT NULL REFERENCES hosts (id),
    role TEXT NOT NULL,
    label TEXT NOT NULL,
    manifest TEXT NOT NULL,
    bundle_path TEXT,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    remote_id TEXT,
    remote_status TEXT,
    metrics TEXT,
    results TEXT,
    artifacts_dir TEXT,
    log_offsets TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    next_attempt_at REAL,
    started_at REAL,
    finished_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_jobs_state ON jobs (state);
CREATE INDEX idx_jobs_experiment ON jobs (experiment_id);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decision_note TEXT,
    decided_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX idx_approvals_status ON approvals (status);
