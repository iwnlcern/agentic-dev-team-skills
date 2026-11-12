"""Per-project paths and opening: catches ambient-home, creation and WAL regressions."""
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
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
        connect = sqlite3.connect
        trace = []

        def defaults_to_off(*args, **kwargs):
            opened = connect(*args, **kwargs)
            opened.execute('PRAGMA synchronous=OFF').close()
            opened.set_trace_callback(trace.append)
            return opened

        with mock.patch.object(sqlite3, 'connect', side_effect=defaults_to_off):
            configured = self.opened()
        self.assertEqual(configured.execute('PRAGMA synchronous').fetchone(), (2,))
        self.assertIn('PRAGMA synchronous=FULL', trace)

        class Answer:
            def fetchone(self):
                return ('delete',)

            def fetchall(self):
                return []

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

    def test_refusal_writes_nothing(self):
        # M16: configuring before validation changes a refused ledger to WAL.
        for fixture in ['empty', 'no-projects-table', 'wrong-project']:
            with self.subTest(fixture=fixture):
                root = Path(self.tmp.name) / fixture
                path = store_layout.ledger_path(root, P)
                path.parent.mkdir(parents=True)
                if fixture == 'wrong-project':
                    conn = submit.open_store(path)
                    self.project(conn, Q)
                    conn.close()
                elif fixture == 'no-projects-table':
                    conn = sqlite3.connect(path)
                    conn.execute('CREATE TABLE unrelated (id TEXT)').close()
                    conn.close()
                else:
                    path.touch()
                before = (hashlib.sha256(path.read_bytes()).hexdigest(),
                          path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir()))
                with self.assertRaises((store_layout.LayoutFault, errors.EngineError)) as caught:
                    store_layout.open_ledger(root, P).close()
                self.assertIsInstance(caught.exception, store_layout.LayoutFault)
                self.assertEqual(caught.exception.cause,
                                 'wrong-project' if fixture == 'wrong-project' else 'no-project-row')
                self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(),
                                  path.stat().st_mtime_ns,
                                  sorted(p.name for p in path.parent.iterdir())), before)
                conn = sqlite3.connect(path)
                try:
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('delete',))
                finally:
                    conn.close()

    def test_missing_after_check_is_not_recreated(self):
        # A removal after exists() must never reach a creating connection.
        conn = self.opened()
        self.project(conn)
        conn.close()
        path = store_layout.ledger_path(self.root, P)
        exists = Path.exists

        def remove_after_check(candidate):
            result = exists(candidate)
            if candidate == path and result:
                candidate.unlink()
            return result

        with mock.patch.object(Path, 'exists', remove_after_check):
            with self.assertRaises(store_layout.LayoutFault) as caught:
                store_layout.open_ledger(self.root, P).close()
        self.assertEqual(caught.exception.cause, 'missing')
        self.assertFalse(path.exists())

    def test_wrong_project_refused_with_create(self):
        conn = self.opened()
        self.project(conn, Q)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(create=True)
        self.assertEqual(caught.exception.cause, 'wrong-project')

    def test_project_check_reads_the_table(self):
        # A stale covering index must not let a different table row through.
        conn = self.opened()
        self.project(conn)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').close()
        page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='projects'").fetchone()[0]
        size = conn.execute('PRAGMA page_size').fetchone()[0]
        conn.close()
        path = store_layout.ledger_path(self.root, P)
        data = path.read_bytes()
        offset = (page - 1) * size
        table = data[offset:offset + size]
        self.assertEqual(table.count(P.encode()), 1)
        path.write_bytes(data[:offset] + table.replace(P.encode(), Q.encode()) + data[offset + size:])
        control = sqlite3.connect(path)
        try:
            self.assertEqual(control.execute('SELECT id FROM projects LIMIT 2').fetchall(), [(P,)])
            self.assertEqual(control.execute('SELECT id FROM projects NOT INDEXED').fetchall(), [(Q,)])
        finally:
            control.close()
        with self.assertRaises(store_layout.LayoutFault) as caught:
            store_layout.open_ledger(self.root, P).close()
        self.assertEqual(caught.exception.cause, 'wrong-project')

    def test_open_ledger_never_reads_catalogue(self):
        # F-4: a corrupt or exclusively locked catalogue cannot block bound work.
        conn = self.opened()
        self.project(conn)
        conn.close()
        catalogue_path = self.root / 'machine.sqlite3'
        catalogue_path.write_bytes(b'not a database')

        def write_bound():
            started = time.monotonic()
            bound = store_layout.open_ledger(self.root, P)
            try:
                bound.execute('UPDATE projects SET created_at=created_at+1').close()
                bound.execute('COMMIT').close()
            finally:
                bound.close()
            self.assertLess(time.monotonic() - started, 1)

        write_bound()
        catalogue_path.unlink()
        catalogue = sqlite3.connect(catalogue_path)
        try:
            catalogue.execute('CREATE TABLE locked (id INTEGER)').close()
            catalogue.execute('BEGIN EXCLUSIVE').close()
            write_bound()
        finally:
            catalogue.close()

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

    def test_openers_create_only_declared_entries(self):
        self.catalogue()
        self.opened()
        actual = {path.relative_to(self.root).as_posix() for path in self.root.rglob('*')}
        ledger = 'projects/' + P + '/ledger.sqlite3'
        required = {'machine.sqlite3', 'projects', 'projects/' + P, ledger}
        sqlite_side_files = {database + suffix for database in ['machine.sqlite3', ledger]
                             for suffix in ['-wal', '-shm', '-journal']}
        self.assertEqual(actual - sqlite_side_files, required)
        self.assertLessEqual(actual, required | sqlite_side_files)

    def catalogue(self):
        conn = store_layout.open_catalogue(self.root)
        self.addCleanup(conn.close)
        return conn

    def reserve(self, conn, project=P, name='example', active=True):
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.reserve_project(conn, project, name, 1)
        if active:
            store_layout.activate_project(conn, project)
        conn.execute('COMMIT').close()

    def test_creation_steps_leave_commit_to_caller(self):
        conn = self.catalogue()
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.reserve_project(conn, P, 'example', 1)
        conn.execute('ROLLBACK').close()
        self.assertEqual(conn.execute('SELECT * FROM project_routes').fetchall(), [])
        self.reserve(conn, active=False)
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.activate_project(conn, P)
        conn.execute('ROLLBACK').close()
        self.assertEqual(conn.execute('SELECT state FROM project_routes').fetchall(), [('creating',)])
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.add_key_route(conn, K, P, 2)
        conn.execute('ROLLBACK').close()
        self.assertEqual(conn.execute('SELECT * FROM key_routes').fetchall(), [])

    def test_activate_touches_only_its_project(self):
        # M11: a missing WHERE would activate Q as well.
        conn = self.catalogue()
        self.reserve(conn, active=False)
        self.reserve(conn, project=Q, name='other', active=False)
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.activate_project(conn, P)
        conn.execute('COMMIT').close()
        self.assertEqual(conn.execute('SELECT project_id,state FROM project_routes ORDER BY project_id').fetchall(),
                         [(P, 'active'), (Q, 'creating')])
        before = conn.total_changes
        store_layout.activate_project(conn, P)
        if conn.in_transaction:
            conn.execute('COMMIT').close()
        self.assertEqual(conn.total_changes, before)

    def test_activate_unknown_refuses(self):
        conn = self.catalogue()
        self.reserve(conn)
        before = '\n'.join(conn.iterdump())
        with self.assertRaises(store_layout.RouteUnavailable) as caught:
            store_layout.activate_project(conn, Q)
        self.assertEqual(caught.exception.cause, 'not-found')
        self.assertEqual('\n'.join(conn.iterdump()), before)

    def test_route_selectors_refuse_noncanonical_ids(self):
        conn = self.catalogue()
        self.reserve(conn)
        for invalid in [P.upper(), P[:-1]]:
            for call in [lambda: store_layout.route(conn, project_id=invalid),
                         lambda: store_layout.route_key(conn, invalid),
                         lambda: store_layout.add_key_route(conn, invalid, P, 1),
                         lambda: store_layout.add_key_route(conn, K, invalid, 1)]:
                with self.assertRaises(ValueError):
                    call()

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

    def test_project_routes_lists_every_route(self):
        # R1 filters creating; R2/R3/R4 use id, NOCASE or insertion order.
        conn = self.catalogue()
        g = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        self.reserve(conn, Q, 'beta')
        self.reserve(conn, g, 'Gamma')
        self.reserve(conn, P, 'alpha', active=False)
        reader = store_layout.open_catalogue_readonly(self.root)
        self.addCleanup(reader.close)
        self.assertEqual(store_layout.project_routes(reader), [
            store_layout.ProjectRoute(g, 'Gamma', 'active',
                                     self.root.resolve() / 'projects' / g / 'ledger.sqlite3'),
            store_layout.ProjectRoute(P, 'alpha', 'creating',
                                     self.root.resolve() / 'projects' / P / 'ledger.sqlite3'),
            store_layout.ProjectRoute(Q, 'beta', 'active',
                                     self.root.resolve() / 'projects' / Q / 'ledger.sqlite3'),
        ])
        with self.assertRaises(store_layout.RouteUnavailable) as caught:
            store_layout.route(reader, name='alpha')
        self.assertEqual(caught.exception.cause, 'creating')
        self.assertFalse(reader.in_transaction)

    def test_project_routes_empty(self):
        # An empty catalogue must not invent a route or require a ledger.
        self.assertEqual(store_layout.project_routes(self.catalogue()), [])

    def test_project_routes_keeps_caller_transaction(self):
        # R5 commits the caller's pending reservation.
        conn = self.catalogue()
        conn.execute('BEGIN IMMEDIATE').close()
        store_layout.reserve_project(conn, P, 'example', 1)
        self.assertEqual(store_layout.project_routes(conn), [
            store_layout.ProjectRoute(P, 'example', 'creating',
                                     self.root.resolve() / 'projects' / P / 'ledger.sqlite3'),
        ])
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(store_layout.project_routes(conn), [])

    def test_project_routes_cleans_up_its_own(self):
        # R7 omits the rollback after a failing owned SELECT.
        conn = self.catalogue()
        self.assertEqual(store_layout.project_routes(conn), [])
        self.assertFalse(conn.in_transaction)
        conn.execute('DROP TABLE project_routes').close()
        conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.project_routes(conn)
        self.assertFalse(conn.in_transaction)

    def test_project_routes_not_file_backed(self):
        # R6 accepts an empty main filename as the current folder.
        from relay_engine import catalogue
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        catalogue.init_catalogue(conn)
        conn.commit()
        store_layout.reserve_project(conn, P, 'example', 1)
        conn.commit()
        self.assertFalse(conn.in_transaction)
        with self.assertRaisesRegex(ValueError, '^route requires a file-backed catalogue$'):
            store_layout.project_routes(conn)
        self.assertFalse(conn.in_transaction)

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


