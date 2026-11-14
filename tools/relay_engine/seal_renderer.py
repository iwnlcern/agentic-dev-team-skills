"""Per-generation ledger owners and the approval seal's durable renderer."""
from collections import deque
from dataclasses import dataclass, field
import os
import sqlite3
import threading

from . import seal_slots as slots, store_layout
from .seal_types import (RecordInvalid, RecordUnreadable, SealUnusable,
                         require_canonical_id)


@dataclass(frozen=True, slots=True)
class Generation:
    number: int
    state: str


@dataclass(slots=True)
class _Request:
    kind: str
    chain: str | None = None
    through: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    result: int = 0
    error: BaseException | None = None
    skip: frozenset = frozenset()


@dataclass(slots=True)
class _Owner:
    project: str
    number: int
    state: str = 'opened'
    reader: object = None
    ident: int | None = None
    ledger_fd: int | None = None
    closing: bool = False
    queue: deque = field(default_factory=deque)
    passes: set = field(default_factory=set)
    condition: threading.Condition = field(default_factory=threading.Condition)
    thread: threading.Thread | None = None


class SealRenderer:
    def __init__(self, service, *, store_root, open_reader, events_after,
                 clock_ms, tick, wait, bound_s):
        self.service, self.store_root = service, store_root
        self.open_reader, self.events_after = open_reader, events_after
        self.clock_ms, self.tick, self.wait, self.bound_s = clock_ms, tick, wait, bound_s
        self._lock = threading.Lock()
        self._owners = {}
        self._stopped = threading.Event()
        self._backstop = None

    def generations(self, project_id):
        require_canonical_id(project_id)
        with self._lock:
            return tuple(Generation(g.number, g.state) for g in self._owners.get(project_id, ()))

    def _newest(self, project):
        with self._lock:
            owners = self._owners.get(project, ())
            return owners[-1] if owners else None

    def _state(self, owner, state):
        with self._lock:
            owner.state = state

    def _enqueue(self, owner, request):
        with owner.condition:
            if owner.closing or owner.state == 'closed':
                raise RuntimeError('seal_pass_thread_closed')
            owner.queue.append(request)
            owner.condition.notify()

    def ledger_opened(self, project_id):
        require_canonical_id(project_id)
        with self._lock:
            owners = self._owners.setdefault(project_id, [])
            owner = _Owner(project_id, len(owners) + 1)
            owners.append(owner)
        opened = _Request('open')
        owner.thread = threading.Thread(target=self._own, args=(owner, opened),
                                        name='seal-ledger-' + project_id, daemon=True)
        owner.thread.start()
        completed = self.wait(opened.done, self.bound_s)
        if not completed:
            with owner.condition:
                if not opened.done.is_set():
                    owner.closing = True
                    self._state(owner, 'closing_late')
                    raise RuntimeError('seal_ledger_open_timed_out')
        if opened.error is not None:
            raise opened.error

    def _hook_wait(self, owner, request):
        if not self.wait(request.done, self.bound_s / 2):
            with owner.condition:
                reader = owner.reader
                if reader is not None:
                    reader.interrupt()
            completed = self.wait(request.done, self.bound_s / 2)
        else:
            completed = True
        if completed and request.error is not None:
            raise request.error
        return completed

    def ledger_serving(self, project_id):
        require_canonical_id(project_id)
        owner = self._newest(project_id)
        if owner is None:
            raise RuntimeError('seal_no_pass_thread')
        request = _Request('start')
        self._enqueue(owner, request)
        completed = self._hook_wait(owner, request)
        with self._lock:
            if owner.state != 'closed' and not owner.closing:
                owner.state = 'serving' if completed else 'serving_late'

    def ledger_closing(self, project_id):
        require_canonical_id(project_id)
        owner = self._newest(project_id)
        if owner is None or owner.state == 'closed':
            return
        request = _Request('stop')
        with owner.condition:
            owner.closing = True
            self._state(owner, 'closing')
            owner.queue.append(request)
            owner.condition.notify()
        if not self._hook_wait(owner, request):
            with self._lock:
                if owner.state != 'closed':
                    owner.state = 'closing_late'

    def signal(self, project_id, chain_id):
        require_canonical_id(project_id); require_canonical_id(chain_id)
        owner = self._newest(project_id)
        if owner is None:
            return
        with owner.condition:
            if (owner.closing or owner.state not in ('serving', 'serving_late') or
                    chain_id in owner.passes or None in owner.passes):
                return
            owner.passes.add(chain_id)
            owner.queue.append(_Request('pass', chain_id))
            owner.condition.notify()

    def committed(self, project_id, chain_id):
        self.signal(project_id, chain_id)

    def drain(self, project_id, chain_id, *, through):
        require_canonical_id(project_id); require_canonical_id(chain_id)
        if type(through) is not int or through < 0:
            raise ValueError('nonnegative_integer_required')
        owner = self._newest(project_id)
        if owner is None:
            raise RuntimeError('seal_no_pass_thread')
        # Start/stop drains already run on the owner; all other callers queue.
        if owner.ident == threading.get_ident():
            return self._drain(owner, chain_id, through)
        request = _Request('drain', chain_id, through)
        self._enqueue(owner, request)
        request.done.wait()
        if request.error is not None:
            raise request.error
        return request.result

    def start(self):
        with self._lock:
            if self._backstop is not None:
                return
            self._stopped.clear()
            self._backstop = threading.Thread(target=self._ticks, name='seal-backstop', daemon=True)
            self._backstop.start()

    def stop(self):
        self._stopped.set()
        backstop = self._backstop
        if backstop is not None:
            # tick's injected contract returns on stop, and at most every second.
            backstop.join()
        self._backstop = None

    def _ticks(self):
        while self.tick(1.0) and not self._stopped.is_set():
            with self._lock:
                owners = [generations[-1] for generations in self._owners.values()
                          if generations[-1].state in ('serving', 'serving_late')]
            for owner in owners:
                with owner.condition:
                    if owner.closing or None in owner.passes:
                        continue
                    # Discovery, like all seal I/O, belongs to the owner.
                    request = _Request('pass', skip=frozenset(owner.passes))
                    owner.passes.add(None)
                    owner.queue.append(request)
                    owner.condition.notify()

    def _own(self, owner, opened):
        owner.ident = threading.get_ident()
        pending_stop = None
        failed_request = None
        open_error = None
        try:
            reader = self.open_reader(self.store_root, owner.project)
            with owner.condition:
                owner.reader = reader
                if owner.closing:
                    return
                opened.done.set()
            while True:
                with owner.condition:
                    owner.condition.wait_for(lambda: owner.queue)
                    request = owner.queue.popleft()
                    if request.kind == 'pass':
                        owner.passes.discard(request.chain)
                if owner.closing and request.kind != 'stop':
                    request.error = RuntimeError('seal_pass_thread_closing')
                    request.done.set()
                    continue
                try:
                    if request.kind == 'start':
                        self._start_drain(owner)
                    elif request.kind == 'stop':
                        pending_stop = request
                        self._stop_drain(owner)
                        break
                    elif request.kind == 'pass' and request.chain is None:
                        self._backstop_pass(owner, request.skip)
                    else:
                        if request.kind == 'pass' and not self.service.ledger_matches(
                                owner.project, request.chain, owner.ledger_fd):
                            continue
                        request.result = self._drain(owner, request.chain, request.through)
                except SealUnusable as exc:
                    if request.kind == 'drain':
                        request.error = exc
                except sqlite3.OperationalError as exc:
                    if str(exc) != 'interrupted':
                        request.error = exc
                        failed_request = request
                        raise
                    if request.kind == 'drain':
                        request.error = exc
                except BaseException as exc:
                    request.error = exc
                    failed_request = request
                    raise
                finally:
                    if request.kind != 'stop' and failed_request is not request:
                        request.done.set()
                if request.kind == 'stop':
                    break
        except BaseException as exc:
            if not opened.done.is_set():
                open_error = exc
            else:
                # A code fault ends this reader generation, never another one's.
                with owner.condition:
                    for queued in owner.queue:
                        queued.error = exc
        finally:
            try:
                with owner.condition:
                    reader = owner.reader
                    owner.reader = None
                try:
                    if reader is not None:
                        reader.close()
                finally:
                    if owner.ledger_fd is not None:
                        os.close(owner.ledger_fd)
                        owner.ledger_fd = None
            finally:
                with owner.condition:
                    if open_error is not None:
                        opened.error = open_error
                    self._state(owner, 'closed')
                    opened.done.set()
                    for queued in owner.queue:
                        queued.error = queued.error or RuntimeError('seal_pass_thread_closed')
                        queued.done.set()
                    owner.queue.clear(); owner.passes.clear()
                if pending_stop is not None:
                    pending_stop.done.set()
                if failed_request is not None:
                    failed_request.done.set()

    def _backstop_pass(self, owner, skip):
        for chain in self.service.chains(owner.project):
            if owner.closing:
                break
            if chain in skip:
                continue
            try:
                if self.service.ledger_matches(owner.project, chain, owner.ledger_fd):
                    self._drain(owner, chain, 0)
            except SealUnusable as exc:
                if exc.chain_id is None:
                    break
            except (RecordInvalid, RecordUnreadable):
                continue

    def _start_drain(self, owner):
        project = owner.project
        if owner.ledger_fd is None:
            owner.ledger_fd = os.open(store_layout.ledger_path(self.store_root, project),
                                      os.O_RDONLY | os.O_CLOEXEC)
        for chain in self.service.chains(project):
            if owner.closing:
                break
            try:
                if self.service.ledger_matches(project, chain, owner.ledger_fd):
                    self.drain(project, chain, through=0)
            except SealUnusable as exc:
                if exc.chain_id is None:
                    break
            except (RecordInvalid, RecordUnreadable):
                continue

    def _stop_drain(self, owner):
        project = owner.project
        for chain in self.service.chains(project):
            try:
                if owner.ledger_fd is None or not self.service.ledger_matches(
                        project, chain, owner.ledger_fd):
                    continue
                conn = owner.reader
                conn.execute('BEGIN').close()
                try:
                    high = self.service.rendered_through(project, chain)
                    rows = self.events_after(conn, project, chain, high)
                    if rows:
                        self._raise(project, chain, max(row.number for row in rows))
                finally:
                    if conn.in_transaction:
                        conn.execute('ROLLBACK').close()
            except SealUnusable as exc:
                if exc.chain_id is None:
                    break
            except (RecordInvalid, RecordUnreadable):
                continue
            except sqlite3.OperationalError as exc:
                if str(exc) == 'interrupted':
                    return
                raise

    def _high(self, project, chain):
        with self.service._locked(project, exclusive=False):
            return self.service._item(project, chain, slots.ItemKind.HIGH_WATER).value

    def _raise(self, project, chain, through):
        with self.service._locked(project, exclusive=True):
            state = self.service._item(project, chain, slots.ItemKind.HIGH_WATER)
            if through > state.value:
                try:
                    with self.service._open(project, chain + '.high-water', write=True) as fd:
                        slots.write_next(fd, state, value=through)
                except (OSError, slots.CannotRecord):
                    self.service._fail(project, 'high_water_unusable', chain)

    def _drain(self, owner, chain, through):
        project = owner.project
        if through > 0:
            self._raise(project, chain, through)
        if owner.closing:
            return self._high(project, chain)
        conn = owner.reader
        conn.execute('BEGIN').close()
        try:
            high = self.service.rendered_through(project, chain)
            if owner.closing:
                return high
            rows = self.events_after(conn, project, chain, high)
            if rows:
                self._raise(project, chain, max(row.number for row in rows))
            if not owner.closing:
                self._records(owner, chain)
            return self._high(project, chain)
        finally:
            # Rollback ends the read snapshot even on interrupted SQL or faults.
            if conn.in_transaction:
                conn.execute('ROLLBACK').close()

    def _records(self, owner, chain):
        project = owner.project
        if self.service.is_broken(project, chain):
            return
        previous = None
        number = 0
        try:
            head = self.service.folder.record_head(project, chain)
            if head is not None:
                first = self.service.read_record(project, chain, 0)
                previous = first if head == 0 else self.service.read_record(project, chain, head)
                if first is None or previous is None or first.key_id != previous.key_id:
                    return
                number = head + 1
        except (RecordInvalid, RecordUnreadable):
            return
        if owner.closing:
            return
        rows = self.events_after(owner.reader, project, chain, number - 1)
        key_id = previous.key_id if previous is not None else None
        prev_mac = previous.mac if previous is not None else None
        for row in rows:
            if owner.closing or row.number != number:
                return
            data = self.service.seal_record(row, prev_mac, key_id)
            if data is None:
                return
            conflict = False
            try:
                with self.service._locked(project, exclusive=True, report=False):
                    if owner.closing:
                        return
                    if self.service.folder.mark_exists(project, chain):
                        return
                    conflict = not self.service.folder.publish_record(project, chain, number, data)
            except (OSError, SealUnusable):
                return
            if conflict:
                self.service.mark_broken(project, chain, cause='record_conflict', number=number)
                return
            try:
                record = self.service.open_record(data)
            except RecordInvalid:
                return
            prev_mac, key_id = record.mac, record.key_id
            number += 1
