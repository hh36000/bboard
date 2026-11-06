"""Public shared text: committed PostgreSQL revisions define last-write-wins."""
from routing import public
from edge import trusted_edge
import asyncio
import anyio
from contextlib import suppress
import json
import hashlib
import logging
import secrets
import re
from urllib.parse import parse_qsl, unquote_plus
from uuid import UUID

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from routers.schemas import BoardCreateResult, BoardContent, BoardAppendResult, BoardWaitResult, CreateInput, AppendInput, UpdateInput, PatchInput, PatchMatchError, RevisionConflict, Error, Message, ERROR_RESPONSES
from routers.pages import HEADERS, CREATE_MESSAGE, canonical_read_path, creation_response, message_response, response_kind, is_site_request, not_found
from security import BOARD_KEY_PATTERN, client_key

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_CONTENT_CHARACTERS = 5000
MAX_HTTP_BODY_BYTES = 65536
MAX_QUERY_BYTES = 8192
MAX_FRAME_BYTES = 400 * 1024
POLL_SECONDS = 0.25
MAX_CONNECTIONS = 64
MAX_CONNECTIONS_PER_IP = 32
IDEMPOTENCY_KEY_PATTERN = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
IDEMPOTENCY_KEY_OPENAPI_PATTERN = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'


def snapshot(row):
    return {'type': 'snapshot', 'content': row['content'], 'revision': row['revision']}


class BoardNotFound(LookupError):
    pass


class BoardRevisionConflict(Exception):
    def __init__(self, value):
        super().__init__('revision_conflict')
        self.value = value


class BoardPatchError(Exception):
    def __init__(self, detail, value):
        super().__init__(detail)
        self.value = value


class DocumentStore:
    def __init__(self, engine, board_id):
        if not BOARD_KEY_PATTERN.fullmatch(board_id):
            raise ValueError('Invalid board key')
        self.engine = engine
        self.board_id = board_id

    def read(self, after=-1):
        if self.engine is None:
            raise RuntimeError('PostgreSQL is required for document sync')
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            row = connection.execute(text('''SELECT content, revision FROM app.boards
                WHERE id = :board AND revision > :after'''), {'board': self.board_id, 'after': after}).mappings().first()
        if row is None and after == -1:
            raise BoardNotFound('Board not found')
        return snapshot(row) if row else None

    def replace(self, content, expected_revision):
        validate_content(content)
        return self._update(expected_revision, lambda row: (content, None))

    def append(self, content, idempotency_key):
        """Append under the browser/API row lock; persist a receipt in the same commit."""
        validate_content(content)
        if not content:
            raise ValueError('content_empty')
        idempotency_key = validate_idempotency_key(idempotency_key)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if self.engine is None:
            raise RuntimeError('PostgreSQL is required for document sync')
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            row = connection.execute(text('SELECT content, revision FROM app.boards WHERE id=:board FOR UPDATE'),
                                     {'board': self.board_id}).mappings().first()
            if row is None:
                raise BoardNotFound('Board not found')
            params = {'board': self.board_id, 'operation': idempotency_key}
            receipt = connection.execute(text('''SELECT content_hash, revision FROM app.board_appends
                WHERE board_id=:board AND operation_id=CAST(:operation AS uuid)'''), params).mappings().first()
            if receipt:
                if receipt['content_hash'] != digest:
                    raise ValueError('idempotency_key_reused_with_different_content')
                return {**snapshot(row), 'appended_revision': receipt['revision'], 'duplicate': True}
            combined = validate_content(row['content'] + content)
            row = connection.execute(text('''UPDATE app.boards SET content=:content,
                revision=revision+1, updated_at=statement_timestamp() WHERE id=:board
                RETURNING content, revision'''), {**params, 'content': combined}).mappings().one()
            connection.execute(text('''INSERT INTO app.board_appends (board_id, operation_id, content_hash, revision)
                VALUES (:board, CAST(:operation AS uuid), :digest, :revision)'''),
                {**params, 'digest': digest, 'revision': row['revision']})
            result = {**snapshot(row), 'appended_revision': row['revision'], 'duplicate': False}
        return result

    def patch(self, old_text, new_text, expected_revision):
        validate_text(old_text)
        validate_text(new_text)

        def replace_span(row):
            content = row['content']
            if not old_text:
                raise BoardPatchError('old_text_empty', snapshot(row))
            start = content.find(old_text)
            if start == -1:
                raise BoardPatchError('old_text_not_found', snapshot(row))
            # Count overlapping occurrences too: "aa" is ambiguous in "aaa".
            if content.find(old_text, start + 1) != -1:
                raise BoardPatchError('old_text_ambiguous', snapshot(row))
            return (content[:start] + new_text + content[start + len(old_text):],
                    {'offset': start, 'removed': old_text, 'inserted': new_text})

        return self._update(expected_revision, replace_span)

    def _update(self, expected_revision, transform):
        validate_revision(expected_revision)
        if self.engine is None:
            raise RuntimeError('PostgreSQL is required for document sync')
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            # Share the browser writer's row lock. Check and update cannot race.
            row = connection.execute(text('SELECT content, revision FROM app.boards WHERE id=:board FOR UPDATE'),
                                     {'board': self.board_id}).mappings().first()
            if row is None:
                raise BoardNotFound('Board not found')
            if row['revision'] != expected_revision:
                raise BoardRevisionConflict(snapshot(row))
            content, change = transform(row)
            validate_content(content)
            if change is not None:
                # Preserve the caller's exact span when repeated boundary text
                # would make an inferred replacement ambiguous. The trigger
                # validates replay before accepting this transaction-local hint.
                connection.execute(text("SELECT set_config('bboard.change', :change, true)"),
                                   {'change': json.dumps(change)})
            row = connection.execute(text('''UPDATE app.boards SET content=:content,
                revision=revision+1, updated_at=statement_timestamp() WHERE id=:board
                RETURNING content, revision'''), {'board': self.board_id, 'content': content}).mappings().one()
            result = snapshot(row)
        return result

    def write(self, client_id, sequence, content):
        # Validate again here: database callers share the same document contract.
        validate_write({'type': 'write', 'client_id': client_id, 'sequence': sequence, 'content': content})
        if self.engine is None:
            raise RuntimeError('PostgreSQL is required for document sync')
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            row = connection.execute(text("SELECT content, revision FROM app.boards WHERE id=:board FOR UPDATE"), {'board': self.board_id}).mappings().first()
            if row is None:
                raise BoardNotFound('Board not found')
            previous = connection.execute(text('''SELECT sequence FROM app.board_writers
                WHERE document_id=:board AND client_id = CAST(:client AS uuid)'''), {'board': self.board_id, 'client': client_id}).scalar_one_or_none()
            # Retried or reordered messages must never become a fresh write.
            if previous is None or sequence > previous:
                row = connection.execute(text('''UPDATE app.boards SET content=:content,
                    revision=revision+1, updated_at=statement_timestamp() WHERE id=:board
                    RETURNING content, revision'''), {'board': self.board_id, 'content': content}).mappings().one()
                connection.execute(text('''INSERT INTO app.board_writers
                    (client_id, document_id, sequence, revision)
                    VALUES (CAST(:client AS uuid), :board, :sequence, :revision)
                    ON CONFLICT (document_id, client_id) DO UPDATE SET sequence=EXCLUDED.sequence,
                    revision=EXCLUDED.revision, updated_at=statement_timestamp()'''),
                    {'board': self.board_id, 'client': client_id, 'sequence': sequence, 'revision': row['revision']})
            result = snapshot(row)
        # The transaction has committed before either ack or broadcast.
        return result


