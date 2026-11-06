"""Explicit database configuration. Any PostgreSQL reachable through pg8000 works."""
import ipaddress
import os
import ssl
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

TRUE, FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


def production() -> bool:
    """Production requires shared Postgres storage and trusted proxy settings.

    It defaults on for DB_MODE=postgres so a forgotten flag cannot fail open.
    """
    value = os.getenv("BBOARD_PRODUCTION", "").strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    if value:
        raise ValueError("BBOARD_PRODUCTION must be 1/true/yes/on or 0/false/no/off")
    return os.getenv("DB_MODE", "disabled") == "postgres"


def is_loopback(host: str | None) -> bool:
    if host is None:
        return True  # Unix-domain socket
    host = host.lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def connect_args(url: URL) -> dict:
    """pg8000 arguments. Remote connections verify TLS unless DATABASE_TLS=disable."""
    mode = os.getenv("DATABASE_TLS", "verify").strip().lower()
    if mode not in {"verify", "disable"}:
        raise ValueError("DATABASE_TLS must be verify or disable")
    args = {"timeout": 10}
    if mode == "verify" and not is_loopback(url.host):
        args["ssl_context"] = ssl.create_default_context(cafile=os.getenv("DATABASE_TLS_CA") or None)
    return args


@dataclass
class Database:
    engine: Engine | None = None

    def close(self):
        if self.engine is not None:
            self.engine.dispose()

    def check(self):
        if self.engine is None:
            raise RuntimeError("Database is not configured")
        with self.engine.connect() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            connection.execute(text("SELECT 1"))
            # Startup requires the tables used by board workflows and shared limits.
            connection.execute(text("SELECT ip_hash, second_window, second_count, minute_window, minute_count FROM app.ip_rate_limits LIMIT 0"))
            connection.execute(text("SELECT id, content, revision FROM app.boards LIMIT 0"))
            connection.execute(text("SELECT document_id, client_id, sequence, revision FROM app.board_writers LIMIT 0"))
            connection.execute(text("SELECT board_id, operation_id, content_hash, revision FROM app.board_appends LIMIT 0"))
            connection.execute(text("SELECT operation_id, board_id, content_hash FROM app.board_creations LIMIT 0"))
            connection.execute(text("SELECT board_id, revision, recorded_at, kind, content, change FROM app.board_events LIMIT 0"))


def database_url() -> URL:
    url = make_url(os.environ["DATABASE_URL"])
    if url.drivername != "postgresql+pg8000":
        raise ValueError("DATABASE_URL must use the postgresql+pg8000 driver")
    return url


def create_database() -> Database:
    """DB_MODE=disabled (tests), local (loopback only), or postgres (any host)."""
    mode = os.getenv("DB_MODE", "disabled")
    options = dict(pool_size=2, max_overflow=0, pool_timeout=5,
                   pool_recycle=1800, pool_pre_ping=True)
    if mode not in {"disabled", "local", "postgres"}:
        raise ValueError("DB_MODE must be disabled, local, or postgres")
    if mode == "disabled":
        if production():
            raise ValueError("Production requires DB_MODE=postgres")
        return Database()
    if mode == "local" and production():
        raise ValueError("Local database mode is not allowed in production")
    url = database_url()
    if mode == "local" and (url.host is None or not is_loopback(url.host)):
        raise ValueError("Local mode requires a loopback PostgreSQL URL")
    return Database(create_engine(url, connect_args=connect_args(url), **options))
