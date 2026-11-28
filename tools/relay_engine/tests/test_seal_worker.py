"""Worker conditions, recovery and catalogue ownership on temporary stores."""
from contextlib import ExitStack, contextmanager
from collections.abc import Mapping
from copy import deepcopy
import importlib
import os
import sqlite3
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from relay_engine import approval_seal, seal_slots, seal_types, store_layout, strings
from relay_engine.tests import seal_standins as st

P = '10000000-0000-4000-8000-000000000001'
Q = '10000000-0000-4000-8000-000000000002'
Z = '10000000-0000-4000-8000-000000000003'
C = '20000000-0000-4000-8000-000000000002'
D = '30000000-0000-4000-8000-000000000003'
R = '40000000-0000-4000-8000-000000000004'


def await_event(event):
    if not event.wait(30):
        raise AssertionError('worker ordering hang ceiling')


class CatalogueReader:
    def __init__(self, conn):
        self.conn = conn
        self.owner = threading.get_ident()
        self.trace = []
        self.closed = False
        self.interrupted = threading.Event()

    def execute(self, *args):
        assert threading.get_ident() == self.owner
        self.trace.append(('sql', threading.get_ident()))
        return self.conn.execute(*args)

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def interrupt(self):
        self.interrupted.set()
        self.trace.append(('interrupt', threading.get_ident()))
        self.conn.interrupt()

    def close(self):
        assert threading.get_ident() == self.owner
        self.conn.close()
        self.trace.append(('close', threading.get_ident()))
        self.closed = True


@contextmanager
def receipt_queue(queue):
    state = SimpleNamespace(keys=[], attempts=[], receipts={}, lose_next=False)
    original = queue.run
    fields = ('kind', 'subkind', 'project_id', 'texts', 'pins', 'condition_key')
    def run(operation, args, principal):
        if operation != 'queue.raise':
            return original(operation, args, principal)
        key = args['operation_key']
        state.keys.append(key)
        state.attempts.append(deepcopy(args))
        digest = deepcopy({name: args[name] for name in fields})
        if key in state.receipts:
            previous, answer = state.receipts[key]
            if digest != previous:
                return st.Refusal('E_API_CONFLICT', 'operation_key_reused')
            return dict(answer, replayed=True)
        answer = original(operation, args, principal)
        if isinstance(answer, Mapping):
            state.receipts[key] = (digest, dict(answer))
            if state.lose_next:
                state.lose_next = False
                raise ConnectionError('fixture lost committed answer')
        return answer
    with mock.patch.object(queue, 'run', side_effect=run):
        yield state


