"""Browser navigation redirects through the real HTTP policy without storage."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from main import app
from realtime import BoardNotFound
from security import RateLimitDecision
import test_agent_pages
from test_agent_pages import KEY, SITE

BROWSER = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}


class BrowserRedirectTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_page_reads_render_directly_and_head_has_no_body(self):
        with patch('realtime.DocumentStore.read', return_value={'content': 'hello', 'revision': 0}):
            for path in ('/', '/help', '/recent', '/' + KEY):
                for method in ('GET', 'HEAD'):
                    response = self.client.request(method, 'https://bboard.ai' + path,
                        headers=BROWSER, follow_redirects=False)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('location', response.headers)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    if method == 'HEAD':
                        self.assertEqual(response.content, b'')
                    else:
                        self.assertIn('src="/app.js"', response.text)

    def test_redirect_lands_on_interactive_frontend_without_a_loop(self):
        with patch('realtime.DocumentStore.read', return_value={'content': 'hello', 'revision': 0}):
            for path in ('/', '/help', '/recent'):
                response = self.client.get('https://bboard.ai' + path, headers=BROWSER)
                self.assertEqual(len(response.history), 0)
                self.assertEqual(str(response.url), 'https://bboard.ai' + path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('src="/app.js"', response.text)
                proxied = self.client.get(path, headers={**BROWSER, **SITE}, follow_redirects=False)
                self.assertEqual(proxied.status_code, 200)

    def test_api_representations_and_non_page_routes_do_not_redirect(self):
        with patch('realtime.DocumentStore.read', return_value={'content': 'hello', 'revision': 0}):
            for accept in ('*/*', 'application/json', 'text/markdown',
                           'application/json, text/html;q=0.5', 'text/html;q=0, */*'):
                for path in ('/', '/help', '/' + KEY):
                    response = self.client.get(path, headers={'Accept': accept}, follow_redirects=False)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('location', response.headers)
            for path in ('/health', '/openapi.json', '/llms.txt', '/help.md', '/index.md'):
                response = self.client.get(path, headers=BROWSER, follow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('location', response.headers)
            for path in ('/ready', '/unknown', '/recent/private'):
                self.assertEqual(self.client.get(path, headers=BROWSER).status_code, 401)
            self.assertEqual(self.client.post('/recent', headers=BROWSER).status_code, 401)

    def test_write_urls_stay_on_api_and_head_cannot_write(self):
        with patch('realtime.DocumentStore.append') as append, patch('realtime.create_board_once') as create:
            for path in ('/' + KEY + '?append=hello', '/' + KEY + '?op=invalid', '/write?content=hello'):
                response = self.client.head(path, headers=BROWSER, follow_redirects=False)
                self.assertEqual(response.status_code, 405)
                self.assertNotIn('location', response.headers)
            append.assert_not_called()
            create.assert_not_called()
            response = self.client.get('/' + KEY + '?append=hello', headers=BROWSER, follow_redirects=False)
            self.assertEqual(response.status_code, 422)

    def test_html_append_returns_saved_text_and_receipt_without_a_read_redirect(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        content = '\n\nClaude: hello world via GET append'
        operation = str(uuid4())
        for origin in ('https://bboard.ai', 'https://runtime.test'):
            for duplicate in (False, True):
                value = {'type': 'snapshot', 'content': 'latest board text', 'revision': 7,
                         'appended_revision': 2, 'duplicate': duplicate}
                with self.subTest(origin=origin, duplicate=duplicate), \
                        patch('realtime.DocumentStore.append', return_value=value) as append, \
                        patch('realtime.DocumentStore.read', return_value={'content': value['content'], 'revision': 7}) as read:
                    response = self.client.get(origin + '/' + KEY,
                                               params={'append': content, 'op': operation}, headers=BROWSER)
                    self.assertEqual(response.history, [])
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(str(response.url).split('?')[0], origin + '/' + KEY)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
                    self.assertIn('noindex', response.headers['x-robots-tag'])
                    self.assertIn('src="/app.js"', response.text)
                    self.assertNotIn('location', response.headers)
                    self.assertIn('latest board text', response.text)
                    self.assertIn(('Already appended' if duplicate else 'Append saved') + ' at revision 2.', response.text)
                    self.assertIn('Current revision: 7.', response.text)
                    self.assertIn('history.replaceState', response.text)
                    append.assert_called_once_with(content, operation)
                    read.assert_not_called()
                    # The receipt's history cleanup removes the query. Reloading
                    # that path, including the operation suffix, only reads.
                    self.client.get('https://bboard.ai' + response.url.path, headers=BROWSER)
                    append.assert_called_once_with(content, operation)
                    read.assert_called_once_with()

    def test_failed_browser_append_does_not_redirect_to_editor(self):
        for failure, status in ((BoardNotFound(), 404), (ValueError('content_too_long'), 413),
                                (ValueError('idempotency_key_reused_with_different_content'), 409),
                                (RuntimeError('storage unavailable'), 503)):
            with self.subTest(status=status), patch('realtime.DocumentStore.append', side_effect=failure):
                response = self.client.get('https://bboard.ai/' + KEY,
                                           params={'append': 'hello', 'op': str(uuid4())},
                                           headers=BROWSER, follow_redirects=False)
                self.assertEqual(response.status_code, status)
                self.assertNotIn('location', response.headers)

    def test_browser_creation_opens_new_board_without_recreating_on_refresh(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        content = 'Claude: created this board via GET write'
        for origin in ('https://bboard.ai', 'https://runtime.test'):
            for duplicate in (False, True):
                board = {'key': KEY, 'path': '/' + KEY, 'url': 'https://bboard.ai/' + KEY,
                         'content': content, 'revision': 0, 'duplicate': bool(duplicate)}
                params = {'content': content, 'op': str(uuid4())}
                with self.subTest(origin=origin, duplicate=duplicate), \
                        patch('realtime.create_board_once', return_value=board) as create_once, \
                        patch('realtime.DocumentStore.read', return_value={'content': content, 'revision': 0}):
                    response = self.client.get(origin + '/write', params=params, headers=BROWSER)
                    self.assertEqual([hop.status_code for hop in response.history],
                                     [303])
                    self.assertEqual(str(response.url), board['url'])
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('src="/app.js"', response.text)
                    self.assertIn(content, response.text)
                    redirect = response.history[-1]
                    self.assertEqual(redirect.headers['location'], board['url'])
                    self.assertEqual(redirect.headers['cache-control'], 'no-store')
                    self.assertEqual(redirect.headers['referrer-policy'], 'no-referrer')
                    self.client.get(str(response.url), headers=BROWSER)
                    create_once.assert_called_once_with(None, content, params['op'])

    def test_redirect_keeps_rate_limits_and_version_validation(self):
        response = self.client.get('/recent', headers={**BROWSER, 'API-Version': '2'}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
        response = self.client.get('/', headers=BROWSER, follow_redirects=False)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '5')
        app.state.limiter.consume_with_status.side_effect = RuntimeError('unavailable')
        response = self.client.get('/', headers=BROWSER, follow_redirects=False)
        self.assertEqual(response.status_code, 503)
