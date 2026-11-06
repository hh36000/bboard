from security import RateLimitDecision
"""No credentials or real storage: exercise the complete HTTP boundary."""
import json
import os
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from fastapi.routing import iter_route_contexts
from openapi_spec_validator import validate
from main import app
from realtime import BoardNotFound
from routers.pages import HOME_MESSAGE, HELP_MESSAGE, SEARCH_METADATA, negotiate, render_html
from routers.schemas import public_spec

KEY = 'a' * 64
SITE = {'Host': 'bboard.ai'}


class VisibleText(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.hidden = 0
        self.text = ''
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'):
            self.hidden -= 1
    def handle_data(self, text):
        if not self.hidden:
            self.text += text


class AgentPagesTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        app.state.limiter = MagicMock(consume_with_status=MagicMock(return_value=RateLimitDecision(0, 9, 59, 1, 60)))

    def test_home_and_help_are_readable_without_javascript(self):
        for path, message in [('/', HOME_MESSAGE), ('/help', HELP_MESSAGE), ('/help/', HELP_MESSAGE)]:
            response = self.client.get(path, headers=SITE)
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/html', response.headers['content-type'])
            self.assertIn('https://bboard.ai/help', response.text)
            self.assertIn(message['title'], VisibleText(response.text).text)
            self.assertEqual(response.text.count('<h1>'), 1)
            self.assertIn('src="/app.js"', response.text)
            self.assertIn('href="https://bboard.ai/openapi.json"', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_public_guides_do_not_publish_the_operator_identity(self):
        for path in ('/help', '/help.md', '/llms.txt'):
            for headers in ({}, SITE):
                text = self.client.get(path, headers=headers).text.lower()
                self.assertNotIn('github.com/', text, path)

    def test_metadata_and_identity_are_accurate(self):
        text = self.client.get('/', headers=SITE).text
        self.assertIn('<html lang="en">', text)
        self.assertIn('<link rel="canonical" href="https://bboard.ai/"', text)
        self.assertIn('property="og:type" content="website"', text)
        self.assertIn('property="og:image" content="https://bboard.ai/images/bb-logo.png"', text)
        data = json.loads(re.search(r'<script id="page-structured-data" type="application/ld\+json">(.*?)</script>', text).group(1))
        website, software = data['@graph']
        self.assertEqual(website['@type'], 'WebSite')
        self.assertEqual(software['@type'], 'SoftwareApplication')
        self.assertEqual(software['url'], 'https://bboard.ai/')
        self.assertNotIn('address', software)
        self.assertNotIn('aggregateRating', software)

    def test_public_pages_use_the_shared_search_metadata_without_javascript(self):
        for path, expected in SEARCH_METADATA.items():
            response = self.client.get(path, headers=SITE)
            self.assertIn(f'<title>{expected["title"]}</title>', response.text)
            self.assertIn(f'name="description" content="{expected["description"]}"', response.text)
            self.assertIn(f'property="og:description" content="{expected["description"]}"', response.text)
            self.assertIn(f'name="twitter:description" content="{expected["description"]}"', response.text)
            self.assertIn('name="robots" content="index, follow"', response.text)
            self.assertNotIn('x-robots-tag', response.headers)
            data = json.loads(re.search(r'<script id="page-structured-data" type="application/ld\+json">(.*?)</script>', response.text).group(1))
            self.assertEqual(data, expected['structuredData'])
        guide = self.client.get('/help/read/fresh', headers=SITE).text
        self.assertIn('href="https://bboard.ai/help"', guide)
        self.assertIn('"@type": "TechArticle"', guide)

    def test_non_html_duplicates_point_to_the_canonical_landing_page(self):
        for path, canonical in [('/index.md', '/'), ('/help.md', '/help'),
                                ('/?format=json', '/'), ('/?format=markdown', '/'),
                                ('/help/read/fresh?format=markdown', '/help')]:
            response = self.client.get(path, headers=SITE)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'<https://bboard.ai{canonical}>; rel="canonical"', response.headers['link'])
            self.assertIn('rel="api-catalog"', response.headers['link'])

    def test_write_instructions_are_usable_but_not_a_search_landing_page(self):
        response = self.client.get('/write', headers=SITE)
        self.assertEqual(response.status_code, 200)
        self.assertIn('noindex', response.headers['x-robots-tag'])
        self.assertIn('name="robots" content="noindex, noarchive"', response.text)
        self.assertNotIn('application/ld+json', response.text)

    def test_api_and_site_pages_load_brand_images_from_hosting(self):
        for headers in ({'Host': 'runtime.test'}, SITE):
            response = self.client.get('/write', headers={**headers, 'Accept': 'text/html'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('<img id="initial-logo" src="https://bboard.ai/images/bb-logo.png"', response.text)
            self.assertIn('href="https://bboard.ai/images/favicon.png"', response.text)
            self.assertIn('href="https://bboard.ai/images/favicon.svg"', response.text)
            # Cross-origin CSS masks require CORS; ordinary images do not.
            self.assertNotIn('mask:url(', response.text)
            self.assertNotIn('crossorigin', response.text)
            self.assertIn('filter:brightness(0) invert(1)', response.text)
            self.assertIn(':root.light #initial-logo{filter:brightness(0)}', response.text)

    def test_recent_page_direct_load_is_local_only_and_not_indexed(self):
        for path in ('/recent', '/recent/'):
            response = self.client.get(path, headers=SITE)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Recent bboards', response.text)
            self.assertIn('src="/app.js"', response.text)
            self.assertIn('href="/recent"', response.text)
            self.assertIn('noindex', response.headers['x-robots-tag'])
            self.assertNotIn('application/ld+json', response.text)
            self.assertNotIn('notFoundPath', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/recent').json()['title'], 'Recent bboards')
        self.assertEqual(self.client.get('/recent/private', headers=SITE).status_code, 404)

    def test_unknown_site_paths_return_real_markdown_404(self):
        for path in ['/not-a-page', '/not/a/page', '/ready', '/boards/help', '/not-a-file.txt']:
            response = self.client.get(path, headers={**SITE, 'Accept': 'text/markdown'})
            self.assertEqual(response.status_code, 404, path)
            self.assertIn('text/markdown', response.headers['content-type'])
            self.assertIn('/llms.txt', response.text)
            self.assertNotIn(HELP_MESSAGE['content'], response.text)
            self.assertIn('Not found (404)', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/ready').status_code, 401)
        self.assertEqual(self.client.get('/future-endpoint').status_code, 401)

    def test_spoofed_site_headers_never_dispatch_to_private_handler(self):
        self.assertNotIn('/ready', [route.path for route in iter_route_contexts(app.router.routes)])
        for header in ('Host', 'X-Forwarded-Host', 'X-FH-Requested-Host'):
            self.assertEqual(self.client.get('/ready', headers={header: 'bboard.ai'}).status_code, 404)
            self.assertEqual(self.client.post('/ready', headers={header: 'bboard.ai'}).status_code, 401)
        # Real API handlers stay denied, even through the site host.
        for method in ('post', 'put', 'patch', 'delete'):
            self.assertEqual(getattr(self.client, method)('/help', headers=SITE).status_code, 401)

    def test_site_host_aliases_and_unknown_hosts(self):
        for host in ('bboard.ai', 'www.bboard.ai'):
            self.assertIn('text/html', self.client.get('/', headers={'Host': host}).headers['content-type'])
        self.assertIn('application/json', self.client.get('/', headers={'Host': 'bboard.ai.attacker.example'}).headers['content-type'])

    def test_api_json_defaults_and_markdown_negotiation(self):
        home = {**HOME_MESSAGE, 'content': HOME_MESSAGE['content'].rstrip() + '\n\nbboards in existence: temporarily unavailable\n'}
        for path, message in [('/', home), ('/help', HELP_MESSAGE)]:
            self.assertEqual(self.client.get(path).json(), message)
            for site_headers in ({}, SITE):
                response = self.client.get(path, headers={**site_headers, 'Accept': 'text/markdown'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('text/markdown', response.headers['content-type'])
                self.assertEqual(response.text, f"# {message['title']}\n\n{message['content']}")
                self.assertTrue({'accept', 'accept-encoding'} <= {v.strip().lower() for v in response.headers['vary'].split(',')})
        response = self.client.get('/help', headers={**SITE, 'Accept': 'image/png'})
        self.assertEqual(response.status_code, 406)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_head_matches_get_status_type_and_vary_without_body(self):
        for path in ('/', '/help', '/robots.txt', '/llms.txt', '/sitemap.xml', '/openapi.json', '/help.md', '/index.md', '/missing'):
            response = self.client.head(path, headers={**SITE, 'Accept': 'text/markdown'})
            get = self.client.get(path, headers={**SITE, 'Accept': 'text/markdown'})
            self.assertEqual(response.status_code, get.status_code)
            self.assertEqual(response.headers['content-type'], get.headers['content-type'])
            self.assertEqual(response.headers['vary'], get.headers['vary'])
            self.assertEqual(response.content, b'')

    def test_board_content_is_readable_escaped_and_not_indexable(self):
        content = '</script><img src=x onerror=alert(1)> & "hello"'
        with patch('realtime.DocumentStore.read', return_value={'content': content, 'revision': 7}):
            response = self.client.get('/' + KEY, headers=SITE)
            self.assertEqual(response.status_code, 200)
            self.assertIn(content, VisibleText(response.text).text)
            self.assertIn('Current revision: 7.', VisibleText(response.text).text)
            self.assertIn(f'href="https://bboard.ai/{KEY}"', response.text)
            self.assertNotIn('<img src=x', response.text)
            self.assertIn('noindex', response.headers['x-robots-tag'])
            self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
            self.assertNotIn('application/ld+json', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            markdown = self.client.get('/' + KEY, headers={**SITE, 'Accept': 'text/markdown'})
            self.assertEqual(markdown.text, content)
            api = self.client.get('/' + KEY)
            self.assertEqual(api.json(), {'key': KEY, 'content': content, 'revision': 7})
            self.assertIn('noindex', api.headers['x-robots-tag'])

    def test_missing_and_unavailable_boards_have_correct_status(self):
        with patch('realtime.DocumentStore.read', side_effect=BoardNotFound):
            self.assertEqual(self.client.get('/' + KEY, headers=SITE).status_code, 404)
            self.assertEqual(self.client.get('/' + KEY).status_code, 404)
        with patch('realtime.DocumentStore.read', side_effect=RuntimeError('secret database info')):
            response = self.client.get('/' + KEY, headers=SITE)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('secret', response.text)

    def test_bootstrap_cannot_inject_markup_or_replace_tokens(self):
        message = {'title': '$& <title>', 'content': '</script><script>alert(1)</script> & $&'}
        text = render_html(message, '/help')
        self.assertNotIn('<script>alert', text)
        seed = json.loads(re.search(r'<script id="initial-message" type="application/json">(.*?)</script>', text).group(1))
        self.assertEqual(seed['content'], message['content'])
        self.assertIn('$&amp; &lt;title&gt;', text)

    def test_discovery_uses_configured_site_url(self):
        app.state.board_site_url = 'https://boards.example.com'
        self.assertIn('Sitemap: https://boards.example.com/sitemap.xml', self.client.get('/robots.txt').text)
        self.assertIn('<loc>https://boards.example.com/help</loc>', self.client.get('/sitemap.xml').text)
        catalog = self.client.get('/.well-known/api-catalog').json()['linkset'][0]
        self.assertEqual(catalog['anchor'], 'https://boards.example.com')
        self.assertNotIn('bboard.ai', self.client.get('/.well-known/api-catalog').text)
        self.assertEqual(self.client.get('/openapi.json').json()['externalDocs']['url'], 'https://boards.example.com/help')
        for path in ('/robots.txt', '/llms.txt', '/help', '/help.md', '/openapi.json', '/' + KEY):
            link = self.client.get(path, headers=SITE).headers['link']
            self.assertIn('<https://boards.example.com/.well-known/api-catalog>', link, path)
            self.assertNotIn('bboard.ai', link, path)

    def test_machine_readable_files_on_both_hosts(self):
        for headers, origin in [(SITE, 'https://bboard.ai'), ({}, 'https://bboard.ai')]:
            robots = self.client.get('/robots.txt', headers=headers)
            self.assertTrue(robots.headers['content-type'].startswith('text/plain'))
            parser = RobotFileParser()
            parser.parse(robots.text.splitlines())
            for agent in ('Claude-User', 'ClaudeBot', 'GPTBot', 'OAI-SearchBot', 'Googlebot', 'Bingbot', 'generic-client'):
                self.assertTrue(parser.can_fetch(agent, origin + '/help'))
                self.assertTrue(parser.can_fetch(agent, origin + '/' + KEY))
                self.assertTrue(parser.can_fetch(agent, origin + '/write'))
                self.assertTrue(parser.can_fetch(agent, origin + '/write?content=Hello'))
                self.assertTrue(parser.can_fetch(agent, origin + '/' + KEY + '?append=Hello&op=00000000-0000-0000-0000-000000000001'))
            self.assertEqual(parser.site_maps(), [origin + '/sitemap.xml'])
            sitemap = self.client.get('/sitemap.xml', headers=headers)
            self.assertTrue(sitemap.headers['content-type'].startswith('application/xml'))
            root = ElementTree.fromstring(sitemap.content)
            ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
            self.assertEqual([item.text for item in root.findall('s:url/s:loc', ns)], [origin+'/', origin+'/help'])
            self.assertEqual(len(root.findall('s:url/s:lastmod', ns)), 0)
            self.assertNotIn(KEY, sitemap.text)
            guide = self.client.get('/llms.txt', headers=headers)
            self.assertEqual(guide.status_code, 200)
            self.assertTrue(guide.text.startswith('# bboard.ai\n\n> '))
            self.assertIn('When to use bboard:', guide.text)
            self.assertIn('/write', guide.text)
            self.assertIn('Idempotency-Key', guide.text)
            self.assertIn('/help.md', guide.text)
            self.assertIn('/help.md', guide.text)
            self.assertLess(len(guide.text), len(HELP_MESSAGE['content']))
            for path, message in [('/help.md', HELP_MESSAGE), ('/index.md', HOME_MESSAGE)]:
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertIn('text/markdown', response.headers['content-type'])
                self.assertIn(message['content'], response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_openapi_validates_and_only_lists_public_contract(self):
        response = self.client.get('/openapi.json')
        self.assertEqual(response.status_code, 200)
        spec = response.json()
        validate(spec)
        self.assertEqual(spec['servers'][0]['url'], 'https://bboard.ai')
        self.assertEqual(set(spec['paths']), {'/', '/help', '/health', '/write', '/boards', '/{board_id}', '/{board_id}/append', '/{board_id}/wait', '/{board_id}/events', '/{board_id}/revisions/{revision}'})
        operation_ids = []
        for methods in spec['paths'].values():
            for operation in methods.values():
                operation_ids.append(operation['operationId'])
                self.assertTrue(operation['description'])
                self.assertEqual(operation['security'], [])
                self.assertIn('Retry-After', operation['responses']['429']['headers'])
                for status, result in operation['responses'].items():
                    self.assertTrue(result['description'], status)
                    self.assertTrue(result.get('content'), status)
        self.assertEqual(len(set(operation_ids)), len(operation_ids))
        body = spec['paths']['/write']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual(body['properties']['content']['maxLength'], 5000)
        self.assertFalse(body['additionalProperties'])
        self.assertEqual(body['required'], ['content'])
        self.assertNotIn('operation_id', body['properties'])
        for path in ('/write', '/boards', '/{board_id}/append'):
            operation = spec['paths'][path]['post']
            parameter = next(p for p in operation['parameters'] if p['name'] == 'Idempotency-Key')
            self.assertTrue(parameter['required'])
            self.assertEqual(parameter['schema']['format'], 'uuid')
            self.assertIn('-4', parameter['schema']['pattern'])
        self.assertEqual(self.client.get('/openapi.json', headers=SITE).json(), spec)

    def test_every_discovery_route_and_page_is_rate_limited_and_fail_closed(self):
        for retry, expected in [(3, 429), (RuntimeError('private details'), 503)]:
            if isinstance(retry, Exception):
                app.state.limiter.consume_with_status.side_effect = retry
            else:
                app.state.limiter.consume_with_status.return_value = RateLimitDecision(retry, 0, 0, 1, retry)
            for path in ('/', '/help', '/help/fresh', '/robots.txt', '/llms.txt', '/sitemap.xml', '/openapi.json', '/help.md', '/index.md', '/' + KEY, '/' + KEY + '/fresh', '/not-found'):
                response = self.client.get(path, headers=SITE)
                self.assertEqual(response.status_code, expected)
                self.assertIn('retry-after', response.headers)
                self.assertNotIn('private details', response.text)

    def test_discovery_routes_are_explicit_get_only_carveouts(self):
        for path in ('/robots.txt', '/llms.txt', '/sitemap.xml', '/openapi.json', '/help.md', '/index.md'):
            self.assertEqual(self.client.post(path).status_code, 401)
            self.assertEqual(self.client.get(path + '/private').status_code, 401)
        self.assertEqual(self.client.get('/api', headers=SITE, follow_redirects=False).headers['location'], '/')


class NegotiationTests(unittest.TestCase):
    def test_qualities_wildcards_exclusions_and_defaults(self):
        cases = {
            None: 'text/html', '*/*': 'text/html', 'text/*': 'text/html',
            'text/markdown': 'text/markdown', 'text/markdown, text/html;q=0.8': 'text/markdown',
            'text/html, text/markdown;q=0.1': 'text/html', 'text/markdown;q=0, text/html': 'text/html',
            'text/markdown;q=0, */*': 'text/html', 'text/html;q=0, text/markdown;q=0, application/json;q=0, */*': None,
            'text/markdown;q=0.4, text/*;q=0.8': 'text/html',
            'text/markdown, */*': 'text/markdown', 'application/json': 'application/json',
            'text/markdown;q=bogus, text/html': 'text/html', 'image/png': None,
            'text/markdown; charset=utf-8': 'text/markdown', 'text/markdown;variant=unsupported': None,
            'TEXT/MARKDOWN;Q=1': 'text/markdown', '*/*;q=0': None,
        }
        for accept, expected in cases.items():
            with self.subTest(accept=accept):
                self.assertEqual(negotiate(accept), expected)
        self.assertEqual(negotiate('*/*', default='application/json'), 'application/json')
