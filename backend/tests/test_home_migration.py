"""Retiring root storage must preserve keyed boards and allow startup."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import pg8000.dbapi
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from database import Database
from main import app


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class HomeMigrationTests(unittest.TestCase):
    def test_cleanup_preserves_boards_and_startup_without_root_tables(self):
        # Cloud Build uploads only backend/ and skips PostgreSQL integration tests.
        # Load the repository's migration runner only when this test actually runs.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        import migrations

        settings = dict(host='127.0.0.1', port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']),
                        user=os.environ['BBOARD_TEST_POSTGRES_USER'], timeout=5)
        admin = pg8000.dbapi.connect(**settings, database='postgres')
        admin.autocommit = True
        admin.cursor().execute('CREATE DATABASE home_migration_test')
        self.addCleanup(admin.close)
        connection = pg8000.dbapi.connect(**settings, database='home_migration_test')
        self.addCleanup(connection.close)
        source = Path(__file__).resolve().parents[1] / 'migrations'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'backend/migrations'
            target.mkdir(parents=True)
            for path in sorted(source.glob('00[1-4]_*.sql')):
                shutil.copy(path, target)
            with patch.object(migrations, 'ROOT', root):
                cursor = connection.cursor()
                migrations.apply_migrations(cursor)
                cursor.execute("UPDATE app.realtime_documents SET content='old root', revision=1")
                cursor.execute("INSERT INTO app.realtime_document_writers VALUES ('00000000-0000-0000-0000-000000000001', 'main', 1, 1)")
                cursor.execute("INSERT INTO app.boards (id, content, revision) VALUES (%s, 'keep board', 1)", ('b' * 64,))
                cursor.execute("INSERT INTO app.board_writers (document_id, client_id, sequence, revision) VALUES (%s, '00000000-0000-0000-0000-000000000001', 1, 1)", ('b' * 64,))
                connection.commit()
                shutil.copy(source / '005_retire_home_document.sql', target)
                shutil.copy(source / '006_board_appends.sql', target)
                shutil.copy(source / '007_board_creations.sql', target)
                shutil.copy(source / '008_board_events.sql', target)
                shutil.copy(source / '009_exact_patch_events.sql', target)
                migrations.apply_migrations(cursor)
                connection.commit()
                migrations.apply_migrations(cursor)
                connection.commit()
                cursor.execute("SELECT to_regclass('app.realtime_documents'), to_regclass('app.realtime_document_writers')")
                self.assertEqual(tuple(cursor.fetchone()), (None, None))
                cursor.execute('SELECT content, revision FROM app.boards')
                self.assertEqual(tuple(cursor.fetchone()), ('keep board', 1))
                cursor.execute('SELECT sequence, revision FROM app.board_writers')
                self.assertEqual(tuple(cursor.fetchone()), (1, 1))
                connection.rollback()
        engine = create_engine(URL.create('postgresql+pg8000', host=settings['host'], port=settings['port'],
                               username=settings['user'], database='home_migration_test'), connect_args={'timeout': 5})
        self.addCleanup(engine.dispose)
        with patch.dict(os.environ, {'DB_MODE': 'disabled'}, clear=True), \
             patch('main.create_database', return_value=Database(engine)), TestClient(app) as client:
            self.assertEqual(client.get('/').json()['title'], 'bboard.ai: Shared text for AI agents')
            self.assertEqual(client.get('/' + 'b' * 64).json()['content'], 'keep board')
