ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT REFERENCES messages(message_id) ON DELETE SET NULL;
ALTER TABLE messages ADD COLUMN saved_before_revision INTEGER CHECK (saved_before_revision IS NULL OR saved_before_revision > 0);
ALTER TABLE messages ADD COLUMN saved_after_revision INTEGER CHECK (saved_after_revision IS NULL OR saved_after_revision > 0);
ALTER TABLE messages ADD COLUMN reply_candidates_json TEXT CHECK (reply_candidates_json IS NULL OR (json_valid(reply_candidates_json) AND json_type(reply_candidates_json) = 'array'));

CREATE TABLE assistant_messages (
    session_id TEXT NOT NULL,
    message_id TEXT PRIMARY KEY,
    user_message_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('reply','question','change_question')),
    mode TEXT NOT NULL CHECK (mode IN ('host','synthetic')),
    bound_state_revision INTEGER NOT NULL CHECK (bound_state_revision > 0),
    bound_draft_revision INTEGER NOT NULL CHECK (bound_draft_revision > 0),
    resume_generation INTEGER NOT NULL CHECK (resume_generation > 0),
    source_refs_json TEXT NOT NULL CHECK (json_valid(source_refs_json) AND json_type(source_refs_json) = 'array'),
    UNIQUE (session_id,message_id),
    FOREIGN KEY (session_id,message_id) REFERENCES messages(session_id,message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,user_message_id) REFERENCES messages(session_id,message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,run_id) REFERENCES runs(session_id,run_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE assistant_suggestion_links (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'),
    PRIMARY KEY (message_id,suggestion_id),
    FOREIGN KEY (session_id,message_id) REFERENCES assistant_messages(session_id,message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,suggestion_id) REFERENCES suggestions(session_id,suggestion_id) ON DELETE CASCADE
) STRICT;
CREATE INDEX assistant_prompt_scope ON assistant_messages(session_id,bound_state_revision,resume_generation,kind);
CREATE INDEX message_reply_revisions ON messages(session_id,saved_before_revision,saved_after_revision);
