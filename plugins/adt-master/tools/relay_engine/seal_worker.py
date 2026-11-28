"""Nonblocking seal reports, condition recovery and catalogue-owned rechecks."""
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import queue as channels
import threading
from time import monotonic
from collections.abc import Callable
import uuid

from . import seal_slots as slots, store_layout, strings
from .api_principals import ServicePrincipal
from .api_writer import StopDeadline
from .seal_types import SealReportDropped, SealCalleeFailed, POINTER_UNREADABLE


class ReportChannel:
    def __init__(self, size: int = 1024):
        self._queue = channels.Queue(maxsize=size)
        self._dropped = set()
        self._notify = None

    def offer(self, report) -> None:
        try:
            self._queue.put_nowait(report)
        except channels.Full:
            key = (report.project_id, report.cause)
            if key not in self._dropped:
                self._dropped.add(key)
                strings.report_diagnostic(SealReportDropped(*key))
        else:
            self._dropped.clear()
        if self._notify is not None:
            self._notify()


@dataclass
class _Fault:
    project: str
    chain: str | None
    cause: str
    first: int
    item_id: str | None = None
    occurrence: str | None = None
    older: list = field(default_factory=list)
    cleared: bool = False

    @property
    def key(self):
        return {'chain_id': self.chain or 'none', 'cause': self.cause}


def _new_occurrence_id() -> str:
    return uuid.uuid4().hex


