ALTER TABLE attachments ADD COLUMN reading_order INTEGER CHECK (reading_order IS NULL OR reading_order >= 0);
ALTER TABLE attachments ADD COLUMN recognition_report_json TEXT CHECK (recognition_report_json IS NULL OR json_valid(recognition_report_json));

CREATE TABLE recognition_results (
    result_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number BETWEEN 1 AND 40),
    original_hash TEXT NOT NULL,
    render_version TEXT NOT NULL,
    recognition_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    resume_generation INTEGER NOT NULL CHECK (resume_generation > 0),
    status TEXT NOT NULL CHECK (status IN ('success','partial','failed')),
    text TEXT NOT NULL,
    proposed_role TEXT NOT NULL CHECK (proposed_role IN ('resume','jd','supplement','unknown')),
    error_message TEXT NOT NULL,
    source_id TEXT,
    source_revision INTEGER,
    mode TEXT NOT NULL CHECK (mode IN ('host','synthetic')),
    preserved_correction INTEGER NOT NULL DEFAULT 0 CHECK (preserved_correction IN (0,1)),
    CHECK ((status = 'failed' AND text = '' AND length(trim(error_message)) > 0 AND source_id IS NULL AND source_revision IS NULL)
        OR (status IN ('success','partial') AND length(trim(text)) > 0 AND source_id IS NOT NULL AND source_revision IS NOT NULL)),
    FOREIGN KEY (session_id, attachment_id) REFERENCES attachments(session_id, attachment_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, source_id, source_revision) REFERENCES source_blocks(session_id, source_id, source_revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX recognition_lookup ON recognition_results(session_id,attachment_id,page_number,original_hash,render_version,recognition_version);

CREATE TABLE recognition_retry_requests (
    retry_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    resume_generation INTEGER NOT NULL,
    pages_json TEXT NOT NULL CHECK (json_valid(pages_json) AND json_type(pages_json) = 'array'),
    completed_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(completed_json) AND json_type(completed_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','failed')),
    FOREIGN KEY (session_id,attachment_id) REFERENCES attachments(session_id,attachment_id) ON DELETE CASCADE
) STRICT;
