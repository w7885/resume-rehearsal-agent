CREATE TABLE suggestion_decisions (
    session_id TEXT NOT NULL,
    suggestion_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'revise')),
    feedback TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (decision != 'revise' OR length(trim(feedback)) > 0),
    FOREIGN KEY (session_id, suggestion_id) REFERENCES suggestions(session_id, suggestion_id) ON DELETE CASCADE
) STRICT;
