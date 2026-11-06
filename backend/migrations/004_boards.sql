-- Keyed boards are independent of the existing shared home document.
CREATE TABLE app.boards (
    id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
    content text NOT NULL DEFAULT '' CHECK (octet_length(content) <= 65536),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE TABLE app.board_writers (
    document_id text NOT NULL REFERENCES app.boards(id),
    client_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    revision bigint NOT NULL CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (document_id, client_id)
);
