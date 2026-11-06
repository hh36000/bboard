"""Current runtime permissions, checked against an isolated local PostgreSQL role."""
import os
from pathlib import Path
import sys
import unittest

import pg8000.dbapi
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from database import Database


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Repository scripts are absent from backend-only Cloud Build contexts.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        from migrations import apply_migrations
        from verify_database import verify_runtime_permissions
        cls.verify = staticmethod(verify_runtime_permissions)
        cls.settings = dict(host='127.0.0.1', port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']),
                            user=os.environ['BBOARD_TEST_POSTGRES_USER'], timeout=5)
        cls.admin = pg8000.dbapi.connect(**cls.settings, database='postgres')
        cls.admin.autocommit = True
        cls.addClassCleanup(cls.admin.close)
        cursor = cls.admin.cursor()
        cursor.execute('CREATE DATABASE runtime_permissions_test')
        cursor.execute('CREATE ROLE bboard_permission_probe LOGIN')
        cls.owner = pg8000.dbapi.connect(**cls.settings, database='runtime_permissions_test')
        cls.addClassCleanup(cls.owner.close)
        cursor = cls.owner.cursor()
        apply_migrations(cursor)
        cursor.execute('GRANT USAGE ON SCHEMA app TO bboard_permission_probe')
        cursor.execute('GRANT SELECT, INSERT, UPDATE ON app.boards, app.board_writers TO bboard_permission_probe')
        cursor.execute('GRANT SELECT, INSERT ON app.board_appends, app.board_creations TO bboard_permission_probe')
        cursor.execute('GRANT SELECT ON app.board_events TO bboard_permission_probe')
        cursor.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON app.ip_rate_limits TO bboard_permission_probe')
        cls.owner.commit()

    def setUp(self):
        self.connection = pg8000.dbapi.connect(**{**self.settings, 'user': 'bboard_permission_probe'},
                                               database='runtime_permissions_test')
        self.addCleanup(self.connection.close)

    def assert_no_probe_data(self):
        cursor = self.owner.cursor()
        for table in ('boards', 'board_writers', 'board_appends', 'board_creations', 'board_events', 'ip_rate_limits'):
            cursor.execute(f'SELECT count(*) FROM app.{table}')
            self.assertEqual(cursor.fetchone()[0], 0, table)
        cursor.execute("SELECT to_regclass('app.must_not_be_allowed')")
        self.assertIsNone(cursor.fetchone()[0])
        self.owner.rollback()

    def test_runtime_needs_no_private_document_access_and_probes_rollback(self):
        engine = create_engine(URL.create('postgresql+pg8000', host=self.settings['host'],
            port=self.settings['port'], username='bboard_permission_probe', database='runtime_permissions_test'),
            connect_args={'timeout': 5})
        self.addCleanup(engine.dispose)
        Database(engine).check()
        with self.assertRaises(pg8000.dbapi.DatabaseError) as denied:
            self.connection.cursor().execute('SELECT id FROM app.documents LIMIT 0')
        self.assertEqual(denied.exception.args[0]['C'], '42501')
        self.connection.rollback()
        self.verify(self.connection)
        self.assert_no_probe_data()

    def test_missing_creation_permission_fails_and_rolls_back_earlier_writes(self):
        self.owner.cursor().execute('REVOKE INSERT ON app.board_creations FROM bboard_permission_probe')
        self.owner.commit()
        try:
            with self.assertRaises(pg8000.dbapi.DatabaseError) as denied:
                self.verify(self.connection)
            self.assertEqual(denied.exception.args[0]['C'], '42501')
            self.assert_no_probe_data()
        finally:
            self.owner.cursor().execute('GRANT INSERT ON app.board_creations TO bboard_permission_probe')
            self.owner.commit()

    def test_excess_schema_permission_fails_and_rolls_back_created_table(self):
        self.owner.cursor().execute('GRANT CREATE ON SCHEMA app TO bboard_permission_probe')
        self.owner.commit()
        try:
            with self.assertRaisesRegex(RuntimeError, 'schema creation privileges'):
                self.verify(self.connection)
            self.assert_no_probe_data()
        finally:
            self.owner.cursor().execute('REVOKE CREATE ON SCHEMA app FROM bboard_permission_probe')
            self.owner.commit()
