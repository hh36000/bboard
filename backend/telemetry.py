"""Log route templates and duration, never capability URLs or write bodies."""
import logging
import re
from time import perf_counter

logger = logging.getLogger(__name__)


class RedactCapabilityURLs(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r'[0-9a-fA-F]{64}', '[board-key]', message)
        message = re.sub(r'\?[^\s\"\']*', '?[query]', message)
        record.msg, record.args = message, ()
        if hasattr(record, 'color_message'):
            record.color_message = message
        return True


def configure_request_logging():
    # Uvicorn's ordinary access log includes raw paths and query strings.
    logging.getLogger('uvicorn.access').disabled = True
    # WebSocket accept/reject notices are emitted on uvicorn.error.
    logging.getLogger('uvicorn.error').addFilter(RedactCapabilityURLs())


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        start, status = perf_counter(), 500
        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)
        try:
            await self.app(scope, receive, timed_send)
        finally:
            route = getattr(scope.get('route'), 'path', 'unmatched')
            method = scope.get('method')
            method = method if method in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'} else 'OTHER'
            logger.info('%s %s status=%d elapsed_ms=%.1f', method, route, status, (perf_counter() - start) * 1000)
