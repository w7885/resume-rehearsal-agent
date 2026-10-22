ALTER TABLE sessions ADD COLUMN direction_status TEXT NOT NULL DEFAULT 'provided' CHECK (direction_status IN ('pending', 'provided'));
ALTER TABLE sessions ADD COLUMN chat_title TEXT;
ALTER TABLE messages ADD COLUMN input_channel TEXT NOT NULL DEFAULT 'legacy' CHECK (input_channel IN ('legacy', 'chat'));

CREATE TABLE chat_message_attachments (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (message_id, attachment_id),
    UNIQUE (message_id, position),
    FOREIGN KEY (session_id, message_id) REFERENCES messages(session_id, message_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, attachment_id) REFERENCES attachments(session_id, attachment_id) ON DELETE CASCADE
) STRICT;
