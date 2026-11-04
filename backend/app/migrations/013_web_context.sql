-- Web permission has its own revision; it must not invalidate resume snapshots.
ALTER TABLE sessions ADD COLUMN web_enabled INTEGER NOT NULL DEFAULT 0 CHECK (web_enabled IN (0, 1));
ALTER TABLE sessions ADD COLUMN web_settings_revision INTEGER NOT NULL DEFAULT 1 CHECK (web_settings_revision > 0);

CREATE TABLE web_requests (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    resume_generation INTEGER NOT NULL CHECK (resume_generation > 0),
    web_settings_revision INTEGER NOT NULL CHECK (web_settings_revision > 0),
    target_key TEXT NOT NULL CHECK (length(trim(target_key)) > 0),
    operation TEXT NOT NULL CHECK (operation IN ('search', 'fetch')),
    provider TEXT NOT NULL CHECK (provider = 'volcengine'),
    request_hash TEXT NOT NULL,
    input_json TEXT NOT NULL CHECK (json_valid(input_json) AND json_type(input_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed', 'cancelled', 'unknown')),
    error_code TEXT,
    usage_json TEXT CHECK (usage_json IS NULL OR json_valid(usage_json)),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (session_id, request_id),
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE external_references (
    reference_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    url TEXT NOT NULL CHECK (length(trim(url)) > 0),
    content TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    match_status TEXT NOT NULL CHECK (match_status IN ('matched', 'unconfirmed', 'mismatch')),
    hiring_status TEXT NOT NULL CHECK (hiring_status IN ('unconfirmed', 'conflict', 'closed', 'open')),
    content_status TEXT NOT NULL CHECK (content_status IN ('sufficient', 'partial', 'unavailable')),
    UNIQUE (session_id, reference_id),
    FOREIGN KEY (session_id, request_id) REFERENCES web_requests(session_id, request_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX web_requests_scope ON web_requests(session_id, run_id, web_settings_revision, target_key);
CREATE INDEX external_references_request ON external_references(session_id, request_id);

-- Preserve exactly what was fetched. New readings create new reference records.
CREATE TRIGGER external_references_immutable BEFORE UPDATE ON external_references
BEGIN
    SELECT RAISE(ABORT, 'external reference snapshots are immutable');
END;
