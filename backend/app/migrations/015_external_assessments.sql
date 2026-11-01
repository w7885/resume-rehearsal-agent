-- Assessments and citation links never become personal source_refs.
CREATE TABLE external_assessments (
    reference_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    goal_hash TEXT NOT NULL,
    target_key TEXT NOT NULL,
    web_settings_revision INTEGER NOT NULL CHECK (web_settings_revision > 0),
    resume_generation INTEGER NOT NULL CHECK (resume_generation > 0),
    purpose TEXT NOT NULL CHECK (purpose IN ('job_requirements','company_background','terminology')),
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted','needs_body','unconfirmed','mismatch','closed','conflict')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
    assessed_at TEXT NOT NULL,
    UNIQUE (session_id,reference_id),
    FOREIGN KEY (session_id,reference_id) REFERENCES external_references(session_id,reference_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE suggestion_external_references (
    session_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    PRIMARY KEY (suggestion_id,reference_id),
    FOREIGN KEY (session_id,suggestion_id) REFERENCES suggestions(session_id,suggestion_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,reference_id) REFERENCES external_assessments(session_id,reference_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE assistant_external_references (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    PRIMARY KEY (message_id,reference_id),
    FOREIGN KEY (session_id,message_id) REFERENCES assistant_messages(session_id,message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id,reference_id) REFERENCES external_assessments(session_id,reference_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX external_assessments_goal ON external_assessments(session_id,goal_hash,web_settings_revision,purpose,verdict);
CREATE TRIGGER external_assessments_immutable BEFORE UPDATE ON external_assessments
BEGIN
    SELECT RAISE(ABORT, 'external assessments are immutable');
END;
