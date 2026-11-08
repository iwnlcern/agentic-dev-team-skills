"""Per-project paths and opening: catches ambient-home, creation and WAL regressions."""
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from relay_engine import errors, schema, store_layout, submit


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
K = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'


class TestStoreLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'

    def opened(self, *, project=P, create=True):
        conn = store_layout.open_ledger(self.root, project, create=create)
        self.addCleanup(conn.close)
        return conn

    def project(self, conn, project=P):
        conn.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, 1)',
                     (project, 'example'))
        conn.execute('COMMIT')

    def test_production_root_uses_password_home(self):
        # Break: trusting the caller's HOME instead of the password database.
        previous = os.environ.get('HOME')
        try:
            os.environ['HOME'] = '/ambient-home-must-not-win'
            with mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir='/password-home')):
                self.assertEqual(store_layout.production_root(), Path('/password-home/.adt'))
        finally:
            if previous is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = previous

    def test_paths_refuse_noncanonical_ids(self):
        # Break: allowing an id to escape its root or alias another spelling.
        invalid = [P.upper(), P[:-1], '..', '../' + P, P + '/child', '', 1]
        for value in invalid:
            with self.subTest(value=value):
                for call in [lambda: store_layout.ledger_path(self.root, value),
                             lambda: store_layout.evidence_dir(self.root, value),
                             lambda: store_layout.key_path(self.root, P, value, K),
                             lambda: store_layout.key_path(self.root, P, Q, value),
                             lambda: store_layout.jev_log_dir(self.root, value)]:
                    with self.assertRaises(ValueError):
                        call()
        self.assertEqual(store_layout.ledger_path(self.root, P),
                         self.root / 'projects' / P / 'ledger.sqlite3')
        self.assertEqual(store_layout.evidence_dir(self.root, P),
                         self.root / 'projects' / P / 'evidence')
        self.assertEqual(store_layout.key_path(self.root, P, Q, K),
                         self.root / 'projects' / P / 'keys' / Q / (K + '.key'))
        self.assertFalse(self.root.exists())

    def test_jev_paths_unchanged(self):
        self.assertEqual(store_layout.jev_key_path(self.root), self.root / 'jev' / 'key')
        self.assertEqual(store_layout.jev_log_dir(self.root, P),
                         self.root / 'projects' / P / 'jev')

    def test_open_missing_ledger_creates_nothing(self):
        # M1: without the mode=rw probe open_store silently initializes a file.
        path = self.root / 'projects' / P / 'ledger.sqlite3'
        path.parent.mkdir(parents=True)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(create=False)
        self.assertEqual(caught.exception.cause, 'missing')
        self.assertFalse(path.exists())
        control = submit.open_store(path)
        control.close()
        self.assertTrue(path.exists())

    def test_open_sets_and_asserts_wal(self):
        # M2: issuing the pragma without checking its answer accepts delete mode.
        conn = self.opened()
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        self.assertEqual(conn.execute('PRAGMA synchronous').fetchone(), (2,))
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone(), (1,))

        class Answer:
            def fetchone(self):
                return ('delete',)

            def close(self):
                pass

        class RefusingWal:
            def execute(self, sql):
                return Answer()

            def close(self):
                pass

        with mock.patch.object(submit, 'open_store', return_value=RefusingWal()):
            with self.assertRaises(store_layout.LayoutFault) as caught:
                self.opened()
        self.assertEqual(caught.exception.cause, 'not-wal')
        control = sqlite3.connect(Path(self.tmp.name) / 'delete.sqlite3')
        self.addCleanup(control.close)
        control.execute('BEGIN IMMEDIATE')
        self.assertEqual(control.execute('PRAGMA journal_mode=WAL').fetchone(), ('delete',))
        control.execute('ROLLBACK')

    def test_open_darwin_full_syncs(self):
        conn = self.opened()
        want = 1 if sys.platform == 'darwin' else 0
        self.assertEqual(conn.execute('PRAGMA fullfsync').fetchone(), (want,))
        self.assertEqual(conn.execute('PRAGMA checkpoint_fullfsync').fetchone(), (want,))

    def test_open_refuses_wrong_project(self):
        conn = self.opened()
        self.project(conn, Q)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(create=False)
        self.assertEqual(caught.exception.cause, 'wrong-project')
        # A second fresh ledger may be provisioned, but may not be served empty.
        self.opened(project=Q)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(project=Q, create=False)
        self.assertEqual(caught.exception.cause, 'no-project-row')

    def test_open_refuses_old_digest(self):
        conn = self.opened()
        self.project(conn)
        conn.execute("UPDATE schema_version SET ddl_sha256 = ?", ('f' * 64,))
        conn.execute('COMMIT')
        with self.assertRaises(errors.EngineError) as caught:
            self.opened(create=False)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')

    def test_created_dirs_are_0700(self):
        old = os.umask(0)
        try:
            self.opened()
        finally:
            os.umask(old)
        for path in [self.root, self.root / 'projects', self.root / 'projects' / P]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def catalogue(self):
        conn = store_layout.open_catalogue(self.root)
        self.addCleanup(conn.close)
        return conn

    def reserve(self, conn, project=P, name='example', active=True):
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.reserve_project(conn, project, name, 1)
        if active:
            store_layout.activate_project(conn, project)
        self.assertTrue(conn.in_transaction)
        conn.execute('COMMIT').close()

    def test_route_by_id_and_name(self):
        conn = self.catalogue()
        self.reserve(conn)
        by_id = store_layout.route(conn, project_id=P)
        self.assertEqual(by_id, store_layout.route(conn, name='example'))
        self.assertEqual((by_id.project_id, by_id.name, by_id.state, by_id.ledger_path),
                         (P, 'example', 'active', self.root.resolve() / 'projects' / P / 'ledger.sqlite3'))
        for selectors in [{}, {'project_id': P, 'name': 'example'}]:
            with self.assertRaises(ValueError):
                store_layout.route(conn, **selectors)
        for selectors in [{'project_id': Q}, {'name': 'missing'}]:
            with self.assertRaises(store_layout.RouteUnavailable) as caught:
                store_layout.route(conn, **selectors)
            self.assertEqual(caught.exception.cause, 'not-found')
        # The authoritative name is the route, even when the ledger copy differs.
        ledger = self.opened()
        self.project(ledger)
        conn.execute("UPDATE project_routes SET name='renamed' WHERE project_id=?", (P,)).close()
        conn.execute('COMMIT').close()
        self.assertEqual(store_layout.route(conn, name='renamed').project_id, P)

    def test_route_creating_is_unavailable(self):
        conn = self.catalogue()
        self.reserve(conn, active=False)
        for selectors in [{'project_id': P}, {'name': 'example'}]:
            with self.assertRaises(store_layout.RouteUnavailable) as caught:
                store_layout.route(conn, **selectors)
            self.assertEqual(caught.exception.cause, 'creating')
        self.assertEqual(conn.execute('SELECT state FROM project_routes').fetchone(), ('creating',))

    def creation_attempt(self, root, crash_at=None):
        """Caller saga fixture: stop on both sides of each single-file commit."""
        class Crash(Exception):
            pass

        def checkpoint(name):
            if crash_at == name:
                raise Crash()

        cat = store_layout.open_catalogue(root)
        ledger = None
        try:
            row = cat.execute('SELECT state FROM project_routes WHERE project_id=?', (P,)).fetchone()
            if row == ('active',):
                return
            if row is None:
                cat.execute('BEGIN IMMEDIATE').close()
                store_layout.reserve_project(cat, P, 'example', 1)
                checkpoint('before-1')
                cat.execute('COMMIT').close()
                checkpoint('after-1')
            ledger = store_layout.open_ledger(root, P, create=True)
            ledger.execute('BEGIN IMMEDIATE').close()
            ledger.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, 1) '
                           'ON CONFLICT(id) DO NOTHING', (P, 'example')).close()
            checkpoint('before-2')
            ledger.execute('COMMIT').close()
            checkpoint('after-2')
            cat.execute('BEGIN IMMEDIATE').close()
            store_layout.activate_project(cat, P)
            checkpoint('before-3')
            cat.execute('COMMIT').close()
            checkpoint('after-3')
        except Crash:
            pass
        finally:
            for conn in [ledger, cat]:
                if conn is not None:
                    if conn.in_transaction:
                        conn.execute('ROLLBACK').close()
                    conn.close()

    def creation_state(self, root):
        cat = store_layout.open_catalogue(root)
        try:
            routes = cat.execute('SELECT project_id,name,state,created_at FROM project_routes').fetchall()
        finally:
            cat.close()
        path = store_layout.ledger_path(root, P)
        projects = []
        if path.exists():
            self.assertTrue(routes, 'ledger without its durable route')
            ledger = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                projects = ledger.execute('SELECT id,name,created_at FROM projects').fetchall()
            finally:
                ledger.close()
        return routes, projects, path.exists()

    def test_creation_crash_points(self):
        expected = {
            'before-1': (None, 0, False), 'after-1': ('creating', 0, False),
            'before-2': ('creating', 0, True), 'after-2': ('creating', 1, True),
            'before-3': ('creating', 1, True), 'after-3': ('active', 1, True),
        }
        for point, want in expected.items():
            with self.subTest(point=point):
                root = Path(self.tmp.name) / point
                self.creation_attempt(root, point)
                routes, projects, exists = self.creation_state(root)
                self.assertEqual((routes[0][2] if routes else None, len(projects), exists), want)
                if routes:
                    conn = store_layout.open_catalogue(root)
                    try:
                        conn.execute('BEGIN IMMEDIATE').close()
                        with self.assertRaises(sqlite3.IntegrityError):
                            store_layout.reserve_project(conn, Q, 'example', 2)
                        conn.execute('ROLLBACK').close()
                    finally:
                        conn.close()
                self.creation_attempt(root)
                final = self.creation_state(root)
                self.assertEqual(final, ([(P, 'example', 'active', 1)], [(P, 'example', 1)], True))
                self.creation_attempt(root)
                self.assertEqual(self.creation_state(root), final)

    def test_reserve_refuses_duplicate_name(self):
        conn = self.catalogue()
        self.reserve(conn)
        before = '\n'.join(conn.iterdump())
        conn.execute('BEGIN IMMEDIATE').close()
        with self.assertRaises(sqlite3.IntegrityError):
            store_layout.reserve_project(conn, Q, 'example', 2)
        conn.execute('ROLLBACK').close()
        self.assertEqual('\n'.join(conn.iterdump()), before)

    def test_key_routes_resolve_and_persist(self):
        conn = self.catalogue()
        self.reserve(conn)
        self.assertIsNone(store_layout.route_key(conn, K))
        for key in [K, Q]:
            conn.execute('BEGIN IMMEDIATE').close()
            store_layout.add_key_route(conn, key, P, 2)
            self.assertTrue(conn.in_transaction)
            conn.execute('COMMIT').close()
        reopened = self.catalogue()
        for key in [K, Q]:
            self.assertEqual(store_layout.route_key(reopened, key), P)
        conn.execute('BEGIN IMMEDIATE').close()
        with self.assertRaises(sqlite3.IntegrityError):
            store_layout.add_key_route(conn, K, P, 3)
        conn.execute('ROLLBACK').close()
        self.assertEqual(conn.execute('SELECT count(*) FROM key_routes').fetchone(), (2,))
        self.assertFalse([name for name, value in vars(store_layout).items()
                          if callable(value) and not name.startswith('_')
                          and ('delete' in name or 'rewrite' in name)])

    def test_reserve_refuses_noncanonical_id(self):
        conn = self.catalogue()
        conn.execute('BEGIN IMMEDIATE').close()
        for value in [P.upper(), '../' + P, '', P[:-1]]:
            with self.assertRaises(ValueError):
                store_layout.reserve_project(conn, value, 'example', 1)
        conn.execute('ROLLBACK').close()
        self.assertEqual(conn.execute('SELECT count(*) FROM project_routes').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
