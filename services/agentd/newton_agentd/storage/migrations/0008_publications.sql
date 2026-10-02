-- B6: reports published to GitHub or Notion, each one approved first. The text that
-- was approved is frozen (by sha256, and on disk) and is exactly what is sent; tokens
-- live in the secret store, never here.
CREATE TABLE publications (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments (id),
    target TEXT NOT NULL CHECK (target IN ('github', 'notion')),
    destination TEXT NOT NULL,      -- JSON: {kind: gist|issue, repo} or {parent_page_id}
    state TEXT NOT NULL,            -- awaiting_approval, approved, publishing, published,
                                    -- rejected, failed
    content_sha256 TEXT NOT NULL,
    content_path TEXT NOT NULL,
    url TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_publications_experiment ON publications (experiment_id);
