"""Delta 8 creation: real turns, crash boundaries and admission side effects."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

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
