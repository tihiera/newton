-- B7: a goal's poll that couldn't reach arXiv (offline, arXiv down) says why and when
-- the loop looks again: 5 min, doubling to 1 h while failing (next_poll_at is set only
-- while polls fail). last_polled_at stays the last poll that worked; a successful poll
-- clears all three.
ALTER TABLE goals ADD COLUMN last_poll_error TEXT;
ALTER TABLE goals ADD COLUMN next_poll_at REAL;
ALTER TABLE goals ADD COLUMN poll_failures INTEGER NOT NULL DEFAULT 0;
