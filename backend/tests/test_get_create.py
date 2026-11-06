"""GET creation boundaries and durable, concurrent creation receipts."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from main import app
from realtime import create_board_once
from security import RateLimitDecision
import test_agent_pages
from test_agent_pages import KEY, SITE


class GetCreateTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_blank_content_never_creates_or_redirects_on_either_host(self):
        app.state.production = True
        with patch('realtime.create_board_once') as create_once:
            for host in ({}, SITE):
                for content in ('', ' ', '\t\r\n', '\u00a0\u2003'):
                    for kind in ('json', 'markdown', 'html'):
                        with self.subTest(host=host, content=content, format=kind):
                            response = self.client.get('/write', params={
                                'content': content, 'op': str(uuid4()), 'format': kind},
                                headers=host, follow_redirects=False)
                            self.assertEqual(response.status_code, 422)
                            self.assertEqual(response.json()['detail'], 'invalid_create_query')
                            self.assertEqual(response.json()['errors'][0]['field'], 'content')
                            self.assertEqual(response.json()['errors'][0]['reason'], 'empty')
                            self.assertNotIn('location', response.headers)
            create_once.assert_not_called()

    def test_browser_and_post_can_still_create_blank_boards(self):
        board = {'key': KEY, 'path': '/' + KEY, 'url': 'https://bboard.ai/' + KEY,
                 'content': '', 'revision': 0, 'duplicate': False}
        for path, status in (('/boards', 201), ('/write', 200)):
            idempotency_key = str(uuid4())
            with patch('realtime.create_board_once', return_value=board) as create:
                response = self.client.post(path, json={'content': ''},
                    headers={**SITE, 'Idempotency-Key': idempotency_key})
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json(), board)
                create.assert_called_once_with(None, '', idempotency_key)

    def test_receipts_on_both_hosts_in_all_representations(self):
        board = {'key': KEY, 'path': '/' + KEY, 'url': 'https://bboard.ai/' + KEY,
                 'content': '<script>alert(1)</script> & 😀', 'revision': 0, 'duplicate': False}
        for duplicate in (False, True):
            value = {**board, 'duplicate': duplicate}
            for host in ({}, SITE):
                for accept in ('application/json', 'text/html', 'text/markdown'):
                    with patch('realtime.create_board_once', return_value=value) as create:
                        params = {'content': board['content'], 'op': str(uuid4())}
                        response = self.client.get('/write', params=params, headers={**host, 'Accept': accept},
                                                   follow_redirects=False)
                        self.assertEqual(response.status_code, 303 if accept == 'text/html' else 200, response.text)
                        create.assert_called_once_with(None, params['content'], params['op'])
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertIn('noindex', response.headers['x-robots-tag'])
                    if accept == 'application/json':
                        self.assertEqual(response.json(), value)
                    elif accept == 'text/markdown':
                        self.assertIn('Board already created' if duplicate else 'Board created', response.text)
                        self.assertIn(board['url'], response.text)
                        self.assertIn('https://bboard.ai/' + KEY, response.text)
                    else:
                        self.assertEqual(response.headers['location'], board['url'])

    def test_validation_and_nonwriting_requests_never_create(self):
        op = str(uuid4())
        queries = ['content=x&op=', 'content=x&content=y', 'content=x&extra=y', 'content',
                   'content=%FF', 'content=%ZZ', 'op=' + op, 'content=x&op=bad',
                   f'content=x&content=y&op={op}', f'content=x&op={op}&extra=1',
                   f'content=x&op={op}&op={op}', f'content=%FF&op={op}',
                   f'content=%ZZ&op={op}', f'content=%&op={op}',
                   f'append=x&op={op}', f'content&op={op}']
        with patch('realtime.create_board_once') as create:
            for host in ({}, SITE):
                for query in queries:
                    response = self.client.get('/write?' + query, headers=host)
                    self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.client.get('/write?content=' + 'x' * 8192 + '&op=' + op,
                                                headers=host).status_code, 414)
                params = {'content': 'test', 'op': op}
                for params_for_head in (params,):
                    response = self.client.head('/write', params=params_for_head, headers=host)
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response.content, b'')
                for name, value in [('Purpose', 'prefetch'), ('Sec-Purpose', 'prerender'),
                                    ('X-Purpose', 'preview'), ('X-Moz', 'prefetch'), ('Sec-Fetch-Dest', 'image')]:
                    self.assertEqual(self.client.get('/write', params=params,
                        headers={**host, name: value}).status_code, 403)
                self.assertEqual(self.client.get('/write', params=params,
                    headers={**host, 'Accept': 'image/png'}).status_code, 406)
                self.assertEqual(self.client.get('/write', params=params,
                    headers={**host, 'API-Version': '2'}).status_code, 400)
            create.assert_not_called()

    def test_production_website_creates_directly_with_exact_content(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        params = {'content': '\nHello + & # % 😀', 'op': str(uuid4()), 'format': 'json'}
        board = {'key': KEY, 'path': '/' + KEY, 'url': 'https://bboard.ai/' + KEY,
                 'content': params['content'], 'revision': 0, 'duplicate': False}
        with patch('realtime.create_board_once', return_value=board) as create:
            response = self.client.get('/write', params=params, headers=SITE, follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('location', response.headers)
            self.assertEqual(response.json(), board)
            create.assert_called_once_with(None, params['content'], params['op'])
            self.assertEqual(self.client.head('/write', params=params, headers=SITE).status_code, 405)
            self.assertEqual(create.call_count, 1)

    def test_storage_errors_and_limits_do_not_expose_details(self):
        for failure, status in [(ValueError('invalid_content'), 422), (ValueError('content_too_long'), 413),
                                (ValueError('idempotency_key_reused_with_different_content'), 409),
                                (RuntimeError('private database details'), 503)]:
            with patch('realtime.create_board_once', side_effect=failure):
                response = self.client.get('/write', params={'content': 'test', 'op': str(uuid4())})
                self.assertEqual(response.status_code, status)
                self.assertNotIn('private database details', response.text)
        with patch('realtime.create_board_once') as create:
            app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
            response = self.client.get('/write', params={'content': '', 'op': str(uuid4())})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '5')
            create.assert_not_called()

    def test_missing_uuid_is_rejected_and_request_guards_still_apply(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        params = {'content': 'Hello + & 😀', 'format': 'json'}
        with patch('realtime.create_board_once') as create:
            self.assertEqual(self.client.head('/write', params=params, headers=SITE).status_code, 405)
            self.assertEqual(self.client.get('/write', params=params, headers={**SITE, 'Purpose': 'prefetch'}).status_code, 403)
            response = self.client.get('/write', params=params, headers=SITE, follow_redirects=False)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['errors'][0]['field'], 'op')
            self.assertNotIn('location', response.headers)
            create.assert_not_called()

    def test_discovery_explains_creation_without_executable_write_links(self):
        for path in ('/help', '/help.md', '/llms.txt', '/'):
            response = self.client.get(path, headers=SITE)
            self.assertIn('https://bboard.ai/write' if path == '/' else '/write?content=', response.text)
            self.assertNotRegex(response.text, r'href="[^"]*\?content=')
        spec = self.client.get('/openapi.json').json()['paths']['/write']
        params = {p['name']: p for p in spec['get']['parameters']}
        self.assertFalse(params['content']['required'])
        self.assertEqual(params['content']['schema']['minLength'], 1)
        self.assertEqual(params['content']['schema']['pattern'], r'\S')
        self.assertFalse(params['op']['required'])
        self.assertEqual(params['op']['schema']['format'], 'uuid')
        variants = spec['get']['responses']['200']['content']['application/json']['schema']['anyOf']
        self.assertTrue(any('duplicate' in item['properties'] for item in variants))
        self.assertIn('post', spec)
        self.assertNotIn('Disallow: /write', self.client.get('/robots.txt').text)

    def test_agent_can_follow_published_links_to_create_and_read_without_editing_paths(self):
        from html.parser import HTMLParser
        import re
        class Links(HTMLParser):
            def __init__(self, body):
                super().__init__()
                self.urls = []
                self.feed(body)
            def handle_starttag(self, tag, attrs):
                if tag == 'a':
                    self.urls.append(dict(attrs).get('href'))

        for page in ('/', '/help'):
            links = Links(self.client.get(page, headers=SITE).text).urls
            if page == '/':
                self.assertIn('https://bboard.ai/write', links)
        endpoint = 'https://bboard.ai/write'
        board = {'key': KEY, 'path': '/' + KEY, 'url': 'https://bboard.ai/' + KEY}
        operation = str(uuid4())
        result = {**board, 'content': 'hello', 'revision': 0, 'duplicate': False}
        with patch('realtime.create_board_once', return_value=result) as create:
            self.assertEqual(self.client.get(endpoint).status_code, 200)
            create.assert_not_called()
            receipt = self.client.get(endpoint, params={'content': 'hello', 'op': operation},
                                      headers={'Accept': 'text/markdown'})
            self.assertEqual(receipt.status_code, 200)
            api_link = re.search(r'\[Open board\]\((https://bboard\.ai/[^)]+)\)', receipt.text).group(1)
            create.assert_called_once_with(None, 'hello', operation)
        with patch('realtime.DocumentStore.read', return_value={'content': 'hello', 'revision': 0}):
            read = self.client.get(api_link, headers={'Accept': 'application/json'})
            self.assertEqual(read.json()['content'], 'hello')
            self.assertEqual(read.json()['revision'], 0)
            self.assertEqual(read.headers['cache-control'], 'no-store')


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class GetCreatePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(URL.create('postgresql+pg8000', host='127.0.0.1',
            port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']), username=os.environ['BBOARD_TEST_POSTGRES_USER'],
            database='postgres'), connect_args={'timeout': 5}, pool_size=4, max_overflow=0)
        directory = Path(__file__).resolve().parents[1] / 'migrations'
        with cls.engine.begin() as connection:
            connection.execute(text('CREATE SCHEMA IF NOT EXISTS app'))
            if not connection.execute(text("SELECT to_regclass('app.boards')")).scalar():
                connection.execute(text((directory / '004_boards.sql').read_text()))
            if not connection.execute(text("SELECT to_regclass('app.board_creations')")).scalar():
                connection.execute(text((directory / '007_board_creations.sql').read_text()))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        test_agent_pages.AgentPagesTests.setUp(self)
        app.state.database.engine = self.engine

    def test_get_creates_text_unicode_and_full_length_boards_and_retries_after_edit(self):
        keys = set()
        for content in ('Hello', '\nHello + & # % 😀', 'x' * 5000):
            params = {'content': content, 'op': str(uuid4())}
            response = self.client.get('/write', params=params)
            self.assertEqual(response.status_code, 200, response.text)
            board = response.json()
            self.assertFalse(board['duplicate'])
            self.assertEqual(board['revision'], 0)
            self.assertEqual(self.client.get(board['path']).json()['content'], content)
            keys.add(board['key'])
            self.assertEqual(self.client.put(board['path'], json={
                'content': 'later human edit', 'expected_revision': 0}).status_code, 200)
            # A new connection pool models process restart; receipts persist in PostgreSQL.
            self.engine.dispose()
            replay = self.client.get('/write', params=params).json()
            self.assertTrue(replay['duplicate'])
            self.assertEqual(replay['key'], board['key'])
            self.assertEqual((replay['content'], replay['revision']), ('later human edit', 1))
            conflict = self.client.get('/write', params={**params, 'content': 'different'})
            self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(keys), 3)

    def test_without_uuid_creates_nothing(self):
        with self.engine.connect() as connection:
            boards_before = connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one()
            before = connection.execute(text('SELECT count(*) FROM app.board_creations')).scalar_one()
        for content in ('Hello', '\nHello + & # % 😀', 'x' * 5000):
            for host in ({}, SITE):
                for _ in range(2):
                    response = self.client.get('/write', params={'content': content},
                                               headers={**host, 'Accept': 'application/json'})
                    self.assertEqual(response.status_code, 422, response.text)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one(), boards_before)
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.board_creations')).scalar_one(), before)

    def test_concurrent_identical_requests_create_exactly_one_board(self):
        op = str(uuid4())
        barrier = threading.Barrier(2)
        def create(_):
            barrier.wait(timeout=5)
            return create_board_once(self.engine, 'once', op)
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(create, range(2)))
        self.assertEqual(len({r['key'] for r in results}), 1)
        self.assertEqual(sum(not r['duplicate'] for r in results), 1)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.board_creations '
                'WHERE operation_id=CAST(:op AS uuid)'), {'op': op}).scalar_one(), 1)
        other = create_board_once(self.engine, 'once', str(uuid4()))
        self.assertNotEqual(other['key'], results[0]['key'])

    def test_concurrent_conflicting_requests_commit_only_one(self):
        op = str(uuid4())
        barrier = threading.Barrier(2)
        def create(content):
            barrier.wait(timeout=5)
            try:
                return create_board_once(self.engine, content, op)
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(create, ('first', 'second')))
        self.assertEqual(results.count('idempotency_key_reused_with_different_content'), 1)

    def test_rejected_content_and_failed_receipt_leave_no_board(self):
        with self.engine.connect() as connection:
            before = connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one()
        for content, status in [('', 422), (' \t\n', 422), ('x' * 5001, 413), ('\x00', 422)]:
            response = self.client.get('/write', params={'content': content, 'op': str(uuid4())})
            self.assertEqual(response.status_code, status)
        from realtime import insert_board
        def insert_then_fail(connection, content):
            insert_board(connection, content)
            raise RuntimeError('receipt failed')
        with patch('realtime.insert_board', side_effect=insert_then_fail):
            response = self.client.get('/write', params={'content': 'rollback', 'op': str(uuid4())})
            self.assertEqual(response.status_code, 503)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM app.boards')).scalar_one(), before)
