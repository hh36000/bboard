-- Every committed board revision has one permanent event and a complete state.
-- The trigger covers HTTP, MCP, the live editor, and future board writers.
LOCK TABLE app.boards IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE app.board_events (
    board_id text NOT NULL REFERENCES app.boards(id),
    revision bigint NOT NULL CHECK (revision >= 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    kind text NOT NULL CHECK (kind IN ('baseline', 'created', 'append', 'delete', 'edit')),
    content text NOT NULL,
    change jsonb,
    PRIMARY KEY (board_id, revision)
);

-- Earlier text was not retained. This is explicitly a baseline, not a creation.
INSERT INTO app.board_events (board_id, revision, kind, content)
SELECT id, revision, 'baseline', content FROM app.boards;

CREATE FUNCTION app.record_board_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    before_text text := '';
    prefix_length integer := 0;
    suffix_length integer := 0;
    before_length integer;
    after_length integer := char_length(NEW.content);
    removed text;
    inserted text;
    event_kind text := 'created';
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id OR NEW.revision <> OLD.revision + 1 THEN
            RAISE EXCEPTION 'Board updates must advance exactly one revision';
        END IF;
        before_text := OLD.content;
        event_kind := 'edit';
    ELSIF NEW.revision <> 0 THEN
        RAISE EXCEPTION 'New boards must start at revision zero';
    END IF;
    before_length := char_length(before_text);
    -- One exact replacement span, measured in Unicode characters, is sufficient
    -- to replay any edit. Unchanged prefix and suffix are excluded from the span.
    WHILE prefix_length < least(before_length, after_length)
      AND substr(before_text, prefix_length + 1, 1) = substr(NEW.content, prefix_length + 1, 1) LOOP
        prefix_length := prefix_length + 1;
    END LOOP;
    WHILE suffix_length < least(before_length, after_length) - prefix_length
      AND substr(before_text, before_length - suffix_length, 1) = substr(NEW.content, after_length - suffix_length, 1) LOOP
        suffix_length := suffix_length + 1;
    END LOOP;
    removed := substr(before_text, prefix_length + 1, before_length - prefix_length - suffix_length);
    inserted := substr(NEW.content, prefix_length + 1, after_length - prefix_length - suffix_length);
    IF TG_OP = 'UPDATE' THEN
        IF removed <> '' AND inserted = '' THEN event_kind := 'delete';
        ELSIF removed = '' AND inserted <> '' AND prefix_length = before_length THEN event_kind := 'append';
        END IF;
    END IF;
    INSERT INTO app.board_events (board_id, revision, kind, content, change)
    VALUES (NEW.id, NEW.revision, event_kind, NEW.content,
        jsonb_build_object('offset', prefix_length, 'removed', removed, 'inserted', inserted));
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION app.record_board_event() FROM PUBLIC;
CREATE TRIGGER board_event_after_write AFTER INSERT OR UPDATE ON app.boards
FOR EACH ROW EXECUTE FUNCTION app.record_board_event();

-- Runtime receives SELECT only. This also catches accidental maintenance DML.
CREATE FUNCTION app.reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'Board events are append-only';
END;
$$;
CREATE TRIGGER board_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON app.board_events
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_event_mutation();
