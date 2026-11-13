CREATE TABLE user_feedback (
    id UUID PRIMARY KEY,
    submitter_id UUID NOT NULL,
    submitter_email TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('bug', 'suggestion', 'other')),
    description TEXT NOT NULL CHECK (char_length(description) BETWEEN 1 AND 2000),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'resolved')),
    note TEXT NOT NULL DEFAULT '' CHECK (char_length(note) <= 2000),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX feedback_newest ON user_feedback (created_at DESC, id DESC);
CREATE INDEX feedback_status_newest ON user_feedback (status, created_at DESC, id DESC);

CREATE TABLE feedback_history (
    feedback_id UUID NOT NULL REFERENCES user_feedback(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    actor_id UUID NOT NULL,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    note TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (feedback_id, revision)
);
REVOKE ALL ON TABLE user_feedback, feedback_history FROM PUBLIC;
