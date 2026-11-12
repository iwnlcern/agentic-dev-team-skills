"""Evidence schema and object behavior, independent of the caller's admission."""
from dataclasses import replace
import re
import sqlite3
import unittest
from unittest import mock

from relay_engine import errors, schema
from relay_engine.tests.test_submit import Store as SubmitStore


EVIDENCE_TABLES = frozenset({
    'evidence_objects', 'evidence_object_bundles', 'evidence_bundles',
    'evidence_captures', 'evidence_uploads', 'evidence_retention',
    'evidence_derive', 'evidence_prune_marks', 'evidence_pruned',
})


class TestEvidenceSchema(unittest.TestCase):
    def connect(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA foreign_keys=ON')
        schema.init_v3(conn)
        return conn

    def require_evidence(self, conn):
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(EVIDENCE_TABLES, names, 'missing evidence tables')

    def test_evidence_tables_are_strict_and_owned(self):
        # Breaks: omitting a table/owner, weakening STRICT, or adding object FKs
        # that prevent capture and association history from outliving a prune.
        conn = self.connect()
        self.require_evidence(conn)
        self.assertEqual({t for t, owner in schema.TABLE_OWNERS.items()
                          if owner == 'store'}, EVIDENCE_TABLES)
        strict = {r[1]: r[5] for r in conn.execute('PRAGMA table_list')}
        for table in sorted(EVIDENCE_TABLES):
            with self.subTest(table=table):
                self.assertEqual(strict[table], 1)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='evidence_objects'").fetchone()[0]
        self.assertRegex(sql, r'object_id INTEGER PRIMARY KEY AUTOINCREMENT')
        for table in ('evidence_captures', 'evidence_object_bundles'):
            with self.subTest(table=table):
                foreign = list(conn.execute('PRAGMA foreign_key_list(' + table + ')'))
                self.assertFalse(any(r[3] == 'object_id' for r in foreign))
                indexes = [r[1] for r in conn.execute('PRAGMA index_list(' + table + ')')]
                indexed = [tuple(r[2] for r in conn.execute('PRAGMA index_info(' + name + ')'))
                           for name in indexes]
                self.assertTrue(any(cols and cols[0] == 'object_id' for cols in indexed))
        for table in ('evidence_object_bundles', 'evidence_bundles'):
            foreign = list(conn.execute('PRAGMA foreign_key_list(' + table + ')'))
            self.assertTrue(any(r[2:5] == ('bundles', 'bundle_id', 'id') for r in foreign))
            self.assertTrue(all(r[5] == r[6] == 'NO ACTION' for r in foreign))

    def test_evidence_tables_not_hook_writable(self):
        # Breaks: the old owner-label denylist permits Store tables to a hook.
        self.require_evidence(self.connect())
        fixture = SubmitStore()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for table in sorted(EVIDENCE_TABLES):
            with self.subTest(table=table), self.assertRaises(ValueError):
                replace(fixture.ctx(), after_admit_writes=frozenset({table}))

    def test_old_store_refuses_after_evidence_ddl(self):
        # Breaks: opening the actual pre-evidence layout as current or writing
        # the new tables into it. The independent control opens today's layout.
        control = self.connect()
        self.require_evidence(control)
        old_tables = tuple(sql for sql in schema._TABLES if not re.search(
            r'CREATE TABLE(?: IF NOT EXISTS)? evidence_', sql))
        old_indexes = tuple(sql for sql in schema._INDEXES if not re.search(
            r' ON evidence_', sql))
        old = sqlite3.connect(':memory:')
        self.addCleanup(old.close)
        with mock.patch.object(schema, '_TABLES', old_tables), mock.patch.object(
                schema, '_INDEXES', old_indexes):
            schema.init_v3(old)
        before = old.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        digest = old.execute('SELECT ddl_sha256 FROM schema_version').fetchone()
        with self.assertRaises(errors.EngineError) as caught:
            schema.init_v3(old)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')
        self.assertEqual(old.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall(), before)
        self.assertEqual(old.execute('SELECT ddl_sha256 FROM schema_version').fetchone(), digest)
        self.assertFalse(old.in_transaction)
        schema.init_v3(control)
        self.assertFalse(control.in_transaction)


if __name__ == '__main__':
    unittest.main()
