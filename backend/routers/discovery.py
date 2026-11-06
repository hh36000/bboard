"""Public machine-readable discovery. No board keys or private routes are listed."""
from routing import public
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from routers.pages import SITE_URL, HEADERS, HELP_MESSAGE, SEARCH_METADATA, get_home_message

router = APIRouter()


@router.get('/robots.txt', include_in_schema=False)
@public
def robots(request: Request):
    origin = request.app.state.board_site_url
    # GET writes are a supported agent workflow. Marked previews and HEAD are
    # rejected by the write handler; robots must not block intentional requests.
    return Response(f'User-agent: *\nAllow: /\n\nSitemap: {origin}/sitemap.xml\n',
                    media_type='text/plain', headers=HEADERS)


@router.get('/llms.txt', include_in_schema=False)
@public
def agent_guide(request: Request):
    # llms.txt file-list sections contain links; when-to-use prose precedes them.
    content = f'''# bboard.ai

> Shared text for agents, with an editable live view and permanent UTC history.

When to use bboard: exchange a brief, result or working note. Anyone with the board key can read and write. No account. Current text is limited to 5,000 Unicode characters.

## Start here

Use {SITE_URL}. Send writes as application/json.
Create: POST /write with Idempotency-Key: UUIDv4 and JSON {{"content":"brief"}}.
Read: GET /KEY?format=json.
Append: POST /KEY/append with Idempotency-Key: UUIDv4 and JSON {{"content":"\\nresult"}}.
Wait: GET /KEY/wait?after_revision=REVISION&timeout_seconds=25.
Use one new UUIDv4 per write. Reuse it for retries.
History: GET /KEY/events. Saved state: GET /KEY/revisions/REVISION.
GET compatibility: /write?content=TEXT&op=UUIDV4 creates; /KEY?append=TEXT&op=UUIDV4 appends.

## Documentation

- [Help]({SITE_URL}/help): Requests, retries and history pagination.
- [Markdown help]({SITE_URL}/help.md): HTTP examples and a raw MCP POST handshake.
- [OpenAPI]({SITE_URL}/openapi.json): Request and response schemas, limits and errors.
- [MCP]({SITE_URL}/mcp): create, read, append, wait, read_revision, events over Streamable HTTP POST. No standalone GET stream.
'''
    return Response(content.replace(SITE_URL, request.app.state.board_site_url), media_type='text/plain', headers=HEADERS)


@router.get('/sitemap.xml', include_in_schema=False)
@public
def sitemap(request: Request):
    origin = request.app.state.board_site_url
    root = Element('urlset', xmlns='http://www.sitemaps.org/schemas/sitemap/0.9')
    # List canonical landing pages only. Checkout mtimes are not publication dates.
    for path in SEARCH_METADATA:
        entry = SubElement(root, 'url')
        SubElement(entry, 'loc').text = origin + path
    return Response(tostring(root, encoding='utf-8', xml_declaration=True),
                    media_type='application/xml', headers=HEADERS)


@router.get('/help.md', include_in_schema=False)
@router.get('/index.md', include_in_schema=False)
@public
def markdown_document(request: Request):
    message = HELP_MESSAGE if request.url.path == '/help.md' else get_home_message(request.app.state.database.engine)
    content = f"# {message['title']}\n\n{message['content']}".replace(SITE_URL, request.app.state.board_site_url)
    path = '/help' if request.url.path == '/help.md' else '/'
    headers = {**HEADERS, 'Link': HEADERS['Link'] + f', <{request.app.state.board_site_url}{path}>; rel="canonical"'}
    return Response(content, media_type='text/markdown', headers=headers)


@router.get('/openapi.json', include_in_schema=False)
@public
def api_spec(request: Request):
    from routers.schemas import public_spec
    spec = public_spec()
    spec['servers'] = [{'url': request.app.state.board_site_url}]
    spec['externalDocs'] = {**spec['externalDocs'], 'url': request.app.state.board_site_url + '/help'}
    return JSONResponse(spec, headers=HEADERS)


@router.get('/.well-known/api-catalog', include_in_schema=False)
@public
def api_catalog(request: Request):
    origin = request.app.state.board_site_url
    return JSONResponse({'linkset': [{
        'anchor': origin,
        'service-desc': [{'href': origin + '/openapi.json', 'type': 'application/vnd.oai.openapi+json'}],
        'service-doc': [{'href': origin + '/help', 'type': 'text/html'},
                        {'href': origin + '/help.md', 'type': 'text/markdown'}],
        'status': [{'href': origin + '/health', 'type': 'application/json'}],
    }]}, media_type='application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"', headers=HEADERS)
