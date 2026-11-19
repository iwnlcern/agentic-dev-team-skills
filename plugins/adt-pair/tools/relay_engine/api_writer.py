"""One worker owns every served ledger and runs both transaction modes.

A job that ends its own transaction produces an indeterminate outcome.
In that case even KeyboardInterrupt or SystemExit is returned as its value,
not propagated: ending the writer's transaction is a defect of the job.
The caller must reconcile that outcome rather than assume failure.

A queued hook that raises withdraws its job before the worker starts it.
An asynchronous exception in the calling thread while it waits for an outcome
leaves the job running to its own outcome without telling that caller.
A same-key retry reconciles through the session receipt in that case.
"""

from dataclasses import dataclass, field
import os
import queue
import sqlite3
import threading
from collections.abc import Callable
from typing import Protocol, TypeVar

from relay_engine import store_layout


@dataclass(frozen=True)
class JobOutcome:
    commit: bool
    value: object
    indeterminate: bool = False
    fault: str | None = None


class WriterJob(Protocol):
    files: frozenset[str]
    def run(self, conn: sqlite3.Connection) -> JobOutcome: ...


class CalleeJob(Protocol):
    files: frozenset[str]
    def run(self, conn: sqlite3.Connection) -> JobOutcome: ...


T = TypeVar('T')


class StorePort(Protocol):
    def submit(self, project_id: str, job: WriterJob) -> JobOutcome: ...
    def submit_callee(self, project_id: str, job: CalleeJob) -> JobOutcome: ...
    def read(self, project_id: str, fn: Callable[[sqlite3.Connection], T]) -> T: ...
    def ledger_file(self, project_id: str) -> str: ...
    def store_id(self) -> str: ...
    def served_projects(self) -> frozenset[str]: ...


class CalleeLeftTransaction(Exception):
    """A callee returned without closing its transaction."""


class UndeclaredFile(ValueError):
    """A job does not declare exactly its served ledger file."""


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
    project_id: str | None = None
    job: WriterJob | CalleeJob | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None
    withdrawn: bool = False


def _unavailable(reason, cause):
    error = WriterUnavailable(reason)
    error.__cause__ = cause
    return error


