# Backend

Python 3.11, FastAPI, and PostgreSQL. Follow the [root setup](../README.md#install-and-run) for local development. Public request examples belong in the [agent guide](../docs/agents.md); `/openapi.json` describes the HTTP contract.

## Code

- `realtime.py` owns board writes, retry receipts, HTTP operations, and WebSocket sync.
- `history.py` reads immutable events and saved states for HTTP and MCP.
- `mcp_server.py` exposes create, read, append, wait, read_revision, and events.
- `routers/pages.py` renders initial HTML; home/help JSON files also produce Markdown and JSON.
- `routing.py` and `security.py` own public-route selection, quotas, HEAD handling, and API versions.
- `database.py` configures storage; `telemetry.py` keeps board keys and content out of request logs.

All writes share the board's PostgreSQL row lock. Browser saves replace the whole document; PUT/PATCH require an expected revision. Append operations preserve concurrent additions. Create/append idempotency keys and browser sequence numbers prevent duplicate retries.

The database trigger records an event in the same transaction as each saved state. Runtime can read history but cannot insert, change, or delete events directly. PATCH records its requested span; whole-document saves infer a replacement span. See [history and recovery](../docs/history.md).

The browser keeps one write in flight and one pending replacement, with at least 120 ms between sends. `DocumentHub` publishes local commits and polls other workers every 250 ms. Current-state subscriptions may coalesce revisions; the event API preserves every saved revision.

## Configuration and limits

`DB_MODE=local` requires a loopback PostgreSQL URL. `DB_MODE=postgres` accepts any PostgreSQL `DATABASE_URL` (`postgresql+pg8000://…`) and verifies TLS for non-loopback hosts unless `DATABASE_TLS=disable`. `DB_MODE=disabled` is for tests. Settings are documented in `.env.example`; the app does not load env files automatically.

The board key grants read/write access, including history. Current text is limited to 5,000 Unicode characters. HTTP requests and WebSocket handshakes share 60/minute and 10/second per IP. Browser writes have a separate 600/minute and 10/second allowance. Each process supports 64 sockets total and 32 per IP; HTTP/MCP waits share 64 total and 4 per IP. Honor Retry-After. Production quota storage failures deny requests.

## Self-hosting

The backend is a plain ASGI app (`python main.py`, or the `Dockerfile`) and needs only PostgreSQL (tests run on 17). Database, proxy, and hostnames are environment variables, so any container host, database provider, and static host or CDN works. Product copy, the frontend's canonical and social-preview tags (`frontend/src/services/page-metadata.ts`), and help text still name bboard.ai; edit those to rebrand.

| Setting | Purpose |
| --- | --- |
| `DB_MODE=postgres`, `DATABASE_URL` | Any PostgreSQL database, as `postgresql+pg8000://USER:PASSWORD@HOST/DB` (or `…@/DB?unix_sock=PATH`). |
| `DATABASE_TLS`, `DATABASE_TLS_CA` | Non-loopback connections verify TLS against the system CAs, or against `DATABASE_TLS_CA` (a PEM file). `DATABASE_TLS=disable` turns TLS off; use it only on a private network. |
| `BBOARD_PRODUCTION` | On by default with `DB_MODE=postgres`. Requires the database, shares rate limits across instances through PostgreSQL, and applies the proxy rules below. Set `0` only for a single-process test install; unrecognized values stop startup. |
| `BBOARD_EDGE_TOKEN` | Shared secret your reverse proxy sends as `X-Bboard-Edge`, together with the caller's address in `X-Bboard-Client-IP`. In production, GET writes (`/write?content=…`, `?append=…`) require it. |
| `BBOARD_TRUSTED_PROXIES` | Comma-separated proxy addresses or CIDRs whose `X-Forwarded-For` (last entry) identifies the caller, for proxies that cannot send the edge token. `*` trusts every peer; use it only when the platform appends the caller's address and the backend is unreachable any other way. |
| `BBOARD_SITE_URL` | Public origin used in generated links, `Link` headers, robots.txt, the sitemap, and the API catalog. |
| `BBOARD_SITE_HOSTS`, `BBOARD_MCP_HOSTS` | Comma-separated hostnames that receive browser pages and that MCP accepts. Site hosts are bare names (ports are ignored); add `:PORT` to an MCP host on a non-default port. |
| `CORS_ORIGINS` | Browser origins allowed to call the API. |
| `PORT` | Listen port. |

Rate limits identify a caller in this order: `X-Bboard-Client-IP` from a request carrying the edge token, then `X-Forwarded-For` from a trusted proxy, then the connecting address. Forwarding headers from anyone else are ignored, so a backend reachable without a proxy cannot be given a spoofed identity. Behind a proxy that you have not configured, every caller shares the proxy's quota.

Your proxy must overwrite `X-Bboard-Edge` and `X-Bboard-Client-IP` on every incoming request, append the caller's address to `X-Forwarded-For`, and never turn HEAD into GET. HEAD and marked previews never write.

The frontend build (`npm --prefix frontend run build`) writes `/app.js`, `/assets/*`, and `/images/*` to `frontend/dist`. Serve those paths from any static host, and send every other path to the backend, which renders the pages. The frontend calls the API on its own origin; set `VITE_API_URL` at build time only if the API lives elsewhere.

## Migrations

Migrations live in `migrations/` and apply in order under a transaction and advisory lock, with checksums. Applied migrations must not change. Local development applies them through `scripts/local_database.py`.

For a hosted database, create the database and a runtime login, then apply migrations as the owner:

```sh
psql "$ADMIN_URL" -c "CREATE DATABASE bboard" -c "CREATE ROLE bboard_runtime LOGIN PASSWORD '…'"
DATABASE_URL=postgresql+pg8000://OWNER:PASSWORD@HOST/bboard backend/.venv/bin/python scripts/migrations.py
```

Then grant the runtime role only what the backend uses, and point the backend's `DATABASE_URL` at that role:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;  -- already the default on PostgreSQL 15+
GRANT CONNECT ON DATABASE bboard TO bboard_runtime;
GRANT USAGE ON SCHEMA app TO bboard_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.ip_rate_limits TO bboard_runtime;
GRANT SELECT, INSERT, UPDATE ON app.boards, app.board_writers TO bboard_runtime;
GRANT SELECT, INSERT ON app.board_appends, app.board_creations TO bboard_runtime;
GRANT SELECT ON app.board_events TO bboard_runtime;
```

Migration 010 adds `app.expire_retry_receipts()`, which the backend calls about once a minute to delete retry receipts older than 7 days. It runs with the owner's rights, so the runtime role needs no `DELETE` on receipt tables. Board history is never deleted.

`verify_runtime_permissions` in `scripts/verify_database.py` checks those grants as the runtime role and rolls back its probe data. Rerunning `scripts/migrations.py` applies only new migrations.
