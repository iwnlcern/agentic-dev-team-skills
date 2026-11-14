"""Renderer ordering, reader ownership and durable high-water boundaries."""
import ast
from contextlib import ExitStack
import errno
import fcntl
import importlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from relay_engine.tests import seal_standins as stand

P = '10000000-0000-4000-8000-000000000001'
Q = '10000000-0000-4000-8000-000000000002'
C = '20000000-0000-4000-8000-000000000002'
D = '30000000-0000-4000-8000-000000000003'
R = '40000000-0000-4000-8000-000000000004'


def key_child(root, project, chain, pipe, rename_control=False):
    """Each child owns its service and only exposes public key ids."""
    from relay_engine import approval_seal as a, seal_files, seal_renderer, store_layout
    ledger = stand.Ledger()
    ledger.put(stand.event_row(project, chain, 0, 'chain-start'))
    ledger_path = store_layout.ledger_path(root, project)
    ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(ledger_path, os.O_CREAT | os.O_RDWR, 0o600)
    service = a.SealService(root, runtime_instance_id=R, clock_ms=lambda: 0, report=lambda r: None)
    original = os.link
    created = False
    def link(source, target, *args, **kwargs):
        nonlocal created
        if str(target) == '1':
            pipe.send('before-link'); assert pipe.recv() == 'go'
            if rename_control:
                directory = kwargs['src_dir_fd']
                os.rename(source, target, src_dir_fd=directory, dst_dir_fd=directory)
                # Keep the helper's normal unlink step, after replacing the final name.
                original(target, source, src_dir_fd=directory, dst_dir_fd=directory)
                created = True
                return
            result = original(source, target, *args, **kwargs)
            created = True
            return result
        return original(source, target, *args, **kwargs)
    renderer = None
    try:
        with a.install(service), mock.patch.object(seal_files.os, 'link', side_effect=link):
            service.set_current(project, chain, ledger_fd=fd, expected=None, bind='first')
        pipe.send('key-created'); assert pipe.recv() == 'render'
        renderer = seal_renderer.SealRenderer(service, store_root=root,
                    open_reader=ledger.open_reader, events_after=stand.events_after,
                    clock_ms=lambda: 0, tick=lambda s: False,
                    wait=lambda e, s: e.wait(30), bound_s=10)
        service.attach_renderer(renderer)
        renderer.ledger_opened(project); renderer.ledger_serving(project)
        pipe.send({'key_id': service.read_record(project, chain, 0).key_id, 'created': created})
    finally:
        if renderer is not None:
            renderer.ledger_closing(project); renderer.stop()
        os.close(fd); pipe.close()


