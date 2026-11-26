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
