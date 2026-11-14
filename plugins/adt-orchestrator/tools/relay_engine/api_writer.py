"""One worker owns the project connection and each job's transaction.

A job that ends its own transaction produces an indeterminate outcome.
In that case even KeyboardInterrupt or SystemExit is returned as its value,
not propagated: ending the writer's transaction is a defect of the job.
The caller must reconcile that outcome rather than assume failure.
"""

from dataclasses import dataclass, field
import os
import queue
import sqlite3
import threading
from collections.abc import Callable
from typing import Protocol

from relay_engine import store_layout


@dataclass(frozen=True)
class JobOutcome:
    commit: bool
    value: object
    indeterminate: bool = False
    fault: str | None = None


class WriterJob(Protocol):
    def run(self, conn: sqlite3.Connection) -> JobOutcome: ...


class WriterUnavailable(Exception):
    """A stable reason for a writer refusal, without request material."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class NestedWriterJob(Exception):
    """The caller is already inside this writer and cannot wait on it."""


@dataclass
class _Request:
    kind: str
    job: WriterJob | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


def _unavailable(reason, cause):
    error = WriterUnavailable(reason)
    error.__cause__ = cause
    return error


class StoreWriter:
    def __init__(self, root: str | os.PathLike, project_id: str, *,
                 phase_hook: Callable[[str], None] | None = None):
        self._root = root
        self._project_id = project_id
        self._phase_hook = phase_hook
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._local = threading.local()
        self._worker = None
        self._closed = False
        self._served = False
        self._rollback_error = None
        self._conn = None

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise WriterUnavailable('closed')
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name='store-writer')
                self._worker.start()

    def _nested(self):
        return (threading.current_thread() is self._worker
                or getattr(self._local, 'active', False))

    def _phase(self, phase):
        if self._phase_hook is not None:
            self._phase_hook(phase)

    def _call(self, request):
        if self._nested():
            raise NestedWriterJob()
        self._local.active = True
        try:
            with self._lock:
                if self._closed or self._worker is None:
                    raise WriterUnavailable('closed')
                if request.kind == 'job' and not self._served:
                    raise WriterUnavailable('project_not_served')
                self._queue.put(request)
            try:
                if request.kind == 'job':
                    self._phase('queued')
            finally:
                request.ready.set()
            request.done.wait()
            if request.error is not None:
                error = request.error
                context, cause = error.__context__, error.__cause__
                suppress_context = error.__suppress_context__
                try:
                    raise error
                except BaseException:
                    # Raising here can attach the caller's active exception.
                    error.__context__, error.__cause__ = context, cause
                    error.__suppress_context__ = suppress_context
                    raise
            return request.result
        finally:
            self._local.active = False

    def open_project(self, project_id: str) -> None:
        if self._nested():
            raise NestedWriterJob()
        if project_id != self._project_id:
            raise ValueError('wrong_project')
        self._call(_Request('open'))

    def submit(self, job: WriterJob) -> JobOutcome:
        return self._call(_Request('job', job))

    def close(self) -> None:
        if self._nested():
            raise NestedWriterJob()
        with self._lock:
            worker = self._worker
            if not self._closed:
                self._closed = True
                if worker is not None:
                    request = _Request('close')
                    request.ready.set()
                    self._queue.put(request)
        if worker is not None:
            worker.join()

    def _open(self):
        if self._rollback_error is not None:
            raise _unavailable('rollback_failed', self._rollback_error)
        if self._conn is None:
            conn = store_layout.open_ledger(self._root, self._project_id)
            try:
                conn.isolation_level = None
            except BaseException:
                conn.close()
                raise
            self._conn = conn
            with self._lock:
                self._served = True

    def _rollback(self, original=None):
        failure = None
        try:
            self._conn.execute('ROLLBACK').close()
        except BaseException as error:
            failure = error
        if failure is not None:
            with self._lock:
                self._served = False
                self._rollback_error = failure
            conn, self._conn = self._conn, None
            conn.close()
            if original is not None:
                original.add_note('writer ROLLBACK failed: ' + type(failure).__name__
                                  + ' ' + str(getattr(failure, 'sqlite_errorname', None)))
                return original
            return _unavailable('rollback_failed', failure)
        return original

    def _run_job(self, job):
        if self._rollback_error is not None:
            return None, _unavailable('rollback_failed', self._rollback_error)
        self._phase('dequeued')
        try:
            self._conn.execute('BEGIN IMMEDIATE').close()
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_BUSY:
                return None, _unavailable('busy', error)
            return None, error
        original = None
        outcome = None
        try:
            self._phase('began')
            outcome = job.run(self._conn)
            self._phase('finishing')
        except BaseException as error:
            original = error
        if not self._conn.in_transaction:
            value = original if original is not None else outcome.value
            return JobOutcome(False, value, True, 'job_ended_transaction'), None
        if original is not None:
            return None, self._rollback(original)
        try:
            if outcome.commit:
                self._conn.execute('COMMIT').close()
            else:
                failure = self._rollback()
                if failure is not None:
                    return None, failure
        except BaseException as error:
            original = error
        if original is not None:
            if self._conn is not None and self._conn.in_transaction:
                original = self._rollback(original)
            return None, original
        return outcome, None

    def _work(self):
        while True:
            request = self._queue.get()
            request.ready.wait()
            try:
                if request.kind == 'close':
                    if self._conn is not None:
                        self._conn.close()
                        self._conn = None
                    return
                if request.kind == 'open':
                    self._open()
                else:
                    request.result, request.error = self._run_job(request.job)
            except BaseException as error:
                request.error = error
            finally:
                request.done.set()
                self._queue.task_done()
