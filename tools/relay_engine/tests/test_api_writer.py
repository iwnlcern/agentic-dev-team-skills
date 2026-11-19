"""Slice A's worker transaction, failure and thread ownership contract."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

from relay_engine import api_writer, store_layout, submit, catalogue
from relay_engine.tests.writer_support import serve_for_test


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class Job:
    def __init__(self, call, files):
        self.run = call
        self.files = files


class RecordingConnection:
    def __init__(self, conn, closes):
        self.conn = conn
        self.closes = closes

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __setattr__(self, name, value):
        if name == 'isolation_level':
            self.conn.isolation_level = value
        else:
            object.__setattr__(self, name, value)

    def close(self):
        self.closes.append(threading.get_ident())
        self.conn.close()


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        catalogue_conn = store_layout.open_catalogue(self.root, kind='isolated')
        catalogue_conn.close()
        self.make_ledger()

    def make_ledger(self, project=P):
        conn = store_layout.open_ledger(self.root, P, create=True)
        try:
            conn.execute('INSERT INTO projects (id, name, created_at) VALUES (?, ?, 1)', (project, 'before')).close()
            conn.commit()
        finally:
            conn.close()

    def writer(self, hook=None, opened=True):
        writer = api_writer.StoreWriter(self.root, runtime_instance_id="r1", phase_hook=hook)
        writer.start()
        self.addCleanup(writer.close)
        if opened:
            serve_for_test(writer, P)
        return writer

    def job(self, call, project=P):
        return Job(call, frozenset({str(store_layout.ledger_path(self.root, project).absolute())}))

    def mode_submit(self, writer, call, mode):
        if mode == 'worker':
            return writer.submit(P, self.job(call))
        def callee(conn):
            conn.execute('BEGIN IMMEDIATE').close()
            outcome = call(conn)
            conn.execute('COMMIT' if outcome.commit else 'ROLLBACK').close()
            return outcome
        return writer.submit_callee(P, self.job(callee))

    def value(self):
        conn = sqlite3.connect(store_layout.ledger_path(self.root, P))
        try:
            return conn.execute('SELECT name FROM projects').fetchone()[0]
        finally:
            conn.close()

    def change(self, conn, name='after'):
        conn.execute('UPDATE projects SET name=?', (name,)).close()

    def success(self, conn):
        self.change(conn)
        return api_writer.JobOutcome(True, 'value')

    def assert_unavailable(self, call, reason):
        with self.assertRaises(api_writer.WriterUnavailable) as caught:
            call()
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_job_runs_inside_begin_immediate(self):
        finishing, release, attempting, acquired = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)

        def hook(phase):
            if phase == 'finishing':
                finishing.set()
                self.assertTrue(release.wait(3))

        writer = self.writer(hook)

        def run(conn):
            self.assertTrue(conn.in_transaction)
            return self.success(conn)

        def outside():
            conn = sqlite3.connect(store_layout.ledger_path(self.root, P))
            try:
                attempting.set()
                conn.execute('BEGIN IMMEDIATE').close()
                acquired.set()
                conn.rollback()
            finally:
                conn.close()

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(writer.submit, P, self.job(run))
            self.assertTrue(finishing.wait(3))
            second = pool.submit(outside)
            self.assertTrue(attempting.wait(3))
            self.assertFalse(acquired.wait(.05))
            release.set()
            self.assertTrue(first.result(3).commit)
            second.result(3)
        self.assertTrue(acquired.is_set())

    def test_commit_false_rolls_back(self):
        writer = self.writer()

        def run(conn):
            self.change(conn)
            return api_writer.JobOutcome(False, 'no')

        self.assertEqual(writer.submit(P, self.job(run)), api_writer.JobOutcome(False, 'no'))
        self.assertEqual(self.value(), 'before')

    def test_exception_rolls_back_and_reraises(self):
        writer = self.writer()
        original = ValueError('job error')

        def run(conn):
            self.change(conn)
            raise original

        with self.assertRaises(ValueError) as caught:
            writer.submit(P, self.job(run))
        self.assertIs(caught.exception, original)
        self.assertEqual(self.value(), 'before')

    def test_base_exception_from_job_rolls_back(self):
        writer = self.writer()
        for original in (KeyboardInterrupt(), SystemExit()):
            def run(conn):
                self.change(conn, 'uncommitted')
                raise original
            with self.assertRaises(type(original)) as caught:
                writer.submit(P, self.job(run))
            self.assertIs(caught.exception, original)
            self.assertEqual(self.value(), 'before')
            self.assertTrue(writer.submit(P, self.job(self.success)).commit)
            self.assertEqual(self.value(), 'after')
            conn = sqlite3.connect(store_layout.ledger_path(self.root, P))
            try:
                conn.execute("UPDATE projects SET name='before'").close()
                conn.commit()
            finally:
                conn.close()

    def test_job_error_context_survives_a_caller_except_block(self):
        # Break caught: handing a worker error back by an ordinary raise chains
        # it to a caller's active exception instead of retaining the job's chain.
        for mode, arm in ((mode, arm) for mode in ('worker', 'callee')
                          for arm in ('context', 'rollback_failed', 'no_context')):
            with self.subTest(arm=arm):
                writer = self.writer()
                original = RuntimeError('job')
                context = None if arm == 'no_context' else ValueError('worker')

                def run(conn):
                    conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,2)',
                                 (Q, 'uncommitted')).close()
                    if arm == 'rollback_failed':
                        self.deny_rollback(conn)
                    if context is not None:
                        try:
                            raise context
                        except ValueError:
                            raise original
                    raise original

                try:
                    try:
                        raise KeyError('caller')
                    except KeyError:
                        with self.assertRaises(RuntimeError) as caught:
                            self.mode_submit(writer, run, mode)
                    self.assertIs(caught.exception, original)
                    conn = sqlite3.connect(store_layout.ledger_path(self.root, P))
                    try:
                        self.assertEqual(conn.execute('SELECT id FROM projects WHERE id=?',
                                                      (Q,)).fetchall(), [])
                    finally:
                        conn.close()
                    self.assertIs(original.__context__, context)
                    self.assertIsNone(original.__cause__)
                    self.assertFalse(original.__suppress_context__)
                    if arm == 'rollback_failed':
                        self.assertEqual(original.__notes__, [
                            'writer ROLLBACK failed: DatabaseError SQLITE_AUTH'])
                    else:
                        self.assertFalse(hasattr(original, '__notes__'))
                finally:
                    writer.close()

    def test_queued_hook_exception_withdraws_the_job(self):
        # Break caught: releasing a request after its queued hook raises lets
        # the job commit even though submit has already raised to its caller.
        for mode, error_type in ((mode, error_type) for mode in ('worker', 'callee')
                                 for error_type in (RuntimeError, KeyboardInterrupt)):
            with self.subTest(error_type=error_type.__name__):
                original = error_type('queued')
                armed = True
                called = []

                def hook(phase):
                    nonlocal armed
                    if phase == 'queued' and armed:
                        armed = False
                        raise original

                writer = self.writer(hook)

                def run(conn):
                    called.append(True)
                    conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,2)',
                                 (Q, 'withdrawn')).close()
                    return api_writer.JobOutcome(True, 'withdrawn')

                def following(conn):
                    conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,3)',
                                 ('cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'following')).close()
                    return api_writer.JobOutcome(True, 'following')

                try:
                    with self.assertRaises(error_type) as caught:
                        self.mode_submit(writer, run, mode)
                    self.assertIs(caught.exception, original)
                    self.assertEqual(self.mode_submit(writer, following, mode),
                                     api_writer.JobOutcome(True, 'following'))
                    conn = sqlite3.connect(store_layout.ledger_path(self.root, P))
                    try:
                        rows = conn.execute('SELECT name FROM projects WHERE id != ?',
                                            (P,)).fetchall()
                    finally:
                        conn.close()
                    self.assertEqual((called, sorted(rows)), ([], [('following',)]))
                finally:
                    writer.close()
                    conn = sqlite3.connect(store_layout.ledger_path(self.root, P))
                    try:
                        conn.execute('DELETE FROM projects WHERE id != ?', (P,)).close()
                        conn.commit()
                    finally:
                        conn.close()

    def test_indeterminate_outcomes(self):
        writer = self.writer()
        trace = []
        for statement, original, commit in [('COMMIT', None, True),
                                             ('ROLLBACK', ValueError('ended'), False),
                                             ('ROLLBACK', None, False),
                                             ('ROLLBACK', KeyboardInterrupt(), False)]:
            with self.subTest(statement=statement, error=type(original).__name__):
                trace.clear()

                def run(conn):
                    conn.set_trace_callback(None)
                    conn.set_trace_callback(trace.append)
                    self.change(conn, 'own-' + statement)
                    conn.execute(statement).close()
                    if original is not None:
                        raise original
                    return api_writer.JobOutcome(commit, 'returned')

                out = writer.submit(P, self.job(run))
                self.assertFalse(out.commit)
                self.assertTrue(out.indeterminate)
                self.assertEqual(out.fault, 'job_ended_transaction')
                if original is None:
                    self.assertEqual(out.value, 'returned')
                else:
                    self.assertIs(out.value, original)
                    self.assertIsNone(original.__context__)
                    self.assertIsNone(original.__cause__)
                    self.assertFalse(hasattr(original, '__notes__'))
                self.assertEqual(trace[-1], statement)
                self.assertEqual(self.value(), 'own-COMMIT')
        writer.submit(P, self.job(lambda c: (c.set_trace_callback(None),
                                    api_writer.JobOutcome(False, None))[1]))

    def test_failed_commit_rolls_back(self):
        writer = self.writer()
        denied = []

        def authorizer(action, arg, *rest):
            if action == sqlite3.SQLITE_TRANSACTION and arg == 'COMMIT' and not denied:
                denied.append(True)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def run(conn):
            self.change(conn)
            conn.set_authorizer(authorizer)
            return api_writer.JobOutcome(True, None)

        with self.assertRaises(sqlite3.DatabaseError):
            writer.submit(P, self.job(run))
        self.assertEqual(self.value(), 'before')
        self.assertTrue(writer.submit(P, self.job(self.success)).commit)
        self.assertEqual(self.value(), 'after')

    def deny_rollback(self, conn):
        denied = []

        def authorizer(action, arg, *rest):
            if action == sqlite3.SQLITE_TRANSACTION and arg == 'ROLLBACK' and not denied:
                denied.append(True)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)

    def test_failed_rollback_faults_the_file(self):
        for contextual in (False, True):
            with self.subTest(contextual=contextual):
                release, entered, queued = (threading.Event() for _ in range(3))
                closes, originals, contexts = [], [], []
                main_id = threading.get_ident()
                real_open = store_layout.open_ledger

                def opened(*args, **kwargs):
                    return RecordingConnection(real_open(*args, **kwargs), closes)

                def hook(phase):
                    if phase == 'queued' and threading.current_thread().name.startswith('second'):
                        queued.set()

                with mock.patch.object(store_layout, 'open_ledger', side_effect=opened):
                    writer = self.writer(hook)

                def run(conn):
                    self.change(conn)
                    self.deny_rollback(conn)
                    entered.set()
                    self.assertTrue(release.wait(3))
                    if contextual:
                        try:
                            raise ValueError('context')
                        except ValueError as context:
                            contexts.append(context)
                            original = RuntimeError('job')
                            originals.append(original)
                            raise original
                    original = RuntimeError('job')
                    originals.append(original)
                    contexts.append(None)
                    raise original

                called = []
                with ThreadPoolExecutor(1, thread_name_prefix='first') as first_pool:
                    first = first_pool.submit(writer.submit, P, self.job(run))
                    self.assertTrue(entered.wait(3))
                    with ThreadPoolExecutor(1, thread_name_prefix='second') as second_pool:
                        second = second_pool.submit(writer.submit, P, self.job(lambda c: called.append(True)))
                        self.assertTrue(queued.wait(3))
                        release.set()
                        with self.assertRaises(RuntimeError) as caught:
                            first.result(3)
                        self.assertIs(caught.exception, originals[0])
                        error = originals[0]
                        self.assertIs(error.__context__, contexts[0])
                        self.assertIsNone(error.__cause__)
                        self.assertEqual(error.__notes__, [
                            'writer ROLLBACK failed: DatabaseError SQLITE_AUTH'])
                        seen = set()
                        cur = error
                        while cur is not None:
                            self.assertNotIn(id(cur), seen)
                            seen.add(id(cur))
                            cur = cur.__context__
                        queued_error = self.assert_unavailable(lambda: second.result(3), 'rollback_failed')
                        self.assertIsInstance(queued_error.__cause__, sqlite3.DatabaseError)
                self.assertFalse(called)
                self.assertEqual(len(closes), 1)
                self.assertNotEqual(closes[0], main_id)
                self.assertEqual(self.value(), 'before')
                self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')
                opened_error = self.assert_unavailable(lambda: writer.open_project(P), 'rollback_failed')
                self.assertIs(opened_error.__cause__, queued_error.__cause__)
                writer.close()
                self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'closed')

    def test_failed_rollback_without_job_error(self):
        writer = self.writer()

        def run(conn):
            self.change(conn)
            self.deny_rollback(conn)
            return api_writer.JobOutcome(False, None)

        error = self.assert_unavailable(lambda: writer.submit(P, self.job(run)), 'rollback_failed')
        self.assertIsInstance(error.__cause__, sqlite3.DatabaseError)
        self.assertEqual(self.value(), 'before')
        self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')

    def test_jobs_run_in_submission_order(self):
        entered, release, queued = (threading.Event() for _ in range(3))
        order, phases = [], []

        def hook(phase):
            phases.append(phase)
            if phase == 'queued' and entered.is_set():
                queued.set()

        writer = self.writer(hook)

        def first(conn):
            order.append('first')
            entered.set()
            self.assertTrue(release.wait(3))
            return api_writer.JobOutcome(False, None)

        def second(conn):
            order.append('second')
            return api_writer.JobOutcome(False, None)

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(writer.submit, P, self.job(first))
            self.assertTrue(entered.wait(3))
            b = pool.submit(writer.submit, P, self.job(second))
            self.assertTrue(queued.wait(3))
            release.set()
            a.result(3)
            b.result(3)
        self.assertEqual(order, ['first', 'second'])
        self.assertEqual([p for p in phases if p != 'queued'],
                         ['dequeued', 'began', 'finishing'] * 2)

    def test_nested_submit_is_refused_without_deadlock(self):
        nested = []
        writer = None

        def hook(phase):
            if phase == 'began':
                with self.assertRaises(api_writer.NestedWriterJob):
                    writer.submit(P, self.job(self.success))
                nested.append('hook')

        writer = self.writer(hook)

        def run(conn):
            for call in (lambda: writer.submit(P, self.job(self.success)), lambda: writer.open_project(P)):
                with self.assertRaises(api_writer.NestedWriterJob):
                    call()
            return api_writer.JobOutcome(True, None)

        with ThreadPoolExecutor(1) as pool:
            self.assertTrue(pool.submit(writer.submit, P, self.job(run)).result(1).commit)
        self.assertEqual(nested, ['hook'])

        # The submitting main thread is nested even when the worker has another id.
        release, began = threading.Event(), threading.Event()
        signal_results = []
        previous = signal.getsignal(signal.SIGALRM)

        def handler(signum, frame):
            try:
                writer.submit(P, self.job(self.success))
            except api_writer.NestedWriterJob:
                signal_results.append('refused')
            finally:
                release.set()

        def blocked(conn):
            began.set()
            self.assertTrue(release.wait(1))
            return api_writer.JobOutcome(True, None)

        def alarm():
            if began.wait(1):
                signal.pthread_kill(threading.main_thread().ident, signal.SIGALRM)

        signal.signal(signal.SIGALRM, handler)
        timer = threading.Thread(target=alarm)
        timer.start()
        try:
            self.assertTrue(writer.submit(P, self.job(blocked)).commit)
        finally:
            release.set()
            timer.join(2)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(signal_results, ['refused'])

    def test_busy_timeout_raises_writer_unavailable(self):
        writer = self.writer()
        outside = sqlite3.connect(store_layout.ledger_path(self.root, P))
        called = []
        try:
            outside.execute('BEGIN IMMEDIATE').close()
            error = self.assert_unavailable(
                lambda: writer.submit(P, self.job(lambda c: called.append(True))), 'busy')
            self.assertIsInstance(error.__cause__, sqlite3.OperationalError)
        finally:
            outside.rollback()
            outside.close()
        self.assertFalse(called)
        self.assertTrue(writer.submit(P, self.job(self.success)).commit)

    def test_close_stops_worker(self):
        closes = []
        real_open = store_layout.open_ledger

        def opened(*args, **kwargs):
            return RecordingConnection(real_open(*args, **kwargs), closes)

        with mock.patch.object(store_layout, 'open_ledger', side_effect=opened):
            writer = self.writer()
        writer.close()
        self.assertEqual(len(closes), 1)
        self.assertNotEqual(closes[0], threading.get_ident())
        self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'closed')
        self.assert_unavailable(lambda: writer.open_project(P), 'closed')
        with mock.patch.object(store_layout, 'open_ledger') as opening:
            empty = self.writer(opened=False)
            empty.close()
            opening.assert_not_called()

    def test_worker_connection_has_store_settings(self):
        calls = []
        real_open = store_layout.open_ledger

        def opened(*args, **kwargs):
            calls.append((threading.get_ident(), args, kwargs))
            return real_open(*args, **kwargs)

        with mock.patch.object(store_layout, 'open_ledger', side_effect=opened):
            writer = self.writer(opened=False)
            self.assertEqual(calls, [])
            writer.open_project(P)
            writer.open_project(P)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertEqual(calls[0][1], (self.root, P))
        self.assertFalse(calls[0][2].get('create', False))

        def check(conn):
            self.assertIsNone(conn.isolation_level)
            for pragma, value in [('synchronous', 2), ('journal_mode', 'wal'), ('foreign_keys', 1)]:
                self.assertEqual(conn.execute('PRAGMA ' + pragma).fetchone()[0], value)
            if sys.platform == 'darwin':
                for pragma in ('fullfsync', 'checkpoint_fullfsync'):
                    self.assertEqual(conn.execute('PRAGMA ' + pragma).fetchone()[0], 1)
            return api_writer.JobOutcome(False, None)

        writer.submit(P, self.job(check))
        direct = real_open(self.root, P)
        try:
            self.assertEqual(direct.isolation_level, '')
        finally:
            direct.close()
        if sys.platform == 'darwin':
            legacy = submit.open_store(store_layout.ledger_path(self.root, P))
            try:
                self.assertEqual(legacy.execute('PRAGMA fullfsync').fetchone()[0], 0)
            finally:
                legacy.close()

    def test_open_project_refuses_missing_and_wrong_project(self):
        for kind in ('missing', 'wrong-project'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'store'
                catalogue_conn = store_layout.open_catalogue(root, kind='isolated')
                catalogue_conn.close()
                path = store_layout.ledger_path(root, P)
                if kind == 'wrong-project':
                    conn = store_layout.open_ledger(root, P, create=True)
                    conn.execute('INSERT INTO projects (id, name, created_at) VALUES (?, ?, 1)', (Q, 'wrong')).close()
                    conn.commit()
                    conn.close()
                writer = api_writer.StoreWriter(root, runtime_instance_id="r1")
                writer.start()
                before = set(Path(tmp).rglob('*'))
                self.addCleanup(writer.close)
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    writer.open_project(P)
                self.assertEqual(caught.exception.cause, kind)
                self.assertEqual(set(Path(tmp).rglob('*')), before)
                self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')
                conn = (sqlite3.connect(path) if kind == 'wrong-project'
                        else store_layout.open_ledger(root, P, create=True))
                try:
                    conn.execute('DELETE FROM projects').close()
                    conn.execute('INSERT INTO projects (id, name, created_at) VALUES (?, ?, 1)', (P, 'before')).close()
                    conn.commit()
                finally:
                    conn.close()
                writer.open_project(P)
                self.assertTrue(writer.submit(P, Job(self.success, frozenset({str(path.absolute())}))).commit)
                writer.close()
                self.assertTrue(path.is_file())
        conn = store_layout.open_ledger(self.root, Q, create=True)
        conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, 1)', (Q, 'other')).close()
        conn.commit()
        conn.close()
        writer = self.writer(opened=False)
        real_open = store_layout.open_ledger
        with mock.patch.object(store_layout, 'open_ledger', wraps=real_open) as opened:
            writer.open_project(P)
            writer.open_project(Q)
            self.assertEqual(opened.call_args_list, [mock.call(self.root, P), mock.call(self.root, Q)])

    def test_writer_issues_no_savepoint(self):
        writer = self.writer()
        trace = []

        def first(conn):
            conn.set_trace_callback(trace.append)
            return self.success(conn)

        writer.submit(P, self.job(first))
        writer.submit(P, self.job(lambda c: api_writer.JobOutcome(False, None)))
        with self.assertRaises(ValueError):
            writer.submit(P, self.job(lambda c: (_ for _ in ()).throw(ValueError('job'))))
        writer.submit(P, self.job(lambda c: (c.set_trace_callback(None), api_writer.JobOutcome(False, None))[1]))
        self.assertFalse(any('SAVEPOINT' in s.upper() for s in trace))
        self.assertEqual(sum(s == 'COMMIT' for s in trace), 1)
        self.assertEqual(sum(s == 'ROLLBACK' for s in trace), 2)
        self.assertNotIn('SAVEPOINT', Path(api_writer.__file__).read_text().upper())
        trace.clear()

        def control(conn):
            conn.set_trace_callback(trace.append)
            conn.execute('SAVEPOINT platform_probe').close()
            return api_writer.JobOutcome(False, None)

        writer.submit(P, self.job(control))
        writer.submit(P, self.job(lambda c: (c.set_trace_callback(None), api_writer.JobOutcome(False, None))[1]))
        self.assertIn('SAVEPOINT platform_probe', trace)

    def test_constructor_takes_no_project(self):
        with mock.patch.object(store_layout, 'open_ledger') as opened:
            writer = api_writer.StoreWriter(self.root, runtime_instance_id='r1')
            self.addCleanup(writer.close)
            self.assertEqual(writer.runtime_instance_id, 'r1')
            with self.assertRaises(AttributeError):
                writer.runtime_instance_id = 'r2'
            opened.assert_not_called()
        with self.assertRaises(TypeError):
            api_writer.StoreWriter(self.root, P, runtime_instance_id='r1')

    def test_callee_job_receives_connection_without_transaction(self):
        writer = self.writer()
        trace = []
        writer.submit(P, self.job(lambda c: (c.set_trace_callback(trace.append),
            api_writer.JobOutcome(False, None))[1]))
        trace.clear()
        def run(conn):
            self.assertFalse(conn.in_transaction)
            self.assertFalse(any(s.startswith('BEGIN') for s in trace))
            conn.execute('BEGIN IMMEDIATE').close()
            self.change(conn)
            conn.execute('COMMIT').close()
            return api_writer.JobOutcome(True, 'callee')
        self.assertEqual(writer.submit_callee(P, self.job(run)).value, 'callee')
        self.assertEqual(self.value(), 'after')

    def test_callee_begin_is_linearization_point(self):
        handed, attempting, acquired = (threading.Event() for _ in range(3))
        writer = self.writer(lambda phase: handed.set() if phase == 'handed' else None)
        outside = sqlite3.connect(store_layout.ledger_path(self.root, P))
        outside.execute('BEGIN IMMEDIATE').close()
        def run(conn):
            attempting.set()
            conn.execute('BEGIN IMMEDIATE').close()
            acquired.set()
            self.change(conn)
            conn.execute('COMMIT').close()
            return api_writer.JobOutcome(True, None)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(writer.submit_callee, P, self.job(run))
            try:
                self.assertTrue(handed.wait(3))
                self.assertTrue(attempting.wait(3))
                self.assertFalse(acquired.is_set())
            finally:
                outside.rollback()
                outside.close()
            future.result(3)
        def read(conn):
            return api_writer.JobOutcome(False, conn.execute('SELECT name FROM projects').fetchone()[0])
        self.assertEqual(writer.submit(P, self.job(read)).value, 'after')

    def test_callee_left_transaction_is_rolled_back(self):
        writer = self.writer()
        def run(conn):
            conn.execute('BEGIN IMMEDIATE').close()
            self.change(conn)
            return api_writer.JobOutcome(True, 'bad')
        with self.assertRaises(api_writer.CalleeLeftTransaction):
            writer.submit_callee(P, self.job(run))
        self.assertEqual(self.value(), 'before')
        self.assertTrue(writer.submit(P, self.job(self.success)).commit)

    def test_worker_never_begins_before_callee_job(self):
        phases, trace = [], []
        writer = self.writer(lambda phase: phases.append((phase, tuple(trace))))
        writer.submit(P, self.job(lambda c: (c.set_trace_callback(trace.append),
            api_writer.JobOutcome(False, None))[1]))
        phases.clear(); trace.clear()
        writer.submit_callee(P, self.job(lambda c: api_writer.JobOutcome(False, None)))
        self.assertEqual([p for p, _ in phases if p != 'queued'], ['dequeued', 'handed', 'returned'])
        self.assertEqual(next(t for p, t in phases if p == 'handed'), ())
        writer.submit(P, self.job(self.success))
        self.assertIn('began', [p for p, _ in phases])

    def test_callee_and_worker_jobs_share_one_queue_in_order(self):
        entered, release, queued = (threading.Event() for _ in range(3))
        self.addCleanup(release.set)
        def hook(phase):
            if phase == 'queued' and entered.is_set(): queued.set()
        writer = self.writer(hook)
        order = []
        def first(conn):
            order.append('callee'); entered.set()
            self.assertTrue(release.wait(3))
            return api_writer.JobOutcome(False, None)
        def second(conn):
            order.append('worker')
            return api_writer.JobOutcome(False, None)
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(writer.submit_callee, P, self.job(first))
            self.assertTrue(entered.wait(3))
            two = pool.submit(writer.submit, P, self.job(second))
            self.assertTrue(queued.wait(3)); release.set()
            one.result(3); two.result(3)
        self.assertEqual(order, ['callee', 'worker'])

    def test_read_does_not_enter_queue(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hook(phase):
            if phase == 'dequeued':
                entered.set(); self.assertTrue(release.wait(3))
        writer = self.writer(hook)
        def read(conn):
            with self.assertRaises(sqlite3.OperationalError): self.change(conn)
            return conn.execute('SELECT name FROM projects').fetchone()[0]
        with ThreadPoolExecutor(2) as pool:
            job = pool.submit(writer.submit, P, self.job(self.success))
            self.assertTrue(entered.wait(3))
            observed = pool.submit(writer.read, P, read)
            try: self.assertEqual(observed.result(2), 'before')
            finally: release.set()
            job.result(3)

    def test_job_files_must_equal_the_ledger(self):
        self.second_project()
        phases = []
        writer = self.writer(phases.append)
        phases.clear()
        path = writer.ledger_file(P)
        other_path = store_layout.ledger_path(self.root, Q)
        self.assertTrue(other_path.is_file())
        actual, other = Path(path).stat(), other_path.stat()
        self.assertNotEqual((actual.st_dev, actual.st_ino), (other.st_dev, other.st_ino))
        for files in (frozenset(), frozenset({str(store_layout.ledger_path(self.root, Q))}),
                      frozenset({path, str(self.root / 'other')})):
            for submitter in (writer.submit, writer.submit_callee):
                with self.assertRaises(api_writer.UndeclaredFile):
                    submitter(P, Job(self.success, files))
        self.assertEqual(phases, [])
        link = Path(self.tmp.name) / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        alias = str(store_layout.ledger_path(link, P))
        self.assertNotEqual(alias, path)
        self.assertTrue(writer.submit(P, Job(self.success, frozenset({alias}))).commit)

    def test_job_without_files_is_refused(self):
        phases = []
        writer = self.writer(phases.append); phases.clear()
        class Missing:
            def run(self, conn): raise AssertionError('must not run')
        for submitter in (writer.submit, writer.submit_callee):
            with self.assertRaises(api_writer.UndeclaredFile): submitter(P, Missing())
        self.assertEqual(phases, [])

    def test_unserved_project_refused_before_files(self):
        phases = []
        writer = self.writer(phases.append)
        def fault(conn):
            self.deny_rollback(conn)
            return api_writer.JobOutcome(False, None)
        self.assert_unavailable(lambda: writer.submit(P, self.job(fault)), 'rollback_failed')
        phases.clear()
        for project in (P, Q):
            for job in (Job(self.success, frozenset()), object()):
                for submitter in (writer.submit, writer.submit_callee):
                    self.assert_unavailable(lambda: submitter(project, job), 'project_not_served')
            self.assert_unavailable(lambda: writer.read(project, lambda c: None), 'project_not_served')
            self.assert_unavailable(lambda: writer.ledger_file(project), 'project_not_served')
        self.assertEqual(phases, [])

    def second_project(self):
        conn = store_layout.open_ledger(self.root, Q, create=True)
        conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, 1)', (Q, 'other')).close()
        conn.commit(); conn.close()

    def test_two_projects_one_worker(self):
        self.second_project()
        writer = self.writer(); serve_for_test(writer, Q)
        worker_ids, order = [], []
        def run(project, fault=False):
            def job(conn):
                worker_ids.append(threading.get_ident()); order.append(project)
                self.assertEqual(conn.execute('PRAGMA database_list').fetchone()[2], writer.ledger_file(project))
                conn.execute("UPDATE projects SET name='changed'").close()
                if fault: self.deny_rollback(conn)
                return api_writer.JobOutcome(not fault, project)
            return self.job(job, project)
        entered, release, queued = (threading.Event() for _ in range(3))
        self.addCleanup(release.set)
        def hook(phase):
            if phase == 'dequeued' and not entered.is_set():
                entered.set(); self.assertTrue(release.wait(3))
            elif phase == 'queued' and entered.is_set(): queued.set()
        writer._phase_hook = hook
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(writer.submit, P, run(P))
            self.assertTrue(entered.wait(3))
            two = pool.submit(writer.submit, Q, run(Q))
            self.assertTrue(queued.wait(3)); release.set()
            one.result(3); two.result(3)
        self.assert_unavailable(lambda: writer.submit(P, run(P, True)), 'rollback_failed')
        self.assertTrue(writer.submit(Q, run(Q)).commit)
        self.assertEqual(order, [P, Q, P, Q])
        self.assertEqual(len(set(worker_ids)), 1)

    def test_store_id_read_each_call(self):
        # A fresh read must see a catalogue update made after an earlier call.
        root = Path(self.tmp.name) / 'catalogue-store'
        conn = store_layout.open_catalogue(root, kind='isolated')
        self.addCleanup(conn.close)
        writer = api_writer.StoreWriter(root, runtime_instance_id='r1')
        self.addCleanup(writer.close)
        first = writer.store_id()
        conn = store_layout.open_catalogue(root)
        conn.execute("UPDATE catalogue_identity SET store_id='second'").close()
        conn.commit(); conn.close()
        self.assertNotEqual(first, 'second')
        self.assertEqual(writer.store_id(), 'second')

    def test_served_projects_excludes_unopened_and_faulted(self):
        self.second_project()
        writer = self.writer(opened=False)
        snapshot = writer.served_projects()
        self.assertIsInstance(snapshot, frozenset)
        self.assertEqual(snapshot, frozenset())
        serve_for_test(writer, P); serve_for_test(writer, Q)
        self.assertEqual(snapshot, frozenset())
        def fault(conn):
            self.deny_rollback(conn)
            return api_writer.JobOutcome(False, None)
        self.assert_unavailable(lambda: writer.submit(P, self.job(fault)), 'rollback_failed')
        self.assertEqual(writer.served_projects(), frozenset({Q}))

    def test_interim_served_is_opened(self):
        """Pins this slice's interim meaning; Store replaces it with served state."""
        writer = self.writer()
        self.assertEqual(writer.served_projects(), frozenset({P}))


    def test_writer_start_opens_the_catalogue_once(self):
        path = store_layout.catalogue_path(self.root)
        expected = store_layout.read_store_id(self.root)
        self.assertFalse(Path(str(path) + '-wal').exists())
        calls = []
        real_open = store_layout.open_catalogue
        def opened(*args, **kwargs):
            calls.append((args, kwargs, threading.get_ident()))
            return real_open(*args, **kwargs)
        with mock.patch.object(store_layout, 'open_catalogue', opened):
            writer = self.writer(opened=False)
            self.assertTrue(Path(str(path) + '-wal').exists())
            self.assertTrue(Path(str(path) + '-shm').exists())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ((self.root,), {}))
            self.assertNotEqual(calls[0][2], threading.get_ident())
            writer.open_project(P)
            self.assertEqual(writer.store_id(), expected)
            self.assertEqual(writer.store_id(), expected)
            self.assertEqual(len(calls), 1)
            writer.close()

    def test_writer_start_refuses_a_missing_catalogue(self):
        for kind in ('empty', 'zero-byte', 'ledger-only'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'store'
                if kind == 'zero-byte':
                    root.mkdir()
                    store_layout.catalogue_path(root).touch()
                elif kind == 'ledger-only':
                    conn = store_layout.open_ledger(root, P, create=True)
                    conn.close()
                before = set(Path(tmp).rglob('*'))
                writer = api_writer.StoreWriter(root, runtime_instance_id='r1')
                try:
                    with self.assertRaises(store_layout.LayoutFault) as caught:
                        writer.start()
                    self.assertEqual(caught.exception.cause, 'missing')
                    self.assertEqual(set(Path(tmp).rglob('*')), before)
                    path = store_layout.catalogue_path(root)
                    if kind == 'zero-byte':
                        self.assertEqual(path.stat().st_size, 0)
                        self.assertFalse(Path(str(path) + '-wal').exists())
                        self.assertFalse(Path(str(path) + '-shm').exists())
                    else:
                        self.assertFalse(path.exists())
                    self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'closed')
                    self.assert_unavailable(lambda: writer.open_project(P), 'closed')
                    self.assertFalse(any(t.name == 'store-writer' and t.is_alive()
                                         for t in threading.enumerate()))
                finally:
                    writer.close()

    def test_writer_close_closes_the_catalogue_last(self):
        self.second_project()
        closes = []
        class Recorded:
            def __init__(self, conn, name):
                object.__setattr__(self, 'conn', conn)
                object.__setattr__(self, 'name', name)
            def __getattr__(self, name):
                return getattr(self.conn, name)
            def __setattr__(self, name, value):
                setattr(self.conn, name, value)
            def close(self):
                closes.append((self.name, threading.get_ident()))
                self.conn.close()
        real_ledger, real_catalogue = store_layout.open_ledger, store_layout.open_catalogue
        def ledger(root, project, **kwargs):
            return Recorded(real_ledger(root, project, **kwargs), project)
        def cat(root, **kwargs):
            return Recorded(real_catalogue(root, **kwargs), 'catalogue')
        with mock.patch.object(store_layout, 'open_ledger', ledger), mock.patch.object(store_layout, 'open_catalogue', cat):
            writer = self.writer(opened=False)
            writer.open_project(P); writer.open_project(Q)
            path = store_layout.catalogue_path(self.root)
            self.assertTrue(Path(str(path) + '-wal').exists())
            self.assertTrue(Path(str(path) + '-shm').exists())
            writer.close()
            self.assertEqual([name for name, _ in closes][-1:], ['catalogue'])
            self.assertCountEqual([name for name, _ in closes][:-1], [P, Q])
            self.assertTrue(all(tid != threading.get_ident() for _, tid in closes))
            self.assertFalse(Path(str(path) + '-wal').exists())
            self.assertFalse(Path(str(path) + '-shm').exists())
            with self.assertRaises(store_layout.LayoutFault) as caught:
                writer.store_id()
            self.assertEqual(caught.exception.cause, 'no-writer')
