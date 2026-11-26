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

from dataclasses import dataclass, field, replace
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from types import MappingProxyType
from typing import Literal, Protocol, TypeVar

from relay_engine import restore_cancel, store_layout, store_steps, strings


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


@dataclass(frozen=True)
class DeclaredJob:
    declared: store_steps.Declared
    run: Callable[[Mapping[str, sqlite3.Connection]], JobOutcome]


T = TypeVar('T')
ProjectStopResult = Literal['stopped', 'already_stopped', 'not_recorded', 'not_known']
ProjectStartResult = Literal['started', 'started_not_served', 'not_stopped', 'still_stopped', 'not_known']


class StorePort(Protocol):
    def submit(self, project_id: str, job: WriterJob) -> JobOutcome: ...
    def submit_callee(self, project_id: str, job: CalleeJob) -> JobOutcome: ...
    def submit_declared(self, job: DeclaredJob) -> JobOutcome: ...
    def read(self, project_id: str, fn: Callable[[sqlite3.Connection], T]) -> T: ...
    def read_catalogue(self, fn: Callable[[sqlite3.Connection], T]) -> T: ...
    def ledger_file(self, project_id: str) -> str: ...
    def store_id(self) -> str: ...
    def served_projects(self) -> frozenset[str]: ...
    def request_project_stop(self, project_id: str, principal: str) -> ProjectStopResult: ...
    def request_project_start(self, project_id: str, principal: str) -> ProjectStartResult: ...


@dataclass(frozen=True)
class StartResult:
    state: Literal['serving', 'held']
    causes: tuple[str, ...]
    port: StorePort


class _HeldPort:
    def __init__(self, writer):
        self._writer = writer

    def read_catalogue(self, fn):
        return self._writer.read_catalogue(fn)

    def store_id(self):
        return self._writer.store_id()

    def submit_declared(self, job):
        if job.declared.ledgers:
            raise WriterUnavailable('store_held')
        return self._writer.submit_declared(job)

    def _refuse(self, *_args, **_kwargs):
        raise WriterUnavailable('store_held')

    submit = submit_callee = read = ledger_file = served_projects = _refuse
    request_project_stop = request_project_start = _refuse


class CalleeLeftTransaction(Exception):
    """A callee returned without closing its transaction."""


class UndeclaredFile(ValueError):
    """A job declaration does not match its writer-owned files."""