class StoreWriter:
    def __init__(self, root: str | os.PathLike, *, runtime_instance_id: str,
                 phase_hook: Callable[[str], None] | None = None):
        self._root = root
        self._runtime_instance_id = runtime_instance_id
        self._active_project = None
        self._phase_hook = phase_hook
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._worker = None
        self._closed = False
        self._served = set()
        self._rollback_errors = {}
        self._connections = {}
        self._ledger_files = {}
        self._conn = None
        self._catalogue = None
        self._started = threading.Event()
        self._start_error = None

    @property
    def runtime_instance_id(self) -> str:
        return self._runtime_instance_id

    def _require_served(self, project_id):
        if self._closed or self._worker is None:
            raise WriterUnavailable('closed')
        if project_id not in self._served:
            raise WriterUnavailable('project_not_served')

    def _check_files(self, project_id, job):
        files = getattr(job, 'files', None)
        if not isinstance(files, frozenset) or len(files) != 1:
            raise UndeclaredFile()
        try:
            actual = os.stat(next(iter(files)))
            expected = os.stat(self._ledger_files[project_id])
        except (OSError, TypeError, ValueError):
            raise UndeclaredFile() from None
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise UndeclaredFile()

    def ledger_file(self, project_id: str) -> str:
        with self._lock:
            self._require_served(project_id)
            return self._ledger_files[project_id]

    def served_projects(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._served)

    def store_id(self) -> str:
        conn = store_layout.open_catalogue_readonly(self._root)
        try:
            cursor = conn.execute('SELECT store_id FROM catalogue_identity WHERE id=1')
            try:
                return cursor.fetchone()[0]
            finally:
                cursor.close()
        finally:
            conn.close()

    def read(self, project_id: str, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            self._require_served(project_id)
        conn = store_layout.open_ledger_readonly(self._root, project_id)
        try:
            return fn(conn)
        finally:
            conn.close()

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise WriterUnavailable('closed')
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name='store-writer')
                self._worker.start()
            self._started.wait()
            if self._start_error is not None:
                self._closed = True
                self._worker.join()
                raise self._start_error

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
                if request.kind in {'job', 'callee'}:
                    self._require_served(request.project_id)
                    self._check_files(request.project_id, request.job)
                self._queue.put(request)
            try:
                if request.kind in {'job', 'callee'}:
                    self._phase('queued')
            except BaseException:
                request.withdrawn = True
                raise
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
        self._call(_Request('open', project_id))

    def submit(self, project_id: str, job: WriterJob) -> JobOutcome:
        return self._call(_Request('job', project_id, job))

    def submit_callee(self, project_id: str, job: CalleeJob) -> JobOutcome:
        return self._call(_Request('callee', project_id, job))

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

    def _open(self, project_id):
        if project_id in self._rollback_errors:
            raise _unavailable('rollback_failed', self._rollback_errors[project_id])
        if project_id not in self._connections:
            conn = store_layout.open_ledger(self._root, project_id)
            try:
                conn.isolation_level = None
                path = os.path.realpath(store_layout.ledger_path(self._root, project_id))
            except BaseException:
                conn.close()
                raise
            self._connections[project_id] = conn
            with self._lock:
                self._ledger_files[project_id] = path
                self._served.add(project_id)

    def _rollback(self, original=None):
        failure = None
        try:
            self._conn.execute('ROLLBACK').close()
        except BaseException as error:
            failure = error
        if failure is not None:
            with self._lock:
                self._served.discard(self._active_project)
                self._rollback_errors[self._active_project] = failure
            conn, self._conn = self._connections.pop(self._active_project), None
            conn.close()
            if original is not None:
                original.add_note('writer ROLLBACK failed: ' + type(failure).__name__
                                  + ' ' + str(getattr(failure, 'sqlite_errorname', None)))
                return original
            return _unavailable('rollback_failed', failure)
        return original

    def _run_job(self, job):
        if self._active_project in self._rollback_errors:
            return None, _unavailable('rollback_failed', self._rollback_errors[self._active_project])
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

    def _run_callee(self, job):
        if self._active_project in self._rollback_errors:
            return None, _unavailable('rollback_failed', self._rollback_errors[self._active_project])
        self._phase('dequeued')
        if self._conn.in_transaction:
            return None, self._rollback(CalleeLeftTransaction())
        original = None
        outcome = None
        try:
            self._phase('handed')
            outcome = job.run(self._conn)
            self._phase('returned')
        except BaseException as error:
            original = error
        if self._conn.in_transaction:
            original = self._rollback(original if original is not None else CalleeLeftTransaction())
        if original is not None:
            return None, original
        return outcome, None

    def _work(self):
        try:
            path = store_layout.catalogue_path(self._root)
            if not path.exists() or path.stat().st_size == 0:
                raise store_layout.LayoutFault('missing')
            self._catalogue = store_layout.open_catalogue(self._root)
        except BaseException as error:
            self._start_error = error
            return
        finally:
            self._started.set()
        while True:
            request = self._queue.get()
            request.ready.wait()
            try:
                if request.withdrawn:
                    continue
                if request.kind == 'close':
                    for conn in self._connections.values():
                        conn.close()
                    self._connections.clear()
                    self._catalogue.close()
                    self._catalogue = None
                    self._conn = None
                    with self._lock:
                        self._served.clear()
                    return
                if request.kind == 'open':
                    self._open(request.project_id)
                else:
                    self._active_project = request.project_id
                    self._conn = self._connections.get(request.project_id)
                    run = self._run_callee if request.kind == 'callee' else self._run_job
                    request.result, request.error = run(request.job)
            except BaseException as error:
                request.error = error
            finally:
                request.done.set()
                self._queue.task_done()
