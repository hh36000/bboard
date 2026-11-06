"""OpenAPI describes the public contract; request-body streaming stays bounded."""
from copy import deepcopy
from typing import Literal

from fastapi.openapi.utils import get_openapi
from fastapi.routing import iter_route_contexts
from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str
    content: str


class BoardCreated(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern='^[0-9a-f]{64}$', description='Access secret for this board. Anyone holding it can read and edit.')
    path: str = Field(pattern='^/[0-9a-f]{64}$')
    url: str = Field(json_schema_extra={'format': 'uri'}, description='Browser URL for the new board.')


class BoardContent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern='^[0-9a-f]{64}$')
    content: str = Field(description='Current saved text. New writes are capped at 5,000 Unicode characters.')
    revision: int = Field(ge=0, description='Monotonic committed revision.')


class BoardChange(BaseModel):
    offset: int = Field(ge=0, description='Zero-based Unicode character offset in the previous state.')
    removed: str = Field(description='Exact span removed from the previous state.')
    inserted: str = Field(description='Exact replacement for that span. Empty means deletion.')


class RevisionEvent(BaseModel):
    revision: int = Field(ge=0)
    recorded_at: str = Field(pattern='Z$', json_schema_extra={'format': 'date-time'}, description='Server recording time in UTC. Revision determines ordering.')
    kind: Literal['baseline', 'created', 'append', 'delete', 'edit']
    content: str = Field(description='Complete board state after this event.')
    change: BoardChange | None = Field(description='One exact replacement span. Apply to the preceding state to recover content. Null only for a migration baseline with unknown previous text.')


class BoardEvent(RevisionEvent):
    key: str = Field(pattern='^[0-9a-f]{64}$')


class BoardEvents(BaseModel):
    key: str = Field(pattern='^[0-9a-f]{64}$')
    first_revision: int = Field(ge=0, description='Earliest retained revision; may be greater than zero for boards predating history.')
    through_revision: int = Field(ge=0, description='Inclusive upper bound of this stable range. Preserve when paging.')
    events: list[RevisionEvent]
    next_after_revision: int | None = Field(description='Use as after_revision on the next page; null means this range is complete.')


class BoardAppendResult(BoardContent):
    appended_revision: int = Field(ge=1, description='Revision at which this operation originally appended.')
    duplicate: bool = Field(description='True when this operation already committed; no text added again.')


class BoardWaitResult(BoardContent):
    changed: bool = Field(description='True if revision is newer than after_revision. False is a normal timeout.')


class BoardCreateResult(BoardCreated, BoardContent):
    duplicate: bool = Field(description='True when this idempotency key already committed. Returns the same board and its current content without overwriting edits.')


class WriteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(max_length=5000, description='Up to 5,000 Unicode characters. Empty allowed; null characters and invalid Unicode rejected.')


class CreateInput(WriteInput):
    pass


class AppendInput(WriteInput):
    content: str = Field(min_length=1, max_length=5000, description='Exact text to append, including separators. The resulting board must fit 5,000 Unicode characters.')


class UpdateInput(WriteInput):
    expected_revision: int = Field(ge=0, strict=True, description='Revision from the latest read or conflict response. Required; a mismatch returns 409 without changing the board.')


class RevisionConflict(BoardContent):
    detail: Literal['revision_conflict']


class PatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    old_text: str = Field(min_length=1, description='Exact literal span to replace. Must occur once, including overlapping matches. Empty, absent, or repeated spans return 422 with current board state.')
    new_text: str = Field(description='Literal replacement. Empty deletes the span. Result must fit 5,000 Unicode characters; null characters and invalid Unicode are rejected.')
    expected_revision: int = Field(ge=0, strict=True, description='Revision from the latest read or conflict response. Checked before matching; mismatch returns 409 with current board state.')


class PatchMatchError(BoardContent):
    detail: Literal['old_text_empty', 'old_text_not_found', 'old_text_ambiguous']


class QueryFieldError(BaseModel):
    field: str
    reason: str
    message: str


class Error(BaseModel):
    detail: str
    max_characters: int | None = Field(default=None, description='Present only for content_too_long.')
    errors: list[QueryFieldError] | None = None


ERROR_RESPONSES = {status: {'description': description, 'model': Error} for status, description in {
    400: 'Invalid JSON.', 408: 'Request body timed out.', 413: 'Content exceeds 5,000 characters or body exceeds 64 KiB.',
    415: 'Content-Type must be application/json.', 422: 'Invalid fields or content.',
    429: 'IP request limit exceeded. Wait Retry-After seconds.', 503: 'Storage or rate-limit service unavailable.',
}.items()}


