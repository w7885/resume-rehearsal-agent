ALTER TABLE attachments ADD COLUMN page_report_json TEXT
    CHECK (page_report_json IS NULL OR json_valid(page_report_json));

CREATE TABLE material_pages (
    session_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    render_version TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'failed')),
    image_data BLOB,
    width INTEGER,
    height INTEGER,
    error_code TEXT,
    error_message TEXT,
    CHECK ((status = 'ready' AND image_data IS NOT NULL AND length(image_data) > 0 AND width IS NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0 AND error_code IS NULL)
        OR (status = 'failed' AND image_data IS NULL AND width IS NULL AND height IS NULL AND error_code IS NOT NULL)),
    PRIMARY KEY (session_id, attachment_id, page_number, render_version),
    FOREIGN KEY (session_id, attachment_id) REFERENCES attachments(session_id, attachment_id) ON DELETE CASCADE
) STRICT;
