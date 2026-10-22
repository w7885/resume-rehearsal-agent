-- Original bytes and historical source snapshots remain unchanged.
ALTER TABLE attachments ADD COLUMN parse_report_json TEXT
    CHECK (parse_report_json IS NULL OR json_valid(parse_report_json));
