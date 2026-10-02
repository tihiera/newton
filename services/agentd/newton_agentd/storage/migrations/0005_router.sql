-- SV3: the local profile (no user accounts: one person on one Mac) and the
-- router's provenance log. The router key lives in the secret store, never here;
-- no prompt or completion text is ever stored.
CREATE TABLE profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    display_name TEXT,
    default_model TEXT,             -- what model "default" means in /v1 requests
    updated_at REAL NOT NULL
);
INSERT INTO profile (id, updated_at) VALUES (1, strftime('%s', 'now'));

CREATE TABLE router_requests (
    id TEXT PRIMARY KEY,            -- also the X-Newton-Request-Id header
    path TEXT NOT NULL,             -- /v1/chat/completions, ...
    model_requested TEXT,
    service_id TEXT,                -- what served it (NULL: nothing did)
    host_id TEXT,
    engine TEXT,
    model TEXT,
    revision TEXT,
    stream INTEGER NOT NULL DEFAULT 0,
    status INTEGER,                 -- HTTP status returned to the client
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    queued_ms REAL,                 -- waiting for a free slot
    first_byte_ms REAL,             -- from sending upstream to its first byte
    duration_ms REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX idx_router_requests_created ON router_requests (created_at);
CREATE INDEX idx_router_requests_service ON router_requests (service_id, created_at);
