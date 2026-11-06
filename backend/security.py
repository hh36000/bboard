"""Default-deny HTTP boundary and shared, atomic per-IP request limits."""
from dataclasses import dataclass
from edge import trusted_edge, trusted_forwarder
import hashlib
import ipaddress
import logging
import math
import threading
import time

from routers.pages import is_site_request, not_found
from routing import BOARD_KEY_PATTERN, public_route
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)
PER_SECOND = 10
PER_MINUTE = 60


@dataclass(frozen=True)
class RateLimitDecision:
    retry_after: int
    second_remaining: int
    minute_remaining: int
    second_reset: int
    minute_reset: int
    per_second: int = PER_SECOND
    per_minute: int = PER_MINUTE

    def headers(self):
        # draft-ietf-httpapi-ratelimit-headers-11 structured fields. No IP or
        # partition identifier is exposed. Values describe this atomic charge.
        active = 'minute' if self.minute_remaining == 0 and self.retry_after >= self.minute_reset else 'second'
        # On 429, only advertise the exhausted policy so Retry-After and its
        # effective window agree. Successful responses describe both windows.
        windows = ['second', 'minute'] if not self.retry_after else [active]
        result = {
            'RateLimit-Policy': f'"second";q={self.per_second};w=1, "minute";q={self.per_minute};w=60',
            'RateLimit': ', '.join(f'"{name}";r={getattr(self, name + "_remaining")};t={getattr(self, name + "_reset")}' for name in windows),
        }
        # Compatibility names used by older clients. These always describe the
        # tighter window; reset is a delay in seconds, never an epoch timestamp.
        if not self.retry_after:
            active = min(windows, key=lambda name: getattr(self, name + '_remaining') / getattr(self, name + '_reset'))
        result.update({'RateLimit-Limit': str(self.per_minute if active == 'minute' else self.per_second),
                       'RateLimit-Remaining': str(getattr(self, active + '_remaining')),
                       'RateLimit-Reset': str(getattr(self, active + '_reset'))})
        return result


RATE_LIMIT_EXPOSE_HEADERS = ['Retry-After', 'RateLimit', 'RateLimit-Policy',
                            'RateLimit-Limit', 'RateLimit-Remaining', 'RateLimit-Reset', 'API-Version']

# One row lock makes both limits atomic across workers/instances. Saturated
# counters are capped; attempts do not extend the fixed expiration window.
CONSUME = text('''
INSERT INTO app.ip_rate_limits AS r
    (ip_hash, second_window, second_count, minute_window, minute_count)
VALUES (:key, date_trunc('second', statement_timestamp()), 1,
        date_trunc('minute', statement_timestamp()), 1)
ON CONFLICT (ip_hash) DO UPDATE SET
    second_window = EXCLUDED.second_window,
    second_count = CASE WHEN r.second_window = EXCLUDED.second_window
        THEN least(r.second_count + 1, :second_limit + 1) ELSE 1 END,
    minute_window = EXCLUDED.minute_window,
    minute_count = CASE WHEN r.minute_window = EXCLUDED.minute_window
        THEN least(r.minute_count + 1, :minute_limit + 1) ELSE 1 END
RETURNING CASE
    WHEN minute_count > :minute_limit THEN greatest(1, ceil(extract(epoch FROM
        minute_window + interval '1 minute' - statement_timestamp())))
    WHEN second_count > :second_limit THEN 1
    ELSE 0 END AS retry_after, second_count, minute_count,
    greatest(1, ceil(extract(epoch FROM second_window + interval '1 second' - statement_timestamp()))) AS second_reset,
    greatest(1, ceil(extract(epoch FROM minute_window + interval '1 minute' - statement_timestamp()))) AS minute_reset
''')


# Board pages are interactive; never let another site frame them (clickjacking).
FRAMING_HEADERS = {'Content-Security-Policy': "frame-ancestors 'none'", 'X-Frame-Options': 'DENY'}


