CREATE TABLE request_log (
    scope_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    response_json TEXT,
    state TEXT NOT NULL DEFAULT 'completed' CHECK (state IN ('completed', 'deleted')),
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (scope_key, request_id),
    CHECK ((state = 'completed' AND request_hash IS NOT NULL AND response_json IS NOT NULL
            AND json_valid(response_json) AND json_type(response_json) = 'object')
        OR (state = 'deleted' AND request_hash IS NULL AND response_json IS NULL AND session_id IS NULL))
) STRICT;

CREATE INDEX requests_expiry ON request_log(expires_at);
CREATE INDEX requests_session ON request_log(session_id);

CREATE TRIGGER scrub_deleted_session_requests BEFORE DELETE ON sessions
BEGIN
    UPDATE request_log SET state = 'deleted', request_hash = NULL,
        response_json = NULL, session_id = NULL WHERE session_id = OLD.session_id;
END;