def validate_text(content):
    if not isinstance(content, str):
        raise ValueError('invalid_content')
    try:
        content.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('invalid_content') from None
    if '\x00' in content:
        raise ValueError('invalid_content')
    return content


def validate_content(content):
    validate_text(content)
    if len(content) > MAX_CONTENT_CHARACTERS:
        raise ValueError('content_too_long')
    return content


def validate_revision(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid_expected_revision')
    return value


def validate_idempotency_key(value):
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise ValueError('invalid_idempotency_key')
    key = UUID(value)
    if key.version != 4:
        raise ValueError('invalid_idempotency_key')
    return str(key)


def request_idempotency_key(request):
    values = request.headers.getlist('idempotency-key')
    if not values:
        raise ValueError('idempotency_key_required')
    if len(values) != 1:
        raise ValueError('invalid_idempotency_key')
    return validate_idempotency_key(values[0])


def idempotency_header_parameter():
    return {
        'name': 'Idempotency-Key',
        'in': 'header',
        'required': True,
        'description': 'A new UUIDv4 for this write. Reuse it for retries.',
        'schema': {'type': 'string', 'format': 'uuid', 'pattern': IDEMPOTENCY_KEY_OPENAPI_PATTERN},
    }


def validate_write(message):
    if not isinstance(message, dict) or set(message) != {'type', 'client_id', 'sequence', 'content'}:
        raise ValueError('invalid_message')
    if message['type'] != 'write' or not isinstance(message['client_id'], str):
        raise ValueError('invalid_message')
    try:
        if str(UUID(message['client_id'])) != message['client_id']:
            raise ValueError('invalid_message')
    except (ValueError, AttributeError):
        raise ValueError('invalid_message') from None
    if type(message['sequence']) is not int or not 0 < message['sequence'] <= 9007199254740991:
        raise ValueError('invalid_message')
    validate_content(message['content'])
    return message


class DocumentHub:
    """One poller per active board per process; bounded subscriber queues."""
    def __init__(self, store):
        self.store = store
        self.subscribers = {}
        self.latest = -1
        self.task = None

    def subscribe(self, key):
        if len(self.subscribers) >= MAX_CONNECTIONS or list(self.subscribers.values()).count(key) >= MAX_CONNECTIONS_PER_IP:
            raise ValueError('too_many_connections')
        queue = asyncio.Queue(maxsize=1)
        self.subscribers[queue] = key
        if self.task is None:
            self.task = asyncio.create_task(self.poll())
        return queue

    async def unsubscribe(self, queue):
        self.subscribers.pop(queue, None)
        if not self.subscribers and self.task:
            task, self.task = self.task, None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.latest = -1

    def publish(self, value):
        if value['revision'] <= self.latest:
            return
        self.latest = value['revision']
        for queue in self.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(value)

    async def poll(self):
        try:
            while True:
                value = await run_in_threadpool(self.store.read, self.latest)
                if value:
                    self.publish(value)
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = 'board_not_found' if isinstance(error, BoardNotFound) else 'storage_unavailable'
            if code == 'storage_unavailable':
                logger.warning('Document sync storage unavailable')
            for queue in self.subscribers:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait({'type': 'error', 'code': code})

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


@router.post('/boards', status_code=201, response_model=BoardCreateResult, operation_id='createBlankBoard',
             description='Create a blank board. Idempotency-Key must be a new UUIDv4. Reuse it for retries.',
             responses={**ERROR_RESPONSES, 409: {'description': 'Idempotency key reused with different content.', 'model': Error}},
             openapi_extra={'parameters': [idempotency_header_parameter()]})
@public
def create_board(request: Request):
    try:
        key = request_idempotency_key(request)
        board = create_board_once(request.app.state.database.engine, '', key)
        return JSONResponse(board_url(request, board), status_code=201, headers=GET_WRITE_HEADERS)
    except ValueError as error:
        return write_error(error)
    except Exception:
        logger.warning('Board creation storage unavailable')
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=GET_WRITE_HEADERS)


