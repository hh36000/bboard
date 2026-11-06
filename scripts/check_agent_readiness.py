#!/usr/bin/env python3
"""Read-only checks of a running bboard's public discovery and API surface. Creates no boards."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree



def api_contract_matches(spec):
    required = {'/write': {'post'}, '/{board_id}': {'get', 'put', 'patch'},
                '/{board_id}/append': {'post'}, '/{board_id}/wait': {'get'},
                '/{board_id}/events': {'get'}, '/{board_id}/revisions/{revision}': {'get'}}
    paths = spec.get('paths', {})
    return all(methods <= paths.get(path, {}).keys() for path, methods in required.items())


@dataclass
class Observation:
    status: int
    headers: dict
    body: str

    def json(self):
        return json.loads(self.body)


class PageText(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.hidden = 0
        self.text = ''
        self.feed(body)
    def handle_starttag(self, tag, attrs):
        if tag in ('head', 'script', 'style'):
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('head', 'script', 'style'):
            self.hidden -= 1
    def handle_data(self, data):
        if not self.hidden:
            self.text += data


def fetch(url, headers=None):
    time.sleep(.25)  # Stay below the per-second request budget; no aggressive retries.
    try:
        response = urlopen(Request(url, headers=headers or {}), timeout=15)
    except HTTPError as error:
        response = error
    with response:
        return Observation(response.status, {name.lower(): value for name, value in response.headers.items()},
                           response.read(2_000_000).decode('utf-8'))


def check(origin):
    results = []
    def record(name, url, predicate, headers=None):
        try:
            response = fetch(url, headers)
            ok = bool(predicate(response))
            results.append({'check': name, 'url': url, 'passed': ok, 'status': response.status,
                'content_type': response.headers.get('content-type'), 'retry_after': response.headers.get('retry-after')})
        except (OSError, ValueError, KeyError, TypeError, ElementTree.ParseError) as error:
            results.append({'check': name, 'url': url, 'passed': False, 'error': str(error)})
    frontend = api = origin
    def permits_creation(response, origin):
        parser = RobotFileParser()
        parser.parse(response.body.splitlines())
        return response.status == 200 and all(parser.can_fetch(agent, origin + path)
            for agent in ('Claude-User', 'ClaudeBot', 'GPTBot')
            for path in ('/help', '/write', '/write?content=Hello'))
    record('Readable homepage', frontend+'/', lambda r: r.status == 200 and '<h1>' in r.body and 'bboard' in PageText(r.body).text.lower() and '/help' in r.body)
    record('Real frontend 404', frontend+'/__bboard-readiness-check__', lambda r: r.status == 404 and 'llms.txt' in r.body,
           {'Accept': 'text/markdown'})
    record('Developer docs alias', frontend+'/developers', lambda r: r.status == 200 and '/openapi.json' in r.body and '/write' in r.body)
    for origin in (origin,):
        record('Markdown negotiation', origin+'/help', lambda r: r.status == 200 and r.headers.get('content-type', '').startswith('text/markdown') and 'accept' in r.headers.get('vary', '').lower(), {'Accept': 'text/markdown'})
        record('Robots permits agent reads and GET creation', origin+'/robots.txt',
               lambda r: r.headers.get('content-type', '').startswith('text/plain') and permits_creation(r, origin))
        record('Agent guide', origin+'/llms.txt', lambda r: r.status == 200 and r.body.startswith('# bboard.ai') and 'When to use' in r.body)
        record('Sitemap XML', origin+'/sitemap.xml', lambda r: r.status == 200 and ElementTree.fromstring(r.body).tag == '{http://www.sitemaps.org/schemas/sitemap/0.9}urlset')
        record('API catalog', origin+'/.well-known/api-catalog', lambda r: r.status == 200 and r.headers.get('content-type', '').startswith('application/linkset+json') and bool(r.json()['linkset'][0]['service-desc']))
        record('Self-contained OpenAPI', origin+'/openapi.json', lambda r: r.status == 200 and api_contract_matches(r.json()) and '"$ref"' not in r.body)
        for agent in ('ChatGPT-User', 'ClaudeBot', 'Google-Extended', 'ora-agent', 'DeepSeekBot'):
            record('Crawler: '+agent, origin+'/', lambda r: r.status == 200, {'User-Agent': agent})
    record('Public API liveness', api+'/health', lambda r: r.status == 200 and r.json() == {'status':'healthy'})
    record('Rate-limit metadata', api+'/health', lambda r: r.status == 200 and all(name in r.headers for name in ('ratelimit', 'ratelimit-policy', 'ratelimit-remaining', 'ratelimit-reset')))
    record('API version 1', api+'/health', lambda r: r.status == 200 and r.headers.get('api-version') == '1', {'API-Version': '1'})
    record('Unsupported version rejected', api+'/health', lambda r: r.status == 400 and r.json().get('detail') == 'unsupported_api_version', {'API-Version': '2'})
    record('Private route stays unavailable', api+'/ready', lambda r: r.status == 404)
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'origin': origin,
            'passed': sum(item['passed'] for item in results), 'total': len(results), 'checks': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='https://bboard.ai')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = check(args.origin.rstrip('/'))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2)+'\n')
    for item in report['checks']:
        print(f"{'PASS' if item['passed'] else 'FAIL'} {item['check']}: {item.get('status', item.get('error'))} {item['url']}")
    print(f"{report['passed']}/{report['total']} checks passed. This is a live verification, not an Is Agentic score.")
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
