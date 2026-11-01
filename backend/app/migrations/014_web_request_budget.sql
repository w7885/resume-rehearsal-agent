-- Append metadata; existing reference snapshots and cost records stay intact.
ALTER TABLE web_requests ADD COLUMN scope_run_id TEXT;
ALTER TABLE web_requests ADD COLUMN budget_reservation_id TEXT;
ALTER TABLE web_requests ADD COLUMN deadline_at REAL;
UPDATE web_requests SET scope_run_id=run_id WHERE scope_run_id IS NULL;
CREATE INDEX web_requests_turn ON web_requests(session_id, scope_run_id, operation);