class WriterUnavailable(Exception):
    """A stable reason for a writer refusal, without request material."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class NestedWriterJob(Exception):
    """The caller is already inside this writer and cannot wait on it."""


class TurnIndeterminate(Exception):
    """The caller must recover the result from durable state."""


@dataclass(frozen=True)
class _FileBinding:
    conn: sqlite3.Connection
    identity: tuple[int, int]


def _identity(path):
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino


def _require_identity(path, expected):
    try:
        if _identity(path) == expected:
            return
    except OSError:
        pass
    raise UndeclaredFile()


@dataclass
class _Request:
    kind: str
    project_id: str | None = None
    job: WriterJob | CalleeJob | DeclaredJob | None = None
    bindings: dict[str, _FileBinding] = field(default_factory=dict)
    ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None
    withdrawn: bool = False
    committing: bool = False


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
        self._changed = threading.Condition(self._lock)
        self._epoch = None
        self._holder = None
        self._store_guard = None
        self._store_arm = None
        self._project_members = {}
        self._stop_pending = None
        self._checkpoint_only = False
        self._checkpoint_reentry = False
        self._readers = {}
        self._failed_closes = []
        self._requests = {}
        self._active_request = None
        self._close_complete = False
        self._local = threading.local()
        self._worker = None
        self._closed = False
        self._served = set()
        self._project_locks = {}
        self._project_admission = {}
        self._pending_project_start = set()
        self._closing_projects = set()
        self._pending_project_stop = set()
        self._rollback_errors = {}
        self._connections = {}
        self._ledger_files = {}
        self._conn = None
        self._catalogue = None
        self._catalogue_fault = None
        self._bindings: dict[str, _FileBinding] = {}
        self._started = threading.Event()
        self._start_error = None
        self._startup_state = None
        self._start_result = None
        self._before_project_join = None

    @property
    def runtime_instance_id(self) -> str:
        return self._runtime_instance_id

    def bind_service_owner(self, holder, epoch):
        self._holder = holder
        self._epoch = epoch

    def bind_store_arm(self, guard, arm, stopping, members):
        self._store_guard, self._store_arm, self._stop_pending = guard, arm, stopping
        self._admitted_members = members

    def _check_writer_admission(self, project_id=None):
        self._check_store_open()
        if project_id is None:
            self._store_guard._check_admitted_members(self._store_arm, self._admitted_members)
            if self._catalogue is not None:
                store_layout._require_admission_identity(self._catalogue, self._store_arm.store,
                                                         self._store_arm.project)
        elif self._store_guard.runtime_members(
                self._store_arm, project_id, phase='connected') != self._project_members[project_id]:
            raise store_layout.LayoutFault('member-changed')
        self._check_store_open()

    def _check_store_open(self):
        if self._store_guard is None:
            return
        self._store_guard._current(self._store_arm)
        if self._closed or self._stop_pending():
            if self._checkpoint_only:
                self._checkpoint_reentry = True
            raise WriterUnavailable('stopped')

    def verify_service_owner(self):
        if self._store_guard is not None:
            self._store_guard._current(self._store_arm)
        fd, path, identity = self._holder
        status = os.fstat(fd)
        if (status.st_dev, status.st_ino) != identity or status.st_nlink != 1:
            raise UndeclaredFile()
        _require_identity(path, identity)
        with self._lock:
            for label, binding in self._bindings.items():
                self._check_binding(label, binding)

    def _stop_attempt(self):
        return self._epoch.attempt if self._epoch is not None else None

    def _admission_closed(self):
        return self._epoch is not None and self._epoch.admission_closed

    def _require_running(self):
        if self._admission_closed():
            raise WriterUnavailable('stopped')
        if self._closed or self._worker is None:
            if self._checkpoint_only:
                self._checkpoint_reentry = True
            raise WriterUnavailable('closed')

    def _require_project_startup(self):
        if self._startup_state in {'opening', 'hooking', 'held'}:
            raise WriterUnavailable('store_held')

    def _require_served(self, project_id):
        self._require_running()
        self._require_project_startup()
        if project_id not in self._served or not self._project_available(project_id):
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
        def identity(conn):
            with closing(conn.execute('SELECT store_id FROM catalogue_identity WHERE id=1')) as cursor:
                return cursor.fetchone()[0]

        return self.read_catalogue(identity)

    @contextmanager
    def _reader(self, open_connection, check):
        reader = object()
        with self._changed:
            check()
            self._readers[reader] = None
        conn = None
        try:
            try:
                self._check_store_open()
                conn = open_connection()
            except store_layout.LayoutFault as error:
                if error.cause == 'no-writer' and self._stop_attempt() is not None:
                    raise WriterUnavailable('stopped') from error
                raise
            with self._changed:
                self._readers[reader] = conn
                check()
            yield conn
        finally:
            try:
                if conn is not None:
                    self._close_handle(conn)
            finally:
                with self._changed:
                    del self._readers[reader]
                    self._changed.notify_all()

    def read(self, project_id: str, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._epoch is None and self._store_guard is None:
            with self._lock:
                self._require_served(project_id)
            conn = store_layout.open_ledger_readonly(self._root, project_id)
            try:
                return fn(conn)
            finally:
                conn.close()
        with self._lock:
            self._require_served(project_id)
            binding = self._bindings[project_id]

        def check():
            self._require_served(project_id)
            self._check_binding(project_id, binding)

        def open_connection():
            if self._store_guard is not None:
                with self._lock:
                    check()
                    self._check_writer_admission(project_id)
            return store_layout.open_ledger_readonly(self._root, project_id)

        with self._reader(open_connection, check) as conn:
            was_reading = getattr(self._local, 'reader', False)
            self._local.reader = True
            try:
                return fn(conn)
            finally:
                self._local.reader = was_reading

    def _file_path(self, label):
        if label == 'catalogue':
            return store_layout.catalogue_path(self._root)
        return store_layout.ledger_path(self._root, label)

    def _check_binding(self, label, binding):
        conn = self._catalogue if label == 'catalogue' else self._connections.get(label)
        if self._bindings.get(label) is not binding or conn is not binding.conn:
            raise UndeclaredFile()
        _require_identity(self._file_path(label), binding.identity)

    def _require_catalogue(self):
        self._require_running()
        if self._catalogue_fault is not None:
            raise _unavailable('catalogue_faulted', self._catalogue_fault)
        if self._startup_state == 'opening' and not self._started.is_set():
            raise WriterUnavailable('store_held')
        return self._bindings['catalogue']

    def read_catalogue(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._nested():
            raise NestedWriterJob()
        with self._lock:
            binding = self._require_catalogue()
            self._check_binding('catalogue', binding)
        def open_connection():
            try:
                return store_layout.open_catalogue_readonly(self._root)
            except store_layout.LayoutFault as error:
                if error.cause == 'no-writer':
                    raise _unavailable('store_held', error) from error
                raise

        def check():
            self._require_catalogue()
            self._check_binding('catalogue', binding)

        with self._reader(open_connection, check) as conn:
            self._local.active = True
            try:
                return fn(conn)
            finally:
                self._local.active = False

    def _admit_declared(self, job):
        ledgers = dict(job.declared.ledgers)
        if (job.declared.catalogue != 'written' or len(ledgers) > 1
                or any(mode != 'read' for mode in ledgers.values())):
            raise UndeclaredFile()
        try:
            labels = store_layout.declared_order(True, ledgers)
        except (TypeError, ValueError) as error:
            raise UndeclaredFile() from error
        bindings = {'catalogue': self._require_catalogue()}
        for project in labels[1:]:
            self._require_served(project)
            bindings[project] = self._bindings[project]
        for label, binding in bindings.items():
            self._check_binding(label, binding)
        return bindings

    def start(self, *, mode: Literal['service'] | None = None,
              before_joins: Callable[[StorePort], None] | None = None,
              before_project_join: Callable[[StorePort], None] | None = None) -> None:
        if mode not in (None, 'service'):
            raise ValueError('unsupported startup mode')
        if mode is None and (before_joins is not None or before_project_join is not None):
            raise ValueError('start hooks require service mode')
        with self._lock:
            if self._worker is not None and (mode == 'service' or self._startup_state is not None):
                raise RuntimeError('writer already started')
            if self._admission_closed():
                raise WriterUnavailable('stopped')
            if self._closed:
                raise WriterUnavailable('closed')
            if self._worker is None:
                if mode == 'service':
                    self._startup_state = 'opening'
                    self._before_project_join = before_project_join
                self._worker = threading.Thread(target=self._work, name='store-writer')
                self._worker.start()
                if mode == 'service':
                    hook = threading.Thread(target=self._run_start_hook, args=(before_joins,),
                                            name='store-writer-start-hook')
                    hook.start()
                    return
        self._started.wait()
        if self._start_error is not None:
            with self._lock:
                self._closed = True
            self._worker.join()
            raise self._start_error

    def _run_start_hook(self, before_joins):
        self._started.wait()
        with self._changed:
            if self._closed or self._admission_closed() or self._start_error is not None:
                return
            self._startup_state = 'hooking'
        try:
            if before_joins is not None:
                before_joins(self)
        except BaseException:
            result = StartResult('held', ('ingest_fault',), _HeldPort(self))
        else:
            result = StartResult('serving', (), self)
        with self._changed:
            self._startup_state = result.state
            self._start_result = result
            self._changed.notify_all()

    def _wait_start_result(self, since):
        with self._changed:
            while True:
                if self._closed or self._admission_closed() or self._startup_state is None:
                    raise WriterUnavailable('closed')
                if self._start_error is not None:
                    raise self._start_error
                result = self._start_result
                if result is not None and (since is None
                                           or (result.state, result.causes) != (since.state, since.causes)):
                    return result
                self._changed.wait()

    def wait_started(self) -> StartResult:
        return self._wait_start_result(None)

    def wait_state_change(self, since: StartResult) -> StartResult:
        return self._wait_start_result(since)

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
                self._require_running()
                if request.kind in {'job', 'callee'}:
                    self._require_served(request.project_id)
                    self._check_files(request.project_id, request.job)
                elif request.kind == 'open':
                    self._require_project_startup()
                elif request.kind == 'declared':
                    if request.job.declared.ledgers:
                        self._require_project_startup()
                    request.bindings = self._admit_declared(request.job)
                self._require_running()
                self._requests[id(request)] = request
                self._queue.put(request)
            try:
                if request.kind in {'job', 'callee', 'declared'}:
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

    def submit_declared(self, job: DeclaredJob) -> JobOutcome:
        return self._call(_Request('declared', job=job))

    def _project_available(self, project):
        return (self._project_admission.get(project, 'open') == 'open'
                and project not in self._closing_projects)

    def _project_request_lock(self, project):
        if self._nested() or getattr(self._local, 'reader', False):
            raise NestedWriterJob()
        with self._lock:
            self._require_running()
            return self._project_locks.setdefault(project, threading.Lock())

    def _fence_project(self, project):
        for request in self._requests.values():
            if request is self._active_request or request.done.is_set():
                continue
            direct = request.kind in {'job', 'callee', 'open'} and request.project_id == project
            declared = request.kind == 'declared' and project in request.bindings
            if direct or declared:
                request.withdrawn = True
                request.error = WriterUnavailable('project_not_served')
                request.done.set()
                request.ready.set()

    def _post_project_request(self, kind, project):
        request = _Request(kind, project)
        request.ready.set()
        self._requests[id(request)] = request
        self._queue.put(request)

    def _post_project_stop(self, project):
        with self._lock:
            if (project in self._served and project not in self._pending_project_stop
                    and project not in self._closing_projects
                    and not self._closed and not self._admission_closed()):
                self._pending_project_stop.add(project)
                self._post_project_request('project_stop', project)

    def _record_project(self, record, project, principal):
        def run(conns):
            event = record(conns['catalogue'], 'project', project, by=principal)
            return JobOutcome(True, event)

        return self.submit_declared(DeclaredJob(store_steps.Declared('written', {}), run))

    def _project_stop_row(self, project):
        try:
            rows = self.read_catalogue(store_layout.open_stops)
        except Exception:
            return None
        return any(row.scope == 'project' and row.project_id == project for row in rows)

    def request_project_stop(self, project_id: str, principal: str) -> ProjectStopResult:
        with self._project_request_lock(project_id):
            with self._changed:
                was = self._project_admission.get(project_id, 'open')
                self._project_admission[project_id] = 'stopping'
                self._fence_project(project_id)
            try:
                outcome = self._record_project(store_layout.record_stop, project_id, principal)
            except sqlite3.IntegrityError as error:
                if (getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
                        and "index 'engine_stops_one_open'" in str(error)):
                    return 'already_stopped'
                outcome = JobOutcome(False, None)
            except Exception:
                outcome = JobOutcome(False, None)
            if outcome.commit:
                self._post_project_stop(project_id)
                return 'stopped'
            row = self._project_stop_row(project_id)
            if row is None:
                return 'not_known'
            if row:
                self._post_project_stop(project_id)
                return 'stopped' if outcome.indeterminate else 'already_stopped'
            with self._lock:
                self._project_admission[project_id] = was
            return 'not_recorded'

    def request_project_start(self, project_id: str, principal: str) -> ProjectStartResult:
        with self._project_request_lock(project_id):
            with self._lock:
                was_pending = project_id in self._pending_project_start
                self._pending_project_start.add(project_id)
                if self._project_admission.get(project_id) != 'stopping':
                    self._project_admission[project_id] = 'starting'
            try:
                outcome = self._record_project(store_layout.record_start, project_id, principal)
            except Exception:
                outcome = JobOutcome(False, None)
            if outcome.commit:
                row = False
                if outcome.value is None and not was_pending:
                    with self._lock:
                        self._pending_project_start.discard(project_id)
                        self._project_admission[project_id] = 'open'
                    return 'not_stopped'
            else:
                row = self._project_stop_row(project_id)
            with self._lock:
                if row is None:
                    self._project_admission[project_id] = 'starting'
                    return 'not_known'
                if row:
                    if not was_pending:
                        self._pending_project_start.discard(project_id)
                    self._project_admission[project_id] = 'stopping'
                    return 'still_stopped'
                self._project_admission[project_id] = 'starting'
                if not self._can_publish_start():
                    return 'started_not_served'
            if self._before_project_join is not None:
                try:
                    self._before_project_join(self)
                except BaseException:
                    return 'started_not_served'
            with self._lock:
                if not self._can_publish_start():
                    return 'started_not_served'
                self._pending_project_start.discard(project_id)
                self._project_admission[project_id] = 'open'
                self._post_project_request('open', project_id)
            return 'started'

    def _can_publish_start(self):
        return (not self._closed and not self._admission_closed()
                and self._startup_state in (None, 'serving'))

    def _project_stop(self, project):
        self._phase('project_stop')
        with self._changed:
            if self._project_admission.get(project) != 'stopping':
                return
            conn = self._connections.get(project)
            if conn is None:
                return
            self._closing_projects.add(project)
        try:
            self._close_handle(conn)
        except BaseException as error:
            with self._changed:
                self._rollback_errors[project] = error
            raise
        with self._changed:
            self._served.discard(project)
            self._connections.pop(project, None)
            self._bindings.pop(project, None)
            self._ledger_files.pop(project, None)
            self._closing_projects.discard(project)

    def _request_close(self):
        if not self._closed:
            self._closed = True
            self._changed.notify_all()
            if self._worker is not None:
                request = _Request('close')
                request.ready.set()
                self._queue.put(request)

    def close(self) -> None:
        if self._nested() or getattr(self._local, 'reader', False):
            raise NestedWriterJob()
        with self._lock:
            self._request_close()
            worker = self._worker
        if worker is not None:
            worker.join()

    def _publish_stop_outcome(self, request):
        if request.done.is_set():
            return
        if request.kind in {'job', 'declared', 'callee'}:
            uncertain = request.committing or request.kind == 'callee'
            fault = 'stop_commit_unknown' if uncertain else 'stop_unwritten'
            request.result = JobOutcome(False, None, uncertain, fault)
        else:
            request.error = WriterUnavailable('stopped')
        request.done.set()

    def fence_for_stop(self, attempt):
        with self._changed:
            active = self._active_request
            for request in self._requests.values():
                if request is not active:
                    request.withdrawn = True
                    request.error = WriterUnavailable('stopped')
                    request.done.set()
                    request.ready.set()
            self._request_close()
            connections = [conn for conn in self._readers.values() if conn is not None]
            if active is not None and active.kind == 'declared':
                connections.extend(binding.conn for binding in active.bindings.values())
            elif active is not None:
                conn = self._connections.get(active.project_id)
                if conn is not None:
                    connections.append(conn)

        interrupt = threading.Thread(target=self._interrupt, args=(connections,),
                                     name='store-stop-interrupt')
        interrupt.start()
        if active is not None:
            with self._changed:
                remaining = max(0, attempt.active - time.monotonic())
                if not self._changed.wait_for(lambda: self._active_request is not active, remaining):
                    self._publish_stop_outcome(active)
        interrupt.join(max(0, attempt.work - time.monotonic()))
        return not interrupt.is_alive()

    @staticmethod
    def _interrupt(connections):
        for conn in connections:
            try:
                conn.interrupt()
            except sqlite3.Error:
                # The original worker may have retired the handle.
                pass

    def settle_service_stop(self, attempt):
        if self._stop_attempt() is not attempt:
            return False
        worker = self._worker
        if worker is not None:
            worker.join(max(0, attempt.work - time.monotonic()))
            if worker.is_alive() or not (self._close_complete or self._start_error is not None):
                return False
        with self._changed:
            self._changed.wait_for(lambda: not self._readers, max(0, attempt.work - time.monotonic()))
            return not self._readers and not self._failed_closes

    def settle_private_io(self, deadline, *, checkpoint=False):
        with self._changed:
            if checkpoint:
                self._checkpoint_only = True
            self._request_close()
            worker = self._worker
        if worker is not None:
            worker.join(None if deadline is None else max(0, deadline - time.monotonic()))
            if worker.is_alive() or not (self._close_complete or self._start_error is not None):
                return False
        with self._changed:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            return self._changed.wait_for(lambda: not self._readers, remaining) and not self._failed_closes

    def _close_handle(self, conn):
        try:
            conn.close()
        except BaseException:
            with self._lock:
                self._failed_closes.append(conn)
            raise

    def _permit_commit(self):
        while True:
            with self._lock:
                stopped = self._admission_closed()
                attempt = self._stop_attempt()
                if not stopped or attempt is not None:
                    if attempt is not None and time.monotonic() >= attempt.active:
                        self._publish_stop_outcome(self._active_request)
                        return False
                    self._active_request.committing = True
                    break
            self._epoch.wait_for_attempt()
        if self._epoch is not None:
            self._phase('commit_permitted')
        return True

    def _guarded_failed_close(self, project_id, conn, failure):
        with self._lock:
            self._failed_closes.append(conn)
            self._rollback_errors[project_id] = failure

    def _close_guarded_open(self, project_id, conn, original):
        try:
            conn.close()
        except BaseException as failure:
            self._guarded_failed_close(project_id, conn, failure)
            original.add_note('cleanup close failed: ' + type(failure).__name__)

    def _open_guarded(self, project_id, *, purpose):
        guard, arm = self._store_guard, self._store_arm
        self._check_store_open()
        members = guard.runtime_members(arm, project_id, phase='initial')
        guard.runtime_admission(arm, project_id, self._catalogue, purpose=purpose)

        def check():
            self._check_store_open()
            if guard.runtime_members(arm, project_id, phase='connected') != members:
                raise store_layout.LayoutFault('member-changed')
            guard.runtime_admission(arm, project_id, self._catalogue, purpose=purpose)

        def on_failed_close(conn, failure):
            self._guarded_failed_close(project_id, conn, failure)

        conn = store_layout.open_ledger_runtime(self._root, project_id, check=check,
                                                on_failed_close=on_failed_close)
        try:
            if restore_cancel.held(conn, project_id) is not None:
                raise store_layout.LayoutFault('incumbent-hold')
            conn.isolation_level = None
            self._check_store_open()
        except BaseException as original:
            self._close_guarded_open(project_id, conn, original)
            raise
        return conn, members

    def _open(self, project_id):
        with self._lock:
            if project_id in self._rollback_errors:
                raise _unavailable('rollback_failed', self._rollback_errors[project_id])
        guarded = self._store_guard is not None
        if not guarded and any(row.scope == 'store' or row.project_id == project_id
                               for row in store_layout.open_stops(self._catalogue)):
            with self._lock:
                self._project_admission[project_id] = 'stopping'
            return
        with self._lock:
            if not self._project_available(project_id):
                raise WriterUnavailable('project_not_served')
        if project_id not in self._connections:
            path = self._file_path(project_id)
            members = None
            self._check_store_open()
            if guarded:
                try:
                    conn, members = self._open_guarded(project_id, purpose='join')
                except store_layout.LayoutFault as failure:
                    if failure.cause == 'close-unresolved':
                        raise _unavailable('rollback_failed', failure) from failure
                    raise
                identity = members[-1]
            else:
                try:
                    identity = _identity(path)
                except FileNotFoundError:
                    raise store_layout.LayoutFault('missing') from None
                conn = store_layout.open_ledger(self._root, project_id)
            try:
                conn.isolation_level = None
                _require_identity(path, identity)
                path = os.path.realpath(path)
            except BaseException as original:
                if guarded:
                    self._close_guarded_open(project_id, conn, original)
                else:
                    self._close_handle(conn)
                raise
            with self._lock:
                if self._project_available(project_id):
                    self._connections[project_id] = conn
                    self._ledger_files[project_id] = path
                    self._bindings[project_id] = _FileBinding(conn, identity)
                    if members is not None:
                        self._project_members[project_id] = members
                    self._served.add(project_id)
                    return
            if guarded:
                refusal = WriterUnavailable('project_not_served')
                self._close_guarded_open(project_id, conn, refusal)
                raise refusal
            try:
                self._close_handle(conn)
            except BaseException as error:
                with self._lock:
                    self._rollback_errors[project_id] = error
                raise _unavailable('rollback_failed', error) from error
            raise WriterUnavailable('project_not_served')

    def _rollback(self, original=None):
        failure = None
        try:
            self._conn.execute('ROLLBACK').close()
        except BaseException as error:
            failure = error
        if failure is not None:
            with self._lock:
                self._served.discard(self._active_project)
                self._bindings.pop(self._active_project, None)
                self._rollback_errors[self._active_project] = failure
            conn, self._conn = self._connections.pop(self._active_project), None
            self._close_handle(conn)
            try:
                strings.emit('stderr', 'store-writer-rollback-failed')
            except Exception:
                pass
            try:
                strings.report_diagnostic(failure)
            except Exception:
                pass
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
                if not self._permit_commit():
                    failure = self._rollback()
                    return JobOutcome(False, None, False, 'stop_unwritten'), failure
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
            if self._epoch is not None and self._active_request.committing:
                return JobOutcome(False, original, True, 'commit_unknown'), None
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

    def _require_declared_bindings(self, bindings):
        # A queued request can reach the worker after a prior turn fenced a file.
        if self._catalogue_fault is not None:
            raise _unavailable('catalogue_faulted', self._catalogue_fault)
        for label, binding in bindings.items():
            if label in self._rollback_errors:
                raise _unavailable('rollback_failed', self._rollback_errors[label])
            self._check_binding(label, binding)

    def _retire_declared_file(self, label, conn, failure):
        with self._lock:
            self._bindings.pop(label)
            if label == 'catalogue':
                self._catalogue = None
                self._catalogue_fault = failure
            else:
                self._served.discard(label)
                self._rollback_errors[label] = failure
                self._connections.pop(label)
        try:
            self._close_handle(conn)
        except Exception as close_error:
            failure.add_note(f'{label}: connection close failed: {type(close_error).__name__}')

    def _finish_declared(self, conns, original, read_label=None):
        failures = dict(store_layout.rollback_declared(conns))
        if read_label is not None and read_label not in failures:
            try:
                conns[read_label].execute('PRAGMA query_only=OFF').close()
            except BaseException as error:
                failures[read_label] = error
        for label, failure in failures.items():
            self._retire_declared_file(label, conns[label], failure)
            if original is not None:
                original.add_note(
                    f'{label}: declared cleanup failed: {type(failure).__name__} '
                    f'{getattr(failure, "sqlite_errorname", None)}'
                )
        return next(iter(failures.values()), None)

    def _run_declared(self, request):
        self._phase('dequeued')
        bindings = request.bindings
        self._require_declared_bindings(bindings)
        conns = {label: binding.conn for label, binding in bindings.items()}
        read_label = next((label for label in conns if label != 'catalogue'), None)
        try:
            store_layout.begin_declared(conns, root=self._root)
        except BaseException as error:
            self._finish_declared(conns, error)
            for label, binding in bindings.items():
                _require_identity(self._file_path(label), binding.identity)
            if (isinstance(error, sqlite3.OperationalError)
                    and getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_BUSY):
                raise _unavailable('busy', error) from error
            raise

        original = None
        outcome = None
        try:
            if read_label is not None:
                conns[read_label].execute('PRAGMA query_only=ON').close()
            self._phase('began')
            self._require_declared_bindings(bindings)
            outcome = request.job.run(MappingProxyType(conns))
            if not isinstance(outcome, JobOutcome):
                raise TypeError('declared job must return JobOutcome')
            self._phase('finishing')
        except BaseException as error:
            original = error

        try:
            ended_early = any(not conn.in_transaction for conn in conns.values())
        except sqlite3.ProgrammingError as error:
            # A callback also ends its transaction if it closes the handle.
            if original is None:
                original = error
            ended_early = True
        if ended_early:
            value = original if original is not None else outcome.value
            failure = self._finish_declared(conns, original, read_label)
            fault = 'job_ended_transaction' if failure is None else 'rollback_failed'
            return JobOutcome(False, value, True, fault)

        committed = False
        if original is None and outcome.commit and not outcome.indeterminate:
            try:
                if self._permit_commit():
                    conns['catalogue'].execute('COMMIT').close()
                    committed = True
                else:
                    outcome = JobOutcome(False, None, False, 'stop_unwritten')
            except BaseException as error:
                original = error

        failure = self._finish_declared(conns, original, read_label)
        if original is not None:
            if self._epoch is not None and request.committing:
                return JobOutcome(False, original, True, 'commit_unknown')
            raise original
        if failure is not None:
            if committed:
                return replace(outcome, fault='rollback_failed')
            raise _unavailable('rollback_failed', failure) from failure
        return outcome

    def _work(self):
        try:
            path = store_layout.catalogue_path(self._root)
            if not path.exists() or path.stat().st_size == 0:
                raise store_layout.LayoutFault('missing')
            identity = _identity(path)
            self._check_store_open()
            if self._store_guard is None:
                conn = store_layout.open_catalogue(self._root)
            else:
                arm = self._store_arm
                conn = store_layout._open_catalogue(self._root, None, admission=(arm.store, arm.project),
                                                    check_admission=self._check_writer_admission)
            try:
                _require_identity(path, identity)
            except BaseException:
                self._close_handle(conn)
                raise
            self._catalogue = conn
            self._bindings['catalogue'] = _FileBinding(conn, identity)
        except BaseException as error:
            self._start_error = error
            return
        finally:
            with self._changed:
                self._started.set()
                self._changed.notify_all()
        while True:
            request = self._queue.get()
            request.ready.wait()
            result = error = None
            try:
                if request.kind == 'close':
                    # The last SQLite handle must be writable to retire WAL and SHM.
                    with self._changed:
                        self._changed.wait_for(lambda: not self._readers)
                    self._close_files()
                    return
                with self._changed:
                    if request.withdrawn:
                        continue
                    if self._admission_closed():
                        request.error = WriterUnavailable('stopped')
                        continue
                    self._active_request = request
                if request.kind == 'open':
                    self._open(request.project_id)
                elif request.kind == 'project_stop':
                    self._project_stop(request.project_id)
                elif request.kind == 'declared':
                    result = self._run_declared(request)
                else:
                    self._active_project = request.project_id
                    self._conn = self._connections.get(request.project_id)
                    run = self._run_callee if request.kind == 'callee' else self._run_job
                    result, error = run(request.job)
            except BaseException as caught:
                error = caught
                if request.kind == 'close':
                    return
            finally:
                with self._changed:
                    if self._active_request is request:
                        attempt = self._stop_attempt()
                        if attempt is not None and time.monotonic() >= attempt.active:
                            self._publish_stop_outcome(request)
                        self._active_request = None
                    if not request.done.is_set():
                        if request.error is None:
                            request.result, request.error = result, error
                        request.done.set()
                    self._requests.pop(id(request), None)
                    if request.kind == 'project_stop':
                        self._pending_project_stop.discard(request.project_id)
                    self._changed.notify_all()
                self._queue.task_done()

    def _close_files(self):
        for project_id, conn in tuple(self._connections.items()):
            self._close_handle(conn)
            del self._connections[project_id]
        if self._catalogue is not None:
            self._close_handle(self._catalogue)
        self._catalogue = None
        self._conn = None
        with self._lock:
            self._served.clear()
            self._bindings.clear()
            self._close_complete = True
