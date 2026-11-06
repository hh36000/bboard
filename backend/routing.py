"""Registered routes own public access; presentation never grants permission."""
import re

from fastapi.routing import iter_route_contexts
from starlette.routing import Match

BOARD_KEY_PATTERN = re.compile(r'[0-9a-f]{64}')


def public(endpoint=None, *, representations=False):
    """Opt an endpoint into public access without a second method/path list."""
    def mark(handler):
        handler.bboard_public = True
        handler.bboard_representations = representations
        return handler
    return mark(endpoint) if endpoint is not None else mark


def public_route(scope):
    for route in iter_route_contexts(scope['app'].router.routes):
        match, child_scope = route.matches(scope)
        if match != Match.FULL:
            continue
        endpoint = child_scope.get('endpoint', route.endpoint)
        if getattr(endpoint, 'bboard_public', False) is not True:
            return None
        key = child_scope.get('path_params', {}).get('board_id')
        if key is not None and BOARD_KEY_PATTERN.fullmatch(key) is None:
            return None
        return route
    return None
