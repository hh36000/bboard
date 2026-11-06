from security import RateLimitDecision
"""Keyed-board API, persistence, routing, and isolation checks."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import threading
import os
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from starlette.websockets import WebSocketDisconnect

from main import app
from realtime import DocumentStore
from test_realtime import receive_until, write


class BoardBoundaryTests(unittest.TestCase):
    def test_create_and_append_require_one_uuid4_idempotency_key(self):
        with patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True), TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(
                return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            requests = [('/boards', None), ('/write', {'content': 'x'}),
                        ('/' + 'a' * 64 + '/append', {'content': 'x'})]
            for path, payload in requests:
                missing = client.post(path, json=payload) if payload is not None else client.post(path)
                self.assertEqual(missing.status_code, 422)
                self.assertEqual(missing.json()['detail'], 'idempotency_key_required')
                invalid = client.post(path, json=payload,
                    headers={'Idempotency-Key': '00000000-0000-1000-8000-000000000000'}) \
                    if payload is not None else client.post(path,
                        headers={'Idempotency-Key': '00000000-0000-1000-8000-000000000000'})
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()['detail'], 'invalid_idempotency_key')
            duplicated = client.post('/write', json={'content': 'x'}, headers=[
                ('Idempotency-Key', str(uuid4())), ('Idempotency-Key', str(uuid4()))])
            self.assertEqual(duplicated.status_code, 422)
            self.assertEqual(duplicated.json()['detail'], 'invalid_idempotency_key')

    def test_patch_validates_body_and_keeps_nonboard_paths_private(self):
        with patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True), TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            path = '/' + 'a' * 64
            valid = {'old_text': 'before', 'new_text': '', 'expected_revision': 0}
            self.assertEqual(client.patch(path, json=valid).status_code, 503)
            invalid = [{}, [], {**valid, 'replace_all': True}, {**valid, 'replace_all': False},
                       {'append': 'text', 'expected_revision': 0}, {'old_text': 'before', 'new_text': ''}]
            invalid += [{**valid, 'expected_revision': r} for r in [True, -1, None, '0', 0.0]]
            invalid += [{**valid, field: v} for field in ['old_text', 'new_text'] for v in [None, 1, '\x00', '\ud800']]
            for payload in invalid:
                response = client.patch(path, content=json.dumps(payload), headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 422, payload)
            self.assertEqual(client.patch(path, content='{', headers={'Content-Type': 'application/json'}).status_code, 400)
            self.assertEqual(client.patch(path, content='plain').status_code, 415)
            self.assertEqual(client.patch(path, content=b' ' * 65537, headers={'Content-Type': 'application/json'}).status_code, 413)
            for denied in ['/help', '/', '/write', '/boards/' + 'a' * 64, '/' + 'A' * 64, path + '/', path + '/other']:
                self.assertEqual(client.patch(denied, json=valid).status_code, 401)
            preflight = client.options(path, headers={'Origin': 'http://localhost:6600',
                'Access-Control-Request-Method': 'PATCH', 'Access-Control-Request-Headers': 'content-type,api-version'})
            self.assertEqual(preflight.status_code, 200)

    def test_put_validates_content_revision_and_exact_public_paths(self):
        with patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True), TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            path = '/' + 'a' * 64
            for content in ['', 'x' * 5000, '😀' * 5000]:
                self.assertEqual(client.put(path, json={'content': content, 'expected_revision': 0}).status_code, 503)
            for revision in [None, True, False, -1, 1.0, '0', [], {}]:
                response = client.put(path, json={'content': '', 'expected_revision': revision})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {'detail': 'invalid_expected_revision'})
            for payload in [{}, [], {'content': ''}, {'expected_revision': 0},
                            {'content': '', 'expected_revision': 0, 'extra': 1},
                            *[{'content': c, 'expected_revision': 0} for c in [None, 3, '\x00', '\ud800']]]:
                self.assertEqual(client.put(path, content=json.dumps(payload), headers={'Content-Type': 'application/json'}).status_code, 422)
            self.assertEqual(client.put(path, json={'content': '😀' * 5001, 'expected_revision': 0}).status_code, 413)
            self.assertEqual(client.put(path, content='{', headers={'Content-Type': 'application/json'}).status_code, 400)
            self.assertEqual(client.put(path, content='text').status_code, 415)
            self.assertEqual(client.put(path, content=b' ' * 65537, headers={'Content-Type': 'application/json'}).status_code, 413)
            for denied in ['/help', '/', '/write', '/boards/' + 'a' * 64, '/' + 'A' * 64, path + '/', path + '/other']:
                self.assertEqual(client.put(denied, json={'content': '', 'expected_revision': 0}).status_code, 401)
            self.assertEqual(client.patch(path, json={'content': ''}).status_code, 422)
            preflight = client.options(path, headers={'Origin': 'http://localhost:6600',
                'Access-Control-Request-Method': 'PUT', 'Access-Control-Request-Headers': 'content-type,api-version'})
            self.assertEqual(preflight.status_code, 200)

    def test_write_validates_json_and_limits_before_database_access(self):
        with patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True), TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            for content in ['', 'x' * 5000, '😀' * 5000]:
                self.assertEqual(client.post('/write', json={'content': content},
                    headers={'Idempotency-Key': str(uuid4())}).status_code, 503)
            for content in ['x' * 5001, '😀' * 5001]:
                response = client.post('/write', json={'content': content})
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()['max_characters'], 5000)
            for payload in [{}, [], {'content': 3}, {'content': None}, {'content': '\x00'},
                            {'content': '\ud800'}, {'content': '', 'key': 'a' * 64}]:
                response = client.post('/write', content=json.dumps(payload), headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 422)
            self.assertEqual(client.post('/write', content='{', headers={'Content-Type': 'application/json'}).status_code, 400)
            self.assertEqual(client.post('/write', content='hello').status_code, 415)
            self.assertEqual(client.post('/write', content=b' ' * 65537, headers={'Content-Type': 'application/json'}).status_code, 413)
            for method, path in [('POST', '/write/'), ('DELETE', '/write')]:
                self.assertEqual(client.request(method, path).status_code, 401)
            self.assertEqual(client.options('/write', headers={'Origin': 'http://localhost:6600',
                'Access-Control-Request-Method': 'POST'}).status_code, 200)

    def test_only_explicit_board_methods_and_valid_key_paths_are_public(self):
        with patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True), TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            self.assertEqual(client.post('/boards').status_code, 422)
            self.assertEqual(client.post('/boards', headers={'Idempotency-Key': str(uuid4())}).status_code, 503)
            self.assertEqual(client.get('/' + 'a' * 64).status_code, 503)
            self.assertEqual(client.get('/' + 'a' * 64 + '/other').status_code, 503)
            for method, path in [('GET', '/boards'), ('DELETE', '/' + 'a' * 64),
                                 ('GET', '/' + 'A' * 64), ('GET', '/main'), ('GET', '/boards/' + 'a' * 64),
                                 ('GET', '/' + 'A' * 64 + '/other')]:
                self.assertEqual(client.request(method, path).status_code, 401)
            for method, path in [('POST', '/boards'), ('GET', '/' + 'a' * 64), ('GET', '/' + 'a' * 64 + '/other')]:
                response = client.options(path, headers={'Origin': 'http://localhost:6600',
                    'Access-Control-Request-Method': method})
                self.assertEqual(response.status_code, 200)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/ws/boards/not-a-key'):
                    pass


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class PostgresBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(URL.create('postgresql+pg8000', host='127.0.0.1',
            port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']), username=os.environ['BBOARD_TEST_POSTGRES_USER'],
            database='postgres'), connect_args={'timeout': 5}, pool_size=4, max_overflow=0)
        with cls.engine.begin() as connection:
            connection.execute(text('CREATE SCHEMA IF NOT EXISTS app'))
            directory = Path(__file__).resolve().parents[1] / 'migrations'
            connection.execute(text((directory / '004_boards.sql').read_text()))
            if not connection.execute(text("SELECT to_regclass('app.board_creations')")).scalar():
                connection.execute(text((directory / '007_board_creations.sql').read_text()))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text('DELETE FROM app.board_writers'))
            connection.execute(text('DELETE FROM app.boards'))
        env = patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        app.state.database.engine = self.engine
        app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))

    def create(self):
        response = self.client.post('/boards', headers={'Idempotency-Key': str(uuid4())})
        self.assertEqual(response.status_code, 201)
        board = response.json()
        self.assertRegex(board['key'], r'^[0-9a-f]{64}$')
        self.assertEqual(board['path'], '/' + board['key'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return board['key']

    def post_write(self, content):
        return self.client.post('/write', json={'content': content},
                                headers={'Idempotency-Key': str(uuid4())})

    def test_creation_keys_are_durable_and_get_returns_current_content(self):
        a, b = self.create(), self.create()
        self.assertNotEqual(a, b)
        self.assertEqual(self.client.get('/' + a).json(), {'key': a, 'content': '', 'revision': 0})
        writer = str(uuid4())
        DocumentStore(self.engine, a).write(writer, 1, 'board A')
        DocumentStore(self.engine, b).write(writer, 1, 'board B')
        DocumentStore(self.engine, a).write(writer, 1, 'stale retry')
        for key, content in [(a, 'board A'), (b, 'board B')]:
            response = self.client.get('/' + key)
            self.assertEqual(response.json(), {'key': key, 'content': content, 'revision': 1})
            self.assertEqual(response.headers['cache-control'], 'no-store')
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.board_writers')).scalar_one(), 2)

    def test_home_count_only_excludes_unused_empty_boards(self):
        self.create()
        self.post_write('current text')
        cleared = self.create()
        self.client.put('/' + cleared, json={'content': 'used once', 'expected_revision': 0})
        self.client.put('/' + cleared, json={'content': '', 'expected_revision': 1})

        response = self.client.get('/', headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['content'].endswith('bboards in existence: 2\n'))

    def test_post_write_commits_content_and_returns_live_board_url(self):
        for content in ['', 'x' * 5000, '😀' * 5000]:
            response = self.post_write(content)
            self.assertEqual(response.status_code, 200)
            key = response.json()['key']
            self.assertRegex(key, r'^[0-9a-f]{64}$')
            self.assertEqual(response.json()['url'], 'https://bboard.ai/' + key)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(self.client.get('/' + key).json()['content'], content)
            with self.client.websocket_connect('/ws/boards/' + key) as socket:
                self.assertEqual(socket.receive_json()['content'], content)
                socket.send_json(write('😀' * 5000))
                self.assertEqual(receive_until(socket, lambda v: v['type'] == 'ack')['revision'], 1)
                socket.send_json(write('x' * 5001, sequence=2))
                self.assertEqual(receive_until(socket, lambda v: v['type'] == 'error')['code'], 'invalid_message')
            self.assertEqual(DocumentStore(self.engine, key).read()['content'], '😀' * 5000)

    def test_page_representations_follow_live_commits_without_cross_board_leaks(self):
        board = self.post_write('First visible note').json()
        other = self.post_write('Another board').json()
        path = board['path']
        site = {'X-FH-Requested-Host': 'bboard.ai'}
        page = self.client.get(path, headers=site)
        self.assertEqual(page.status_code, 200)
        self.assertIn('First visible note', page.text)
        self.assertNotIn('Another board', page.text)
        self.assertIn('noindex', page.headers['x-robots-tag'])
        with self.client.websocket_connect('/ws/boards/' + board['key']) as socket:
            self.assertEqual(socket.receive_json()['content'], 'First visible note')
            socket.send_json(write('Updated live note'))
            receive_until(socket, lambda value: value['type'] == 'ack')
        self.assertIn('Updated live note', self.client.get(path, headers=site).text)
        self.assertEqual(self.client.get(path, headers={**site, 'Accept': 'text/markdown'}).text, 'Updated live note')
        self.assertEqual(self.client.get(path).json()['revision'], 1)
        self.assertEqual(self.client.get(other['path']).json()['content'], 'Another board')

    def test_rejected_post_write_creates_no_board(self):
        self.assertEqual(self.client.post('/write', json={'content': 'x' * 5001}).status_code, 413)
        app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
        self.assertEqual(self.client.post('/write', json={'content': 'blocked'}).status_code, 429)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one(), 0)

    def test_unknown_keys_do_not_create_boards(self):
        key = 'f' * 64
        response = self.client.get('/' + key)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'detail': 'board_not_found'})
        with self.client.websocket_connect('/ws/boards/' + key) as socket:
            self.assertEqual(socket.receive_json(), {'type': 'error', 'code': 'board_not_found'})
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one(), 0)

    def test_live_broadcasts_and_receipts_stay_with_their_board(self):
        a, b = self.create(), self.create()
        with self.client.websocket_connect('/ws/boards/' + a) as first, \
             self.client.websocket_connect('/ws/boards/' + a) as peer, \
             self.client.websocket_connect('/ws/boards/' + b) as other:
            for socket in [first, peer, other]:
                self.assertEqual(socket.receive_json()['content'], '')
            identity = str(uuid4())
            first.send_json(write('only board A', client=identity))
            self.assertEqual(receive_until(first, lambda v: v['type'] == 'ack')['revision'], 1)
            self.assertEqual(receive_until(peer, lambda v: v.get('revision') == 1)['content'], 'only board A')
            other.send_json(write('only board B', client=identity))
            for _ in range(5):
                value = other.receive_json()
                self.assertNotEqual(value.get('content'), 'only board A')
                if value['type'] == 'ack':
                    self.assertEqual(value['revision'], 1)
                    break
            else:
                self.fail('Missing board B acknowledgment')
            self.assertEqual(self.client.get('/' + a).json()['content'], 'only board A')
            self.assertEqual(self.client.get('/' + b).json()['content'], 'only board B')
        self.assertEqual(app.state.board_hubs, {})

    def test_connection_cap_cannot_be_bypassed_by_switching_board_keys(self):
        keys = [self.create() for _ in range(32)]
        with ExitStack() as stack:
            sockets = []
            for key in keys[:31]:
                socket = stack.enter_context(self.client.websocket_connect('/ws/boards/' + key))
                socket.receive_json()
                sockets.append(socket)
            # The 32nd tab can still save and update an existing peer.
            peer = stack.enter_context(self.client.websocket_connect('/ws/boards/' + keys[0]))
            peer.receive_json()
            peer.send_json(write('saved from tab 32'))
            self.assertEqual(receive_until(peer, lambda v: v['type'] == 'ack')['content'], 'saved from tab 32')
            self.assertEqual(receive_until(sockets[0], lambda v: v.get('revision') == 1)['content'], 'saved from tab 32')
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect('/ws/boards/' + keys[31]):
                    pass
        self.assertEqual(app.state.board_hubs, {})

    def test_rate_limited_creation_does_not_insert_a_board(self):
        app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
        self.assertEqual(self.client.post('/boards').status_code, 429)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one(), 0)

    def test_generated_key_collision_never_overwrites_existing_text(self):
        first = self.create()
        DocumentStore(self.engine, first).write(str(uuid4()), 1, 'keep this')
        with patch('realtime.secrets.token_hex', side_effect=[first, 'c' * 64]):
            second = self.create()
        self.assertEqual(second, 'c' * 64)
        self.assertEqual(self.client.get('/' + first).json()['content'], 'keep this')

    def test_put_replaces_clears_and_conflicts_with_current_state(self):
        key, other = self.create(), self.create()
        path = '/' + key
        for revision, content in enumerate(['first response', '😀' * 5000, '']):
            payload = {'content': content, 'expected_revision': revision}
            response = self.client.put(path, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'key': key, 'content': content, 'revision': revision + 1})
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(self.client.get(path).json(), response.json())
            conflict = self.client.put(path, json=payload)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json(), {'detail': 'revision_conflict', **response.json()})
            self.assertEqual(conflict.headers['cache-control'], 'no-store')
            self.assertIn('ratelimit', conflict.headers)
        future = self.client.put(path, json={'content': 'future', 'expected_revision': 99})
        self.assertEqual(future.status_code, 409)
        self.assertEqual(future.json()['revision'], 3)
        self.assertEqual(self.client.get('/' + other).json(), {'key': other, 'content': '', 'revision': 0})
        # HTTP writes do not create browser sequence receipts.
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.board_writers')).scalar_one(), 0)

    def test_concurrent_puts_at_same_revision_have_one_winner(self):
        key = self.create()
        barrier = threading.Barrier(2)
        def put(content):
            barrier.wait(timeout=5)
            return self.client.put('/' + key, json={'content': content, 'expected_revision': 0})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(put, content) for content in ['agent A', 'agent B']]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        winner = next(r.json() for r in results if r.status_code == 200)
        loser = next(r.json() for r in results if r.status_code == 409)
        self.assertEqual(loser, {'detail': 'revision_conflict', **winner})
        self.assertEqual(self.client.get('/' + key).json(), winner)

    def test_put_and_browser_share_revisions_broadcasts_and_retry_receipts(self):
        key = self.create()
        path = '/' + key
        with self.client.websocket_connect('/ws/boards/' + key) as socket:
            self.assertEqual(socket.receive_json()['revision'], 0)
            message = write('browser saved first')
            socket.send_json(message)
            receive_until(socket, lambda v: v['type'] == 'ack')
            stale = self.client.put(path, json={'content': 'stale agent', 'expected_revision': 0})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json(), {'detail': 'revision_conflict', 'key': key, 'content': 'browser saved first', 'revision': 1})
            updated = self.client.put(path, json={'content': 'agent reconciled', 'expected_revision': 1})
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(receive_until(socket, lambda v: v.get('revision') == 2)['content'], 'agent reconciled')
            socket.send_json(message)  # Lost browser ack retried after an API commit.
            ack = receive_until(socket, lambda v: v['type'] == 'ack')
            self.assertEqual((ack['revision'], ack['content']), (2, 'agent reconciled'))
            socket.send_json({**message, 'sequence': 2, 'content': 'new browser edit'})
            self.assertEqual(receive_until(socket, lambda v: v['type'] == 'ack')['revision'], 3)
        self.assertEqual(self.client.get(path).json()['content'], 'new browser edit')

    def test_rejected_put_never_mutates_or_creates(self):
        unknown = '/' + 'f' * 64
        missing = self.client.put(unknown, json={'content': 'missing', 'expected_revision': 0})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {'detail': 'board_not_found'})
        key = self.create()
        self.assertEqual(self.client.put('/' + key, json={'content': 'x' * 5001, 'expected_revision': 0}).status_code, 413)
        self.assertEqual(self.client.put('/' + key, json={'content': 'no', 'expected_revision': 0}, headers={'API-Version': '2'}).status_code, 400)
        app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
        self.assertEqual(self.client.put('/' + key, json={'content': 'blocked', 'expected_revision': 0}).status_code, 429)
        with self.engine.connect() as connection:
            rows = connection.execute(text('SELECT id, content, revision FROM app.boards')).mappings().all()
            self.assertEqual([dict(row) for row in rows], [{'id': key, 'content': '', 'revision': 0}])

    def test_patch_is_literal_supports_deletion_and_returns_full_state(self):
        board = self.post_write('Keep [a.*] then 😀.').json()
        path = board['path']
        for revision, old, new, expected in [
            (0, '[a.*]', r'\1$literal', 'Keep \\1$literal then 😀.'),
            (1, ' then 😀', '', 'Keep \\1$literal.'),
            (2, 'Keep \\1$literal.', '', ''),
        ]:
            response = self.client.patch(path, json={'old_text': old, 'new_text': new, 'expected_revision': revision})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'key': board['key'], 'content': expected, 'revision': revision + 1})
            self.assertEqual(self.client.get(path).json(), response.json())
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_patch_missing_duplicate_overlapping_and_empty_matches_return_current_state(self):
        for content, old, detail in [('a note', 'missing', 'old_text_not_found'),
                                     ('a', 'a' * 5001, 'old_text_not_found'),
                                     ('note note', 'note', 'old_text_ambiguous'),
                                     ('aaa', 'aa', 'old_text_ambiguous'),
                                     ('😀😀😀', '😀😀', 'old_text_ambiguous'),
                                     ('', '', 'old_text_empty')]:
            board = self.post_write(content).json()
            with self.engine.connect() as connection:
                before = connection.execute(text('SELECT updated_at FROM app.boards WHERE id=:id'), {'id': board['key']}).scalar_one()
            response = self.client.patch(board['path'], json={'old_text': old, 'new_text': 'replacement', 'expected_revision': 0})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {'detail': detail, 'key': board['key'], 'content': content, 'revision': 0})
            self.assertEqual(response.headers['cache-control'], 'no-store')
            with self.engine.connect() as connection:
                self.assertEqual(connection.execute(text('SELECT updated_at FROM app.boards WHERE id=:id'), {'id': board['key']}).scalar_one(), before)

    def test_patch_checks_revision_before_matching_and_result_length(self):
        board = self.post_write('new current note').json()
        self.client.put(board['path'], json={'content': 'newer note', 'expected_revision': 0})
        response = self.client.patch(board['path'], json={'old_text': 'missing', 'new_text': 'x' * 5001, 'expected_revision': 0})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': 'revision_conflict', 'key': board['key'], 'content': 'newer note', 'revision': 1})

    def test_patch_limits_result_to_5000_unicode_characters(self):
        board = self.post_write('a😀').json()
        response = self.client.patch(board['path'], json={'old_text': 'a', 'new_text': '😀' * 4999, 'expected_revision': 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['content'], '😀' * 5000)
        response = self.client.patch(board['path'], json={'old_text': '😀' * 5000, 'new_text': '😀' * 5001, 'expected_revision': 1})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()['max_characters'], 5000)
        self.assertEqual(self.client.get(board['path']).json()['revision'], 1)

    def test_concurrent_put_and_patch_have_one_winner(self):
        board = self.post_write('original').json()
        barrier = threading.Barrier(2)
        def update(method, payload):
            barrier.wait(timeout=5)
            return self.client.request(method, board['path'], json={**payload, 'expected_revision': 0})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(update, 'PUT', {'content': 'put wins'}),
                       pool.submit(update, 'PATCH', {'old_text': 'original', 'new_text': 'patch wins'})]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        winner = next(r.json() for r in results if r.status_code == 200)
        self.assertEqual(next(r.json() for r in results if r.status_code == 409), {'detail': 'revision_conflict', **winner})
        self.assertEqual(self.client.get(board['path']).json(), winner)

    def test_patch_broadcasts_after_browser_save_and_safe_retry(self):
        key = self.create()
        with self.client.websocket_connect('/ws/boards/' + key) as socket:
            socket.receive_json()
            socket.send_json(write('browser note'))
            receive_until(socket, lambda v: v['type'] == 'ack')
            payload = {'old_text': 'note', 'new_text': 'reply', 'expected_revision': 0}
            self.assertEqual(self.client.patch('/' + key, json=payload).status_code, 409)
            payload['expected_revision'] = 1
            response = self.client.patch('/' + key, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(receive_until(socket, lambda v: v.get('revision') == 2)['content'], 'browser reply')
            repeated = self.client.patch('/' + key, json=payload)
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(repeated.json(), {'detail': 'revision_conflict', **response.json()})

    def test_patch_rate_limits_and_missing_boards_never_create_or_modify(self):
        payload = {'old_text': 'before', 'new_text': 'after', 'expected_revision': 0}
        self.assertEqual(self.client.patch('/' + 'f' * 64, json=payload).status_code, 404)
        board = self.post_write('before').json()
        self.assertEqual(self.client.patch(board['path'], json=payload, headers={'API-Version': '2'}).status_code, 400)
        app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
        self.assertEqual(self.client.patch(board['path'], json=payload).status_code, 429)
        self.assertEqual(DocumentStore(self.engine, board['key']).read(), {'type': 'snapshot', 'content': 'before', 'revision': 0})