@router.post('/write', response_model=BoardCreateResult, operation_id='writeNewBoard',
             description='Create a board. Idempotency-Key must be a new UUIDv4. Reuse it with the same content for retries.',
             responses={**ERROR_RESPONSES, 409: {'description': 'Idempotency key reused with different content.', 'model': Error}},
             openapi_extra={'parameters': [idempotency_header_parameter()], 'requestBody': {'required': True, 'content': {
                 'application/json': {'schema': CreateInput.model_json_schema()}}}})
@public
async def write_board(request: Request):
    payload = await read_write_payload(request, {'content'})
    if isinstance(payload, JSONResponse):
        return payload
    try:
        key = request_idempotency_key(request)
        board = await run_in_threadpool(create_board_once, request.app.state.database.engine,
                                       payload['content'], key)
        return JSONResponse(board_url(request, board), headers=GET_WRITE_HEADERS)
    except ValueError as error:
        return write_error(error)
    except Exception:
        logger.warning('Board creation storage unavailable')
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=GET_WRITE_HEADERS)


async def read_write_payload(request: Request, fields):
    """HTTP writers share bounded reads and text validation."""
    headers = {'Cache-Control': 'no-store'}
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        return JSONResponse({'detail': 'Use application/json.'}, status_code=415, headers=headers)
    raw = bytearray()
    try:
        with anyio.fail_after(10):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_HTTP_BODY_BYTES:
                    return JSONResponse({'detail': 'request_too_large'}, status_code=413, headers=headers)
                raw.extend(chunk)
        payload = json.loads(raw)
    except TimeoutError:
        return JSONResponse({'detail': 'request_timeout'}, status_code=408, headers=headers)
    except (ValueError, UnicodeError, RecursionError):
        return JSONResponse({'detail': 'invalid_json'}, status_code=400, headers=headers)
    if not isinstance(payload, dict) or set(payload) != fields:
        detail = 'Required fields: ' + ', '.join(sorted(fields)) + '. No other fields are allowed.'
        return JSONResponse({'detail': detail}, status_code=422, headers=headers)
    try:
        if 'content' in fields:
            validate_content(payload['content'])
        else:
            validate_text(payload['old_text'])
            validate_text(payload['new_text'])
        if 'expected_revision' in fields:
            validate_revision(payload['expected_revision'])
    except ValueError as error:
        return write_error(error, headers=headers)
    return payload


def write_error(error, *, headers=None):
    detail = str(error)
    status = 409 if detail == 'idempotency_key_reused_with_different_content' else 413 if detail == 'content_too_long' else 422
    payload = {'detail': detail}
    if detail == 'content_too_long':
        payload['max_characters'] = MAX_CONTENT_CHARACTERS
    return JSONResponse(payload, status_code=status, headers=GET_WRITE_HEADERS if headers is None else headers)


