-- Public shared editor. Separate from private owner-scoped app.documents.
CREATE TABLE app.realtime_documents (
    id text PRIMARY KEY CHECK (id = 'main'),
    content text NOT NULL DEFAULT '' CHECK (octet_length(content) <= 65536),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
INSERT INTO app.realtime_documents (id) VALUES ('main');

-- Durable retry receipts: one row per editor session, rather than per keystroke.
CREATE TABLE app.realtime_document_writers (
    client_id uuid PRIMARY KEY,
    document_id text NOT NULL REFERENCES app.realtime_documents(id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    revision bigint NOT NULL CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