class TestDeclaredLocks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'store'
        cat = store_layout.open_catalogue(self.root)
        try:
            cat.execute('BEGIN IMMEDIATE').close()
            for project in [P, Q]:
                store_layout.reserve_project(cat, project, project, 1)
                store_layout.activate_project(cat, project)
            cat.execute('COMMIT').close()
        finally:
            cat.close()
        for project in [P, Q]:
            ledger = store_layout.open_ledger(self.root, project, create=True)
            try:
                ledger.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, 1)',
                               (project, project)).close()
                if project == P:
                    ledger.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by) "
                                   "VALUES ('team', ?, 'v3-store', 'pair', 1, 'operator')", (P,)).close()
                    for role, key in [('planner', K), ('implementer', Q)]:
                        ledger.execute("INSERT INTO seats VALUES (?, 'team', ?, 'occupied')",
                                       (role, role)).close()
                        ledger.execute("INSERT INTO seat_occupants VALUES (?, ?, ?, 'v', 1, NULL)",
                                       ('occupant-' + role, role, key)).close()
                        ledger.execute("INSERT INTO seat_sessions(id,seat_id,occupant_id,host,host_session_id,"
                                       "generation,signed_in_at,last_seen_at) VALUES (?, ?, ?, 'codex', ?, 1, 1, 1)",
                                       ('session-' + role, role, 'occupant-' + role, 'host-' + role)).close()
                    ledger.execute("INSERT INTO repositories VALUES ('repo-1', ?, 'example')", (P,)).close()
                ledger.execute('COMMIT').close()
            finally:
                ledger.close()

    def connect(self, name, timeout=2):
        path = (self.root / 'machine.sqlite3' if name == 'catalogue'
                else store_layout.ledger_path(self.root, name))
        conn = sqlite3.connect(path, timeout=timeout)
        conn.execute('PRAGMA foreign_keys=ON').close()
        return conn

    def connections(self, names=('catalogue', P), timeout=2):
        conns = {name: self.connect(name, timeout) for name in names}
        for conn in conns.values():
            self.addCleanup(conn.close)
        return conns

    def scalar(self, conn, sql):
        cursor = conn.execute(sql)
        try:
            return cursor.fetchone()[0]
        finally:
            cursor.close()

    def test_begin_declared_refuses_another_file(self):
        # F1-M1: accepting a Q ledger as P would lock and use the wrong file.
        for label, actual in [(P, Q), ('catalogue', P)]:
            conn = self.connect(actual)
            self.addCleanup(conn.close)
            with self.assertRaisesRegex(ValueError, '^declared connection holds another file$'):
                store_layout.begin_declared({label: conn}, root=self.root)
            self.assertFalse(conn.in_transaction)
            for name in {label, actual}:
                other = self.connect(name, timeout=0)
                try:
                    other.execute('BEGIN IMMEDIATE').close()
                    other.execute('ROLLBACK').close()
                finally:
                    other.close()
        memory = sqlite3.connect(':memory:')
        self.addCleanup(memory.close)
        with self.assertRaisesRegex(ValueError, '^declared connection holds another file$'):
            store_layout.begin_declared({P: memory}, root=self.root)
        link = self.root.parent / 'store-link'
        link.symlink_to(self.root, target_is_directory=True)
        conns = {name: sqlite3.connect(path) for name, path in [
            ('catalogue', store_layout.catalogue_path(link)),
            (P, store_layout.ledger_path(link, P)),
        ]}
        for conn in conns.values():
            self.addCleanup(conn.close)
        store_layout.begin_declared(conns, root=link)
        self.assertTrue(all(conn.in_transaction for conn in conns.values()))
        store_layout.rollback_declared(conns)

    def test_begin_declared_refuses_one_file_under_two_labels(self):
        conns = {name: self.connect(name) for name in (P, Q)}
        try:
            store_layout.begin_declared(conns, root=self.root, except_written=Q)
            self.assertTrue(conns[P].in_transaction)
            self.assertFalse(conns[Q].in_transaction)
            store_layout.rollback_declared(conns)
        finally:
            for conn in conns.values():
                conn.close()

        p_path = store_layout.ledger_path(self.root, P)
        q_path = store_layout.ledger_path(self.root, Q)
        for alias in ('hard link', 'symbolic link'):
            with self.subTest(alias=alias):
                # Close all connections before replacing this temporary fixture's path.
                q_path.unlink()
                for suffix in ('-wal', '-shm'):
                    Path(str(q_path) + suffix).unlink(missing_ok=True)
                if alias == 'hard link':
                    os.link(p_path, q_path)
                else:
                    q_path.symlink_to(p_path)
                conns = {name: self.connect(name) for name in (P, Q)}
                try:
                    with self.assertRaisesRegex(ValueError, '^declared connections hold one file$'):
                        store_layout.begin_declared(conns, root=self.root, except_written=Q)
                    self.assertFalse(any(conn.in_transaction for conn in conns.values()))
                    other = self.connect(P, timeout=0)
                    try:
                        other.execute('BEGIN IMMEDIATE').close()
                        other.execute('ROLLBACK').close()
                    finally:
                        other.close()
                finally:
                    for conn in conns.values():
                        conn.close()

    def test_malformed_labels_keep_original_error_and_clean_up(self):
        for mode in ('begin', 'rollback', 'valid labels'):
            with self.subTest(mode=mode):
                raw = {name: self.connect(name) for name in ('catalogue', P, Q)}
                labels = ('catalogue', Q, P) if mode == 'valid labels' else ('catalogue', uuid.UUID(P), None)
                trace = []

                class RecordingRollback:
                    def __init__(self, label, conn):
                        self.label = label
                        self.conn = conn

                    @property
                    def in_transaction(self):
                        return self.conn.in_transaction

                    def execute(self, sql):
                        if sql == 'ROLLBACK':
                            trace.append(self.label)
                        return self.conn.execute(sql)

                conns = {label: RecordingRollback(label, conn)
                         for label, conn in zip(labels, raw.values())}
                try:
                    for conn in raw.values():
                        conn.execute('BEGIN IMMEDIATE').close()
                    caught = None
                    failures = None
                    try:
                        if mode == 'begin':
                            store_layout.begin_declared(conns, root=self.root)
                        else:
                            failures = store_layout.rollback_declared(conns)
                    except BaseException as error:
                        caught = error
                    if mode == 'begin':
                        self.assertIs(type(caught), ValueError)
                        self.assertEqual(str(caught), 'id must be canonical lowercase UUID text')
                    else:
                        self.assertIsNone(caught)
                        self.assertEqual(failures, {})
                    self.assertFalse(any(conn.in_transaction for conn in raw.values()))
                    expected = ['catalogue', P, Q] if mode == 'valid labels' else list(labels)
                    self.assertEqual(trace, expected)
                finally:
                    for conn in raw.values():
                        conn.close()

    def test_rollback_declared_tries_every_connection(self):
        # F1-M3: propagating the first rollback error strands later locks.
        conns = self.connections(('catalogue', P, Q))
        for conn in conns.values():
            conn.execute('BEGIN IMMEDIATE').close()
        rollback_error = sqlite3.OperationalError('rollback failed')
        first = conns['catalogue']

        class FailingRollback:
            @property
            def in_transaction(self):
                return first.in_transaction

            def execute(self, sql):
                if sql == 'ROLLBACK':
                    raise rollback_error
                return first.execute(sql)

        conns['catalogue'] = FailingRollback()
        caught = None
        failures = None
        try:
            failures = store_layout.rollback_declared(conns)
        except BaseException as error:
            caught = error
        self.assertIsNone(caught)
        self.assertEqual(set(failures), {'catalogue'})
        self.assertIs(failures['catalogue'], rollback_error)
        self.assertFalse(conns[P].in_transaction)
        self.assertFalse(conns[Q].in_transaction)

    def test_begin_declared_keeps_original_error(self):
        # F1-M4: a rollback error must not replace the failed BEGIN's cause.
        for begin_failure, rollback_failure in [(P, 'catalogue'), (Q, P)]:
            with self.subTest(begin=begin_failure, rollback=rollback_failure):
                conns = self.connections(('catalogue', P, Q))
                wrapped = conns[rollback_failure]
                failing = conns[begin_failure]

                class FailingRollback:
                    @property
                    def in_transaction(self):
                        return wrapped.in_transaction

                    def execute(self, sql):
                        cursor = wrapped.execute(sql)
                        if sql == 'ROLLBACK':
                            cursor.close()
                            raise sqlite3.OperationalError('rollback failed')
                        return cursor

                class FailingBegin:
                    @property
                    def in_transaction(self):
                        return failing.in_transaction

                    def execute(self, sql):
                        if sql == 'BEGIN IMMEDIATE':
                            raise sqlite3.OperationalError('database is locked')
                        return failing.execute(sql)

                conns[rollback_failure] = FailingRollback()
                conns[begin_failure] = FailingBegin()
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    store_layout.begin_declared(conns, root=self.root)
                self.assertEqual(str(caught.exception), 'database is locked')
                notes = caught.exception.__notes__
                self.assertEqual(len(notes), 1)
                self.assertIn(rollback_failure, notes[0])
                self.assertIn('OperationalError', notes[0])
                self.assertIn('rollback failed', notes[0])
                self.assertFalse(any(conn.in_transaction for conn in conns.values()))

    def test_declared_order_is_canonical(self):
        self.assertEqual(store_layout.declared_order(True, [Q, P]), ('catalogue', P, Q))
        self.assertEqual(store_layout.declared_order(False, [Q, P]), (P, Q))
        self.assertEqual(store_layout.declared_order(True, []), ('catalogue',))
        for ids in [[P, P], [P.upper()], ['../' + P]]:
            with self.assertRaises(ValueError):
                store_layout.declared_order(True, ids)

    def test_concurrent_declared_sets_never_deadlock(self):
        def job(index):
            names = (P, 'catalogue') if index % 2 == 0 else (P,)
            conns = {name: self.connect(name) for name in names}
            trace = []
            for name, conn in conns.items():
                conn.set_trace_callback(lambda sql, name=name: trace.append((name, sql)))
            written = 'catalogue' if index % 2 == 0 else P
            try:
                store_layout.begin_declared(conns, root=self.root)
                if written == 'catalogue':
                    self.scalar(conns[P], 'SELECT ended_at FROM seat_sessions LIMIT 1')
                    conns[written].execute('UPDATE project_routes SET created_at=created_at+1 '
                                           'WHERE project_id=?', (P,)).close()
                else:
                    conns[P].execute("UPDATE seat_sessions SET last_seen_at=last_seen_at+1 "
                                     "WHERE id='session-planner'").close()
                conns[written].execute('COMMIT').close()
                return written, trace
            finally:
                try:
                    store_layout.rollback_declared(conns)
                finally:
                    for conn in conns.values():
                        conn.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(job, index) for index in range(200)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(len(results), 200)
        for written, trace in results:
            self.assertEqual([name for name, sql in trace if sql == 'COMMIT'], [written])
            self.assertEqual([name for name, sql in trace if sql.startswith('UPDATE ')], [written])
        conns = self.connections()
        self.assertEqual(self.scalar(conns['catalogue'],
                         "SELECT created_at FROM project_routes WHERE project_id='" + P + "'"), 101)
        self.assertEqual(self.scalar(conns[P],
                         "SELECT last_seen_at FROM seat_sessions WHERE id='session-planner'"), 101)

    def test_read_lock_holds_binding_stable(self):
        conns = self.connections()

        def end(started, finished):
            conn = self.connect(P)
            try:
                started.set()
                conn.execute('BEGIN IMMEDIATE').close()
                conn.execute("UPDATE seat_sessions SET ended_at=2 WHERE id='session-planner'").close()
                conn.execute('COMMIT').close()
                finished.set()
            finally:
                conn.close()

        store_layout.begin_declared(conns, root=self.root)
        self.assertIsNone(self.scalar(conns[P],
                          "SELECT ended_at FROM seat_sessions WHERE id='session-planner'"))
        started, finished = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(end, started, finished)
            self.assertTrue(started.wait(1))
            try:
                self.assertFalse(finished.wait(0.1), 'end escaped the held ledger lock')
                conns['catalogue'].execute('UPDATE project_routes SET created_at=2 WHERE project_id=?', (P,)).close()
                conns['catalogue'].execute('COMMIT').close()
            finally:
                store_layout.rollback_declared(conns)
            future.result(timeout=3)
        self.assertTrue(finished.is_set())

        # Control: catalogue-only exclusion permits an end before its commit.
        conns[P].execute("UPDATE seat_sessions SET ended_at=NULL WHERE id='session-planner'").close()
        conns[P].execute('COMMIT').close()
        store_layout.begin_declared({'catalogue': conns['catalogue']}, root=self.root)
        self.assertIsNone(self.scalar(conns[P],
                          "SELECT ended_at FROM seat_sessions WHERE id='session-planner'"))
        started, finished = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(end, started, finished)
            future.result(timeout=3)
            self.assertTrue(finished.is_set())
            self.assertEqual(self.scalar(conns[P],
                             "SELECT ended_at FROM seat_sessions WHERE id='session-planner'"), 2)
            conns['catalogue'].execute('UPDATE project_routes SET created_at=3 WHERE project_id=?', (P,)).close()
            conns['catalogue'].execute('COMMIT').close()

    def test_submit_after_read_locks(self):
        conns = self.connections()
        seats = {role: submit.SeatRef(role, 'v3-store.' + role, role, 'team', P)
                 for role in ['planner', 'implementer']}

        class Directory:
            def resolve(self, address, sender):
                return seats[address.partition('.')[2]]

        ctx = submit.SubmitContext(seats['planner'],
                                   submit.GitState('repo-1', 'a' * 40, 'main', 0, '0' * 64),
                                   lambda: 1_700_000_000_000, Directory(), submission_key='lock-1')
        data = (b'KIND: SITREP\nTO: v3-store.implementer\nBUNDLE: b1\n'
                b'NEEDS: none\nSUBJECT: locked\n\nbody\n')
        store_layout.begin_declared(conns, except_written=P, root=self.root)
        try:
            admitted = submit.submit(conns[P], ctx, data)
            self.assertEqual(admitted.handle, 1)
            self.assertFalse(conns[P].in_transaction)
            self.assertTrue(conns['catalogue'].in_transaction)
        finally:
            store_layout.rollback_declared(conns)
        conns[P].execute('BEGIN IMMEDIATE').close()
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'within a transaction'):
                submit.submit(conns[P], replace(ctx, submission_key='lock-2'), data)
        finally:
            store_layout.rollback_declared(conns)

    def test_begin_declared_acquires_in_canonical_order(self):
        conns = self.connections((Q, P, 'catalogue'))
        trace = []
        for name, conn in conns.items():
            conn.set_trace_callback(lambda sql, name=name:
                                    trace.append(name) if sql == 'BEGIN IMMEDIATE' else None)
        store_layout.begin_declared(conns, root=self.root)
        self.assertEqual(trace, ['catalogue', P, Q])
        store_layout.rollback_declared(conns)
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        trace.clear()
        store_layout.begin_declared(conns, except_written=Q, root=self.root)
        self.assertEqual(trace, ['catalogue', P])
        self.assertFalse(conns[Q].in_transaction)
        store_layout.rollback_declared(conns)
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))

    def test_except_written_must_be_last(self):
        # M15: deferring P's lock until after Q breaks the global order.
        conns = self.connections((Q, P, 'catalogue'))
        with self.assertRaises(ValueError):
            store_layout.begin_declared(conns, except_written=P, root=self.root)
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        store_layout.begin_declared(conns, except_written=Q, root=self.root)
        self.assertTrue(conns['catalogue'].in_transaction)
        self.assertTrue(conns[P].in_transaction)
        self.assertFalse(conns[Q].in_transaction)
        store_layout.rollback_declared(conns)

    def test_except_written_outside_set_refuses(self):
        conns = self.connections()
        with self.assertRaises(ValueError):
            store_layout.begin_declared(conns, except_written=Q, root=self.root)
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))

    def test_mixed_order_control_deadlocks_only_when_mixed(self):
        # This handshake belongs only to the mixed-order negative case.
        first_locks = threading.Barrier(2, timeout=2)

        class FirstLock:
            def __init__(self, conn):
                self.conn = conn

            @property
            def in_transaction(self):
                return self.conn.in_transaction

            def execute(self, sql):
                cursor = self.conn.execute(sql)
                if sql == 'BEGIN IMMEDIATE':
                    first_locks.wait()
                return cursor

        def mixed(canonical):
            conns = {name: self.connect(name, timeout=0.2) for name in ['catalogue', P]}
            try:
                if canonical:
                    store_layout.begin_declared({'catalogue': FirstLock(conns['catalogue']), P: conns[P]}, root=self.root)
                else:
                    conns[P].execute('BEGIN IMMEDIATE').close()
                    first_locks.wait()
                    conns['catalogue'].execute('BEGIN IMMEDIATE').close()
                conns['catalogue'].execute('COMMIT').close()
                return 'committed'
            except sqlite3.OperationalError as error:
                self.assertIn('locked', str(error))
                return 'locked'
            finally:
                try:
                    store_layout.rollback_declared(conns)
                finally:
                    for conn in conns.values():
                        conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mixed, canonical) for canonical in [True, False]]
            answers = [future.result(timeout=4) for future in futures]
        self.assertIn('locked', answers)
        start = threading.Barrier(2, timeout=2)

        def canonical_job():
            conns = {name: self.connect(name) for name in ['catalogue', P]}
            try:
                start.wait()  # Before all acquisitions; no post-lock handshake.
                store_layout.begin_declared(conns, root=self.root)
                conns['catalogue'].execute('UPDATE project_routes SET created_at=created_at+1').close()
                conns['catalogue'].execute('COMMIT').close()
                return 'committed'
            finally:
                try:
                    store_layout.rollback_declared(conns)
                finally:
                    for conn in conns.values():
                        conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(canonical_job) for _ in range(2)]
            self.assertEqual([f.result(timeout=4) for f in futures], ['committed', 'committed'])

    def test_failed_begin_rolls_back_all(self):
        conns = self.connections()
        blocker = self.connect(P)
        self.addCleanup(blocker.close)
        blocker.execute('BEGIN IMMEDIATE').close()
        conns[P].execute('PRAGMA busy_timeout=50').close()
        with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
            store_layout.begin_declared(conns, root=self.root)
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        blocker.execute('ROLLBACK').close()
        store_layout.begin_declared(conns, root=self.root)
        store_layout.rollback_declared(conns)

    def test_open_transaction_is_refused(self):
        conns = self.connections()
        trace = []
        conns[P].execute('BEGIN').close()
        for name, conn in conns.items():
            conn.set_trace_callback(lambda sql, name=name: trace.append((name, sql)))
        with self.assertRaises(ValueError):
            store_layout.begin_declared(conns, root=self.root)
        self.assertFalse(any(sql.startswith('BEGIN') for _, sql in trace))
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        trace.clear()
        store_layout.begin_declared(conns, root=self.root)
        self.assertEqual([name for name, sql in trace if sql == 'BEGIN IMMEDIATE'], ['catalogue', P])
        store_layout.rollback_declared(conns)

    def test_unfinished_cursor_snapshot_failure_rolls_back_set(self):
        conns = self.connections()
        cursor = conns[P].execute('SELECT id FROM seats UNION ALL SELECT id FROM seats')
        self.addCleanup(cursor.close)
        cursor.fetchone()
        other = self.connect(P)
        self.addCleanup(other.close)
        other.execute("UPDATE seat_sessions SET last_seen_at=2 WHERE id='session-planner'").close()
        other.execute('COMMIT').close()
        self.assertFalse(conns[P].in_transaction)
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError) as caught:
            store_layout.begin_declared(conns, root=self.root)
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY_SNAPSHOT)
        self.assertLess(time.monotonic() - started, 1)  # Busy timeout is 2 seconds.
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        cursor.close()
        store_layout.begin_declared(conns, root=self.root)
        store_layout.rollback_declared(conns)


R = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


class ReadonlyDirectory(sqlite3.OperationalError):
    """A first-statement failure carrying SQLite's read-only-directory name."""

    sqlite_errorname = 'SQLITE_READONLY_DIRECTORY'
    sqlite_errorcode = 1544


class StandInConnection:
    """A connection whose first statement fails as a read-only directory does."""

    def __init__(self):
        self.closed = False

    def execute(self, *args, **kwargs):
        raise ReadonlyDirectory('attempt to write a readonly database')

    def close(self):
        self.closed = True


def route_state_calls(source):
    """Calls of store_layout.route_state in one module's source, by any alias."""
    import ast

    tree = ast.parse(source)
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith('store_layout') and alias.asname:
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == 'store_layout':
                    modules.add(alias.asname or alias.name)
                elif alias.name == 'route_state' and (node.module or '').endswith('store_layout'):
                    functions.add(alias.asname or alias.name)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in functions:
            calls.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == 'route_state':
            value = func.value
            if isinstance(value, ast.Name) and value.id in modules:
                calls.append(node.lineno)
            elif isinstance(value, ast.Attribute) and value.attr == 'store_layout':
                calls.append(node.lineno)
    return calls


class TestReadonlyOpeners(unittest.TestCase):
    """Delta 1 r3 D1, D2 and D4: readers that write no page, and one served path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'

    def keep(self, conn):
        self.addCleanup(conn.close)
        return conn

    def ledger_writer(self, project=P, row=P):
        conn = self.keep(store_layout.open_ledger(self.root, project, create=True))
        if row is not None:
            conn.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, 1)',
                         (row, 'example'))
            conn.execute('COMMIT')
        else:
            # A connection that has run nothing since the switch to WAL holds no side files yet;
            # one read opens them, so the file is held as D5's cases require.
            conn.execute('SELECT count(*) FROM projects').fetchall()
        return conn

    def catalogue_writer(self):
        return self.keep(store_layout.open_catalogue(self.root))

    def names(self, directory):
        return sorted(os.listdir(directory))

    def main_name(self, conn):
        return next(path for _, database, path in conn.execute('PRAGMA database_list')
                    if database == 'main')

    def test_readonly_openers_write_nothing(self):
        ledger = store_layout.ledger_path(self.root, P)
        catalogue = self.root / 'machine.sqlite3'
        self.catalogue_writer()  # before any ledger: open_catalogue refuses a missing catalogue beside projects
        self.ledger_writer()
        openers = [(ledger, lambda: store_layout.open_ledger_readonly(self.root, P), 'projects'),
                   (catalogue, lambda: store_layout.open_catalogue_readonly(self.root),
                    'catalogue_identity')]
        for path, opener, table in openers:
            with self.subTest(path=path.name):
                before = (path.read_bytes(), Path(str(path) + '-wal').read_bytes(),
                          self.names(path.parent))
                reader = opener()
                try:
                    self.assertIsNone(reader.isolation_level)
                    self.assertEqual(reader.execute('PRAGMA query_only').fetchone(), (1,))
                    self.assertTrue(reader.execute('SELECT * FROM ' + table).fetchall())
                    self.assertFalse(reader.in_transaction)
                finally:
                    reader.close()
                after = (path.read_bytes(), Path(str(path) + '-wal').read_bytes(),
                         self.names(path.parent))
                self.assertEqual(before, after)
                # With query_only lifted, the mode=ro connection itself refuses the write.
                reader = opener()
                try:
                    reader.execute('PRAGMA query_only=OFF')
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        reader.execute('CREATE TABLE ro_probe(a)')
                    self.assertIn('attempt to write a readonly database', str(caught.exception))
                finally:
                    reader.close()
        self.writer_waits_reader_returns(ledger, catalogue)

    def writer_waits_reader_returns(self, ledger, catalogue):
        # Control: the writer openers wait the busy timeout behind a held BEGIN IMMEDIATE,
        # while the read-only openers return at once.
        connect = sqlite3.connect

        def short_timeout(*args, **kwargs):
            kwargs['timeout'] = 0.5
            return connect(*args, **kwargs)

        cases = [(ledger, lambda: store_layout.open_ledger(self.root, P),
                  lambda: store_layout.open_ledger_readonly(self.root, P)),
                 (catalogue, lambda: store_layout.open_catalogue(self.root),
                  lambda: store_layout.open_catalogue_readonly(self.root))]
        for path, writer_opener, reader_opener in cases:
            with self.subTest(path=path.name):
                holder = self.keep(connect(path, timeout=0))
                holder.execute('BEGIN IMMEDIATE')
                try:
                    with mock.patch.object(sqlite3, 'connect', side_effect=short_timeout):
                        started = time.monotonic()
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            writer_opener().close()
                        waited = time.monotonic() - started
                        self.assertIn('locked', str(caught.exception))
                        self.assertGreaterEqual(waited, 0.4)
                        started = time.monotonic()
                        reader_opener().close()
                        self.assertLess(time.monotonic() - started, 0.4)
                finally:
                    holder.execute('ROLLBACK')

    def refusal(self, opener, cause, directory):
        before = self.names(directory)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            opener()
        self.assertEqual(caught.exception.cause, cause)
        self.assertEqual(self.names(directory), before)

    def test_readonly_openers_refuse(self):
        ledger = store_layout.ledger_path(self.root, P)
        ledger_open = lambda: store_layout.open_ledger_readonly(self.root, P)
        catalogue_open = lambda: store_layout.open_catalogue_readonly(self.root)
        # Missing: nothing is created.
        self.root.mkdir()
        self.refusal(ledger_open, 'missing', self.root)
        self.refusal(catalogue_open, 'missing', self.root)
        # No writer: both files exist with their side files gone after a clean close.
        store_layout.open_catalogue(self.root).close()
        store_layout.open_ledger(self.root, P, create=True).close()
        self.assertEqual(self.names(ledger.parent), ['ledger.sqlite3'])
        self.refusal(ledger_open, 'no-writer', ledger.parent)
        self.refusal(catalogue_open, 'no-writer', self.root)
        # Control: a plain mode=ro connection opens it and leaves both side files behind.
        plain = sqlite3.connect(ledger.absolute().as_uri() + '?mode=ro', uri=True)
        plain.execute('PRAGMA journal_mode').fetchone()
        plain.close()
        self.assertEqual(self.names(ledger.parent),
                         ['ledger.sqlite3', 'ledger.sqlite3-shm', 'ledger.sqlite3-wal'])
        for suffix in ['-wal', '-shm']:
            Path(str(ledger) + suffix).unlink()
        ledger.unlink()
        self.refuse_held_files(ledger, ledger_open, catalogue_open)
        self.refuse_changed_ddl()
        self.refuse_delete_mode()

    def refuse_held_files(self, ledger, ledger_open, catalogue_open):
        # Every case after missing and no-writer runs with a connection holding the file.
        writer = self.ledger_writer(row=None)
        self.refusal(ledger_open, 'no-project-row', ledger.parent)
        writer.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, 1)', (Q, 'other'))
        writer.execute('COMMIT')
        self.refusal(ledger_open, 'wrong-project', ledger.parent)
        catalogue = self.catalogue_writer()
        catalogue.execute('DELETE FROM catalogue_identity')
        catalogue.commit()
        self.refusal(catalogue_open, 'no-identity', self.root)
        # An injected read-only-directory failure at the first statement is no writer.
        stand_in = StandInConnection()
        with mock.patch.object(sqlite3, 'connect', return_value=stand_in):
            self.refusal(ledger_open, 'no-writer', ledger.parent)
        self.assertTrue(stand_in.closed)

    def refuse_changed_ddl(self):
        self.root = Path(self.tmp.name) / 'ddl-store'
        catalogue = self.catalogue_writer()
        writer = self.ledger_writer()
        writer.execute('CREATE TABLE extra_ledger_table(a)')
        writer.commit()
        ledger = store_layout.ledger_path(self.root, P)
        before = self.names(ledger.parent)
        with self.assertRaises(errors.EngineError) as caught:
            store_layout.open_ledger_readonly(self.root, P)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')
        self.assertEqual(self.names(ledger.parent), before)
        catalogue.execute('CREATE TABLE extra_catalogue_table(a)')
        catalogue.commit()
        before = self.names(self.root)
        with self.assertRaises(errors.EngineError) as caught:
            store_layout.open_catalogue_readonly(self.root)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')
        self.assertEqual(self.names(self.root), before)

    def refuse_delete_mode(self):
        # A delete-mode file has no side files while held, so empty ones are planted.
        self.root = Path(self.tmp.name) / 'delete-store'
        ledger = store_layout.ledger_path(self.root, P)
        ledger.parent.mkdir(parents=True)
        holder = self.keep(sqlite3.connect(ledger))
        holder.execute('CREATE TABLE t(a)')
        holder.commit()
        self.assertEqual(holder.execute('PRAGMA journal_mode').fetchone(), ('delete',))
        for suffix in ['-wal', '-shm']:
            Path(str(ledger) + suffix).write_bytes(b'')
        self.refusal(lambda: store_layout.open_ledger_readonly(self.root, P), 'not-wal',
                     ledger.parent)

    def test_route_state_reports_every_state(self):
        catalogue = self.catalogue_writer()
        catalogue.execute('BEGIN IMMEDIATE').close()
        store_layout.reserve_project(catalogue, P, 'active-one', 1)
        store_layout.activate_project(catalogue, P)
        store_layout.reserve_project(catalogue, Q, 'creating-one', 1)
        catalogue.execute('COMMIT').close()
        reader = self.keep(store_layout.open_catalogue_readonly(self.root))
        for conn in [catalogue, reader]:
            with self.subTest(conn='writer' if conn is catalogue else 'reader'):
                self.assertEqual(store_layout.route_state(conn, P), 'active')
                self.assertEqual(store_layout.route_state(conn, Q), 'creating')
                self.assertEqual(store_layout.route_state(conn, R), 'absent')
                self.assertFalse(conn.in_transaction)
                statements = []
                conn.set_trace_callback(statements.append)
                try:
                    for bad in [P.upper(), 'not-a-uuid', None]:
                        with self.assertRaises(ValueError):
                            store_layout.route_state(conn, bad)
                finally:
                    conn.set_trace_callback(None)
                self.assertEqual(statements, [])
        with self.assertRaises(store_layout.RouteUnavailable) as caught:
            store_layout.route(catalogue, project_id=Q)
        self.assertEqual(caught.exception.cause, 'creating')

    def test_route_state_one_caller(self):
        package = Path(store_layout.__file__).resolve().parent
        callers = {}
        for path in sorted(package.rglob('*.py')):
            calls = route_state_calls(path.read_text(encoding='utf-8'))
            if calls:
                callers[path.relative_to(package).as_posix()] = calls
        self.assertTrue(callers, 'the scan found no call at all, not even this module')
        for relative in callers:
            self.assertTrue(relative.startswith('tests/'), relative)
        # Control: the scanner sees aliases and ignores a field of the same name.
        samples = {
            'from relay_engine import store_layout\nstore_layout.route_state(c, p)\n': 1,
            'from relay_engine import store_layout as sl\nsl.route_state(c, p)\n': 1,
            'import relay_engine.store_layout as sl\nsl.route_state(c, p)\n': 1,
            'from relay_engine.store_layout import route_state as rs\nrs(c, p)\n': 1,
            'import relay_engine\nrelay_engine.store_layout.route_state(c, p)\n': 1,
            'confirmation.route_state\nrecord(route_state=1)\n': 0,
            'from relay_engine import store_layout\nconfirmation.route_state(c)\n': 0,
        }
        for source, count in samples.items():
            with self.subTest(source=source):
                self.assertEqual(len(route_state_calls(source)), count)

    def test_served_path_one_spelling(self):
        real = Path(self.tmp.name) / 'real'
        real.mkdir()
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(real, target_is_directory=True)
        resolved = store_layout.resolve_root(link)
        self.assertEqual(resolved, Path(os.path.realpath(link)))
        self.assertEqual(store_layout.catalogue_path(resolved), resolved / 'machine.sqlite3')
        catalogue = self.keep(store_layout.open_catalogue(resolved))
        catalogue.execute('BEGIN IMMEDIATE').close()
        store_layout.reserve_project(catalogue, P, 'example', 1)
        store_layout.activate_project(catalogue, P)
        catalogue.execute('COMMIT').close()
        writer = self.keep(store_layout.open_ledger(resolved, P, create=True))
        writer.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, 1)', (P, 'example'))
        writer.execute('COMMIT')
        ledger_reader = self.keep(store_layout.open_ledger_readonly(resolved, P))
        catalogue_reader = self.keep(store_layout.open_catalogue_readonly(resolved))
        pairs = [
            (str(store_layout.route(catalogue, project_id=P).ledger_path),
             str(store_layout.ledger_path(resolved, P))),
            (self.main_name(ledger_reader), str(store_layout.ledger_path(resolved, P))),
            (self.main_name(catalogue_reader), str(store_layout.catalogue_path(resolved))),
        ]
        for first, second in pairs:
            with self.subTest(pair=first):
                self.assertEqual(first, second)
                self.assertEqual(os.stat(first).st_ino, os.stat(second).st_ino)
        # Control: built from the unresolved link, at least one pair differs.
        unresolved = [
            (str(store_layout.route(catalogue, project_id=P).ledger_path),
             str(store_layout.ledger_path(link, P))),
            (self.main_name(ledger_reader), str(store_layout.ledger_path(link, P))),
            (self.main_name(catalogue_reader), str(store_layout.catalogue_path(link))),
        ]
        self.assertTrue(any(first != second for first, second in unresolved))

    def sized(self, directory) -> list[tuple[str, int]]:
        return sorted((entry.name, entry.stat().st_size) for entry in directory.iterdir())

    def traced_read(self) -> tuple[object, list[tuple[str, sqlite3.Connection]]]:
        connect = sqlite3.connect
        conns = []

        def traced_connect(database, *args, **kwargs):
            conn = connect(database, *args, **kwargs)
            conns.append((database, conn))
            self.addCleanup(conn.close)
            return conn

        try:
            with mock.patch.object(sqlite3, 'connect', side_effect=traced_connect):
                value = store_layout.read_store_id(self.root)
        except Exception as exc:
            value = exc
        return value, conns

    def test_read_store_id_writer_open(self):
        # N1 ignores uncheckpointed creation in WAL; N5 leaves the reader open.
        writer = self.catalogue_writer()
        expected = writer.execute('SELECT store_id FROM catalogue_identity WHERE id=1').fetchone()[0]
        path = store_layout.catalogue_path(self.root)
        self.assertTrue(Path(str(path) + '-wal').exists())
        control = self.keep(sqlite3.connect(
            path.absolute().as_uri() + '?mode=ro&immutable=1', uri=True))
        self.assertEqual(control.execute('SELECT name FROM sqlite_master').fetchall(), [])
        value, conns = self.traced_read()
        self.assertEqual(value, expected)
        self.assertEqual(sum(database != ':memory:' for database, _ in conns), 1)
        for _, conn in conns:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_read_store_id_writer_closed_writes_nothing(self):
        # N2 demands a writer, N3 makes side files, and N4 leaves the reader open.
        writer = store_layout.open_catalogue(self.root)
        expected = writer.execute('SELECT store_id FROM catalogue_identity WHERE id=1').fetchone()[0]
        writer.close()
        self.assertEqual(self.names(self.root), ['machine.sqlite3'])
        before = self.sized(self.root)
        value, conns = self.traced_read()
        self.assertEqual(value, expected)
        self.assertEqual(self.sized(self.root), before)
        self.assertEqual(sum(database != ':memory:' for database, _ in conns), 1)
        for _, conn in conns:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
        self.refusal(lambda: store_layout.open_catalogue_readonly(self.root),
                     'no-writer', self.root)
        path = store_layout.catalogue_path(self.root)
        plain = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)
        try:
            plain.execute('PRAGMA journal_mode').fetchone()
        finally:
            plain.close()
        self.assertEqual(self.names(self.root),
                         ['machine.sqlite3', 'machine.sqlite3-shm', 'machine.sqlite3-wal'])

    def test_read_store_id_faults(self):
        # N6 loses missing, N7 loses no-identity, N8 accepts changed DDL,
        # and N9 leaves the catalogue connection open when a check raises.
        self.root.mkdir()
        before = self.sized(self.root)
        value, conns = self.traced_read()
        self.assertIsInstance(value, store_layout.LayoutFault)
        self.assertEqual(value.cause, 'missing')
        self.assertEqual(conns, [])
        self.assertEqual(self.sized(self.root), before)

        store_layout.open_catalogue(self.root).close()
        direct = sqlite3.connect(store_layout.catalogue_path(self.root))
        try:
            direct.execute('DELETE FROM catalogue_identity')
            direct.commit()
        finally:
            direct.close()
        self.assertEqual(self.names(self.root), ['machine.sqlite3'])
        before = self.sized(self.root)
        value, conns = self.traced_read()
        self.assertIsInstance(value, store_layout.LayoutFault)
        self.assertEqual(value.cause, 'no-identity')
        self.assertEqual(self.sized(self.root), before)
        self.assertEqual(sum(database != ':memory:' for database, _ in conns), 1)
        for _, conn in conns:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

        self.root = Path(self.tmp.name) / 'ddl-store'
        store_layout.open_catalogue(self.root).close()
        direct = sqlite3.connect(store_layout.catalogue_path(self.root))
        try:
            direct.execute('CREATE TABLE extra_catalogue_table(a)')
            direct.commit()
        finally:
            direct.close()
        self.assertEqual(self.names(self.root), ['machine.sqlite3'])
        before = self.sized(self.root)
        value, conns = self.traced_read()
        self.assertIsInstance(value, errors.EngineError)
        self.assertEqual(value.code, 'E-SCHEMA-MISMATCH')
        self.assertEqual(self.sized(self.root), before)
        self.assertEqual(sum(database != ':memory:' for database, _ in conns), 1)
        for _, conn in conns:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
