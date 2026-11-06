"""Exercise domain parity through real dispatch and the shared access boundary."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.routing import APIRoute
from main import app
from realtime import BoardNotFound
from routing import public
from routers.schemas import public_spec
import test_agent_pages
from test_agent_pages import KEY


class RouteParityTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_registered_public_route_drives_access_and_discovery(self):
        @public
        def probe():
            return {'available': True}
        route = APIRoute('/contract-probe', endpoint=probe, methods=['GET'])
        app.router.routes.insert(0, route)
        try:
            self.assertIn('/contract-probe', public_spec()['paths'])
            for host in ('bboard.ai', 'runtime.test'):
                self.assertEqual(self.client.get('https://' + host + '/contract-probe').json(), {'available': True})
                self.assertEqual(self.client.post('https://' + host + '/contract-probe').status_code, 401)
            probe.bboard_public = False
            self.assertNotIn('/contract-probe', public_spec()['paths'])
            self.assertEqual(self.client.get('/contract-probe').status_code, 401)
            self.assertEqual(self.client.get('/contract-probe', headers={'Host': 'bboard.ai'}).status_code, 404)
            self.assertEqual(self.client.options('/contract-probe', headers={
                'Origin': 'http://localhost:6600', 'Access-Control-Request-Method': 'GET'}).status_code, 401)
        finally:
            app.router.routes.remove(route)

    def test_public_pages_and_aliases_work_on_either_host(self):
        for host in ('bboard.ai', 'runtime.test'):
            for path in ('/health', '/recent', '/docs', '/developers', '/api'):
                response = self.client.get('https://' + host + path, headers={'Accept': 'application/json'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('application/json', response.headers['content-type'])

    def test_format_selects_the_same_read_without_headers_or_editor_changes(self):
        with patch('realtime.DocumentStore.read', return_value={'content': 'saved text', 'revision': 7}):
            for host in ('bboard.ai', 'runtime.test'):
                for path in ('/', '/help', '/help/read/fresh', '/' + KEY, '/' + KEY + '/read/fresh'):
                    for fmt, media in (('json', 'application/json'), ('markdown', 'text/markdown')):
                        response = self.client.get('https://' + host + path + '?format=' + fmt,
                            headers={'Accept': 'text/html'}, follow_redirects=False)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(media, response.headers['content-type'])
                        self.assertNotIn('location', response.headers)
            browser = self.client.get('https://bboard.ai/' + KEY, headers={'Accept': 'text/html'})
            self.assertIn('src="/app.js"', browser.text)
            self.assertIn('"path":"/' + KEY, browser.text.replace(' ', ''))

    def test_bare_creation_is_help_and_never_hydrates_as_a_missing_board(self):
        with patch('realtime.create_board_once') as once:
            for host in ('bboard.ai', 'runtime.test'):
                for query in ('', '?format=json', '?format=markdown', '?format=html'):
                    response = self.client.get('https://' + host + '/write' + query)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('/write?content=', response.text)
                    self.assertNotIn('src="/app.js"', response.text)
                self.assertEqual(self.client.head('https://' + host + '/write').status_code, 200)
                self.assertEqual(self.client.get('https://' + host + '/write?op=' + str(uuid4())).status_code, 422)
            once.assert_not_called()

    def test_get_write_format_survives_hosting_without_changing_the_operation(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = 'test-edge-token'
        self.client.headers['X-Bboard-Edge'] = 'test-edge-token'
        op = str(uuid4())
        text = '\n+ & # % 😀'
        board = {'key': KEY, 'path': '/' + KEY, 'url': 'https://bboard.ai/' + KEY,
                 'content': text, 'revision': 1, 'duplicate': False}
        with patch('realtime.create_board_once', return_value=board) as create, \
                patch('realtime.DocumentStore.append', return_value={**board, 'appended_revision': 1}) as append:
            for path, params, operation in (
                ('/write', {'content': text, 'op': op}, create),
                ('/' + KEY, {'append': text, 'op': op}, append),
            ):
                for fmt, media in (('json', 'application/json'), ('markdown', 'text/markdown')):
                    operation.reset_mock()
                    target = 'https://bboard.ai' + path
                    query = {**params, 'format': fmt}
                    self.assertEqual(self.client.head(target, params=query).status_code, 405)
                    self.assertEqual(self.client.get(target, params=query, headers={'Sec-Purpose': 'preview'}).status_code, 403)
                    operation.assert_not_called()
                    saved = self.client.get(target, params=query, headers={'Accept': 'image/png'}, follow_redirects=False)
                    self.assertNotIn('location', saved.headers)
                    self.assertEqual(saved.status_code, 200)
                    self.assertIn(media, saved.headers['content-type'])
                    operation.assert_called_once()
                    self.assertEqual(operation.call_args.args[-2:], (text, op))

    def test_invalid_formats_never_write_and_do_not_echo_values(self):
        op = str(uuid4())
        with patch('realtime.create_board_once') as create, patch('realtime.DocumentStore.append') as append:
            for host in ('bboard.ai', 'runtime.test'):
                for query in ('format=', 'format=secret', 'format=json&format=markdown'):
                    for path in ('/write?content=hello&op=' + op + '&', '/' + KEY + '?append=hello&op=' + op + '&'):
                        response = self.client.get('https://' + host + path + query)
                        self.assertEqual(response.status_code, 422)
                        self.assertEqual(response.json()['errors'][0]['field'], 'format')
                        self.assertNotIn('secret', response.text)
            create.assert_not_called()
            append.assert_not_called()

    def test_malformed_keys_and_wrong_methods_are_never_public(self):
        for path in ('/not-a-key', '/' + KEY[:-1], '/' + KEY.upper(), '/' + KEY + '/read/fresh'):
            self.assertEqual(self.client.put(path, json={}).status_code, 401)
            self.assertEqual(self.client.options(path, headers={'Origin': 'http://localhost:6600',
                'Access-Control-Request-Method': 'PUT'}).status_code, 401)

    def test_read_errors_are_predictable_before_storage_and_across_hosts(self):
        with patch('realtime.DocumentStore.read', side_effect=BoardNotFound) as read:
            for host in ('bboard.ai', 'runtime.test'):
                url = 'https://' + host + '/' + KEY
                read.reset_mock()
                self.assertEqual(self.client.get(url + '?format=wrong').status_code, 422)
                self.assertEqual(self.client.get(url, headers={'Accept': 'image/png'}).status_code, 406)
                read.assert_not_called()
                missing = self.client.get(url + '?format=json')
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(missing.json(), {'detail': 'board_not_found'})
