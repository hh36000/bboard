"""HTML, JSON, and Markdown representations of the same public resources."""
import html
import json
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from board_count import COUNTED_BOARD_SQL

SITE_URL = 'https://bboard.ai'
CONTENT_DIR = Path(__file__).parent
HOME_MESSAGE = json.loads((CONTENT_DIR / 'home_content.json').read_text())
HELP_MESSAGE = json.loads((CONTENT_DIR / 'help_content.json').read_text())
SEARCH_METADATA = json.loads((CONTENT_DIR / 'search_metadata.json').read_text())
RECENT_MESSAGE = {'title': 'Recent bboards', 'content': 'The last 100 boards opened in this browser, newest first.'}
def host_list(name, default):
    """Comma-separated hostnames from the environment."""
    return [host.strip().lower() for host in os.getenv(name, default).split(',') if host.strip()]


# Hosts that receive browser HTML by default. Presentation only; see is_site_request.
SITE_HOSTS = frozenset(host_list('BBOARD_SITE_HOSTS', 'bboard.ai,www.bboard.ai'))
DESCRIPTION = 'A shared text board on bboard.ai. Anyone with its key can read and write.'
LINKS = f'<{SITE_URL}/.well-known/api-catalog>; rel="api-catalog", <{SITE_URL}/help>; rel="service-doc"; type="text/html", <{SITE_URL}/llms.txt>; rel="describedby", <{SITE_URL}/openapi.json>; rel="service-desc"; type="application/vnd.oai.openapi+json"'
HEADERS = {'Cache-Control': 'no-store', 'Vary': 'Accept, Accept-Encoding', 'Link': LINKS,
           'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'}


class SiteLinkMiddleware:
    """Point every Link header target at BBOARD_SITE_URL instead of the default site."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        origin = scope['app'].state.board_site_url
        if origin == SITE_URL:
            return await self.app(scope, receive, send)
        prefix, replacement = f'<{SITE_URL}/'.encode(), f'<{origin}/'.encode()
        async def rewrite(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [(name, value.replace(prefix, replacement) if name.lower() == b'link' else value)
                                                  for name, value in message['headers']]}
            await send(message)
        await self.app(scope, receive, rewrite)


FORMATS = {'html': 'text/html', 'json': 'application/json', 'markdown': 'text/markdown'}
CREATE_MESSAGE = {'title': 'Create a board', 'content': '''GET /write?content=YOUR_URL_ENCODED_TEXT&op=YOUR_NEW_UUID

Use a new UUIDv4. Retry with the same UUID and text within 7 days.

Add &format=json or &format=markdown to choose the response.

Opening this page creates nothing.
Help: https://bboard.ai/help
'''}


def canonical_read_path(path):
    """Suffixes give fetch clients a fresh address for the same public content."""
    match = re.fullmatch(r'/(help|[0-9a-f]{64})(?:/.*)?', path, re.DOTALL)
    return '/' + match[1] if match else None


def get_home_message(engine):
    count = None
    if engine is not None:
        try:
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL statement_timeout = '2s'"))
                count = connection.execute(text(
                    f'SELECT count(*) FROM app.boards WHERE {COUNTED_BOARD_SQL}'
                )).scalar_one()
        except SQLAlchemyError:
            logging.getLogger(__name__).warning('Homepage board count unavailable')
    total = f'{count:,}' if count is not None else 'temporarily unavailable'
    return {**HOME_MESSAGE, 'content': HOME_MESSAGE['content'].rstrip() + f'\n\nbboards in existence: {total}\n'}


def is_site_request(scope):
    # Presentation only. These headers NEVER grant access to a backend handler,
    # select a rate-limit identity, or authorize writes. Both hosts expose the
    # same public board operations; unknown site paths still get a real 404.
    headers = Headers(scope=scope)
    return any(headers.get(name, '').lower().split(':')[0] in SITE_HOSTS
               for name in ('x-fh-requested-host', 'x-forwarded-host', 'host'))


def negotiate(accept, default='text/html'):
    """RFC 9110 media ranges: most specific match supplies each type's quality."""
    offered = [default] + [kind for kind in ('text/html', 'text/markdown', 'application/json') if kind != default]
    ranges = []
    for entry in (accept or '*/*').split(','):
        pieces = [part.strip().lower() for part in entry.split(';')]
        media = pieces[0]
        if '/' not in media:
            continue
        quality, supported_parameters = 1.0, True
        for param in pieces[1:]:
            name, sep, value = param.partition('=')
            if name.strip() == 'q':
                try:
                    quality = float(value) if re.fullmatch(r'(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)', value) else 0
                except ValueError:
                    quality = 0
                break
            if sep and not (name.strip() == 'charset' and value.strip('"') == 'utf-8'):
                supported_parameters = False
        if supported_parameters:
            specificity = 0 if media == '*/*' else 1 if media.endswith('/*') else 2
            ranges.append((media, specificity, quality))
    scores = []
    for kind in offered:
        matches = [(specificity, quality) for media, specificity, quality in ranges
                   if media in (kind, kind.split('/')[0] + '/*', '*/*')]
        specificity, quality = max(matches, default=(-1, 0))
        # On equal q, an explicit Markdown preference beats a wildcard default.
        scores.append((quality, specificity, -offered.index(kind), kind))
    quality, _, _, kind = max(scores)
    return kind if quality > 0 else None


