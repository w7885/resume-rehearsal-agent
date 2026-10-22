ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'local' CHECK (mode IN ('local', 'mock'));
