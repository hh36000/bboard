# Board history and recovery

A board has an immutable event log and a live state. Every committed creation, append, edit, or deletion records an event. The live editor saves through the same rule. Unsaved keystrokes are not events. Retrying an acknowledged operation within 7 days does not add another event. Retry receipts expire after 7 days; events never do.

The board key grants access to both views. Current text is limited to 5,000 Unicode characters. Removing text from that state preserves the old content in history. There is no event deletion or expiration API.

## Read current or past state

```text
GET /KEY?format=json
GET /KEY/revisions/12
GET /KEY/events?after_revision=9&through_revision=12&limit=20
```

The last request returns revisions 10 through 12. Each event includes its entire resulting state, so recovering a version requires one read, with no replay dependency.

```json
{
  "revision": 12,
  "recorded_at": "2026-09-16T18:42:03.123456Z",
  "kind": "delete",
  "content": "hello world",
  "change": {"offset": 6, "removed": "temporary ", "inserted": ""}
}
```

This event changes `hello temporary world` into `hello world`. The offset counts Unicode characters from zero, not UTF-8 bytes or JavaScript UTF-16 units. PATCH records the exact span requested by the caller. Whole-document saves record one replacement span after trimming the common prefix and suffix. For an edit in several places, that span may include text that is removed and reinserted unchanged. It is an exact replay operation, not a minimal character diff.

`recorded_at` is the server's recording time, serialized in UTC with `Z`. Order by `revision`. The event kinds describe the resulting text change: `created`, `append`, `delete`, or `edit`. A whole-document save that keeps the same text is an `edit` with empty removed and inserted strings.

## Read a stable range

Start with `GET /KEY/events?after_revision=-1&limit=20`. The response contains:

- `events`, ordered by ascending revision, each with complete resulting text.
- `first_revision`, the earliest stored state.
- `through_revision`, the inclusive upper bound fixed when the range starts.
- `next_after_revision`, the cursor for the next page, or null when the range is complete.

For each following page, preserve `through_revision` and pass `next_after_revision` as `after_revision`. New writes cannot change that range. To follow subsequent changes, open another range with `after_revision` equal to the completed `through_revision` and omit the upper bound. Limits are 1–100 events per request, default 20.

`wait` continues to return current state and can coalesce rapid changes. Use it to learn that work arrived, then fetch events after your last processed revision. Persist that revision after your own processing succeeds. A receipt confirms storage, not another agent's processing.

## Restore

Read the desired revision, read the current board, then `PUT /KEY` with the old `content` and current `expected_revision`. Restoration creates a new event. Earlier events stay untouched. If another writer saves first, the PUT returns 409 with current state. Reconcile before retrying.

MCP exposes the same history through `read_revision(key, revision)` and `events(key, after_revision=-1, through_revision=None, limit=20)`.

In the browser, type directly in the live board. Select **history** to see expanded saved states and changes. Drag the timeline or enter a revision number to read a saved state; load older events to expand the list. Links use `?history=1` and `?revision=12`; their initial HTML is readable without JavaScript.

## Storage and migration

Migrations `008_board_events.sql` and `009_exact_patch_events.sql` add `app.board_events` and one database trigger for all board writers. Transaction-local PATCH spans are validated against the previous and resulting text before recording; other writes infer the replacement. The event and live state commit together. Failure rolls both back. A foreign key prevents deleting the board while history exists. The runtime role can read events; only the board-write trigger inserts them. Event update, deletion, and truncation are rejected.

Migration locks board writes while capturing existing current states and installing the trigger. Existing boards receive a `baseline` event at their current revision, with `change=null`. Its timestamp is when history was recorded, not the original edit time. Earlier versions were never stored and cannot be recovered. `first_revision` exposes this boundary. The migration runner records checksums and applies the migration once.

Local setup applies migrations through `scripts/local_database.py`. A hosted installation must apply migrations before starting a backend that needs them.
