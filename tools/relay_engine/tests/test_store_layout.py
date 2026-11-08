"""Per-project paths and opening: catches ambient-home, creation and WAL regressions."""
import os
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
        path = (self.root / 'catalogue.sqlite3' if name == 'catalogue'
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
                store_layout.begin_declared(conns)
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

        store_layout.begin_declared(conns)
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
        store_layout.begin_declared({'catalogue': conns['catalogue']})
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
        store_layout.begin_declared(conns, except_written=P)
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
        store_layout.begin_declared(conns)
        self.assertEqual(trace, ['catalogue', P, Q])
        store_layout.rollback_declared(conns)
        trace.clear()
        store_layout.begin_declared(conns, except_written=P)
        self.assertEqual(trace, ['catalogue', Q])
        self.assertFalse(conns[P].in_transaction)
        store_layout.rollback_declared(conns)

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
                    store_layout.begin_declared({'catalogue': FirstLock(conns['catalogue']), P: conns[P]})
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
                store_layout.begin_declared(conns)
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
            store_layout.begin_declared(conns)
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        blocker.execute('ROLLBACK').close()
        store_layout.begin_declared(conns)
        store_layout.rollback_declared(conns)

    def test_open_transaction_is_refused(self):
        conns = self.connections()
        trace = []
        conns[P].execute('BEGIN').close()
        for name, conn in conns.items():
            conn.set_trace_callback(lambda sql, name=name: trace.append((name, sql)))
        with self.assertRaises(ValueError):
            store_layout.begin_declared(conns)
        self.assertFalse(any(sql.startswith('BEGIN') for _, sql in trace))
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        trace.clear()
        store_layout.begin_declared(conns)
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
            store_layout.begin_declared(conns)
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY_SNAPSHOT)
        self.assertLess(time.monotonic() - started, 1)  # Busy timeout is 2 seconds.
        self.assertFalse(any(conn.in_transaction for conn in conns.values()))
        cursor.close()
        store_layout.begin_declared(conns)
        store_layout.rollback_declared(conns)


if __name__ == '__main__':
    unittest.main()
