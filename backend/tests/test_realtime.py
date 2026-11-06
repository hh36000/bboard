"""Offline protocol tests and optional isolated PostgreSQL integration."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from starlette.websockets import WebSocketDisconnect

from main import app
from realtime import DocumentHub, DocumentStore, validate_write


BOARD_ID = 'a' * 64
BOARD_SOCKET = '/ws/boards/' + BOARD_ID


def write(content='hello', sequence=1, client=None):
    return {'type': 'write', 'client_id': client or str(uuid4()), 'sequence': sequence, 'content': content}


class FakeStore:
    def __init__(self):
        self.value = {'type': 'snapshot', 'content': '', 'revision': 0}
        self.receipts = {}
        self.lock = threading.Lock()

    def read(self, after=-1):
        with self.lock:
            return self.value.copy() if self.value['revision'] > after else None

    def write(self, client, sequence, content):
        with self.lock:
            if sequence > self.receipts.get(client, 0):
                self.receipts[client] = sequence
                self.value = {'type': 'snapshot', 'content': content, 'revision': self.value['revision'] + 1}
            return self.value.copy()


def receive_until(socket, predicate):
    for _ in range(12):
        value = socket.receive_json()
        if predicate(value):
            return value
    raise AssertionError('Expected message was not received')


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_public_socket_loads_saves_and_broadcasts(self):
        with TestClient(app) as client:
            store = FakeStore()
            app.state.board_hubs[BOARD_ID] = DocumentHub(store)
            with client.websocket_connect(BOARD_SOCKET, headers={'Origin': 'http://localhost:6600'}) as first, \
                 client.websocket_connect(BOARD_SOCKET) as second:
                self.assertEqual(first.receive_json()['content'], '')
                self.assertEqual(second.receive_json()['revision'], 0)
                message = write('shared text')
                first.send_json(message)
                ack = receive_until(first, lambda value: value['type'] == 'ack')
                self.assertEqual(ack['sequence'], 1)
                self.assertEqual(store.read()['content'], 'shared text')
                remote = receive_until(second, lambda value: value.get('revision') == 1)
                self.assertEqual(remote['content'], 'shared text')

    def test_origin_and_path_exceptions_are_exact(self):
        with TestClient(app) as client:
            for path, headers in [(BOARD_SOCKET, {'Origin': 'https://unrelated.example'}),
                                  ('/ws/document', {}), ('/ws/boards/main', {}),
                                  ('/ws/document/', {}), ('/ws/other', {}), ('/health', {})]:
                with self.subTest(path=path), self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(path, headers=headers):
                        pass
            self.assertEqual(client.get('/ws/document').status_code, 401)

    def test_rate_limit_blocks_messages_before_writes(self):
        with TestClient(app) as client:
            store = FakeStore()
            app.state.board_hubs[BOARD_ID] = DocumentHub(store)
            app.state.write_limiter = MagicMock(consume=MagicMock(return_value=2))
            with client.websocket_connect(BOARD_SOCKET) as socket:
                socket.receive_json()
                socket.send_json(write())
                error = receive_until(socket, lambda value: value['type'] == 'error')
                self.assertEqual(error['code'], 'rate_limit_exceeded')
                self.assertEqual(error['retry_after'], 2)
                self.assertEqual(store.read()['revision'], 0)

    def test_invalid_message_never_saves(self):
        with TestClient(app) as client:
            store = FakeStore()
            app.state.board_hubs[BOARD_ID] = DocumentHub(store)
            with client.websocket_connect(BOARD_SOCKET) as socket:
                socket.receive_json()
                socket.send_json(write('x' * 5001))
                error = receive_until(socket, lambda value: value['type'] == 'error')
                self.assertEqual(error['code'], 'invalid_message')
                self.assertEqual(store.read()['revision'], 0)

    def test_commit_failure_is_never_acknowledged(self):
        with TestClient(app) as client:
            store = FakeStore()
            app.state.board_hubs[BOARD_ID] = DocumentHub(store)
            with patch.object(store, 'write', side_effect=RuntimeError('private database details')), \
                 client.websocket_connect(BOARD_SOCKET) as socket:
                socket.receive_json()
                socket.send_json(write())
                error = receive_until(socket, lambda value: value['type'] == 'error')
                self.assertEqual(error, {'type': 'error', 'code': 'storage_unavailable'})

    def test_missing_database_does_not_claim_sync_is_available(self):
        with TestClient(app) as client, client.websocket_connect(BOARD_SOCKET) as socket:
            self.assertEqual(socket.receive_json()['code'], 'storage_unavailable')

    def test_root_is_not_a_document_store(self):
        with self.assertRaises(ValueError):
            DocumentStore(None, 'main')

    def test_write_validation(self):
        for message in [[], {}, write(sequence=True), write(sequence=0), write(client='invalid'),
                        write('\x00'), write('\ud800'), write('é' * 5001), write('😀' * 5001)]:
            with self.subTest(message_type=type(message)), self.assertRaises(ValueError):
                validate_write(message)
        self.assertEqual(validate_write(write('é' * 5000))['sequence'], 1)
        self.assertEqual(validate_write(write('😀' * 5000))['sequence'], 1)


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_instances_see_committed_updates_and_coalesce(self):
        store = FakeStore()
        first, second = DocumentHub(store), DocumentHub(store)
        a, b = first.subscribe('ip'), second.subscribe('ip')
        try:
            await asyncio.wait_for(a.get(), 1)
            await asyncio.wait_for(b.get(), 1)
            first.publish(store.write(str(uuid4()), 1, 'one'))
            first.publish(store.write(str(uuid4()), 1, 'two'))
            self.assertEqual(a.qsize(), 1)
            self.assertEqual((await a.get())['content'], 'two')
            value = await asyncio.wait_for(b.get(), 1)
            self.assertEqual(value['content'], 'two')
            first.publish({'type': 'snapshot', 'content': 'old', 'revision': 0})
            self.assertEqual(a.qsize(), 0)
        finally:
            await first.unsubscribe(a)
            await second.unsubscribe(b)
        self.assertIsNone(first.task)
        self.assertIsNone(second.task)

    async def test_connection_limit(self):
        hub = DocumentHub(FakeStore())
        queues = [hub.subscribe('one-ip') for _ in range(32)]
        try:
            with self.assertRaises(ValueError):
                hub.subscribe('one-ip')
        finally:
            for queue in queues:
                await hub.unsubscribe(queue)


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class PostgresSyncTests(unittest.TestCase):
    def test_durable_ordering_retries_and_database_constraints(self):
        engine = create_engine(URL.create('postgresql+pg8000', host='127.0.0.1',
            port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']), username=os.environ['BBOARD_TEST_POSTGRES_USER'],
            database='postgres'), connect_args={'timeout': 5}, pool_size=4, max_overflow=0)
        try:
            with engine.begin() as connection:
                connection.execute(text('CREATE SCHEMA IF NOT EXISTS app'))
                if connection.execute(text("SELECT to_regclass('app.boards')")).scalar_one() is None:
                    connection.execute(text((Path(__file__).resolve().parents[1] / 'migrations/004_boards.sql').read_text()))
                connection.execute(text("INSERT INTO app.boards (id) VALUES (:id)"), {'id': 'e' * 64})
            stores = [DocumentStore(engine, 'e' * 64), DocumentStore(engine, 'e' * 64)]
            original = str(uuid4())
            self.assertEqual(stores[0].write(original, 1, 'first')['revision'], 1)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda i: stores[i % 2].write(str(uuid4()), 1, str(i)), range(8)))
            self.assertEqual(sorted(value['revision'] for value in results), list(range(2, 10)))
            latest = max(results, key=lambda value: value['revision'])
            self.assertEqual(stores[1].read(), latest)
            self.assertEqual(stores[1].write(original, 1, 'stale retry'), latest)
            self.assertEqual(stores[1].write(original, 3, 'newer')['revision'], 10)
            self.assertEqual(stores[0].write(original, 2, 'out of order')['content'], 'newer')
            # Recreated store reads durable state; conditional polling omits unchanged text.
            self.assertEqual(DocumentStore(engine, 'e' * 64).read()['revision'], 10)
            self.assertIsNone(stores[0].read(after=10))
            with self.assertRaises(Exception):
                with engine.begin() as connection:
                    connection.execute(text("UPDATE app.boards SET content=repeat('x',65537)"))
            self.assertEqual(stores[0].read()['content'], 'newer')
        finally:
            engine.dispose()
