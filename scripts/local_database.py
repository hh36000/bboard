#!/usr/bin/env python3.11
"""Prepare a persistent, loopback-only bboard PostgreSQL server in shared tmux."""
from pathlib import Path
import os
import shlex
import shutil
import socket
import subprocess
import time

import pg8000.dbapi
from migrations import apply_migrations

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / '.local/postgres'
PORT = 55432
SESSION = 'bboard-dev'


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def main():
    binary = os.getenv('BBOARD_POSTGRES_BIN')
    candidates = [Path(binary)] if binary else [Path('/opt/homebrew/opt/postgresql@17/bin')]
    postgres = shutil.which('postgres') or next((str(path / 'postgres') for path in candidates if (path / 'postgres').is_file()), None)
    if not postgres:
        raise RuntimeError('Install PostgreSQL locally, or set BBOARD_POSTGRES_BIN to its bin directory')
    bin_dir = Path(postgres).parent
    if not (DIRECTORY / 'PG_VERSION').exists():
        DIRECTORY.parent.mkdir(parents=True, exist_ok=True)
        run(str(bin_dir / 'initdb'), '-D', str(DIRECTORY), '-U', 'bboard', '-A', 'trust', '--no-locale', '--encoding=UTF8')
    with socket.socket() as sock:
        running = sock.connect_ex(('127.0.0.1', PORT)) == 0
    if not running:
        if subprocess.run(['tmux', 'has-session', '-t', SESSION], capture_output=True, timeout=5).returncode:
            run('tmux', 'new-session', '-d', '-s', SESSION, '-n', 'backend', '-c', str(ROOT / 'backend'))
        command = shlex.join([postgres, '-D', str(DIRECTORY), '-h', '127.0.0.1', '-p', str(PORT), '-k', str(DIRECTORY.parent)])
        run('tmux', 'new-window', '-d', '-t', SESSION, '-n', 'postgres', command)
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = pg8000.dbapi.connect(host='127.0.0.1', port=PORT, user='bboard', database='postgres', timeout=2)
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError('Local PostgreSQL did not start; inspect bboard-dev:postgres') from None
            time.sleep(0.1)
    try:
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute('SHOW data_directory')
        if Path(cursor.fetchone()[0]).resolve() != DIRECTORY.resolve():
            raise RuntimeError('Port 55432 belongs to another PostgreSQL server; refusing to modify it')
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
    print('Local bboard PostgreSQL ready on 127.0.0.1:55432; data in .local/postgres')


if __name__ == '__main__':
    main()
