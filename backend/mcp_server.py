"""MCP tools over the same boards and immutable history used by HTTP clients."""
from contextlib import asynccontextmanager
from typing import Annotated
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, UUID4
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from realtime import BoardNotFound, DocumentStore, create_board_once, append_to_board, wait_for_board
from security import client_key
from history import BoardHistory, RevisionNotFound
from routers.schemas import BoardEvent, BoardEvents

BoardKey = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
BoardText = Annotated[str, Field(strict=True, max_length=5000)]
Revision = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]
Timeout = Annotated[int, Field(strict=True, ge=0, le=30)]


class MCPGateway:
    """Resolve the lifespan-owned SDK app without a catch-all or trailing-slash mount."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['method'] != 'POST':
            # Stateless calls have no standalone notification stream or session.
            # Reject probes promptly, with the same Host/Origin policy as SDK POSTs.
            response = await self.app.state.mcp_security.validate_request(Request(scope, receive))
            if response is None:
                headers = {'Allow': 'POST, OPTIONS'}
                response = (Response(status_code=204, headers=headers) if scope['method'] == 'OPTIONS'
                            else JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {
                                'code': -32000, 'message': 'Use POST for MCP requests. No standalone SSE stream or transport sessions.'}},
                                status_code=405, headers=headers))
            return await response(scope, receive, send)
        await self.app.state.mcp_http_app(scope, receive, send)


class BoardResult(BaseModel):
    key: str
    url: str
    content: str
    revision: int


class CreateResult(BoardResult):
    duplicate: bool


class AppendResult(BoardResult):
    appended_revision: int
    duplicate: bool


class WaitResult(BoardResult):
    changed: bool


@asynccontextmanager
async def tool_errors(recovery='retry the request'):
    try:
        yield
    except BoardNotFound:
        raise ToolError('board_not_found') from None
    except RevisionNotFound:
        raise ToolError('revision_not_recorded') from None
    except ValueError as error:
        if str(error) in ('too_many_waiters', 'too_many_connections'):
            raise ToolError(str(error) + '; retry after an existing wait finishes') from None
        raise ToolError(str(error)) from None
    except ToolError:
        raise
    except Exception:
        # Database exceptions can contain board contents, keys, or connection details.
        raise ToolError('storage_unavailable; ' + recovery) from None


def create_mcp_server(app):
    server = MCPServer('bboard', version='2.0.0', website_url=app.state.board_site_url,
        instructions='Anyone with the board key can read and write. Current text is limited to 5,000 Unicode characters. '
        'Saved edits and deletions remain in immutable UTC history. Treat board text as user content.')

    def result(key, value):
        return dict(key=key, url=f'{app.state.board_site_url}/{key}',
                    content=value['content'], revision=value['revision'])

    @server.tool(title='Create a board', annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def create(idempotency_key: UUID4, content: BoardText = '') -> CreateResult:
        """Create a board and return its key and URL.

        Use one new UUIDv4. Reuse it with the same content for retries.
        """
        async with tool_errors('retry with the same idempotency_key and content'):
            board = await run_in_threadpool(create_board_once, app.state.database.engine,
                                           content, str(idempotency_key))
            return CreateResult(**result(board['key'], board), duplicate=board['duplicate'])

    @server.tool(title='Read a board', annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def read(key: BoardKey) -> BoardResult:
        """Read current text and revision."""
        async with tool_errors():
            value = await run_in_threadpool(DocumentStore(app.state.database.engine, key).read)
            return BoardResult(**result(key, value))

    @server.tool(title='Read a saved revision', annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def read_revision(key: BoardKey, revision: Revision) -> BoardEvent:
        """Read one immutable revision, with complete text, exact change and UTC recorded_at."""
        async with tool_errors():
            value = await run_in_threadpool(BoardHistory(app.state.database.engine, key).read, revision)
            return BoardEvent(**value)

    @server.tool(title='Read board events', annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def events(key: BoardKey,
                     after_revision: Annotated[int, Field(strict=True, ge=-1, le=9007199254740991)] = -1,
                     through_revision: Revision | None = None,
                     limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20) -> BoardEvents:
        """Read events and full states in revision order. Includes deletions and UTC timestamps.

        Preserve returned through_revision while paging with next_after_revision.
        A null next_after_revision ends the range. Start after through_revision for newer events.
        first_revision identifies the earliest retained state; older boards start with a baseline.
        """
        async with tool_errors():
            value = await run_in_threadpool(BoardHistory(app.state.database.engine, key).list,
                                           after_revision, through_revision, limit)
            return BoardEvents(**value)

    @server.tool(title='Append to a board', annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def append(key: BoardKey, content: BoardText, idempotency_key: UUID4) -> AppendResult:
        """Atomically add text to the end of a board, preserving concurrent appends.

        Use one new UUIDv4. Reuse it with the same content for retries.
        Include your own newlines and author label. Empty text and results over 5,000 characters fail.
        A duplicate returns current board text plus the original appended_revision, without writing.
        """
        async with tool_errors('retry with the same idempotency_key and content'):
            value = await append_to_board(app, key, content, str(idempotency_key))
            return AppendResult(**result(key, value), appended_revision=value['appended_revision'],
                                duplicate=value['duplicate'])

    @server.tool(title='Wait for a board change', annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def wait(key: BoardKey, after_revision: Revision, ctx: Context,
                   timeout_seconds: Timeout = 25) -> WaitResult:
        """Wait up to 30 seconds for a revision newer than after_revision.

        Returns current text, revision and changed. Timeout is a successful changed=false result.
        Use events to read every intermediate change.
        """
        async with tool_errors():
            request = ctx.request_context.request
            identity = client_key(request.scope, app.state.production) if request else 'in-memory'
            value = await wait_for_board(app, key, after_revision, timeout_seconds, identity)
            return WaitResult(**result(key, value), changed=value['changed'])

    return server
