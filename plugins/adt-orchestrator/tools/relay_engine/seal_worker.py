"""Nonblocking seal reports, condition recovery and catalogue-owned rechecks."""
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import queue as channels
import threading

from . import seal_slots as slots, store_layout, strings
from .api_principals import ServicePrincipal
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
    older: list = field(default_factory=list)

    @property
    def key(self):
        return {'chain_id': self.chain or 'none', 'cause': self.cause}


class SealWorker:
    def __init__(self, service, *, channel, store_root, queue, refusal_type,
                 unavailable_types, seal_texts, backup_texts, backup_pins,
                 open_catalogue_reader, route_state, project_routes, clock_ms,
                 tick, wait, bound_s, on_fault_ended, backup_exclusion):
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
        self._wake, self._check, self._stopping = True, False, False
        self._idle, self._stopped = threading.Event(), threading.Event()
        self._thread, self._ticker, self._error = None, None, None
        self._faults, self._clear_pending = {}, []
        self._listed_gen, self._recover = None, set()
        self._backup_read, self._backup_item = False, None
        self._backup_raised = False
        self.channel._notify = self._notify

    def _notify(self):
        with self._condition:
            self._wake = True
            self._idle.clear()
            self._condition.notify_all()

    def start(self):
        if self._thread is not None:
            raise RuntimeError('seal_worker_already_started')
        self._thread = threading.Thread(target=self._own, name='seal-worker', daemon=True)
        self._ticker = threading.Thread(target=self._ticks, name='seal-worker-tick', daemon=True)
        self._thread.start(); self._ticker.start()

    def stop(self):
        with self._condition:
            self._stopping = True
        self._notify()
        if self._thread is not None:
            self._thread.join()
        if self._ticker is not None:
            self._ticker.join()
        self.channel._notify = None

    def catalogue_opened(self):
        with self._condition:
            self._generation += 1
            self._leave = True
            self._check = True
        self._notify()

    def catalogue_closing(self):
        done = threading.Event()
        with self._condition:
            generation = self._generation
            self._leave = False
            self._generation += 1
            self._closing.append((generation, done))
            reader = self._reader
        self._notify()
        if not self.wait(done, self.bound_s / 2):
            if reader is not None:
                reader.interrupt()
            self.wait(done, self.bound_s / 2)

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

    def _reader_boundary(self):
        with self._condition:
            stale = self._reader is not None and (not self._leave or self._reader_gen != self._generation)
        if stale:
            self._reader.close()
            with self._condition:
                self._reader, self._reader_gen = None, None
        with self._condition:
            # This executes on the owner, hence an in-flight open/read has ended.
            for generation, done in self._closing:
                if self._reader_gen != generation:
                    done.set()
            self._closing = [(g,d) for g,d in self._closing if not d.is_set()]
            leave, generation = self._leave, self._generation
            missing = self._reader is None
        if leave and missing:
            reader = self.open_catalogue_reader(self.store_root)
            if not self._valid(generation):
                reader.close()
                return
            with self._condition:
                self._reader, self._reader_gen = reader, generation
                self._listed_gen = None

    def _read(self, call, *args):
        self._reader_boundary()
        with self._condition:
            generation = self._reader_gen
            if self._reader is None or not self._leave or generation != self._generation:
                return None
            reader = self._reader
            self._inflight = generation
        try:
            result = call(reader, *args)
        except Exception:
            result = None
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
        new = _Fault(project, chain, cause, at_ms, item_id)
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
        if fault.cause == 'pointer_unset' and self.clock_ms() - fault.first < 60000:
            return
        args = dict(kind='notice', subkind='approval_seal_unusable', project_id=fault.project,
                    condition_key=fault.key, pins=fault.key,
                    texts=self.seal_texts(fault.cause,fault.chain or 'none'),
                    operation_key=':'.join((fault.project,fault.chain or 'none',fault.cause,str(fault.first))))
        result = self._call(fault.project,self.queue.run,'queue.raise',args,self.principal)
        if result is not None:
            fault.item_id = result['item_id']
        if fault.item_id is not None:
            older, fault.older = fault.older, []
            for old in older:
                self._clear(old, generation)

    def _clear(self, fault, generation):
        if fault.item_id is None:
            return
        if generation is not None and not self._valid(generation):
            return
        result = self._call(fault.project,self.queue.clear_condition,self.principal,fault.item_id,
                            'notice','approval_seal_unusable',fault.project,fault.key)
        if result is None and all(f.item_id != fault.item_id for f in self._clear_pending):
            self._clear_pending.append(fault)

    def _end(self, fault, generation):
        if generation is not None and not self._valid(generation):
            return
        for old in fault.older:
            self._clear(old, generation)
        self._clear(fault, generation)
        self._faults.pop(fault.project, None)
        self._publish()
        if generation is not None and not self._valid(generation):
            return
        try:
            self.on_fault_ended(fault.project, fault.cause)
        except Exception as exc:
            strings.report_diagnostic(SealCalleeFailed('on_fault_ended',type(exc).__name__,fault.project))

    def _recover_items(self):
        if not self._backup_read:
            rows = self._call(None,self.queue.open_condition_items,self.principal,None)
            if rows is not None:
                self._backup_read = True
                self._backup_item = next((r.item_id for r in rows if r.kind == 'notice' and
                                          r.subkind == 'approval_seal_backup_included' and
                                          r.condition_key == 'backup_exclusion'),None)
        if self._listed_gen != self._generation:
            listed = self._read(self.project_routes)
            if listed is not None and listed[0] is not None:
                routes, generation = listed
                self._recover.update(r.project_id for r in routes)
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
            self._recover.discard(project)
            for row in rows:
                if row.kind == 'notice' and row.subkind == 'approval_seal_unusable':
                    key = row.condition_key
                    self._learn(project,None if key['chain_id']=='none' else key['chain_id'],
                                key['cause'],self.clock_ms(),row.item_id,recovered=True)

    def _backup(self):
        if self.backup_exclusion == 'excluded':
            if self._backup_read and self._backup_item is not None:
                result = self._call(None,self.queue.clear_condition,self.principal,self._backup_item,
                                    'notice','approval_seal_backup_included',None,'backup_exclusion')
                if result is not None:
                    self._backup_item = None
        elif not self._backup_raised:
            args = dict(kind='notice',subkind='approval_seal_backup_included',project_id=None,
                        condition_key='backup_exclusion',texts=self.backup_texts(),pins=self.backup_pins(),
                        operation_key='backup_exclusion:'+str(self.clock_ms()))
            result = self._call(None,self.queue.run,'queue.raise',args,self.principal)
            if result is not None:
                self._backup_raised = True
                self._backup_item = result['item_id']

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
                self._reader_boundary()
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
                        if route is not None and route[0] is not None:
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
            if self._reader is not None:
                self._reader.close()
                self._reader = None
            for _,done in self._closing:
                done.set()
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

    def ledger_closing(self, project_id):
        self.renderer.ledger_closing(project_id)

    def catalogue_closing(self):
        self.worker.catalogue_closing()
