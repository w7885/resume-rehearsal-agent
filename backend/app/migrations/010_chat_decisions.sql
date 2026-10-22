CREATE TABLE chat_decision_resolutions (
    session_id TEXT NOT NULL,
    user_message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    input_state_revision INTEGER NOT NULL CHECK (input_state_revision > 0),
    resume_generation INTEGER NOT NULL CHECK (resume_generation > 0),
    mode TEXT NOT NULL CHECK (mode IN ('host','synthetic')),
    status TEXT NOT NULL CHECK (status IN ('none','clarify','resolved')),
    request_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json) AND json_type(receipt_json) = 'object'),
    FOREIGN KEY (session_id,user_message_id) REFERENCES messages(session_id,message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,run_id) REFERENCES runs(session_id,run_id) ON DELETE CASCADE
) STRICT;
CREATE INDEX chat_resolution_scope ON chat_decision_resolutions(session_id,input_state_revision,resume_generation);
