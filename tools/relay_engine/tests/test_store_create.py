"""Delta 8 creation: real turns, crash boundaries and admission side effects."""
from contextlib import closing, contextmanager
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from relay_engine import store_layout, store_steps
from relay_engine.store_create import Creation, CreationHeld, create_project, finish_creation


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
J = '00000000-0001-7000-8000-000000000001'
Declared = store_steps.Declared


def outcome(fn):
    try:
        return fn()
    except Exception as error:
        return error


def snapshot(root):
    return [(str(p.relative_to(root)),
             hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in sorted(Path(root).rglob('*'))
            if not p.name.endswith(('-wal', '-shm'))]


class RecordingTurns:
    def __init__(self, inner):
        self.inner = inner
        self.turns = []

    def run(self, declared, body, *, fence_project=None):
        self.turns.append(declared)
        return self.inner.run(declared, body, fence_project=fence_project)


class Crash(Exception):
    pass


class CrashingTurns:
    def __init__(self, inner, crash_at):
        self.inner, self.crash_at = inner, crash_at
        self.writes = 0

    def run(self, declared, body, *, fence_project=None):
        written = (declared.catalogue == 'written'
                   or any(v in ('written', 'created') for v in declared.ledgers.values()))
        if written:
            self.writes += 1
        point = self.writes

        def wrapped(conns):
            value = body(conns)
            if written and self.crash_at == f'before-{point}':
                raise Crash()
            return value

        value = self.inner.run(declared, wrapped, fence_project=fence_project)
        if written and self.crash_at == f'after-{point}':
            raise Crash()
        return value


@contextmanager
def wal_write_holder(path, release):
    ready = threading.Event()
    errors = []

    def hold():
        try:
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('BEGIN IMMEDIATE').close()
                ready.set()
                if not release.wait(timeout=12):
                    raise AssertionError('WAL test holder was not released')
                conn.commit()
        except Exception as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        if not ready.wait(timeout=8):
            raise AssertionError('WAL test holder did not acquire its lock')
        if errors:
            raise errors[0]
        yield
    finally:
        release.set()
        worker.join(timeout=13)
        if worker.is_alive():
            raise AssertionError('WAL test holder did not finish')
        if errors:
            raise errors[0]


def database_value(conn, sql):
    with closing(conn.execute(sql)) as cursor:
        return cursor.fetchone()[0]


class TestCreateProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        store_layout.open_catalogue(self.root).close()

    def fresh(self, label):
        root = Path(self.tmp.name) / label
        store_layout.open_catalogue(root).close()
        return root

    def rows(self, root, *, ledger=False):
        path = store_layout.ledger_path(root, P) if ledger else store_layout.catalogue_path(root)
        with closing(sqlite3.connect(path)) as conn:
            if ledger:
                return conn.execute('SELECT id, name, created_at FROM projects').fetchall()
            return conn.execute(
                'SELECT project_id, name, state, created_at, held_by_job FROM project_routes').fetchall()

    def ledger_rows(self, root, project):
        with closing(sqlite3.connect(store_layout.ledger_path(root, project))) as conn:
            return conn.execute('SELECT id, name, created_at FROM projects').fetchall()

    def fixture(self, root, *, holder=None, active=False, ledger=False, row=False):
        def reserve(conns):
            store_layout.reserve_project(conns['catalogue'], P, 'example', 1000,
                                         held_by_job=holder)
            if active:
                store_layout.activate_project(conns['catalogue'], P)
        store_steps.DirectTurns(root).run(Declared('written', {}), reserve)
        if ledger:
            conn = store_layout.open_ledger(root, P, create=True)
            try:
                if row:
                    conn.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)',
                                 (P, 'example', 1000)).close()
                    conn.commit()
            finally:
                conn.close()

    def test_create_from_nothing(self):
        # Break: omitting reservation, project row or activation.
        value = create_project(store_steps.DirectTurns(self.root), 'example', now_ms=1000)
        self.assertIsInstance(value, Creation)
        self.assertEqual(value.outcome, 'created')
        self.assertEqual(self.rows(self.root), [(value.project_id, 'example', 'active', 1000, None)])
        self.assertEqual(self.ledger_rows(self.root, value.project_id),
                         [(value.project_id, 'example', 1000)])

    def test_create_crash_points_resume(self):
        # C1: a duplicate insert prevents resumption at after-2 and before-3.
        cases = [('before-1', 'created'), ('after-1', 'resumed'),
                 ('before-2', 'resumed'), ('after-2', 'resumed'),
                 ('before-3', 'resumed'), ('after-3', 'active')]
        for point, expected in cases:
            with self.subTest(point=point):
                root = self.fresh(point)
                first = outcome(lambda: create_project(
                    CrashingTurns(store_steps.DirectTurns(root), point), 'example', now_ms=1000))
                self.assertIsInstance(first, Crash)
                second = outcome(lambda: create_project(
                    store_steps.DirectTurns(root), 'example', now_ms=2000))
                routes = self.rows(root)
                self.assertEqual(len(routes), 1)
                self.assertEqual(routes[0][2], 'active')
                project = routes[0][0]
                projects = self.ledger_rows(root, project)
                self.assertEqual(len(projects), 1)
                self.assertEqual(projects[0][0], project)
                self.assertIsInstance(second, Creation)
                self.assertEqual(second.outcome, expected)
                self.assertEqual(second.project_id, project)
                self.assertEqual(routes[0][3], 2000 if point == 'before-1' else 1000)
                before = snapshot(root)
                third = outcome(lambda: create_project(
                    store_steps.DirectTurns(root), 'example', now_ms=3000))
                self.assertEqual(snapshot(root), before)
                self.assertEqual(third, Creation(project, 'active'))

    def test_create_active_is_read_only(self):
        # Break: continuing an active name into the finish turns.
        first = create_project(store_steps.DirectTurns(self.root), 'example', now_ms=1000)
        before = snapshot(self.root)
        turns = RecordingTurns(store_steps.DirectTurns(self.root))
        value = outcome(lambda: create_project(turns, 'example', now_ms=2000))
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(turns.turns, [Declared('written', {})])
        self.assertEqual(value, Creation(first.project_id, 'active'))

    def test_create_held_refuses(self):
        # C2: extra route-check turn; C4: forbidden row and activation.
        for ledger in (False, True):
            with self.subTest(ledger=ledger):
                root = self.fresh(f'held-{ledger}')
                self.fixture(root, holder=J, ledger=ledger)
                before = snapshot(root)
                turns = RecordingTurns(store_steps.DirectTurns(root))
                value = outcome(lambda: create_project(turns, 'example', now_ms=2000))
                self.assertEqual(snapshot(root), before)
                self.assertEqual(turns.turns, [Declared('written', {})])
                self.assertIsInstance(value, CreationHeld)
                self.assertEqual(value.name, 'example')

    def test_finish_by_id_for_reconciler(self):
        # Break: treating a present schema/row as sufficient without activation.
        for ledger in (False, True):
            with self.subTest(ledger=ledger):
                root = self.fresh(f'reconcile-{ledger}')
                self.fixture(root, ledger=ledger, row=ledger)
                self.assertIsNone(finish_creation(store_steps.DirectTurns(root), P))
                self.assertEqual(self.rows(root), [(P, 'example', 'active', 1000, None)])
                self.assertEqual(self.rows(root, ledger=True), [(P, 'example', 1000)])

    def test_finish_by_id_active_is_read_only(self):
        # C5: active must return before any ledger open or activation turn.
        for holder in (None, J):
            for ledger in (False, True):
                with self.subTest(holder=holder, ledger=ledger):
                    root = self.fresh(f'active-{holder}-{ledger}')
                    self.fixture(root, holder=holder, active=True, ledger=ledger, row=ledger)
                    before = snapshot(root)
                    turns = RecordingTurns(store_steps.DirectTurns(root))
                    value = outcome(lambda: finish_creation(turns, P))
                    self.assertEqual(snapshot(root), before)
                    self.assertEqual(turns.turns, [Declared('read', {})])
                    self.assertIsNone(value)

    def test_finish_by_id_refuses_without_effect(self):
        # C3: a late re-check cannot undo created folders or the extra turn.
        for held in (False, True):
            for ledger in (False, True):
                with self.subTest(held=held, ledger=ledger):
                    root = self.fresh(f'refused-{held}-{ledger}')
                    if held:
                        self.fixture(root, holder=J, ledger=ledger)
                    elif ledger:
                        store_layout.open_ledger(root, P, create=True).close()
                    before = snapshot(root)
                    turns = RecordingTurns(store_steps.DirectTurns(root))
                    value = outcome(lambda: finish_creation(turns, P))
                    self.assertEqual(snapshot(root), before)
                    self.assertEqual(turns.turns, [Declared('read', {})])
                    if held:
                        self.assertIsInstance(value, CreationHeld)
                        self.assertEqual(value.name, 'example')
                    else:
                        self.assertIsInstance(value, store_layout.RouteUnavailable)
                        self.assertEqual(value.cause, 'not-found')

    def new_rollback_db(self, label):
        path = Path(self.tmp.name) / label
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE seed(value)').close()
            self.assertEqual(database_value(conn, 'PRAGMA journal_mode'), 'delete')
        return path

    def test_configure_waits_for_a_write_holder(self):
        path = self.new_rollback_db('wal-write-holder.sqlite3')
        release = threading.Event()
        retried = threading.Event()

        def pause(seconds):
            retried.set()
            release.set()
            time.sleep(seconds)

        with closing(sqlite3.connect(path)) as conn:
            with wal_write_holder(path, release):
                with mock.patch.object(store_layout, '_pause', pause, create=True):
                    store_layout._configure(conn)
            self.assertTrue(retried.is_set())
            self.assertEqual(database_value(conn, 'PRAGMA journal_mode'), 'wal')
            self.assertEqual(database_value(conn, 'PRAGMA busy_timeout'), 5000)

    def test_configure_budget_covers_sqlites_wait(self):
        for mode in ('reader', 'write holder'):
            with self.subTest(mode=mode):
                path = self.new_rollback_db(f'wal-budget-{mode}.sqlite3')
                state = {}

                def configure():
                    with closing(sqlite3.connect(path)) as conn:
                        started = time.monotonic()
                        state['outcome'] = outcome(lambda: store_layout._configure(conn))
                        state['elapsed'] = time.monotonic() - started
                        state['timeout'] = database_value(conn, 'PRAGMA busy_timeout')

                with closing(sqlite3.connect(path)) as other:
                    other.execute('BEGIN' if mode == 'reader' else 'BEGIN IMMEDIATE').close()
                    if mode == 'reader':
                        with closing(other.execute('SELECT * FROM seed')) as cursor:
                            cursor.fetchall()
                    with mock.patch.object(store_layout, '_WAL_SWITCH_BUDGET_S', 0.3, create=True):
                        worker = threading.Thread(target=configure)
                        worker.start()
                        try:
                            worker.join(timeout=2)
                            alive_at_bound = worker.is_alive()
                        finally:
                            other.commit()
                            worker.join(timeout=8)
                        self.assertFalse(worker.is_alive(), 'worker did not finish after release')
                    self.assertFalse(alive_at_bound, 'WAL switch exceeded its busy-wait budget')
                    self.assertIsInstance(state['outcome'], sqlite3.OperationalError)
                    self.assertEqual(state['outcome'].sqlite_errorname, 'SQLITE_BUSY')
                    self.assertGreaterEqual(state['elapsed'], 0.3)
                    self.assertLess(state['elapsed'], 1.5)
                    self.assertEqual(database_value(other, 'PRAGMA journal_mode'), 'delete')
                    self.assertEqual(state['timeout'], 5000)

    def test_two_finishes_wait_out_a_held_switch(self):
        self.fixture(self.root)
        database = store_layout.ledger_path(self.root, P).resolve()
        barrier = threading.Barrier(2, timeout=10)
        setup = threading.Lock()
        release = threading.Event()
        retried = threading.Event()
        targeted = threading.local()
        first = False
        configure = store_layout._configure

        class ConcurrentTurns:
            def __init__(inner_self):
                inner_self.inner = store_steps.DirectTurns(self.root)

            def run(inner_self, declared, body, *, fence_project=None):
                if declared.ledgers:
                    barrier.wait()
                return inner_self.inner.run(declared, body, fence_project=fence_project)

        def pause(seconds):
            if getattr(targeted, 'first', False):
                retried.set()
                release.set()
            time.sleep(seconds)

        def configure_ledger(conn):
            nonlocal first
            with closing(conn.execute('PRAGMA database_list')) as cursor:
                main = next(filename for _, name, filename in cursor if name == 'main')
            if Path(main).resolve() == database:
                # The peer cannot enter the switch until DDL setup and the
                # write holder are in place for the targeted ledger worker.
                with setup:
                    run_with_holder = not first
                    first = True
                    if run_with_holder:
                        self.assertEqual(database_value(conn, 'PRAGMA journal_mode'), 'delete')
                        holder = wal_write_holder(database, release)
                        holder.__enter__()
                if run_with_holder:
                    targeted.first = True
                    try:
                        return configure(conn)
                    finally:
                        targeted.first = False
                        holder.__exit__(None, None, None)
            return configure(conn)

        results = [None, None]

        def finish(index):
            results[index] = outcome(lambda: finish_creation(ConcurrentTurns(), P))

        threads = [threading.Thread(target=finish, args=(i,)) for i in range(2)]
        with mock.patch.object(store_layout, '_pause', pause, create=True):
            with mock.patch.object(store_layout, '_configure', configure_ledger):
                for thread in threads:
                    thread.start()
                try:
                    for thread in threads:
                        thread.join(timeout=30)
                finally:
                    release.set()
                    for thread in threads:
                        thread.join(timeout=12)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results, [None, None])
        self.assertEqual(self.rows(self.root, ledger=True), [(P, 'example', 1000)])
        self.assertEqual(self.rows(self.root)[0][2], 'active')
        self.assertTrue(first)
        self.assertTrue(retried.is_set())

    def test_configure_raises_other_errors_at_once(self):
        error = sqlite3.OperationalError('disk I/O error')
        error.sqlite_errorname = 'SQLITE_IOERR'

        class Stub:
            in_transaction = False
            timeout = 5000
            wal_calls = 0

            def execute(self, sql):
                if sql == 'PRAGMA journal_mode=WAL':
                    self.wal_calls += 1
                    raise error
                if sql.startswith('PRAGMA busy_timeout='):
                    self.timeout = int(sql.partition('=')[2])
                elif sql != 'PRAGMA busy_timeout':
                    raise AssertionError(f'unexpected statement: {sql}')
                cursor = mock.Mock()
                cursor.fetchone.return_value = (self.timeout,)
                return cursor

        conn = Stub()
        with mock.patch.object(store_layout, '_WAL_SWITCH_BUDGET_S', 0.03, create=True):
            with self.assertRaises(sqlite3.OperationalError) as raised:
                store_layout._configure(conn)
        self.assertIs(raised.exception, error)
        self.assertEqual(conn.wal_calls, 1)

    def test_finish_twice_concurrently(self):
        # C1: both callers pass admission, so duplicate insertion is observed.
        self.fixture(self.root)
        barrier = threading.Barrier(2, timeout=10)

        class ConcurrentTurns:
            def __init__(self, root):
                self.inner = store_steps.DirectTurns(root)

            def run(self, declared, body, *, fence_project=None):
                if declared.ledgers:
                    barrier.wait()
                return self.inner.run(declared, body, fence_project=fence_project)

        values = [None, None]
        def finish(index):
            values[index] = outcome(lambda: finish_creation(ConcurrentTurns(self.root), P))
        threads = [threading.Thread(target=finish, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(self.rows(self.root, ledger=True), [(P, 'example', 1000)])
        self.assertEqual(self.rows(self.root)[0][2], 'active')
        self.assertEqual(values, [None, None])