def safe_json(value):
    return json.dumps(value, ensure_ascii=True).replace('<', '\\u003c')


def response_kind(request, default=None):
    values = request.query_params.getlist('format')
    if values:
        if len(values) != 1 or values[0] not in FORMATS:
            return JSONResponse({'detail': 'invalid_format', 'errors': [{
                'field': 'format', 'reason': 'invalid',
                'message': 'Supply format once: json, markdown, or html.'}]},
                status_code=422, headers=HEADERS)
        return FORMATS[values[0]]
    return negotiate(request.headers.get('accept'), default or
                     ('text/html' if is_site_request(request.scope) else 'application/json'))


def linked_text(content):
    # Match raw text, escape each segment, and leave sentence punctuation outside links.
    parts, end = [], 0
    for match in re.finditer(r'https?://[^\s<>"\']+|(?<![/\w])bboard\.ai/help', content):
        parts.append(html.escape(content[end:match.start()]))
        link = match[0].rstrip('.,;:!?)')
        suffix = match[0][len(link):]
        target = link if link.startswith('http') else 'https://' + link
        parts.append(f'<a href="{html.escape(target, quote=True)}">{html.escape(link)}</a>' + html.escape(suffix))
        end = match.end()
    parts.append(html.escape(content[end:]))
    return ''.join(parts)


def render_html(message, path, *, board=False, status_code=200, interactive=True, origin=SITE_URL, history_view=False):
    title = message['title']
    canonical = origin + path
    metadata = SEARCH_METADATA.get(path) if status_code == 200 and not board else None
    search_title = metadata['title'] if metadata else title
    description = metadata['description'] if metadata else DESCRIPTION
    markdown_path = metadata['markdownPath'] if metadata else path
    structured = ('<script id="page-structured-data" type="application/ld+json">'
                  + safe_json(metadata['structuredData']).replace(SITE_URL, origin) + '</script>') if metadata else ''
    content = html.escape(message['content']) if board else linked_text(message['content'])
    # No board text in metadata, and no indexing of capability URLs.
    robots = '<meta name="robots" content="' + ('index, follow' if metadata else 'noindex, noarchive') + '" />'
    initial_message = {'path': path, **message}
    if history_view:
        # Never seed the live editor/session with formatted historical content.
        initial_message['path'] = path + '?history=1'
    notice = ''
    if status_code == 404:
        initial_message['notFoundPath'] = path
    heading = '' if board and not history_view else f'<h1>{html.escape(title)}</h1>'
    agent_help = ''
    clean_write_url = ''
    read_links = ''
    if status_code == 200 and (board or path == '/help'):
        fresh_url = origin + path + '/read/' + str(uuid4())
        read_links = f'<p><a href="{fresh_url}">Fresh read</a></p>'
    if board:
        notice = f'<p>Current revision: {message["revision"]}.</p>'
        # Put the canonical board link before user content, inside the main article.
        # Some fetch tools discard aside/navigation links during extraction.
        read_links = f'<p>Board: <a href="{origin + path}">{origin + path}</a></p>' + read_links
        agent_help = '<aside aria-label="Agent instructions"><h2>Write to this board</h2><p>' + html.escape(
            'POST to this URL plus /append with an Idempotency-Key UUIDv4 header and JSON content. For GET-only clients, add '
            '?append=YOUR_URL_ENCODED_TEXT&op=YOUR_NEW_UUID. '
            'Use a new UUIDv4 per addition; retry with the same UUID and identical text within 7 days. Include your own newlines.'
        ) + '</p><p><a href="/help">Help</a></p></aside>'
        if 'duplicate' in message:
            receipt = ('Already appended' if message['duplicate'] else 'Append saved') + f' at revision {message["appended_revision"]}.'
            notice = f'<p role="status">{receipt} Current revision: {message["revision"]}.</p>'
            # Once committed, the editor and Copy URL should use the ordinary read URL.
            clean_write_url = "<script>try{history.replaceState(history.state,'',location.pathname+location.hash)}catch{}</script>"
        read_links += f'<p><a href="{origin + path}?history=1">History</a> · <a href="{origin + path}/events">Events JSON</a></p>'
        if history_view:
            notice = '<p>Saved history. All event times are UTC.</p>'
            read_links = f'<p><a href="{origin + path}">Live board</a> · <a href="{origin + path}?history=1">History</a> · <a href="{origin + path}/events">Events JSON</a></p>'
            agent_help = ''
    # Navbar places Help, Recent, and Copy before 40px create, 20px status, and 40px theme controls,
    # with three 8px gaps. Reserve those 124px before React adds the controls.
    help_link = '''<a class="help" href="/help" aria-label="Help" title="Help"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg></a>''' if path == '/' and status_code == 200 else ''
    status = f'{len(message["content"]):,} / 5,000 · ' + ('Connecting…' if interactive else 'Saved snapshot') if board else ''
    if path.rstrip('/') == '/recent':
        status = 'Stored in this browser'
    if history_view:
        status = 'Read only · UTC'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<script>try{{document.documentElement.classList.add(localStorage.getItem('theme')==='light'?'light':'dark')}}catch{{document.documentElement.classList.add('dark')}}</script>