class PostgresLimiter:
    def __init__(self, engine, *, per_second=PER_SECOND, per_minute=PER_MINUTE, namespace=''):
        self.engine = engine
        self.per_second = per_second
        self.per_minute = per_minute
        self.namespace = namespace
        self.next_cleanup = 0
        self.lock = threading.Lock()

    def consume(self, key):
        return self.consume_with_status(key).retry_after

    def consume_with_status(self, key):
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            connection.execute(text("SET LOCAL lock_timeout = '1s'"))
            bucket = hashlib.sha256((self.namespace + key).encode()).hexdigest() if self.namespace else key
            row = connection.execute(CONSUME, {'key': bucket, 'second_limit': self.per_second,
                'minute_limit': self.per_minute}).mappings().one()
            decision = RateLimitDecision(int(row['retry_after']), max(0, self.per_second - row['second_count']),
                max(0, self.per_minute - row['minute_count']), int(row['second_reset']), int(row['minute_reset']),
                self.per_second, self.per_minute)
        # Bounded opportunistic expiry, once per minute per process.
        with self.lock:
            clean = time.monotonic() >= self.next_cleanup
            if clean:
                self.next_cleanup = time.monotonic() + 60
        if clean:
            try:
                with self.engine.begin() as connection:
                    connection.execute(text("SET LOCAL statement_timeout = '2s'"))
                    connection.execute(text('''DELETE FROM app.ip_rate_limits WHERE ip_hash IN (
                        SELECT ip_hash FROM app.ip_rate_limits
                        WHERE minute_window < statement_timestamp() - interval '10 minutes'
                        LIMIT 1000 FOR UPDATE SKIP LOCKED)'''))
            except Exception:
                logger.warning('Rate-limit cleanup deferred')
            if not self.namespace:
                try:
                    with self.engine.begin() as connection:
                        connection.execute(text("SET LOCAL statement_timeout = '2s'"))
                        connection.execute(text('SELECT app.expire_retry_receipts()'))
                except Exception:
                    logger.warning('Retry receipt expiry deferred')
        return decision


