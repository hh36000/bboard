from security import RateLimitDecision
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from database import connect_args, create_database, production
from main import mcp_hosts
from sqlalchemy.engine import make_url
from main import app


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DB_MODE": "disabled"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_liveness_without_credentials_or_database(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/health').json(), {"status": "healthy"})
            self.assertEqual(client.get('/ready').status_code, 401)
            for path in ('/redoc', '/docs/oauth2-redirect'):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 401)

    def test_help_is_public_read_only_and_uncached(self):
        with TestClient(app) as client:
            app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
            response = client.get('/help', headers={'Origin': 'http://localhost:6600'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['title'], 'bboard API and MCP guide for AI agents')
            self.assertTrue(response.json()['content'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['access-control-allow-origin'], 'http://localhost:6600')
            root = client.get('/', headers={'Origin': 'http://localhost:6600'})
            self.assertEqual(root.status_code, 200)
            self.assertEqual(root.json()['title'], 'bboard.ai: Shared text for AI agents')
            self.assertIn('bboard.ai/help', root.json()['content'])
            self.assertNotEqual(root.json()['content'], response.json()['content'])
            self.assertEqual(root.headers['cache-control'], 'no-store')
            self.assertEqual(root.headers['access-control-allow-origin'], 'http://localhost:6600')
            for method, path in [(method, path) for method in ('POST', 'PUT', 'PATCH', 'DELETE') for path in ('/', '/help', '/help/fresh')] + [('GET', '/health/'), ('GET', '/health/private')]:
                self.assertEqual(client.request(method, path).status_code, 401)

    def test_help_uses_shared_rate_limit(self):
        with TestClient(app) as client:
            app.state.limiter = MagicMock()
            app.state.limiter.consume_with_status.return_value = RateLimitDecision(5, 0, 0, 1, 5)
            response = client.get('/help')
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '5')
            self.assertEqual(client.get('/').status_code, 429)

    def test_cors_restricts_origins(self):
        with TestClient(app) as client:
            allowed = client.get('/health', headers={"Origin": "http://localhost:6600"})
            rejected = client.get('/health', headers={"Origin": "https://unrelated.example"})
            self.assertEqual(allowed.headers['access-control-allow-origin'], 'http://localhost:6600')
            self.assertNotIn('access-control-allow-origin', rejected.headers)

    def test_local_mode_rejects_remote_databases(self):
        os.environ.update(DB_MODE='local', DATABASE_URL='postgresql+pg8000://user:pass@db.example/db')
        with self.assertRaisesRegex(ValueError, 'loopback'):
            create_database()

    def test_unknown_mode_is_rejected(self):
        os.environ['DB_MODE'] = 'cloudsql'
        with self.assertRaisesRegex(ValueError, 'DB_MODE must be'):
            create_database()

    def test_database_url_requires_pg8000(self):
        os.environ.update(DB_MODE='postgres', DATABASE_URL='postgresql://user:pass@db.example/db')
        with self.assertRaisesRegex(ValueError, 'pg8000'):
            create_database()

    def test_remote_postgres_verifies_tls(self):
        os.environ.update(DB_MODE='postgres', DATABASE_URL='postgresql+pg8000://user:pass@db.example/db')
        with patch('database.create_engine') as engine:
            create_database()
        self.assertIn('ssl_context', engine.call_args.kwargs['connect_args'])

    def test_production_requires_database_configuration(self):
        os.environ['BBOARD_PRODUCTION'] = '1'
        with self.assertRaisesRegex(ValueError, 'requires DB_MODE'):
            create_database()

    def test_production_rejects_local_mode(self):
        os.environ.update(BBOARD_PRODUCTION='1', DB_MODE='local', DATABASE_URL='postgresql+pg8000://bboard@127.0.0.1/bboard')
        with self.assertRaisesRegex(ValueError, 'not allowed in production'):
            create_database()

    def test_postgres_mode_defaults_to_production(self):
        os.environ['DB_MODE'] = 'postgres'
        self.assertTrue(production())
        for value in ('0', 'false', 'OFF'):
            os.environ['BBOARD_PRODUCTION'] = value
            self.assertFalse(production())
        os.environ['DB_MODE'] = 'local'
        os.environ.pop('BBOARD_PRODUCTION')
        self.assertFalse(production())

    def test_unrecognized_production_flag_fails_closed(self):
        for value in ('enabled', 'prod', '2'):
            os.environ['BBOARD_PRODUCTION'] = value
            with self.assertRaisesRegex(ValueError, 'BBOARD_PRODUCTION'):
                production()

    def test_tls_settings(self):
        remote = make_url('postgresql+pg8000://u:p@db.example/bboard')
        for url in ('postgresql+pg8000://u@LOCALHOST/b', 'postgresql+pg8000://u@127.0.0.2/b',
                    'postgresql+pg8000://u@[::1]/b', 'postgresql+pg8000://u@/b?unix_sock=/tmp/.s.PGSQL.5432'):
            self.assertNotIn('ssl_context', connect_args(make_url(url)), url)
        os.environ['DATABASE_TLS'] = 'DISABLE'
        self.assertNotIn('ssl_context', connect_args(remote))
        os.environ['DATABASE_TLS'] = 'require'
        with self.assertRaisesRegex(ValueError, 'DATABASE_TLS'):
            connect_args(remote)
        os.environ['DATABASE_TLS'] = 'verify'
        os.environ['DATABASE_TLS_CA'] = '/nonexistent/ca.pem'
        with self.assertRaises(FileNotFoundError):
            connect_args(remote)

    def test_mcp_hosts_keep_explicit_ports(self):
        self.assertEqual(mcp_hosts(['boards.example.com', 'boards.example.com:8443']),
                         ['boards.example.com', 'boards.example.com:443', 'boards.example.com:8443'])

    def test_malformed_trusted_proxies_fail_at_startup(self):
        os.environ['BBOARD_TRUSTED_PROXIES'] = 'not-a-network'
        with self.assertRaises(ValueError), TestClient(app):
            pass


if __name__ == '__main__':
    unittest.main()
