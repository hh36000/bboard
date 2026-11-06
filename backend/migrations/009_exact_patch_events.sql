-- Preserve supplied PATCH spans; validate all hints against before/after text.
CREATE OR REPLACE FUNCTION app.record_board_event() RETURNS trigger
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
    supplied_change jsonb := nullif(current_setting('bboard.change', true), '')::jsonb;
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
    -- A PATCH carries an exact span; preserve it instead of guessing which
    -- repeated boundary characters the caller intended to remove.
    PERFORM set_config('bboard.change', '', true);
    IF supplied_change IS NOT NULL THEN
        IF TG_OP <> 'UPDATE' OR jsonb_typeof(supplied_change) <> 'object'
           OR jsonb_typeof(supplied_change->'offset') IS DISTINCT FROM 'number'
           OR (supplied_change->>'offset') !~ '^[0-9]+$'
           OR jsonb_typeof(supplied_change->'removed') IS DISTINCT FROM 'string'
           OR jsonb_typeof(supplied_change->'inserted') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid board change';
        END IF;
        prefix_length := (supplied_change->>'offset')::integer;
        removed := supplied_change->>'removed';
        inserted := supplied_change->>'inserted';
        IF prefix_length > before_length
           OR substr(before_text, prefix_length + 1, char_length(removed)) <> removed
           OR substr(before_text, 1, prefix_length) || inserted || substr(before_text, prefix_length + char_length(removed) + 1) <> NEW.content THEN
            RAISE EXCEPTION 'Board change must reproduce the saved state';
        END IF;
    ELSE
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
    END IF;
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
