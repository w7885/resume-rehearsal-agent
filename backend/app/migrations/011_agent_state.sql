-- Preserve B's already-published 004_agent_state.sql and its existing rows.
-- The initializer first bridges the exact known B4 structure to A4.
CREATE TABLE IF NOT EXISTS agent_state (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('session', 'experience', 'run', 'budget')),
    key TEXT NOT NULL CHECK (length(key) > 0),
    doc_json TEXT NOT NULL CHECK (json_valid(doc_json) AND json_type(doc_json) = 'object'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (session_id, scope, key)
) STRICT;