async def append_to_board(app, key, content, idempotency_key):
    """One committed append and publication path for every transport."""
    value = await run_in_threadpool(DocumentStore(app.state.database.engine, key).append, content, idempotency_key)
    hub = app.state.board_hubs.get(key)
    if hub and not value['duplicate']:
        hub.publish({field: value[field] for field in ('type', 'content', 'revision')})
    return value


@router.post('/{board_id}/append', response_model=BoardAppendResult, operation_id='appendBoard',
    description='Append exact text. Idempotency-Key must be a new UUIDv4. Reuse it with the same content for retries.',
    responses={**ERROR_RESPONSES, 404: {'description': 'Board not found.', 'model': Error},
               409: {'description': 'Idempotency key reused with different content.', 'model': Error}},
    openapi_extra={'parameters': [idempotency_header_parameter()], 'requestBody': {'required': True, 'content': {'application/json': {'schema': AppendInput.model_json_schema()}}}})
@public
async def append_board(request: Request, board_id: str):
    payload = await read_write_payload(request, {'content'})
    if isinstance(payload, JSONResponse):
        return payload
    try:
        key = request_idempotency_key(request)
        value = await append_to_board(request.app, board_id, payload['content'], key)
        return JSONResponse({'key': board_id, **{k: value[k] for k in ('content', 'revision', 'appended_revision', 'duplicate')}}, headers=GET_WRITE_HEADERS)
    except BoardNotFound:
        return JSONResponse({'detail': 'board_not_found'}, status_code=404, headers=GET_WRITE_HEADERS)
    except ValueError as error:
        return write_error(error)
    except Exception:
        logger.warning('Board append storage unavailable')
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=GET_WRITE_HEADERS)


async def wait_for_board(app, key, after_revision, timeout_seconds, identity):
    """HTTP and MCP share subscriptions, latest-state semantics, and capacity."""
    counts = app.state.mcp_waiters
    if sum(counts.values()) >= 64 or counts.get(identity, 0) >= 4:
        raise ValueError('too_many_waiters')
    counts[identity] = counts.get(identity, 0) + 1
    hub = None
    queue = None
    try:
        store = DocumentStore(app.state.database.engine, key)
        hub = app.state.board_hubs.get(key)
        if hub is None:
            hub = DocumentHub(store)
            app.state.board_hubs[key] = hub
        queue = hub.subscribe(identity)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        value = await run_in_threadpool(store.read)
        while value['revision'] <= after_revision:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                update = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                break
            if update.get('type') == 'error':
                raise ValueError(update['code'])
            if update['revision'] > value['revision']:
                value = update
        return {**value, 'changed': value['revision'] > after_revision}
    finally:
        counts[identity] -= 1
        if not counts[identity]:
            del counts[identity]
        if queue is not None:
            with anyio.CancelScope(shield=True):
                await hub.unsubscribe(queue)
        if hub is not None and not hub.subscribers and app.state.board_hubs.get(key) is hub:
            del app.state.board_hubs[key]


async def connected_wait(request, board_id, after_revision, timeout_seconds):
    task = asyncio.create_task(wait_for_board(request.app, board_id, after_revision, timeout_seconds,
                                             client_key(request.scope, request.app.state.production)))
    disconnected = False
    async def watch_disconnect():
        nonlocal disconnected
        while not task.done():
            if await request.is_disconnected():
                disconnected = True
                task.cancel()
                return
            await asyncio.sleep(0.25)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        return await task
    except asyncio.CancelledError:
        if disconnected:
            return None
        raise
    finally:
        watcher.cancel()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        with suppress(asyncio.CancelledError):
            await task


@router.get('/{board_id}/wait', response_model=BoardWaitResult, operation_id='waitForBoard',
    description='Wait for a newer revision, then return the current text. changed=false is a normal timeout. This returns latest state, not message history, and cannot wake a stopped agent.',
    responses={**ERROR_RESPONSES, 404: {'description': 'Board not found.', 'model': Error}})
@public
async def wait_board(request: Request, board_id: str, after_revision: int = Query(ge=0, le=9007199254740991),
                     timeout_seconds: float = Query(default=25, ge=0, le=30)):
    try:
        value = await connected_wait(request, board_id, after_revision, 0 if request.scope.get('bboard.head_request') else timeout_seconds)
        if value is None:
            return Response(status_code=204, headers=GET_WRITE_HEADERS)
        return JSONResponse({'key': board_id, **{k: value[k] for k in ('content', 'revision', 'changed')}}, headers=GET_WRITE_HEADERS)
    except BoardNotFound:
        return JSONResponse({'detail': 'board_not_found'}, status_code=404, headers=GET_WRITE_HEADERS)
    except ValueError as error:
        capacity = str(error) in ('too_many_waiters', 'too_many_connections')
        return JSONResponse({'detail': str(error)}, status_code=429 if capacity else 503,
                            headers={**GET_WRITE_HEADERS, 'Retry-After': '5'})
    except Exception:
        logger.warning('Board wait storage unavailable')
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=GET_WRITE_HEADERS)


