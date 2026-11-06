"""GET-only clients can discover fresh reads and receive verifiable write receipts."""
import re
import unittest
import os
from unittest.mock import patch
from uuid import uuid4

from main import app
from realtime import BoardNotFound
from routers.pages import HELP_MESSAGE
import test_agent_pages
from test_agent_pages import KEY, SITE, VisibleText


class GetFetchTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_suffix_reads_keep_the_canonical_content_without_redirecting(self):
        value = {'content': '<private> board text', 'revision': 15}
        with patch('realtime.DocumentStore.read', return_value=value) as read:
            for host in ({}, SITE):
                for suffix in ('/', '/1', '/read/new/path', '/docs.json'):
                    for accept in ('text/html', 'text/markdown', 'application/json'):
                        for path in ('/help', '/' + KEY):
                            with self.subTest(host=host, suffix=suffix, accept=accept, path=path):
                                response = self.client.get(path + suffix, headers={**host, 'Accept': accept},
                                                           follow_redirects=False)
                                self.assertEqual(response.status_code, 200, response.text)
                                self.assertNotIn('location', response.headers)
                                self.assertNotIn('notFoundPath', response.text)
                                self.assertNotIn('This board does not exist', response.text)
                                self.assertEqual(response.headers['cache-control'], 'no-store')
                                if accept == 'text/html':
                                    self.assertIn(f'rel="canonical" href="https://bboard.ai{path}"', response.text)
                                if path == '/' + KEY:
                                    self.assertIn('noindex', response.headers['x-robots-tag'])
                                    if accept == 'application/json':
                                        self.assertEqual(response.json(), {'key': KEY, **value})
                                    else:
                                        text = VisibleText(response.text).text if accept == 'text/html' else response.text
                                        self.assertIn(value['content'], text)
                                elif accept == 'application/json':
                                    self.assertEqual(response.json(), HELP_MESSAGE)
            self.assertGreater(read.call_count, 0)

    def test_api_board_and_fresh_links_are_visible_before_board_text(self):
        value = {'content': 'Board text starts here', 'revision': 15}
        with patch('realtime.DocumentStore.read', return_value=value) as read:
            response = self.client.get('/' + KEY, headers=SITE)
            api_url = 'https://bboard.ai/' + KEY
            self.assertLess(response.text.index('Fresh read</a>'), response.text.index(value['content']))
            self.assertIn(api_url, VisibleText(response.text).text)
            api = self.client.get(api_url, headers={'Accept': 'text/html'}, follow_redirects=False)
            self.assertEqual(api.status_code, 200)
            self.assertNotIn('location', api.headers)
            fresh = re.search(r'href="([^"]+)">Fresh read</a>', api.text)[1]
            self.assertTrue(fresh.startswith(api_url + '/read/'))
            self.assertNotIn('?', fresh)
            read.return_value = {'content': 'New text', 'revision': 16}
            updated = self.client.get(fresh, headers={'Accept': 'text/html'}, follow_redirects=False)
            self.assertEqual(updated.status_code, 200)
            self.assertIn('New text', VisibleText(updated.text).text)
            self.assertIn('Current revision: 16.', updated.text)
            self.assertNotEqual(fresh, re.search(r'href="([^"]+)">Fresh read</a>', updated.text)[1])

    def test_aliases_do_not_expose_other_methods_or_missing_boards(self):
        with patch('realtime.DocumentStore.read', side_effect=BoardNotFound), \
                patch('realtime.DocumentStore.append') as append:
            for host in ({}, SITE):
                self.assertEqual(self.client.get('/' + KEY + '/fresh', headers=host).status_code, 404)
                for path in ('/help/fresh', '/' + KEY + '/fresh'):
                    for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                        self.assertEqual(self.client.request(method, path, headers=host).status_code, 401)
                for path in ('/ready/fresh', '/write/fresh', '/' + 'a' * 63 + '/fresh'):
                    self.assertEqual(self.client.get(path, headers=host).status_code, 404 if host else 401)
                params = {'append': 'x', 'op': str(uuid4())}
                self.assertEqual(self.client.head('/' + KEY + '/fresh', params=params, headers=host).status_code, 405)
                self.assertEqual(self.client.get('/' + KEY + '/fresh', params=params,
                    headers={**host, 'Purpose': 'prefetch'}).status_code, 403)
            append.assert_not_called()

    def test_query_errors_identify_fields_without_echoing_values(self):
        operation = str(uuid4())
        cases = [
            ('/write', 'op=' + operation, 'content', 'missing'),
            ('/write', 'content=private&content=again', 'content', 'repeated'),
            ('/write', 'content=private&extra=secret', 'extra', 'unexpected'),
            ('/write', 'content', 'content', 'missing_equals'),
            ('/write', 'content=%FF', 'content', 'invalid_encoding'),
            ('/write', 'content=%ZZ', 'content', 'invalid_encoding'),
            ('/write', 'content=%00&op=' + operation, 'content', 'invalid_content'),
            ('/write', 'content=private&op=', 'op', 'invalid_uuid'),
            ('/write', 'content=private&op=secret', 'op', 'invalid_uuid'),
            ('/' + KEY, 'append=private', 'op', 'missing'),
            ('/' + KEY, 'append=&op=' + operation, 'append', 'empty'),
            ('/' + KEY, 'append=%ZZ&op=' + operation, 'append', 'invalid_encoding'),
        ]
        with patch('realtime.create_board_once') as once, \
                patch('realtime.DocumentStore.append') as append, self.assertLogs('realtime', level='INFO') as logs:
            for host in ({}, SITE):
                for path, query, field, reason in cases:
                    response = self.client.get(path + '?' + query, headers=host)
                    self.assertEqual(response.status_code, 422, response.text)
                    error = response.json()['errors'][0]
                    self.assertEqual((error['field'], error['reason']), (field, reason))
                    self.assertTrue(error['message'])
                    self.assertNotIn('private', response.text)
                    self.assertNotIn('secret', response.text)
                    self.assertNotIn(operation, response.text)
            once.assert_not_called()
            append.assert_not_called()
        diagnostics = '\n'.join(logs.output)
        for private in ('private', 'secret', KEY, operation):
            self.assertNotIn(private, diagnostics)
        self.assertIn('query_bytes=', diagnostics)

    def test_suffix_write_keeps_exact_text_without_redirect(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        content = '\n+ & # % 😀'
        operation = str(uuid4())
        value = {'type': 'snapshot', 'content': content, 'revision': 1, 'appended_revision': 1, 'duplicate': False}
        with patch('realtime.DocumentStore.append', return_value=value) as append:
            response = self.client.get('https://bboard.ai/' + KEY + '/read/fresh',
                params={'append': content, 'op': operation, 'format': 'json'}, follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('location', response.headers)
            self.assertEqual(response.json()['content'], content)
            append.assert_called_once_with(content, operation)

    def test_help_markdown_and_agent_guide_share_the_current_contract(self):
        for path in ('/help.md',):
            response = self.client.get(path)
            self.assertIn(HELP_MESSAGE['content'], response.text)
            for marker in ('PUT /YOUR_BOARD_KEY', 'PATCH /YOUR_BOARD_KEY', 'expected_revision',
                           '/write?content=', '422', 'Idempotency-Key', '/openapi.json'):
                self.assertIn(marker, response.text)
