-- Model services (SV2): long-running model servers on a host, managed by agentd.
-- The worker runs them (services/worker/newton_worker/services.py); agentd keeps
-- the record, the approval, the API key (in the Keychain: only its reference is
-- stored here) and the Mac-side forward.
CREATE TABLE services (
    id TEXT PRIMARY KEY,            -- also the worker's service_id
    host_id TEXT NOT NULL REFERENCES hosts (id),
    name TEXT NOT NULL,
    spec TEXT NOT NULL,             -- ServiceSpec JSON
    state TEXT NOT NULL,
    remote_state TEXT,              -- the worker's own state, as last seen
    healthy INTEGER,
    remote_port INTEGER,
    local_port INTEGER,             -- 127.0.0.1 port on this Mac
    api_key_ref TEXT,               -- secret-store account, never the key
    error TEXT,
    last_seen_at REAL,
    ready_at REAL,
    finished_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_services_host_state ON services (host_id, state);
