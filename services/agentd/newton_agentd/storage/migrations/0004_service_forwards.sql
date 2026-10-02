-- The ssh master behind each service's forward: if agentd dies, a new one finds
-- and ends it (instead of leaving a stray connection holding the local port).
ALTER TABLE services ADD COLUMN forward_pid INTEGER;
ALTER TABLE services ADD COLUMN forward_control TEXT;
