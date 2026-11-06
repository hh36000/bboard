CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id text NOT NULL CHECK (length(owner_id) > 0),
    title text NOT NULL DEFAULT 'Untitled',
    content text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_owner_updated_idx
    ON app.documents (owner_id, updated_at DESC);

CREATE OR REPLACE FUNCTION app.touch_document_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS documents_updated_at ON app.documents;
CREATE TRIGGER documents_updated_at BEFORE UPDATE ON app.documents
    FOR EACH ROW EXECUTE FUNCTION app.touch_document_updated_at();
