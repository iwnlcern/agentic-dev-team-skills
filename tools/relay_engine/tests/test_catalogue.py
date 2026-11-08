"""Machine catalogue identity, classification and pre-release refusal."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from relay_engine import catalogue, errors, store_layout


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class TestCatalogue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'

    def opened(self, root=None, **kwargs):
        conn = store_layout.open_catalogue(root or self.root, **kwargs)
        self.addCleanup(conn.close)
        return conn

    def identity(self, conn):
        return conn.execute('SELECT store_id, kind, created_at FROM catalogue_identity').fetchall()

    def snapshot(self, conn):
        return '\n'.join(conn.iterdump())

    def test_init_empty_builds_and_identifies(self):
        conn = self.opened()
        rows = self.identity(conn)
        self.assertEqual(len(rows), 1)
        identity, kind, created = rows[0]
        self.assertEqual(str(UUID(identity)), identity)
        self.assertEqual(UUID(identity).version, 7)
        self.assertEqual(kind, 'isolated')
        self.assertIsInstance(created, int)
        self.assertEqual(self.identity(self.opened()), rows)
        production = Path(self.tmp.name) / 'production'
        rows = self.identity(self.opened(production, kind='production'))
        self.assertEqual(rows[0][1], 'production')
        self.assertEqual(self.identity(self.opened(production)), rows)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_bound_call_never_resolves_default(self):
        with mock.patch.object(store_layout, 'production_root', side_effect=AssertionError) as resolver:
            for index, kwargs in enumerate([{}, {'kind': 'isolated'}]):
                root = Path(self.tmp.name) / str(index)
                rows = self.identity(self.opened(root, **kwargs))
                self.assertEqual(rows[0][1], 'isolated')
                self.assertEqual(self.identity(self.opened(root, **kwargs)), rows)
            resolver.assert_not_called()
        # Control: comparing with a default path does resolve the default.
        with mock.patch.object(store_layout, 'production_root', return_value=self.root) as resolver:
            self.assertEqual(self.root, store_layout.production_root())
            resolver.assert_called_once_with()

    def test_default_never_creates_production(self):
        with mock.patch.object(store_layout, 'production_root', return_value=self.root) as resolver:
            self.assertEqual(self.identity(self.opened(str(self.root)))[0][1], 'isolated')
            resolver.assert_not_called()

    def test_explicit_other_kind_refuses(self):
        for initial, other in [('isolated', 'production'), ('production', 'isolated')]:
            with self.subTest(initial=initial):
                root = Path(self.tmp.name) / initial
                conn = self.opened(root, kind=initial)
                before = self.snapshot(conn)
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    self.opened(root, kind=other)
                self.assertEqual(caught.exception.cause, 'wrong-kind')
                self.assertEqual(self.snapshot(conn), before)

    def test_init_refuses_other_digest(self):
        for mutation in ["UPDATE schema_version SET ddl_sha256 = 'old'",
                         'CREATE TABLE extra(value TEXT) STRICT']:
            with self.subTest(mutation=mutation):
                root = Path(self.tmp.name) / str(len(mutation))
                conn = self.opened(root)
                conn.execute(mutation)
                if conn.in_transaction:
                    conn.execute('COMMIT')
                before = self.snapshot(conn)
                with self.assertRaises(errors.EngineError) as caught:
                    self.opened(root)
                self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')
                self.assertEqual(self.snapshot(conn), before)

    def test_owner_map_covers_every_table(self):
        conn = self.opened()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(set(catalogue.TABLE_OWNERS), names)
        self.assertEqual(set(catalogue.TABLE_OWNERS.values()), {'store'})
        self.assertEqual(conn.execute('SELECT version, ddl_sha256 FROM schema_version').fetchone(),
                         ('1', catalogue.ddl_digest()))

    def test_name_is_unique(self):
        conn = self.opened()
        conn.execute("INSERT INTO project_routes VALUES (?, 'same', 'creating', 1)", (P,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO project_routes VALUES (?, 'same', 'creating', 1)", (Q,))
        conn.execute('ROLLBACK')

    def test_catalogue_opens_wal(self):
        conn = self.opened()
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        self.assertEqual(conn.execute('PRAGMA synchronous').fetchone(), (2,))
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone(), (1,))
        for pragma in ['fullfsync', 'checkpoint_fullfsync']:
            self.assertEqual(conn.execute('PRAGMA ' + pragma).fetchone(),
                             (1 if sys.platform == 'darwin' else 0,))
        with mock.patch.object(store_layout, '_row', return_value=('delete',)):
            with self.assertRaises(store_layout.LayoutFault) as caught:
                self.opened()
        self.assertEqual(caught.exception.cause, 'not-wal')


if __name__ == '__main__':
    unittest.main()