class SealWorker:
    def __init__(self, service, *, channel, store_root, queue, refusal_type,
                 unavailable_types, seal_texts, backup_texts, backup_pins,
                 open_catalogue_reader, route_state, project_routes, clock_ms,
                 tick, wait, bound_s, on_fault_ended, backup_exclusion,
                 new_occurrence_id: Callable[[], str] = _new_occurrence_id):
        if backup_exclusion not in ('excluded', 'add_failed', 'check_failed', 'not_excluded'):
            raise ValueError('invalid_backup_exclusion')
        self.service, self.channel, self.store_root = service, channel, Path(store_root)
        self.queue, self.refusal_type, self.unavailable_types = queue, refusal_type, unavailable_types
        self.seal_texts, self.backup_texts, self.backup_pins = seal_texts, backup_texts, backup_pins
        self.open_catalogue_reader, self.route_state, self.project_routes = open_catalogue_reader, route_state, project_routes
        self.clock_ms, self.tick, self.wait, self.bound_s = clock_ms, tick, wait, bound_s
        self.on_fault_ended, self.backup_exclusion = on_fault_ended, backup_exclusion
        self.principal = ServicePrincipal('approval_seal')
        self._condition = threading.Condition()
        self._leave, self._generation = False, 0
        self._reader, self._reader_gen, self._inflight = None, None, None
        self._closing = []
        self._catalogue_close_late = set()
        # One outstanding count per acquired reader, including stale candidates.
        self._unacknowledged = {}
        self._wake, self._check, self._stopping = True, False, False
        self._idle, self._stopped = threading.Event(), threading.Event()
        self._thread, self._ticker, self._error = None, None, None
        self._faults, self._clear_pending = {}, []
        self._listed_gen, self._recover = None, set()
        self._reader_failed = False
        self._backup_read, self._backup_item = False, None
        self._backup_raised = False
        self.new_occurrence_id = new_occurrence_id
        self._backup_occurrence = None
        self.channel._notify = self._notify

    def _notify(self):
        with self._condition:
            self._wake = True
            self._idle.clear()
            self._condition.notify_all()

    def start(self):
        if self._thread is not None:
            raise RuntimeError('seal_worker_already_started')
        owner = threading.Thread(target=self._own, name='seal-worker', daemon=True)
        ticker = threading.Thread(target=self._ticks, name='seal-worker-tick', daemon=True)
        owner.start()
        self._thread = owner
        ticker.start()
        self._ticker = ticker

    def stop(self, deadline: float | None = None) -> bool:
        with self._condition:
            self._stopping = True
        self._notify()
        threads = tuple(thread for thread in (self._thread, self._ticker) if thread is not None)
        for thread in threads:
            thread.join(None if deadline is None else max(0, deadline - monotonic()))
        self.channel._notify = None
        with self._condition:
            return not any(thread.is_alive() for thread in threads) and not self._unacknowledged

    def catalogue_opened(self):
        with self._condition:
            self._generation += 1
            self._leave = True
            self._check = True
        self._notify()

    def _wait_closing_half(self, done, deadline):
        seconds = self.bound_s / 2
        end = deadline.at() if deadline is not None else None
        if end is not None:
            seconds = min(seconds, end - monotonic())
        return seconds > 0 and self.wait(done, seconds)

    def catalogue_closing(self, *, deadline: StopDeadline | None = None) -> bool:
        done = threading.Event()
        with self._condition:
            generation = self._generation
            self._leave = False
            self._generation += 1
            self._closing.append((generation, done))
        self._notify()
        if not self._wait_closing_half(done, deadline):
            with self._condition:
                reader = self._reader
                if reader is not None and self._reader_gen == generation and not done.is_set():
                    reader.interrupt()
            self._wait_closing_half(done, deadline)
        with self._condition:
            confirmed = done.is_set() and generation not in self._unacknowledged
            if not confirmed:
                self._catalogue_close_late.add(generation)
            return confirmed

    def _ticks(self):
        while self.tick(60):
            if self._stopped.is_set():
                return
            with self._condition:
                self._check = True
            self._notify()

    def _valid(self, generation):
        with self._condition:
            return self._leave and generation == self._generation

    def _close_reader(self, reader, generation):
        try:
            reader.close()
        except Exception as exc:
            self._reader_failed = True
            strings.report_diagnostic(SealCalleeFailed('catalogue', type(exc).__name__, None))
        else:
            with self._condition:
                remaining = self._unacknowledged[generation] - 1
                if remaining:
                    self._unacknowledged[generation] = remaining
                else:
                    del self._unacknowledged[generation]

    def _signal_catalogue_closings(self):
        # Owner-only, with the condition held and no open/read still in flight.
        for generation, done in self._closing:
            if self._reader_gen != generation:
                done.set()
        self._closing = [(g, done) for g, done in self._closing if not done.is_set()]

    def _reader_boundary(self):
        with self._condition:
            stale = self._reader is not None and (not self._leave or self._reader_gen != self._generation)
            if stale:
                reader, reader_gen = self._reader, self._reader_gen
                self._reader, self._reader_gen = None, None
        if stale:
            self._close_reader(reader, reader_gen)
        with self._condition:
            self._signal_catalogue_closings()
            leave, generation = self._leave, self._generation
            missing = self._reader is None
        if self._reader_failed:
            return False
        if leave and missing:
            try:
                reader = self.open_catalogue_reader(self.store_root)
            except Exception as exc:
                self._reader_failed = True
                strings.report_diagnostic(SealCalleeFailed('catalogue', type(exc).__name__, None))
                return False
            with self._condition:
                self._unacknowledged[generation] = self._unacknowledged.get(generation, 0) + 1
                if self._leave and generation == self._generation:
                    self._reader, self._reader_gen = reader, generation
                    self._listed_gen = None
                    return True
            self._close_reader(reader, generation)
            with self._condition:
                self._signal_catalogue_closings()
            return False
        return True

    def _read(self, call, *args):
        if not self._reader_boundary():
            return None
        with self._condition:
            generation = self._reader_gen
            if self._reader is None or not self._leave or generation != self._generation:
                return None
            reader = self._reader
            self._inflight = generation
        try:
            result = call(reader, *args)
        except Exception as exc:
            project = args[0] if args else None
            strings.report_diagnostic(SealCalleeFailed('catalogue', type(exc).__name__, project))
            self._reader_failed = True
            with self._condition:
                self._reader, self._reader_gen = None, None
            self._close_reader(reader, generation)
            return None
        finally:
            with self._condition:
                self._inflight = None
        if not self._valid(generation):
            return None
        return result, generation

    def _call(self, project, method, *args):
        try:
            result = method(*args)
            return None if isinstance(result, self.refusal_type) else result
        except self.unavailable_types:
            return None
        except Exception as exc:
            strings.report_diagnostic(SealCalleeFailed('queue', type(exc).__name__, project))
            return None

    def _publish(self):
        self.service.publish_mismatch_projects(frozenset(p for p,f in self._faults.items()
                                                       if f.cause == 'ledger_chain_mismatch'))

    def _learn(self, project, chain, cause, at_ms, item_id=None, *, recovered=False):
        old = self._faults.get(project)
        if old is not None and (old.chain,old.cause) == (chain,cause):
            if item_id is not None:
                old.item_id = item_id
            return old
        new = _Fault(project, chain, cause, at_ms, item_id,
                     occurrence=self.new_occurrence_id())
        if old is not None:
            if recovered:
                if item_id and all(f.item_id != item_id for f in [old]+old.older):
                    old.older.append(new)
                return old
            new.older = old.older + [old]
            old.older = []
        self._faults[project] = new
        self._publish()
        return new

    def _raise(self, fault, generation):
        if fault.item_id is not None or not self._valid(generation):
            return
        if fault.cleared:
            fault.first = self.clock_ms()
            fault.occurrence = self.new_occurrence_id()
            fault.cleared = False
        if fault.cause == 'pointer_unset' and self.clock_ms() - fault.first < 60000:
            return
        try:
            texts = self.seal_texts(fault.cause, fault.chain or 'none')
        except Exception as exc:
            strings.report_diagnostic(SealCalleeFailed('texts', type(exc).__name__, fault.project))
            return
        args = dict(kind='notice', subkind='approval_seal_unusable', project_id=fault.project,
                    condition_key=fault.key, pins=fault.key,
                    texts=texts,
                    operation_key=':'.join((fault.project,fault.chain or 'none',fault.cause,fault.occurrence)))
        result = self._call(fault.project,self.queue.run,'queue.raise',args,self.principal)
        if result is not None:
            try:
                item_id = result['item_id']
            except Exception as exc:
                strings.report_diagnostic(SealCalleeFailed('queue', type(exc).__name__, fault.project))
                return
            fault.item_id = item_id
        if fault.item_id is not None:
            older, fault.older = fault.older, []
            for old in older:
                self._clear(old, generation)

    def _clear(self, fault, generation):
        if fault.item_id is None:
            return
        if generation is not None and not self._valid(generation):
            if all(f.item_id != fault.item_id for f in self._clear_pending):
                self._clear_pending.append(fault)
            return
        if any(other is not fault and other.item_id == fault.item_id
               for current in self._faults.values() for other in [current, *current.older]):
            fault.item_id = None
            fault.cleared = True
            return
        result = self._call(fault.project,self.queue.clear_condition,self.principal,fault.item_id,
                            'notice','approval_seal_unusable',fault.project,fault.key)
        if result is not None:
            fault.item_id = None
            fault.cleared = True
        elif all(f.item_id != fault.item_id for f in self._clear_pending):
            self._clear_pending.append(fault)

    def _end(self, fault, generation):
        if generation is not None and not self._valid(generation):
            return
        for old in fault.older:
            self._clear(old, generation)
        self._clear(fault, generation)
        if generation is not None and not self._valid(generation):
            return
        try:
            self.on_fault_ended(fault.project, fault.cause)
        except Exception as exc:
            strings.report_diagnostic(SealCalleeFailed('on_fault_ended',type(exc).__name__,fault.project))
        self._faults.pop(fault.project, None)
        self._publish()

    def _recover_items(self):
        if not self._backup_read:
            rows = self._call(None,self.queue.open_condition_items,self.principal,None)
            if rows is not None:
                try:
                    item = next((r.item_id for r in rows if r.kind == 'notice' and
                                 r.subkind == 'approval_seal_backup_included' and
                                 r.condition_key == 'backup_exclusion'), None)
                except Exception as exc:
                    strings.report_diagnostic(SealCalleeFailed('queue', type(exc).__name__, None))
                else:
                    self._backup_item = item
                    self._backup_read = True
        if self._listed_gen != self._generation:
            listed = self._read(lambda reader: None if (routes := self.project_routes(reader)) is None
                                else tuple(row.project_id for row in routes))
            if listed is not None and listed[0] is not None:
                projects, generation = listed
                self._recover.update(projects)
                self._listed_gen = generation
        for project in sorted(self._recover):
            read = self._read(self.route_state, project)
            if read is None or read[0] is None:
                continue
            _, generation = read
            if not self._valid(generation):
                continue
            rows = self._call(project,self.queue.open_condition_items,self.principal,project)
            if rows is None or not self._valid(generation):
                continue
            try:
                recovered = [(None if row.condition_key['chain_id'] == 'none' else row.condition_key['chain_id'],
                              row.condition_key['cause'], row.item_id)
                             for row in rows if row.kind == 'notice' and row.subkind == 'approval_seal_unusable']
            except Exception as exc:
                strings.report_diagnostic(SealCalleeFailed('queue', type(exc).__name__, project))
                continue
            self._recover.discard(project)
            for chain, cause, item in recovered:
                self._learn(project,chain,cause,self.clock_ms(),item,recovered=True)

    def _backup(self):
        if self.backup_exclusion == 'excluded':
            if self._backup_read and self._backup_item is not None:
                result = self._call(None,self.queue.clear_condition,self.principal,self._backup_item,
                                    'notice','approval_seal_backup_included',None,'backup_exclusion')
                if result is not None:
                    self._backup_item = None
        elif not self._backup_raised:
            if self._backup_occurrence is None:
                self._backup_occurrence = self.new_occurrence_id()
            try:
                texts = self.backup_texts()
                pins = self.backup_pins()
            except Exception as exc:
                strings.report_diagnostic(SealCalleeFailed('texts', type(exc).__name__, None))
                return
            args = dict(kind='notice',subkind='approval_seal_backup_included',project_id=None,
                        condition_key='backup_exclusion',texts=texts,pins=pins,
                        operation_key='backup_exclusion:'+self._backup_occurrence)
            result = self._call(None,self.queue.run,'queue.raise',args,self.principal)
            if result is not None:
                try:
                    item_id = result['item_id']
                except Exception as exc:
                    strings.report_diagnostic(SealCalleeFailed('queue', type(exc).__name__, None))
                    return
                self._backup_raised = True
                self._backup_item = item_id

    def _observe(self, fault):
        read = self._read(self.route_state, fault.project)
        if read is None or read[0] is None:
            return None
        route, generation = read
        if fault.cause == 'pointer_unset' and route != 'active':
            return 'healthy',None,generation
        ledger = None
        try:
            ledger = os.open(store_layout.ledger_path(self.store_root,fault.project),
                             os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
            identity = slots.file_identity(ledger)
        except OSError:
            if ledger is not None:
                os.close(ledger)
            return None
        try:
            project = self.service.folder._project(fault.project)
            try:
                os.listdir(project)
                if not self.service.folder.project_usable(fault.project):
                    raise OSError('unusable_project')
                lock = os.open(project/'lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
                try:
                    fcntl.flock(lock,fcntl.LOCK_SH)
                    try:
                        pointer = self.service._pointer_or_damage(fault.project)
                        if pointer is POINTER_UNREADABLE:
                            return 'pointer_damaged',None,generation
                        if pointer is None:
                            return 'pointer_unset',None,generation
                        state = 'healthy'
                        for kind,suffix,cause in ((slots.ItemKind.COUNTER,'.counter','counter_unusable'),
                                                  (slots.ItemKind.HIGH_WATER,'.high-water','high_water_unusable')):
                            fd = None
                            try:
                                fd = os.open(project/(pointer+suffix),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
                                slots.read_slots(fd,kind=kind,binding=(fault.project,pointer),
                                                 ledger_identity=identity if kind==slots.ItemKind.COUNTER else None)
                            except (OSError,slots.CannotRecord):
                                state = cause
                                break
                            finally:
                                if fd is not None: os.close(fd)
                        return state,pointer,generation
                    finally:
                        fcntl.flock(lock,fcntl.LOCK_UN)
                finally:
                    os.close(lock)
            except OSError:
                return 'folder_unusable',None,generation
        finally:
            os.close(ledger)

    def _recheck(self, fault):
        observed = self._observe(fault)
        if observed is None:
            return
        state, chain, generation = observed
        if not self._valid(generation):
            return
        if state == 'folder_unusable':
            if fault.cause == 'pointer_damaged':
                fault = self._learn(fault.project,None,state,self.clock_ms())
            self._raise(fault,generation)
            return
        if state == 'pointer_damaged':
            if fault.cause == 'folder_unusable':
                fault = self._learn(fault.project,None,state,self.clock_ms())
            self._raise(fault,generation)
            return
        if fault.cause == 'folder_unusable' or fault.cause == 'pointer_damaged':
            self._end(fault,generation)
        elif fault.cause == 'pointer_unset':
            if state == 'healthy':
                self._end(fault,generation)
            elif state == 'pointer_unset':
                self._raise(fault,generation)
        elif chain != fault.chain:
            self._end(fault,generation)
        elif fault.cause != 'ledger_chain_mismatch' and state == 'healthy':
            self._end(fault,generation)
        else:
            self._raise(fault,generation)

    def _own(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda:self._wake or self._stopping)
                    self._wake = False
                    check, self._check = self._check, False
                    stopping = self._stopping
                self._reader_failed = False
                if not self._reader_boundary():
                    with self._condition:
                        if not self._wake:
                            self._idle.set()
                    if stopping:
                        break
                    continue
                if stopping:
                    break
                self._recover_items()
                self._backup()
                while True:
                    try:
                        report = self.channel._queue.get_nowait()
                    except channels.Empty:
                        break
                    if report.cause == 'recorded':
                        fault = self._faults.get(report.project_id)
                        if fault is not None and fault.cause == 'ledger_chain_mismatch':
                            self._end(fault,None)
                    elif report.cause is None:
                        check = True
                    else:
                        fault = self._learn(report.project_id,report.chain_id,report.cause,report.at_ms)
                        route = self._read(self.route_state,report.project_id)
                        if (route is not None and route[0] is not None and
                                (report.cause != 'pointer_unset' or route[0] == 'active')):
                            self._raise(fault,route[1])
                if check:
                    for fault in list(self._faults.values()):
                        self._reader_boundary()
                        self._recheck(fault)
                    pending, self._clear_pending = self._clear_pending, []
                    for fault in pending:
                        route = self._read(self.route_state,fault.project)
                        if route is None or route[0] is None:
                            self._clear_pending.append(fault)
                        else:
                            self._clear(fault,route[1])
                self._reader_boundary()
                with self._condition:
                    if not self._wake:
                        self._idle.set()
        except BaseException as exc:
            self._error = exc
        finally:
            with self._condition:
                reader, generation = self._reader, self._reader_gen
                self._reader, self._reader_gen = None, None
            try:
                if reader is not None:
                    self._close_reader(reader, generation)
            finally:
                with self._condition:
                    self._signal_catalogue_closings()
                self._stopped.set()
                self._idle.set()


class SealHooks:
    def __init__(self, renderer, worker):
        self.renderer, self.worker = renderer, worker

    def catalogue_opened(self):
        self.worker.catalogue_opened()

    def ledger_opened(self, project_id):
        self.renderer.ledger_opened(project_id)

    def ledger_serving(self, project_id):
        self.renderer.ledger_serving(project_id)

    def ledger_closing(self, project_id, *, deadline: StopDeadline | None = None) -> bool:
        return self.renderer.ledger_closing(project_id, deadline=deadline)

    def catalogue_closing(self, *, deadline: StopDeadline | None = None) -> bool:
        return self.worker.catalogue_closing(deadline=deadline)