@router.put('/{board_id}', response_model=BoardContent, operation_id='updateBoard',
            description='Replace all text on an existing board only when expected_revision matches its current revision. '
                        'Read the board first. HTTP 200 returns the committed content and incremented revision. '
                        'HTTP 409 changes nothing and returns the latest content and revision so the caller can reconcile and retry. '
                        'Up to 5,000 Unicode characters; 64 KiB JSON body. Browser writes remain last committed write wins.',
            responses={**ERROR_RESPONSES, 404: {'description': 'No board exists for this key.', 'model': Error},
                       409: {'description': 'Revision mismatch. Current board state; no update committed.', 'model': RevisionConflict}},
            openapi_extra={'requestBody': {'required': True, 'content': {
                'application/json': {'schema': UpdateInput.model_json_schema()}}}})
@public
async def update_board(request: Request, board_id: str):
    return await update_existing_board(request, board_id, {'content', 'expected_revision'}, 'replace')


@router.patch('/{board_id}', response_model=BoardContent, operation_id='patchBoard',
              description='Replace one literal text span on an existing board. expected_revision is required and checked before matching. '
                          'HTTP 409 returns current key, content, and revision on mismatch. old_text must be nonempty and occur exactly once, '
                          'including overlapping occurrences; otherwise HTTP 422 returns current key, content, and revision. '
                          'Empty new_text deletes the span. No regex, offsets, append field, or replace_all. '
                          'HTTP 200 returns the full committed board with its next revision. Results are capped at 5,000 Unicode characters.',
              responses={**ERROR_RESPONSES, 404: {'description': 'No board exists for this key.', 'model': Error},
                         409: {'description': 'Revision mismatch. Current board state; no update committed.', 'model': RevisionConflict},
                         422: {'description': 'Invalid fields/text, or empty, missing, or ambiguous old_text. Match errors include current board state.',
                               'model': Error | PatchMatchError}},
              openapi_extra={'requestBody': {'required': True, 'content': {
                  'application/json': {'schema': PatchInput.model_json_schema()}}}})
@public
async def patch_board(request: Request, board_id: str):
    return await update_existing_board(request, board_id, {'old_text', 'new_text', 'expected_revision'}, 'patch')


async def update_existing_board(request: Request, board_id: str, fields, operation):
    payload = await read_write_payload(request, fields)
    if isinstance(payload, JSONResponse):
        return payload
    headers = {'Cache-Control': 'no-store'}
    try:
        store = DocumentStore(request.app.state.database.engine, board_id)
        value = await run_in_threadpool(getattr(store, operation), **payload)
    except BoardRevisionConflict as conflict:
        return JSONResponse({'detail': 'revision_conflict', 'key': board_id,
                             'content': conflict.value['content'], 'revision': conflict.value['revision']},
                            status_code=409, headers=headers)
    except BoardPatchError as error:
        return JSONResponse({'detail': str(error), 'key': board_id,
                             'content': error.value['content'], 'revision': error.value['revision']},
                            status_code=422, headers=headers)
    except BoardNotFound:
        return JSONResponse({'detail': 'board_not_found'}, status_code=404, headers=headers)
    except ValueError as error:
        return write_error(error, headers=headers)
    except Exception:
        logger.warning('Board update storage unavailable')
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=headers)
    # Publish on the event loop only after the database transaction commits.
    hub = request.app.state.board_hubs.get(board_id)
    if hub:
        hub.publish(value)
    return JSONResponse({'key': board_id, 'content': value['content'], 'revision': value['revision']}, headers=headers)


def insert_board(connection, content):
    for _ in range(3):
        key = secrets.token_hex(32)
        created = connection.execute(text("INSERT INTO app.boards (id, content) VALUES (:key, :content) ON CONFLICT DO NOTHING RETURNING id"), {'key': key, 'content': content}).scalar_one_or_none()
        if created:
            return {'key': created, 'path': f'/{created}'}
    raise RuntimeError('Board creation failed')


