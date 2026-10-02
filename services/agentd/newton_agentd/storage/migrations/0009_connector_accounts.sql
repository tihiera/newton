-- One-click Connect for GitHub and Notion: who is connected and how. Tokens (and
-- Notion's refresh token) live in the secret store, never here; expires_at is when
-- Notion's access token runs out (not a secret), so it can be refreshed in time.
CREATE TABLE connector_accounts (
    target TEXT PRIMARY KEY CHECK (target IN ('github', 'notion')),
    name TEXT NOT NULL DEFAULT '',  -- the GitHub login, or the Notion workspace's name
    icon TEXT,                      -- GitHub avatar URL, Notion workspace icon (emoji or URL)
    method TEXT NOT NULL CHECK (method IN ('oauth', 'token', 'gh')),
    expires_at REAL,
    connected_at REAL NOT NULL
);
