"""Slice A's worker transaction, failure and thread ownership contract."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from relay_engine import api_writer, store_layout, store_steps, strings, submit, catalogue
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
        conn = store_layout.open_ledger(self.root, project, create=True)
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

    def declared(self, run, project=None):
        ledgers = {} if project is None else {project: 'read'}
        return api_writer.DeclaredJob(store_steps.Declared('written', ledgers), run)

    def record_receipt(self, conn):
        conn.execute(
            'INSERT INTO queue_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            ('receipt', 'operator', 'principal', 'answer', 'key', 'a' * 64, 'result', 1),
        ).close()

    @staticmethod
    def receipts(conn):
        with closing(conn.execute('SELECT receipt_id, result FROM queue_receipts')) as cursor:
            return cursor.fetchall()

    def test_declared_catalogue_only_commits(self):
        order = []
        waiting, release, queued = (threading.Event() for _ in range(3))

        def hook(phase):
            if phase == 'queued' and waiting.is_set():
                queued.set()

        writer = self.writer(hook)

        def run(conns):
            waiting.set()
            self.assertTrue(release.wait(5))
            self.assertEqual(tuple(conns), ('catalogue',))
            self.assertTrue(conns['catalogue'].in_transaction)
            order.append('write')
            self.record_receipt(conns['catalogue'])
            return api_writer.JobOutcome(True, 'receipt')

        def project_run(conn):
            order.append('project')
            return api_writer.JobOutcome(False, None)

        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(writer.submit_declared, self.declared(run))
            try:
                self.assertTrue(waiting.wait(5))
                project = pool.submit(writer.submit, P, self.job(project_run))
                self.assertTrue(queued.wait(5))
            finally:
                release.set()
            outcome = pending.result(5)
            self.assertFalse(project.result(5).commit)
        self.assertEqual(outcome, api_writer.JobOutcome(True, 'receipt'))
        self.assertEqual(order, ['write', 'project'])
        self.assertEqual(writer.read_catalogue(self.receipts), [('receipt', 'result')])

    def test_declared_catalogue_write_can_read_named_project(self):
        writer = self.writer()

        def run(conns):
            self.assertEqual(tuple(conns), ('catalogue', P))
            self.assertTrue(conns[P].in_transaction)
            with closing(conns[P].execute('SELECT id, name FROM projects')) as cursor:
                self.assertEqual(cursor.fetchall(), [(P, 'before')])
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.change(conns[P], 'uncommitted')
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
            self.record_receipt(conns['catalogue'])
            return api_writer.JobOutcome(True, 'receipt')

        self.assertTrue(writer.submit_declared(self.declared(run, P)).commit)
        self.assertEqual(self.value(), 'before')
        self.assertEqual(writer.read_catalogue(self.receipts), [('receipt', 'result')])
        self.assertTrue(writer.submit(P, self.job(self.success)).commit)
        self.assertEqual(self.value(), 'after')

    def test_declared_refusal_rolls_back_and_preserves_value(self):
        writer = self.writer()
        refusal = object()

        def run(conns):
            self.record_receipt(conns['catalogue'])
            return api_writer.JobOutcome(False, refusal)

        outcome = writer.submit_declared(self.declared(run, P))
        self.assertFalse(outcome.commit)
        self.assertFalse(outcome.indeterminate)
        self.assertIsNone(outcome.fault)
        self.assertIs(outcome.value, refusal)
        self.assertEqual(self.value(), 'before')
        self.assertEqual(writer.read_catalogue(self.receipts), [])

    def test_declared_refuses_wrong_identity_or_unserved_project_before_callback(self):
        for label in ('catalogue', P):
            with self.subTest(label=label):
                self.check_declared_identity_drift(label)

    def check_declared_identity_drift(self, label):
        paths = {'catalogue': store_layout.catalogue_path(self.root),
                 P: store_layout.ledger_path(self.root, P)}
        target = paths[label]
        replacement = target.with_suffix('.replacement')
        original = target.with_suffix('.original')
        with closing(sqlite3.connect(target)) as source:
            with closing(sqlite3.connect(replacement)) as destination:
                source.backup(destination)
        entered = threading.Event()
        release = threading.Event()
        callback = mock.Mock(side_effect=AssertionError('unexpected declared callback'))

        def hook(phase):
            if phase == 'queued' and threading.current_thread().name.startswith('identity'):
                entered.set()
                self.assertTrue(release.wait(5))

        writer = self.writer(hook)
        job = self.declared(callback, P)
        self.assert_unavailable(
            lambda: writer.submit_declared(self.declared(callback, Q)), 'project_not_served',
        )

        def read_path(conns):
            with closing(conns[label].execute('PRAGMA database_list')) as cursor:
                main = next(row[2] for row in cursor if row[1] == 'main')
            return api_writer.JobOutcome(False, main)

        cached = writer.submit_declared(self.declared(read_path, P)).value
        before = os.stat(cached)
        try:
            with mock.patch.object(store_layout, 'begin_declared',
                                   wraps=store_layout.begin_declared) as begin:
                with ThreadPoolExecutor(1, thread_name_prefix='identity') as pool:
                    result = pool.submit(writer.submit_declared, job)
                    try:
                        self.assertTrue(entered.wait(5))
                        target.rename(original)
                        replacement.rename(target)
                        after = target.stat()
                        cached_now = os.stat(cached)
                        self.assertNotEqual((before.st_dev, before.st_ino),
                                            (after.st_dev, after.st_ino))
                        self.assertEqual((cached_now.st_dev, cached_now.st_ino),
                                         (after.st_dev, after.st_ino))
                    finally:
                        release.set()
                    with self.assertRaises(api_writer.UndeclaredFile):
                        result.result(5)
                with self.assertRaises(api_writer.UndeclaredFile):
                    writer.submit_declared(job)
                begin.assert_not_called()
            callback.assert_not_called()
        finally:
            if original.exists():
                if target.exists():
                    target.rename(replacement)
                original.rename(target)
            writer.close()
        for file in (target, replacement):
            with closing(sqlite3.connect(file)) as conn:
                if label == 'catalogue':
                    self.assertEqual(self.receipts(conn), [])
                else:
                    with closing(conn.execute('SELECT name FROM projects')) as cursor:
                        self.assertEqual(cursor.fetchall(), [('before',)])
        with closing(sqlite3.connect(paths['catalogue'])) as conn:
            self.assertEqual(self.receipts(conn), [])
        # The second subcase needs a fresh independently prepared replacement.
        replacement.unlink()

    def test_declared_early_transaction_end_is_indeterminate(self):
        writer = self.writer()
        original = ValueError('callback')
        cause = RuntimeError('cause')
        original.__cause__ = cause

        def run(conns):
            self.record_receipt(conns['catalogue'])
            conns['catalogue'].execute('ROLLBACK').close()
            raise original

        result = writer.submit_declared(self.declared(run, P))
        self.assertFalse(result.commit)
        self.assertTrue(result.indeterminate)
        self.assertEqual(result.fault, 'job_ended_transaction')
        self.assertIs(result.value, original)
        self.assertIs(result.value.__cause__, cause)
        self.assertEqual(writer.read_catalogue(self.receipts), [])

    def test_declared_committed_receipt_survives_ledger_cleanup_fault(self):
        writer = self.writer()
        commit_and_cleanup = []

        def run(conns):
            cat, ledger = conns['catalogue'], conns[P]

            def deny_cleanup(action, arg, *rest):
                if action == sqlite3.SQLITE_TRANSACTION and arg == 'ROLLBACK':
                    with closing(store_layout.open_catalogue_readonly(self.root)) as reader:
                        commit_and_cleanup.append(self.receipts(reader))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            self.record_receipt(cat)
            ledger.set_authorizer(deny_cleanup)
            return api_writer.JobOutcome(True, 'receipt')

        outcome = writer.submit_declared(self.declared(run, P))
        self.assertEqual(outcome, api_writer.JobOutcome(True, 'receipt', fault='rollback_failed'))
        self.assertEqual(commit_and_cleanup, [[('receipt', 'result')]])
        self.assert_unavailable(lambda: writer.open_project(P), 'rollback_failed')
        self.assertEqual(writer.read_catalogue(self.receipts), [('receipt', 'result')])
        self.assertEqual(writer.read_catalogue(self.receipts), [('receipt', 'result')])

    def test_read_catalogue_is_readonly_and_closes_after_callback_or_failure(self):
        writer = self.writer()
        escaped = []
        original = ValueError('catalogue callback')
        nested_read = mock.Mock()

        def mutate(conns):
            self.record_receipt(conns['catalogue'])
            return api_writer.JobOutcome(True, 'unexpected receipt')

        nested_write = mock.Mock(side_effect=mutate)

        def check_nested_refusals():
            with self.assertRaises(api_writer.NestedWriterJob):
                writer.submit_declared(self.declared(nested_write))
            with self.assertRaises(api_writer.NestedWriterJob):
                writer.read_catalogue(nested_read)

        def inspect(conn):
            escaped.append(conn)
            check_nested_refusals()
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.record_receipt(conn)
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
            return self.receipts(conn)

        def fail(conn):
            escaped.append(conn)
            check_nested_refusals()
            raise original

        self.assertEqual(writer.read_catalogue(inspect), [])
        self.assertEqual(writer.read_catalogue(self.receipts), [])
        with self.assertRaises(ValueError) as caught:
            writer.read_catalogue(fail)
        self.assertIs(caught.exception, original)
        self.assertEqual(writer.read_catalogue(self.receipts), [])
        nested_read.assert_not_called()
        nested_write.assert_not_called()
        for conn in escaped:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

        def fail_project(conn):
            self.deny_rollback(conn)
            return api_writer.JobOutcome(False, None)

        self.assert_unavailable(lambda: writer.submit(P, self.job(fail_project)), 'rollback_failed')
        self.assertEqual(writer.read_catalogue(self.receipts), [])
        writer.close()
        self.assert_unavailable(lambda: writer.read_catalogue(self.receipts), 'closed')

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

    @contextmanager
    def rollback_diagnostics(self, stream, sink):
        strings.set_diagnostic_sink(sink)
        try:
            with redirect_stderr(stream):
                yield
        finally:
            strings.set_diagnostic_sink(None)

    def rollback_refusal(self, conn):
        self.change(conn)
        self.deny_rollback(conn)
        return api_writer.JobOutcome(False, None)

    def test_rollback_fault_reports_without_changing_caller_error(self):
        original = RuntimeError('private job value')
        context = ValueError('private context')
        cause = LookupError('private cause')

        def fail(conn):
            self.rollback_refusal(conn)
            try:
                raise context
            except ValueError:
                raise original from cause

        for run in (fail, self.rollback_refusal):
            with self.subTest(run=run.__name__):
                writer = self.writer()
                stderr = StringIO()
                diagnostics = []
                with self.rollback_diagnostics(stderr, diagnostics.append):
                    if run is fail:
                        with self.assertRaises(RuntimeError) as caught:
                            writer.submit(P, self.job(run))
                        self.assertIs(caught.exception, original)
                        self.assertIs(original.__context__, context)
                        self.assertIs(original.__cause__, cause)
                        self.assertEqual(original.__notes__, [
                            'writer ROLLBACK failed: DatabaseError SQLITE_AUTH'])
                    else:
                        error = self.assert_unavailable(
                            lambda: writer.submit(P, self.job(run)), 'rollback_failed')
                self.assertEqual(stderr.getvalue(), 'store writer rollback failed\n')
                self.assertEqual(len(diagnostics), 1)
                if run is not fail:
                    self.assertIs(error.__cause__, diagnostics[0])
                self.assertEqual(diagnostics[0].sqlite_errorcode, sqlite3.SQLITE_AUTH)
                fenced = self.assert_unavailable(lambda: writer.open_project(P), 'rollback_failed')
                self.assertIs(diagnostics[0], fenced.__cause__)
                self.assertEqual(self.value(), 'before')
                writer.close()

        writer = self.writer()
        stderr = StringIO()
        diagnostics = []
        ordinary = ValueError('ordinary failure')

        def fail_with_successful_rollback(conn):
            self.change(conn)
            raise ordinary

        with self.rollback_diagnostics(stderr, diagnostics.append):
            with self.assertRaises(ValueError) as caught:
                writer.submit(P, self.job(fail_with_successful_rollback))
        self.assertIs(caught.exception, ordinary)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(diagnostics, [])
        self.assertEqual(self.value(), 'before')

    def test_rollback_diagnostic_sink_failure_preserves_stderr(self):
        writer = self.writer()
        stderr = StringIO()
        sink = mock.Mock(side_effect=RuntimeError('sink failure'))

        with self.rollback_diagnostics(stderr, sink):
            error = self.assert_unavailable(
                lambda: writer.submit(P, self.job(self.rollback_refusal)), 'rollback_failed')
        self.assertIsInstance(error.__cause__, sqlite3.DatabaseError)
        sink.assert_called_once_with(error.__cause__)
        self.assertEqual(stderr.getvalue(), 'store writer rollback failed\n')

    def test_rollback_diagnostic_stderr_failure_preserves_sink(self):
        writer = self.writer()
        stderr = mock.Mock()
        stderr.write.side_effect = BrokenPipeError('stderr failure')
        diagnostics = []

        with self.rollback_diagnostics(stderr, diagnostics.append):
            error = self.assert_unavailable(
                lambda: writer.submit(P, self.job(self.rollback_refusal)), 'rollback_failed')
        stderr.write.assert_called_once_with('store writer rollback failed\n')
        self.assertIsInstance(error.__cause__, sqlite3.DatabaseError)
        self.assertEqual(diagnostics, [error.__cause__])

    def test_rollback_diagnostic_preserves_other_project_progress(self):
        self.make_ledger(Q)
        writer = self.writer()
        serve_for_test(writer, Q)
        stderr = StringIO()
        diagnostics = []

        with self.rollback_diagnostics(stderr, diagnostics.append):
            self.assert_unavailable(
                lambda: writer.submit(P, self.job(self.rollback_refusal)), 'rollback_failed')
            self.assertEqual(writer.served_projects(), frozenset({Q}))
            self.assertTrue(writer.submit(Q, self.job(self.success, Q)).commit)
        with closing(store_layout.open_ledger_readonly(self.root, Q)) as observer:
            with closing(observer.execute('SELECT id, name FROM projects')) as cursor:
                self.assertEqual(cursor.fetchall(), [(Q, 'after')])
        self.assertEqual(stderr.getvalue(), 'store writer rollback failed\n')
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(self.value(), 'before')

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

    def test_callee_closed_handle_keeps_exception(self):
        class JobFailure(BaseException):
            pass

        writer = self.writer()
        original = JobFailure('callee failed')

        def run(conn):
            conn.close()
            raise original

        with self.assertRaises(JobFailure) as caught:
            writer.submit_callee(P, self.job(run))
        self.assertIs(caught.exception, original)
        self.assertEqual(original.__notes__, ['writer handle closed by the job'])

    def test_callee_closed_handle_returned_answers_rollback_failed(self):
        writer = self.writer()

        def run(conn):
            conn.close()
            return api_writer.JobOutcome(True, 'not published')

        error = self.assert_unavailable(
            lambda: writer.submit_callee(P, self.job(run)), 'rollback_failed')
        self.assertIsInstance(error.__cause__, sqlite3.ProgrammingError)

    def test_callee_closed_handle_retires_project(self):
        self.second_project()
        reasons = {}
        for mode in ('rollback', 'closed'):
            with self.subTest(mode=mode):
                entered, released, queued, ran = (threading.Event() for _ in range(4))

                def hook(phase):
                    if phase == 'queued' and entered.is_set():
                        queued.set()

                def fail(conn):
                    entered.set()
                    self.assertTrue(released.wait(5))
                    if mode == 'closed':
                        conn.close()
                    else:
                        conn.execute('BEGIN IMMEDIATE').close()
                        self.deny_rollback(conn)
                    raise original

                def next_job(conn):
                    ran.set()
                    return api_writer.JobOutcome(True, 'another job')

                writer = self.writer(hook)
                serve_for_test(writer, Q)
                original = RuntimeError(mode)
                with ThreadPoolExecutor(2) as pool:
                    first = pool.submit(writer.submit_callee, P, self.job(fail))
                    try:
                        self.assertTrue(entered.wait(5))
                        second = pool.submit(writer.submit_callee, P, self.job(next_job))
                        self.assertTrue(queued.wait(5))
                    finally:
                        released.set()
                    with self.assertRaises(RuntimeError) as caught:
                        first.result(5)
                    self.assertIs(caught.exception, original)
                    behind = self.assert_unavailable(
                        lambda: second.result(5), 'rollback_failed')
                fresh = self.assert_unavailable(
                    lambda: writer.submit_callee(P, self.job(next_job)), 'project_not_served')
                opened = self.assert_unavailable(lambda: writer.open_project(P), 'rollback_failed')
                self.assertFalse(ran.is_set())
                self.assertEqual(writer.submit_callee(Q, self.job(next_job, Q)).value, 'another job')
                self.assertTrue(ran.is_set())
                reasons[mode] = (behind.reason, fresh.reason, opened.reason)
                if mode == 'closed':
                    self.assertEqual(reasons['closed'], reasons['rollback'])

    def test_rollback_failure_fence_unchanged(self):
        writer = self.writer()
        original = RuntimeError('job failed')

        def run(conn):
            self.deny_rollback(conn)
            raise original

        with self.assertRaises(RuntimeError) as caught:
            writer.submit(P, self.job(run))
        self.assertIs(caught.exception, original)
        self.assertEqual(original.__notes__, ['writer ROLLBACK failed: DatabaseError SQLITE_AUTH'])
        failure = writer._rollback_errors[P]
        self.assertIsInstance(failure, sqlite3.DatabaseError)
        self.assertEqual(failure.sqlite_errorname, 'SQLITE_AUTH')
        self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')
        error = self.assert_unavailable(lambda: writer.open_project(P), 'rollback_failed')
        self.assertIs(error.__cause__, failure)

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

    def test_store_id_requires_a_started_writer(self):
        writer = api_writer.StoreWriter(self.root, runtime_instance_id='r1')
        self.addCleanup(writer.close)
        self.assert_unavailable(writer.store_id, 'closed')

    def test_store_id_read_each_call(self):
        # A fresh read must see a catalogue update made after an earlier call.
        root = Path(self.tmp.name) / 'catalogue-store'
        conn = store_layout.open_catalogue(root, kind='isolated')
        self.addCleanup(conn.close)
        writer = api_writer.StoreWriter(root, runtime_instance_id='r1')
        self.addCleanup(writer.close)
        writer.start()
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
            self.assert_unavailable(writer.store_id, 'closed')


class ProjectWriterFixture(unittest.TestCase):
    setUp = WriterTests.setUp

    def make_ledger(self, project=P):
        WriterTests.make_ledger(self, project)
        with closing(store_layout.open_catalogue(self.root)) as conn:
            conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) '
                         "VALUES (?, ?, 'active', 1)", (project, f'project-{project}')).close()
            conn.commit()

    writer = WriterTests.writer
    job = WriterTests.job
    change = WriterTests.change
    success = WriterTests.success
    value = WriterTests.value
    declared = WriterTests.declared
    assert_unavailable = WriterTests.assert_unavailable

    principal = ' Exact Principal \u00e9 '

    def writer_pair(self):
        self.make_ledger(Q)
        writer = self.writer()
        serve_for_test(writer, Q)
        return writer

    def row(self, project=P):
        with closing(store_layout.open_catalogue(self.root)) as conn:
            scope = 'store' if project is None else 'project'
            store_layout.record_stop(conn, scope, project, by=self.principal)
            conn.commit()

    def stops(self, writer):
        return writer.read_catalogue(store_layout.open_stops)

    def flush(self, writer):
        self.assertTrue(writer.submit_declared(
            self.declared(lambda _: api_writer.JobOutcome(True, None))).commit)

    def fail_record(self, function):
        return mock.patch.object(store_layout, function, side_effect=ValueError('no write'))

    @staticmethod
    def indeterminate(conn, *_args, **_kwargs):
        conn.execute('COMMIT').close()

    @contextmanager
    def pool(self, size, *releases):
        with ThreadPoolExecutor(size) as pool:
            try:
                yield pool
            finally:
                for release in releases:
                    release.set()

    def pause_close(self, writer, entered, release, *, fail=False):
        target = writer._connections[P]
        real_close = writer._close_handle

        def close(conn):
            if conn is target and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(3))
                if fail:
                    class FailedClose:
                        def close(self):
                            raise OSError('injected close failure')

                    return real_close(FailedClose())
            return real_close(conn)

        return mock.patch.object(writer, '_close_handle', side_effect=close)


class ProjectStopTests(ProjectWriterFixture):
    def test_stop_withdraws_queued_and_off_queue_requests(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        for off_queue in (False, True):
            with self.subTest(off_queue=off_queue):
                active, finish, queued, ready, popped, fence = (threading.Event() for _ in range(6))
                calls = []
                real_get = writer._queue.get

                def get(*args, **kwargs):
                    request = real_get(*args, **kwargs)
                    if request.kind == 'job' and queued.is_set():
                        popped.set()
                    return request

                caller = threading.local()

                def as_role(role, fn, *args):
                    caller.role = role
                    try:
                        return fn(*args)
                    finally:
                        del caller.role

                def hold(phase):
                    role = getattr(caller, 'role', None)
                    if phase == 'queued' and role == 'waiting':
                        queued.set()
                        if off_queue:
                            self.assertTrue(ready.wait(3))
                    elif phase == 'queued' and role == 'stop':
                        fence.set()

                def first(conn):
                    active.set()
                    self.assertTrue(finish.wait(3))
                    return self.success(conn)

                def later(_conn):
                    calls.append('ran')
                    return api_writer.JobOutcome(True, None)

                writer._phase_hook = hold
                with mock.patch.object(writer._queue, 'get', side_effect=get):
                    with self.pool(3, finish, ready) as pool:
                        one = pool.submit(writer.submit, P, self.job(first))
                        self.assertTrue(active.wait(3))
                        two = pool.submit(as_role, 'waiting', writer.submit, P, self.job(later))
                        self.assertTrue(queued.wait(3))
                        if off_queue:
                            finish.set()
                            self.assertTrue(one.result(3).commit)
                            self.assertTrue(popped.wait(3))
                        three = pool.submit(as_role, 'stop', stop, P, self.principal)
                        self.assertTrue(fence.wait(3))
                        ready.set()
                        self.assert_unavailable(lambda: two.result(2), 'project_not_served')
                        finish.set()
                        self.assertTrue(one.result(3).commit)
                        self.assertEqual(three.result(3), 'stopped')
                writer._phase_hook = None
                self.flush(writer)
                self.assertEqual(calls, [])
                self.assertNotIn(P, writer.served_projects())
                self.assertEqual(self.stops(writer)[0].stopped_by, self.principal)
                self.assertEqual(writer.request_project_start(P, self.principal), 'started')
                self.flush(writer)

    def test_stop_leaves_other_work_queued(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        entered, release, queued = (threading.Event() for _ in range(3))

        def block(conn):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.success(conn)

        def phase(name):
            if name == 'queued' and entered.is_set():
                queued.set()

        writer._phase_hook = phase
        with self.pool(4, release) as pool:
            active = pool.submit(writer.submit, P, self.job(block))
            self.assertTrue(entered.wait(3))
            stopping = pool.submit(stop, P, self.principal)
            self.assertTrue(queued.wait(3))
            other = pool.submit(writer.submit, Q, self.job(self.success, Q))
            catalogue_only = pool.submit(writer.submit_declared, self.declared(
                lambda conns: (self.record_receipt(conns['catalogue'])
                               or api_writer.JobOutcome(True, None))))
            release.set()
            self.assertTrue(active.result(3).commit)
            self.assertTrue(other.result(3).commit)
            self.assertTrue(catalogue_only.result(3).commit)
            self.assertEqual(stopping.result(3), 'stopped')
        self.assertEqual(writer.read_catalogue(WriterTests.receipts), [('receipt', 'result')])

    record_receipt = WriterTests.record_receipt

    def test_queued_declared_naming_project_withdrawn(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        entered, release, queued, fenced = (threading.Event() for _ in range(4))
        ran = []

        def block(conn):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.success(conn)

        def phase(name):
            if name == 'queued' and entered.is_set():
                (fenced if queued.is_set() else queued).set()

        writer._phase_hook = phase
        with self.pool(3, release) as pool:
            active = pool.submit(writer.submit, P, self.job(block))
            self.assertTrue(entered.wait(3))
            pending = pool.submit(writer.submit_declared, self.declared(ran.append, P))
            self.assertTrue(queued.wait(3))
            stopping = pool.submit(stop, P, self.principal)
            self.assertTrue(fenced.wait(3))
            self.assert_unavailable(lambda: pending.result(2), 'project_not_served')
            release.set()
            self.assertTrue(active.result(3).commit)
            self.assertEqual(stopping.result(3), 'stopped')
        self.assertEqual(ran, [])

    def test_second_stop_already_stopped(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        phases = []
        writer._phase_hook = phases.append
        self.assertEqual(stop(P, self.principal), 'stopped')
        self.assertEqual(stop(P, 'different'), 'already_stopped')
        self.flush(writer)
        self.assertEqual(phases.count('project_stop'), 1)
        self.assertEqual(self.stops(writer)[0].stopped_by, self.principal)

    def test_not_recorded_restores_open(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        before = writer._connections[P]
        with self.fail_record('record_stop'):
            self.assertEqual(stop(P, self.principal), 'not_recorded')

        def job(conn):
            self.assertIs(conn, before)
            return self.success(conn)

        self.assertTrue(writer.submit(P, self.job(job)).commit)
        self.assertEqual(self.value(), 'after')

    def test_not_recorded_from_starting_publishes_nothing(self):
        writer = self.writer(opened=False)
        stop = writer.request_project_stop
        with writer._lock:
            writer._project_admission[P] = 'starting'
        with self.fail_record('record_stop'):
            self.assertEqual(stop(P, self.principal), 'not_recorded')
        self.assertEqual(writer._project_admission[P], 'starting')
        self.assertNotIn(P, writer.served_projects())

    def test_known_no_write_reads_row(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        phases = []
        writer._phase_hook = phases.append
        with self.fail_record('record_stop'):
            self.assertEqual(stop(P, self.principal), 'not_recorded')
        self.assertTrue(writer.submit(P, self.job(self.success)).commit)
        self.row()
        with self.fail_record('record_stop'):
            self.assertEqual(stop(P, self.principal), 'already_stopped')
        self.flush(writer)
        self.assertNotIn(P, writer._served)
        self.assertEqual(phases.count('project_stop'), 1)
        self.assertEqual(len(self.stops(writer)), 1)
        self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')

    def test_known_no_write_open_row_posts_once(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        entered, release = threading.Event(), threading.Event()
        phases = []
        writer._phase_hook = phases.append
        self.row()
        with self.pause_close(writer, entered, release):
            try:
                with self.fail_record('record_stop'):
                    self.assertEqual(stop(P, self.principal), 'already_stopped')
                self.assertTrue(entered.wait(3))
                with mock.patch.object(writer, 'submit_declared',
                                       return_value=api_writer.JobOutcome(False, None)):
                    self.assertEqual(stop(P, self.principal), 'already_stopped')
            finally:
                release.set()
        self.flush(writer)
        with self.fail_record('record_stop'):
            self.assertEqual(stop(P, self.principal), 'already_stopped')
        self.flush(writer)
        self.assertEqual(phases.count('project_stop'), 1)
        self.assertNotIn(P, writer._served)
        self.assertEqual(len(self.stops(writer)), 1)

    def test_not_recorded_restores_stopping(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        with (mock.patch.object(store_layout, 'record_stop', self.indeterminate),
              mock.patch.object(store_layout, 'open_stops', side_effect=OSError('unreadable'))):
            self.assertEqual(stop(P, self.principal), 'not_known')
        with self.fail_record('record_stop'):
            self.assertEqual(stop(P, self.principal), 'not_recorded')
        self.assertEqual(writer._project_admission[P], 'stopping')
        self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')

    def test_indeterminate_recorded_stop_answers_stopped(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        real_record = store_layout.record_stop

        def record(conn, *args, **kwargs):
            real_record(conn, *args, **kwargs)
            self.indeterminate(conn)

        with mock.patch.object(store_layout, 'record_stop', record):
            self.assertEqual(stop(P, self.principal), 'stopped')
        self.flush(writer)
        self.assertEqual(self.stops(writer)[0].stopped_by, self.principal)
        self.assertNotIn(P, writer._served)

    def test_not_known_leaves_stopping(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        with (mock.patch.object(store_layout, 'record_stop', self.indeterminate),
              mock.patch.object(store_layout, 'open_stops', side_effect=OSError('unreadable'))):
            self.assertEqual(stop(P, self.principal), 'not_known')
        self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'project_not_served')

    def test_stop_during_open_leaves_unpublished(self):
        writer = self.writer(opened=False)
        stop = writer.request_project_stop
        entered, release, fenced = (threading.Event() for _ in range(3))
        real_open = store_layout.open_ledger
        closes = []

        def open_ledger(*args, **kwargs):
            conn = RecordingConnection(real_open(*args, **kwargs), closes)
            entered.set()
            self.assertTrue(release.wait(3))
            return conn

        writer._phase_hook = lambda phase: fenced.set() if phase == 'queued' else None
        with mock.patch.object(store_layout, 'open_ledger', open_ledger):
            with self.pool(2, release) as pool:
                opening = pool.submit(writer.open_project, P)
                self.assertTrue(entered.wait(3))
                stopping = pool.submit(stop, P, self.principal)
                self.assertTrue(fenced.wait(3))
                release.set()
                self.assert_unavailable(lambda: opening.result(3), 'project_not_served')
                self.assertEqual(stopping.result(3), 'stopped')
        self.flush(writer)
        self.assertNotIn(P, writer._served)
        self.assertEqual(closes, [writer._worker.ident])

    def test_project_stop_close_outside_lock(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        import time
        from types import SimpleNamespace

        entered, release = threading.Event(), threading.Event()
        with self.pause_close(writer, entered, release):
            with self.pool(1, release) as pool:
                self.assertEqual(stop(P, self.principal), 'stopped')
                self.assertTrue(entered.wait(3))
                now = time.monotonic()
                fence = pool.submit(writer.fence_for_stop,
                                    SimpleNamespace(active=now + 0.1, work=now + 0.5))
                self.assertTrue(fence.result(1))
                self.assertFalse(release.is_set())
                release.set()

        writer = self.writer_pair_for_close_retry()
        entered, release = threading.Event(), threading.Event()
        with self.pause_close(writer, entered, release, fail=True):
            try:
                self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
                self.assertTrue(entered.wait(3))
            finally:
                release.set()
            self.flush(writer)
        self.assert_unavailable(lambda: writer.open_project(P), 'rollback_failed')

    def writer_pair_for_close_retry(self):
        # The first writer fenced the entire service; reopen with a fresh row state.
        self.addCleanup(lambda: None)
        with closing(store_layout.open_catalogue(self.root)) as conn:
            store_layout.record_start(conn, 'project', P, by=self.principal)
            conn.commit()
        return self.writer()

    def test_start_of_unstopped_not_stopped(self):
        writer = self.writer_pair()
        start = writer.request_project_start
        phases = []
        writer._phase_hook = phases.append
        self.assertEqual(start(P, self.principal), 'not_stopped')
        self.flush(writer)
        self.assertNotIn('open', phases)
        self.assertIn(P, writer.served_projects())

    def test_start_still_stopped(self):
        writer = self.writer_pair()
        start = writer.request_project_start
        self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
        with self.fail_record('record_start'):
            self.assertEqual(start(P, self.principal), 'still_stopped')
        self.assertNotIn(P, writer._pending_project_start)
        self.assertEqual(writer._project_admission[P], 'stopping')

    def test_start_not_known_then_retry(self):
        writer = self.writer_pair()
        start = writer.request_project_start
        self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
        real_record = store_layout.record_start

        def record(conn, *args, **kwargs):
            real_record(conn, *args, **kwargs)
            self.indeterminate(conn)

        with (mock.patch.object(store_layout, 'record_start', record),
              mock.patch.object(store_layout, 'open_stops', side_effect=OSError('unreadable'))):
            self.assertEqual(start(P, self.principal), 'not_known')
        self.assertNotIn(P, writer._served)
        self.assertEqual(self.stops(writer), [])
        self.assertEqual(start(P, self.principal), 'started')
        self.flush(writer)
        self.assertIn(P, writer.served_projects())

    def test_start_serves_stopped_project(self):
        writer = self.writer_pair()
        start = writer.request_project_start
        self.assertEqual(writer.request_project_stop(P, 'stopper'), 'stopped')
        self.assertEqual(start(P, self.principal), 'started')
        self.flush(writer)

        def read(conn):
            with closing(conn.execute('SELECT ended_by FROM engine_stops')) as cursor:
                return cursor.fetchall()

        self.assertEqual(writer.read_catalogue(read), [(self.principal,)])
        self.assertIn(P, writer.served_projects())
        self.assertTrue(writer.submit(P, self.job(self.success)).commit)

    def test_open_with_open_row_stays_unopened(self):
        for project in (P, None):
            with self.subTest(project=project):
                self.row(project)
                writer = self.writer(opened=False)
                try:
                    writer.open_project(P)
                    self.assertNotIn(P, writer.served_projects())
                    self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)),
                                            'project_not_served')
                finally:
                    writer.close()
                    with closing(store_layout.open_catalogue(self.root)) as conn:
                        store_layout.record_start(conn, 'store' if project is None else 'project',
                                                  project, by=self.principal)
                        conn.commit()

    def test_requests_on_one_project_serialize(self):
        writer = self.writer_pair()
        stop = writer.request_project_stop
        entered, release, starting = (threading.Event() for _ in range(3))
        real_stop = store_layout.record_stop
        order = []

        def record(*args, **kwargs):
            order.append('stop')
            entered.set()
            self.assertTrue(release.wait(3))
            return real_stop(*args, **kwargs)

        def start():
            starting.set()
            return writer.request_project_start(P, self.principal)

        real_start = store_layout.record_start

        def record_start(*args, **kwargs):
            order.append('start')
            return real_start(*args, **kwargs)

        with (mock.patch.object(store_layout, 'record_stop', record),
              mock.patch.object(store_layout, 'record_start', record_start)):
            with self.pool(2, release) as pool:
                stopping = pool.submit(stop, P, self.principal)
                self.assertTrue(entered.wait(3))
                started = pool.submit(start)
                self.assertTrue(starting.wait(3))
                self.assertEqual(order, ['stop'])
                release.set()
                self.assertEqual(stopping.result(3), 'stopped')
                self.assertEqual(started.result(3), 'started')
        self.flush(writer)
        self.assertEqual(order, ['stop', 'start'])
        self.assertEqual(writer._project_admission.get(P, 'open'), 'open')
        self.assertIn(P, writer.served_projects())


class CatalogueHookTests(ProjectWriterFixture):
    def make_writer(self, hooks=None):
        writer = api_writer.StoreWriter(self.root, runtime_instance_id='r1', hooks=hooks)
        self.addCleanup(writer.close)
        return writer

    def attempt(self, writer):
        now = time.monotonic()
        attempt = SimpleNamespace(active=now + 2, work=now + 3)
        writer.bind_service_owner(None, SimpleNamespace(attempt=attempt, admission_closed=True))
        return attempt

    def test_no_hooks_keeps_writer_unchanged(self):
        writer = self.make_writer()
        writer.start()
        self.assertTrue(writer.store_id())

    def test_catalogue_hooks_called_on_worker(self):
        writer = self.make_writer(hooks := mock.Mock())
        seen = []
        hooks.catalogue_opened.side_effect = lambda: seen.append(
            (threading.current_thread().name, writer._started.is_set()))
        hooks.catalogue_closing.side_effect = lambda **_: seen.append(
            (threading.current_thread().name, writer._started.is_set()))
        writer.start(mode='service')
        self.assertEqual(writer.wait_started().state, 'serving')
        writer.close()
        self.assertEqual(seen, [('store-writer', False), ('store-writer', True)])

    def test_catalogue_retirement_calls_closing(self):
        writer = self.make_writer(hooks := mock.Mock())
        seen = []

        def before_close(**kwargs):
            self.assertIsNone(kwargs['deadline'].at())
            seen.append(threading.current_thread().name)

        hooks.catalogue_closing.side_effect = before_close
        writer.start()

        def retire(_):
            writer._retire_declared_file('catalogue', writer._catalogue, OSError('fault'))
            return api_writer.JobOutcome(False, None)

        with mock.patch.object(writer, '_run_declared', side_effect=retire):
            writer.submit_declared(self.declared(lambda _: None))
        writer.close()
        self.assertEqual(seen, ['store-writer'])

    def test_stop_deadline_armed_once_by_fence(self):
        writer = self.make_writer()
        writer.start()
        attempt = self.attempt(writer)
        self.assertIsNone(writer._stop_deadline.at())
        self.assertTrue(writer.fence_for_stop(attempt))
        self.assertEqual(writer._stop_deadline.at(), attempt.active)
        self.assertTrue(writer.fence_for_stop(SimpleNamespace(active=0, work=attempt.work)))
        self.assertEqual(writer._stop_deadline.at(), attempt.active)

    def test_catalogue_closing_receives_the_stop_deadline(self):
        writer = self.make_writer(hooks := mock.Mock())
        hooks.catalogue_closing.return_value = False
        writer.start()
        attempt = self.attempt(writer)
        self.assertTrue(writer.fence_for_stop(attempt))
        self.assertTrue(writer.settle_service_stop(attempt))
        hooks.catalogue_closing.assert_called_once_with(deadline=writer._stop_deadline)
        self.assertEqual(hooks.catalogue_closing.call_args.kwargs['deadline'].at(), attempt.active)

    def fault_result(self, writer, cause, error):
        result = writer.wait_started()
        self.assertEqual((result.state, result.causes), ('held', (cause,)))
        self.assertIs(result.port.error, error)
        for name, args in {
            'submit': (P, None), 'submit_callee': (P, None), 'submit_declared': (None,),
            'submit_transfer': (None,), 'read_declared': (None, None), 'read': (P, None),
            'read_catalogue': (None,), 'ledger_file': (P,), 'store_id': (),
            'served_projects': (), 'request_project_stop': (P, 'actor'),
            'request_project_start': (P, 'actor'),
        }.items():
            with self.subTest(operation=name):
                with self.assertRaises(api_writer.WriterUnavailable) as raised:
                    getattr(result.port, name)(*args)
                self.assertEqual(raised.exception.reason, cause)
                self.assertIs(raised.exception.__cause__, error)

    def test_start_fault_port_touches_nothing(self):
        hooks = mock.Mock()
        error = OSError('open hook failed')
        hooks.catalogue_opened.side_effect = error
        writer = self.make_writer(hooks)
        ingest = mock.Mock()
        writer.start(mode='service', before_joins=ingest)
        self.fault_result(writer, 'start_fault', error)
        ingest.assert_not_called()
        hooks.catalogue_closing.assert_not_called()
        self.assertIsNone(writer._catalogue)

    def test_catalogue_unopenable_held_in_service_mode(self):
        for error in (store_layout.LayoutFault('missing'), PermissionError(), sqlite3.DatabaseError()):
            with self.subTest(error=type(error).__name__):
                writer = self.make_writer(hooks := mock.Mock())
                ingest = mock.Mock()
                with mock.patch.object(store_layout, 'open_catalogue', side_effect=error):
                    writer.start(mode='service', before_joins=ingest)
                    self.fault_result(writer, 'catalogue_unopenable', error)
                    with self.assertRaises(type(error)) as ordinary:
                        self.make_writer().start()
                self.assertIs(ordinary.exception, error)
                hooks.catalogue_opened.assert_not_called()
                hooks.catalogue_closing.assert_not_called()
                ingest.assert_not_called()

    def test_held_start_stops_clean(self):
        for cause in ('start_fault', 'catalogue_unopenable'):
            with self.subTest(cause=cause):
                writer = self.make_writer(hooks := mock.Mock())
                error = OSError('startup fault')
                failure = (mock.patch.object(store_layout, 'open_catalogue', side_effect=error)
                           if cause == 'catalogue_unopenable'
                           else mock.patch.object(hooks, 'catalogue_opened', side_effect=error))
                with failure:
                    writer.start(mode='service')
                    self.fault_result(writer, cause, error)
                attempt = self.attempt(writer)
                self.assertTrue(writer.fence_for_stop(attempt))
                self.assertTrue(writer.settle_service_stop(attempt))
                self.assertIsNone(writer._start_error)
                self.assertIsNone(writer._catalogue)
                self.assertFalse(writer._worker.is_alive())

    def test_held_start_failed_close_stays_unclean(self):
        writer = self.make_writer(hooks := mock.Mock())
        error = OSError('open hook failed')
        hooks.catalogue_opened.side_effect = error
        open_catalogue = store_layout.open_catalogue
        captured = []

        class FailedClose:
            def close(self):
                raise OSError('injected close failure')

        def open_with_close_failure(*args, **kwargs):
            open_catalogue(*args, **kwargs).close()
            captured.append(FailedClose())
            return captured[-1]

        with mock.patch.object(store_layout, 'open_catalogue', open_with_close_failure):
            writer.start(mode='service')
            self.fault_result(writer, 'start_fault', error)
        attempt = self.attempt(writer)
        self.assertTrue(writer.fence_for_stop(attempt))
        self.assertFalse(writer.settle_service_stop(attempt))
        self.assertEqual(writer._failed_closes, captured)
        self.assertFalse(writer._close_complete)
        self.assertIsNone(writer._start_error)

    def test_closing_raise_reported_and_close_goes_on(self):
        writer = self.make_writer(hooks := mock.Mock())
        error = OSError('close hook failed')
        hooks.catalogue_closing.side_effect = error
        writer.start()
        with mock.patch.object(strings, 'report_diagnostic') as report:
            writer.close()
        report.assert_called_once()
        self.assertEqual(str(report.call_args.args[0]), 'catalogue_closing none raised')
        self.assertTrue(writer._close_complete)
        self.assertFalse(writer._failed_closes)


class StartHookTests(ProjectWriterFixture):
    def service_writer(self, *, root=None, **hooks):
        writer = api_writer.StoreWriter(self.root if root is None else root,
                                       runtime_instance_id='r1')
        self.addCleanup(writer.close)
        writer.start(mode='service', **hooks)
        return writer

    @staticmethod
    def fail_ingest(_port):
        raise ValueError('ingest failed')

    def test_open_refused_while_catalogue_opens(self):
        entered, release = threading.Event(), threading.Event()
        open_catalogue = store_layout.open_catalogue

        def paused(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return open_catalogue(*args, **kwargs)

        with mock.patch.object(store_layout, 'open_catalogue', paused):
            writer = self.service_writer()
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(writer._started.is_set())
                # A missing gate must fail without waiting on the paused opener.
                with mock.patch.object(writer._queue, 'put',
                                       side_effect=AssertionError('queued before catalogue opened')):
                    self.assert_unavailable(lambda: writer.open_project(P), 'store_held')
                self.assertTrue(writer._queue.empty())
                self.assertEqual(writer._requests, {})
            finally:
                release.set()
            self.assertEqual(writer.wait_started().state, 'serving')

    def test_open_refused_while_hook_runs(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def hook(port):
            self.assertTrue(port.submit_declared(self.declared(
                lambda _: api_writer.JobOutcome(True, 'ingest'))).commit)
            calls.append(threading.current_thread().name)
            entered.set()
            self.assertTrue(release.wait(3))

        writer = self.service_writer(before_joins=hook)
        try:
            self.assertTrue(entered.wait(3))
            self.assert_unavailable(lambda: writer.open_project(P), 'store_held')
            self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)), 'store_held')
            self.assert_unavailable(lambda: writer.submit_callee(P, self.job(self.success)), 'store_held')
            self.assertEqual(writer._requests, {})
            self.assertEqual(writer.served_projects(), frozenset())
        finally:
            release.set()
        self.assertEqual(writer.wait_started().state, 'serving')
        self.assertEqual(calls, ['store-writer-start-hook'])

    def test_failed_first_ingest_holds(self):
        writer = self.service_writer(before_joins=self.fail_ingest)
        outcome = writer.wait_started()
        self.assertEqual((outcome.state, outcome.causes), ('held', ('ingest_fault',)))
        self.assertEqual(writer.served_projects(), frozenset())
        self.assert_unavailable(lambda: writer.open_project(P), 'store_held')

    def test_held_port_allows_catalogue_only(self):
        writer = self.service_writer(before_joins=self.fail_ingest)
        port = writer.wait_started().port
        self.assertIsNot(port, writer)
        self.assertEqual(port.read_catalogue(store_layout.open_stops), [])
        self.assertEqual(port.store_id(), writer.store_id())
        self.assertTrue(port.submit_declared(self.declared(
            lambda _: api_writer.JobOutcome(True, None))).commit)
        self.assert_unavailable(lambda: port.submit(P, self.job(self.success)), 'store_held')
        self.assert_unavailable(lambda: port.submit_declared(self.declared(lambda _: None, P)),
                                'store_held')
        self.assert_unavailable(lambda: port.request_project_start(P, self.principal), 'store_held')

    def test_hook_runs_once(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def hook(port):
            calls.append(port)
            entered.set()
            self.assertTrue(release.wait(3))

        writer = self.service_writer(before_joins=hook)
        with self.pool(2, release) as pool:
            self.assertTrue(entered.wait(3))
            first = pool.submit(writer.wait_started)
            second = pool.submit(writer.wait_started)
            release.set()
            self.assertEqual(first.result(2), second.result(2))
            self.assertEqual(first.result(2).state, 'serving')
        self.assertEqual(calls, [writer])
        with self.assertRaises(RuntimeError):
            writer.start(mode='service', before_joins=hook)

    def test_project_start_runs_callable_before_open(self):
        calls = []
        caller = threading.get_ident()

        def hook(port):
            calls.append(('hook', threading.get_ident(), self.stops(port)))
            self.assertTrue(port.submit_declared(self.declared(
                lambda _: api_writer.JobOutcome(True, None))).commit)

        writer = self.service_writer(before_project_join=hook)
        writer.wait_started()
        writer.open_project(P)
        self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
        original = writer._post_project_request

        def post(kind, project):
            if kind == 'open':
                calls.append(('post', project))
            original(kind, project)

        with mock.patch.object(writer, '_post_project_request', side_effect=post):
            self.assertEqual(writer.request_project_start(P, self.principal), 'started')
            self.flush(writer)
        self.assertEqual(calls, [('hook', caller, []), ('post', P)])
        self.assertIn(P, writer.served_projects())

    def test_project_start_callable_retries(self):
        seen = []

        def hook(port):
            seen.append(self.stops(port))
            if len(seen) == 1:
                raise ValueError('join did not run')

        writer = self.service_writer(before_project_join=hook)
        writer.wait_started()
        writer.open_project(P)
        self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
        self.assertEqual(writer.request_project_start(P, self.principal), 'started_not_served')
        self.assertNotIn(P, writer.served_projects())
        self.assertEqual(writer._project_admission[P], 'starting')
        self.assertEqual(writer.request_project_start(P, self.principal), 'started')
        self.flush(writer)
        self.assertEqual(seen, [[], []])
        self.assertIn(P, writer.served_projects())

    def test_project_start_in_held(self):
        self.row()
        writer = self.service_writer(before_joins=self.fail_ingest)
        self.assertEqual(writer.wait_started().state, 'held')
        self.assertEqual(writer.request_project_start(P, self.principal), 'started_not_served')
        self.flush(writer)
        self.assertEqual(self.stops(writer), [])
        self.assertNotIn(P, writer.served_projects())

    def test_waiters_end_closed(self):
        for state_change in (False, True):
            with self.subTest(state_change=state_change):
                entered, release, waiting = (threading.Event() for _ in range(3))

                def hook(_port):
                    entered.set()
                    self.assertTrue(release.wait(3))

                writer = self.service_writer(before_joins=None if state_change else hook)
                if state_change:
                    since = writer.wait_started()
                    def wait():
                        return writer.wait_state_change(since)
                else:
                    self.assertTrue(entered.wait(3))
                    wait = writer.wait_started
                original = writer._changed.wait

                def signal_wait(timeout=None):
                    waiting.set()
                    return original(timeout)

                with mock.patch.object(writer._changed, 'wait', side_effect=signal_wait):
                    with self.pool(2, release) as pool:
                        waiter = pool.submit(wait)
                        self.assertTrue(waiting.wait(2))
                        closing_writer = pool.submit(writer.close)
                        self.assert_unavailable(lambda: waiter.result(2), 'closed')
                        release.set()
                        closing_writer.result(2)

    def test_service_catalogue_open_error_wakes_waiters(self):
        calls = []
        writer = self.service_writer(root=Path(self.tmp.name) / 'missing',
                                     before_joins=calls.append)
        with self.pool(2) as pool:
            waiters = [pool.submit(writer.wait_started) for _ in range(2)]
            first, second = (waiter.result(2) for waiter in waiters)
            self.assertIs(first, second)
            self.assertIsInstance(first, api_writer.StartResult)
            self.assertEqual((first.state, first.causes), ('held', ('catalogue_unopenable',)))
            self.assertIsInstance(first.port, api_writer._FaultedPort)
            self.assertIsInstance(first.port.error, store_layout.LayoutFault)
            self.assertEqual(first.port.error.cause, 'missing')
            with self.assertRaises(api_writer.WriterUnavailable) as unavailable:
                first.port.store_id()
            self.assertEqual(unavailable.exception.reason, 'catalogue_unopenable')
            self.assertIs(unavailable.exception.__cause__, first.port.error)
        self.assertEqual(calls, [])

    def test_callables_need_service_mode(self):
        writer = api_writer.StoreWriter(self.root, runtime_instance_id='r1')
        self.addCleanup(writer.close)
        with self.assertRaises(ValueError):
            writer.start(before_joins=lambda _: None)
        self.assertIsNone(writer._worker)


class StartHookCustodyTests(ProjectWriterFixture):
    def blocked_writer(self):
        entered, release = threading.Event(), threading.Event()

        def hook(_port):
            entered.set()
            if not release.wait(2):
                raise AssertionError('start hook barrier not released')

        writer = api_writer.StoreWriter(self.root, runtime_instance_id='custody')
        self.addCleanup(writer.close)
        self.addCleanup(release.set)
        writer.start(mode='service', before_joins=hook)
        self.assertTrue(entered.wait(1))
        return writer, release

    def attempt(self, writer, *, work_after):
        now = time.monotonic()
        attempt = SimpleNamespace(active=now, work=now + work_after)
        writer.bind_service_owner(None, SimpleNamespace(attempt=attempt, admission_closed=True))
        self.assertTrue(writer.fence_for_stop(attempt))
        return attempt

    def test_start_hook_custody_at_stop(self):
        writer, release = self.blocked_writer()
        attempt = self.attempt(writer, work_after=0.1)
        self.assert_unavailable(writer.wait_started, 'closed')
        self.assertFalse(release.is_set())
        self.assertFalse(writer.settle_service_stop(attempt))
        self.assertTrue(writer._start_hook.is_alive())
        release.set()
        writer._start_hook.join(1)
        self.assertFalse(writer._start_hook.is_alive())
        self.assertFalse(writer.settle_service_stop(attempt))

    def test_start_hook_released_before_work_settles_true(self):
        writer, release = self.blocked_writer()
        attempt = self.attempt(writer, work_after=1)
        self.assert_unavailable(writer.wait_started, 'closed')
        release.set()
        self.assertTrue(writer.settle_service_stop(attempt))
        self.assertFalse(writer._worker.is_alive())
        self.assertFalse(writer._start_hook.is_alive())

    def test_settle_private_io_waits_start_hook(self):
        writer, release = self.blocked_writer()
        self.assertFalse(writer.settle_private_io(time.monotonic() + 0.1))
        self.assertTrue(writer._start_hook.is_alive())
        release.set()
        self.assertTrue(writer.settle_private_io(time.monotonic() + 1))
        self.assertFalse(writer._start_hook.is_alive())

    def test_close_joins_start_hook(self):
        writer, release = self.blocked_writer()
        started_close = threading.Event()
        close_request = writer._request_close

        def record_request():
            close_request()
            started_close.set()

        with mock.patch.object(writer, '_request_close', side_effect=record_request):
            with self.pool(1, release) as pool:
                closed = pool.submit(writer.close)
                self.assertTrue(started_close.wait(1))
                with self.assertRaises(TimeoutError):
                    closed.result(0.1)
                release.set()
                closed.result(1)
        self.assertFalse(writer._start_hook.is_alive())

    def test_no_start_hook_outside_service_mode(self):
        writer = self.writer(opened=False)
        self.assertIsNone(writer._start_hook)
        self.assertTrue(writer.settle_private_io(time.monotonic() + 1))


class JoinTests(ProjectWriterFixture):
    deny_rollback = WriterTests.deny_rollback

    def join_writer(self, *, hooks=None, join=None, phase=None):
        writer = api_writer.StoreWriter(self.root, runtime_instance_id='join',
                                       hooks=hooks, phase_hook=phase)
        self.addCleanup(writer.close)
        writer.start(mode='service', project_join=join)
        self.assertEqual(writer.wait_started().state, 'serving')
        return writer

    def wait_join(self, ticket, state, *, kind=None, cause=None):
        result = ticket.wait(time.monotonic() + 2)
        self.assertEqual(result.state, state)
        self.assertEqual(result.kind, kind)
        if cause is not None:
            self.assertEqual(result.cause, cause)
        return result

    def block(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def pause(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('join barrier not released')

        return entered, release, pause

    def test_hooks_called_on_worker_in_5c_order(self):
        seen = []
        hooks = mock.Mock()

        def record(name):
            return lambda *args, **kwargs: seen.append((name, threading.current_thread().name))

        hooks.ledger_opened.side_effect = record('opened')
        hooks.ledger_serving.side_effect = record('serving')
        hooks.ledger_closing.side_effect = record('closing')
        writer = self.join_writer(hooks=hooks, join=record('join'))
        self.wait_join(writer.join_project(P), 'served')
        hooks.ledger_closing.assert_not_called()
        writer.close()
        self.assertEqual(seen, [('opened', 'store-writer'), ('join', 'store-writer-join'),
                                ('serving', 'store-writer'), ('closing', 'store-writer')])
        control = self.join_writer()
        self.assertIsNone(control._join_thread)

    def test_joinport_has_only_three_operations(self):
        seen = []
        writer = self.join_writer(join=lambda port, _: seen.append(port))
        self.wait_join(writer.join_project(P), 'served')
        self.assertEqual({attr for attr in dir(seen[0]) if not attr.startswith('_')},
                         {'read', 'read_catalogue', 'submit'})

    def test_join_submit_commit_then_drain_order(self):
        seen = []
        hooks = mock.Mock()

        def join(port, project):
            self.assertEqual(project, P)
            self.assertNotIn(P, writer.served_projects())
            self.assert_unavailable(lambda: writer.submit(P, self.job(self.success)),
                                    'project_not_served')
            self.assertTrue(port.submit(self.job(self.success)).commit)
            def read_name(conn):
                with closing(conn.execute('SELECT name FROM projects')) as cursor:
                    return cursor.fetchone()[0]

            self.assertEqual(port.read(read_name), 'after')
            self.assertTrue(port.read_catalogue(lambda c: store_layout.route_state(c, P)))
            seen.append('drain')

        hooks.ledger_serving.side_effect = lambda _: seen.append('serving')
        writer = self.join_writer(hooks=hooks, join=join)
        self.wait_join(writer.join_project(P), 'served')
        self.assertEqual(seen, ['drain', 'serving'])

    def test_stale_joinport_refused_on_second_join(self):
        ports = []

        def join(port, project):
            if ports:
                self.assert_unavailable(lambda: ports[0].read(lambda _: None), 'project_not_served')
            ports.append(port)
            if len(ports) == 1:
                raise ValueError('first join failed')

        writer = self.join_writer(join=join)
        self.wait_join(writer.join_project(P), 'not_served')
        self.flush(writer)
        self.wait_join(writer.join_project(P), 'served')
        self.assertEqual(len(ports), 2)

    def test_joinport_revoked_on_serve(self):
        ports = []
        writer = self.join_writer(join=lambda port, _: ports.append(port))
        self.wait_join(writer.join_project(P), 'served')
        self.assertNotIn(P, writer._join_slot)
        self.assertNotIn(P, writer._joining)
        self.assert_unavailable(lambda: ports[0].read(lambda _: None), 'project_not_served')

    def test_ledger_opened_timeout_leaves_unserved(self):
        hooks = mock.Mock()
        hooks.ledger_opened.side_effect = TimeoutError()
        join = mock.Mock()
        writer = self.join_writer(hooks=hooks, join=join)
        self.wait_join(writer.join_project(P), 'not_served')
        writer.close()
        hooks.ledger_closing.assert_not_called()
        join.assert_not_called()
        self.assertNotIn(P, writer.served_projects())

    def test_join_outcome_seal_held(self):
        callback = mock.Mock(side_effect=lambda *args: self.raise_held('seal'))
        writer = self.join_writer(join=callback)
        self.wait_join(writer.join_project(P), 'held', kind='seal', cause='recorded')

    def test_join_outcome_other_held(self):
        callback = mock.Mock(side_effect=lambda *args: self.raise_held('other'))
        writer = self.join_writer(join=callback)
        self.wait_join(writer.join_project(P), 'held', kind='other', cause='recorded')

    @staticmethod
    def raise_held(kind):
        raise api_writer.ProjectHeld(P, kind, 'recorded')

    def test_join_outcome_ledger_serving_raise(self):
        hooks = mock.Mock()
        hooks.ledger_serving.side_effect = ValueError('injected')
        writer = self.join_writer(hooks=hooks)
        self.wait_join(writer.join_project(P), 'not_served')
        writer.close()
        hooks.ledger_closing.assert_called_once()

    def test_join_outcome_other_raise(self):
        writer = self.join_writer(join=mock.Mock(side_effect=ValueError('injected')))
        self.wait_join(writer.join_project(P), 'not_served')
        self.assertNotIn(P, writer.served_projects())

    def test_hold_on_published_binding_decided_before_close(self):
        hooks = mock.Mock()
        seen = []
        hooks.ledger_closing.side_effect = lambda *_args, **_kw: seen.append(
            (writer._held[P].kind, P in writer._joining))
        writer = self.join_writer(hooks=hooks, join=lambda *_: self.raise_held('seal'))
        self.wait_join(writer.join_project(P), 'held', kind='seal')
        writer.close()
        self.assertEqual(seen, [('seal', False)])

    def test_ledger_closing_on_every_long_lived_close(self):
        for path in ('stop', 'rollback', 'declared', 'finalizer', 'shutdown'):
            with self.subTest(path=path):
                hooks = mock.Mock()
                join = (lambda *_: self.raise_held('other')) if path == 'finalizer' else None
                writer = self.join_writer(hooks=hooks, join=join)
                self.wait_join(writer.join_project(P), 'held' if join else 'served',
                               kind='other' if join else None)
                if path == 'stop':
                    self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
                    self.flush(writer)
                    hooks.ledger_closing.assert_called_once()
                    with closing(store_layout.open_catalogue(self.root)) as conn:
                        store_layout.record_start(conn, 'project', P, by=self.principal)
                        conn.commit()
                elif path == 'rollback':
                    error = self.assert_unavailable(
                        lambda: writer.submit(P, self.job(lambda conn: WriterTests.rollback_refusal(self, conn))),
                        'rollback_failed')
                    self.assertIsNotNone(error.__cause__)
                    hooks.ledger_closing.assert_called_once()
                elif path == 'declared':
                    def retire(_):
                        writer._retire_declared_file(P, writer._connections[P], OSError('fault'))
                        return api_writer.JobOutcome(False, None)
                    with mock.patch.object(writer, '_run_declared', side_effect=retire):
                        writer.submit_declared(self.declared(lambda _: None))
                writer.close()
                self.assertTrue(hooks.ledger_closing.called)
                for call in hooks.ledger_closing.call_args_list:
                    self.assertEqual(call.args, (P,))
                    self.assertIs(call.kwargs['deadline'], writer._stop_deadline)

    def test_guarded_cleanup_record(self):
        writer = self.join_writer()
        failure = OSError('failed close')
        conn = mock.Mock()
        conn.close.side_effect = failure
        cleanup = api_writer.GuardedCleanup()
        writer._close_guarded_open(P, conn, store_layout.LayoutFault('incumbent-hold'), cleanup)
        self.assertIs(cleanup.failure, failure)
        self.assertIn(conn, writer._failed_closes)
        self.assertIs(writer._rollback_errors[P], failure)

    def test_guarded_refusal_finalizes(self):
        hooks = mock.Mock()
        writer = self.join_writer(hooks=hooks)
        with mock.patch.object(writer, '_open_join_handle', side_effect=store_layout.LayoutFault('missing')):
            self.wait_join(writer.join_project(P), 'not_served')
        writer.close()
        self.assertNotIn(P, writer._joining)
        hooks.ledger_opened.assert_not_called()

    def test_incumbent_hold_with_raising_close(self):
        hooks = mock.Mock()
        writer = self.join_writer(hooks=hooks)

        def refuse(project, cleanup):
            conn = mock.Mock()
            conn.close.side_effect = OSError('failed close')
            fault = store_layout.LayoutFault('incumbent-hold')
            writer._close_guarded_open(project, conn, fault, cleanup)
            raise fault

        with mock.patch.object(writer, '_open_join_handle', side_effect=refuse):
            self.wait_join(writer.join_project(P), 'not_served', cause='rollback_failed')
        hooks.ledger_opened.assert_not_called()
        self.assertNotIn(P, writer._held)
        self.assertIn(P, writer._rollback_errors)

    def test_restore_held_outcome(self):
        hooks = mock.Mock()
        join = mock.Mock()
        writer = self.join_writer(hooks=hooks, join=join)
        with mock.patch.object(writer, '_open_join_handle',
                               side_effect=api_writer.ProjectHeld(P, 'other', 'restore_held')):
            self.wait_join(writer.join_project(P), 'held', kind='other', cause='restore_held')
        join.assert_not_called()
        hooks.ledger_opened.assert_not_called()

    def test_failed_private_close_fences_project(self):
        hooks = mock.Mock()
        hooks.ledger_opened.side_effect = ValueError('failed open hook')
        writer = self.join_writer(hooks=hooks)
        original_close = writer._close_handle
        original_open = writer._open_join_handle
        private = []

        def capture(*args):
            opened = original_open(*args)
            private.append(opened[0].conn)
            return opened

        def fail_private(conn):
            original_close(conn)
            if private and conn is private[0]:
                writer._failed_closes.append(conn)
                raise OSError('failed close')

        with (mock.patch.object(writer, '_open_join_handle', side_effect=capture),
              mock.patch.object(writer, '_close_handle', side_effect=fail_private)):
            self.wait_join(writer.join_project(P), 'not_served')
            self.flush(writer)
        self.assertIn(P, writer._rollback_errors)
        hooks.ledger_closing.assert_not_called()

    def test_stop_after_revalidation(self):
        self.join_revoked_at('join_revalidated', by='stop')

    def test_stop_during_ledger_serving(self):
        self.join_revoked_at('ledger_serving', by='stop')

    def test_stop_during_ledger_opened(self):
        self.join_revoked_at('ledger_opened', by='stop')

    def test_end_hold_during_ledger_opened(self):
        self.join_revoked_at('ledger_opened', by='takeover')

    def join_revoked_at(self, where, *, by):
        entered, release, pause = self.block()
        hooks = mock.Mock()
        hook = mock.Mock()

        def phase(name):
            if name == where:
                pause()

        if where.startswith('ledger'):
            getattr(hooks, where).side_effect = pause
        writer = self.join_writer(hooks=hooks, join=hook, phase=phase)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            if where != 'ledger_serving':
                self.assertNotIn(P, writer._joining)
            if by == 'stop':
                attempt = CatalogueHookTests.attempt(self, writer)
                attempt.active = time.monotonic()
                self.assertTrue(writer.fence_for_stop(attempt))
            else:
                writer.end_hold(P)
        finally:
            release.set()
        self.wait_join(ticket, 'stopped' if by == 'stop' else 'not_served')
        writer.close()
        self.assertNotIn(P, writer._held)
        self.assertNotIn(P, writer._joining)
        self.assertNotIn(P, writer.served_projects())
        if where != 'ledger_serving':
            hook.assert_not_called()

    def test_restore_held_after_takeover_not_published(self):
        entered, release, pause = self.block()
        writer = self.join_writer()

        def restore(*_):
            pause()
            raise api_writer.ProjectHeld(P, 'other', 'restore_held')

        try:
            with mock.patch.object(writer, '_open_join_handle', side_effect=restore):
                ticket = writer.join_project(P)
                self.assertTrue(entered.wait(2))
                writer.end_hold(P)
                release.set()
                self.wait_join(ticket, 'not_served')
        finally:
            release.set()
        self.assertNotIn(P, writer._held)

    def test_end_hold_revokes_queued_submit(self):
        self.queued_join_revoked()

    def test_queued_join_request_released_after_revocation(self):
        self.queued_join_revoked()

    def queued_join_revoked(self):
        entered, release, pause = self.block()
        ran, reasons = [], []

        def join(port, _):
            try:
                port.submit(self.job(lambda conn: ran.append(conn)))
            except api_writer.WriterUnavailable as error:
                reasons.append(error.reason)

        writer = self.join_writer(join=join,
                                  phase=lambda name: pause() if name == 'join_port_queued' else None)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            writer.end_hold(P)
        finally:
            release.set()
        self.wait_join(ticket, 'not_served')
        self.assertEqual(ran, [])
        self.assertEqual(reasons, ['project_not_served'])

    def test_late_hold_after_stop_not_published(self):
        entered, release, pause = self.block()

        def join(*args):
            pause()
            self.raise_held('seal')

        writer = self.join_writer(join=join)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            attempt = CatalogueHookTests.attempt(self, writer)
            self.assertTrue(writer.fence_for_stop(attempt))
        finally:
            release.set()
        self.wait_join(ticket, 'stopped')
        self.assertNotIn(P, writer._held)

    def test_active_operation_continues_then_discarded(self):
        entered, release, pause = self.block()
        seen = []

        def run(conn):
            pause()
            seen.append('ran')
            return self.success(conn)

        def join(port, _):
            seen.append(port.submit(self.job(run)).commit)
            self.raise_held('seal')

        writer = self.join_writer(join=join)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            writer.end_hold(P)
        finally:
            release.set()
        self.wait_join(ticket, 'not_served')
        self.assertEqual(seen, ['ran', True])
        self.assertNotIn(P, writer._held)

    def test_takeover_without_stop_not_incomplete(self):
        entered, release, pause = self.block()
        writer = self.join_writer(join=pause)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            writer.end_hold(P)
        finally:
            release.set()
        self.wait_join(ticket, 'not_served')
        self.assertTrue(writer.settle_private_io(time.monotonic() + 2))

    def test_join_route_creating_not_eligible(self):
        join = mock.Mock()
        writer = self.join_writer(join=join)
        with closing(store_layout.open_catalogue(self.root)) as conn:
            with closing(conn.execute(
                    "UPDATE project_routes SET state='creating' WHERE project_id=?", (P,))) as cursor:
                self.assertEqual(cursor.rowcount, 1)
            conn.commit()

        self.wait_join(writer.join_project(P), 'not_served')
        join.assert_not_called()
        self.assertNotIn(P, writer.served_projects())
        self.assertNotIn(P, writer._join_slot)

    def test_join_ticket_outcomes(self):
        entered, release, pause = self.block()
        writer = self.join_writer(join=pause)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            self.assertIs(writer.join_project(P), ticket)
            self.assertEqual(ticket.wait(time.monotonic()).state, 'pending')
        finally:
            release.set()
        self.wait_join(ticket, 'served')

    def test_lazy_join_thread_start_fenced(self):
        writer = self.join_writer()
        self.assertIsNone(writer._join_thread)
        attempt = CatalogueHookTests.attempt(self, writer)
        self.assertTrue(writer.fence_for_stop(attempt))
        self.wait_join(writer.join_project(P), 'stopped')
        self.assertIsNone(writer._join_thread)

        control = self.join_writer()
        registered = []
        start = threading.Thread.start

        def record_start(thread):
            if thread.name == 'store-writer-join':
                registered.append((control._lock._is_owned(), control._join_thread is thread))
            return start(thread)

        with mock.patch.object(threading.Thread, 'start', record_start):
            self.wait_join(control.join_project(P), 'served')
        self.assertEqual(registered, [(True, True)])

    def test_join_thread_custody(self):
        thread_observations = []

        def join(*args):
            thread_observations.append(threading.current_thread())

        writer = self.join_writer(join=join)
        self.wait_join(writer.join_project(P), 'served')
        self.assertEqual(thread_observations, [writer._join_thread])
        writer.close()
        self.assertFalse(thread_observations[0].is_alive())

    def test_writer_settle_false_with_join_blocked_at_work(self):
        entered, release, pause = self.block()
        writer = self.join_writer(join=pause)
        try:
            writer.join_project(P)
            self.assertTrue(entered.wait(2))
            attempt = CatalogueHookTests.attempt(self, writer)
            attempt.work = time.monotonic() + 0.03
            writer.fence_for_stop(attempt)
            self.assertFalse(writer.settle_service_stop(attempt))
        finally:
            release.set()
        writer.close()
        self.assertFalse(writer.settle_service_stop(attempt))

    def test_stop_deadline_reaches_every_close_after_fence(self):
        hooks = mock.Mock()
        writer = self.join_writer(hooks=hooks)
        self.wait_join(writer.join_project(P), 'served')
        attempt = CatalogueHookTests.attempt(self, writer)
        self.assertTrue(writer.fence_for_stop(attempt))
        self.assertTrue(writer.settle_service_stop(attempt))
        hooks.ledger_closing.assert_called_once_with(P, deadline=writer._stop_deadline)
        hooks.catalogue_closing.assert_called_once_with(deadline=writer._stop_deadline)
        self.assertEqual(writer._stop_deadline.at(), attempt.active)

    def test_request_project_start_unconfigured_unchanged(self):
        self.row()
        writer = self.join_writer()
        with mock.patch.object(writer, 'join_project', create=True) as join:
            self.assertEqual(writer.request_project_start(P, self.principal), 'started')
            self.flush(writer)
            join.assert_not_called()
        self.assertIn(P, writer.served_projects())


class JoinRetryTests(ProjectWriterFixture):
    join_writer = JoinTests.join_writer
    block = JoinTests.block
    raise_held = staticmethod(JoinTests.raise_held)

    def test_seal_cleared_rejoins_seal_held_project_off_worker(self):
        threads = []
        rejoined = threading.Event()

        def join(*_):
            threads.append(threading.current_thread().name)
            if len(threads) == 1:
                self.raise_held('seal')
            rejoined.set()

        writer = self.join_writer(join=join)
        self.wait_join(writer.join_project(P), 'held', kind='seal')
        self.flush(writer)
        writer.seal_cleared(P, 'ignored')
        self.assertTrue(rejoined.wait(2))
        self.wait_join(writer.join_project(P), 'served')
        self.assertEqual(threads, ['store-writer-join'] * 2)

    def test_seal_cleared_ignores_other_holds(self):
        join = mock.Mock(side_effect=lambda *_: self.raise_held('other'))
        writer = self.join_writer(join=join)
        self.wait_join(writer.join_project(P), 'held', kind='other')
        self.flush(writer)
        writer.seal_cleared(P, 'irrelevant')
        self.wait_join(writer.join_project(P), 'held', kind='other')
        self.assertEqual(join.call_count, 1)

    def clear_while_running(self, boundary, *, takeover=False, clears=1, blocked_close=False):
        entered, release, pause = self.block()
        closing, clean, pause_close = self.block()
        calls = []
        hooks = mock.Mock()

        def phase(name):
            if name == 'join_revalidated' and boundary == name and not entered.is_set():
                pause()

        def opened(_project):
            if boundary == 'ledger_opened' and not entered.is_set():
                pause()

        def close(*_, **_kw):
            if blocked_close and not closing.is_set():
                pause_close()

        def join(*_):
            calls.append(len(calls))
            if len(calls) == 1:
                if boundary == 'project_join':
                    pause()
                self.raise_held('seal')

        hooks.ledger_opened.side_effect = opened
        hooks.ledger_closing.side_effect = close
        writer = self.join_writer(hooks=hooks, join=join, phase=phase)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            for _ in range(clears):
                writer.seal_cleared(P, 'recorded')
            if takeover:
                writer.end_hold(P)
            release.set()
            if blocked_close:
                self.assertTrue(closing.wait(2))
                self.wait_join(ticket, 'not_served' if takeover else 'pending', seconds=0.02)
                self.assertEqual(calls, [0])
                clean.set()
            self.wait_join(ticket, 'not_served' if takeover else 'served')
        finally:
            release.set()
            clean.set()
        self.assertEqual(calls, [0] if takeover else [0, 1])
        self.assertNotIn(P, writer._held)

    def retry_at_ending(self, *, takeover=False, fail_close=False):
        entered, release, pause = self.block()
        hooks = mock.Mock()
        hooks.ledger_closing.side_effect = pause
        calls = []

        def join(*_):
            calls.append(1)
            if len(calls) == 1:
                self.raise_held('seal')

        writer = self.join_writer(hooks=hooks, join=join)
        close_handle = writer._close_handle

        def close(conn):
            close_handle(conn)
            if fail_close and not writer._rollback_errors and entered.is_set():
                writer._failed_closes.append(conn)
                raise OSError('unresolved join close')

        try:
            with (mock.patch.object(writer, '_close_handle', side_effect=close),
                  mock.patch.object(writer, '_queue_join', wraps=writer._queue_join) as queued):
                self.wait_join(writer.join_project(P), 'held', kind='seal')
                self.assertTrue(entered.wait(2))
                writer.seal_cleared(P, 'recorded')
                if takeover:
                    writer.end_hold(P)
                self.assertNotIn(P, writer._held)
                self.assertEqual(calls, [1])
                release.set()
                self.flush(writer)
                if fail_close:
                    self.assertEqual(queued.call_count, 1)
                if not takeover and not fail_close:
                    self.wait_join(writer.join_project(P), 'served')
        finally:
            release.set()
        self.assertEqual(calls, [1] if takeover or fail_close else [1, 1])
        if fail_close:
            self.assertIn(P, writer._rollback_errors)
            self.assertNotIn(P, writer._join_slot)

    def wait_join(self, ticket, state, *, kind=None, cause=None, seconds=2):
        result = ticket.wait(time.monotonic() + seconds)
        self.assertEqual((result.state, result.kind), (state, kind))
        if cause is not None:
            self.assertEqual(result.cause, cause)
        return result

    def test_clear_at_each_boundary(self):
        for boundary in ('join_revalidated', 'ledger_opened', 'project_join'):
            with self.subTest(boundary=boundary):
                self.clear_while_running(boundary)

    def test_clear_after_hold_with_cleanup_blocked(self):
        self.retry_at_ending()

    def test_clear_between_decision_and_cleanup(self):
        entered, release, pause = self.block()
        calls = []

        def join(*_):
            calls.append(1)
            if len(calls) == 1:
                self.raise_held('seal')

        writer = self.join_writer(join=join)
        close = writer._close_ledger

        def close_binding(*args, **kwargs):
            if not entered.is_set():
                pause()
            return close(*args, **kwargs)

        try:
            with mock.patch.object(writer, '_close_ledger', side_effect=close_binding):
                self.wait_join(writer.join_project(P), 'held', kind='seal')
                self.assertTrue(entered.wait(2))
                writer.seal_cleared(P, 'irrelevant')
                self.assertEqual(calls, [1])
                release.set()
                self.wait_join(writer.join_project(P), 'served')
        finally:
            release.set()
        self.assertEqual(calls, [1, 1])

    def test_clear_then_takeover_during_running_join(self):
        self.clear_while_running('project_join', takeover=True, blocked_close=True)

    def test_one_successor_after_cleanup(self):
        self.clear_while_running('project_join', clears=4, blocked_close=True)

    def test_takeover_after_retry_queued(self):
        self.retry_at_ending(takeover=True)

    def test_no_promotion_while_handle_unresolved(self):
        self.retry_at_ending(fail_close=True)

    def release_at(self, boundary, *, stop=False):
        entered, release, pause = self.block()
        hooks = mock.Mock()
        calls = []
        released = threading.Event()

        def once(*args, **kwargs):
            if not entered.is_set():
                pause(*args, **kwargs)

        if boundary == 'ending':
            hooks.ledger_closing.side_effect = once
        elif boundary == 'opened':
            hooks.ledger_opened.side_effect = once

        def join(*_):
            calls.append(1)
            if released.is_set():
                return
            if boundary == 'running':
                once()
                return
            self.raise_held('other')

        writer = self.join_writer(hooks=hooks, join=join)
        try:
            ticket = writer.join_project(P)
            self.assertTrue(entered.wait(2))
            writer.end_hold(P, rejoin=True)
            released.set()
            if stop:
                self.assertEqual(writer.request_project_stop(P, self.principal), 'stopped')
                self.assertNotIn(P, writer._release_intent)
            release.set()
            self.wait_join(ticket, 'held' if boundary == 'ending' else 'stopped' if stop else 'not_served',
                           kind='other' if boundary == 'ending' else None)
            self.flush(writer)
            if stop:
                self.assertNotIn(P, writer._join_slot)
                self.assertNotIn(P, writer._release_intent)
                self.assertEqual(writer.request_project_start(P, self.principal), 'started')
            self.wait_join(writer.join_project(P), 'served')
        finally:
            release.set()
        self.assertEqual(len(calls), 1 if boundary == 'opened' else 2)

    def test_release_intent_running(self):
        self.release_at('running')

    def test_release_intent_ending(self):
        self.release_at('ending')

    def test_release_intent_project_stop_reopen(self):
        self.release_at('running', stop=True)

    def test_release_during_ledger_opened(self):
        self.release_at('opened')

    def test_successor_carries_epochs(self):
        writer = self.join_writer()
        real = writer._run_join
        checked = threading.Event()
        seen = []

        def intercept(slot):
            if slot.issued_epochs is not None:
                with writer._lock:
                    writer._hold_epochs[P] += 1
                real(slot)
                seen.append(slot.ticket.wait(time.monotonic()).state)
                checked.set()
            else:
                real(slot)

        with mock.patch.object(writer, '_run_join', side_effect=intercept):
            writer.end_hold(P, rejoin=True)
            self.assertTrue(checked.wait(2))
        self.assertEqual(seen, ['not_served'])
        self.assertNotIn(P, writer.served_projects())


class GuardedWriterFixture(unittest.TestCase):
    def setUp(self):
        from uuid import uuid4
        from relay_engine.tests.test_store_lifecycle import (
            _INCUMBENT_STORE_ID, _incumbent_birth, _incumbent_boot,
            _materialize_incumbent_store,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'store'
        self.guard, birth = store_layout.IncumbentGuard._create_new(
            self.root, _INCUMBENT_STORE_ID, P)
        self.addCleanup(self.guard.close)
        idle, _ = self.guard.enroll_new(birth)
        owner = store_layout._IncumbentOwner(
            str(uuid4()), os.getpid(), _incumbent_birth(os.getpid()), _incumbent_boot(),
            'fixture-runtime', 'off')
        self.arm = self.guard.arm(idle, owner)
        _materialize_incumbent_store(self.root)
        self.writer = api_writer.StoreWriter(self.root, runtime_instance_id='fixture-runtime')
        self.writer.bind_store_arm(self.guard, self.arm, lambda: False,
                                   self.guard._admit_members(self.arm))
        self.writer.start()
        self.addCleanup(self.writer.close)
        self.writer.open_project(P)
        self.catalogue_conn = store_layout.open_catalogue(self.root)
        self.addCleanup(self.catalogue_conn.close)

    def admit_second(self):
        with closing(store_layout.open_ledger(self.root, Q, create=True)) as conn:
            conn.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'second', 1)",
                         (Q,)).close()
            conn.commit()
        store_layout.ledger_path(self.root, Q).chmod(0o600)
        store_layout.reserve_project(self.catalogue_conn, Q, 'second', 1)
        store_layout.activate_project(self.catalogue_conn, Q)
        self.catalogue_conn.commit()

    def fault(self, cause, fn):
        with self.assertRaises(store_layout.LayoutFault) as error:
            fn()
        self.assertEqual(error.exception.cause, cause)
        return error.exception

    def unavailable(self, reason, fn):
        with self.assertRaises(api_writer.WriterUnavailable) as error:
            fn()
        self.assertEqual(error.exception.reason, reason)
        return error.exception

    @contextmanager
    def ledger_connects(self, project, *, swap_on_first=False, fail_close_once=False):
        path = store_layout.ledger_path(self.root, project)
        handles, closed = [], []
        real_connect = sqlite3.connect

        class Probe(sqlite3.Connection):
            def close(self):
                if fail_close_once and not getattr(self, 'close_attempted', False):
                    self.close_attempted = True
                    raise sqlite3.OperationalError('fixture close failed')
                super().close()
                closed.append(self)

        def connect(database, *args, **kwargs):
            if os.fspath(database) not in (str(path), path.as_uri() + '?mode=rw'):
                return real_connect(database, *args, **kwargs)
            conn = real_connect(database, *args, factory=Probe, check_same_thread=False, **kwargs)
            handles.append(conn)
            if swap_on_first and len(handles) == 1:
                replacement = path.with_suffix('.replacement')
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(0o600)
                replacement.replace(path)
            return conn

        def retire_test_handles():
            self.writer.close()
            retained = store_layout._UNOWNED_CLOSES
            for entry in tuple(retained):
                if entry[1] in handles:
                    retained.remove(entry)
            for conn in handles:
                if conn not in closed:
                    if fail_close_once and not getattr(conn, 'close_attempted', False):
                        conn.close_attempted = True
                    conn.close()

        self.addCleanup(retire_test_handles)
        with mock.patch.object(sqlite3, 'connect', side_effect=connect):
            yield handles, closed


class GuardedOpenTests(GuardedWriterFixture):
    def test_second_admitted_project_opens(self):
        self.admit_second()
        self.writer.open_project(Q)
        self.assertIn(Q, self.writer.served_projects())

    def test_project_with_wal_stop_row_refused_join(self):
        self.admit_second()
        self.catalogue_conn.execute('PRAGMA wal_autocheckpoint=0').close()
        with closing(self.catalogue_conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')) as cursor:
            self.assertEqual(cursor.fetchone()[0], 0)
        store_layout.record_stop(self.catalogue_conn, 'project', Q, 'test')
        self.catalogue_conn.commit()
        with closing(store_layout._immutable_incumbent_read(
                store_layout.catalogue_path(self.root))) as snapshot:
            self.assertFalse(any(s.project_id == Q for s in store_layout.open_stops(snapshot)))
        with self.ledger_connects(Q) as (handles, _):
            self.fault('incumbent-hold', lambda: self.writer.open_project(Q))
            self.assertEqual(handles, [])
        self.assertNotIn(Q, self.writer.served_projects())

    def test_hold_refuses_open(self):
        self.admit_second()
        job_id = '019a7850-0000-7000-8000-000000000001'
        store_layout.take_holds(self.catalogue_conn, job_id, 'cutover',
                                [store_layout.project_scope(Q)], 1)
        self.catalogue_conn.commit()
        with self.ledger_connects(Q) as (handles, _):
            self.fault('incumbent-hold', lambda: self.writer.open_project(Q))
            self.assertEqual(handles, [])

    def test_swap_after_proof_refused_and_closed(self):
        self.admit_second()
        with self.ledger_connects(Q, swap_on_first=True) as (handles, closed):
            self.fault('member-changed', lambda: self.writer.open_project(Q))
            self.assertEqual(len(handles), 1)
            self.assertEqual(closed, handles)
            with self.assertRaises(sqlite3.ProgrammingError):
                handles[0].execute('SELECT 1').close()
        self.assertNotIn(Q, self.writer.served_projects())

    def test_failed_cleanup_close_blocks_reopen(self):
        self.admit_second()
        with self.ledger_connects(Q, swap_on_first=True, fail_close_once=True) as (handles, _):
            self.fault('member-changed', lambda: self.writer.open_project(Q))
            error = self.unavailable('rollback_failed', lambda: self.writer.open_project(Q))
            self.assertIsInstance(error.__cause__, sqlite3.OperationalError)
            self.assertEqual(len(handles), 1)

    def test_unowned_close_refuses_open(self):
        self.admit_second()
        real_open = store_layout.open_ledger_runtime

        def refused_handoff(*args, **kwargs):
            def reject(_conn, _error):
                raise RuntimeError('handoff before recording')
            return real_open(*args, **dict(kwargs, on_failed_close=reject))

        with (self.ledger_connects(Q, swap_on_first=True, fail_close_once=True) as (handles, _),
              mock.patch.object(store_layout, 'open_ledger_runtime', side_effect=refused_handoff)):
            self.fault('member-changed', lambda: self.writer.open_project(Q))
            self.assertNotIn(Q, self.writer._rollback_errors)
            with self.assertRaises(api_writer.WriterUnavailable) as error:
                self.writer.open_project(Q)
            self.assertEqual(len(handles), 1)
            self.assertEqual(error.exception.reason, 'rollback_failed')
            self.assertIsInstance(error.exception.__cause__, store_layout.LayoutFault)
            self.assertEqual(error.exception.__cause__.cause, 'close-unresolved')

    def test_wrong_project_refusal_removed(self):
        self.admit_second()
        with self.ledger_connects(Q) as (handles, _):
            self.writer.open_project(Q)
            self.assertEqual(len(handles), 1)
            self.assertEqual(self.writer.served_projects(), frozenset({P, Q}))


class StoppedReadTests(GuardedWriterFixture):
    def read_set(self, project=P):
        return store_steps.Declared('read', {project: 'read'})

    def flush(self):
        self.writer.submit_declared(api_writer.DeclaredJob(
            store_steps.Declared('written', {}), lambda _: api_writer.JobOutcome(False, None)))

    def stop(self):
        self.assertEqual(self.writer.request_project_stop(P, 'test'), 'stopped')
        self.flush()
        self.assertNotIn(P, self.writer._served)

    def pause_stop_close(self, entered, release, *, fail=False):
        return ProjectWriterFixture.pause_close(self, self.writer, entered, release, fail=fail)

    def test_read_refuses_writes(self):
        read = self.writer.read_declared
        with tempfile.TemporaryDirectory() as tmp:
            isolated = Path(tmp) / 'isolated'
            with closing(store_layout.open_catalogue(isolated, kind='isolated')):
                pass
            with closing(store_layout.open_ledger(isolated, P, create=True)) as conn:
                conn.execute('INSERT INTO projects (id, name, created_at) VALUES (?, ?, 1)',
                             (P, 'test')).close()
                conn.commit()
            with closing(api_writer.StoreWriter(isolated, runtime_instance_id='test')) as served:
                served.start()
                serve_for_test(served, P)
                self.check_readonly(served.read_declared)
        self.stop()
        self.check_readonly(read)

    def check_readonly(self, read):
        for label in ('catalogue', P):
            with self.subTest(label=label):
                def try_insert(conns):
                    self.assertEqual(list(conns), ['catalogue', P])
                    self.assertTrue(all(conn.in_transaction for conn in conns.values()))
                    if label == 'catalogue':
                        conns[label].execute('INSERT INTO project_routes '
                                             '(project_id,name,state,created_at) '
                                             "VALUES (?, 'probe', 'active', 1)", (Q,)).close()
                    else:
                        conns[label].execute('INSERT INTO projects (id,name,created_at) '
                                             "VALUES (?, 'probe', 1)", (Q,)).close()

                with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                    read(self.read_set(), try_insert)

        def check(conns):
            for label, table, col in (('catalogue', 'project_routes', 'project_id'),
                                      (P, 'projects', 'id')):
                with closing(conns[label].execute(
                        f'SELECT count(*) FROM {table} WHERE {col}=?', (Q,))) as cursor:
                    self.assertEqual(cursor.fetchone(), (0,))

        read(self.read_set(), check)

    def test_read_after_fence_before_project_stop_refused(self):
        read = self.writer.read_declared
        entered, release = threading.Event(), threading.Event()
        actual = self.writer._record_project

        def pause(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return actual(*args)

        with mock.patch.object(self.writer, '_record_project', side_effect=pause):
            with ProjectWriterFixture.pool(self, 1, release) as pool:
                stop = pool.submit(self.writer.request_project_stop, P, 'test')
                self.assertTrue(entered.wait(3))
                self.unavailable('project_not_served', lambda: read(self.read_set(), self.unexpected))
                release.set()
                self.assertEqual(stop.result(3), 'stopped')

    def test_stopped_read_refused_when_served_again(self):
        read = self.writer.read_declared
        self.stop()
        before_worker, resume = threading.Event(), threading.Event()

        def phase(name):
            if name == 'dequeued':
                before_worker.set()
                self.assertTrue(resume.wait(3))

        self.writer._phase_hook = phase
        with ProjectWriterFixture.pool(self, 1, resume) as pool:
            pending = pool.submit(read, self.read_set(), lambda _: 'ran')
            self.assertTrue(before_worker.wait(3))
            # Change the routing signal at the worker boundary while keeping the
            # stop row readable, so the worker's reclassification is decisive.
            with self.writer._lock:
                self.writer._served.add(P)
            resume.set()
            self.unavailable('project_not_served', lambda: pending.result(3))

    def unexpected(self, _):
        self.fail('refused read ran')

    def test_read_of_other_project_refused(self):
        read = self.writer.read_declared
        with self.ledger_connects(Q) as (handles, _):
            self.unavailable('project_not_served', lambda: read(self.read_set(Q), self.unexpected))
            self.assertFalse(handles)

    def test_stopped_read_needs_stop_row(self):
        read = self.writer.read_declared
        with self.writer._lock:
            self.writer._project_admission[Q] = 'stopping'
        self.admit_second()
        with self.ledger_connects(Q) as (handles, _):
            self.unavailable('project_not_served', lambda: read(self.read_set(Q), self.unexpected))
            self.assertFalse(handles)
        store_layout.record_stop(self.catalogue_conn, 'project', P, 'test')
        self.catalogue_conn.commit()
        with mock.patch.object(store_layout, 'record_stop', side_effect=ValueError('no write')):
            self.assertEqual(self.writer.request_project_stop(P, 'test'), 'already_stopped')
        self.flush()
        self.assertNotIn(P, self.writer._served)
        self.assertEqual(read(self.read_set(), lambda conns: tuple(conns)), ('catalogue', P))

    def test_no_transient_handle_left(self):
        read = self.writer.read_declared
        def capture(conns):
            return self.writer._active_request, conns[P]

        served, _ = read(self.read_set(), capture)
        self.assertIsNone(served.transient)
        self.stop()
        with self.ledger_connects(P) as (handles, closed):
            stopped, conn = read(self.read_set(), capture)
            self.assertEqual(handles, [conn])
            self.assertEqual(closed, handles)
            self.assertIsNone(stopped.transient)

    def test_transient_close_failure_blocks_reopen(self):
        read = self.writer.read_declared
        self.stop()
        with self.ledger_connects(P, fail_close_once=True) as (handles, _):
            error = self.unavailable('rollback_failed', lambda: read(self.read_set(), lambda _: 73))
            self.assertIsInstance(error.__cause__, sqlite3.OperationalError)
            self.unavailable('rollback_failed', lambda: read(self.read_set(), self.unexpected))
            self.unavailable('rollback_failed', lambda: self.writer.open_project(P))
            self.assertEqual(len(handles), 1)

    def test_stopped_read_during_project_stop_close(self):
        read = self.writer.read_declared
        entered, release = threading.Event(), threading.Event()
        with (self.pause_stop_close(entered, release),
              self.ledger_connects(P) as (handles, _)):
            try:
                self.assertEqual(self.writer.request_project_stop(P, 'test'), 'stopped')
                self.assertTrue(entered.wait(3))
                self.unavailable('project_not_served', lambda: read(self.read_set(), self.unexpected))
                self.assertFalse(handles)
            finally:
                release.set()

    def test_fence_reaches_transient_handle(self):
        from time import monotonic
        from types import SimpleNamespace

        read = self.writer.read_declared
        self.stop()
        querying, interrupted = threading.Event(), threading.Event()
        handles = []

        def work(conns):
            conn = conns[P]
            handles.append(conn)

            def progress():
                querying.set()
                self.assertTrue(interrupted.wait(3))
                return 0

            conn.set_progress_handler(progress, 1)
            try:
                with closing(conn.execute('SELECT id FROM projects')) as cursor:
                    return cursor.fetchall()
            finally:
                conn.set_progress_handler(None, 0)

        real_interrupt = self.writer._interrupt

        def interrupt(conns):
            try:
                self.assertIn(handles[0], conns)
                real_interrupt(conns)
            finally:
                interrupted.set()

        with mock.patch.object(self.writer, '_interrupt', side_effect=interrupt):
            with ProjectWriterFixture.pool(self, 1, interrupted) as pool:
                pending = pool.submit(read, self.read_set(), work)
                self.assertTrue(querying.wait(3))
                now = monotonic()
                attempt = SimpleNamespace(active=now + 2, work=now + 2.5)
                self.assertTrue(self.writer.fence_for_stop(attempt))
                with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                    pending.result(max(0, attempt.work - monotonic()))
                self.assertLess(monotonic(), attempt.work)

    def test_unowned_close_refuses_stopped_read(self):
        read = self.writer.read_declared
        self.stop()
        real_open = store_layout.open_ledger_runtime

        def refused_handoff(*args, **kwargs):
            def reject(_conn, _error):
                raise RuntimeError('handoff before recording')
            return real_open(*args, **dict(kwargs, on_failed_close=reject))

        with (self.ledger_connects(P, swap_on_first=True, fail_close_once=True) as (handles, _),
              mock.patch.object(store_layout, 'open_ledger_runtime', side_effect=refused_handoff)):
            self.unavailable('project_not_served', lambda: read(self.read_set(), self.unexpected))
            self.assertNotIn(P, self.writer._rollback_errors)
            try:
                error = self.unavailable('rollback_failed', lambda: read(self.read_set(), self.unexpected))
            finally:
                self.assertEqual(len(handles), 1)
            self.assertIsInstance(error.__cause__, store_layout.LayoutFault)
            self.assertEqual(error.__cause__.cause, 'close-unresolved')

    def test_failed_project_stop_close_refuses_read(self):
        read = self.writer.read_declared
        entered, release = threading.Event(), threading.Event()
        old = self.writer._connections[P]
        with (self.pause_stop_close(entered, release, fail=True),
              self.ledger_connects(P) as (handles, _)):
            try:
                self.assertEqual(self.writer.request_project_stop(P, 'test'), 'stopped')
                self.assertTrue(entered.wait(3))
            finally:
                release.set()
            self.flush()
            self.assertIn(P, self.writer._closing_projects)
            self.assertIs(self.writer._connections[P], old)
            self.unavailable('rollback_failed', lambda: read(self.read_set(), self.unexpected))
            self.assertFalse(handles)

    def test_stopped_admission_refusal_not_served(self):
        read = self.writer.read_declared
        with self.writer._lock:
            self.writer._project_admission[P] = 'stopping'
            self.writer._post_project_stop(P)
        self.flush()
        self.assertNotIn(P, self.writer._served)
        with self.ledger_connects(P) as (handles, _):
            error = self.unavailable('project_not_served', lambda: read(self.read_set(), self.unexpected))
            self.assertIsInstance(error.__cause__, store_layout.LayoutFault)
            self.assertFalse(handles)

    def test_queued_declared_read_not_rerouted(self):
        read = self.writer.read_declared
        busy, queued, finish = (threading.Event() for _ in range(3))

        def block(_):
            busy.set()
            self.assertTrue(finish.wait(3))
            return api_writer.JobOutcome(False, None)

        def phase(name):
            if name == 'queued' and busy.is_set():
                queued.set()

        with ProjectWriterFixture.pool(self, 3, finish) as pool:
            turn = api_writer.DeclaredJob(store_steps.Declared('written', {}), block)
            active = pool.submit(self.writer.submit_declared, turn)
            self.assertTrue(busy.wait(3))
            self.writer._phase_hook = phase
            pending = pool.submit(read, self.read_set(), self.unexpected)
            self.assertTrue(queued.wait(3))
            with mock.patch.object(store_layout, 'record_stop', side_effect=ValueError('no write')):
                stop = pool.submit(self.writer.request_project_stop, P, 'test')
                self.unavailable('project_not_served', lambda: pending.result(2))
                finish.set()
                active.result(3)
                self.assertEqual(stop.result(3), 'not_recorded')
            self.assertIn(P, self.writer.served_projects())
            self.assertEqual(read(self.read_set(), lambda _: 73), 73)


class TransferModeTests(ProjectWriterFixture):
    def transfer(self, writer, run, sender=P):
        submit_transfer = writer.submit_transfer
        return submit_transfer(api_writer.TransferJob(sender, run))

    def write(self, turn, value='after'):
        turn.open_sender_writes()
        self.change(turn.sender, value)
        return api_writer.JobOutcome(True, value)

    def probe_write(self, turn):
        turn.catalogue.execute("INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, 'probe', 'active', 1)",
                               ('cccccccc-cccc-4ccc-8ccc-cccccccccccc',)).close()

    @staticmethod
    def make_transfer_constraint(conn):
        conn.execute('CREATE TABLE commit_parent (id INTEGER PRIMARY KEY)').close()
        conn.execute(
            'CREATE TABLE commit_child (parent INTEGER REFERENCES commit_parent(id) '
            'DEFERRABLE INITIALLY DEFERRED)'
        ).close()
        return api_writer.JobOutcome(True, None)

    def seed_commit_constraint(self, writer, *, catalogue=False):
        if catalogue:
            outcome = writer.submit_declared(self.declared(
                lambda conns: self.make_transfer_constraint(conns['catalogue'])))
        else:
            outcome = writer.submit(P, self.job(self.make_transfer_constraint))
        self.assertEqual(outcome, api_writer.JobOutcome(True, None))

    @staticmethod
    def write_deferred_violation(conn):
        conn.execute('INSERT INTO commit_child VALUES (42)').close()
        return api_writer.JobOutcome(True, None)

    def transfer_deferred_violation(self, turn):
        turn.open_sender_writes()
        return self.write_deferred_violation(turn.sender)

    def assert_commit_constraint(self, outcome, fault, *, indeterminate):
        self.assertEqual(outcome, api_writer.JobOutcome(
            False, outcome.value, indeterminate, fault))
        self.assertIsInstance(outcome.value, sqlite3.IntegrityError)
        self.assertEqual(outcome.value.sqlite_errorname, 'SQLITE_CONSTRAINT_FOREIGNKEY')

    def assert_no_deferred_row(self, *, catalogue=False):
        path = (store_layout.catalogue_path(self.root) if catalogue
                else store_layout.ledger_path(self.root, P))
        with closing(sqlite3.connect(path)) as conn, closing(
                conn.execute('SELECT COUNT(*) FROM commit_child')) as cursor:
            self.assertEqual(cursor.fetchone(), (0,))

    def test_transfer_deferred_constraint_commit_is_commit_failed(self):
        writer = self.writer()
        self.seed_commit_constraint(writer)
        outcome = self.transfer(writer, self.transfer_deferred_violation)
        self.assert_no_deferred_row()
        self.assert_commit_constraint(outcome, 'commit_failed', indeterminate=False)

    def test_transfer_commit_effect_then_raise_is_commit_unknown(self):
        writer = self.writer()
        real_commit = api_writer._commit_sender
        error = sqlite3.IntegrityError('observed after COMMIT')

        def commit_then_raise(conn):
            real_commit(conn)
            raise error

        with mock.patch.object(api_writer, '_commit_sender', side_effect=commit_then_raise) as commit:
            outcome = self.transfer(writer, self.write)
        commit.assert_called_once()
        self.assertIs(outcome.value, error)
        self.assertEqual(outcome, api_writer.JobOutcome(False, error, True, 'commit_unknown'))
        self.assertEqual(self.value(), 'after')

    def test_transfer_commit_failed_with_cleanup_failure_stays_commit_unknown(self):
        writer = self.writer()
        self.seed_commit_constraint(writer)
        real_cleanup = writer._cleanup_transfer_connections

        def cleanup_with_failure(*args, **kwargs):
            failures = real_cleanup(*args, **kwargs)
            self.assertEqual(failures, {})
            return {P: sqlite3.OperationalError('observed cleanup failure')}

        with mock.patch.object(writer, '_cleanup_transfer_connections',
                               side_effect=cleanup_with_failure) as cleanup:
            outcome = self.transfer(writer, self.transfer_deferred_violation)
        cleanup.assert_called_once()
        self.assert_commit_constraint(outcome, 'commit_unknown', indeterminate=True)
        self.assert_no_deferred_row()

    def test_job_and_declared_deferred_constraint_commit_stay_commit_unknown(self):
        from types import SimpleNamespace

        writer = self.writer()
        self.seed_commit_constraint(writer)
        self.seed_commit_constraint(writer, catalogue=True)
        writer.bind_service_owner(None, SimpleNamespace(attempt=None, admission_closed=False))
        with self.subTest(runner='job'):
            outcome = writer.submit(P, self.job(self.write_deferred_violation))
            self.assert_commit_constraint(outcome, 'commit_unknown', indeterminate=True)
            self.assert_no_deferred_row()
        with self.subTest(runner='declared'):
            outcome = writer.submit_declared(self.declared(
                lambda conns: self.write_deferred_violation(conns['catalogue'])))
            self.assert_commit_constraint(outcome, 'commit_unknown', indeterminate=True)
            self.assert_no_deferred_row(catalogue=True)

    def test_transfer_unreadable_state_is_commit_unknown_and_cleans_up(self):
        writer = self.writer()
        self.seed_commit_constraint(writer)
        real_commit = api_writer._commit_sender
        errors, senders = [], []

        def commit(conn):
            try:
                real_commit(conn)
            except sqlite3.IntegrityError as error:
                errors.append(error)
                raise

        def write(turn):
            senders.append(turn.sender)
            return self.transfer_deferred_violation(turn)

        with (mock.patch.object(api_writer, '_commit_sender', side_effect=commit),
              mock.patch.object(api_writer, '_sender_in_transaction',
                                side_effect=sqlite3.ProgrammingError('transaction state unreadable')) as state):
            outcome = self.transfer(writer, write)
        state.assert_called_once()
        self.assert_commit_constraint(outcome, 'commit_unknown', indeterminate=True)
        self.assertEqual(len(errors), 1)
        self.assertIs(outcome.value, errors[0])
        self.assertEqual(len(senders), 1)
        with self.assertRaises(api_writer.TurnClosed):
            senders[0].execute('SELECT 1')
        self.assert_no_deferred_row()

    def test_transfer_commits_only_the_sender(self):
        writer = self.writer_pair()
        trace = []
        def run(turn):
            with closing(turn.catalogue.execute('SELECT count(*) FROM project_routes')) as cursor:
                self.assertEqual(cursor.fetchone(), (2,))
            self.assertEqual(turn.read_receiver(Q, 'optional', self.read_name), 'before')
            return self.write(turn)

        submit_transfer = writer.submit_transfer
        run_transfer = writer._run_transfer

        def traced(request):
            writer._catalogue.set_trace_callback(trace.append)
            try:
                return run_transfer(request)
            finally:
                writer._catalogue.set_trace_callback(None)

        with mock.patch.object(writer, '_run_transfer', side_effect=traced):
            self.assertTrue(submit_transfer(api_writer.TransferJob(P, run)).commit)
        self.assertEqual(self.value(), 'after')
        self.assertEqual(writer.read(Q, self.read_name), 'before')
        self.assertIn('ROLLBACK', trace)
        self.assertNotIn('COMMIT', trace)

    @staticmethod
    def read_name(conn):
        with closing(conn.execute('SELECT name FROM projects')) as cursor:
            return cursor.fetchone()[0]

    def test_catalogue_write_through_the_proxy_is_refused(self):
        writer = self.writer()
        def run(turn):
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                self.probe_write(turn)
            self.assertEqual(caught.exception.sqlite_errorname, 'SQLITE_AUTH')
            return self.write(turn)

        self.assertEqual(self.transfer(writer, run), api_writer.JobOutcome(False, None, False, 'turn_violation'))
        self.assertEqual(self.value(), 'before')

    def test_catalogue_query_only_is_the_second_layer(self):
        writer = self.writer()
        # Lookup the submit method first so the baseline error pins it.
        submit_transfer = writer.submit_transfer
        real = writer._transfer_authorizer
        def factory(turn, label):
            return (lambda *_: sqlite3.SQLITE_OK) if label == 'catalogue' else real(turn, label)

        def run(turn):
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                self.probe_write(turn)
            self.assertEqual(caught.exception.sqlite_errorname, 'SQLITE_READONLY')
            return api_writer.JobOutcome(False, None)

        with mock.patch.object(writer, '_transfer_authorizer', side_effect=factory):
            self.assertFalse(submit_transfer(api_writer.TransferJob(P, run)).commit)
        self.assertEqual(writer.read_catalogue(
            lambda conn: conn.execute('SELECT count(*) FROM project_routes').fetchone()[0]), 1)

    def optional(self, case):
        writer = self.writer() if case == 'project_not_served' else self.writer_pair()
        calls = []
        def read(conn):
            calls.append(conn)
            raise sqlite3.OperationalError('injected read fault')

        def run(turn):
            self.assertEqual(turn.read_receiver(Q, 'optional', read), api_writer.ReceiverUnavailable(Q, case))
            if case == 'read_error':
                q = writer._connections[Q]
                self.assertFalse(q.in_transaction)
                self.assertEqual(q.execute('PRAGMA query_only').fetchone(), (0,))
            return self.write(turn)

        if case == 'layout_fault':
            real = writer._check_binding

            def check(label, binding):
                if label == Q:
                    raise store_layout.LayoutFault('member-changed')
                return real(label, binding)

            with mock.patch.object(writer, '_check_binding', side_effect=check):
                self.assertTrue(self.transfer(writer, run).commit)
        else:
            self.assertTrue(self.transfer(writer, run).commit)
        self.assertEqual(len(calls), int(case == 'read_error'))
        if case == 'read_error':
            self.assertIn(Q, writer.served_projects())
        self.assertEqual(self.value(), 'after')

    def test_optional_receiver_not_served_is_isolated(self):
        self.optional('project_not_served')

    def test_optional_receiver_read_error_is_isolated(self):
        self.optional('read_error')

    def test_optional_receiver_layout_fault_is_isolated(self):
        self.optional('layout_fault')

    def test_required_receiver_failure_is_sticky(self):
        writer = self.writer()
        failures = []
        def run(turn):
            with self.assertRaises(api_writer.ReceiverRequired) as caught:
                turn.read_receiver(Q, 'required', self.read_name)
            failures.append(caught.exception)
            return self.write(turn)

        result = self.transfer(writer, run)
        self.assertEqual(result, api_writer.JobOutcome(False, failures[0], False, 'receiver_required'))
        self.assertEqual((failures[0].project_id, failures[0].reason), (Q, 'project_not_served'))
        self.assertEqual(self.value(), 'before')

    def stopped_receiver(self, fail_close):
        # Exercise the real guarded stopped route rather than patching the router.
        GuardedWriterFixture.setUp(self)
        writer = self.writer
        GuardedWriterFixture.admit_second(self)
        writer.open_project(Q)
        self.assertEqual(writer.request_project_stop(Q, 'fixture'), 'stopped')
        self.flush(writer)
        handles = []
        requests = []
        error = OSError('injected transient close failure')
        guarded_open = writer._open_guarded
        close = writer._close_handle

        def open_guarded(project, *, purpose, **options):
            conn, members = guarded_open(project, purpose=purpose, **options)
            self.assertEqual((project, purpose), (Q, 'stopped_read'))
            handles.append(conn)
            return conn, members

        def close_handle(conn):
            if fail_close and handles and conn is handles[0]:
                close(conn)
                raise error
            return close(conn)

        def run(turn):
            requests.append(writer._active_request)
            self.assertEqual(turn.read_receiver(Q, 'required', self.read_name, route='stopped'), 'second')
            return self.write(turn)

        submit_transfer = writer.submit_transfer
        run_transfer = writer._run_transfer

        def check_closed(request):
            outcome = run_transfer(request)
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed database'):
                handles[0].execute('SELECT 1')
            return outcome

        with (mock.patch.object(writer, '_open_guarded', side_effect=open_guarded),
              mock.patch.object(writer, '_close_handle', side_effect=close_handle),
              mock.patch.object(writer, '_run_transfer', side_effect=check_closed)):
            self.assertTrue(submit_transfer(api_writer.TransferJob(P, run)).commit)
        self.assertEqual(len(handles), 1)
        self.assertIsNone(requests[0].transient)
        if fail_close:
            self.assertIs(writer._rollback_errors[Q], error)
        self.assertEqual(self.value(), 'after')

    def test_required_stopped_route_reads_and_closes_transient(self):
        self.stopped_receiver(False)

    def test_transient_close_failure_is_isolated(self):
        self.stopped_receiver(True)

    def stopped_guard_snapshot_case(self, case):
        GuardedWriterFixture.setUp(self)
        writer = self.writer
        GuardedWriterFixture.admit_second(self)
        writer.open_project(Q)
        self.assertEqual(writer.request_project_stop(Q, 'fixture'), 'stopped')
        self.flush(writer)
        self.assertIn(P, writer.served_projects())
        self.assertNotIn(Q, writer.served_projects())
        before = self.value()
        catalogue_path = store_layout.catalogue_path(self.root)

        def change_blocker(present):
            with closing(sqlite3.connect(catalogue_path)) as conn, conn:
                if case == 'cleared_store_stop':
                    if present:
                        store_layout.record_stop(conn, 'store', None, 'fixture')
                    else:
                        store_layout.record_start(conn, 'store', None, 'fixture')
                elif present:
                    conn.execute("INSERT INTO maintenance_holds VALUES ('fixture', 'job', 'cutover', 1)").close()
                else:
                    conn.execute("DELETE FROM maintenance_holds WHERE scope='fixture'").close()

        if case != 'late_hold':
            change_blocker(True)

        pin = getattr(writer, '_pin_transfer_catalogue', None)
        pinned_guard = getattr(writer, '_require_transfer_stopped_snapshot', None)
        runtime = self.guard.runtime_admission
        authorizer = writer._transfer_authorizer
        run_transfer = writer._run_transfer
        guard_calls, pinned_causes, handles = [], [], []
        authorised, observed, ran, live, closed = [], [], [], [], []

        def pin_and_interleave(pinned_conn):
            pin(pinned_conn)
            if case == 'late_hold':
                change_blocker(True)

        def after_begin(phase):
            if phase == 'began' and case != 'late_hold':
                change_blocker(False)

        def authorizer_factory(turn, label):
            authorised.append(label)
            return authorizer(turn, label)

        def snapshot_check(pinned_conn, project):
            try:
                return pinned_guard(pinned_conn, project)
            except store_layout.LayoutFault as error:
                pinned_causes.append(error.cause)
                raise

        def check_private(arm, project, conn, *, purpose):
            handles.append(conn)
            common = (project, purpose, conn is writer._catalogue,
                      writer._catalogue.in_transaction, 'catalogue' in authorised)
            try:
                runtime(arm, project, conn, purpose=purpose)
            except store_layout.LayoutFault as error:
                guard_calls.append((*common, error.cause))
                raise
            else:
                guard_calls.append((*common, None))

        def read(conn):
            ran.append(self.read_name(conn))
            return ran[-1]

        def run(turn):
            try:
                turn.read_receiver(Q, 'required', read, route='stopped')
            except api_writer.ReceiverRequired as error:
                observed.append(error)
            for conn in handles:
                try:
                    conn.execute('SELECT 1').close()
                except sqlite3.ProgrammingError as error:
                    live.append(str(error))
                else:
                    live.append(True)
            return self.write(turn)

        def close_witness(request):
            outcome = run_transfer(request)
            for conn in handles:
                try:
                    conn.execute('SELECT 1').close()
                except sqlite3.ProgrammingError as error:
                    closed.append(str(error))
                else:
                    closed.append(False)
            return outcome

        with (mock.patch.object(writer, '_pin_transfer_catalogue', side_effect=pin_and_interleave, create=True),
              mock.patch.object(writer, '_require_transfer_stopped_snapshot', side_effect=snapshot_check, create=True),
              mock.patch.object(writer, '_phase_hook', side_effect=after_begin),
              mock.patch.object(writer, '_transfer_authorizer', side_effect=authorizer_factory),
              mock.patch.object(self.guard, 'runtime_admission', side_effect=check_private),
              mock.patch.object(writer, '_run_transfer', side_effect=close_witness)):
            outcome = self.transfer(writer, run)
        self.assertEqual(ran, [], 'stopped receiver callback ran despite the catalogue blocker')
        prefix = (Q, 'stopped_read', False, True, True)
        if case == 'late_hold':
            self.assertEqual(guard_calls, [(*prefix, 'incumbent-hold')])
            self.assertEqual(pinned_causes, [])
        else:
            self.assertEqual(guard_calls, [(*prefix, None)] * 3)
            self.assertEqual(pinned_causes, ['incumbent-hold'])
        self.assertEqual(len({id(conn) for conn in handles}), 1)
        self.assertEqual(live, [True] * len(handles))
        self.assertEqual(closed, ['Cannot operate on a closed database.'] * len(handles))
        self.assertEqual(len(observed), 1)
        self.assertEqual((observed[0].project_id, observed[0].reason), (Q, 'layout_fault'))
        self.assertEqual(outcome, api_writer.JobOutcome(False, observed[0], False, 'receiver_required'))
        self.assertEqual(self.value(), before)

    def test_stopped_route_guard_sees_a_hold_after_the_turn_snapshot(self):
        self.stopped_guard_snapshot_case('late_hold')

    def test_stopped_route_refuses_a_blocker_cleared_after_the_turn_snapshot(self):
        for case in ('cleared_hold', 'cleared_store_stop'):
            with self.subTest(case=case):
                self.stopped_guard_snapshot_case(case)

    def test_receiver_naming_the_sender_is_refused(self):
        writer = self.writer()
        def run(turn):
            try:
                turn.read_receiver(P, 'required', self.read_name)
            finally:
                self.assertIsNone(writer._active_request.receiver)

        with self.assertRaises(api_writer.UndeclaredFile):
            self.transfer(writer, run)
        self.assertEqual(self.value(), 'before')

    def test_second_read_of_one_receiver_is_refused(self):
        writer = self.writer_pair()
        def run(turn):
            self.assertEqual(turn.read_receiver(Q, 'optional', self.read_name), 'before')
            return turn.read_receiver(Q, 'optional', self.read_name)

        with self.assertRaises(api_writer.UndeclaredFile):
            self.transfer(writer, run)

    def test_sender_writes_refused_in_the_receiver_phase(self):
        writer = self.writer()
        def run(turn):
            for sql in ("INSERT INTO projects (id, name, created_at) VALUES ('extra', 'extra', 1)",
                        'CREATE TABLE blocked (x)'):
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.DatabaseError) as caught:
                        turn.sender.execute(sql).close()
                    self.assertEqual(caught.exception.sqlite_errorname, 'SQLITE_AUTH')
            return self.write(turn)

        self.assertEqual(self.transfer(writer, run).fault, 'turn_violation')
        self.assertEqual(self.value(), 'before')
        self.assertIsNone(writer.read(P, lambda conn: conn.execute(
            "SELECT name FROM sqlite_schema WHERE name='blocked'").fetchone()))

    def test_receiver_read_after_open_is_refused(self):
        writer = self.writer_pair()
        def run(turn):
            self.write(turn)
            turn.read_receiver(Q, 'optional', self.read_name)

        submit_transfer = writer.submit_transfer
        with self.assertRaises(api_writer.ReceiverAfterWrite):
            submit_transfer(api_writer.TransferJob(P, run))
        self.assertEqual(self.value(), 'before')

    def test_transaction_control_is_refused(self):
        writer = self.writer()

        def seed(conn):
            conn.execute('CREATE TABLE transfer_witness (statement TEXT NOT NULL)').close()
            return api_writer.JobOutcome(True, None)

        self.assertTrue(writer.submit(P, self.job(seed)).commit)

        def snapshot():
            with closing(sqlite3.connect(store_layout.ledger_path(self.root, P))) as conn:
                with closing(conn.execute('SELECT statement FROM transfer_witness ORDER BY rowid')) as rows:
                    values = rows.fetchall()
                with closing(conn.execute('PRAGMA user_version')) as rows:
                    return values, rows.fetchone()[0]

        def check(phase, target, sql):
            recorded = []

            def run(turn):
                if phase == 'write':
                    turn.open_sender_writes()
                try:
                    getattr(turn, target).execute(sql).close()
                except sqlite3.Error as error:
                    name = error.sqlite_errorname
                else:
                    name = 'ok'
                recorded.append((name, writer._connections[P].in_transaction, writer._catalogue.in_transaction))
                if phase == 'receiver':
                    turn.open_sender_writes()
                turn.sender.execute('INSERT INTO transfer_witness VALUES (?)', (sql,)).close()
                return api_writer.JobOutcome(True, None)

            before = snapshot()
            outcome = self.transfer(writer, run)
            after = snapshot()
            with self.subTest(phase=phase, target=target, sql=sql, observation='authorizer'):
                self.assertEqual([item[0] for item in recorded], ['SQLITE_AUTH'])
            with self.subTest(phase=phase, target=target, sql=sql, observation='transactions'):
                self.assertEqual([item[1:] for item in recorded], [(True, True)])
                self.assertEqual(outcome, api_writer.JobOutcome(False, None, False, 'turn_violation'))
            with self.subTest(phase=phase, target=target, sql=sql, observation='durable'):
                self.assertEqual(after, before)

        basic = ('COMMIT', 'ROLLBACK', 'BEGIN', 'PRAGMA query_only=OFF', 'PRAGMA optimize',
                 "ATTACH ':memory:' AS other")
        for phase in ('receiver', 'write'):
            for sql in basic + (('PRAGMA user_version=7',) if phase == 'write' else ()):
                check(phase, 'sender', sql)
            check(phase, 'catalogue', 'COMMIT')

    def guard_writer(self):
        writer = self.writer()

        def seed(conn):
            conn.execute('CREATE TABLE guard_witness (value TEXT NOT NULL)').close()
            return api_writer.JobOutcome(True, None)

        self.assertTrue(writer.submit(P, self.job(seed)).commit)
        return writer

    def guard_rows(self):
        path = store_layout.ledger_path(self.root, P)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            with closing(conn.execute('SELECT value FROM guard_witness ORDER BY rowid')) as cursor:
                return cursor.fetchall()

    @staticmethod
    def guard_insert(conn, value):
        conn.execute('INSERT INTO guard_witness VALUES (?)', (value,)).close()

    @staticmethod
    def deny_guard_insert(action, table, *_):
        if action == sqlite3.SQLITE_INSERT and table == 'guard_witness':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @staticmethod
    def guard_status(fn):
        try:
            fn()
        except sqlite3.Error as error:
            return error.sqlite_errorname
        return 'ok'

    def test_turn_guard_cannot_lift_the_writer_policy(self):
        writer = self.guard_writer()
        seen = []

        def run(turn):
            with turn.sender.guard(lambda *_: sqlite3.SQLITE_OK):
                seen.append(self.guard_status(lambda: self.guard_insert(turn.sender, 'blocked')))
                seen.append(self.guard_status(lambda: turn.sender.execute('COMMIT').close()))
            return api_writer.JobOutcome(False, None)

        outcome = self.transfer(writer, run)
        self.assertEqual(seen, ['SQLITE_AUTH', 'SQLITE_AUTH'])
        self.assertEqual(outcome.fault, 'turn_violation')
        self.assertEqual(self.guard_rows(), [])

    def test_turn_guard_restores_writer_policy_after_a_raise(self):
        writer = self.guard_writer()
        seen = []

        def deny_read(action, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ else sqlite3.SQLITE_OK

        def run(turn):
            def select():
                turn.sender.execute('SELECT value FROM guard_witness').close()

            def guarded_select():
                with turn.sender.guard(deny_read):
                    select()

            seen.append(self.guard_status(guarded_select))
            seen.append(self.guard_status(select))
            seen.append(self.guard_status(lambda: turn.sender.execute('COMMIT').close()))
            return api_writer.JobOutcome(False, None)

        outcome = self.transfer(writer, run)
        self.assertEqual(seen, ['SQLITE_AUTH', 'ok', 'SQLITE_AUTH'])
        self.assertEqual(outcome.fault, 'turn_violation')

    def test_open_sender_writes_inside_a_guard_keeps_the_guard(self):
        writer = self.guard_writer()
        seen = []

        def run(turn):
            with turn.sender.guard(self.deny_guard_insert):
                turn.open_sender_writes()
                seen.append(self.guard_status(lambda: self.guard_insert(turn.sender, 'blocked')))
                self.change(turn.sender, 'updated')
            self.guard_insert(turn.sender, 'allowed')
            return api_writer.JobOutcome(True, None)

        outcome = self.transfer(writer, run)
        self.assertEqual(seen, ['SQLITE_AUTH'])
        self.assertEqual(outcome, api_writer.JobOutcome(True, None))
        self.assertEqual(self.value(), 'updated')
        self.assertEqual(self.guard_rows(), [('allowed',)])

    def test_inner_guard_replaces_the_outer_layer(self):
        writer = self.guard_writer()
        seen = []

        def run(turn):
            turn.open_sender_writes()
            with turn.sender.guard(self.deny_guard_insert):
                seen.append(self.guard_status(lambda: self.guard_insert(turn.sender, 'outer before')))
                with turn.sender.guard(None):
                    seen.append(self.guard_status(lambda: self.guard_insert(turn.sender, 'inner')))
                seen.append(self.guard_status(lambda: self.guard_insert(turn.sender, 'outer after')))
            return api_writer.JobOutcome(True, None)

        outcome = self.transfer(writer, run)
        self.assertEqual(seen, ['SQLITE_AUTH', 'ok', 'SQLITE_AUTH'])
        self.assertEqual(outcome, api_writer.JobOutcome(True, None))
        self.assertEqual(self.guard_rows(), [('inner',)])

    def test_engine_layer_deny_does_not_mark_the_turn(self):
        writer = self.guard_writer()
        seen = []

        def run(turn):
            turn.open_sender_writes()
            turn.sender.execute('SAVEPOINT s').close()
            self.guard_insert(turn.sender, 'partial')
            with turn.sender.guard(self.deny_guard_insert):
                seen.append(self.guard_status(lambda: self.guard_insert(turn.sender, 'denied')))
                turn.sender.execute('ROLLBACK TO s').close()
                turn.sender.execute('RELEASE s').close()
            self.guard_insert(turn.sender, 'recorded refusal')
            return api_writer.JobOutcome(True, None)

        outcome = self.transfer(writer, run)
        self.assertEqual(seen, ['SQLITE_AUTH'])
        self.assertEqual(outcome, api_writer.JobOutcome(True, None))
        self.assertEqual(self.guard_rows(), [('recorded refusal',)])

    def test_turn_guard_progress_aborts_and_clears(self):
        writer = self.guard_writer()
        seen = []

        def run(turn):
            def select():
                with closing(turn.sender.execute('SELECT 1')) as cursor:
                    return cursor.fetchone()
            with turn.sender.guard(None, progress=lambda: 1):
                try:
                    select()
                except sqlite3.OperationalError as error:
                    seen.append((error.sqlite_errorname, str(error)))
                else:
                    seen.append('not interrupted')
            seen.append(select())
            return api_writer.JobOutcome(False, None)

        self.transfer(writer, run)
        self.assertEqual(seen, [('SQLITE_INTERRUPT', 'interrupted'), (1,)])

    def test_transfer_turn_admits_named_savepoints(self):
        writer = self.guard_writer()
        seen = []

        def run(turn):
            turn.open_sender_writes()
            statements = (
                lambda: turn.sender.execute('SAVEPOINT s').close(),
                lambda: self.guard_insert(turn.sender, 'rolled back'),
                lambda: turn.sender.execute('ROLLBACK TO s').close(),
                lambda: turn.sender.execute('RELEASE s').close(),
                lambda: self.guard_insert(turn.sender, 'committed'),
            )
            for statement in statements:
                seen.append(self.guard_status(statement))
                if seen[-1] != 'ok':
                    break
            return api_writer.JobOutcome(True, None)

        outcome = self.transfer(writer, run)
        self.assertEqual(seen, ['ok'] * 5)
        self.assertEqual(outcome, api_writer.JobOutcome(True, None))
        self.assertEqual(self.guard_rows(), [('committed',)])

    def test_transfer_turn_refuses_plain_transaction_control_inside_a_savepoint(self):
        writer = self.guard_writer()
        for sql in ('END', 'ROLLBACK', 'COMMIT'):
            with self.subTest(sql=sql):
                seen = []

                def run(turn, sql=sql):
                    turn.open_sender_writes()
                    turn.sender.execute('SAVEPOINT s').close()
                    self.guard_insert(turn.sender, sql)
                    seen.append(self.guard_status(lambda: turn.sender.execute(sql).close()))
                    return api_writer.JobOutcome(True, None)

                before = self.guard_rows()
                outcome = self.transfer(writer, run)
                self.assertEqual(seen, ['SQLITE_AUTH'])
                self.assertEqual(outcome, api_writer.JobOutcome(False, None, False, 'turn_violation'))
                self.assertEqual(self.guard_rows(), before)

    def test_turn_connection_in_transaction_and_closed_guard(self):
        writer = self.guard_writer()
        retained = []

        def run(turn):
            retained.append((turn.sender, turn.sender.in_transaction))
            return api_writer.JobOutcome(False, None)

        self.transfer(writer, run)
        sender, active = retained[0]
        self.assertIs(active, True)
        with self.assertRaises(api_writer.TurnClosed):
            _ = sender.in_transaction
        with self.assertRaises(api_writer.TurnClosed):
            with sender.guard(None):
                self.fail('closed guard entered')

    def test_cached_sender_statement_is_refused(self):
        writer = self.writer()
        sql = "INSERT INTO projects (id, name, created_at) VALUES ('cached', 'extra', 1)"

        def cache(conn):
            conn.execute(sql).close()
            return api_writer.JobOutcome(False, None)

        writer.submit(P, self.job(cache))

        def run(turn):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'not authorized'):
                turn.sender.execute(sql).close()
            return api_writer.JobOutcome(False, None)

        self.assertEqual(self.transfer(writer, run).fault, 'turn_violation')
        self.assertEqual(self.value(), 'before')

    def test_turn_exposes_no_raw_connection(self):
        writer = self.writer_pair()
        retained = []
        def inspect(proxy):
            for name in ('commit', 'rollback', 'close', 'cursor', 'set_authorizer', 'interrupt'):
                self.assertFalse(hasattr(proxy, name), name)
            with closing(proxy.execute('SELECT 1')) as cursor:
                self.assertFalse(hasattr(cursor, 'connection'))
                self.assertEqual(cursor.fetchone(), (1,))
            retained.append(proxy)

        def run(turn):
            inspect(turn.sender)
            turn.read_receiver(Q, 'optional', inspect)
            with self.assertRaises(api_writer.TurnClosed):
                retained[1].execute('SELECT 1')
            return self.write(turn)

        self.assertTrue(self.transfer(writer, run).commit)
        with self.assertRaises(api_writer.TurnClosed):
            retained[0].execute('SELECT 1')

    def test_engine_lost_transaction_is_indeterminate(self):
        writer = self.writer_pair()
        def run(turn):
            conn = writer._connections[P]  # Engine fault outside the proxy.
            conn.set_authorizer(None)
            conn.rollback()
            turn.read_receiver(Q, 'optional', self.read_name)

        result = self.transfer(writer, run)
        self.assertEqual((result.commit, result.indeterminate, result.fault),
                         (False, True, 'job_ended_transaction'))
        self.assertIsInstance(result.value, api_writer.TransferTransactionEnded)

    def test_indeterminate_outcome_publishes_nothing(self):
        writer = self.writer()
        def run(turn):
            self.write(turn)
            return api_writer.JobOutcome(True, 'value', True)

        self.transfer(writer, run)
        self.assertEqual(self.value(), 'before')

    def test_commit_requires_open_sender_writes(self):
        writer = self.writer()
        result = self.transfer(writer, lambda turn: api_writer.JobOutcome(True, 'value'))
        self.assertEqual(result, api_writer.JobOutcome(False, 'value', False, 'sender_not_opened'))
        self.assertEqual(self.value(), 'before')

    def test_optional_stopped_route_is_refused(self):
        writer = self.writer_pair()
        with self.assertRaises(api_writer.UndeclaredFile):
            self.transfer(writer, lambda t: t.read_receiver(Q, 'optional', self.read_name, route='stopped'))

    def test_unserved_sender_is_refused_before_queueing(self):
        writer = self.writer(opened=False)
        self.assert_unavailable(lambda: self.transfer(writer, lambda _: self.fail('callback ran')),
                                'project_not_served')

    def test_queued_transfer_withdrawn_by_sender_stop(self):
        writer = self.writer_pair()
        submit_transfer = writer.submit_transfer
        parked, release, queued = (threading.Event() for _ in range(3))
        def block(conn):
            parked.set()
            self.assertTrue(release.wait(3))
            return api_writer.JobOutcome(False, None)

        with self.pool(3, release) as pool:
            first = pool.submit(writer.submit, Q, self.job(block, Q))
            self.assertTrue(parked.wait(2))
            writer._phase_hook = lambda phase: queued.set() if phase == 'queued' else None
            pending = pool.submit(submit_transfer, api_writer.TransferJob(P, lambda _: self.fail('callback ran')))
            self.assertTrue(queued.wait(2))
            stopping = pool.submit(writer.request_project_stop, P, 'fixture')
            self.assert_unavailable(lambda: pending.result(2), 'project_not_served')
            release.set()
            first.result(2)
            self.assertEqual(stopping.result(2), 'stopped')

    def stop_case(self, case):
        import time
        from types import SimpleNamespace

        writer = self.writer_pair()
        submit_transfer = writer.submit_transfer
        parked, release, interrupted = (threading.Event() for _ in range(3))
        epoch = SimpleNamespace(attempt=None, admission_closed=False)
        writer._epoch = epoch
        actual_interrupt = writer._interrupt
        requests, sites, ran, facts = [], [], [], []

        def interruption(conns):
            if case in ('active', 'idle'):
                self.assertIn(writer._connections[Q], conns)
            actual_interrupt(conns)
            interrupted.set()

        def park():
            parked.set()
            self.assertTrue(release.wait(3))

        def read(conn):
            ran.append(True)
            if case == 'active':
                raw = writer._connections[Q]
                def progress():
                    park()
                    return 0
                raw.set_progress_handler(progress, 10)
                try:
                    conn.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000)'
                                 ' SELECT sum(x) FROM n').fetchone()
                finally:
                    raw.set_progress_handler(None, 0)
                self.fail('active receiver query not interrupted')
            if case == 'idle':
                park()
            value = self.read_name(conn)
            facts.append(value)
            return value

        def run(turn):
            requests.append(writer._active_request)
            if case == 'unpublished':
                park()
            if case != 'committing':
                try:
                    turn.read_receiver(Q, 'optional', read)
                except api_writer.WriterUnavailable as error:
                    sites.append(('read_receiver', error.reason))
                    raise
            if case == 'opening':
                park()
            try:
                return self.write(turn)
            except api_writer.WriterUnavailable as error:
                sites.append(('open_sender_writes', error.reason))
                raise

        if case == 'committing':
            writer._phase_hook = lambda phase: park() if phase == 'commit_permitted' else None
        with mock.patch.object(writer, '_interrupt', side_effect=interruption):
            with self.pool(2, release) as pool:
                pending = pool.submit(submit_transfer, api_writer.TransferJob(P, run))
                self.assertTrue(parked.wait(2))
                now = time.monotonic()
                epoch.attempt = SimpleNamespace(active=now + (0.2 if case == 'committing' else 2), work=now + 3)
                epoch.admission_closed = True
                fence = pool.submit(writer.fence_for_stop, epoch.attempt)
                self.assertTrue(interrupted.wait(2))
                if case != 'committing':
                    release.set()
                outcome = pending.result(3)
                release.set()
                fence.result(3)
        if case == 'committing':
            self.assertEqual(outcome, api_writer.JobOutcome(False, None, True, 'stop_commit_unknown'))
        else:
            self.assertEqual(outcome, api_writer.JobOutcome(False, None, False, 'stop_unwritten'))
            self.assertEqual(self.value(), 'before')
        self.assertIsNone(requests[0].receiver)
        if case in ('idle', 'unpublished'):
            self.assertEqual(sites, [('read_receiver', 'stopped')])
        if case == 'idle':
            self.assertEqual(facts, ['before'])
        if case == 'opening':
            self.assertEqual(sites, [('open_sender_writes', 'stopped')])
        if case == 'unpublished':
            self.assertFalse(ran)

    def test_stop_during_receiver_read_is_unwritten(self):
        self.stop_case('active')

    def test_stop_on_an_idle_published_handle(self):
        self.stop_case('idle')

    def test_open_sender_writes_after_a_stop(self):
        self.stop_case('opening')

    def test_stop_before_publication_runs_no_callback(self):
        self.stop_case('unpublished')

    def test_stop_while_committing_is_commit_unknown(self):
        self.stop_case('committing')

    def test_catalogue_job_after_a_transfer(self):
        writer = self.writer()
        def blocked(turn):
            with self.assertRaises(sqlite3.DatabaseError):
                self.probe_write(turn)
            return self.write(turn)

        for index, run in enumerate((self.write, lambda _: api_writer.JobOutcome(False, None), blocked)):
            self.transfer(writer, run)
            def write_catalogue(conns):
                conns['catalogue'].execute('UPDATE project_routes SET name=? WHERE project_id=?',
                                           (f'updated-{index}', P)).close()
                return api_writer.JobOutcome(True, None)
            self.assertTrue(writer.submit_declared(self.declared(write_catalogue)).commit)

    def test_catalogue_cleanup_failure_after_commit(self):
        writer = self.writer()
        rollback = store_layout.rollback_declared
        error = sqlite3.OperationalError('catalogue rollback error')
        def fail(conns):
            outcomes = dict(rollback(conns))
            if 'catalogue' in conns:
                outcomes['catalogue'] = error
            return outcomes

        with mock.patch.object(store_layout, 'rollback_declared', side_effect=fail):
            outcome = self.transfer(writer, self.write)
        self.assertTrue(outcome.commit)
        self.assertEqual(outcome.fault, 'rollback_failed')
        self.assertNotIn('catalogue', writer._bindings)
        self.assertIs(writer._catalogue_fault, error)
        self.assertEqual(self.value(), 'after')

    def test_held_port_refuses_transfer(self):
        port = api_writer._HeldPort(self.writer())
        self.assert_unavailable(lambda: self.transfer(port, lambda _: self.fail('callback ran')), 'store_held')

    def test_nested_transfer_is_refused(self):
        writer = self.writer()
        with self.assertRaises(api_writer.NestedWriterJob):
            self.transfer(writer, lambda turn: self.transfer(writer, self.write))

    def test_job_error_commits_nothing(self):
        writer = self.writer()
        def run(turn):
            self.write(turn)
            raise ValueError('job failure')

        with self.assertRaisesRegex(ValueError, 'job failure'):
            self.transfer(writer, run)
        self.assertEqual(self.value(), 'before')

    def test_non_outcome_return_is_a_type_error(self):
        writer = self.writer()
        def run(turn):
            self.write(turn)

        with self.assertRaises(TypeError):
            self.transfer(writer, run)
        self.assertEqual(self.value(), 'before')
