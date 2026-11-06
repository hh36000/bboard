from security import RateLimitDecision
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from main import app
from security import MemoryLimiter, client_key


class SecurityTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_only_exact_health_get_is_public(self):
        with TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            self.assertEqual(client.get('/health').status_code, 200)
            for method, path in [('POST', '/health'), ('GET', '/health/'), ('GET', '/health/secret'),
                                 ('GET', '/ready'), ('GET', '/future-endpoint')]:
                with self.subTest(method=method, path=path):
                    response = client.request(method, path, headers={'Authorization': 'Bearer invented',
                                              'X-Forwarded-User': 'someone@example.com'})
                    self.assertEqual(response.status_code, 401)

    def test_burst_limit_includes_protected_requests_and_preflights(self):
        with patch('security.time.monotonic', return_value=120.1), TestClient(app) as client:
            for _ in range(5):
                self.assertEqual(client.get('/ready').status_code, 401)
            for _ in range(5):
                self.assertEqual(client.options('/health', headers={'Origin': 'http://localhost:6600',
                    'Access-Control-Request-Method': 'GET'}).status_code, 200)
            response = client.get('/health', headers={'Origin': 'http://localhost:6600'})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['Retry-After'], '1')
            self.assertEqual(response.headers['access-control-allow-origin'], 'http://localhost:6600')

    def test_preflight_does_not_bypass_private_routes(self):
        with TestClient(app) as client:
            response = client.options('/ready', headers={'Origin': 'http://localhost:6600',
                                                       'Access-Control-Request-Method': 'GET'})
            self.assertEqual(response.status_code, 401)

    def test_minute_limit_recovers_and_ips_are_independent(self):
        limiter = MemoryLimiter()
        for second in range(60):
            with patch('security.time.monotonic', return_value=120 + second):
                self.assertEqual(limiter.consume('one'), 0)
        with patch('security.time.monotonic', return_value=179.5):
            self.assertEqual(limiter.consume('one'), 1)
            self.assertEqual(limiter.consume('two'), 0)
        with patch('security.time.monotonic', return_value=180):
            self.assertEqual(limiter.consume('one'), 0)

    def test_forwarded_ip_spoofing_cannot_change_bucket(self):
        def scope(value):
            return {'client': ('127.0.0.1', 1234), 'headers': [(b'x-forwarded-for', value)]}
        self.assertEqual(client_key(scope(b'1.2.3.4'), False), client_key(scope(b'8.8.8.8'), False))
        self.assertEqual(client_key(scope(b'1.2.3.4, 203.0.113.8'), True),
                         client_key(scope(b'8.8.8.8, 203.0.113.8'), True))
        self.assertEqual(client_key(scope(b'bad-ip'), True), client_key(scope(b''), True))

    def test_storage_failure_denies_even_public_health(self):
        with TestClient(app) as client:
            app.state.limiter = MagicMock()
            app.state.limiter.consume_with_status.side_effect = RuntimeError('secret connection details')
            response = client.get('/health')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'detail': 'temporarily_unavailable'})

    def test_websockets_are_closed_by_default(self):
        from starlette.websockets import WebSocketDisconnect
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/health'):
                    pass
