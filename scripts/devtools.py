"""Find local development tools. Standard library only, so any Python 3.11+ can run it."""
import glob
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == 'nt'
EMBEDDED = ROOT / '.local/embedded-postgres'
# PostgreSQL 17 builds for macOS, Linux, and Windows, published on npm.
EMBEDDED_PACKAGE = 'embedded-postgres@17.11.0-beta.19'
TOOLS = ('initdb', 'pg_ctl', 'postgres')


def venv_python():
    return ROOT / 'backend/.venv' / ('Scripts/python.exe' if WINDOWS else 'bin/python')


def tool(bin_dir, name):
    return str(Path(bin_dir) / (name + '.exe' if WINDOWS else name))


def has_tools(bin_dir):
    return all(Path(tool(bin_dir, name)).is_file() for name in TOOLS)


def postgres_bin(download=False):
    """BBOARD_POSTGRES_BIN, then an installed PostgreSQL, then a private copy in .local/."""
    configured = os.getenv('BBOARD_POSTGRES_BIN')
    if configured:
        if not has_tools(configured):
            raise SystemExit(f'BBOARD_POSTGRES_BIN={configured} lacks initdb, pg_ctl, or postgres')
        return configured
    found = shutil.which('initdb')
    candidates = [str(Path(found).parent)] if found else []
    candidates += ['/opt/homebrew/opt/postgresql@17/bin', '/usr/local/opt/postgresql@17/bin',
                   *sorted(glob.glob('/usr/lib/postgresql/*/bin'), reverse=True),
                   *glob.glob('/Applications/Postgres.app/Contents/Versions/latest/bin')]
    embedded = lambda: glob.glob(str(EMBEDDED / 'node_modules/@embedded-postgres/*/native/bin'))
    for candidate in candidates + embedded():
        if has_tools(candidate):
            return candidate
    if not download:
        raise SystemExit('PostgreSQL not found. Run `python3 scripts/dev.py --install-only` to download '
                         'a private copy, or install PostgreSQL and set BBOARD_POSTGRES_BIN.')
    npm = shutil.which('npm')
    if not npm:
        raise SystemExit('Install Node.js 22.12 or newer; npm is needed to download PostgreSQL.')
    print('PostgreSQL not found; downloading a private copy of PostgreSQL 17 into .local/ (one time)...', flush=True)
    EMBEDDED.mkdir(parents=True, exist_ok=True)
    subprocess.run([npm, 'install', '--no-audit', '--no-fund', '--prefix', str(EMBEDDED), EMBEDDED_PACKAGE], check=True)
    # Newer npm skips install scripts; this one restores the shared-library symlinks Linux needs.
    for script in glob.glob(str(EMBEDDED / 'node_modules/@embedded-postgres/*/scripts/hydrate-symlinks.js')):
        subprocess.run([shutil.which('node') or 'node', script], check=True)
    for candidate in embedded():
        if has_tools(candidate):
            return candidate
    raise SystemExit('No PostgreSQL download is available for this platform; install PostgreSQL 17 instead.')