class MemoryLimiter:
    """Bounded per-process storage for development; production uses PostgresLimiter."""
    def __init__(self, *, per_second=PER_SECOND, per_minute=PER_MINUTE):
        self.per_second = per_second
        self.per_minute = per_minute
        self.entries = {}
        self.lock = threading.Lock()

    def consume(self, key):
        return self.consume_with_status(key).retry_after

    def consume_with_status(self, key):
        now = time.monotonic()
        second, minute = int(now), int(now // 60)
        with self.lock:
            if key not in self.entries and len(self.entries) >= 10000:
                self.entries = {k: v for k, v in self.entries.items() if v[2] == minute}
                if len(self.entries) >= 10000:
                    return RateLimitDecision(max(1, math.ceil(60 - now % 60)), 0, 0, 1, max(1, math.ceil(60 - now % 60)), self.per_second, self.per_minute)
            s, sc, m, mc = self.entries.get(key, (second, 0, minute, 0))
            sc = min(sc + 1, self.per_second + 1) if s == second else 1
            mc = min(mc + 1, self.per_minute + 1) if m == minute else 1
            self.entries[key] = (second, sc, minute, mc)
        second_reset, minute_reset = max(1, math.ceil(1 - now % 1)), max(1, math.ceil(60 - now % 60))
        retry = minute_reset if mc > self.per_minute else second_reset if sc > self.per_second else 0
        return RateLimitDecision(retry, max(0, self.per_second - sc), max(0, self.per_minute - mc),
                                 second_reset, minute_reset, self.per_second, self.per_minute)


def client_key(scope, production):
    headers = Headers(scope=scope)
    peer = (scope.get('client') or ('', 0))[0]
    if production and trusted_edge(headers):
        # The trusted proxy (BBOARD_EDGE_TOKEN) must overwrite this header with the
        # connecting client address. X-Forwarded-For may contain both caller and proxy addresses.
        raw = headers.get('x-bboard-client-ip', '')
    elif production and trusted_forwarder(peer):
        # A configured proxy appends the connecting IP. Ignore all earlier
        # caller-controlled entries. A different proxy topology needs review.
        values = headers.getlist('x-forwarded-for')
        raw = values[-1].rsplit(',', 1)[-1].strip() if values else ''
    else:
        # Unauthenticated callers never choose their own identity.
        raw = peer
    try:
        address = ipaddress.ip_address(raw)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        # One subscriber usually holds a whole IPv6 /64, so it shares one budget.
        normalized = (str(ipaddress.ip_network(f'{address}/64', strict=False))
                      if isinstance(address, ipaddress.IPv6Address) else str(address))
    except ValueError:
        # Missing/invalid trusted identity shares a bucket; never trust a fallback header.
        normalized = 'unknown'
    return hashlib.sha256(normalized.encode()).hexdigest()


class SecurityMiddleware:
    def __init__(self, app, origins):
        self.app = app
        self.origins = frozenset(origins)

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        head = scope.get('method') == 'HEAD'
        if head:
            scope = {**scope, 'method': 'GET', 'bboard.head_request': True}
            original_send = send
            async def send(message):
                if message['type'] == 'http.response.body':
                    message = {**message, 'body': b''}
                await original_send(message)
        state = scope['app'].state
        headers = {'Cache-Control': 'no-store', 'API-Version': '1', **FRAMING_HEADERS}
        origin = Headers(scope=scope).get('origin')
        if origin in self.origins:
            headers.update({'Access-Control-Allow-Origin': origin, 'Vary': 'Origin',
                            'Access-Control-Expose-Headers': ', '.join(RATE_LIMIT_EXPOSE_HEADERS)})
        try:
            decision = await run_in_threadpool(state.limiter.consume_with_status, client_key(scope, state.production))
            retry = decision.retry_after
            headers.update(decision.headers())
        except Exception:
            logger.warning('Rate-limit storage unavailable')
            status, detail = 503, 'temporarily_unavailable'
            headers['Retry-After'] = '5'
        else:
            if retry:
                status, detail = 429, 'rate_limit_exceeded'
                headers['Retry-After'] = str(retry)
            else:
                method, path = scope.get('method'), scope.get('path')
                route = public_route(scope)
                site_not_found = scope['type'] == 'http' and method == 'GET' and is_site_request(scope) and route is None
                if scope['type'] == 'websocket' and route is not None and (origin is None or origin in self.origins):
                    return await self.app(scope, receive, send)
                requested_method = Headers(scope=scope).get('access-control-request-method')
                preflight = method == 'OPTIONS' and origin in self.origins and public_route({**scope, 'method': requested_method}) is not None
                if scope['type'] == 'http' and (route is not None or site_not_found or preflight):
                    async def policy_send(message):
                        if message['type'] == 'http.response.start':
                            response_headers = message.setdefault('headers', [])
                            if path == '/mcp':
                                response_headers[:] = [(k, v) for k, v in response_headers if k.lower() != b'cache-control']
                                response_headers.extend([(b'cache-control', b'no-store'), (b'x-robots-tag', b'noindex, noarchive')])
                            response_headers.append((b'x-bboard-access-policy', b'agent-pages-v7'))
                            response_headers.append((b'api-version', b'1'))
                            response_headers.extend((name.lower().encode(), value.encode()) for name, value in FRAMING_HEADERS.items())
                            response_headers.extend((name.lower().encode(), value.encode()) for name, value in decision.headers().items())
                        await send(message)
                    if not preflight and Headers(scope=scope).get('api-version', '1') != '1':
                        return await JSONResponse({'detail': 'unsupported_api_version', 'supported_versions': ['1']},
                            status_code=400, headers=headers)(scope, receive, send)
                    if site_not_found:
                        from starlette.requests import Request
                        return await not_found(Request(scope, receive))(scope, receive, policy_send)
                    return await self.app(scope, receive, policy_send)
                # No authentication provider has been selected. Fail closed even
                # when a caller supplies an arbitrary Bearer token or identity header.
                status, detail = 401, 'authentication_required'
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        return await JSONResponse({'detail': detail}, status_code=status, headers=headers)(scope, receive, send)
