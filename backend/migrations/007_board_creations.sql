-- Retrying a GET creation must recover the same randomly generated board.
CREATE TABLE app.board_creations (
    operation_id uuid PRIMARY KEY,
    board_id text NOT NULL REFERENCES app.boards(id) ON DELETE CASCADE,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$')
);
