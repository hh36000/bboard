#!/usr/bin/env python3
"""Run backend tests against a disposable loopback PostgreSQL cluster."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from devtools import ROOT, WINDOWS, postgres_bin, tool, venv_python


def main():
    bin_dir = postgres_bin(download=True)
    initdb, pg_ctl = tool(bin_dir, 'initdb'), tool(bin_dir, 'pg_ctl')
    with tempfile.TemporaryDirectory(prefix='bboard-pg-test-') as temp:
        directory = Path(temp)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        subprocess.run([initdb, '-D', str(directory/'data'), '-U', 'postgres', '-A', 'trust', '--no-locale', '--encoding=UTF8'],
                       check=True, capture_output=True, timeout=30)
        subprocess.run([pg_ctl, '-D', str(directory/'data'), '-l', str(directory/'server.log'),
                        '-o', f'-h 127.0.0.1 -p {port}' + ('' if WINDOWS else f' -k {directory}'), '-w', 'start'],
                       check=True, capture_output=True, timeout=30)
        try:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('GOOGLE_', 'GCLOUD_', 'CLOUDSDK_', 'DB_', 'DATABASE_', 'BBOARD_', 'K_'))}
            env.update(BBOARD_TEST_POSTGRES_PORT=str(port),
                       BBOARD_TEST_POSTGRES_USER='postgres', DB_MODE='disabled')
            result = subprocess.run([str(venv_python()), '-m', 'unittest',
                                     'discover', '-s', 'tests', '-v'], cwd=ROOT/'backend', env=env, timeout=45)
        finally:
            subprocess.run([pg_ctl, '-D', str(directory/'data'), '-m', 'fast', '-w', 'stop'],
                           check=True, capture_output=True, timeout=15)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
