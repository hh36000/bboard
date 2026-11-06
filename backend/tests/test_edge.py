"""A converted HEAD or forged forwarding metadata must never authorize a write."""
import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from main import app
from security import client_key
import test_agent_pages
from test_agent_pages import KEY, SITE

TOKEN = 'isolated-test-edge-token'


class EdgeTests(unittest.TestCase):
    setUp = test_agent_pages.AgentPagesTests.setUp

    def test_cloud_get_writes_fail_closed_without_trusted_edge(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = TOKEN
        headers = [{}, {'X-Bboard-Edge': 'invented'},
                   {'Via': '1.1 Firebase Hosting', 'X-Forwarded-Host': 'bboard.ai'},
                   {'X-Original-Method': 'GET', 'X-Forwarded-Method': 'GET'},
                   {'X-FH-Requested-Host': 'bboard.ai'},
                   {'X-Bboard-Client-IP': '1.2.3.4'}]
        with patch('realtime.create_board_once') as once, \
             patch('realtime.DocumentStore.append') as append:
            for extra in headers:
                for path, params in [('/write', {'content': 'test', 'op': str(uuid4())}),
                                     ('/' + KEY, {'append': 'test', 'op': str(uuid4())}),
                                     ('/' + KEY + '/fresh', {'append': 'test', 'op': str(uuid4())})]:
                    response = self.client.get(path, params=params, headers={**SITE, **extra})
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertNotIn(TOKEN, response.text)
            del os.environ['BBOARD_EDGE_TOKEN']
            self.assertEqual(self.client.get('/write', params={'content': 'test', 'op': str(uuid4())}, headers={
                **SITE, 'X-Bboard-Edge': TOKEN}).status_code, 503)
            once.assert_not_called(); append.assert_not_called()

    def test_edge_authentication_never_bypasses_head_or_preview_guards(self):
        app.state.production = True
        os.environ['BBOARD_EDGE_TOKEN'] = TOKEN
        with patch('realtime.create_board_once') as create, patch('realtime.DocumentStore.append') as append:
            for path, params in [('/write', {'content': 'test', 'op': str(uuid4())}),
                                 ('/' + KEY, {'append': 'test', 'op': str(uuid4())})]:
                headers = {**SITE, 'X-Bboard-Edge': TOKEN}
                response = self.client.head(path, params=params, headers=headers)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.content, b'')
                for marker in ({'Purpose': 'prefetch'}, {'Sec-Purpose': 'prerender'}, {'Sec-Fetch-Dest': 'image'}):
                    self.assertEqual(self.client.get(path, params=params,
                        headers={**headers, **marker}).status_code, 403)
            create.assert_not_called(); append.assert_not_called()

    def test_edge_client_identity_ignores_spoofed_forwarded_chain(self):
        os.environ['BBOARD_EDGE_TOKEN'] = TOKEN
        def scope(token, client, chain):
            return {'client': ('127.0.0.1', 1), 'headers': [(b'x-bboard-edge', token.encode()),
                (b'x-bboard-client-ip', client.encode()), (b'x-forwarded-for', chain.encode())]}
        expected = hashlib.sha256(b'203.0.113.2').hexdigest()
        self.assertEqual(client_key(scope(TOKEN, '203.0.113.2', 'fake, proxy'), True), expected)
        self.assertEqual(client_key(scope(TOKEN, '203.0.113.2', 'other, proxy'), True), expected)
        self.assertNotEqual(client_key(scope('invented', '203.0.113.2', '203.0.113.9'), True), expected)
        self.assertNotEqual(client_key(scope(TOKEN, '203.0.113.2', ''), False), expected)
        self.assertEqual(client_key(scope(TOKEN, 'bad', ''), True), hashlib.sha256(b'unknown').hexdigest())

    def test_unauthenticated_forwarding_headers_never_choose_the_bucket(self):
        os.environ['BBOARD_EDGE_TOKEN'] = TOKEN
        os.environ.pop('BBOARD_TRUSTED_PROXIES', None)
        def scope(chain, token='invented', peer='198.51.100.7'):
            return {'client': (peer, 1), 'headers': [(b'x-bboard-edge', token.encode()),
                (b'x-bboard-client-ip', b'203.0.113.50'), (b'x-forwarded-for', chain.encode())]}
        peer = hashlib.sha256(b'198.51.100.7').hexdigest()
        for production in (True, False):
            for chain in ('203.0.113.1', '203.0.113.2', 'a, 203.0.113.3', ''):
                self.assertEqual(client_key(scope(chain), production), peer)
        # Authenticated proxy identities remain independent of the peer.
        self.assertEqual(client_key(scope('x', TOKEN), True), hashlib.sha256(b'203.0.113.50').hexdigest())

    def test_configured_proxies_may_supply_forwarded_for(self):
        def scope(peer, chain):
            return {'client': (peer, 1), 'headers': [(b'x-forwarded-for', chain.encode())]}
        caller = hashlib.sha256(b'203.0.113.9').hexdigest()
        os.environ['BBOARD_TRUSTED_PROXIES'] = '10.0.0.0/8, 192.0.2.1'
        try:
            self.assertEqual(client_key(scope('10.1.2.3', 'spoof, 203.0.113.9'), True), caller)
            self.assertEqual(client_key(scope('192.0.2.1', '203.0.113.9'), True), caller)
            self.assertNotEqual(client_key(scope('198.51.100.7', '203.0.113.9'), True), caller)
            self.assertNotEqual(client_key(scope('10.1.2.3', '203.0.113.9'), False), caller)
            os.environ['BBOARD_TRUSTED_PROXIES'] = '*'
            self.assertEqual(client_key(scope('198.51.100.7', '203.0.113.9'), True), caller)
        finally:
            del os.environ['BBOARD_TRUSTED_PROXIES']

    def test_ipv6_addresses_in_one_slash_64_share_a_bucket(self):
        def key(address):
            return client_key({'client': (address, 1), 'headers': []}, False)
        self.assertEqual(key('2001:db8:1:2::1'), key('2001:db8:1:2:ffff::9'))
        self.assertNotEqual(key('2001:db8:1:2::1'), key('2001:db8:1:3::1'))
        self.assertEqual(key('::ffff:203.0.113.7'), key('203.0.113.7'))
        self.assertNotEqual(key('203.0.113.7'), key('203.0.113.8'))
