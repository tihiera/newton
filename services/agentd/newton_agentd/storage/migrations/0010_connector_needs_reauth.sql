-- Set when Notion (through the broker) rejected a refresh of an OAuth connection: the
-- user has to connect Notion again. A new connection, a paste or a disconnect clears it.
ALTER TABLE connector_accounts
    ADD COLUMN needs_reauth INTEGER NOT NULL DEFAULT 0 CHECK (needs_reauth IN (0, 1));