def create_board_once(engine, content, idempotency_key):
    """Commit the random board key and creation receipt in the same transaction."""
    validate_content(content)
    idempotency_key = validate_idempotency_key(idempotency_key)
    if engine is None:
        raise RuntimeError('PostgreSQL is required for board creation')
    params = {'operation': idempotency_key, 'digest': hashlib.sha256(content.encode('utf-8')).hexdigest()}
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '2s'"))
        connection.execute(text("SET LOCAL lock_timeout = '2s'"))
        # Serialize this creation ID across workers before a receipt exists.
        # Hash collisions only serialize unrelated operations; IDs are checked below.
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                           {'identity': 'bboard:create:' + idempotency_key})
        previous = connection.execute(text('''SELECT r.content_hash, b.id, b.content, b.revision
            FROM app.board_creations r JOIN app.boards b ON b.id = r.board_id
            WHERE r.operation_id = CAST(:operation AS uuid)'''), params).mappings().first()
        if previous:
            if previous['content_hash'] != params['digest']:
                raise ValueError('idempotency_key_reused_with_different_content')
            key = previous['id']
            return {'key': key, 'path': '/' + key,
                    'content': previous['content'], 'revision': previous['revision'], 'duplicate': True}
        board = insert_board(connection, content)
        connection.execute(text('''INSERT INTO app.board_creations (operation_id, board_id, content_hash)
            VALUES (CAST(:operation AS uuid), :key, :digest)'''), {**params, 'key': board['key']})
        return {**board, 'content': content, 'revision': 0, 'duplicate': False}


def board_url(request, board):
    """Callers add the public URL from BBOARD_SITE_URL; storage returns only key and path."""
    return {**board, 'url': request.app.state.board_site_url + board['path']}


@router.get('/write', response_model=BoardCreateResult | Message, operation_id='createBoardWithGet',
            description='Without write parameters, read creation instructions without creating anything. '
                        'With content, create a new board. No existing board key is needed. '
                        'Content and op are required together. op must be a new UUIDv4. '
                        'Reuse it with the same content for retries. '
                        'HEAD never creates. Marked previews and prefetches are rejected. '
                        'Use only when the user authorizes creation and the client permits GET writes.',
            responses={**ERROR_RESPONSES,
                       303: {'description': 'Successful HTML creation or identical retry redirects to the frontend board without creation query parameters.',
                             'headers': {'Location': {'schema': {'type': 'string'}}},
                             'content': {'text/plain': {'schema': {'type': 'string'}}}},
                       403: {'description': 'Preview or embedded resource request cannot create.', 'model': Error},
                       405: {'description': 'HEAD cannot create.', 'model': Error},
                       409: {'description': 'Idempotency key already used with different content.', 'model': Error},
                       414: {'description': 'Encoded query exceeds 8 KiB.', 'model': Error},
                       },
            openapi_extra={'parameters': [
                {'name': 'content', 'in': 'query', 'required': False,
                 'description': 'Required to create: URL-encoded UTF-8 text with at least one non-whitespace character. Omit all write parameters for instructions. Encoded query limit: 8 KiB.',
                 'schema': {'type': 'string', 'minLength': 1, 'maxLength': 5000, 'pattern': r'\S'}},
                {'name': 'op', 'in': 'query', 'required': False,
                 'description': 'Required with content. Use a new UUIDv4 and reuse it for retries.',
                 'schema': {'type': 'string', 'format': 'uuid', 'pattern': IDEMPOTENCY_KEY_OPENAPI_PATTERN}},
            ]})
@public(representations=True)
async def create_board_get(request: Request):
    if len(request.scope.get('query_string', b'')) > MAX_QUERY_BYTES:
        return JSONResponse({'detail': 'query_too_long'}, status_code=414, headers=GET_WRITE_HEADERS)
    if not (set(request.query_params) - {'format'}):
        return message_response(request, CREATE_MESSAGE, '/write', interactive=False)
    parsed = prepare_get_write(request, 'content')
    if not isinstance(parsed, dict):
        return parsed
    try:
        board = await run_in_threadpool(create_board_once, request.app.state.database.engine,
                                       parsed['content'], parsed['op'])
    except ValueError as exc:
        return write_error(exc)
    except Exception:
        logger.warning('Board creation storage unavailable')
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=GET_WRITE_HEADERS)
    return creation_response(request, board_url(request, board))


GET_WRITE_HEADERS = {**HEADERS, 'X-Robots-Tag': 'noindex, noarchive'}