class TestSealWorker(unittest.TestCase):
    def setUp(self):
        # Every named test initially fails here for the missing worker module.
        self.m = importlib.import_module('relay_engine.seal_worker')
        self.store = st.TempStore(P)
        self.addCleanup(self.store.close)
        self.root = self.store.root / 'seal'
        self.clock = st.ManualClock()
        self.reports, self.diags, self.ended, self.readers = [], [], [], []
        self.channel = self.m.ReportChannel(size=4)
        self.service = approval_seal.SealService(self.root, runtime_instance_id=R,
                                                clock_ms=self.clock, report=self.channel.offer)
        self.service.set_current(P, C, ledger_fd=self.store.fd, expected=None, bind='first')
        self.route(P)
        self.queue = st.QueueServiceStandIn()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        strings.set_diagnostic_sink(self.diags.append)
        self.addCleanup(strings.set_diagnostic_sink, None)
        self.workers = []
        self.addCleanup(self.shutdown)
        self.tx = 0

    def route(self, project, state='active'):
        conn = self.store.catalogue
        if store_layout.route_state(conn, project) == 'absent':
            store_layout.reserve_project(conn, project, project, 0)
        if state == 'active':
            store_layout.activate_project(conn, project)
        else:
            conn.execute('UPDATE project_routes SET state=? WHERE project_id=?', (state, project)).close()
        conn.commit()

    def project(self, project):
        conn = store_layout.open_ledger(self.store.root, project, create=True)
        conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, 1)', (project, project)).close()
        conn.commit(); conn.close()
        fd = os.open(store_layout.ledger_path(self.store.root, project), os.O_RDONLY)
        try:
            self.service.set_current(project, C, ledger_fd=fd, expected=None, bind='first')
        finally:
            os.close(fd)
        self.route(project)

    def opener(self, root):
        reader = CatalogueReader(store_layout.open_catalogue_readonly(root))
        self.readers.append(reader)
        return reader

    def worker(self, *, backup='excluded', opened=True, **changes):
        tick, wait = st.ManualTick(), st.ManualWait()
        kwargs = dict(channel=self.channel, store_root=self.store.root, queue=self.queue,
                      refusal_type=st.Refusal, unavailable_types=(st.Unavailable,),
                      seal_texts=st.seal_texts, backup_texts=st.backup_texts,
                      backup_pins=st.backup_pins, open_catalogue_reader=self.opener,
                      route_state=store_layout.route_state, project_routes=store_layout.project_routes,
                      clock_ms=self.clock, tick=tick, wait=wait, bound_s=10,
                      on_fault_ended=lambda p, c: self.ended.append((p, c)),
                      backup_exclusion=backup)
        kwargs.update(changes)
        worker = self.m.SealWorker(self.service, **kwargs)
        self.workers.append((worker, tick, wait))
        worker.start()
        if opened:
            worker.catalogue_opened()
        self.sync(worker)
        return worker

    def sync(self, worker=None):
        # An idle event is a synchronization observation, not a timing assertion.
        worker = worker or self.workers[-1][0]
        await_event(worker._idle)
        if worker._error is not None:
            raise worker._error

    def tick(self, worker=None):
        worker = worker or self.workers[-1][0]
        for w, tick, wait in self.workers:
            if w is worker:
                prior = len(tick.calls)
                tick.advance(); tick.until_calls(prior + 1)
                self.sync(w)
                return
        raise AssertionError('unknown fixture worker')

    def stop(self, worker):
        for w, tick, wait in list(self.workers):
            if w is worker:
                tick.finish(); w.stop(); self.workers.remove((w, tick, wait)); return

    def shutdown(self):
        for worker, tick, wait in list(self.workers):
            tick.finish(); worker.stop()
        self.workers.clear()

    def path(self, kind='counter', project=P, chain=C):
        return self.root / 'projects' / project / (chain + '.' + kind)

    def damage(self, kind='counter', project=P, chain=C):
        path = (self.root / 'projects' / project / 'pointer') if kind == 'pointer' else self.path(kind, project, chain)
        old = path.read_bytes(); path.write_bytes(b'bad')
        return path, old

    def repair(self, saved):
        # Preserve the inode, which is part of the slot's self binding.
        saved[0].write_bytes(saved[1])

    def reserve(self, project=P, chain=C):
        self.tx += 1
        return self.service.reserve(project, chain, kind='approve', subject='test', payload=b'{}',
                                    transaction=self.tx, declared_kinds=('approve',))

    def refuse(self, project=P, chain=C):
        with self.assertRaises(seal_types.SealUnusable):
            self.reserve(project, chain)
        self.sync()

    def report(self, cause, project=P, chain=C):
        self.channel.offer(seal_types.SealReport(project, chain, cause, self.clock()))
        self.sync()

    def items(self, project=P):
        return [v for v in self.queue.items.values() if v['project_id'] == project and v['open']]

    def assert_cause(self, cause, chain=C, project=P):
        items = self.items(project)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['condition_key'], {'cause': cause, 'chain_id': chain or 'none'})
        return items[0]['item_id']

    def folder_fault(self, worker=None):
        lock = self.root / 'projects' / P / 'lock'
        original = os.open
        def opened(path, flags, *args, **kwargs):
            if str(path) == str(lock) or (str(path) == 'lock' and kwargs.get('dir_fd') is not None):
                raise PermissionError('fixture lock')
            return original(path, flags, *args, **kwargs)
        patch = mock.patch('os.open', side_effect=opened)
        patch.start()
        return patch

    def test_catalogue_interrupt_never_meets_a_closed_reader(self):
        # The first wait lets the owner close before the hook resumes.
        closed = threading.Event()
        class Reader(CatalogueReader):
            def close(reader):
                super().close()
                closed.set()
        def opener(root):
            reader = Reader(store_layout.open_catalogue_readonly(root))
            self.readers.append(reader)
            return reader
        worker = self.worker(open_catalogue_reader=opener)
        reader = self.readers[-1]
        waits, errors = [], []
        def wait(done, seconds):
            waits.append(seconds)
            if len(waits) == 1:
                await_event(closed)
                return False
            return done.wait(30)
        worker.wait = wait
        try:
            worker.catalogue_closing()
        except BaseException as exc:
            errors.append(exc)
        self.assertEqual(errors, [], 'catalogue hook interrupted closed reader')
        self.assertFalse(reader.interrupted.is_set(), 'closed reader was interrupted')
        self.assertEqual(waits, [5, 5])
        self.stop(worker)

        # With an interrupt in flight, the owner must be excluded by the slot lock.
        reading, release_read = threading.Event(), threading.Event()
        interrupting, release_interrupt = threading.Event(), threading.Event()
        owner_recorded, closed = threading.Event(), threading.Event()
        measuring, armed = threading.Event(), threading.Event()
        records, trace, coordinator_errors = [], [], []
        class HeldReader(CatalogueReader):
            def execute(reader, *args):
                result = super().execute(*args)
                if armed.is_set():
                    armed.clear()
                    reading.set()
                    await_event(release_read)
                return result
            def interrupt(reader):
                trace.append('interrupt-entry')
                interrupting.set()
                await_event(release_interrupt)
                super().interrupt()
                trace.append('interrupt-return')
            def close(reader):
                super().close()
                trace.append('close')
                closed.set()
        def opener(root):
            reader = HeldReader(store_layout.open_catalogue_readonly(root))
            self.readers.append(reader)
            return reader
        worker = self.worker(open_catalogue_reader=opener)
        class ObservedCondition(threading.Condition):
            def __enter__(condition):
                if measuring.is_set() and threading.get_ident() == worker._thread.ident:
                    acquired = condition.acquire(False)
                    records.append('owner-acquired' if acquired else 'owner-blocked')
                    measuring.clear()
                    owner_recorded.set()
                    if not acquired:
                        condition.acquire()
                    return True
                return super().__enter__()
        armed.set()
        self.channel.offer(seal_types.SealReport(P, C, 'counter_unusable', self.clock()))
        await_event(reading)
        worker._condition = ObservedCondition()
        measuring.set()
        waits, errors = [], []
        def wait(done, seconds):
            waits.append(seconds)
            return False if len(waits) == 1 else done.wait(30)
        worker.wait = wait
        def coordinate():
            try:
                await_event(interrupting)
                release_read.set()
                await_event(owner_recorded)
                if records[0] == 'owner-acquired':
                    await_event(closed)
            except BaseException as exc:
                coordinator_errors.append(exc)
            finally:
                release_interrupt.set()
        coordinator = threading.Thread(target=coordinate)
        coordinator.start()
        try:
            try:
                worker.catalogue_closing()
            except BaseException as exc:
                errors.append(exc)
        finally:
            release_read.set(); release_interrupt.set()
            coordinator.join(30)
        self.assertEqual(coordinator_errors, [])
        self.assertFalse(coordinator.is_alive())
        self.assertEqual(records, ['owner-blocked'], 'owner was not excluded during interrupt')
        self.assertEqual(trace, ['interrupt-entry', 'interrupt-return', 'close'],
                         'catalogue close preceded interrupt return')
        self.assertEqual(errors, [], 'catalogue interrupt met closed connection')
        self.assertEqual(waits, [5, 5])

    def test_recheck_keeps_unwritable_folder(self):
        self.worker()
        project = self.root / 'projects' / P
        clears = lambda: [call for call in self.queue.calls if call[0] == 'clear']
        project.chmod(0o500)
        try:
            self.refuse()
            item = self.assert_cause('folder_unusable', None)
            before = len(clears())
            self.tick()
            self.assertEqual(self.assert_cause('folder_unusable', None), item,
                             'unwritable folder item was cleared')
            self.assertEqual(len(clears()), before)
            self.refuse()
            self.assertEqual(self.assert_cause('folder_unusable', None), item)
        finally:
            project.chmod(0o700)
        self.tick()
        self.assertEqual(self.items(), [])
        self.assertIn(item, [call[1] for call in clears()])
        before = self.queue.notifications
        with mock.patch.object(type(self.service.folder), 'project_usable', return_value=False):
            self.refuse()
            item = self.assert_cause('folder_unusable', None)
            before_clear = len(clears())
            self.tick()
            self.assertEqual(self.assert_cause('folder_unusable', None), item)
            self.assertEqual(len(clears()), before_clear)
        self.tick()
        self.assertEqual(self.items(), [])
        self.assertIn(item, [call[1] for call in clears()])
        self.assertEqual(self.queue.notifications, before + 1)

    def test_operation_key_is_per_occurrence(self):
        for case in ('retried', 'cleared-equal-clock', 'cleared-clock-back',
                     'replaced-cause', 'restart'):
            with self.subTest(case=case):
                self.queue = st.QueueServiceStandIn(); self.clock.value = 2000
                with receipt_queue(self.queue) as receipts:
                    worker = self.worker()
                    patch = self.folder_fault()
                    try:
                        if case == 'retried':
                            self.queue.behavior[('raise', P)] = st.Refusal()
                            self.refuse(); self.tick(worker)
                            self.queue.behavior.clear(); self.tick(worker)
                            self.assertEqual(len(receipts.keys), 3)
                            self.assertEqual(len(set(receipts.keys)), 1, 'retry changed occurrence key')
                            self.assertEqual(receipts.attempts, [receipts.attempts[0]] * 3)
                            self.assert_cause('folder_unusable', None)
                            continue
                        self.refuse()
                        first = self.assert_cause('folder_unusable', None)
                        first_key = receipts.keys[-1]
                        patch.stop(); patch = None
                        if case == 'replaced-cause':
                            saved = self.damage('pointer')
                            self.refuse()
                            self.assert_cause('pointer_damaged', None)
                            self.repair(saved)
                        self.tick(worker)
                        self.assertEqual(self.items(), [])
                        self.assertFalse(self.queue.items[first]['open'])
                        if case == 'restart':
                            self.stop(worker); worker = self.worker()
                        if case == 'cleared-clock-back':
                            self.clock.advance(-1000)
                        patch = self.folder_fault()
                        self.refuse()
                        self.assertNotEqual(receipts.keys[-1], first_key,
                                            'new occurrence reused raise key')
                        current = self.assert_cause('folder_unusable', None)
                        self.assertNotEqual(current, first)
                        self.assertFalse(self.queue.items[first]['open'])
                    finally:
                        if patch is not None:
                            patch.stop()
                        self.shutdown()

    def test_lost_raise_answer_restart_meets_open_item(self):
        self.queue = st.QueueServiceStandIn(); self.clock.value = 2000
        with receipt_queue(self.queue) as receipts:
            worker = self.worker(); patch = self.folder_fault()
            try:
                receipts.lose_next = True
                self.refuse()
                first = self.assert_cause('folder_unusable', None)
                self.assertIsNone(worker._faults[P].item_id)
                self.assertEqual([(d.callee, d.exc_type, d.project_id) for d in self.diags],
                                 [('queue', 'ConnectionError', P)])
                first_keys = set(receipts.keys)
                boundary = len(receipts.keys)
                self.stop(worker)
                self.queue.behavior[('recover', P)] = st.Refusal()
                worker = self.worker()
                self.refuse()
                self.assertEqual(len(self.items()), 1, 'lost answer opened a second item')
                self.assertEqual(self.queue.notifications, 1)
                self.assertEqual(worker._faults[P].item_id, first)
                self.assertEqual(self.assert_cause('folder_unusable', None), first)
                patch.stop(); patch = None
                self.queue.behavior.clear(); self.tick(worker)
                self.assertEqual(self.items(), [])
                second_keys = set(receipts.keys[boundary:])
                self.assertEqual(len(first_keys), 1)
                self.assertEqual(len(second_keys), 1)
                self.assertNotEqual(first_keys, second_keys,
                                    'restart reused lost-answer occurrence key')
            finally:
                if patch is not None:
                    patch.stop()
                self.shutdown()

    def test_new_occurrence_id_is_injectable(self):
        for family in ('fault', 'backup'):
            with self.subTest(family=family):
                self.queue = st.QueueServiceStandIn()
                values = []
                def new_id():
                    value = 'occ-' + str(len(values) + 1)
                    values.append(value)
                    return value
                with receipt_queue(self.queue) as receipts:
                    worker = self.worker(backup='add_failed' if family == 'backup' else 'excluded',
                                         new_occurrence_id=new_id)
                    patch = None
                    try:
                        if family == 'backup':
                            self.assertIn(receipts.keys[0].removeprefix('backup_exclusion:'), values)
                            self.assertEqual(receipts.keys[0], 'backup_exclusion:' + values[0])
                        else:
                            patch = self.folder_fault(); self.refuse()
                            first = receipts.keys[-1]
                            patch.stop(); patch = None
                            self.tick(worker)
                            patch = self.folder_fault(); self.refuse()
                            second = receipts.keys[-1]
                            prefix = ':'.join((P, 'none', 'folder_unusable')) + ':'
                            self.assertTrue(first.startswith(prefix) and second.startswith(prefix))
                            left, right = first[len(prefix):], second[len(prefix):]
                            self.assertIn(left, values); self.assertIn(right, values)
                            self.assertLess(values.index(left), values.index(right))
                    finally:
                        if patch is not None:
                            patch.stop()
                        self.shutdown()

    def test_raise_once_per_condition(self):
        self.worker(); self.damage()
        for _ in range(10): self.refuse()
        self.assert_cause('counter_unusable')
        self.assertEqual(self.queue.notifications, 1)

    def test_cause_change_raises_then_clears(self):
        self.worker(); patch = self.folder_fault()
        try: self.refuse()
        finally: patch.stop()
        old = self.assert_cause('folder_unusable', None)
        self.damage(); self.refuse()
        new = self.assert_cause('counter_unusable')
        calls = self.queue.calls
        self.assertLess(next(i for i,c in enumerate(calls) if c[0]=='raise' and c[1]==new),
                        next(i for i,c in enumerate(calls) if c[0]=='clear' and c[1]==old))
        self.assertEqual(self.ended, [])

    def test_clear_on_recheck(self):
        self.worker(); saved=self.damage(); self.refuse(); item=self.assert_cause('counter_unusable')
        self.repair(saved); self.tick()
        self.assertEqual(self.items(), []); self.assertIn(('clear', item), self.queue.calls)

    def test_recheck_across_preswap_window(self):
        self.worker(); self.damage(); self.refuse()
        other = self.store.root / 'staged'; other.write_bytes(b'')
        fd=os.open(other, os.O_RDONLY)
        try: self.service.set_current(P,D,ledger_fd=fd,expected=C,bind='first')
        finally: os.close(fd)
        self.sync(); self.assertEqual(self.items(), [])
        # A swap publishes the staged inode at the served path.
        os.replace(other, store_layout.ledger_path(self.store.root,P))
        self.assertEqual(self.reserve(chain=D), 1); self.sync(); self.assertEqual(self.items(), [])

    def test_clear_on_pointer_leaving_chain(self):
        self.worker(); self.damage(); self.refuse()
        self.service.set_current(P,D,ledger_fd=self.store.fd,expected=C,bind='first')
        self.sync(); self.assertEqual(self.items(), [])

    def test_clear_on_undo_clear(self):
        self.worker(); self.damage(); self.refuse()
        self.service.clear_current(P,expected=C); self.sync(); self.assertEqual(self.items(), [])

    def snapshot(self):
        paths=list(self.root.rglob('*'))+[store_layout.ledger_path(self.store.root,P)]
        return {str(p):(p.stat().st_ino,p.stat().st_size,p.read_bytes()) for p in paths if p.is_file()}

    def test_recheck_reads_only(self):
        self.worker(); self.damage(); self.refuse(); before=self.snapshot()
        for _ in range(100): self.tick()
        self.assertEqual(self.snapshot(),before)

    def test_recheck_opens_ledger_before_seal_lock(self):
        import fcntl
        self.worker(); self.damage(); self.refuse(); found=[]
        old=os.open; ledger=store_layout.ledger_path(self.store.root,P)
        def opened(path,flags,*a,**kw):
            if Path(path)==ledger:
                lock=old(self.root/'projects'/P/'lock',os.O_RDWR)
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); found.append(True)
                finally: os.close(lock)
            return old(path,flags,*a,**kw)
        with mock.patch('os.open',side_effect=opened): self.tick()
        self.assertTrue(found)

    def test_restart_recovers_open_items(self):
        self.project(Q); self.route(Q, "creating"); w=self.worker(); p=self.damage(); self.damage(project=Q)
        self.refuse(); self.refuse(Q); ids={p['item_id'] for p in self.items()+self.items(Q)}
        self.stop(w); self.repair(p)
        before=len(self.queue.calls)
        w=self.worker(opened=False)
        self.assertEqual({x['item_id'] for x in self.items()+self.items(Q)},ids)
        self.assertFalse(any(call[0]=='recover' and call[1] is not None for call in self.queue.calls[before:]))
        w.catalogue_opened(); self.sync(w)
        self.assertEqual(self.items(),[]); self.assert_cause('counter_unusable',project=Q)
        self.assertTrue(ids)
        self.assertIn(('recover',None),self.queue.calls)
        self.assertIn(('recover',P),self.queue.calls);self.assertIn(('recover',Q),self.queue.calls)
        self.stop(w)
        self.queue.behavior[('recover',None)] = st.Refusal()
        self.queue.behavior[('recover',P)] = st.Refusal()
        self.queue.behavior[('recover',Q)] = st.Refusal()
        w=self.worker(); saved=self.damage(); self.refuse(); known=self.assert_cause('counter_unusable')
        self.queue.behavior[('clear',P)] = st.Refusal()
        self.repair(saved); self.tick(w)
        self.assertIn(('clear',known),self.queue.calls)
        self.assertEqual(self.ended.count((P,'counter_unusable')),2)
        before=len([c for c in self.queue.calls if c==('recover',P)])
        self.tick(w); self.assertGreater(len([c for c in self.queue.calls if c==('recover',P)]),before)
        self.queue.behavior.pop(('clear',P)); self.tick(w)
        self.assertEqual(self.items(),[])
        self.refuse(chain=D); mismatch=self.assert_cause('ledger_chain_mismatch')
        self.reserve();self.sync();self.assertIn(('clear',mismatch),self.queue.calls)
        self.stop(w)
        # A stale route list is never used, even after a new catalogue window.
        entered,release=threading.Event(),threading.Event(); calls=[]
        def routes(conn):
            ids=store_layout.project_routes(conn); calls.append(conn)
            if len(calls)==1: entered.set();await_event(release)
            return ids
        w=self.worker(opened=False,project_routes=routes);w.catalogue_opened();await_event(entered)
        wait=self.workers[-1][2]; done=threading.Event()
        t=threading.Thread(target=lambda:(w.catalogue_closing(),done.set()),daemon=True);t.start()
        wait.expire();wait.expire();await_event(done);w.catalogue_opened();release.set();t.join(30)
        self.sync(w);self.assertGreaterEqual(len(calls),2)

    def test_folder_unusable_several_projects(self):
        self.project(Q);self.project(Z);self.worker()
        original=self.service.folder.lock
        def denied(project,*,exclusive): raise PermissionError('fixture folder')
        with mock.patch.object(self.service.folder,'lock',side_effect=denied):
            self.refuse();self.refuse(Q);self.assertEqual(self.items(Z),[]);self.refuse(Z)
        self.assertEqual(sum(len(self.items(p)) for p in (P,Q,Z)),3)
        self.tick();self.assertEqual(sum(len(self.items(p)) for p in (P,Q,Z)),0)

    def test_handoff_never_waits(self):
        w=self.worker();entered,release=threading.Event(),threading.Event()
        def held(*args): entered.set();await_event(release)
        self.queue.before_raise=held
        self.damage();
        with self.assertRaises(seal_types.SealUnusable):self.reserve()
        await_event(entered)
        try:
            for _ in range(10):
                with self.assertRaises(seal_types.SealUnusable):self.reserve()
            import fcntl
            fd=os.open(self.root/'projects'/P/'lock',os.O_RDWR)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
            self.assertEqual(len([d for d in self.diags if isinstance(d,seal_types.SealReportDropped)]),1)
        finally:release.set()
        self.sync(w)

    def test_queue_failure_never_blocks_reserve(self):
        self.project(Q);self.worker();self.queue.behavior[('raise',P)]=st.Unavailable()
        self.damage();self.refuse();self.assertEqual(self.reserve(Q),1)
        self.assertEqual(self.items(),[]);self.queue.behavior.clear();self.tick();self.assert_cause('counter_unusable')

    def test_worker_holds_no_seal_lock_across_queue_calls(self):
        w=self.worker();saved=self.damage();self.refuse();entered,release=threading.Event(),threading.Event()
        lock_errors=[]
        def held(*args):
            import fcntl
            fd=os.open(self.root/'projects'/P/'lock',os.O_RDWR)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:lock_errors.append('queue call holds seal lock')
            finally:os.close(fd)
            entered.set();await_event(release)
        self.queue.before_clear=held;self.repair(saved)
        self.workers[-1][1].advance();await_event(entered)
        try:
            self.assertEqual(lock_errors,[])
            self.assertEqual(self.reserve(),1)
        finally:release.set()
        self.sync(w)

    def test_pointer_unset_brief_window_raises_nothing(self):
        self.worker();self.service.clear_current(P,expected=C);self.sync();self.refuse()
        self.clock.advance(1000);self.tick();self.assertEqual(self.items(),[])
        self.service.return_current(P,C,expected=None);self.sync();self.assertEqual(self.items(),[])

    def test_pointer_unset_raised_after_age(self):
        self.worker();self.service.clear_current(P,expected=C);self.sync();self.refuse()
        self.clock.advance(60000);self.tick();self.assert_cause('pointer_unset',None)
        for _ in range(10):self.refuse()
        self.assertEqual(self.queue.notifications,1)

    def test_pointer_unset_cleared_by_pointer_step(self):
        self.test_pointer_unset_raised_after_age()
        self.service.return_current(P,C,expected=None);self.sync();self.assertEqual(self.items(),[])

    def test_undo_window_raises_nothing(self):
        self.worker();self.service.clear_current(P,expected=C);self.sync();self.refuse();self.route(P,'creating')
        self.clock.advance(60000);self.tick();self.assertEqual(self.items(),[])

    def test_pointer_unset_then_damaged_counter(self):
        self.test_pointer_unset_raised_after_age();old=self.assert_cause('pointer_unset',None)
        self.damage();self.service.return_current(P,C,expected=None);self.refuse()
        self.assert_cause('counter_unusable');self.assertIn(('clear',old),self.queue.calls)

    def test_ledger_chain_mismatch_stays_open(self):
        self.worker();self.refuse(chain=D);self.assert_cause('ledger_chain_mismatch')
        for _ in range(5):self.tick()
        for _ in range(10):self.refuse(chain=D)
        self.assertEqual(self.queue.notifications,1)
        self.service.set_current(P,D,ledger_fd=self.store.fd,expected=C,bind='first');self.sync()
        self.assertEqual(self.items(),[])

    def test_ledger_chain_mismatch_cleared_by_success(self):
        w=self.worker();self.refuse(chain=D);item=self.assert_cause('ledger_chain_mismatch')
        offered=[];old=self.service.report
        self.service.report=lambda r:(offered.append(r),old(r))[-1]
        self.reserve();self.sync();self.assertEqual(self.items(),[])
        self.assertEqual([r.cause for r in offered],['recorded'])
        self.assertEqual(self.ended,[(P,'ledger_chain_mismatch')]);self.assertIn(('clear',item),self.queue.calls)
        offered.clear();self.reserve();self.sync();self.assertEqual(offered,[])
        self.refuse(chain=D);self.stop(w);w=self.worker();offered.clear();self.reserve();self.sync()
        self.assertEqual([r.cause for r in offered],['recorded']);self.assertEqual(self.items(),[])
        self.queue.behavior[('raise',P)]=st.Unavailable();self.refuse(chain=D);offered.clear()
        self.reserve();self.sync();self.queue.behavior.clear();self.tick();self.assertEqual(self.items(),[])
        self.assertEqual([r.cause for r in offered],['recorded'])
        # Successful hand-off does not wait for the worker's queue call or its locks.
        self.refuse(chain=D);entered,release=threading.Event(),threading.Event()
        def held(*args):entered.set();await_event(release)
        self.queue.before_clear=held
        self.reserve();await_event(entered)
        try:
            import fcntl
            fd=os.open(self.root/'projects'/P/'lock',os.O_RDWR)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
        finally:release.set()
        self.sync(w)

    def test_folder_state_unreadable(self):
        self.worker();old=os.listdir
        def denied(path):
            if Path(path)==self.root/'projects'/P:raise PermissionError('fixture folder')
            return old(path)
        with mock.patch('os.listdir',side_effect=denied):
            self.assertEqual(self.service.folder_state(P),'unreadable');self.sync();self.assert_cause('folder_unusable',None)
        self.tick();self.assertEqual(self.items(),[])

    def test_folder_unusable_key_is_none(self):
        self.worker();p=self.folder_fault()
        try:
            for _ in range(10):self.refuse()
        finally:p.stop()
        self.assert_cause('folder_unusable',None);self.assertEqual(self.queue.notifications,1)
        self.damage('pointer');self.tick();self.assert_cause('pointer_damaged',None)

    def test_folder_unusable_clear_raises_nothing(self):
        self.worker();p=self.folder_fault()
        try:self.refuse()
        finally:p.stop()
        self.damage();self.tick();self.assertEqual(self.items(),[])
        self.refuse();self.assert_cause('counter_unusable')

    def test_chains_unlistable(self):
        self.worker();old=os.listdir
        def denied(path):
            if Path(path)==self.root/'projects'/P:raise PermissionError('fixture')
            return old(path)
        with mock.patch('os.listdir',side_effect=denied):
            with self.assertRaises(seal_types.SealUnusable):self.service.chains(P)
            self.sync();self.assert_cause('folder_unusable',None)
        self.assertEqual(self.service.chains(Q),())

    def test_old_chain_read_raises_no_item(self):
        self.worker();self.service.set_current(P,D,ledger_fd=self.store.fd,expected=C,bind='first');self.sync()
        self.damage('high-water')
        with self.assertRaises(seal_types.SealUnusable):self.service.rendered_through(P,C)
        self.sync();self.assertEqual(self.items(),[])
        self.damage('high-water',chain=D)
        with self.assertRaises(seal_types.SealUnusable):self.service.rendered_through(P,D)
        self.sync();self.assert_cause('high_water_unusable',D)

    def test_drain_unwritable_raise(self):
        from relay_engine.seal_renderer import SealRenderer
        w=self.worker();tick=st.ManualTick()
        r=SealRenderer(self.service,store_root=self.store.root,open_reader=self.store.open_reader,
                       events_after=st.events_after,clock_ms=self.clock,tick=tick,
                       wait=lambda e,s:e.wait(30),bound_s=10)
        self.service.attach_renderer(r);r.ledger_opened(P);r.ledger_serving(P)
        old=os.open
        def denied(path,flags,*a,**kw):
            if Path(path)==self.path('high-water') and flags & os.O_RDWR:raise PermissionError('fixture')
            return old(path,flags,*a,**kw)
        try:
            with mock.patch('os.open',side_effect=denied):
                with self.assertRaises(seal_types.SealUnusable):r.drain(P,C,through=3)
                self.sync(w);self.assert_cause('high_water_unusable')
        finally:r.ledger_closing(P)

    def test_both_slots_invalid_holds(self):
        self.worker();p=self.path();b=bytearray(p.read_bytes());b[0]^=1;b[65536]^=1;p.write_bytes(b)
        self.refuse();self.assert_cause('counter_unusable');self.tick();self.assert_cause('counter_unusable')

    def test_pointer_damaged_key_and_clear(self):
        self.worker()
        pointer=self.root/'projects'/P/'pointer'
        import hashlib
        import struct
        def slot(data, *, sequence=0, chain=C, inode=None, binding=False):
            data=bytearray(data)
            struct.pack_into('<Q',data,16,sequence)
            data[112:148]=chain.encode()
            if inode is not None:struct.pack_into('<Q',data,64,inode)
            if binding:data[32:64]=bytes(32)
            data[224:]=hashlib.sha256(data[:224]).digest()
            return bytes(data)
        for form in ('length','checksums','binding','equal','decrease','self'):
            raw=pointer.read_bytes(); one=raw[:256]
            if form=='length':data=b'bad'
            elif form=='checksums':data=bytes(65792)
            elif form=='binding':data=slot(one,binding=True)+bytes(65280)+slot(one,binding=True)
            elif form=='equal':data=slot(one,sequence=2)+bytes(65280)+slot(one,sequence=2,chain=D)
            elif form=='decrease':
                left=bytearray(slot(one,sequence=2));right=bytearray(slot(one,sequence=1))
                struct.pack_into('<Q',right,24,1);right[224:]=hashlib.sha256(right[:224]).digest()
                data=bytes(left)+bytes(65280)+bytes(right)
            else:data=slot(one,sequence=2,inode=1)+bytes(65280)+slot(one,sequence=1,chain=D)
            pointer.write_bytes(data);self.refuse();self.assert_cause('pointer_damaged',None)
            for _ in range(10):self.refuse()
            self.service.set_current(P,C,ledger_fd=self.store.fd,expected=seal_types.POINTER_UNREADABLE,bind='first')
            self.sync();self.assertEqual(self.items(),[])
        pointer.write_bytes(b'bad');self.refuse();self.service.clear_current(P,expected=seal_types.POINTER_UNREADABLE)
        self.sync();self.assertEqual(self.items(),[])


    def test_worker_survives_callee_faults(self):
        worker = self.worker(opened=False)
        def diagnostics():
            return [(d.callee, d.exc_type, d.project_id) for d in self.diags
                    if isinstance(d, seal_types.SealCalleeFailed)]
        with mock.patch.object(worker, 'open_catalogue_reader', side_effect=sqlite3.OperationalError('secret')):
            worker.catalogue_opened(); await_event(worker._idle)
            self.assertTrue(worker._thread.is_alive(), 'callee exception ended worker')
            self.assertEqual(diagnostics(), [('catalogue', 'OperationalError', None)])
        self.tick(worker); self.assertTrue(self.readers)
        self.diags.clear(); saved = self.damage()
        with mock.patch.object(worker, 'seal_texts', side_effect=KeyError('secret')):
            self.channel.offer(seal_types.SealReport(P, C, 'counter_unusable', self.clock()))
            await_event(worker._idle)
            self.assertTrue(worker._thread.is_alive(), 'callee exception ended worker')
            self.assertEqual(self.items(), [])
            self.assertEqual(diagnostics(), [('texts', 'KeyError', P)])
        self.tick(worker); self.assert_cause('counter_unusable')
        self.repair(saved); self.tick(worker)
        self.diags.clear(); worker._backup_read = False
        self.queue.behavior[('recover', None)] = [object()]
        worker._notify(); await_event(worker._idle)
        self.assertTrue(worker._thread.is_alive(), 'callee exception ended worker')
        self.assertEqual(diagnostics(), [('queue', 'AttributeError', None)])
        del self.queue.behavior[('recover', None)]
        self.tick(worker); self.assertTrue(worker._backup_read)
        # A failed catalogue read closes its reader and retries with another.
        self.diags.clear(); old_reader = self.readers[-1]; saved = self.damage()
        with mock.patch.object(worker, 'route_state', side_effect=sqlite3.OperationalError('secret')):
            self.channel.offer(seal_types.SealReport(P, C, 'counter_unusable', self.clock()))
            await_event(worker._idle)
            self.assertTrue(worker._thread.is_alive())
            self.assertTrue(old_reader.closed)
            self.assertEqual(self.items(), [])
            self.assertEqual(diagnostics(), [('catalogue', 'OperationalError', P)])
        self.tick(worker); self.assertIsNot(self.readers[-1], old_reader)
        self.assert_cause('counter_unusable')
        self.repair(saved); self.tick(worker)
        # A callback that acts and raises counts as called; it is never retried.
        calls = []
        def ended(project, cause):
            calls.append((project, cause))
            raise RuntimeError('secret')
        self.diags.clear(); saved = self.damage()
        self.refuse(); self.repair(saved)
        with mock.patch.object(worker, 'on_fault_ended', side_effect=ended):
            self.tick(worker)
            self.assertTrue(worker._thread.is_alive())
            self.tick(worker)
        self.assertEqual(calls, [(P, 'counter_unusable')])
        self.assertNotIn(P, worker._faults)
        self.assertEqual(diagnostics(), [('on_fault_ended', 'RuntimeError', P)])

    def test_stale_end_then_new_outage_raises(self):
        for elapsed in (1000, 0):
            self.queue = st.QueueServiceStandIn(); self.clock.value = 0
            receipts, keys = {}, []
            run = self.queue.run
            def receipt_run(operation, args, principal):
                if operation != 'queue.raise':
                    return run(operation, args, principal)
                key = args['operation_key']; keys.append(key)
                if key in receipts:
                    previous, result = receipts[key]
                    self.assertEqual(args, previous)
                    return dict(result, replayed=True)
                result = run(operation, args, principal)
                receipts[key] = (dict(args), dict(result))
                return result
            with mock.patch.object(self.queue, 'run', side_effect=receipt_run):
                worker = self.worker(); patch = self.folder_fault()
                try:
                    self.refuse()
                finally:
                    patch.stop()
                first = self.assert_cause('folder_unusable', None)
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                def before_clear(*args):
                    entered.set(); await_event(release)
                self.queue.before_clear = before_clear
                tick = self.workers[-1][1]; prior = len(tick.calls)
                tick.advance(); tick.until_calls(prior + 1); await_event(entered)
                closing = threading.Thread(target=worker.catalogue_closing); closing.start()
                with worker._condition:
                    self.assertTrue(worker._condition.wait_for(lambda: not worker._leave, timeout=30))
                release.set(); closing.join(30); self.assertFalse(closing.is_alive())
                self.queue.before_clear = None; self.sync(worker)
                self.assertIsNone(worker._faults[P].item_id, 'made clear still names its old item')
                self.assertFalse(self.queue.items[first]['open'])
                self.clock.advance(elapsed)
                patch = self.folder_fault()
                try:
                    worker.catalogue_opened(); self.sync(worker); self.tick(worker)
                    self.assertNotEqual(keys[-1], keys[0], 'new outage reused raise receipt key')
                    current = self.assert_cause('folder_unusable', None)
                    self.assertNotEqual(current, first)
                    self.assertFalse(self.queue.items[first]['open'])
                finally:
                    patch.stop(); self.stop(worker)

    def test_fault_end_survives_stale_generation(self):
        worker = self.worker(); patch = self.folder_fault()
        try:
            self.refuse()
        finally:
            patch.stop()
        self.assert_cause('folder_unusable', None)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def before_clear(*args):
            entered.set(); await_event(release)
        self.queue.before_clear = before_clear
        tick = self.workers[-1][1]
        prior = len(tick.calls); tick.advance(); tick.until_calls(prior + 1)
        await_event(entered)
        closing = threading.Thread(target=worker.catalogue_closing); closing.start()
        with worker._condition:
            self.assertTrue(worker._condition.wait_for(lambda: not worker._leave, timeout=30))
        release.set(); closing.join(30); self.assertFalse(closing.is_alive())
        self.queue.before_clear = None; self.sync(worker)
        self.assertIn(P, worker._faults, 'fault forgotten before end callback')
        self.assertEqual(self.ended, [])
        # Exercise a skipped clear on an occurrence whose clear was not made.
        args = {k: v for k, v in next(iter(self.queue.items.values())).items()
                if k not in ('item_id', 'open')}
        args['operation_key'] += ':not-cleared'
        item = self.queue.run('queue.raise', args, worker.principal)['item_id']
        stale = self.m._Fault(P, None, 'folder_unusable', self.clock(), item)
        worker._clear(stale, worker._generation - 1)
        self.assertIn(stale, worker._clear_pending)
        worker.catalogue_opened(); self.sync(worker); self.tick(worker)
        self.assertEqual(self.ended, [(P, 'folder_unusable')])
        self.assertNotIn(P, worker._faults)

    def test_pointer_unset_report_waits_for_active_route(self):
        self.route(P, 'creating'); worker = self.worker()
        self.clock.advance(60000)
        self.channel.offer(seal_types.SealReport(P, None, 'pointer_unset', 0)); self.sync(worker)
        self.assertEqual(self.items(), [])
        self.route(P, 'active')
        self.channel.offer(seal_types.SealReport(P, None, 'pointer_unset', 0)); self.sync(worker)
        self.assert_cause('pointer_unset', None)

    def test_folder_then_pointer_cause_change(self):
        for skipped in (True, False):
            self.queue = st.QueueServiceStandIn(); self.ended.clear()
            worker = self.worker(); patch = self.folder_fault()
            try:
                self.refuse()
            finally:
                patch.stop()
            old = self.assert_cause('folder_unusable', None)
            old_fault = worker._faults[P]
            saved = self.damage('pointer')
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            real_clear = worker._clear
            def before_clear(*args):
                entered.set(); await_event(release)
            def clear(fault, generation):
                if fault is old_fault:
                    before_clear()
                return real_clear(fault, generation)
            try:
                with mock.patch.object(worker, '_clear', side_effect=clear) if skipped else ExitStack():
                    if not skipped:
                        self.queue.before_clear = before_clear
                    tick = self.workers[-1][1]; prior = len(tick.calls)
                    tick.advance(); tick.until_calls(prior + 1); await_event(entered)
                    new_items = [i['item_id'] for i in self.items()
                                 if i['condition_key']['cause'] == 'pointer_damaged']
                    self.assertEqual(len(new_items), 1, 'new fault not raised before old clear')
                    new = new_items[0]
                    closing = threading.Thread(target=worker.catalogue_closing); closing.start()
                    with worker._condition:
                        self.assertTrue(worker._condition.wait_for(lambda: not worker._leave, timeout=30))
                    release.set(); closing.join(30); self.assertFalse(closing.is_alive())
                    self.queue.before_clear = None; self.sync(worker)
                    self.assertTrue(self.queue.items[new]['open'])
                    if skipped:
                        self.assertTrue(self.queue.items[old]['open'])
                        self.assertIn(old_fault, worker._clear_pending, 'skipped clear was not retained')
                        self.assertNotIn(('clear', old), self.queue.calls)
                    else:
                        self.assertLess(self.queue.calls.index(('raise', new)),
                                        self.queue.calls.index(('clear', old)))
                        self.assertFalse(self.queue.items[old]['open'])
                    self.assertEqual(self.ended, [])
            finally:
                release.set(); self.queue.before_clear = None
                self.repair(saved); self.stop(worker)

    def test_stale_pending_clear_spares_adopted_item(self):
        for case in ('recurring-cause', 'stale-shared-item'):
            with self.subTest(case=case):
                self.queue = st.QueueServiceStandIn()
                self.ended.clear()
                worker = self.worker()
                saved = self.damage('pointer')
                patch = None
                try:
                    self.refuse()
                    first = self.assert_cause('pointer_damaged', None)
                    original = worker._faults[P]
                    if case == 'stale-shared-item':
                        stale = self.m._Fault(P, None, 'pointer_damaged', self.clock(), first)
                        count = self.queue.calls.count(('clear', first))
                        worker._clear(stale, worker._generation - 1)
                        self.assertIn(stale, worker._clear_pending)
                        self.assertEqual(self.queue.calls.count(('clear', first)), count)
                        self.tick(worker)
                        self.assertEqual(self.queue.calls.count(('clear', first)), count)
                        self.assertNotIn(stale, worker._clear_pending)
                        self.assertTrue(self.queue.items[first]['open'])
                        self.assertEqual(original.item_id, first)
                        continue
                    self.queue.behavior[('clear', P)] = st.Refusal()
                    patch = self.folder_fault()
                    self.tick(worker)
                    second = next(item['item_id'] for item in self.items()
                                  if item['condition_key']['cause'] == 'folder_unusable')
                    self.assertIn(original, worker._clear_pending)
                    patch.stop(); patch = None
                    self.queue.behavior.pop(('clear', P))
                    count = self.queue.calls.count(('clear', first))
                    for _ in range(3):
                        self.tick(worker)
                        self.assertTrue(self.queue.items[first]['open'])
                        self.assertFalse(self.queue.items[second]['open'])
                        self.assertEqual(worker._faults[P].item_id, first)
                        self.assertEqual(worker._clear_pending, [])
                        self.assertEqual(self.queue.calls.count(('clear', first)), count)
                    self.repair(saved)
                    self.tick(worker)
                    self.assertEqual(self.queue.calls.count(('clear', first)), count + 1)
                    self.assertFalse(self.queue.items[first]['open'])
                    self.assertEqual(self.ended, [(P, 'pointer_damaged')])
                finally:
                    if patch is not None:
                        patch.stop()
                    self.repair(saved)
                    self.shutdown()

    def test_pointer_damaged_then_folder_cause_change(self):
        self.worker();self.damage('pointer');self.refuse();self.assert_cause('pointer_damaged',None)
        p=self.folder_fault()
        try:self.tick();self.assert_cause('folder_unusable',None)
        finally:p.stop()
        self.tick();self.assert_cause('pointer_damaged',None)
        self.service.set_current(P,C,ledger_fd=self.store.fd,expected=seal_types.POINTER_UNREADABLE,bind='first')
        self.sync();self.assertEqual(self.items(),[])

    def test_unknown_recheck_clears_nothing(self):
        w=self.worker();self.damage('pointer');self.refuse();w.catalogue_closing();before=list(self.queue.calls)
        self.tick(w);self.assertEqual(self.items()[0]['condition_key']['cause'],'pointer_damaged')
        self.assertFalse(any(c[0] in ('clear','raise') for c in self.queue.calls[len(before):]))

    def test_fault_ended_signal_once_per_fault(self):
        worker = self.worker(); positions = []
        def ended(project, cause):
            positions.append(len(self.queue.calls)); self.ended.append((project, cause))
        worker.on_fault_ended = ended
        patch = self.folder_fault()
        try:
            self.refuse()
        finally:
            patch.stop()
        self.tick(); self.assertEqual(self.ended, [(P, 'folder_unusable')])
        for _ in range(10):
            self.tick()
        self.assertEqual(len(self.ended), 1)
        patch = self.folder_fault()
        try:
            self.refuse()
        finally:
            patch.stop()
        self.damage('pointer'); self.tick()
        self.assert_cause('pointer_damaged', None)
        self.service.set_current(P, C, ledger_fd=self.store.fd,
                                 expected=seal_types.POINTER_UNREADABLE, bind='first')
        self.sync(); self.tick()
        self.assertEqual(self.ended, [(P, 'folder_unusable'), (P, 'pointer_damaged')])
        last_clear = max(i for i, call in enumerate(self.queue.calls) if call[0] == 'clear')
        self.assertGreater(positions[-1], last_clear)
        self.assertEqual(self.items(), [])

    def test_fault_ended_signal_without_queue_item(self):
        self.worker();self.queue.behavior[('raise',P)]=st.Unavailable();saved=self.damage();self.refuse()
        self.repair(saved);self.tick();self.assertEqual(self.items(),[])
        self.assertEqual(self.ended,[(P,'counter_unusable')])

    def test_fault_ended_signal_unknown_recheck(self):
        self.test_unknown_recheck_clears_nothing();self.assertEqual(self.ended,[])

    def test_fault_ended_signal_pointer_unset_inside_age(self):
        self.test_pointer_unset_brief_window_raises_nothing()
        self.assertEqual(self.ended,[(P,'pointer_unset')])

    def test_fault_ended_callee_raises(self):
        import fcntl
        calls=[]
        def ended(p,c):
            fd=os.open(self.root/'projects'/p/'lock',os.O_RDWR)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
            calls.append((p,c));raise RuntimeError('secret should never escape')
        self.worker(on_fault_ended=ended)
        for _ in range(2):
            saved=self.damage();self.refuse();self.repair(saved);self.tick()
        self.assertEqual(calls,[(P,'counter_unusable')]*2)
        ds=[d for d in self.diags if isinstance(d,seal_types.SealCalleeFailed)]
        self.assertEqual([(d.callee,d.exc_type,d.project_id) for d in ds],[('on_fault_ended','RuntimeError',P)]*2)
        self.assertNotIn('secret',str(ds))

    def test_worker_catalogue_reader_follows_hooks(self):
        w=self.worker(opened=False);self.assertEqual(self.readers,[])
        self.damage();self.refuse();self.assertEqual(self.items(),[])
        w.catalogue_opened();self.sync(w);self.assert_cause('counter_unusable')
        owner=self.readers[-1].owner;self.assertNotEqual(owner,threading.get_ident())
        w.catalogue_closing();self.assertTrue(self.readers[-1].closed);count=len(self.readers)
        self.tick(w);self.assertEqual(len(self.readers),count)
        self.assertTrue(all(t==owner for op,t in self.readers[-1].trace if op!='interrupt'))

    def test_catalogue_close_revokes_before_use(self):
        entered,release=threading.Event(),threading.Event()
        def opener(root):
            reader=self.opener(root);entered.set();await_event(release);return reader
        w=self.worker(opened=False,open_catalogue_reader=opener);w.catalogue_opened();await_event(entered)
        wait=self.workers[-1][2];done=threading.Event()
        t=threading.Thread(target=lambda:(w.catalogue_closing(),done.set()),daemon=True);t.start()
        wait.expire();wait.expire();await_event(done)
        self.assertFalse(self.readers[-1].closed);release.set();t.join(30);self.sync(w)
        self.assertTrue(self.readers[-1].closed)
        self.assertFalse(any(op=='sql' for op,_ in self.readers[-1].trace))

    def test_catalogue_admission_and_stale_result(self):
        entered,release,returned,decide=(threading.Event() for _ in range(4))
        fresh_entered,fresh_release=threading.Event(),threading.Event()
        held=[False]; stale_reader=[None]
        def route(conn,p):
            if held[0]:
                stale_reader[0]=conn
                entered.set();await_event(release)
                result=store_layout.route_state(conn,p);returned.set();await_event(decide);return result
            if stale_reader[0] is not None and conn is not stale_reader[0]:
                fresh_entered.set();await_event(fresh_release)
            return store_layout.route_state(conn,p)
        w=self.worker(route_state=route);saved=self.damage();self.refuse();self.repair(saved)
        held[0]=True;self.workers[-1][1].advance();await_event(entered)
        wait=self.workers[-1][2];done=threading.Event()
        t=threading.Thread(target=lambda:(w.catalogue_closing(),done.set()),daemon=True);t.start()
        wait.expire();wait.expire();await_event(done);release.set();await_event(returned)
        before=list(self.queue.calls);w.catalogue_opened();held[0]=False;decide.set();t.join(30)
        try:
            await_event(fresh_entered)
            self.assertTrue(stale_reader[0].closed)
            self.assertFalse(any(c[0] in ('clear','raise') for c in self.queue.calls[len(before):]))
            self.assertEqual(self.ended,[])
            self.assert_cause('counter_unusable')
        finally:fresh_release.set()
        self.sync(w)
        self.assertGreaterEqual(len(self.readers),2)
        self.assertEqual(self.items(),[]);self.assertEqual(self.ended,[(P,'counter_unusable')])

    def test_backup_exclusion_applied_and_checked(self):
        for answer in ('add_failed','check_failed','not_excluded'):
            w=self.worker(backup=answer);items=self.items(None);self.assertEqual(len(items),1)
            item=items[0];self.assertEqual(item['condition_key'],'backup_exclusion')
            self.assertEqual(item['texts'],st.backup_texts());self.assertEqual(item['pins'],st.backup_pins())
            self.assertEqual(self.reserve(),self.tx);self.assertEqual(self.ended,[]);self.stop(w)
            self.queue.behavior[('recover',None)]=st.Refusal()
            w=self.worker();self.assertEqual(len(self.items(None)),1)
            self.queue.behavior.clear();self.tick(w);self.assertEqual(self.items(None),[]);self.stop(w)
        self.queue.behavior[('raise',None)]=st.Refusal();w=self.worker(backup='add_failed')
        self.assertEqual(self.items(None),[]);self.queue.behavior.clear();self.tick(w)
        self.assertEqual(len(self.items(None)),1)
        self.stop(w)
        for case in ('two-starts', 'retry-one-key'):
            with self.subTest(case=case):
                self.queue = st.QueueServiceStandIn(); self.clock.value = 2000
                with receipt_queue(self.queue) as receipts:
                    try:
                        if case == 'two-starts':
                            w = self.worker(backup='add_failed')
                            first = self.items(None)[0]['item_id']
                            self.stop(w)
                            w = self.worker(backup='excluded')
                            self.assertEqual(self.items(None), [])
                            self.stop(w)
                            w = self.worker(backup='add_failed')
                            self.assertNotEqual(receipts.keys[0], receipts.keys[-1],
                                                'backup restart reused raise key')
                            self.assertEqual(len(self.items(None)), 1)
                            self.assertNotEqual(self.items(None)[0]['item_id'], first)
                            self.assertFalse(self.queue.items[first]['open'])
                        else:
                            self.queue.behavior[('raise', None)] = st.Refusal()
                            w = self.worker(backup='add_failed')
                            self.assertEqual(self.items(None), [])
                            before_advance = len(receipts.keys)
                            self.assertGreater(before_advance, 0)
                            self.clock.advance(1000); self.queue.behavior.clear(); self.tick(w)
                            self.assertGreater(len(receipts.keys), before_advance)
                            self.assertEqual(len(set(receipts.keys)), 1, 'backup retry changed key')
                            self.assertEqual(len(self.items(None)), 1)
                    finally:
                        self.shutdown()

    def test_secret_never_in_messages(self):
        self.worker();faults=[]
        for kind in ('counter','high-water','pointer'):
            saved=self.damage(kind)
            try:self.reserve()
            except seal_types.SealUnusable as exc:faults.append(exc)
            self.sync();self.repair(saved);self.tick()
        self.refuse(chain=D)
        samples=[str(x) for x in faults+self.diags]+[repr(x) for x in self.queue.items.values()]
        keys=self.root/'keys'
        secrets=[p.read_bytes() for p in keys.iterdir() if p.is_file()] if keys.exists() else []
        for text in samples:
            self.assertNotIn(str(keys),text)
            for secret in secrets:
                self.assertNotIn(repr(secret),text)
                self.assertNotIn(secret.hex(),text)

    def test_seal_hooks_forward_deadline(self):
        from relay_engine.api_writer import StopDeadline
        renderer, worker = mock.Mock(), mock.Mock()
        deadline = StopDeadline()
        hooks = self.m.SealHooks(renderer, worker)
        hooks.ledger_closing(P, deadline=deadline)
        hooks.catalogue_closing(deadline=deadline)
        renderer.ledger_closing.assert_called_once_with(P, deadline=deadline)
        worker.catalogue_closing.assert_called_once_with(deadline=deadline)

    def test_closing_half_waits_take_remainder(self):
        from relay_engine.api_writer import StopDeadline
        for initial, arm_during_wait, expected in (
                (0.5, False, [0.5]), (None, False, [5, 5]), (None, True, [5])):
            with self.subTest(initial=initial, late_arm=arm_during_wait):
                worker = self.worker(opened=False)
                deadline = StopDeadline()
                if initial is not None:
                    deadline._arm(initial)
                now, waits = [0.0], []
                def wait(done, seconds):
                    waits.append(seconds)
                    if arm_during_wait:
                        deadline._arm(now[0] + 4)
                    now[0] += seconds
                    return False
                worker.wait = wait
                with mock.patch.object(self.m, 'monotonic', lambda: now[0], create=True):
                    worker.catalogue_closing(deadline=deadline)
                self.assertEqual(waits, expected)

    def test_closing_zero_budget_fences_without_waiting(self):
        from relay_engine.api_writer import StopDeadline
        worker = self.worker()
        deadline = StopDeadline()
        deadline._arm(0)
        worker.wait = mock.Mock(side_effect=AssertionError('zero budget waited'))
        worker.catalogue_closing(deadline=deadline)
        self.assertFalse(worker._leave)
        worker.wait.assert_not_called()

    def test_closing_without_stop_keeps_10s(self):
        from relay_engine.api_writer import StopDeadline
        worker = self.worker(opened=False)
        worker.wait = mock.Mock(return_value=False)
        worker.catalogue_closing(deadline=StopDeadline())
        self.assertEqual([c.args[1] for c in worker.wait.call_args_list], [5, 5])

    def test_catalogue_ack_per_acquired_reader(self):
        for mode, expected in (
                ('normal', True), ('stale', True), ('stale_raise', False),
                ('read', True), ('read_raise', False),
                ('failed_open', True), ('never_held', True)):
            with self.subTest(mode=mode):
                worker = self.worker(opened=False)
                tick = self.workers[-1][1]
                began, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                def open_reader(root):
                    if mode.startswith('stale') or mode == 'never_held':
                        began.set()
                        await_event(release)
                    if mode in ('failed_open', 'never_held'):
                        raise OSError('failed open')
                    reader = self.opener(root)
                    close = reader.close
                    if mode.endswith('_raise'):
                        def raise_after_close():
                            close()
                            raise OSError('failed close')
                        reader.close = raise_after_close
                    return reader
                worker.open_catalogue_reader = open_reader
                worker.wait = lambda event, seconds: event.wait(2)
                worker.catalogue_opened()
                if mode.startswith('stale') or mode == 'never_held':
                    await_event(began)
                    original_wait = worker.wait
                    def wait(event, seconds):
                        release.set()
                        return original_wait(event, seconds)
                    worker.wait = wait
                else:
                    self.sync(worker)
                if mode.startswith('read'):
                    def read_fault(*args):
                        raise OSError('failed read')
                    worker.route_state = read_fault
                    self.damage()
                    self.refuse()
                    self.sync(worker)
                try:
                    self.assertIs(worker.catalogue_closing(), expected)
                finally:
                    release.set()
                    tick.finish()

    def test_late_confirmation_edge(self):
        for publish_before_read in (False, True):
            with self.subTest(publish_before_read=publish_before_read):
                worker = self.worker()
                original = worker._reader.close
                entered, release = threading.Event(), threading.Event()
                def close():
                    entered.set()
                    await_event(release)
                    original()
                worker._reader.close = close
                self.addCleanup(release.set)
                calls = []
                def wait(event, seconds):
                    calls.append(seconds)
                    if len(calls) == 2 and publish_before_read:
                        release.set()
                        await_event(event)
                    return False
                worker.wait = wait
                try:
                    answer = worker.catalogue_closing()
                finally:
                    release.set()
                await_event(entered)
                self.sync(worker)
                self.assertIs(answer, publish_before_read)

    def test_worker_stop_deadline_with_held_catalogue_read(self):
        from time import monotonic
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        armed = threading.Event()
        def route(conn, project):
            if armed.is_set():
                entered.set()
                await_event(release)
            return store_layout.route_state(conn, project)
        worker = self.worker(route_state=route)
        _, ticker, _ = self.workers[-1]
        ticker.finish()
        worker._ticker.join(5)
        self.assertFalse(worker._ticker.is_alive())
        armed.set()
        self.damage()
        producer = threading.Thread(target=self.refuse, daemon=True)
        producer.start()
        try:
            await_event(entered)
            self.assertIs(worker.stop(monotonic()), False)
            self.assertFalse(self.readers[-1].closed)
        finally:
            release.set()
            producer.join(5)
        self.assertTrue(producer.is_alive() is False)

    def test_worker_stop_needs_owner_close_ack(self):
        from time import monotonic
        entered, release = threading.Event(), threading.Event()
        worker = self.worker()
        self.workers[-1][1].finish()
        reader = worker._reader
        original = reader.close
        def close():
            entered.set()
            await_event(release)
            original()
        reader.close = close
        try:
            result = worker.stop(monotonic())
            await_event(entered)
            self.assertIsNone(worker._reader)
            self.assertIs(result, False)
        finally:
            release.set()
        self.assertIs(worker.stop(), True)

    def test_worker_second_thread_start_failure(self):
        worker = self.worker(opened=False)
        self.stop(worker)
        # Start a fresh instance with the same fixture inputs without launching it.
        with mock.patch.object(self.m.SealWorker, 'start', lambda self: self._idle.set()):
            worker = self.worker(opened=False)
        original = threading.Thread.start
        def start(thread):
            if thread.name == 'seal-worker-tick':
                raise RuntimeError('ticker start fault')
            original(thread)
        with mock.patch.object(threading.Thread, 'start', start):
            with self.assertRaisesRegex(RuntimeError, 'ticker start fault'):
                worker.start()
        self.assertIs(worker.stop(), True)

    def test_raising_close_keeps_stop_false(self):
        from time import monotonic

        worker = self.worker()
        original = worker._reader.close
        def broken_close():
            original()
            raise OSError('close outcome unavailable')
        worker._reader.close = broken_close
        self.workers[-1][1].finish()
        owner = worker._thread
        worker.stop(monotonic() + 5)
        owner.join(timeout=5)
        self.assertFalse(owner.is_alive())
        self.assertIs(worker.stop(monotonic() + 5), False)

    def test_worker_rejected_candidate_reader_acknowledged(self):
        entered, release = threading.Event(), threading.Event()
        def open_reader(root):
            entered.set()
            await_event(release)
            reader = self.opener(root)
            original = reader.close
            def close():
                original()
                raise OSError('candidate close failure')
            reader.close = close
            return reader
        worker = self.worker(opened=False, open_catalogue_reader=open_reader)
        self.workers[-1][1].finish()
        worker.catalogue_opened()
        try:
            await_event(entered)
            worker.wait = lambda event, seconds: release.set() or event.wait(2)
            self.assertIs(worker.catalogue_closing(), False)
        finally:
            release.set()
        self.assertIs(worker.stop(), False)

    def test_hooks_object_delegates(self):
        calls=[];owner=threading.get_ident()
        class Delegate:
            def __getattr__(self,name):
                return lambda *a, **kw:calls.append((name,a,threading.get_ident(),kw))
        hooks=self.m.SealHooks(Delegate(),Delegate())
        names={n for n in vars(type(hooks)) if not n.startswith('_')}
        self.assertEqual(names,{'catalogue_opened','catalogue_closing','ledger_opened','ledger_serving','ledger_closing'})
        hooks.catalogue_opened();hooks.ledger_opened(P);hooks.ledger_serving(P);hooks.ledger_closing(P);hooks.catalogue_closing()
        self.assertEqual([c[0] for c in calls],['catalogue_opened','ledger_opened','ledger_serving','ledger_closing','catalogue_closing'])
        self.assertTrue(all(c[2]==owner for c in calls))
        self.assertEqual([c[3] for c in calls], [{}, {}, {}, {'deadline': None}, {'deadline': None}])

    def test_queue_outcomes_not_raised_yet(self):
        w=self.worker();self.damage()
        for answer in (st.Refusal(),st.Unavailable(),RuntimeError('secret')):
            self.queue.behavior[('raise',P)]=answer;self.refuse();self.assertEqual(self.items(),[])
            self.queue.behavior.clear();self.tick();self.assert_cause('counter_unusable')
            # End and re-open the condition for the next independent outcome.
            path=self.path();saved=self.damage();self.repair(saved)
            self.service.set_current(P,D,ledger_fd=self.store.fd,expected=C,bind='first');self.sync()
            self.service.return_current(P,C,expected=D);self.sync()
        self.stop(w);self.queue.behavior[('raise',None)]=RuntimeError('secret')
        w=self.worker(backup='check_failed');self.assertEqual(self.items(None),[])
        ds=[d for d in self.diags if isinstance(d,seal_types.SealCalleeFailed)]
        self.assertIn(('queue','RuntimeError',None),[(d.callee,d.exc_type,d.project_id) for d in ds])
        self.queue.behavior.clear();self.tick(w);self.assertEqual(len(self.items(None)),1)


if __name__=='__main__':unittest.main()
