"""Apply immutable, checksummed migrations.

Hosted use: DATABASE_URL=postgresql+pg8000://OWNER:PASSWORD@HOST/bboard backend/.venv/bin/python scripts/migrations.py
Connect as the role that should own the schema, not the runtime role. DATABASE_TLS and
DATABASE_TLS_CA apply as they do for the backend.
"""
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def apply_migrations(cursor):
    cursor.execute("SET statement_timeout = '30s'")
    cursor.execute("SELECT pg_advisory_xact_lock(7462829)")
    cursor.execute("CREATE SCHEMA IF NOT EXISTS app")
    cursor.execute("CREATE TABLE IF NOT EXISTS app.schema_migrations (version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
    for migration in sorted((ROOT / "backend/migrations").glob("*.sql")):
        sql = migration.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        cursor.execute("SELECT checksum FROM app.schema_migrations WHERE version = %s", (migration.name,))
        existing = cursor.fetchone()
        if existing:
            if existing[0] != checksum:
                raise RuntimeError(f"Applied migration was modified: {migration.name}")
            continue
        cursor.execute(sql)
        cursor.execute("INSERT INTO app.schema_migrations (version, checksum) VALUES (%s, %s)",
                       (migration.name, checksum))
        print(f"Applied {migration.name}", flush=True)


def main():
    sys.path.insert(0, str(ROOT / "backend"))
    from database import connect_args, database_url
    import pg8000.dbapi
    url = database_url()
    connection = pg8000.dbapi.connect(user=url.username, password=url.password, host=url.host or "localhost",
                                      port=url.port or 5432, database=url.database,
                                      unix_sock=url.query.get("unix_sock"), **connect_args(url))
    try:
        apply_migrations(connection.cursor())
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
