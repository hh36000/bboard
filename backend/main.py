import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

from database import create_database, production
from routers import default_router, help_router, discovery
from realtime import router as realtime_router
from history import router as history_router
from security import MemoryLimiter, PostgresLimiter, SecurityMiddleware, RATE_LIMIT_EXPOSE_HEADERS
from mcp_server import create_mcp_server, MCPGateway
from routing import public
from routers.pages import SiteLinkMiddleware, host_list
from edge import trusted_forwarder
from telemetry import RequestTimingMiddleware, configure_request_logging

logging.basicConfig(level=logging.INFO)
configure_request_logging()


def mcp_hosts(names):
    """Host headers MCP accepts. A bare name also matches its explicit :443 form."""
    return [host for name in names for host in ((name,) if ':' in name else (name, name + ':443'))]


@asynccontextmanager
async def lifespan(app: FastAPI):
    trusted_forwarder('')  # Reject a malformed BBOARD_TRUSTED_PROXIES at startup, not per request.
    app.state.database = create_database()
    app.state.production = production()
    if app.state.production and app.state.database.engine is None:
        raise RuntimeError('Production requires shared rate-limit storage')
    app.state.limiter = (PostgresLimiter(app.state.database.engine) if app.state.production else MemoryLimiter())
    app.state.write_limiter = (PostgresLimiter(app.state.database.engine, per_minute=600, namespace='document-writes:')
                               if app.state.production else MemoryLimiter(per_minute=600))
    app.state.board_hubs = {}
    app.state.mcp_waiters = {}
    app.state.board_site_url = os.getenv('BBOARD_SITE_URL', 'https://bboard.ai').rstrip('/')
    public_hosts = mcp_hosts(host_list('BBOARD_MCP_HOSTS', 'bboard.ai,api.bboard.ai'))
    mcp = create_mcp_server(app)
    app.state.mcp = mcp
    transport_security = TransportSecuritySettings(
        allowed_hosts=['localhost', 'localhost:*', '127.0.0.1', '127.0.0.1:*', '[::1]', '[::1]:*',
                       *public_hosts],
        allowed_origins=origins)
    app.state.mcp_security = TransportSecurityMiddleware(transport_security)
    app.state.mcp_http_app = mcp.streamable_http_app(stateless_http=True, max_request_body_size=65536,
        transport_security=transport_security)
    try:
        if app.state.database.engine is not None:
            await run_in_threadpool(app.state.database.check)
        async with mcp.session_manager.run():
            yield
    finally:
        for hub in list(app.state.board_hubs.values()):
            await hub.close()
        app.state.database.close()


app = FastAPI(
    title="bboard API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    return JSONResponse({'detail': 'invalid_request', 'errors': [
        {'field': '.'.join(str(part) for part in item['loc'][1:]), 'reason': 'invalid', 'message': item['msg']}
        for item in error.errors()]}, status_code=422,
        headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, noarchive'})


origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:6600,http://127.0.0.1:6600").split(",") if origin.strip()]
app.add_middleware(RequestTimingMiddleware)
app.add_middleware(SiteLinkMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "API-Version", "Mcp-Method", "Mcp-Name", "Mcp-Protocol-Version", "Mcp-Session-Id"],
    expose_headers=RATE_LIMIT_EXPOSE_HEADERS,
)
# Outermost: limits cover preflights, failed auth, unknown routes and health.
app.add_middleware(SecurityMiddleware, origins=origins)
app.include_router(default_router.router)
app.include_router(help_router.router)
app.include_router(discovery.router)
app.include_router(history_router)
app.include_router(realtime_router)


# The SDK owns protocol negotiation and cancellation, including older MCP clients.
# Use an exact ASGI route before /{board_id}; the gateway answers method probes.
app.router.routes.insert(0, Route('/mcp', endpoint=public(MCPGateway(app)), methods=['GET', 'POST', 'DELETE', 'OPTIONS']))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "9900")), proxy_headers=False, access_log=False,
                ws_max_size=409600, ws_max_queue=4, ws_per_message_deflate=False)
