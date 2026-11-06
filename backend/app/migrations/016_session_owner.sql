-- Existing local sessions remain unclaimed. Never assign them to the first login.
ALTER TABLE sessions ADD COLUMN owner_user_id TEXT;
CREATE INDEX sessions_owner_updated ON sessions(owner_user_id, updated_at);
