-- B5: the research loop. Goals say where to look and how often; findings are the
-- scientific memory: what each tested scheme's experiment showed, claim by claim.
ALTER TABLE goals ADD COLUMN categories TEXT NOT NULL DEFAULT '["physics.comp-ph","math.NA","physics.flu-dyn"]';
ALTER TABLE goals ADD COLUMN poll_hours REAL NOT NULL DEFAULT 24;
ALTER TABLE goals ADD COLUMN last_polled_at REAL;
ALTER TABLE goals ADD COLUMN auto_propose INTEGER NOT NULL DEFAULT 1;

CREATE TABLE findings (
    id TEXT PRIMARY KEY,
    goal_id TEXT REFERENCES goals (id),
    research_item_id TEXT REFERENCES research_items (id),
    experiment_id TEXT NOT NULL REFERENCES experiments (id),
    scheme_name TEXT,
    scheme_digest TEXT,             -- the method tested (ir.method_digest: flux + time)
    evidence TEXT NOT NULL,         -- green / yellow / red / unknown
    claims TEXT NOT NULL DEFAULT '[]',  -- [{claim, claimed, holds}]
    summary TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (experiment_id)
);
CREATE INDEX idx_findings_scheme ON findings (scheme_digest);
