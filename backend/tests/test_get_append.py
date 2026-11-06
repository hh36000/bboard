"""GET writes must be intentional, discoverable, and safe to retry."""
import unittest
import os
from unittest.mock import MagicMock, patch
from uuid import uuid4

from main import app
from realtime import BoardNotFound
from security import RateLimitDecision
import test_agent_pages
from test_agent_pages import KEY, SITE, VisibleText


class GetAppendTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_both_hosts_and_representations_return_receipts(self):
        content = '\nHello + & # % 😀'
        operation = str(uuid4())
        value = {'type': 'snapshot', 'content': 'before' + content, 'revision': 2,
                 'appended_revision': 2, 'duplicate': False}
        for host in ({}, SITE):
            for accept in ('application/json', 'text/html', 'text/markdown'):
                with self.subTest(host=host, accept=accept), patch('realtime.DocumentStore.append', return_value=value) as append:
                    hub = MagicMock()
                    app.state.board_hubs[KEY] = hub
                    response = self.client.get('/' + KEY, params={'append': content, 'op': operation},
                                               headers={**host, 'Accept': accept}, follow_redirects=False)
                    self.assertEqual(response.status_code, 200, response.text)
                    append.assert_called_once_with(content, operation)
                    hub.publish.assert_called_once_with({k: value[k] for k in ('type', 'content', 'revision')})
                    app.state.board_hubs.clear()
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertIn('noindex', response.headers['x-robots-tag'])
                    self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
                    if accept == 'application/json':
                        self.assertEqual(response.json(), {'key': KEY, **{k: v for k, v in value.items() if k != 'type'}})
                    elif accept == 'text/markdown':
                        self.assertIn('Append saved at revision 2.', response.text)
                    if accept == 'text/html':
                        self.assertNotIn('location', response.headers)
                        self.assertIn('Append saved at revision 2.', response.text)
                        self.assertIn(value['content'], VisibleText(response.text).text)

    def test_duplicate_returns_current_board_without_broadcasting(self):
        value = {'type': 'snapshot', 'content': 'later human edit', 'revision': 7,
                 'appended_revision': 2, 'duplicate': True}
        hub = MagicMock()
        app.state.board_hubs[KEY] = hub
        try:
            with patch('realtime.DocumentStore.append', return_value=value):
                response = self.client.get('/' + KEY, params={'append': 'old addition', 'op': str(uuid4())},
                                           headers={**SITE, 'Accept': 'text/markdown'})
            self.assertIn('Already appended at revision 2.', response.text)
            self.assertIn('Current revision: 7.', response.text)
            self.assertIn('later human edit', response.text)
            hub.publish.assert_not_called()
        finally:
            app.state.board_hubs.clear()

    def test_legacy_hosting_cannot_lose_head_method_and_append(self):
        app.state.production = True
        params = {'append': 'must not be written', 'op': str(uuid4())}
        with patch('realtime.DocumentStore.append') as append:
            for method in ('GET', 'HEAD'):
                response = self.client.request(method, '/' + KEY, params=params,
                    headers={**SITE, 'X-FH-Requested-Host': 'bboard.ai'}, follow_redirects=False)
                self.assertEqual(response.status_code, 503 if method == 'GET' else 405)
                self.assertNotIn('location', response.headers)
            append.assert_not_called()

    def test_public_get_appends_directly_and_head_cannot_write(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        params = {'append': 'hello', 'op': str(uuid4()), 'format': 'json'}
        value = {'type': 'snapshot', 'content': 'hello', 'revision': 1, 'appended_revision': 1, 'duplicate': False}
        with patch('realtime.DocumentStore.append', return_value=value) as append:
            self.assertEqual(self.client.head('/' + KEY, params=params, headers=SITE).status_code, 405)
            append.assert_not_called()
            response = self.client.get('/' + KEY, params=params, headers=SITE, follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('location', response.headers)
            self.assertEqual(response.json()['content'], 'hello')
            append.assert_called_once_with('hello', params['op'])

    def test_invalid_queries_and_non_writing_methods_never_append(self):
        operation = str(uuid4())
        queries = [
            ('append=x', 422), ('op=' + operation, 422), ('append=x&op=bad', 422),
            (f'append=x&append=y&op={operation}', 422), (f'append=x&op={operation}&op={operation}', 422),
            (f'append=x&op={operation}&extra=y', 422), (f'append=%FF&op={operation}', 422),
            (f'append=%ZZ&op={operation}', 422), (f'append=%&op={operation}', 422),
            ('append=' + 'a' * 8192 + '&op=' + operation, 414),
        ]
        with patch('realtime.DocumentStore.append') as append:
            for host in ({}, SITE):
                for query, status in queries:
                    response = self.client.get('/' + KEY + '?' + query, headers=host)
                    self.assertEqual(response.status_code, status, response.text)
                params = {'append': 'hello', 'op': operation}
                response = self.client.head('/' + KEY, params=params, headers=host)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.content, b'')
                for header, value in [('Purpose', 'prefetch'), ('Sec-Purpose', 'prefetch;prerender'),
                                      ('X-Moz', 'prefetch'), ('X-Purpose', 'preview'), ('Sec-Fetch-Dest', 'image')]:
                    response = self.client.get('/' + KEY, params=params, headers={**host, header: value})
                    self.assertEqual(response.status_code, 403)
                response = self.client.get('/' + KEY, params=params, headers={**host, 'Accept': 'image/png'})
                self.assertEqual(response.status_code, 406)
                response = self.client.get('/' + KEY, params=params, headers={**host, 'API-Version': '2'})
                self.assertEqual(response.status_code, 400)
            append.assert_not_called()

    def test_validation_storage_and_rate_limit_errors(self):
        for failure, status in [(BoardNotFound(), 404), (ValueError('content_empty'), 422),
                (ValueError('invalid_content'), 422), (ValueError('content_too_long'), 413),
                (ValueError('idempotency_key_reused_with_different_content'), 409),
                (RuntimeError('private database details'), 503)]:
            with patch('realtime.DocumentStore.append', side_effect=failure):
                for host in ({}, SITE):
                    response = self.client.get('/' + KEY, params={'append': 'x', 'op': str(uuid4())}, headers=host)
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn('private database details', response.text)
                    self.assertIn('noindex', response.headers['x-robots-tag'])
        with patch('realtime.DocumentStore.append') as append:
            app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
            response = self.client.get('/' + KEY, params={'append': 'x', 'op': str(uuid4())})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '5')
            append.assert_not_called()

    def test_board_link_explains_writing_without_executable_examples(self):
        with patch('realtime.DocumentStore.read', return_value={'content': 'this is a bboard', 'revision': 0}):
            response = self.client.get('/' + KEY, headers=SITE)
        visible = VisibleText(response.text).text
        self.assertIn('?append=YOUR_URL_ENCODED_TEXT&op=YOUR_NEW_UUID', visible)
        self.assertIn('new UUIDv4', visible)
        self.assertIn('Current revision: 0.', visible)
        self.assertIn(f'href="https://bboard.ai/{KEY}"', response.text)
        self.assertIn('href="/help"', response.text)
        self.assertNotRegex(response.text, r'href="[^"]*\?append=')
        for path in ('/help', '/', '/llms.txt'):
            response = self.client.get(path, headers=SITE)
            self.assertIn('https://bboard.ai/help' if path == '/' else '?append=', response.text)
            self.assertNotRegex(response.text, r'href="[^"]*\?append=')
        robots = self.client.get('/robots.txt').text
        self.assertNotIn('Disallow: /*?*append=', robots)
        self.assertNotIn('Disallow: /*?*op=', robots)

    def test_openapi_describes_query_write_and_receipts(self):
        operation = self.client.get('/openapi.json').json()['paths']['/{board_id}']['get']
        parameters = {p['name']: p for p in operation['parameters']}
        self.assertEqual(parameters['op']['schema']['format'], 'uuid')
        self.assertEqual(parameters['append']['schema']['minLength'], 1)
        self.assertIn('appended_revision', str(operation['responses']['200']))
        self.assertIn('409', operation['responses'])
