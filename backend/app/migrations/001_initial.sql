CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY CHECK (length(trim(session_id)) > 0),
    direction TEXT NOT NULL CHECK (length(trim(direction)) > 0),
    status TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'running', 'waiting_user', 'interrupted', 'failed', 'budget_blocked')),
    state_revision INTEGER NOT NULL DEFAULT 1 CHECK (state_revision > 0),
    draft_revision INTEGER NOT NULL DEFAULT 1 CHECK (draft_revision > 0),
    current_resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (current_resume_generation > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

CREATE TABLE attachments (
    attachment_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    media_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    original_data BLOB NOT NULL,
    original_order INTEGER NOT NULL CHECK (original_order >= 0),
    role TEXT NOT NULL DEFAULT 'unknown' CHECK (role IN ('resume', 'jd', 'supplement', 'unknown')),
    parse_status TEXT NOT NULL DEFAULT 'pending' CHECK (parse_status IN ('pending', 'parsed', 'partial', 'failed')),
    use_status TEXT NOT NULL DEFAULT 'pending' CHECK (use_status IN ('pending', 'active', 'history')),
    resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (resume_generation > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, attachment_id)
) STRICT;

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'supplement', 'correction', 'skip')),
    use_status TEXT NOT NULL DEFAULT 'active' CHECK (use_status IN ('pending', 'active', 'history')),
    resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (resume_generation > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, message_id)
) STRICT;