def public_spec():
    from main import app
    routes = [route for route in iter_route_contexts(app.router.routes)
              if getattr(route.endpoint, 'bboard_public', False) is True]
    endpoints = {(route.path_format, method.lower()): route.endpoint
                 for route in routes for method in (route.methods or ())}
    spec = get_openapi(title='bboard.ai API', version='1.0.0', openapi_version='3.1.0',
        description='Shared text with immutable UTC history. The board key grants read and write access; no account or token. '
                    'Current text is limited to 5,000 Unicode characters. Requests share 60/minute and 10/second per IP; honor Retry-After. '
                    'HTTP and MCP waits share 64 total and 4 per IP per process.',
        routes=routes,
        servers=[{'url': 'https://bboard.ai'}])
    spec['externalDocs'] = {'description': 'Requests, retries and history pagination', 'url': 'https://bboard.ai/help'}
    spec['security'] = []
    # The fallback schema owns its $defs. Resolve them before embedding it in
    # OpenAPI, whose ordinary references use the separate components namespace.
    error_schema = Error.model_json_schema()
    error_schema = inline_refs(error_schema, error_schema)
    error_schema.pop('$defs', None)
    for path, methods in spec['paths'].items():
        for method, operation in methods.items():
            operation['security'] = []
            operation.setdefault('parameters', []).append({
                'name': 'API-Version', 'in': 'header', 'required': False,
                'description': 'Select major API version 1. Omit for version 1. Other versions return HTTP 400.',
                'schema': {'type': 'string', 'enum': ['1'], 'default': '1'},
            })
            responses = operation['responses']
            responses.setdefault('400', {'description': 'Unsupported API-Version header.', 'content': {
                'application/json': {'schema': {'type': 'object', 'required': ['detail', 'supported_versions'], 'properties': {
                    'detail': {'type': 'string'}, 'supported_versions': {'type': 'array', 'items': {'type': 'string'}}}}}}})
            for status in ('429', '503'):
                responses.setdefault(status, {'description': ERROR_RESPONSES[int(status)]['description'],
                    'content': {'application/json': {'schema': error_schema}}})
            responses['429'].setdefault('headers', {}).update({'Retry-After': {'description': 'Seconds to wait before retrying.',
                'schema': {'type': 'integer', 'minimum': 1}}})
            if method == 'get' and getattr(endpoints[path, method], 'bboard_representations', False):
                operation['parameters'].append({'name': 'format', 'in': 'query', 'required': False,
                    'description': 'Optional response format. Takes precedence over Accept. Omit to keep existing defaults.',
                    'schema': {'type': 'string', 'enum': ['json', 'markdown', 'html']}})
                responses.setdefault('422', {'description': 'Invalid or repeated format value.',
                    'content': {'application/json': {'schema': error_schema}}})
                responses['200']['content'].update({kind: {'schema': {'type': 'string'}}
                    for kind in ('text/markdown', 'text/html')})
                responses['406'] = {'description': 'No acceptable representation.',
                    'content': {'application/json': {'schema': error_schema}} if path == '/{board_id}' else {'text/plain': {'schema': {'type': 'string'}}}}
            for status, response in responses.items():
                documented_headers = response_headers()
                if status == '503':
                    # Limiter storage failure cannot provide a quota snapshot.
                    documented_headers = {'API-Version': documented_headers['API-Version']}
                response.setdefault('headers', {}).update(documented_headers)
            responses['400']['description'] += ' Unsupported API-Version is also rejected.'
            if '{board_id}' in path:
                parameter = next(p for p in operation['parameters'] if p['name'] == 'board_id')
                parameter['schema']['pattern'] = '^[0-9a-f]{64}$'
                parameter['description'] = 'The complete 64-character lowercase hexadecimal board access key.'
    # Small self-contained schemas also work with clients that do not resolve $ref.
    expanded = inline_refs(spec, spec)
    expanded.pop('components', None)
    return expanded


def response_headers():
    return {name: {'description': description, 'schema': {'type': 'string'}} for name, description in {
        'API-Version': 'Major API version used for this response (1).',
        'RateLimit-Policy': 'Quota policies as structured fields: second window and minute window.',
        'RateLimit': 'Remaining quota r and reset delay t in seconds for each advertised policy.',
        'RateLimit-Limit': 'Compatibility field: limit for the tighter advertised window.',
        'RateLimit-Remaining': 'Compatibility field: remaining requests in that window.',
        'RateLimit-Reset': 'Compatibility field: seconds until that window resets (not a Unix timestamp).',
    }.items()}


def inline_refs(value, root, seen=frozenset()):
    if isinstance(value, list):
        return [inline_refs(item, root, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if '$ref' in value:
        ref = value['$ref']
        if not ref.startswith('#/') or ref in seen:
            raise ValueError('Public API schemas must use acyclic local references')
        target = root
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        result = inline_refs(deepcopy(target), root, seen | {ref})
        result.update(inline_refs({key: item for key, item in value.items() if key != '$ref'}, root, seen))
        return result
    return {key: inline_refs(item, root, seen) for key, item in value.items()}