def prepare_get_write(request, field):
    """Shared query validation and method protection for every GET mutation."""
    action = 'append' if field == 'append' else 'create'
    def error(detail, status, extra=None):
        return JSONResponse({'detail': detail}, status_code=status, headers={**GET_WRITE_HEADERS, **(extra or {})})
    def invalid(name, reason, message):
        # Diagnostic metadata only. Never log write text, board keys, or UUIDs.
        logger.info('GET %s query rejected: field=%s reason=%s query_bytes=%d',
                    action, name if name in (field, 'op', 'query') else 'unknown', reason,
                    len(request.scope.get('query_string', b'')))
        return JSONResponse({'detail': f'invalid_{action}_query',
                             'errors': [{'field': name, 'reason': reason, 'message': message}]},
                            status_code=422, headers=GET_WRITE_HEADERS)
    if len(request.scope.get('query_string', b'')) > MAX_QUERY_BYTES:
        return error('query_too_long', 414)
    if request.scope.get('bboard.head_request') or request.method != 'GET':
        return error('head_cannot_' + action, 405, {'Allow': 'GET'})
    purpose = ' '.join(request.headers.get(name, '') for name in ('purpose', 'sec-purpose', 'x-purpose', 'x-moz')).lower()
    if any(value in purpose for value in ('prefetch', 'prerender', 'preview')) or \
            request.headers.get('sec-fetch-dest', '') not in ('', 'empty', 'document'):
        return error('prefetch_cannot_' + action, 403)
    try:
        raw = request.scope.get('query_string', b'').decode('ascii')
    except UnicodeError:
        return invalid('query', 'invalid_encoding', 'URL-encode query parameters as UTF-8.')
    params = {}
    for pair in raw.split('&') if raw else []:
        name, separator, value = pair.partition('=')
        try:
            if re.search(r'%(?![0-9a-fA-F]{2})', name):
                raise ValueError()
            name = unquote_plus(name, errors='strict')
        except (ValueError, UnicodeError):
            return invalid('query', 'invalid_encoding', 'A parameter name has invalid percent encoding or UTF-8.')
        if name not in (field, 'op', 'format'):
            return invalid(name, 'unexpected', f'Only {field}, op, and format query parameters are accepted.')
        if not separator:
            return invalid(name, 'missing_equals', f'Use {name}= followed by a URL-encoded value.')
        if name in params:
            return invalid(name, 'repeated', f'Supply {name} exactly once.')
        try:
            if re.search(r'%(?![0-9a-fA-F]{2})', value):
                raise ValueError()
            params[name] = parse_qsl(pair, keep_blank_values=True, strict_parsing=True,
                                     errors='strict', max_num_fields=1)[0][1]
        except (ValueError, UnicodeError):
            return invalid(name, 'invalid_encoding', f'{name} must contain valid percent-encoded UTF-8.')
    for required in (field, 'op'):
        if required not in params:
            return invalid(required, 'missing', f'Required query parameter {required} is missing.')
    if 'op' in params:
        try:
            params['op'] = validate_idempotency_key(params['op'])
        except ValueError:
            return invalid('op', 'invalid_uuid', 'op must be a UUIDv4.')
    try:
        validate_content(params[field])
    except ValueError as exc:
        if str(exc) == 'content_too_long':
            return write_error(exc)
        return invalid(field, 'invalid_content', f'{field} must be valid Unicode without null characters.')
    if field == 'append' and not params[field]:
        return invalid(field, 'empty', 'append must contain at least one character.')
    if field == 'content' and not params[field].strip():
        return invalid(field, 'empty', 'GET creation requires at least one non-whitespace character.')
    kind = response_kind(request)
    if isinstance(kind, Response):
        return kind
    if kind is None:
        return error('unsupported_representation', 406)
    # Some CDNs and static hosts convert HEAD to GET. In production only a
    # method-preserving proxy that sends the BBOARD_EDGE_TOKEN header may
    # deliver GET writes. That proxy must overwrite the header at ingress.
    if request.app.state.production and not trusted_edge(request.headers):
        return error('proxy_method_unavailable', 503, {'Retry-After': '30'})
    return params


@router.get('/{board_id}', response_model=BoardContent | BoardAppendResult, operation_id='readBoard',
            description='Read current board content. With BOTH append and op query parameters, GET instead '
                        'writes an atomic append and returns current content plus duplicate and appended_revision. '
                        'Use for an authorized write when the client permits GET writes. '
                        'op must be a new UUIDv4. Reuse it with the same text for retries. '
                        'No separator is inserted. Results are limited to 5,000 Unicode characters; encoded queries to 8 KiB. '
                        'HEAD never writes. GET writes commit directly on the requested address. '
                        'HTML, Markdown, and JSON return a save receipt.',
            responses={**ERROR_RESPONSES,
                       403: {'description': 'Prefetch or embedded resource request cannot append.', 'model': Error},
                       404: {'description': 'No board exists for this key.', 'model': Error},
                       405: {'description': 'HEAD cannot append.', 'model': Error},
                       409: {'description': 'Idempotency key already used with different content.', 'model': Error},
                       414: {'description': 'Encoded query exceeds 8 KiB.', 'model': Error}},
            openapi_extra={'parameters': [
                {'name': 'append', 'in': 'query', 'required': False,
                 'description': 'Exact text to append. Requires op. URL-encode UTF-8, including newlines, +, &, #, and %.',
                 'schema': {'type': 'string', 'minLength': 1, 'maxLength': 5000}},
                {'name': 'op', 'in': 'query', 'required': False,
                 'description': 'Fresh hyphenated UUID per addition. Requires append. Reuse unchanged for retries.',
                 'schema': {'type': 'string', 'format': 'uuid', 'pattern': IDEMPOTENCY_KEY_OPENAPI_PATTERN}},
            ]})