class SealRendererTests(unittest.TestCase):
    def setUp(self):
        # The individual initial RED is the missing production module.
        self.module = importlib.import_module('relay_engine.seal_renderer')
        self.a = importlib.import_module('relay_engine.approval_seal')
        self.slots = importlib.import_module('relay_engine.seal_slots')
        self.types = importlib.import_module('relay_engine.seal_types')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'seal'
        self.fd = os.open(self.base / 'ledger', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.fd)
        for project in (P, Q):
            directory = self.base / 'projects' / project
            directory.mkdir(parents=True)
            os.link(self.base / 'ledger', directory / 'ledger.sqlite3')
        self.ledger = stand.Ledger(); self.reports = []; self.diags = []
        self.clock = stand.ManualClock(); self.tick = stand.ManualTick()
        self.service = self.a.SealService(self.root, runtime_instance_id=R,
                        clock_ms=self.clock, report=self.reports.append)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(self.a.install(self.service))
        self.service.set_current(P, C, ledger_fd=self.fd, expected=None, bind='first')
        self.reports.clear()
        from relay_engine import strings
        strings.set_diagnostic_sink(self.diags.append)
        self.addCleanup(strings.set_diagnostic_sink, None)
        self.renderers = []
        self.addCleanup(self.cleanup_renderers)
        self.errors = []

    def cleanup_renderers(self):
        self.tick.finish()
        for renderer in reversed(self.renderers):
            for project in (P, Q):
                generations = renderer.generations(project)
                if generations and generations[-1].state not in ('closed', 'closing', 'closing_late'):
                    renderer.ledger_closing(project)
            renderer.stop()

    def renderer(self, *, reader=None, events=None, wait=None, root=None):
        renderer = self.module.SealRenderer(self.service, store_root=root or self.base,
                    open_reader=reader or self.ledger.open_reader,
                    events_after=events or stand.events_after, clock_ms=self.clock,
                    tick=self.tick, wait=wait or (lambda e, s: e.wait(30)), bound_s=10)
        self.renderers.append(renderer)
        if self.service.renderer is None:
            self.service.attach_renderer(renderer)
        return renderer

    def open(self, renderer, *, serving=False, project=P):
        renderer.ledger_opened(project)
        if serving:
            renderer.ledger_serving(project)

    def rows(self, through, *, chain=C, project=P, ledger=None):
        target = ledger or self.ledger
        for n in range(through + 1):
            target.put(stand.event_row(project, chain, n, 'chain-start' if n == 0 else 'approve'))

    def path(self, suffix='high-water', *, chain=C):
        return self.root / 'projects' / P / (chain + '.' + suffix)

    def high(self, *, chain=C):
        fd = os.open(self.path(chain=chain), os.O_RDONLY)
        try:
            return self.slots.read_slots(fd, kind=self.slots.ItemKind.HIGH_WATER,
                                          binding=(P, chain), ledger_identity=None).value
        finally:
            os.close(fd)

    def damage(self, path, offset=None):
        fd = os.open(path, os.O_RDWR)
        try:
            if offset is None:
                os.pwrite(fd, bytes(256), 0); os.pwrite(fd, bytes(256), 65536)
            else:
                os.pwrite(fd, bytes(256), offset)
        finally:
            os.close(fd)

    def spawn(self, call):
        done = threading.Event()
        def run():
            try:
                call()
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=run); thread.start()
        self.addCleanup(self.join, thread)
        return done

    def join(self, thread):
        thread.join(30); self.assertFalse(thread.is_alive()); self.assertEqual(self.errors, [])

    def hold(self, call):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args, **kwargs):
            entered.set(); self.assertTrue(release.wait(30))
            return call(*args, **kwargs)
        return blocked, entered, release

    def real_store(self):
        store = stand.TempStore(P)
        def cleanup():
            self.cleanup_renderers(); store.close()
        self.addCleanup(cleanup)
        self.service.set_current(P, C, ledger_fd=store.fd, expected=C, bind='rebind')
        return store


    def test_stop_drain_raises_unsignalled_commit(self):
        renderer = self.renderer(); self.open(renderer, serving=True)
        self.rows(4)
        renderer.ledger_closing(P)
        self.assertEqual(self.high(), 4)
        self.assertIsNone(self.service.read_record(P, C, 0))
        self.assertEqual(renderer.generations(P)[-1].state, 'closed')
        self.assertEqual(self.ledger.trace[-1][0], 'close')

    def test_record_publish_failure_reports_nothing(self):
        renderer = self.renderer(); self.open(renderer)
        self.rows(2); self.reports.clear()
        with mock.patch.object(self.service.folder, 'publish_record',
                               side_effect=OSError(errno.ENOSPC, 'full')):
            renderer.drain(P, C, through=0)
        self.assertEqual(self.reports, [])
        self.assertEqual(self.high(), 2)
        self.assertIsNone(self.service.read_record(P, C, 0))
        renderer.drain(P, C, through=0)
        self.assertEqual(self.service.read_record(P, C, 2).number, 2)

    def test_damaged_chain_does_not_stop_later_chains(self):
        self.service.set_current(P, D, ledger_fd=self.fd, expected=C, bind='first')
        self.damage(self.path('counter'))
        renderer = self.renderer()
        reader = self.ledger.open_reader(self.base, P)
        owner = self.module._Owner(P, 1, reader=reader, ident=threading.get_ident())
        renderer._owners[P] = [owner]
        observed = []
        try:
            for n, kind in enumerate(('start', 'backstop', 'stop'), 1):
                self.rows(n, chain=D)
                owner.closing = kind == 'stop'
                try:
                    if kind == 'start':
                        renderer._start_drain(owner)
                    elif kind == 'backstop':
                        renderer._backstop_pass(owner, frozenset())
                    else:
                        renderer._stop_drain(owner)
                except self.types.SealUnusable:
                    pass
                observed.append(self.high(chain=D))
            self.assertEqual(observed, [1, 2, 3])
        finally:
            reader.close()
            owner.state = 'closed'
            if getattr(owner, 'ledger_fd', None) is not None:
                os.close(owner.ledger_fd)

    def test_record_walk_bounded(self):
        renderer = self.renderer(); self.open(renderer)
        self.rows(49); renderer.drain(P, C, through=0)
        previous = self.service.read_record(P, C, 49)
        self.rows(50)
        real_listdir = os.listdir
        directory = self.root / 'projects' / P / 'records' / C
        listings = []
        def listed(path):
            if Path(path) == directory:
                listings.append(path)
            return real_listdir(path)
        with mock.patch.object(self.service, 'open_record', wraps=self.service.open_record) as opened,                 mock.patch.object(os, 'listdir', side_effect=listed):
            renderer.drain(P, C, through=0)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(len(listings), 1)
        self.assertEqual(self.service.read_record(P, C, 50).prev_mac, previous.mac)

    def test_passes_never_seal_out_of_a_replaced_ledger(self):
        renderer = self.renderer(); self.open(renderer, serving=True)
        other = os.open(self.base / 'replacement', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.service.set_current(P, C, ledger_fd=other, expected=C, bind='rebind')
        finally:
            os.close(other)
        self.rows(3)
        renderer.committed(P, C)
        # A queued explicit drain is a fence; its caller's contract has no gate.
        # Do not use it here: queue a no-op through the existing owner lane.
        owner = renderer._newest(P)
        fence = self.module._Request('pass', skip=frozenset((C,)))
        renderer._enqueue(owner, fence)
        self.assertTrue(fence.done.wait(30))
        renderer.start(); self.tick.until_calls(1)
        self.tick.advance(); self.tick.until_calls(2)
        fence = self.module._Request('pass', skip=frozenset((C,)))
        renderer._enqueue(owner, fence); self.assertTrue(fence.done.wait(30))
        renderer.ledger_closing(P)
        self.assertEqual(self.high(), 0)
        self.assertIsNone(self.service.read_record(P, C, 0))

    def test_drain_raises_to_through_first(self):
        self.rows(4)
        renderer = self.renderer(); self.open(renderer)
        self.assertEqual(renderer.drain(P, C, through=4), 4)
        observations = []
        def observed(*args):
            observations.append(self.high())
            return stand.events_after(*args)
        renderer2 = self.renderer(events=observed); self.open(renderer2)
        self.assertEqual(renderer2.drain(P, C, through=6), 6)
        self.assertTrue(observations); self.assertEqual(observations[0], 6)

    def test_renderer_record_invalid_after_publish(self):
        self.service.set_current(P, D, ledger_fd=self.fd, expected=C, bind='first')
        renderer = self.renderer(); self.open(renderer, serving=True)
        self.rows(2); self.rows(1, chain=D)
        original = self.service.folder.publish_record
        published = []
        def marked(project, chain, number, data):
            result = original(project, chain, number, data)
            if (project, chain, number) == (P, C, 0):
                self.service.folder.write_mark(P, C, b'another-holder-broke-chain')
                published.append(number)
            return result
        with mock.patch.object(self.service.folder, 'publish_record', side_effect=marked):
            try:
                renderer.drain(P, C, through=2)
            except self.types.RecordInvalid:
                pass  # The later C2 drain is the generation-survival witness.
        records = self.root / 'projects' / P / 'records' / C
        self.assertEqual(published, [0])
        self.assertTrue((records / '0').is_file())
        self.assertFalse((records / '1').exists())
        results, errors = [], []
        def later():
            try:
                results.append(renderer.drain(P, D, through=1))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=later); thread.start(); thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [], 'later C2 drain must preserve the reader generation')
        self.assertEqual(results, [1])
        self.assertEqual(renderer.generations(P)[-1].state, 'serving')

    def test_drain_zero_skips_step_one(self):
        renderer = self.renderer(); self.open(renderer)
        self.damage(self.path())
        calls = []
        original = self.service._locked
        def locked(*args, **kwargs):
            calls.append(kwargs['exclusive']); return original(*args, **kwargs)
        with mock.patch.object(self.service, '_locked', side_effect=locked):
            with self.assertRaises(self.types.SealUnusable):
                renderer.drain(P, C, through=0)
        self.assertTrue(calls); self.assertFalse(calls[0])

    def test_drain_never_lowers(self):
        renderer = self.renderer(); self.open(renderer)
        renderer.drain(P, C, through=7); before = self.path().read_bytes()
        self.assertEqual(renderer.drain(P, C, through=5), 7)
        self.assertEqual(self.path().read_bytes(), before)

    def test_drain_two_in_process_holders(self):
        lock = self.path('counter').parent / 'lock'
        first, second = os.open(lock, os.O_RDWR), os.open(lock, os.O_RDWR)
        duplicate = os.dup(first)
        try:
            fcntl.flock(first, fcntl.LOCK_EX)
            with self.assertRaises(OSError) as caught:
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn(caught.exception.errno, (errno.EWOULDBLOCK, errno.EAGAIN))
            fcntl.flock(duplicate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(duplicate); os.close(first); os.close(second)
        one = self.renderer(); two = self.renderer(); self.open(one); self.open(two)
        values = []
        def drain():
            for n in range(1, 1001):
                one.drain(P, C, through=n); values.append(self.high())
        def raises():
            for n in range(1, 1001):
                self.ledger.put(stand.event_row(P, C, n))
                two.drain(P, C, through=0)
        done = self.spawn(drain); other = self.spawn(raises)
        self.assertTrue(done.wait(30)); self.assertTrue(other.wait(30))
        self.assertEqual(values, sorted(values)); self.assertEqual(self.high(), 1000)
        self.assertFalse(any(isinstance(x, self.types.SealSlotDamaged) for x in self.diags))

    def test_drain_after_rechain_writes_nothing(self):
        renderer = self.renderer(); self.open(renderer)
        self.rows(6); renderer.drain(P, C, through=6)
        self.service.set_current(P, D, ledger_fd=self.fd, expected=C, bind='first')
        before = self.path().read_bytes(); fresh = self.path(chain=D).read_bytes()
        self.assertEqual(renderer.drain(P, C, through=6), 6)
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.path(chain=D).read_bytes(), fresh)

    def test_drain_through_survives_damaged_counter(self):
        for n in range(1, 7):
            self.service.reserve(P, C, kind='approve', subject='s', payload=b'{}',
                                  transaction=n, declared_kinds=('approve',))
        self.damage(self.path('counter'), 0)  # Sequence 6; sequence 5 remains.
        self.assertEqual(self.service.counter_head(P, C), 5)
        self.rows(6); renderer = self.renderer(); self.open(renderer)
        self.assertEqual(renderer.drain(P, C, through=6), 6)
        with self.ledger.lock:
            self.ledger.rows = [r for r in self.ledger.rows if r.number < 6]
        self.assertEqual(renderer.drain(P, C, through=0), 6)
        self.assertGreater(self.service.rendered_through(P, C), 5)

    def test_drain_callers_census(self):
        def callers(source, module):
            tree = ast.parse(source); found = set()
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != 'drain':
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'drain':
                            found.add((module, fn.name))
            return found
        found = set()
        root = Path(__file__).parents[1]
        for path in root.rglob('*.py'):
            if 'tests' not in path.relative_to(root).parts:
                found |= callers(path.read_text(), path.stem)
        expected = {('seal_renderer', '_start_drain')}
        if (root / 'approvals.py').exists():
            expected.add(('approvals', 'commit'))
        self.assertEqual(found, expected)
        self.assertEqual(callers('def extra(r):\n r.drain("p", "c", through=0)\n', 'fixture'),
                         {('fixture', 'extra')})

    def test_high_water_rises_without_record(self):
        renderer = self.renderer(); self.open(renderer); self.rows(5)
        with mock.patch.object(self.service, 'seal_record', return_value=None):
            self.assertEqual(renderer.drain(P, C, through=0), 5)
        self.assertIsNone(self.service.read_record(P, C, 0))
        self.service.mark_broken(P, C, cause='counter_behind', number=5)
        self.rows(6); self.assertEqual(renderer.drain(P, C, through=0), 6)

    def test_high_water_unwritable_refuses(self):
        renderer = self.renderer(); self.open(renderer); self.rows(5)
        original = self.service._open
        def opened(project, name, *, write=False):
            if name.endswith('.high-water') and write:
                raise PermissionError('fixture')
            return original(project, name, write=write)
        with mock.patch.object(self.service, '_open', side_effect=opened):
            with self.assertRaises(self.types.SealUnusable) as caught:
                renderer.drain(P, C, through=0)
        self.assertEqual(caught.exception.cause, 'high_water_unusable')
        self.assertEqual([r.cause for r in self.reports], ['high_water_unusable'])
        self.damage(self.path())
        with self.assertRaises(self.types.SealUnusable) as caught:
            self.service.rendered_through(P, C)
        self.assertEqual(caught.exception.cause, 'high_water_unusable')

    def test_reader_never_sees_half_written_slot(self):
        renderer = self.renderer(); self.open(renderer)
        done = self.spawn(lambda: [renderer.drain(P, C, through=n) for n in range(1, 1001)])
        values = []
        while not done.is_set():
            values.append(self.service.rendered_through(P, C))
        self.assertTrue(values); self.assertEqual(values, sorted(values)); self.assertEqual(self.high(), 1000)
        self.assertFalse(any(isinstance(x, self.types.SealSlotDamaged) for x in self.diags))

    def test_damaged_slot_after_rollback_reuses(self):
        reserve = lambda tx: self.service.reserve(P, C, kind='approve', subject='s', payload=b'{}',
                                transaction=tx, declared_kinds=('approve',))
        self.assertEqual(reserve(1), 1)
        self.damage(self.path('counter'), 65536)
        self.assertEqual(reserve(2), 1)
        self.rows(1); renderer = self.renderer(); self.open(renderer)
        self.assertEqual(renderer.drain(P, C, through=0), 1)
        self.assertEqual([r.number for r in self.ledger.rows], [0, 1])
        self.assertEqual([r.number for r in self.service.reservations_above(P, C, 0)], [1])
        self.assertIsNotNone(self.service.read_record(P, C, 1))

    def test_key_seq_race(self):
        # Remove only a fixture key before either project is created.
        (self.root / 'keys' / '1').unlink()
        other = self.service
        barrier = threading.Barrier(2); original = os.link
        def linked(source, target, *args, **kwargs):
            if str(target) == '1':
                barrier.wait(30)
            return original(source, target, *args, **kwargs)
        with mock.patch.object(os, 'link', side_effect=linked):
            done = self.spawn(lambda: other.set_current(Q, D, ledger_fd=self.fd, expected=None, bind='first'))
            self.service.set_current(P, C, ledger_fd=self.fd, expected=C, bind='first')
            self.assertTrue(done.wait(30))
        self.rows(0); self.rows(0, project=Q, chain=D)
        renderer = self.renderer(); self.open(renderer, serving=True); self.open(renderer, project=Q, serving=True)
        self.assertEqual([p.name for p in (self.root / 'keys').iterdir()], ['1'])
        self.assertEqual(self.service.read_record(P, C, 0).key_id, self.service.read_record(Q, D, 0).key_id)

    def test_entry_sealed_when_key_usable(self):
        self.rows(0); renderer = self.renderer(); self.open(renderer, serving=True)
        reserve = lambda tx: self.service.reserve(P, C, kind='approve', subject='s', payload=b'{}',
                                transaction=tx, declared_kinds=('approve',))
        self.assertEqual(reserve(1), 1)
        self.assertTrue(self.service.reservations_above(P, C, 0)[0].sealed)
        record = self.root / 'projects' / P / 'records' / C / '0'
        record.write_bytes(b'fixture edited record')
        self.assertEqual(reserve(2), 2)
        self.assertFalse(self.service.reservations_above(P, C, 1)[0].sealed)
        self.rows(2); renderer.drain(P, C, through=0)
        self.assertFalse((record.parent / '1').exists())

    def test_reader_owner_threads(self):
        from relay_engine import store_layout
        store = self.real_store(); self.rows(1, ledger=store)
        catalogue_calls = []
        def worker():
            catalogue = store_layout.open_catalogue_readonly(store.root)
            try:
                catalogue_calls.append(('read', threading.get_ident()))
                cursor = catalogue.execute('SELECT kind FROM catalogue_identity WHERE id=1')
                try:
                    self.assertEqual(cursor.fetchone(), ('isolated',))
                finally:
                    cursor.close()
            finally:
                catalogue.close(); catalogue_calls.append(('close', threading.get_ident()))
        worker_done = self.spawn(worker); self.assertTrue(worker_done.wait(30))
        self.assertEqual([op for op, _ in catalogue_calls], ['read', 'close'])
        self.assertEqual(catalogue_calls[0][1], catalogue_calls[1][1])
        self.assertNotEqual(catalogue_calls[0][1], threading.get_ident())
        blocked, entered, release = self.hold(self.service.seal_record)
        renderer = self.renderer(reader=store.open_reader, root=store.root)
        lane = self.spawn(lambda: renderer.ledger_opened(P)); self.assertTrue(lane.wait(30))
        self.assertEqual(store.readers[0].trace, [])
        with mock.patch.object(self.service, 'seal_record', side_effect=blocked):
            serving = self.spawn(lambda: renderer.ledger_serving(P)); self.assertTrue(entered.wait(30))
            drained = self.spawn(lambda: renderer.drain(P, C, through=2))
            self.assertFalse(drained.is_set()); release.set()
            self.assertTrue(serving.wait(30)); self.assertTrue(drained.wait(30))
        closing = self.spawn(lambda: renderer.ledger_closing(P)); self.assertTrue(closing.wait(30))
        reader = store.readers[0]
        self.assertTrue(reader.closed)
        self.assertTrue(reader.trace)
        self.assertTrue(all(tid == reader.owner for op, tid in reader.trace if op != 'interrupt'))
        self.assertNotEqual(reader.owner, threading.get_ident())


    def test_interrupt_never_meets_a_closed_reader(self):
        order = []
        stopping, attempted = threading.Event(), threading.Event()
        interrupting, finish_interrupt = threading.Event(), threading.Event()
        blocked, entered, release = self.hold(stand.events_after)
        original_owner = self.module._Owner
        owners = []
        class Condition(threading.Condition):
            def __enter__(condition):
                if stopping.is_set() and owners and threading.get_ident() == owners[0].ident:
                    attempted.set()
                return super().__enter__()
        def owner(*args):
            value = original_owner(*args, condition=Condition())
            owners.append(value)
            return value
        class Reader(stand.Reader):
            def interrupt(reader):
                stopping.set(); release.set(); interrupting.set()
                if not finish_interrupt.wait(30):
                    raise AssertionError('interrupt not released')
                order.append('interrupt-return')
            def close(reader):
                order.append('close')
                super().close()
        wait = stand.ManualWait()
        renderer = self.renderer(reader=lambda root, project: Reader(self.ledger, project),
                                 events=lambda *a: blocked(*a) if stopping_read.is_set() else stand.events_after(*a),
                                 wait=wait)
        stopping_read = threading.Event()
        with mock.patch.object(self.module, '_Owner', side_effect=owner):
            self.open(renderer, serving=True)
        wait.calls.clear(); stopping_read.set()
        closing = self.spawn(lambda: renderer.ledger_closing(P))
        self.addCleanup(finish_interrupt.set)
        self.assertTrue(entered.wait(30)); wait.until_calls(1); wait.expire()
        self.assertTrue(interrupting.wait(30)); self.assertTrue(attempted.wait(30))
        finish_interrupt.set()
        self.assertTrue(closing.wait(30))
        self.assertEqual(order, ['interrupt-return', 'close'])
        self.assertEqual(self.errors, [])
        # A slot already cleared before the half-bound expires is never interrupted.
        owner = original_owner(P, 2, reader=None)
        request = self.module._Request('stop')
        manual = stand.ManualWait(); renderer.wait = manual
        done = self.spawn(lambda: renderer._hook_wait(owner, request))
        manual.until_calls(1); manual.expire(); manual.until_calls(2)
        request.done.set(); self.assertTrue(done.wait(30))
        self.assertEqual(order, ['interrupt-return', 'close'])

    def test_hook_opened_timed_out(self):
        # A bound-returning hook must leave the held open owned by its old thread.
        for reopen in (False, True):
            if True:  # Keep a failed first schedule from starting another schedule.
                entered, release, done = threading.Event(), threading.Event(), threading.Event()
                readers, errors = [], []
                count = [0]
                def reader(root, project):
                    count[0] += 1
                    if count[0] == 1:
                        entered.set()
                        if not release.wait(30):
                            raise AssertionError('open not released')
                    value = self.ledger.open_reader(root, project)
                    readers.append(value); return value
                wait = stand.ManualWait(); renderer = self.renderer(reader=reader, wait=wait)
                def opened():
                    try:
                        renderer.ledger_opened(P)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        done.set()
                thread = threading.Thread(target=opened); thread.start()
                self.addCleanup(thread.join, 30); self.addCleanup(release.set)
                self.assertTrue(entered.wait(30))
                with wait.condition:
                    registered = wait.condition.wait_for(lambda: wait.calls, timeout=0.1)
                if registered:
                    wait.expire()
                returned_at_bound = done.wait(0.1)
                if not returned_at_bound:
                    release.set(); self.assertTrue(done.wait(30))
                self.assertTrue(returned_at_bound, 'ledger_opened held the lane past the bound')
                self.assertEqual(wait.calls, [10])
                self.assertEqual([str(x) for x in errors], ['seal_ledger_open_timed_out'])
                self.assertEqual(renderer.generations(P)[0].state, 'closing_late')
                old = renderer._newest(P)
                with self.assertRaises(RuntimeError):
                    renderer.drain(P, C, through=0)
                before = len(self.reports)
                if reopen:
                    renderer.ledger_opened(P); renderer.ledger_serving(P)
                    renderer.ledger_closing(P)
                    self.assertFalse(release.is_set())
                    self.assertEqual(renderer.generations(P)[1].state, 'closed')
                release.set(); old.thread.join(30)
                self.assertFalse(old.thread.is_alive())
                self.assertEqual(renderer.generations(P)[0].state, 'closed')
                self.assertTrue(all(r.closed for r in readers))
                self.assertEqual(len(self.reports), before)
                # The late reader's owner has done only its open and close.
                late = [x[0] for x in self.ledger.trace if x[1] == old.ident]
                self.assertEqual(late[-2:], ['open', 'close'])
        # Timeout result is cached; acknowledgment wins before the hook takes its lock.
        manual = stand.ManualWait()
        timed_out, proceed = threading.Event(), threading.Event()
        blocked, entered, release = self.hold(self.ledger.open_reader)
        events = []
        def wait(event, seconds):
            events.append(event)
            answer = manual(event, seconds)
            if not answer:
                timed_out.set()
                if not proceed.wait(30):
                    raise AssertionError('hook not released')
            return answer
        renderer = self.renderer(reader=blocked, wait=wait)
        done = self.spawn(lambda: renderer.ledger_opened(P))
        self.addCleanup(proceed.set)
        self.assertTrue(entered.wait(30)); manual.until_calls(1); manual.expire()
        self.assertTrue(timed_out.wait(30))
        release.set(); self.assertTrue(events[0].wait(30)); proceed.set()
        self.assertTrue(done.wait(30))
        self.assertEqual(renderer.generations(P)[-1].state, 'opened')
        self.assertEqual(self.errors, [])

    def test_hook_close_completed(self):
        store = self.real_store(); self.rows(1, ledger=store)
        renderer = self.renderer(reader=store.open_reader, root=store.root)
        self.open(renderer, serving=True); renderer.ledger_closing(P)
        self.assertTrue(store.readers[0].closed)
        self.assertEqual(renderer.generations(P)[-1].state, 'closed')
        self.assertIsNotNone(self.service.read_record(P, C, 1))

    def test_hook_close_timed_out(self):
        wait = stand.ManualWait(); renderer = self.renderer(wait=wait)
        self.rows(1); self.open(renderer, serving=True)
        wait.calls.clear()
        original = self.service.seal_record
        blocked, entered, release = self.hold(original)
        old_owner = renderer._newest(P)
        def slow_old_owner(*args, **kwargs):
            if threading.get_ident() == old_owner.ident:
                return blocked(*args, **kwargs)
            return original(*args, **kwargs)
        self.rows(2)
        with mock.patch.object(self.service, 'seal_record', side_effect=slow_old_owner):
            renderer.signal(P, C); self.assertTrue(entered.wait(30))
            closing = self.spawn(lambda: renderer.ledger_closing(P))
            wait.until_calls(1); self.assertFalse(closing.is_set())
            wait.expire(); wait.until_calls(2)
            self.assertEqual(self.ledger.trace[-1][0], 'interrupt')
            wait.expire(); self.assertTrue(closing.wait(30))
            self.assertEqual(wait.calls, [5, 5])
            self.assertEqual(renderer.generations(P)[0].state, 'closing_late')
            renderer.ledger_opened(P)
            # New generation can serve while the old owner remains blocked.
            new_serving = self.spawn(lambda: renderer.ledger_serving(P))
            self.assertTrue(new_serving.wait(30)); self.assertFalse(release.is_set())
            self.assertEqual(renderer.generations(P)[1].state, 'serving')
            self.assertFalse(any(op == 'close' for op, _, _ in self.ledger.trace))
            release.set()
            old_owner.thread.join(30); self.assertFalse(old_owner.thread.is_alive())
        renderer.drain(P, C, through=0)
        self.assertEqual(renderer.generations(P)[1].state, 'serving')
        self.assertEqual(sum(op == 'close' for op, _, _ in self.ledger.trace), 1)

    def test_hook_close_interrupts_sql(self):
        # Both completion inside the second half and expiration are driven by events.
        for late in (False, True):
            with self.subTest(late=late):
                store = self.real_store(); self.rows(1, ledger=store)
                wait = stand.ManualWait(); armed = threading.Event()
                progress_entered, progress_release = threading.Event(), threading.Event()
                self.addCleanup(progress_release.set)
                def reader(root, project):
                    conn = store.open_reader(root, project)
                    def progress():
                        if armed.is_set():
                            progress_entered.set(); self.assertTrue(progress_release.wait(30))
                        return 0
                    conn.connection.set_progress_handler(progress, 1)
                    return conn
                def events(conn, *args):
                    if armed.is_set():
                        conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 '
                                     'FROM n WHERE x<100000) SELECT sum(x) FROM n').fetchall()
                    return stand.events_after(conn, *args)
                renderer = self.renderer(reader=reader, events=events, wait=wait, root=store.root)
                self.open(renderer, serving=True); wait.calls.clear(); armed.set()
                renderer.signal(P, C); self.assertTrue(progress_entered.wait(30))
                closing = self.spawn(lambda: renderer.ledger_closing(P))
                wait.until_calls(1); wait.expire(); wait.until_calls(2)
                self.assertEqual(store.readers[0].trace[-1][0], 'interrupt')
                if late:
                    wait.expire(); self.assertTrue(closing.wait(30))
                    self.assertEqual(renderer.generations(P)[-1].state, 'closing_late')
                armed.clear(); progress_release.set()
                self.assertTrue(closing.wait(30)); self.assertEqual(wait.calls, [5, 5])
                # A new open cannot consume or close the old generation's reader.
                renderer.ledger_opened(P); renderer.ledger_serving(P)
                renderer.drain(P, C, through=0); renderer.ledger_closing(P)
                self.assertTrue(all(r.closed for r in store.readers))

    def test_committed_never_waits(self):
        self.rows(0); renderer = self.renderer(); self.open(renderer, serving=True)
        self.rows(1); blocked, entered, release = self.hold(self.service.seal_record)
        with mock.patch.object(self.service, 'seal_record', side_effect=blocked):
            self.service.committed(P, C); self.assertTrue(entered.wait(30))
            finished = self.spawn(lambda: [self.service.committed(P, C) for _ in range(100)])
            self.assertTrue(finished.wait(30))
            chains = self.service.chains
            def owned_chains(project):
                self.assertNotEqual(threading.get_ident(), renderer._backstop.ident)
                return chains(project)
            with mock.patch.object(self.service, 'chains', side_effect=owned_chains):
                renderer.start(); self.tick.until_calls(1); self.tick.advance(); self.tick.until_calls(2)
            drained = self.spawn(lambda: renderer.drain(P, C, through=4))
            self.assertFalse(drained.is_set()); release.set(); self.assertTrue(drained.wait(30))
        # The running pass and one queued pass coalesce the repeated signals.
        self.assertLessEqual(sum(op == 'BEGIN' for op, _, _ in self.ledger.trace), 5)

    def test_hook_serving_timed_out(self):
        wait = stand.ManualWait(); renderer = self.renderer(wait=wait)
        self.open(renderer); wait.calls.clear(); self.rows(1)
        blocked, entered, release = self.hold(self.service.seal_record)
        with mock.patch.object(self.service, 'seal_record', side_effect=blocked):
            serving = self.spawn(lambda: renderer.ledger_serving(P))
            self.assertTrue(entered.wait(30)); wait.until_calls(1)
            wait.expire(); wait.until_calls(2)
            self.assertEqual(self.ledger.trace[-1][0], 'interrupt')
            wait.expire(); self.assertTrue(serving.wait(30))
            self.assertEqual(wait.calls, [5, 5])
            self.assertEqual(renderer.generations(P)[-1].state, 'serving_late')
            self.rows(2); renderer.signal(P, C); release.set()
        self.assertEqual(renderer.drain(P, C, through=0), 2)
        self.assertIsNotNone(self.service.read_record(P, C, 2))

    def test_hook_folder_fault_reports_not_raises(self):
        renderer = self.renderer(); self.open(renderer); self.rows(1)
        self.damage(self.path())
        renderer.ledger_serving(P); renderer.ledger_closing(P)
        self.assertIn('high_water_unusable', [r.cause for r in self.reports])
        self.assertEqual(renderer.generations(P)[-1].state, 'closed')
        failed = self.renderer(reader=mock.Mock(side_effect=RuntimeError('reader failed')))
        with self.assertRaisesRegex(RuntimeError, 'reader failed'):
            failed.ledger_opened(P)
        self.assertEqual(failed.generations(P)[-1].state, 'closed')
        readers = []
        def opened(root, project):
            reader = self.ledger.open_reader(root, project); readers.append(reader); return reader
        self.service.set_current(Q, D, ledger_fd=self.fd, expected=None, bind='first')
        self.rows(1, project=Q, chain=D)
        sql_fault = self.renderer(reader=opened, events=mock.Mock(side_effect=sqlite3.OperationalError('bad SQL')))
        self.open(sql_fault, project=Q)
        with self.assertRaisesRegex(sqlite3.OperationalError, 'bad SQL'):
            sql_fault.ledger_serving(Q)
        self.assertTrue(readers[0].closed)
        self.assertEqual(sql_fault.generations(Q)[-1].state, 'closed')

    def test_no_pass_before_serving_or_after_closing(self):
        renderer = self.renderer(); self.rows(1); self.open(renderer)
        renderer.start(); self.tick.until_calls(1)
        renderer.signal(P, C); self.tick.advance(); self.tick.until_calls(2)
        self.assertFalse(any(op == 'BEGIN' for op, _, _ in self.ledger.trace))
        opened_owner = renderer._newest(P).reader.owner
        self.assertEqual(renderer.drain(P, C, through=1), 1)
        self.assertIsNotNone(self.service.read_record(P, C, 1))
        self.assertEqual(sum(op == 'open' for op, _, _ in self.ledger.trace), 1)
        self.assertTrue(all(tid == opened_owner for op, tid, _ in self.ledger.trace if op == 'events_after'))
        renderer.ledger_serving(P); renderer.ledger_closing(P)
        before = list(self.ledger.trace)
        renderer.signal(P, C); self.tick.advance(); self.tick.until_calls(3)
        self.assertEqual(self.ledger.trace, before)

    def test_renderer_raise_first(self):
        self.rows(5); renderer = self.renderer(); self.open(renderer)
        seen = []
        original = self.service.folder.publish_record
        def publish(*args):
            seen.append(self.high()); return original(*args)
        with mock.patch.object(self.service.folder, 'publish_record', side_effect=publish):
            self.assertEqual(renderer.drain(P, C, through=0), 5)
        self.assertEqual(seen, [5] * 6)
        self.service.set_current(Q, D, ledger_fd=self.fd, expected=None, bind='first')
        self.rows(5, project=Q, chain=D); self.open(renderer, project=Q)
        with mock.patch.object(self.service.folder, 'publish_record', side_effect=OSError(errno.ENOSPC, 'fixture')):
            self.assertEqual(renderer.drain(Q, D, through=0), 5)
        self.assertIsNone(self.service.read_record(Q, D, 0))
        # A replacement ledger is never rendered at the start hook.
        other = os.open(self.base / 'replacement', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.service.set_current(P, D, ledger_fd=other, expected=C, bind='first')
            self.rows(1, chain=D); renderer.ledger_serving(P)
            self.assertIsNone(self.service.read_record(P, D, 0)); self.assertEqual(self.high(chain=D), 0)
        finally:
            os.close(other)

    def test_renderer_no_read_transaction_between_passes(self):
        store = self.real_store(); self.rows(1, ledger=store)
        renderer = self.renderer(reader=store.open_reader, root=store.root)
        self.open(renderer, serving=True)
        self.assertFalse(store.readers[0].in_transaction)
        self.assertEqual(store.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), (0, 0, 0))
        def fail(*args):
            raise self.types.SealUnusable('high_water_unusable', C)
        with mock.patch.object(self.service, 'rendered_through', side_effect=fail):
            with self.assertRaises(self.types.SealUnusable):
                renderer.drain(P, C, through=0)
        self.assertFalse(store.readers[0].in_transaction)

    def test_stop_order_leaves_no_side_files(self):
        from relay_engine import store_layout
        store = self.real_store(); self.rows(1, ledger=store)
        renderer = self.renderer(reader=store.open_reader, root=store.root)
        self.open(renderer, serving=True); renderer.ledger_closing(P)
        self.assertTrue(store.readers[0].closed)
        path = store_layout.ledger_path(store.root, P)
        store.writer.close()
        self.assertFalse(Path(str(path) + '-wal').exists())
        self.assertFalse(Path(str(path) + '-shm').exists())

    def test_record_conflict_marks_broken(self):
        self.rows(2); renderer = self.renderer(); self.open(renderer)
        original = self.service.folder.publish_record
        def conflicted(project, chain, number, data):
            if number == 1:
                original(project, chain, number, b'fixture conflict')
            return original(project, chain, number, data)
        with mock.patch.object(self.service.folder, 'publish_record', side_effect=conflicted):
            self.assertEqual(renderer.drain(P, C, through=0), 2)
        directory = self.root / 'projects' / P / 'records' / C
        self.assertTrue(self.service.is_broken(P, C))
        self.assertEqual((directory / '1').read_bytes(), b'fixture conflict')
        self.assertTrue(any(p.name.startswith('.creating-1.') for p in directory.iterdir()))
        self.assertFalse((directory / '2').exists())
        self.rows(3); self.assertEqual(renderer.drain(P, C, through=0), 3)
        self.assertFalse((directory / '3').exists())

    def test_failed_record_stops_the_pass(self):
        self.rows(7); renderer = self.renderer(); self.open(renderer)
        original = self.service.folder.publish_record
        seen = []
        def failed(project, chain, number, data):
            seen.append(number)
            if number == 6:
                raise OSError(errno.ENOSPC, 'fixture')
            return original(project, chain, number, data)
        with mock.patch.object(self.service.folder, 'publish_record', side_effect=failed):
            self.assertEqual(renderer.drain(P, C, through=0), 7)
        self.assertEqual(seen, list(range(7))); self.assertIsNone(self.service.read_record(P, C, 7))
        self.assertEqual(renderer.drain(P, C, through=0), 7)
        self.assertEqual(self.service.read_record(P, C, 7).prev_mac, self.service.read_record(P, C, 6).mac)

    def test_chain_keeps_its_key(self):
        self.rows(5); renderer = self.renderer(); self.open(renderer, serving=True)
        original_id = self.service.read_record(P, C, 0).key_id
        (self.root / 'keys' / '1').chmod(0o400)
        self.service.set_current(Q, D, ledger_fd=self.fd, expected=None, bind='first')
        self.rows(0, project=Q, chain=D); self.open(renderer, project=Q, serving=True)
        self.assertNotEqual(original_id, self.service.read_record(Q, D, 0).key_id)
        self.rows(6); renderer.drain(P, C, through=0)
        self.assertIsNone(self.service.read_record(P, C, 6))
        n = self.service.reserve(P, C, kind='approve', subject='s', payload=b'{}', transaction=1,
                                 declared_kinds=('approve',))
        self.assertFalse(self.service.reservations_above(P, C, 0)[0].sealed)
        self.assertEqual(n, 1)
        self.service.set_current(P, D, ledger_fd=self.fd, expected=C, bind='first')
        self.rows(0, chain=D); renderer.drain(P, D, through=0)
        self.assertEqual(self.service.read_record(P, D, 0).key_id, self.service.read_record(Q, D, 0).key_id)

    def test_raise_follows_commit_within_a_second(self):
        renderer = self.renderer(); self.rows(0); self.open(renderer, serving=True)
        renderer.start(); self.tick.until_calls(1)
        self.rows(1); self.service.committed(P, C)
        renderer.drain(P, C, through=0); self.assertEqual(self.high(), 1)
        self.rows(2)  # Dropped signal: only the next backstop can raise this row.
        self.assertEqual(self.high(), 1); self.clock.advance(1000); self.tick.advance()
        self.tick.until_calls(2); renderer.drain(P, C, through=0)
        self.assertEqual(self.high(), 2); self.assertEqual(self.tick.calls, [1.0, 1.0])
        self.assertEqual(self.clock(), 1000)

    def test_key_seq_race_two_processes(self):
        for rename_control in (False, True):
            with self.subTest(rename_control=rename_control):
                root = self.base / ('process-seal-control' if rename_control else 'process-seal')
                context = multiprocessing.get_context('spawn'); children = []; pipes = []
                for project, chain in ((P, C), (Q, D)):
                    parent, child = context.Pipe()
                    process = context.Process(target=key_child,
                                args=(str(root), project, chain, child, rename_control))
                    process.start(); child.close(); children.append(process); pipes.append(parent)
                try:
                    for pipe in pipes:
                        self.assertTrue(pipe.poll(30)); self.assertEqual(pipe.recv(), 'before-link')
                    for pipe in pipes:
                        pipe.send('go')
                    for pipe in pipes:
                        self.assertTrue(pipe.poll(30)); self.assertEqual(pipe.recv(), 'key-created')
                    for pipe in pipes:
                        pipe.send('render')
                    results = []
                    for pipe in pipes:
                        self.assertTrue(pipe.poll(30)); results.append(pipe.recv())
                    self.assertEqual(results[0]['key_id'], results[1]['key_id'])
                    self.assertEqual(sum(row['created'] for row in results), 2 if rename_control else 1)
                    self.assertEqual([p.name for p in (root / 'keys').iterdir()], ['1'])
                finally:
                    for pipe in pipes:
                        pipe.close()
                    exits = []
                    for process in children:
                        process.join(60)
                        if process.is_alive():
                            process.kill(); process.join(); self.fail('owned fixture child exceeded hang ceiling')
                        exits.append(process.exitcode)
                    self.assertEqual(exits, [0, 0])

    def test_drain_without_pass_thread_raises(self):
        renderer = self.renderer()
        with self.assertRaises(RuntimeError):
            renderer.drain(P, C, through=0)
        self.assertEqual(self.ledger.trace, [])
        self.open(renderer); renderer.ledger_closing(P)
        before = list(self.ledger.trace)
        with self.assertRaises(RuntimeError):
            renderer.drain(P, C, through=0)
        self.assertEqual(self.ledger.trace, before)

    def test_standin_signatures_match_pins(self):
        self.assertEqual(str(inspect.signature(stand.events_after)), '(conn, project_id, chain_id, after_seq)')
        self.assertEqual(str(inspect.signature(stand.Ledger.open_reader)), '(self, root, project_id)')
        self.assertEqual(str(inspect.signature(stand.event_row)),
                         "(project_id, chain_id, number, kind='approve', payload=None, at_ms=40)")
        self.assertEqual(str(inspect.signature(stand.ManualWait.__call__)), '(self, event, seconds)')
        self.assertEqual(str(inspect.signature(stand.ManualTick.__call__)), '(self, seconds)')
        row = stand.event_row(P, C, 0); self.assertIs(type(row), self.types.EventRow)
        ledger = stand.Ledger(); ledger.put(row)
        reader = ledger.open_reader(self.base, P); reader.execute('BEGIN')
        self.assertEqual(stand.events_after(reader, P, C, -1), [row]); reader.close()
