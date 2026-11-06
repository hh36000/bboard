"""API discovery, version selection, and truthful quota metadata without cloud access."""
import json
import os
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from openapi_spec_validator import validate
from main import app
from security import MemoryLimiter, RateLimitDecision
from routers.schemas import public_spec


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        app.state.limiter = MemoryLimiter()

    def test_success_private_and_failure_responses_charge_one_request(self):
        with patch('security.time.monotonic', return_value=120.25):
            healthy = self.client.get('/health')
            self.assertEqual(healthy.status_code, 200)
            self.assertEqual(healthy.headers['ratelimit-policy'], '"second";q=10;w=1, "minute";q=60;w=60')
            self.assertEqual(healthy.headers['ratelimit'], '"second";r=9;t=1, "minute";r=59;t=60')
            private = self.client.get('/ready')
            self.assertEqual(private.status_code, 401)
            self.assertIn('"minute";r=58;', private.headers['ratelimit'])
            invalid = self.client.post('/write')
            self.assertEqual(invalid.status_code, 415)
            self.assertIn('"minute";r=57;', invalid.headers['ratelimit'])
            self.assertEqual(invalid.headers['api-version'], '1')
            self.assertEqual(invalid.headers['cache-control'], 'no-store')

    def test_burst_rejection_metadata_and_recovery(self):
        with patch('security.time.monotonic', return_value=125.25):
            for _ in range(10):
                self.client.get('/health')
            limited = self.client.get('/health')
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers['retry-after'], '1')
            self.assertEqual(limited.headers['ratelimit'], '"second";r=0;t=1')
            self.assertEqual(limited.headers['ratelimit-remaining'], '0')
            self.assertEqual(limited.headers['ratelimit-reset'], '1')
        with patch('security.time.monotonic', return_value=126.25):
            recovered = self.client.get('/health')
            self.assertEqual(recovered.status_code, 200)
            self.assertIn('"minute";r=48;t=54', recovered.headers['ratelimit'])

    def test_minute_reset_snapshot_and_crossing_both_limits(self):
        limiter = MemoryLimiter()
        for n in range(60):
            with patch('security.time.monotonic', return_value=120+n):
                self.assertEqual(limiter.consume('ip'), 0)
        with patch('security.time.monotonic', return_value=179.25):
            blocked = limiter.consume_with_status('ip')
            self.assertEqual(blocked.retry_after, 1)
            self.assertEqual(blocked.headers()['RateLimit'], '"minute";r=0;t=1')
        with patch('security.time.monotonic', return_value=180.25):
            self.assertEqual(limiter.consume_with_status('ip').minute_remaining, 59)
        # The minute has just reached its quota, but this request was rejected
        # by the second window. Do not advertise a reset beyond Retry-After.
        decision = RateLimitDecision(1, 0, 0, 1, 35)
        self.assertEqual(decision.headers()['RateLimit'], '"second";r=0;t=1')

    def test_storage_failure_never_invents_remaining_quota(self):
        app.state.limiter = MagicMock()
        app.state.limiter.consume_with_status.side_effect = RuntimeError('secret')
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('ratelimit', response.headers)
        self.assertNotIn('ratelimit-remaining', response.headers)
        self.assertEqual(response.headers['retry-after'], '5')
        self.assertNotIn('secret', response.text)

    def test_version_selection_is_real_and_cannot_bypass_access_rules(self):
        with patch('security.time.monotonic', return_value=120.5):
            for version in (None, '1'):
                response = self.client.get('/health', headers={'API-Version': version} if version else {})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['api-version'], '1')
                self.assertNotIn('deprecation', response.headers)
                self.assertNotIn('sunset', response.headers)
            rejected = self.client.post('/write', headers={'API-Version': '2'}, json={'content': 'not saved'})
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.json(), {'detail': 'unsupported_api_version', 'supported_versions': ['1']})
            self.assertIn('"minute";r=57;', rejected.headers['ratelimit'])
            self.assertEqual(self.client.get('/ready', headers={'API-Version': '2'}).status_code, 401)
            self.assertEqual(self.client.get('/health', headers={'API-Version': ''}).status_code, 400)

    def test_cors_allows_version_selection_and_exposes_quota_to_browser(self):
        response = self.client.options('/write', headers={'Origin': 'http://localhost:6600',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'content-type,idempotency-key,api-version'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('idempotency-key', response.headers['access-control-allow-headers'].lower())
        for path in ('/health', '/ready'):
            response = self.client.get(path, headers={'Origin': 'http://localhost:6600'})
            exposed = response.headers['access-control-expose-headers'].lower()
            for name in ('api-version', 'ratelimit', 'ratelimit-policy', 'ratelimit-reset', 'retry-after'):
                self.assertIn(name, exposed)

    def test_catalog_has_standard_media_type_and_links_to_real_api(self):
        for host in ('runtime.test', 'bboard.ai'):
            response = self.client.get('/.well-known/api-catalog', headers={'Host': host})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"')
            api = response.json()['linkset'][0]
            self.assertEqual(api['anchor'], 'https://bboard.ai')
            self.assertEqual(api['service-desc'][0]['href'], 'https://bboard.ai/openapi.json')
            self.assertEqual(api['status'][0]['href'], 'https://bboard.ai/health')
            self.assertEqual(api['service-doc'][0]['href'], 'https://bboard.ai/help')
            self.assertIn('ratelimit', response.headers)
        self.assertEqual(self.client.post('/.well-known/api-catalog').status_code, 401)
        self.assertEqual(self.client.get('/.well-known/api-catalog/private').status_code, 401)

    def test_developer_aliases_point_to_same_help_without_opening_api_docs(self):
        for path in ('/developers', '/developers/', '/docs', '/docs/'):
            response = self.client.get(path, headers={'Host': 'bboard.ai'}, follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers['location'], '/help')
        self.assertEqual(self.client.get('/docs').status_code, 200)
        self.assertEqual(self.client.get('/developers').status_code, 200)
        root = self.client.get('/', headers={'Host': 'bboard.ai'})
        self.assertLess(root.text.index('href="https://bboard.ai/help"'), root.text.index('href="https://bboard.ai/write"'))

    def test_spec_is_valid_self_contained_typed_and_documents_version(self):
        spec = public_spec()
        validate(spec)
        self.assertNotIn('"$ref"', json.dumps(spec))
        for path, methods in spec['paths'].items():
            for method, operation in methods.items():
                version = next(p for p in operation['parameters'] if p['name'] == 'API-Version')
                self.assertEqual(version['schema'], {'type': 'string', 'enum': ['1'], 'default': '1'})
                success = operation['responses'].get('200', operation['responses'].get('201'))
                schema = success['content']['application/json']['schema']
                for variant in schema.get('anyOf', [schema]):
                    self.assertEqual(variant['type'], 'object', (method, path))
                    self.assertTrue(variant['properties'])
                self.assertIn('RateLimit', success['headers'])
                self.assertIn('RateLimit', operation['responses']['429']['headers'])
                self.assertIn('400', operation['responses'])
        self.assertEqual(spec['paths']['/write']['post']['requestBody']['content']['application/json']['schema']['properties']['content']['maxLength'], 5000)
        for path in ('/write', '/boards', '/{board_id}/append'):
            header = next(p for p in spec['paths'][path]['post']['parameters'] if p['name'] == 'Idempotency-Key')
            self.assertTrue(header['required'])
        update = spec['paths']['/{board_id}']['put']
        self.assertEqual(update['operationId'], 'updateBoard')
        body = update['requestBody']['content']['application/json']['schema']
        self.assertEqual(set(body['required']), {'content', 'expected_revision'})
        self.assertFalse(body['additionalProperties'])
        self.assertEqual(body['properties']['content']['maxLength'], 5000)
        self.assertEqual(body['properties']['expected_revision']['type'], 'integer')
        self.assertEqual(body['properties']['expected_revision']['minimum'], 0)
        conflict = update['responses']['409']['content']['application/json']['schema']
        self.assertEqual(set(conflict['properties']), {'detail', 'key', 'content', 'revision'})
        self.assertEqual(conflict['properties']['detail']['const'], 'revision_conflict')
        self.assertNotIn('text/html', update['responses']['200']['content'])

    def test_all_public_instructions_describe_revision_protected_updates(self):
        app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))
        for path in ('/help', '/help.md'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            for term in ('PUT', 'PATCH', 'expected_revision', '409'):
                self.assertIn(term, response.text, path)
            self.assertNotIn('no HTTP', response.text)
        for path in ('/', '/index.md', '/llms.txt'):
            self.assertIn('https://bboard.ai/help', self.client.get(path).text)

    def test_patch_schema_documents_literal_match_errors_and_required_fields(self):
        spec = public_spec()
        operation = spec['paths']['/{board_id}']['patch']
        self.assertEqual(operation['operationId'], 'patchBoard')
        body = operation['requestBody']['content']['application/json']['schema']
        self.assertEqual(set(body['required']), {'old_text', 'new_text', 'expected_revision'})
        self.assertFalse(body['additionalProperties'])
        self.assertEqual(body['properties']['old_text']['minLength'], 1)
        self.assertEqual(body['properties']['expected_revision']['minimum'], 0)
        self.assertEqual(operation['responses']['409'], spec['paths']['/{board_id}']['put']['responses']['409'])
        match_schema = next(s for s in operation['responses']['422']['content']['application/json']['schema']['anyOf'] if 'key' in s['properties'])
        self.assertEqual(set(match_schema['required']), {'detail', 'key', 'content', 'revision'})
        self.assertEqual(set(match_schema['properties']['detail']['enum']), {'old_text_empty', 'old_text_not_found', 'old_text_ambiguous'})