@router.get('/{board_id}/{suffix:path}', include_in_schema=False)
@public(representations=True)
async def get_board(request: Request, board_id: str):
    """One GET/HEAD contract for both the site renderer and the API."""
    headers = {**HEADERS, 'X-Robots-Tag': 'noindex, noarchive'}
    def error(detail, status, extra_headers=None):
        return JSONResponse({'detail': detail}, status_code=status, headers={**headers, **(extra_headers or {})})

    # Bound parsing as well as the eventual write payload.
    if len(request.scope.get('query_string', b'')) > MAX_QUERY_BYTES:
        return error('query_too_long', 414)
    writing = 'append' in request.query_params or 'op' in request.query_params
    if writing:
        params = prepare_get_write(request, 'append')
        if not isinstance(params, dict):
            return params
        idempotency_key = params['op']
    else:
        kind = response_kind(request)
        if isinstance(kind, Response):
            return kind
        if kind is None:
            return error('unsupported_representation', 406)
        if kind == 'text/html' and ('revision' in request.query_params or request.query_params.get('history') == '1'):
            from history import history_page
            return await run_in_threadpool(history_page, request, board_id)
    try:
        store = DocumentStore(request.app.state.database.engine, board_id)
        value = await append_to_board(request.app, board_id, params['append'], idempotency_key) if writing else await run_in_threadpool(store.read)
    except BoardNotFound:
        return not_found(request) if is_site_request(request.scope) and not writing and kind != 'application/json' else error('board_not_found', 404)
    except ValueError as exc:
        return write_error(exc, headers=headers)
    except Exception:
        logger.warning('Board storage unavailable')
        return error('storage_unavailable', 503, {'Retry-After': '5'})
    message = {'title': 'bboard board', 'key': board_id, **value}
    return message_response(request, message, f'/{board_id}', board=True)


@router.websocket('/ws/boards/{board_id}')
@public
async def board_socket(socket: WebSocket, board_id: str):
    state = socket.app.state
    key = client_key(socket.scope, state.production)
    if not BOARD_KEY_PATTERN.fullmatch(board_id):
        await socket.close(code=1008)
        return
    # Share connection caps across boards; changing keys cannot bypass them.
    hubs = list(state.board_hubs.values())
    if (sum(len(h.subscribers) for h in hubs) >= MAX_CONNECTIONS or
            sum(list(h.subscribers.values()).count(key) for h in hubs) >= MAX_CONNECTIONS_PER_IP):
        await socket.close(code=1013)
        return
    hub = state.board_hubs.get(board_id)
    if hub is None:
        hub = DocumentHub(DocumentStore(state.database.engine, board_id))
        state.board_hubs[board_id] = hub
    try:
        queue = hub.subscribe(key)
    except ValueError:
        await socket.close(code=1013)
        return
    send_lock = asyncio.Lock()

    async def send(message):
        async with send_lock:
            await asyncio.wait_for(socket.send_json(message), timeout=5)

    async def updates():
        while True:
            message = await queue.get()
            await send(message)
            if message['type'] == 'error':
                return

    async def writes():
        while True:
            frame = await socket.receive()
            if frame['type'] == 'websocket.disconnect':
                return
            retry = await run_in_threadpool(state.write_limiter.consume, key)
            if retry:
                await send({'type': 'error', 'code': 'rate_limit_exceeded', 'retry_after': retry})
                return
            raw = frame.get('text')
            if raw is None or len(raw.encode('utf-8')) > MAX_FRAME_BYTES:
                await send({'type': 'error', 'code': 'invalid_message'})
                return
            try:
                message = validate_write(json.loads(raw))
            except (ValueError, RecursionError):
                await send({'type': 'error', 'code': 'invalid_message'})
                return
            value = await run_in_threadpool(hub.store.write, message['client_id'], message['sequence'], message['content'])
            hub.publish(value)
            await send({**value, 'type': 'ack', 'sequence': message['sequence']})

    tasks = []
    try:
        await socket.accept()
        await send(await run_in_threadpool(hub.store.read))
        tasks = [asyncio.create_task(updates()), asyncio.create_task(writes())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except BoardNotFound:
        with suppress(Exception):
            await send({'type': 'error', 'code': 'board_not_found'})
    except (WebSocketDisconnect, OSError):
        pass
    except Exception:
        logger.warning('Document connection stopped before completion')
        with suppress(Exception):
            await send({'type': 'error', 'code': 'storage_unavailable'})
    finally:
        # ASGI shutdown must finish cleanup even when its parent scope is cancelled.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await hub.unsubscribe(queue)
            if not hub.subscribers and state.board_hubs.get(board_id) is hub:
                del state.board_hubs[board_id]
            with suppress(Exception):
                await socket.close(code=1013)
