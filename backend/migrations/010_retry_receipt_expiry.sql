-- Retry receipts only recognize a repeated create, append, or browser write.
-- They expire after 7 days so one board cannot grow storage without bound.
-- Board text and its event history are separate and are never expired.
ALTER TABLE app.board_appends ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT statement_timestamp();
ALTER TABLE app.board_creations ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT statement_timestamp();
CREATE INDEX board_appends_recorded_at ON app.board_appends (recorded_at);
CREATE INDEX board_creations_recorded_at ON app.board_creations (recorded_at);
CREATE INDEX board_writers_updated_at ON app.board_writers (updated_at);

-- Runs with the migration owner's rights so the runtime role needs no DELETE
-- privilege on receipts. It can only remove receipts older than 7 days.
CREATE FUNCTION app.expire_retry_receipts() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
    DELETE FROM app.board_appends WHERE (board_id, operation_id) IN (
        SELECT board_id, operation_id FROM app.board_appends
        WHERE recorded_at < statement_timestamp() - interval '7 days'
        LIMIT 1000 FOR UPDATE SKIP LOCKED);
    DELETE FROM app.board_creations WHERE operation_id IN (
        SELECT operation_id FROM app.board_creations
        WHERE recorded_at < statement_timestamp() - interval '7 days'
        LIMIT 1000 FOR UPDATE SKIP LOCKED);
    DELETE FROM app.board_writers WHERE (document_id, client_id) IN (
        SELECT document_id, client_id FROM app.board_writers
        WHERE updated_at < statement_timestamp() - interval '7 days'
        LIMIT 1000 FOR UPDATE SKIP LOCKED);
$$;
GRANT EXECUTE ON FUNCTION app.expire_retry_receipts() TO PUBLIC;
