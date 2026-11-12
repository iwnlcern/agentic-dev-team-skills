"""Machine catalogue identity, classification and pre-release refusal."""
from pathlib import Path
import hashlib
import inspect
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from relay_engine import catalogue, errors, store_layout, submit


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

    def test_catalogue_file_is_machine_sqlite3(self):
        self.opened()
        entries = {path.name for path in self.root.iterdir()}
        self.assertIn('machine.sqlite3', entries)
        self.assertNotIn('catalogue.sqlite3', entries)
        self.assertLessEqual(entries, {'machine.sqlite3', 'machine.sqlite3-wal',
                                      'machine.sqlite3-shm', 'machine.sqlite3-journal'})

    def test_bound_call_never_resolves_default(self):
        with mock.patch.object(store_layout, 'production_root', side_effect=AssertionError) as resolver:
            for index, kwargs in enumerate([{}, {'kind': 'isolated'}]):
                root = Path(self.tmp.name) / str(index)
                rows = self.identity(self.opened(root, **kwargs))
                self.assertEqual(rows[0][1], 'isolated')
                self.assertEqual(self.identity(self.opened(root, **kwargs)), rows)
            resolver.assert_not_called()
        # Control: run a classifying variant of the opener itself.
        source = inspect.getsource(store_layout.open_catalogue)
        self.assertIn("kind or 'isolated'", source)
        source = source.replace("kind or 'isolated'",
                                "kind or ('production' if root == production_root() else 'isolated')")
        with mock.patch.object(store_layout, 'production_root', side_effect=AssertionError('resolved')):
            namespace = dict(vars(store_layout))
            exec(compile(source, '<classifying-opener>', 'exec'), namespace)
            with self.assertRaisesRegex(AssertionError, 'resolved'):
                namespace['open_catalogue'](Path(self.tmp.name) / 'classifying')

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
                         'CREATE TABLE extra(value TEXT) STRICT',
                         'ALTER TABLE project_routes ADD COLUMN extra TEXT']:
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

    def test_missing_catalogue_with_projects_refuses(self):
        # M12: a lost catalogue must not silently become a new empty store.
        for fixture in ['absent', 'empty', 'no-tables']:
            with self.subTest(fixture=fixture):
                root = Path(self.tmp.name) / fixture
                ledger = store_layout.open_ledger(root, P, create=True)
                ledger.close()
                path = root / 'machine.sqlite3'
                if fixture == 'empty':
                    path.touch()
                elif fixture == 'no-tables':
                    conn = sqlite3.connect(path)
                    conn.execute('PRAGMA user_version=1').close()
                    conn.close()
                before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
                entries = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    self.opened(root)
                self.assertEqual(caught.exception.cause, 'missing-catalogue')
                after = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
                self.assertEqual(after, before)
                self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('*')), entries)
        # An empty projects directory still permits a genuinely new store.
        root = Path(self.tmp.name) / 'empty-projects'
        (root / 'projects').mkdir(parents=True)
        self.assertEqual(self.identity(self.opened(root))[0][1], 'isolated')

    def test_creation_is_one_transaction(self):
        # M13: a commit between schema and identity strands a half-created store.
        with mock.patch.object(submit, 'new_uuid7', side_effect=RuntimeError('before identity')):
            with self.assertRaisesRegex(RuntimeError, 'before identity'):
                self.opened(kind='production')
        probe = sqlite3.connect(self.root / 'machine.sqlite3')
        try:
            self.assertEqual(probe.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        finally:
            probe.close()
        trace = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(trace.append)
            return conn

        with mock.patch.object(sqlite3, 'connect', side_effect=traced):
            conn = self.opened(kind='production')
        self.assertEqual(self.identity(conn)[0][1], 'production')
        self.assertEqual([sql for sql in trace if sql in ('BEGIN IMMEDIATE', 'COMMIT')],
                         ['BEGIN IMMEDIATE', 'COMMIT'])

    def test_tables_without_identity_refuse(self):
        for kwargs in [{}, {'kind': 'production'}]:
            with self.subTest(kwargs=kwargs):
                root = Path(self.tmp.name) / str(len(kwargs))
                conn = self.opened(root)
                conn.execute('DELETE FROM catalogue_identity').close()
                conn.execute('COMMIT').close()
                conn.close()
                path = root / 'machine.sqlite3'
                before = (path.read_bytes(), path.stat().st_mtime_ns,
                          sorted(p.name for p in root.iterdir()))
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    self.opened(root, **kwargs)
                self.assertEqual(caught.exception.cause, 'no-identity')
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns,
                                  sorted(p.name for p in root.iterdir())), before)

    def test_pragmas_precede_first_write(self):
        # The first durable schema/identity write must already have full sync.
        connect = sqlite3.connect
        trace = []

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(trace.append)
            return conn

        with mock.patch.object(sqlite3, 'connect', side_effect=traced):
            self.opened()
        first_create = next(i for i, sql in enumerate(trace) if sql.startswith('CREATE '))
        for pragma in ['PRAGMA journal_mode=WAL', 'PRAGMA synchronous=FULL']:
            self.assertLess(trace.index(pragma), first_create)

    def test_sqlite_prefix_is_literal(self):
        # M14: '_' as a wildcard hides all four added object types.
        for statement in ['CREATE TABLE sqliteXextra (value TEXT)',
                          'CREATE INDEX sqlite1 ON project_routes (created_at)',
                          'CREATE VIEW sqlitev AS SELECT * FROM project_routes',
                          'CREATE TRIGGER sqlitet AFTER INSERT ON project_routes BEGIN SELECT 1; END']:
            with self.subTest(statement=statement):
                root = Path(self.tmp.name) / statement.split()[1]
                conn = self.opened(root)
                conn.execute(statement).close()
                with self.assertRaises(errors.EngineError) as caught:
                    self.opened(root)
                self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')

    def test_ddl_is_pinned(self):
        # Pin the actual stored definitions independently of catalogue._TABLES.
        conn = self.opened()
        definitions = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name").fetchall()
        digest = hashlib.sha256('\n'.join(sql for (sql,) in definitions).encode()).hexdigest()
        self.assertEqual(digest, 'bf9db302287fc2ebc7813ff0a0c56bc11bbdf3210ddd7838aad2c6827f618441')

    def test_constraints_hold(self):
        conn = self.opened()
        statements = [
            ("INSERT INTO project_routes VALUES (?, 'invalid', 'other', 1)", (P,)),
            ("INSERT INTO catalogue_identity VALUES (2, 'other', 'isolated', 1)", ()),
            ("INSERT INTO key_routes VALUES (?, ?, 1)", (Q, P)),
        ]
        for sql, parameters in statements:
            with self.subTest(sql=sql):
                conn.execute('BEGIN IMMEDIATE').close()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql, parameters).close()
                conn.execute('ROLLBACK').close()

    def test_created_root_is_0700(self):
        previous = os.umask(0)
        try:
            self.opened()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

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
