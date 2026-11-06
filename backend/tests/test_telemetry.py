import logging
import unittest
from unittest.mock import patch
from telemetry import RedactCapabilityURLs, RequestTimingMiddleware

class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def test_websocket_notice_redacts_key_and_query(self):
        key = 'a' * 64
        record = logging.LogRecord('uvicorn.error', logging.INFO, '', 1, 'WebSocket %s accepted', ('/ws/boards/' + key + '?append=private-text&op=secret-id',), None)
        RedactCapabilityURLs().filter(record)
        self.assertNotIn(key, record.getMessage())
        self.assertNotIn('private-text', record.getMessage())
        self.assertNotIn('secret-id', record.getMessage())

    async def test_timing_logs_registered_template_without_capability_path(self):
        class Route:
            path = '/{board_id}/append'
        async def application(scope, receive, send):
            scope['route'] = Route()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        async def send(message):
            pass
        with patch('telemetry.logger.info') as log:
            await RequestTimingMiddleware(application)({'type': 'http', 'method': 'POST', 'path': '/' + 'b' * 64 + '/append'}, None, send)
        self.assertEqual(log.call_args.args[2], '/{board_id}/append')
        self.assertNotIn('b' * 64, str(log.call_args))
