"""Real PostgreSQL proof of the key-only HTTP/MCP exchange and retry contract."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from main import app
from security import RateLimitDecision
from test_mcp import call
from test_realtime import receive_until
from realtime import DocumentStore
from sqlalchemy.exc import DBAPIError


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'Requires disposable loopback PostgreSQL')
class HTTPAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = dict(host='127.0.0.1', port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']), username=os.environ['BBOARD_TEST_POSTGRES_USER'])
        admin = create_engine(URL.create('postgresql+pg8000', **config, database='postgres'), isolation_level='AUTOCOMMIT')
        with admin.connect() as connection:
            connection.execute(text('CREATE DATABASE http_agents_test'))
        admin.dispose()
        cls.engine = create_engine(URL.create('postgresql+pg8000', **config, database='http_agents_test'), connect_args={'timeout': 5}, pool_size=4, max_overflow=0)
        with cls.engine.begin() as connection:
            connection.execute(text('CREATE SCHEMA app'))
            for name in ('004_boards.sql', '006_board_appends.sql', '007_board_creations.sql'):
                connection.execute(text((Path(__file__).resolve().parents[1] / 'migrations' / name).read_text()))
            connection.execute(text('INSERT INTO app.boards (id, content, revision) VALUES (:key, :content, 7)'),
                               {'key': 'c' * 64, 'content': 'text saved before history'})
            connection.execute(text((Path(__file__).resolve().parents[1] / 'migrations/008_board_events.sql').read_text()))
            connection.execute(text((Path(__file__).resolve().parents[1] / 'migrations/009_exact_patch_events.sql').read_text()))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        env = patch.dict(os.environ, {'DB_MODE': 'disabled', 'BBOARD_SITE_URL': 'http://localhost:6600'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app, base_url='http://localhost:9900')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        app.state.database.engine = self.engine
        app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))

    def create(self, content='coordinator: brief'):
        payload = {'content': content, 'idempotency_key': str(uuid4())}
        response = self.client.post('/write', json={'content': content},
                                    headers={'Idempotency-Key': payload['idempotency_key']})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), payload

    def test_create_retry_across_transports_recovers_current_board_without_overwrite(self):
        board, payload = self.create()
        self.assertEqual(board['url'], 'http://localhost:6600/' + board['key'])
        receipt = call(self.client, 'append', {'key': board['key'], 'content': '\nreviewer: checked',
                                               'idempotency_key': str(uuid4())})['structuredContent']
        retry = call(self.client, 'create', payload)['structuredContent']
        self.assertTrue(retry['duplicate'])
        self.assertEqual(retry['key'], board['key'])
        self.assertEqual(retry['content'], receipt['content'])
        self.assertEqual(self.client.post('/write', json={'content': 'different'},
            headers={'Idempotency-Key': payload['idempotency_key']}).status_code, 409)
        get_retry = self.client.get('/write', params={
            'content': payload['content'], 'op': payload['idempotency_key'], 'format': 'json'}).json()
        self.assertEqual(get_retry['key'], board['key'])
        self.assertTrue(get_retry['duplicate'])

    def test_post_append_survives_lost_response_and_cross_transport_retry(self):
        board, _ = self.create()
        key = board['key']
        addition = {'content': '\nresult + & 😀', 'idempotency_key': str(uuid4())}
        headers = {'Idempotency-Key': addition['idempotency_key']}
        self.client.post('/' + key + '/append', json={'content': addition['content']},
                         headers=headers)  # Discard the first receipt.
        retry = self.client.post('/' + key + '/append', json={'content': addition['content']}, headers=headers).json()
        self.assertTrue(retry['duplicate'])
        self.assertEqual(retry['revision'], 1)
        self.assertEqual(retry['content'].count(addition['content']), 1)
        get_retry = self.client.get('/' + key, params={
            'append': addition['content'], 'op': addition['idempotency_key'], 'format': 'json'}).json()
        self.assertTrue(get_retry['duplicate'])
        conflict = self.client.post('/' + key + '/append', json={'content': 'changed'}, headers=headers)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json(), {'detail': 'idempotency_key_reused_with_different_content'})
        get_conflict = self.client.get('/' + key, params={'append': 'changed', 'op': addition['idempotency_key']})
        self.assertEqual(get_conflict.status_code, 409)
        self.assertEqual(get_conflict.json(), conflict.json())
        self.assertEqual(self.client.get('/' + key + '?format=json').json()['revision'], 1)

    def test_concurrent_http_appends_preserve_both_contributions(self):
        board, _ = self.create('')
        def post(value):
            return self.client.post('/' + board['key'] + '/append', json={'content': value},
                                    headers={'Idempotency-Key': str(uuid4())}).json()
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(post, ['A', 'B']))
        self.assertEqual(sorted(r['revision'] for r in results), [1, 2])
        self.assertIn(self.client.get('/' + board['key'] + '?format=json').json()['content'], ('AB', 'BA'))

    def test_http_wait_receives_mcp_append_and_releases_capacity(self):
        board, _ = self.create()
        async def registered():
            async with asyncio.timeout(3):
                while not app.state.mcp_waiters:
                    await asyncio.sleep(.01)
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.client.get, '/' + board['key'] + '/wait', params={'after_revision': 0, 'timeout_seconds': 3})
            self.client.portal.call(registered)
            call(self.client, 'append', {'key': board['key'], 'content': '\nchecked',
                                         'idempotency_key': str(uuid4())})
            result = future.result(timeout=5)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['changed'])
        self.assertEqual(result.json()['revision'], 1)
        self.assertFalse(app.state.mcp_waiters)
        timeout = self.client.get('/' + board['key'] + '/wait?after_revision=1&timeout_seconds=0')
        self.assertFalse(timeout.json()['changed'])
        self.assertFalse(app.state.mcp_waiters)

    def test_wait_validation_missing_boards_and_shared_capacity(self):
        board, _ = self.create()
        for query in ('after_revision=-1', 'after_revision=x', 'after_revision=0&timeout_seconds=31'):
            invalid = self.client.get('/' + board['key'] + '/wait?' + query)
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(invalid.json()['detail'], 'invalid_request')
            self.assertEqual(invalid.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/' + '0' * 64 + '/wait?after_revision=0').status_code, 404)
        self.assertFalse(app.state.mcp_waiters)
        app.state.mcp_waiters = {'other': 64}
        response = self.client.get('/' + board['key'] + '/wait?after_revision=0')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '5')
        app.state.mcp_waiters.clear()

    def test_invalid_payloads_never_mutate_and_json_limit_matches_spec(self):
        board, _ = self.create()
        path = '/' + board['key'] + '/append'
        for payload, headers in (
                ({'content': 'x'}, {}),
                ({'content': 'x'}, {'Idempotency-Key': 'bad'}),
                ({'content': ''}, {'Idempotency-Key': str(uuid4())}),
                ({'content': 'x', 'operation_id': str(uuid4())}, {'Idempotency-Key': str(uuid4())}),
                ({'content': 'x', 'author': 'name'}, {'Idempotency-Key': str(uuid4())})):
            invalid = self.client.post(path, json=payload, headers=headers)
            self.assertEqual(invalid.status_code, 422)
            self.assertNotIn('max_characters', invalid.json())
        self.assertEqual(self.client.post('/write', json={'content': 'x'}).status_code, 422)
        self.assertEqual(self.client.post('/write', json={'content': 'x'},
            headers={'Idempotency-Key': '00000000-0000-1000-8000-000000000000'}).status_code, 422)
        too_long = self.client.post(path, json={'content': 'x' * 5000},
                                    headers={'Idempotency-Key': str(uuid4())})
        self.assertEqual(too_long.status_code, 413)
        self.assertEqual(too_long.json(), {'detail': 'content_too_long', 'max_characters': 5000})
        self.assertEqual(self.client.get('/' + board['key'] + '?format=json').json()['revision'], 0)

    def test_local_pages_and_schema_link_to_local_services(self):
        board, _ = self.create()
        html = self.client.get('/' + board['key'], headers={'Accept': 'text/html'}).text
        self.assertIn('http://localhost:6600/' + board['key'], html)
        self.assertNotIn('https://bboard.ai/' + board['key'], html)
        guide = self.client.get('/help.md').text
        self.assertIn('Use http://localhost:6600.', guide)
        self.assertEqual(self.client.get('/openapi.json').json()['servers'], [{'url': 'http://localhost:6600'}])

    def test_every_writer_records_recoverable_changes_and_utc_states(self):
        board, payload = self.create('hello 😀 world')
        key = board['key']
        path = '/' + key
        states = [board['content']]
        addition = {'content': '\nreply', 'idempotency_key': str(uuid4())}
        states.append(self.client.post(path + '/append', json={'content': addition['content']},
            headers={'Idempotency-Key': addition['idempotency_key']}).json()['content'])
        self.client.post(path + '/append', json={'content': addition['content']},
                         headers={'Idempotency-Key': addition['idempotency_key']})
        self.client.post('/write', json={'content': payload['content']},
                         headers={'Idempotency-Key': payload['idempotency_key']})
        deleted = self.client.patch(path, json={'old_text': '😀 ', 'new_text': '', 'expected_revision': 1})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        states.append(deleted.json()['content'])
        states.append(self.client.put(path, json={'content': 'replaced', 'expected_revision': 2}).json()['content'])
        self.assertEqual(self.client.put(path, json={'content': 'conflict', 'expected_revision': 2}).status_code, 409)
        with self.client.websocket_connect('/ws/boards/' + key) as socket:
            receive_until(socket, lambda m: m.get('type') == 'snapshot')
            message = {'type': 'write', 'client_id': str(uuid4()), 'sequence': 1, 'content': ''}
            socket.send_json(message)
            receive_until(socket, lambda m: m.get('type') == 'ack')
            socket.send_json(message)
            receive_until(socket, lambda m: m.get('type') == 'ack')
        states.append('')
        states.append(call(self.client, 'append', {'key': key, 'content': 'from MCP',
            'idempotency_key': str(uuid4())})['structuredContent']['content'])
        states.append(self.client.get(path, params={'append': '\nfrom GET', 'op': str(uuid4()), 'format': 'json'}).json()['content'])
        response = self.client.get(path + '/events')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('noindex', response.headers['x-robots-tag'])
        events = response.json()['events']
        self.assertEqual([e['content'] for e in events], states)
        self.assertEqual([e['revision'] for e in events], list(range(len(states))))
        self.assertEqual(events[2]['change'], {'offset': 6, 'removed': '😀 ', 'inserted': ''})
        self.assertEqual(events[2]['kind'], 'delete')
        self.assertEqual(events[4]['change']['removed'], 'replaced')
        previous = ''
        for event in events:
            self.assertRegex(event['recorded_at'], r'^\d{4}-\d{2}-\d{2}T.*Z$')
            change = event['change']
            start = change['offset']
            self.assertEqual(previous[start:start + len(change['removed'])], change['removed'])
            replayed = previous[:start] + change['inserted'] + previous[start + len(change['removed']):]
            self.assertEqual(replayed, event['content'])
            self.assertEqual(self.client.get(path + '/revisions/' + str(event['revision'])).json(), {'key': key, **event})
            previous = replayed
        self.assertEqual(call(self.client, 'events', {'key': key})['structuredContent'], response.json())
        self.assertEqual(call(self.client, 'read_revision', {'key': key, 'revision': 2})['structuredContent'], {'key': key, **events[2]})

    def test_history_pages_keep_the_same_range_during_concurrent_writes(self):
        board, _ = self.create('')
        store = DocumentStore(self.engine, board['key'])
        with ThreadPoolExecutor(4) as executor:
            list(executor.map(lambda content: store.append(content, str(uuid4())), ['A', 'B', 'C', 'D']))
        path = '/' + board['key'] + '/events'
        first = self.client.get(path, params={'limit': 2}).json()
        self.assertEqual(first['through_revision'], 4)
        self.assertEqual(first['next_after_revision'], 1)
        store.append('later', str(uuid4()))
        all_events = first['events']
        page = first
        while page['next_after_revision'] is not None:
            page = self.client.get(path, params={'limit': 2, 'after_revision': page['next_after_revision'], 'through_revision': first['through_revision']}).json()
            all_events.extend(page['events'])
        self.assertEqual([event['revision'] for event in all_events], [0, 1, 2, 3, 4])
        newer = self.client.get(path, params={'after_revision': first['through_revision']}).json()
        self.assertEqual([event['revision'] for event in newer['events']], [5])
        before = ''
        for event in all_events:
            self.assertTrue(event['content'].startswith(before))
            before = event['content']

    def test_baseline_bounds_validation_and_missing_revision_are_explicit(self):
        path = '/' + 'c' * 64
        baseline = self.client.get(path + '/events').json()
        self.assertEqual(baseline['first_revision'], 7)
        self.assertEqual(baseline['events'][0]['kind'], 'baseline')
        self.assertIsNone(baseline['events'][0]['change'])
        self.assertEqual(self.client.get(path + '/revisions/6').json()['detail'], 'revision_not_recorded')
        for query in ('limit=0', 'limit=101', 'after_revision=-2', 'through_revision=8', 'through_revision=x'):
            self.assertEqual(self.client.get(path + '/events?' + query).status_code, 422, query)
        self.assertEqual(self.client.get(path + '/revisions/-1').status_code, 422)
        self.assertEqual(self.client.get('/' + '0' * 64 + '/events').status_code, 404)
        self.assertEqual(self.client.get('/' + '0' * 64 + '/revisions/0').json()['detail'], 'board_not_found')
        self.assertEqual(self.client.get(path + '/events?after_revision=7').json()['events'], [])

    def test_history_and_live_state_commit_together_and_events_cannot_be_changed(self):
        board, _ = self.create('kept')
        key = board['key']
        statements = [
            "UPDATE app.board_events SET content='gone' WHERE board_id=:key",
            'DELETE FROM app.board_events WHERE board_id=:key',
            'DELETE FROM app.boards WHERE id=:key',
            "UPDATE app.boards SET content='unrecorded' WHERE id=:key",
        ]
        for sql in statements:
            with self.assertRaises(DBAPIError), self.engine.begin() as connection:
                connection.execute(text(sql), {'key': key})
        with self.engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("UPDATE app.boards SET content='rolled back', revision=revision+1 WHERE id=:key"), {'key': key})
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.board_events WHERE board_id=:key'), {'key': key}).scalar(), 2)
            transaction.rollback()
        self.assertEqual(self.client.get('/' + key + '?format=json').json()['content'], 'kept')
        self.assertEqual(len(self.client.get('/' + key + '/events').json()['events']), 1)

    def test_history_html_shows_saved_state_and_escapes_deleted_content(self):
        board, _ = self.create('<script>old text</script>')
        path = '/' + board['key']
        self.client.put(path, json={'content': 'current text', 'expected_revision': 0})
        saved = self.client.get(path + '?revision=0', headers={'Accept': 'text/html'})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIn('Revision 0', saved.text)
        self.assertIn('&lt;script&gt;old text&lt;/script&gt;', saved.text)
        self.assertNotIn('<script>old text</script>', saved.text)
        self.assertNotIn('current text', saved.text)
        self.assertIn('"path": "' + path + '?history=1"', saved.text)
        self.assertIn('noindex', saved.headers['x-robots-tag'])
        history = self.client.get(path + '?history=1', headers={'Accept': 'text/html'})
        self.assertIn('Board history', history.text)
        self.assertIn('current text', history.text)
        self.assertIn('Removed:', history.text)
        self.assertEqual(self.client.get(path + '?revision=invalid', headers={'Accept': 'text/html'}).status_code, 422)

    def test_patch_event_preserves_requested_span_at_repeated_boundaries(self):
        board, _ = self.create('researcher: temporary note\nreviewer: ready')
        path = '/' + board['key']
        response = self.client.patch(path, json={'old_text': 'researcher: temporary note\n', 'new_text': '', 'expected_revision': 0})
        self.assertEqual(response.status_code, 200, response.text)
        event = self.client.get(path + '/revisions/1').json()
        self.assertEqual(event['change'], {'offset': 0, 'removed': 'researcher: temporary note\n', 'inserted': ''})
        self.assertEqual(event['content'], 'reviewer: ready')
        # A supplied span must actually reproduce the new state; rejection rolls
        # back the current state and the event in the same transaction.
        with self.assertRaises(DBAPIError), self.engine.begin() as connection:
            connection.execute(text("SELECT set_config('bboard.change', :change, true)"),
                               {'change': '{"offset":0,"removed":"forged","inserted":""}'})
            connection.execute(text("UPDATE app.boards SET revision=revision+1, content='different' WHERE id=:key"), {'key': board['key']})
        self.assertEqual(self.client.get(path + '?format=json').json()['revision'], 1)
