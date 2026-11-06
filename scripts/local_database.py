#!/usr/bin/env python3
"""Create the bboard database on the local server that scripts/dev.py started, and apply migrations.

Run with backend/.venv's Python (it needs pg8000). The server listens on 127.0.0.1:55432
and keeps its data in .local/postgres.
"""
from pathlib import Path
import time

import pg8000.dbapi
from migrations import apply_migrations

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / '.local/postgres'
PORT = 55432


def main():
    deadline = time.monotonic() + 30
    while True:
        try:
            connection = pg8000.dbapi.connect(host='127.0.0.1', port=PORT, user='bboard', database='postgres', timeout=2)
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise SystemExit(f'Local PostgreSQL did not accept connections on port {PORT}') from None
            time.sleep(0.2)
    try:
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute('SHOW data_directory')
        if Path(cursor.fetchone()[0]).resolve() != DIRECTORY.resolve():
            raise SystemExit(f'Port {PORT} belongs to another PostgreSQL server; refusing to modify it')
        cursor.execute("SELECT 1 FROM pg_database WHERE datname='bboard'")
        if cursor.fetchone() is None:
            cursor.execute('CREATE DATABASE bboard')
    finally:
        connection.close()
    connection = pg8000.dbapi.connect(host='127.0.0.1', port=PORT, user='bboard', database='bboard', timeout=5)
    try:
        apply_migrations(connection.cursor())
        connection.commit()
    finally:
        connection.close()


if __name__ == '__main__':
    main()
