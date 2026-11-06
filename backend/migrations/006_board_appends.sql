-- A retry must not append twice, including after a worker restart or a later edit.
CREATE TABLE app.board_appends (
    board_id text NOT NULL REFERENCES app.boards(id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    revision bigint NOT NULL CHECK (revision > 0),
    PRIMARY KEY (board_id, operation_id)
);