CREATE TABLE source_blocks (
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision > 0),
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    attachment_id TEXT,
    message_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('resume', 'jd', 'supplement', 'unknown')),
    text TEXT NOT NULL,
    origin_json TEXT NOT NULL CHECK (json_valid(origin_json) AND json_type(origin_json) = 'object'),
    page_number INTEGER CHECK (page_number > 0),
    line_start INTEGER CHECK (line_start > 0),
    line_end INTEGER CHECK (line_end >= line_start),
    use_status TEXT NOT NULL DEFAULT 'pending' CHECK (use_status IN ('pending', 'active', 'history')),
    resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (resume_generation > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (source_id, source_revision),
    UNIQUE (session_id, source_id, source_revision),
    CHECK ((attachment_id IS NOT NULL) + (message_id IS NOT NULL) = 1),
    CHECK ((line_start IS NULL) = (line_end IS NULL)),
    FOREIGN KEY (session_id, attachment_id) REFERENCES attachments(session_id, attachment_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, message_id) REFERENCES messages(session_id, message_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE draft_blocks (
    block_id TEXT NOT NULL,
    block_revision INTEGER NOT NULL CHECK (block_revision > 0),
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    markdown TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (resume_generation > 0),
    write_kind TEXT NOT NULL CHECK (write_kind IN ('import', 'accepted_suggestion', 'user_edit', 'replacement')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (block_id, block_revision),
    UNIQUE (session_id, block_id, block_revision)
) STRICT;

CREATE TABLE draft_sources (
    session_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    block_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    PRIMARY KEY (block_id, block_revision, source_id, source_revision),
    FOREIGN KEY (session_id, block_id, block_revision) REFERENCES draft_blocks(session_id, block_id, block_revision) ON DELETE CASCADE,
    FOREIGN KEY (session_id, source_id, source_revision) REFERENCES source_blocks(session_id, source_id, source_revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE suggestions (
    suggestion_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('replace', 'insert_after', 'delete')),
    target_block_id TEXT NOT NULL,
    expected_block_revision INTEGER NOT NULL CHECK (expected_block_revision > 0),
    expected_state_revision INTEGER NOT NULL CHECK (expected_state_revision > 0),
    before_text TEXT NOT NULL,
    after_text TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    gaps_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(gaps_json) AND json_type(gaps_json) = 'array'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'invalidated')),
    resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (resume_generation > 0),
    supersedes_suggestion_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at TEXT,
    UNIQUE (session_id, suggestion_id),
    CHECK (operation != 'delete' OR after_text = ''),
    FOREIGN KEY (session_id, target_block_id, expected_block_revision) REFERENCES draft_blocks(session_id, block_id, block_revision) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, supersedes_suggestion_id) REFERENCES suggestions(session_id, suggestion_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE suggestion_sources (
    session_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    PRIMARY KEY (suggestion_id, source_id, source_revision),
    FOREIGN KEY (session_id, suggestion_id) REFERENCES suggestions(session_id, suggestion_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, source_id, source_revision) REFERENCES source_blocks(session_id, source_id, source_revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE issues (
    issue_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'skipped')),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    resume_generation INTEGER NOT NULL DEFAULT 1 CHECK (resume_generation > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, issue_id)
) STRICT;

CREATE TABLE issue_sources (
    session_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    PRIMARY KEY (issue_id, source_id, source_revision),
    FOREIGN KEY (session_id, issue_id) REFERENCES issues(session_id, issue_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, source_id, source_revision) REFERENCES source_blocks(session_id, source_id, source_revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE issue_blocks (
    session_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    block_revision INTEGER NOT NULL,
    PRIMARY KEY (issue_id, block_id, block_revision),
    FOREIGN KEY (session_id, issue_id) REFERENCES issues(session_id, issue_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, block_id, block_revision) REFERENCES draft_blocks(session_id, block_id, block_revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    input_state_revision INTEGER NOT NULL CHECK (input_state_revision > 0),
    resume_generation INTEGER NOT NULL CHECK (resume_generation > 0),
    status TEXT NOT NULL CHECK (status IN ('idle', 'running', 'waiting_user', 'completed', 'interrupted', 'failed', 'budget_blocked')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, run_id),
    UNIQUE (session_id, generation)
) STRICT;

CREATE TABLE run_events (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    type TEXT NOT NULL CHECK (type IN ('progress', 'tool_progress', 'diagnosis', 'question', 'suggestion', 'conflict', 'done', 'error')),
    mode TEXT NOT NULL CHECK (mode IN ('mock', 'live')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (run_id, sequence),
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE draft_replacements (
    replacement_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    previous_generation INTEGER NOT NULL CHECK (previous_generation > 0),
    next_generation INTEGER NOT NULL CHECK (next_generation = previous_generation + 1),
    before_markdown TEXT NOT NULL,
    after_markdown TEXT NOT NULL,
    previous_scope_json TEXT NOT NULL CHECK (json_valid(previous_scope_json) AND json_type(previous_scope_json) = 'array'),
    next_scope_json TEXT NOT NULL CHECK (json_valid(next_scope_json) AND json_type(next_scope_json) = 'array'),
    authorization_text TEXT NOT NULL CHECK (length(trim(authorization_text)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, request_id)
) STRICT;

CREATE INDEX sessions_updated ON sessions(updated_at);
CREATE INDEX messages_session_time ON messages(session_id, created_at, message_id);
CREATE INDEX attachments_session_order ON attachments(session_id, original_order);
CREATE INDEX attachments_content_hash ON attachments(session_id, content_hash);
CREATE INDEX sources_active_role ON source_blocks(session_id, use_status, role, resume_generation);
CREATE UNIQUE INDEX draft_current_position ON draft_blocks(session_id, position) WHERE is_current = 1;
CREATE UNIQUE INDEX draft_current_block ON draft_blocks(block_id) WHERE is_current = 1;
CREATE INDEX suggestions_session_status ON suggestions(session_id, status);
CREATE INDEX issues_session_status ON issues(session_id, is_current, status);
CREATE UNIQUE INDEX runs_one_active ON runs(session_id) WHERE status = 'running';

CREATE TRIGGER sources_same_owner BEFORE INSERT ON source_blocks
WHEN EXISTS (SELECT 1 FROM source_blocks WHERE source_id = NEW.source_id AND session_id != NEW.session_id)
BEGIN
    SELECT RAISE(ABORT, 'source identity belongs to another session');
END;

CREATE TRIGGER sources_immutable BEFORE UPDATE OF source_id, source_revision, session_id, attachment_id, message_id, role, text, origin_json, page_number, line_start, line_end ON source_blocks
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable; create a new revision');
END;

CREATE TRIGGER draft_snapshots_immutable BEFORE UPDATE OF block_id, block_revision, session_id, markdown ON draft_blocks
BEGIN
    SELECT RAISE(ABORT, 'draft snapshots are immutable; create a new revision');
END;

CREATE TRIGGER draft_same_owner BEFORE INSERT ON draft_blocks
WHEN EXISTS (SELECT 1 FROM draft_blocks WHERE block_id = NEW.block_id AND session_id != NEW.session_id)
BEGIN
    SELECT RAISE(ABORT, 'draft identity belongs to another session');
END;
