#!/usr/bin/env python3
"""Run bboard locally with one command: python3 scripts/dev.py

The first run installs Python and npm dependencies and, when PostgreSQL is not
installed, downloads a private copy into .local/. Every run starts PostgreSQL,
the backend, and the frontend in this terminal. Ctrl+C stops all three.
Needs only Python 3.11+ and Node.js 22.12+. Standard library only.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import devtools
from devtools import ROOT, WINDOWS, tool, venv_python

DATA = ROOT / '.local/postgres'
DB_PORT, API_PORT, WEB_PORT = 55432, 9900, 6600


def fail(message):
    raise SystemExit(f'bboard: {message}')


def check_runtimes():
    if sys.version_info < (3, 11):
        fail('Python 3.11 or newer is required.')
    node = shutil.which('node')
    if not node or not shutil.which('npm'):
        fail('Install Node.js 22.12 or newer (https://nodejs.org).')
    version = subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip().lstrip('v')
    major, minor = (int(part) for part in version.split('.')[:2])
    if (major, minor) < (22, 12):
        fail(f'Node.js {version} is too old; install 22.12 or newer.')
    return node


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install():
    """Install dependencies when missing or when their manifests changed."""
    requirements, marker = ROOT / 'backend/requirements-dev.txt', ROOT / 'backend/.venv/.bboard-requirements'
    if not venv_python().exists():
        print('Creating backend/.venv...', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / 'backend/.venv')], check=True)
    if not marker.exists() or marker.read_text() != fingerprint(requirements):
        print('Installing Python packages...', flush=True)
        subprocess.run([str(venv_python()), '-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', str(requirements)], check=True)
        marker.write_text(fingerprint(requirements))
    lock, marker = ROOT / 'frontend/package-lock.json', ROOT / 'frontend/node_modules/.bboard-lock'
    if not marker.exists() or marker.read_text() != fingerprint(lock):
        print('Installing npm packages...', flush=True)
        subprocess.run([shutil.which('npm'), 'ci', '--no-audit', '--no-fund'], cwd=ROOT / 'frontend', check=True)
        marker.write_text(fingerprint(lock))
    return devtools.postgres_bin(download=True)


def listening(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) == 0


def public_origin():
    """The browser's address for the frontend, including GitHub Codespaces' forwarded URL."""
    name, domain = os.getenv('CODESPACE_NAME'), os.getenv('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN')
    return f'https://{name}-{WEB_PORT}.{domain}' if name and domain else f'http://localhost:{WEB_PORT}'


def start(label, command, cwd, env=None):
    process = subprocess.Popen(command, cwd=cwd, env={**os.environ, **(env or {})},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace',
                               start_new_session=not WINDOWS,
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
    def relay():
        for line in process.stdout:
            print(f'[{label}] {line}', end='', flush=True)
    threading.Thread(target=relay, daemon=True).start()
    return process


def stop(process):
    if process.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGINT)  # PostgreSQL treats SIGINT as a fast shutdown.
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL) if not WINDOWS else process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--install-only', action='store_true', help='install dependencies and PostgreSQL, then exit')
    args = parser.parse_args()
    node = check_runtimes()
    bin_dir = install()
    if args.install_only:
        print('Dependencies installed. Start bboard with: python3 scripts/dev.py')
        return
    for port in (API_PORT, WEB_PORT):
        if listening(port):
            fail(f'Port {port} is already in use. Stop that process and try again.')
    # Services run in their own process groups, so stop them on any shutdown signal,
    # not only Ctrl+C (closing the terminal sends SIGHUP; `kill` sends SIGTERM).
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupt)
    for name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)

    if not (DATA / 'PG_VERSION').exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([tool(bin_dir, 'initdb'), '-D', str(DATA), '-U', 'bboard', '-A', 'trust',
                        '--no-locale', '--encoding=UTF8'], check=True, capture_output=True)
    processes = []
    try:
        if listening(DB_PORT):
            print(f'Reusing the PostgreSQL server already on port {DB_PORT}.', flush=True)
        else:
            sockets = [] if WINDOWS else ['-k', str(DATA.parent)]
            processes.append(start('db', [tool(bin_dir, 'postgres'), '-D', str(DATA), '-h', '127.0.0.1',
                                          '-p', str(DB_PORT), *sockets], ROOT))
        subprocess.run([str(venv_python()), str(ROOT / 'scripts/local_database.py')], check=True)

        origin = public_origin()
        origins = ','.join(dict.fromkeys([origin, f'http://localhost:{WEB_PORT}', f'http://127.0.0.1:{WEB_PORT}']))
        processes.append(start('api', [str(venv_python()), '-m', 'uvicorn', 'main:app', '--reload',
            '--host', '127.0.0.1', '--port', str(API_PORT), '--no-proxy-headers', '--no-access-log',
            '--ws-max-size', '409600', '--ws-max-queue', '4', '--ws-per-message-deflate', 'false'], ROOT / 'backend',
            {'DB_MODE': 'local', 'DATABASE_URL': f'postgresql+pg8000://bboard@127.0.0.1:{DB_PORT}/bboard',
             'BBOARD_SITE_URL': origin, 'CORS_ORIGINS': origins}))
        processes.append(start('web', [node, 'node_modules/vite/bin/vite.js'], ROOT / 'frontend'))
        print(f'\nbboard is starting at {origin}  (Ctrl+C stops everything)\n', flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.3)
        fail('a service stopped unexpectedly; see the output above.')
    except KeyboardInterrupt:
        print('\nStopping bboard...', flush=True)
    finally:
        for process in reversed(processes):
            stop(process)


if __name__ == '__main__':
    main()
