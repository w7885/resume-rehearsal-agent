CREATE TABLE agent_run_requests (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    root_run_id TEXT NOT NULL,
    current_run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','done','interrupted','failed')),
    outcome TEXT,
    resume_generation INTEGER NOT NULL,
    PRIMARY KEY (session_id,request_id),
    UNIQUE (session_id,user_message_id),
    FOREIGN KEY (session_id,user_message_id) REFERENCES messages(session_id,message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,root_run_id) REFERENCES runs(session_id,run_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,current_run_id) REFERENCES runs(session_id,run_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE agent_stream_events (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY (session_id,request_id,sequence),
    FOREIGN KEY (session_id,request_id) REFERENCES agent_run_requests(session_id,request_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE agent_turn_effects (
    session_id TEXT NOT NULL,
    user_message_id TEXT PRIMARY KEY,
    state_revision INTEGER NOT NULL,
    draft_revision INTEGER NOT NULL,
    resume_generation INTEGER NOT NULL,
    FOREIGN KEY (session_id,user_message_id) REFERENCES messages(session_id,message_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE agent_material_prompts (
    session_id TEXT NOT NULL,
    prompt_message_id TEXT PRIMARY KEY,
    materials_json TEXT NOT NULL CHECK (json_valid(materials_json)),
    FOREIGN KEY (session_id,prompt_message_id) REFERENCES assistant_messages(session_id,message_id) ON DELETE CASCADE
) STRICT;
-- Cost records intentionally outlive session deletion and never reset daily.
CREATE TABLE model_budget_accounts (
    budget_id TEXT PRIMARY KEY CHECK (budget_id = 'project'),
    limit_cents INTEGER NOT NULL CHECK (limit_cents BETWEEN 1 AND 3000),
    spent_cents INTEGER NOT NULL DEFAULT 0 CHECK (spent_cents >= 0),
    imported_legacy_cents INTEGER NOT NULL DEFAULT 0 CHECK (imported_legacy_cents >= 0)
) STRICT;
CREATE TABLE model_budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    reserved_cents INTEGER NOT NULL CHECK (reserved_cents > 0),
    actual_cents INTEGER CHECK (actual_cents IS NULL OR actual_cents >= 0),
    status TEXT NOT NULL CHECK (status IN ('reserved','settled','unknown')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
