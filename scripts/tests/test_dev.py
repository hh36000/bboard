"""Launcher logic with temporary directories and environment; never starts real services."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev
import devtools


class DevTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='bboard-dev-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def fake_postgres(self, path):
        path.mkdir(parents=True)
        for name in devtools.TOOLS:
            (path / devtools.tool('', name)).touch()
        return path

    def test_public_origin_uses_the_codespaces_forwarded_address(self):
        with patch.dict(os.environ, {'CODESPACE_NAME': 'happy-space', 'GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN': 'app.github.dev'}):
            self.assertEqual(dev.public_origin(), 'https://happy-space-6600.app.github.dev')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dev.public_origin(), 'http://localhost:6600')

    def test_configured_postgres_must_contain_the_tools(self):
        good = self.fake_postgres(self.root / 'good')
        with patch.dict(os.environ, {'BBOARD_POSTGRES_BIN': str(good)}):
            self.assertEqual(devtools.postgres_bin(), str(good))
        with patch.dict(os.environ, {'BBOARD_POSTGRES_BIN': str(self.root / 'empty')}):
            with self.assertRaisesRegex(SystemExit, 'lacks initdb'):
                devtools.postgres_bin()

    def test_installed_postgres_wins_over_the_private_copy(self):
        installed = self.fake_postgres(self.root / 'installed')
        private = self.fake_postgres(self.root / 'private/node_modules/@embedded-postgres/x/native/bin')
        fakes = {str(installed), str(private)}  # Ignore any PostgreSQL really installed on this machine.
        with patch.dict(os.environ, {}, clear=True), patch.object(devtools, 'EMBEDDED', self.root / 'private'), \
             patch.object(devtools, 'has_tools', side_effect=lambda path: str(path) in fakes), \
             patch('devtools.glob.glob', side_effect=lambda pattern: [str(private)] if 'embedded' in pattern else []):
            with patch('devtools.shutil.which', return_value=str(installed / 'initdb')):
                self.assertEqual(devtools.postgres_bin(), str(installed))
            with patch('devtools.shutil.which', return_value=None):
                self.assertEqual(devtools.postgres_bin(), str(private))

    def test_missing_postgres_without_download_explains_the_fix(self):
        with patch.dict(os.environ, {}, clear=True), patch('devtools.shutil.which', return_value=None), \
             patch('devtools.glob.glob', return_value=[]), patch.object(devtools, 'has_tools', return_value=False):
            with self.assertRaisesRegex(SystemExit, 'install-only'):
                devtools.postgres_bin()


if __name__ == '__main__':
    unittest.main()
