"""Real protocol requests and PostgreSQL concurrency, with no external services."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from main import app
from mcp_server import MCPGateway
from realtime import DocumentStore
from security import RateLimitDecision
from test_realtime import receive_until


def rpc(client, method, params=None, version='2026-07-28', **kwargs):
    params = dict(params or {})
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': version}
    if version == '2026-07-28':
        headers['Mcp-Method'] = method
        if 'name' in params:
            headers['Mcp-Name'] = params['name']
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': version,
            'io.modelcontextprotocol/clientInfo': {'name': 'bboard-tests', 'version': '1'},
            'io.modelcontextprotocol/clientCapabilities': {}}
    headers.update(kwargs.pop('headers', {}))
    response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                           headers=headers, **kwargs)
    if 'text/event-stream' in response.headers.get('content-type', ''):
        data = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        return response, data[-1]
    try:
        return response, response.json()
    except ValueError:
        return response, None


def call(client, name, arguments, version='2026-07-28'):
    response, data = rpc(client, 'tools/call', {'name': name, 'arguments': arguments}, version)
    if response.status_code != 200:
        raise AssertionError((response.status_code, response.text))
    return data['result']


class MCPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app, base_url='http://localhost:9900')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        app.state.limiter = MagicMock(consume_with_status=MagicMock(
            return_value=RateLimitDecision(0, 9, 59, 1, 60)))

    def test_stalled_body_times_out_before_the_sdk(self):
        sent = []
        async def receive():
            if not sent:
                sent.append('first')
                return {'type': 'http.request', 'body': b'{', 'more_body': True}
            await asyncio.sleep(3600)  # The client never sends the rest.
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': []}
        sdk = MagicMock()
        with patch('mcp_server.MCP_BODY_SECONDS', 0.05):
            asyncio.run(asyncio.wait_for(MCPGateway(MagicMock(state=MagicMock(mcp_http_app=sdk)))(scope, receive, send), 5))
        self.assertEqual(sent[1]['status'], 408)
        sdk.assert_not_called()

    def test_both_protocol_eras_discover_board_and_history_tools(self):
        response, initialized = rpc(self.client, 'initialize', {'protocolVersion': '2025-11-25',
            'clientInfo': {'name': 'legacy', 'version': '1'}, 'capabilities': {}}, '2025-11-25')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(initialized['result']['protocolVersion'], '2025-11-25')
        self.assertNotIn('mcp-session-id', response.headers)
        for version in ['2025-11-25', '2026-07-28']:
            response, data = rpc(self.client, 'tools/list', version=version)
            self.assertEqual(response.status_code, 200)
            tools = {tool['name']: tool for tool in data['result']['tools']}
            self.assertEqual(set(tools), {'create', 'read', 'append', 'wait', 'read_revision', 'events'})
            for tool in tools.values():
                self.assertIn('outputSchema', tool)
                self.assertIn('title', tool)
                self.assertIn('readOnlyHint', tool['annotations'])
            self.assertTrue(tools['create']['annotations']['idempotentHint'])
            self.assertTrue(tools['append']['annotations']['idempotentHint'])
            self.assertNotIn('ctx', tools['wait']['inputSchema']['properties'])

    def test_origin_host_body_size_and_header_validation(self):
        response, _ = rpc(self.client, 'tools/list', headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        response, _ = rpc(self.client, 'tools/list', headers={'Host': 'attacker.example'})
        self.assertEqual(response.status_code, 421)
        response, _ = rpc(self.client, 'tools/list', headers={'Mcp-Method': 'tools/call'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/mcp', content='x' * 65537,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        self.assertEqual(response.status_code, 413)
        response = self.client.options('/mcp', headers={'Origin': 'http://localhost:6600',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'content-type,mcp-method,mcp-name,mcp-protocol-version'})
        self.assertEqual(response.status_code, 200)

    def test_public_mcp_hosts_support_both_protocol_eras(self):
        for host in ('bboard.ai', 'bboard.ai:443', 'api.bboard.ai', 'api.bboard.ai:443'):
            for version in ('2025-06-18', '2026-07-28'):
                with self.subTest(host=host, version=version):
                    response, data = rpc(self.client, 'tools/list', version=version, headers={'Host': host})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(len(data['result']['tools']), 6)

    def control(self, method, headers=None):
        """Bound the ASGI exchange itself: TestClient timeouts do not stop idle SSE."""
        async def exchange():
            messages, received = [], False
            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                await asyncio.Event().wait()
            async def send(message):
                messages.append(message)
            values = {'host': 'localhost:9900', 'accept': 'text/event-stream', **(headers or {})}
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                     'scheme': 'http', 'method': method, 'path': '/mcp', 'raw_path': b'/mcp',
                     'root_path': '', 'query_string': b'', 'client': ('127.0.0.1', 12345),
                     'server': ('localhost', 9900),
                     'headers': [(k.encode(), v.encode()) for k, v in values.items()]}
            async with asyncio.timeout(1):
                await app(scope, receive, send)
            start = next(m for m in messages if m['type'] == 'http.response.start')
            body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
            return start['status'], dict(start['headers']), body
        return self.client.portal.call(exchange)

    def test_control_methods_finish_without_opening_idle_streams(self):
        for version in ('2025-06-18', '2026-07-28'):
            for method in ('GET', 'HEAD', 'DELETE', 'OPTIONS'):
                with self.subTest(version=version, method=method):
                    status, headers, body = self.control(method, {'mcp-protocol-version': version})
                    self.assertEqual(status, 204 if method == 'OPTIONS' else 405)
                    self.assertEqual(headers[b'allow'], b'POST, OPTIONS')
                    self.assertEqual(headers[b'cache-control'], b'no-store')
                    if method in ('HEAD', 'OPTIONS'):
                        self.assertEqual(body, b'')
                    else:
                        self.assertIn('POST', json.loads(body)['error']['message'])

    def test_control_methods_preserve_host_and_origin_checks(self):
        for method in ('GET', 'HEAD', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertEqual(self.control(method, {'host': 'attacker.example',
                    'x-forwarded-host': 'bboard.ai'})[0], 421)
                self.assertEqual(self.control(method, {'origin': 'https://attacker.example'})[0], 403)

    def test_raw_legacy_handshake_and_modern_initialization_contract(self):
        response, data = rpc(self.client, 'initialize', {'protocolVersion': '2025-06-18',
            'clientInfo': {'name': 'example', 'version': '1'}, 'capabilities': {}}, '2025-06-18')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['result']['protocolVersion'], '2025-06-18')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-06-18'}
        notification = self.client.post('/mcp', headers=headers,
            json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(notification.status_code, 202)
        response, data = rpc(self.client, 'tools/list', version='2025-06-18')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data['result']['tools']), 6)
        response, data = rpc(self.client, 'initialize')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(data['error']['code'], -32601)

    def test_errors_limits_and_private_routes(self):
        for name, arguments, recovery in [
                ('read', {'key': 'a' * 64}, 'retry the request'),
                ('create', {'content': 'brief', 'idempotency_key': str(uuid4())},
                 'retry with the same idempotency_key and content'),
                ('append', {'key': 'a' * 64, 'content': 'brief', 'idempotency_key': str(uuid4())},
                 'retry with the same idempotency_key and content')]:
            result = call(self.client, name, arguments)
            self.assertTrue(result['isError'])
            self.assertEqual(result['content'][0]['text'], f'Error executing tool {name}: storage_unavailable; {recovery}')
        for name, arguments in [('read', {'key': 'bad'}),
                ('wait', {'key': 'a' * 64, 'after_revision': True}),
                ('wait', {'key': 'a' * 64, 'after_revision': 0, 'timeout_seconds': 31}),
                ('create', {'content': 'brief'}),
                ('create', {'content': '😀' * 5001}),
                ('append', {'key': 'a' * 64, 'content': 'x', 'idempotency_key': 'bad'})]:
            response, data = rpc(self.client, 'tools/call', {'name': name, 'arguments': arguments})
            self.assertTrue(data.get('error') or data.get('result', {}).get('isError'), data)
        for path in ['/mcp/', '/mcp/private', '/ready']:
            self.assertEqual(self.client.post(path).status_code, 401)
        app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
        response, _ = rpc(self.client, 'tools/list')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '5')


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class MCPPostgresTests(MCPBoundaryTests):
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
            if not connection.execute(text("SELECT to_regclass('app.board_appends')")).scalar():
                connection.execute(text((directory / '006_board_appends.sql').read_text()))
            if not connection.execute(text("SELECT to_regclass('app.board_creations')")).scalar():
                connection.execute(text((directory / '007_board_creations.sql').read_text()))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        super().setUp()
        # Boundary tests deliberately run without storage; only these cases use it.
        if self._testMethodName.startswith('test_pg_'):
            app.state.database.engine = self.engine

    def create(self, content='brief'):
        result = call(self.client, 'create', {'content': content, 'idempotency_key': str(uuid4())})
        self.assertFalse(result.get('isError'), result)
        return result['structuredContent']['key']

    def test_pg_concurrent_appends_retry_after_restart_and_http_visibility(self):
        key = self.create()
        ids = [str(uuid4()), str(uuid4())]
        def append(i):
            return DocumentStore(self.engine, key).append(f'\nAgent {i}', ids[i])
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(append, [0, 1]))
        self.assertEqual(sorted(r['revision'] for r in results), [1, 2])
        current = self.client.get('/' + key).json()
        self.assertIn('Agent 0', current['content'])
        self.assertIn('Agent 1', current['content'])
        # A new store instance proves deduplication is persisted, not session memory.
        duplicate = DocumentStore(self.engine, key).append('\nAgent 0', ids[0])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['revision'], 2)
        with self.assertRaisesRegex(ValueError, 'idempotency_key_reused'):
            DocumentStore(self.engine, key).append('different', ids[0])
        with self.assertRaisesRegex(ValueError, 'content_too_long'):
            DocumentStore(self.engine, key).append('x' * 5000, str(uuid4()))
        self.assertEqual(DocumentStore(self.engine, key).read()['revision'], 2)

    def test_pg_connector_append_on_api_hostname_is_retry_safe(self):
        key = self.create('brief')
        arguments = {'key': key, 'content': '\nreview', 'idempotency_key': str(uuid4())}
        for version, duplicate in [('2025-06-18', False), ('2026-07-28', True)]:
            response, data = rpc(self.client, 'tools/call', {'name': 'append', 'arguments': arguments},
                                 version, headers={'Host': 'api.bboard.ai'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(data['result'].get('isError'), data)
            result = data['result']['structuredContent']
            self.assertEqual(result['duplicate'], duplicate)
            self.assertEqual(result['content'], 'brief\nreview')
            self.assertEqual(result['appended_revision'], 1)
        self.assertEqual(DocumentStore(self.engine, key).read()['revision'], 1)

    def test_pg_wait_observes_other_client_and_browser_receives_append(self):
        key = self.create()
        async def waiter_registered():
            async with asyncio.timeout(3):
                while not app.state.mcp_waiters:
                    await asyncio.sleep(0.01)
        with self.client.websocket_connect('/ws/boards/' + key) as browser:
            self.assertEqual(browser.receive_json()['content'], 'brief')
            with ThreadPoolExecutor(1) as executor:
                future = executor.submit(call, self.client, 'wait',
                    {'key': key, 'after_revision': 0, 'timeout_seconds': 3})
                self.client.portal.call(waiter_registered)
                appended = call(self.client, 'append', {'key': key, 'content': '\nresult',
                    'idempotency_key': str(uuid4())}, '2025-11-25')
                self.assertFalse(appended.get('isError'), appended)
                result = future.result(timeout=5)['structuredContent']
                self.assertTrue(result['changed'])
                self.assertEqual(result['content'], 'brief\nresult')
                self.assertEqual(receive_until(browser, lambda value: value.get('revision') == 1)['content'], 'brief\nresult')
        timed_out = call(self.client, 'wait', {'key': key, 'after_revision': 1, 'timeout_seconds': 0})
        self.assertFalse(timed_out['structuredContent']['changed'])
        self.assertFalse(app.state.mcp_waiters)

    def test_pg_duplicate_concurrency_and_retry_after_replacement(self):
        key = self.create('')
        operation = str(uuid4())
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda _: DocumentStore(self.engine, key).append('once', operation), [0, 1]))
        self.assertEqual(sum(not r['duplicate'] for r in results), 1)
        DocumentStore(self.engine, key).replace('human edit', 1)
        result = call(self.client, 'append', {
            'key': key, 'content': 'once', 'idempotency_key': operation})['structuredContent']
        self.assertEqual(result['content'], 'human edit')
        self.assertEqual(result['appended_revision'], 1)
        self.assertEqual(result['revision'], 2)
        self.assertTrue(result['duplicate'])

    def test_pg_get_append_retry_across_mcp_and_after_browser_edit(self):
        key = self.create('brief')
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        params = {'append': '\nHello + & # % 😀', 'op': str(uuid4())}
        with self.client.websocket_connect('/ws/boards/' + key) as browser:
            browser.receive_json()
            response = self.client.get('https://bboard.ai/' + key, params=params,
                                       headers={'Accept': 'application/json'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.history, [])
            self.assertEqual(response.url.path, '/' + key)
            self.assertFalse(response.json()['duplicate'])
            self.assertEqual(response.json()['content'], 'brief' + params['append'])
            self.assertEqual(receive_until(browser, lambda v: v.get('revision') == 1)['content'], response.json()['content'])
        replay = call(self.client, 'append', {
            'key': key, 'content': params['append'], 'idempotency_key': params['op']})
        self.assertTrue(replay['structuredContent']['duplicate'])
        DocumentStore(self.engine, key).write(str(uuid4()), 1, 'human edit')
        duplicate = self.client.get('/' + key + '/read/after-human-edit', params=params).json()
        self.assertEqual(duplicate['content'], 'human edit')
        self.assertEqual(duplicate['revision'], 2)
        self.assertEqual(duplicate['appended_revision'], 1)
        self.assertTrue(duplicate['duplicate'])
        conflict = self.client.get('/' + key, params={**params, 'append': 'different'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(DocumentStore(self.engine, key).read()['content'], 'human edit')

    def test_pg_get_append_concurrency_and_rejected_writes(self):
        key = self.create('')
        params = {'append': 'once', 'op': str(uuid4())}
        with ThreadPoolExecutor(2) as executor:
            responses = list(executor.map(lambda _: self.client.get('/' + key, params=params).json(), [0, 1]))
        self.assertEqual(sum(not r['duplicate'] for r in responses), 1)
        additions = [{'append': '\nA', 'op': str(uuid4())}, {'append': '\nB', 'op': str(uuid4())}]
        with ThreadPoolExecutor(2) as executor:
            responses = list(executor.map(lambda p: self.client.get('/' + key, params=p).json(), additions))
        current = DocumentStore(self.engine, key).read()
        self.assertEqual(current['revision'], 3)
        self.assertIn('\nA', current['content'])
        self.assertIn('\nB', current['content'])
        for content, status in [('', 422), ('\x00', 422), ('a' * 5000, 413)]:
            response = self.client.get('/' + key, params={'append': content, 'op': str(uuid4())})
            self.assertEqual(response.status_code, status, response.text)
        self.client.head('/' + key, params={'append': 'head', 'op': str(uuid4())})
        self.assertEqual(DocumentStore(self.engine, key).read(), current)

    def test_pg_cancelled_wait_releases_subscription_and_limits(self):
        from mcp import Client
        key = self.create()
        async def exercise():
            async with Client(app.state.mcp) as client:
                task = asyncio.create_task(client.call_tool('wait', {'key': key, 'after_revision': 0}))
                async with asyncio.timeout(3):
                    while not app.state.mcp_waiters:
                        await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            async with asyncio.timeout(3):
                while app.state.mcp_waiters:
                    await asyncio.sleep(0.01)
            self.assertNotIn(key, app.state.board_hubs)
        self.client.portal.call(exercise)

    def test_pg_missing_board_and_wait_limit_do_not_leak_subscriptions(self):
        result = call(self.client, 'wait', {'key': '0' * 64, 'after_revision': 0})
        self.assertTrue(result['isError'])
        self.assertIn('board_not_found', result['content'][0]['text'])
        self.assertFalse(app.state.mcp_waiters)
        app.state.mcp_waiters = {'other': 64}
        result = call(self.client, 'wait', {'key': self.create(), 'after_revision': 0})
        self.assertTrue(result['isError'])
        self.assertIn('too_many_waiters', result['content'][0]['text'])