<title>{html.escape(search_title)}</title><meta name="description" content="{html.escape(description, quote=True)}" />
<link rel="icon" type="image/png" sizes="64x64" href="{origin}/images/favicon.png" />
<link rel="icon" type="image/svg+xml" sizes="any" href="{origin}/images/favicon.svg" />
<link rel="canonical" href="{html.escape(canonical, quote=True)}" />
<link rel="api-catalog" href="/.well-known/api-catalog" /><link rel="service-doc" href="/help" type="text/html" /><link rel="describedby" href="/llms.txt" /><link rel="service-desc" href="/openapi.json" type="application/vnd.oai.openapi+json" />
<link rel="alternate" type="text/markdown" href="{html.escape(markdown_path, quote=True)}" />
<meta property="og:title" content="{html.escape(search_title, quote=True)}" /><meta property="og:type" content="website" />
<meta property="og:description" content="{html.escape(description, quote=True)}" /><meta property="og:site_name" content="bboard.ai" />
<meta property="og:url" content="{html.escape(canonical, quote=True)}" /><meta property="og:image" content="{origin}/images/bb-logo.png" />
<meta property="og:image:alt" content="bboard.ai logo" />
<meta name="twitter:card" content="summary" /><meta name="twitter:title" content="{html.escape(search_title, quote=True)}" />
<meta name="twitter:description" content="{html.escape(description, quote=True)}" /><meta name="twitter:image" content="{origin}/images/bb-logo.png" />
<meta name="twitter:image:alt" content="bboard.ai logo" />
<meta name="referrer" content="no-referrer" />{robots}{structured}
<style id="page-fallback-style">
:root{{color-scheme:dark;--page-bg:#000;--page-fg:#fff;--page-muted:#c3c3cc;--page-link:#60a5fa;--page-border:#2d2d3a}}
:root.light{{color-scheme:light;--page-bg:#fff;--page-fg:#1e293b;--page-muted:#64748b;--page-link:#2563eb;--page-border:#d1d5db}}
html,body,#root{{height:100%;overflow:hidden}}
body{{margin:0;background:var(--page-bg);color:var(--page-fg);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
#initial-shell{{height:100dvh;display:flex;flex-direction:column;overflow:hidden}}
#initial-nav{{height:4rem;flex-shrink:0;width:100%;max-width:64rem;margin:0 auto;padding:0 1.5rem;box-sizing:border-box;display:flex;align-items:center;gap:1rem;font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
#initial-logo{{display:block;width:3rem;height:3rem;object-fit:contain;filter:brightness(0) invert(1)}}
:root.light #initial-logo{{filter:brightness(0)}}
#initial-nav .actions{{display:flex;align-items:center;gap:8px;margin-left:auto;margin-right:124px}}
#initial-nav .help{{display:flex;align-items:center;justify-content:center;flex-shrink:0;width:40px;height:40px;color:var(--page-muted);border-radius:8px}}
#initial-scroll{{flex:1;min-height:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior-y:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:color-mix(in oklab,var(--page-border) 18%,transparent) transparent}}
#initial-scroll::-webkit-scrollbar{{width:6px;height:6px}}
#initial-scroll::-webkit-scrollbar-track,#initial-scroll::-webkit-scrollbar-corner{{background:transparent}}
#initial-scroll::-webkit-scrollbar-thumb{{background:color-mix(in oklab,var(--page-border) 18%,transparent);border-radius:3px}}
#initial-scroll::-webkit-scrollbar-thumb:hover{{background:color-mix(in oklab,var(--page-border) 38%,transparent)}}
#initial-page{{max-width:64rem;min-height:100%;margin:0 0 0 max(0px,calc((100vw - 64rem)/2));padding:1.5rem 1.5rem 3rem;box-sizing:border-box;font:14px/24px "SFMono-Regular","Menlo",monospace;overflow-wrap:anywhere}}
#initial-page h1{{font:inherit;margin:0 0 1.5rem}}
#initial-page .text{{white-space:pre-wrap}}
#initial-shell a{{color:var(--page-link);text-underline-offset:2px}}
#initial-status{{position:fixed;right:20px;bottom:12px;display:flex;align-items:center;gap:8px;border-radius:4px;background:var(--page-bg);color:var(--page-muted);font:12px/16px system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
#initial-status .status{{padding:4px 8px}}
#initial-nav .copy{{display:flex;align-items:center;justify-content:center;width:40px;height:40px;flex-shrink:0;margin:0;padding:0;border:0;border-radius:8px;background:transparent;color:var(--page-muted);opacity:.5}}
@media(max-width:639px){{#initial-nav .actions{{gap:4px;margin-right:96px}}#initial-nav .help,#initial-nav .copy{{width:32px;height:32px}}}}
</style>
</head><body><div id="root"><div id="initial-shell">
<nav id="initial-nav" aria-label="Main navigation"><a href="/" aria-label="bb home"><img id="initial-logo" src="{origin}/images/bb-logo.png" width="48" height="48" alt="" aria-hidden="true" /></a><div class="actions">{help_link}<a class="help" href="/recent" aria-label="Recent bboards" title="Recent bboards"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg></a><button class="copy" type="button" aria-label="Copy" title="Copy" aria-haspopup="menu" aria-expanded="false" disabled><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="14" height="14" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg></button></div></nav>
<div id="initial-scroll"><main id="initial-page" aria-label="{html.escape(title, quote=True)}">{notice}{heading}{read_links}<div class="text">{content}</div>{agent_help}</main></div>
<div id="initial-status"><span class="status" role="status">{status}</span></div>
</div></div>
<script id="initial-message" type="application/json">{safe_json(initial_message)}</script>
{clean_write_url}
<script type="module" src="/app.js"></script></body></html>'''


def message_response(request, message, path, *, board=False, default=None, status_code=200, interactive=None):
    origin = request.app.state.board_site_url
    if not board:
        message = {**message, 'content': message['content'].replace(SITE_URL, origin)}
    kind = response_kind(request, default)
    if isinstance(kind, Response):
        return kind
    headers = dict(HEADERS)
    if board or status_code != 200 or path not in SEARCH_METADATA:
        headers['X-Robots-Tag'] = 'noindex, noarchive'
    elif kind in ('text/markdown', 'application/json'):
        headers['Link'] += f', <{origin}{path}>; rel="canonical"'
    if kind is None:
        return Response('Supported representations: text/html, text/markdown, application/json.\n', 406,
                        headers=headers, media_type='text/plain')
    if kind == 'text/html':
        interactive = True if interactive is None else interactive
        body = render_html(message, path, board=board, status_code=status_code,
                           interactive=interactive, origin=request.app.state.board_site_url)
        if not interactive:
            body = body.replace('<script type="module" src="/app.js"></script>', '')
        return HTMLResponse(body, status_code=status_code, headers=headers)
    if kind == 'text/markdown':
        text = message['content'] if board else f"# {message['title']}\n\n{message['content']}"
        if board and 'duplicate' in message:
            receipt = 'Already appended' if message['duplicate'] else 'Append saved'
            text = f'{receipt} at revision {message["appended_revision"]}. Current revision: {message["revision"]}.\n\n' + text
        return Response(text, status_code=status_code, media_type='text/markdown', headers=headers)
    payload = {k: message[k] for k in ('key', 'content', 'revision')} if board else message
    if board and 'duplicate' in message:
        payload.update({k: message[k] for k in ('duplicate', 'appended_revision')})
    return JSONResponse(payload, status_code=status_code, headers=headers)


def not_found(request):
    message = {'title': 'Not found (404)', 'content': 'Check the URL.\n\nHelp: https://bboard.ai/help\nAgent guide: https://bboard.ai/llms.txt\n'}
    return message_response(request, message, request.url.path,
                            default='text/html', status_code=404)


def creation_response(request, board):
    """Return an API receipt or open the newly created board in the browser."""
    headers = {**HEADERS, 'X-Robots-Tag': 'noindex, noarchive'}
    kind = response_kind(request)
    if isinstance(kind, Response):
        return kind
    receipt = 'Board already created' if board['duplicate'] else 'Board created'
    if kind == 'application/json':
        return JSONResponse(board, headers=headers)
    if kind == 'text/markdown':
        return Response(f'{receipt}.\n\n[Open board]({board["url"]})\n\nCurrent revision: {board["revision"]}.\n\n'
                        + board['content'], media_type='text/markdown', headers=headers)
    return RedirectResponse(board['url'], status_code=303, headers=headers)
