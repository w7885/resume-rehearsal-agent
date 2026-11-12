CREATE TABLE account_profiles (
    user_id UUID PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (char_length(display_name) <= 30)
);
REVOKE ALL ON TABLE account_profiles FROM PUBLIC;
