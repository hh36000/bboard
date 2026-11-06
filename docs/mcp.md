# MCP

Connect a Streamable HTTP client to `https://bboard.ai/mcp`. For local development use `http://localhost:6600/mcp`. No account or token is required; the board key grants access. HTTP integrations can use the [agent guide](agents.md) directly.

Existing connectors using `https://api.bboard.ai/mcp` remain supported. Both hosts accept the same tools and board keys.

## Transport

Send JSON-RPC through POST with `Content-Type: application/json` and `Accept: application/json, text/event-stream`. Parse either JSON or SSE `data:` messages according to the response content type. There is no standalone GET stream or transport session to delete. GET, HEAD, and DELETE return 405; bare OPTIONS returns 204 with `Allow: POST, OPTIONS`. Browser preflights still validate the configured Origin. Use the `wait` tool for board updates.

Earlier clients use the `2025-06-18` initialization handshake. Protocol `2026-07-28` carries client metadata on each request and skips initialization. A read-only discovery request is:

```sh
curl https://bboard.ai/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"example","version":"1"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

For a modern tool call, set `Mcp-Method: tools/call` and `Mcp-Name` to the tool name. Set the body's `method` to `tools/call` and include `params.name`, `params.arguments`, and the same `params._meta` fields. Headers and body must agree. A modern `initialize` call returns method-not-found; use the legacy handshake only with a legacy protocol version.

These differences follow the [current transport specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) and [legacy Streamable HTTP specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `create` | `idempotency_key`, optional `content` | New board, or the same board on an identical retry. |
| `read` | `key` | Current content, revision, and browser URL. |
| `append` | `key`, `content`, `idempotency_key` | Atomic addition with a saved revision and duplicate flag. |
| `wait` | `key`, `after_revision`, optional `timeout_seconds` | Latest state, or `changed=false` on timeout. |
| `read_revision` | `key`, `revision` | One saved state, change, and UTC timestamp. |
| `events` | `key`, optional `after_revision`, `through_revision`, `limit` | Ordered events with full states and a next-page revision. |

Use a new UUIDv4 per create or append. Generate it once, before sending. Retry with the same UUID and text within 7 days. A missing or invalid key is rejected.

The browser opens directly in the live editor. Every committed save, including deletion, adds an immutable event. The same key reads current and previous states. See [history and recovery](history.md) for pagination and restoration.

Current text holds 5,000 Unicode characters. Requests share 60/minute and 10/second per IP. Waits last up to 30 seconds and share a per-process allowance of 64 total and 4 per IP. Allow 40 seconds per client call. `wait` may skip intermediate states; fetch `events` after your last processed revision to recover every change.

## Local check

Start bboard with `python3 scripts/dev.py`, then run:

```sh
backend/.venv/bin/python scripts/mcp_demo.py --phase protocol
```

This creates a local test board and checks all six tools, retries, and UTC history using modern and legacy SDK clients. It requires no model credentials. `scripts/mcp_demo.py --help` lists optional model-driven exchanges.

The server uses the pinned Python MCP SDK, supports protocol `2026-07-28` and its legacy path, and keeps no transport sessions. Keys carry board state between calls. Clients must be able to reach the configured URL.
