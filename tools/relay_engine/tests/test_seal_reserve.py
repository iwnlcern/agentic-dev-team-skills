"""Records and reservation crash boundaries on explicit temporary roots."""
from contextlib import ExitStack
import errno
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

P = '10000000-0000-4000-8000-000000000001'
Q = '10000000-0000-4000-8000-000000000002'
C = '20000000-0000-4000-8000-000000000002'
D = '30000000-0000-4000-8000-000000000003'
R = '40000000-0000-4000-8000-000000000004'


class Stopped(BaseException):
    pass


class SealReserveTests(unittest.TestCase):
    def setUp(self):
        self.a = importlib.import_module('relay_engine.approval_seal')
        self.s = importlib.import_module('relay_engine.seal_slots')
        self.f = importlib.import_module('relay_engine.seal_files')
        self.t = importlib.import_module('relay_engine.seal_types')
        from relay_engine.jcs import jcs_encode
        self.jcs = jcs_encode
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'seal'
        self.ledger = os.open(self.base / 'ledger', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.ledger)
        self.reports, self.diags = [], []
        self.service = self.a.SealService(self.root, runtime_instance_id=R,
                                        clock_ms=lambda: 50, report=self.reports.append)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(self.a.install(self.service))
        self.a.set_current(P, C, ledger_fd=self.ledger, expected=None, bind='first')
        self.reports.clear()
        from relay_engine import strings
        strings.set_diagnostic_sink(self.diags.append)
        self.addCleanup(strings.set_diagnostic_sink, None)

    @property
    def project(self):
        return self.root / 'projects' / P

    @property
    def counter(self):
        return self.project / (C + '.counter')

    def entry(self, n):
        return self.project / (C + '.entry.' + str(n))

    def reserve(self, *, kind='approve', tx=1, declared=('approve',), payload=b'{"x":1}', chain=C):
        return self.a.reserve(P, chain, kind=kind, subject='subject', payload=payload,
                              transaction=tx, declared_kinds=declared)

    def row(self, n=0, *, project=P, chain=C, kind='chain-start', payload=None):
        return self.t.EventRow(project, chain, n, kind, {} if payload is None else payload, 40)

    def record(self, n=0, *, project=P, chain=C, kind='chain-start', payload=None):
        keys = self.service.folder.usable_keys()
        return self.a.seal_record(self.row(n, project=project, chain=chain, kind=kind, payload=payload),
                                  None if n == 0 else 'a' * 64,
                                  None if n == 0 else keys[-1].key_id)

    def publish(self, n=0, *, project=P, chain=C):
        fd = self.service.folder.project_dir(project); os.close(fd)
        data = self.record(n, project=project, chain=chain)
        self.assertTrue(self.service.folder.publish_record(project, chain, n, data))
        return data

    def counter_state(self):
        fd = os.open(self.counter, os.O_RDONLY)
        try:
            return self.s.read_slots(fd, kind=self.s.ItemKind.COUNTER,
                                     binding=(P, C), ledger_identity=None)
        finally:
            os.close(fd)

    def fault(self, call, cause, chain=C):
        with self.assertRaises(self.t.SealUnusable) as caught:
            call()
        self.assertEqual((caught.exception.cause, caught.exception.chain_id), (cause, chain))

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.stat().st_ino, p.read_bytes())
                for p in self.root.rglob('*') if p.is_file()}

    def test_record_bytes_are_a_function_of_row(self):
        for n, kind, payload in [(0, 'chain-start', {'predecessor': None}), (1, 'void', {})]:
            row = self.row(n, kind=kind, payload=payload)
            key_id = None if n == 0 else self.service.folder.usable_keys()[0].key_id
            prev = None if n == 0 else 'b' * 64
            first = self.a.seal_record(row, prev, key_id)
            self.assertEqual(first, self.a.seal_record(row, prev, key_id))
            self.assertEqual(self.jcs(json.loads(first)), first)
            opened = self.a.open_record(first)
            self.assertEqual(opened.event, {'kind': kind, 'payload': payload, 'at_ms': 40})
            self.assertEqual((opened.number, opened.prev_mac), (n, prev))

    def test_open_record_checks(self):
        good = self.record()
        for cause in ('format', 'binding', 'key_unknown', 'mac', 'chain_broken'):
            value = json.loads(good)
            if cause == 'format':
                value['record']['format'] = 'wrong'
            elif cause == 'binding':
                value['record']['project_id'] = 'not-a-uuid'
            elif cause == 'key_unknown':
                value['record']['key_id'] = '0' * 32
            elif cause == 'mac':
                value['mac'] = '0' * 64
            else:
                self.service.folder.write_mark(P, C, b'bad-checksum-is-still-broken')
            with self.assertRaises(self.t.RecordInvalid) as caught:
                self.a.open_record(self.jcs(value))
            self.assertEqual(caught.exception.cause, cause)

    def test_read_record_by_name(self):
        good = self.publish(5)
        (self.project / 'lock').unlink()  # Fixture: prove the reader creates none.
        self.reports.clear()
        def read(number, *, project=P, chain=C):
            before = sorted(x.name for x in self.project.iterdir())
            try:
                return self.a.read_record(project, chain, number)
            finally:
                self.assertEqual(sorted(x.name for x in self.project.iterdir()), before)
                self.assertFalse((self.project / 'lock').exists())
                self.assertEqual(self.reports, [])
        self.assertEqual(read(5), self.a.open_record(good))
        self.assertIsNone(read(6))
        self.assertIsNone(read(0, chain=D))
        self.assertIsNone(read(0, project=Q))
        directory = self.project / 'records' / C
        target = self.base / 'outside'; target.write_bytes(self.record(7))
        (directory / '7').symlink_to(target)
        (directory / '8').mkdir()
        os.mkfifo(directory / '9', 0o600)
        real_open = os.open
        flags_seen = []
        def opened(path, flags, *args, **kw):
            if Path(path) == directory / '9':
                flags_seen.append(flags)
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(path, flags, *args, **kw)
        with mock.patch.object(os, 'open', side_effect=opened):
            for n in (7, 8, 9):
                with self.assertRaises(self.t.RecordUnreadable) as caught:
                    read(n)
                self.assertEqual((caught.exception.number, caught.exception.cause), (n, 'not_regular'))
        self.assertEqual(len(flags_seen), 1)
        with mock.patch.object(os, 'read', side_effect=OSError(errno.EIO, 'injected')):
            with self.assertRaises(self.t.RecordUnreadable) as caught:
                read(5)
            self.assertEqual(caught.exception.cause, 'io')
        (directory / '10').write_bytes(b'not JCS')
        with self.assertRaises(self.t.RecordInvalid) as caught:
            read(10)
        self.assertEqual(caught.exception.cause, 'format')
        for hardlink in (True, False):
            if hardlink:
                os.link(directory / '5', directory / '6')
            else:
                (directory / '6').write_bytes(good)
            with self.assertRaises(self.t.RecordUnreadable) as caught:
                read(6)
            self.assertEqual(caught.exception.cause, 'name_mismatch')
            (directory / '6').unlink()
        self.publish(5, chain=D)
        directory.rename(self.project / 'records' / 'saved')
        directory.symlink_to(self.project / 'records' / D)
        with self.assertRaises(self.t.RecordUnreadable) as caught:
            read(5)
        self.assertEqual(caught.exception.cause, 'name_mismatch')
        self.publish(5, project=Q)
        (self.project / 'records').rename(self.project / 'saved-records')
        (self.project / 'records').symlink_to(self.root / 'projects' / Q / 'records')
        with self.assertRaises(self.t.RecordUnreadable) as caught:
            read(5)
        self.assertEqual(caught.exception.cause, 'name_mismatch')
        with mock.patch.object(os, 'open', side_effect=AssertionError('opened')):
            for args in [('wrong', C, 0), (P, 'wrong', 0), (P, C, -1), (P, C, True)]:
                with self.assertRaises(ValueError):
                    self.a.read_record(*args)

    def test_key_usable_checks(self):
        key = self.root / 'keys' / '1'; original = key.read_bytes()
        good = self.record()
        for fault in ('symlink', 'mode', 'length', '01', '1a'):
            if fault == 'symlink':
                key.unlink(); target = self.base / 'test-key'; target.write_bytes(original)
                key.symlink_to(target)
            elif fault == 'mode':
                key.chmod(0o640)
            elif fault == 'length':
                key.write_bytes(original[:31])
            else:
                key.rename(key.with_name(fault))
            self.assertIsNone(self.a.seal_record(self.row(), None, None))
            with self.assertRaises(self.t.RecordInvalid) as caught:
                self.a.open_record(good)
            self.assertEqual(caught.exception.cause, 'key_unknown')
            if fault in ('01', '1a'):
                key.with_name(fault).rename(key)
            elif fault == 'symlink':
                key.unlink(); key.write_bytes(original)
            else:
                key.write_bytes(original)
            key.chmod(0o600)

    def test_reserve_entry_before_head(self):
        real = os.pwrite
        def stopped(fd, data, offset):
            if data[:8] == b'ADTSLOT1':
                self.assertTrue(self.entry(1).is_file())
                self.assertEqual(self.counter_state().value, 0)
                self.assertEqual(json.loads(self.entry(1).read_bytes())['entry']['number'], 1)
                raise Stopped()
            return real(fd, data, offset)
        with mock.patch.object(os, 'pwrite', side_effect=stopped):
            with self.assertRaises(Stopped):
                self.reserve()
        inode = self.entry(1).stat().st_ino
        self.assertEqual(self.reserve(payload=b'{"x":2}'), 1)
        self.assertEqual(self.entry(1).stat().st_ino, inode)
        self.assertEqual(self.a.reservations_above(P, C, 0)[0].payload, {'x': 2})

    def test_reserve_declaration_rules(self):
        for reason, kwargs in [('declaration_missing', {'declared': None}),
                               ('declaration_missing', {'declared': ()}),
                               ('kind_mismatch', {'kind': 'void'})]:
            before = self.snapshot()
            with self.assertRaises(self.t.ReserveRefused) as caught:
                self.reserve(**kwargs)
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.reserve(declared=('approve', 'void')), 1)
        for reason, kwargs in [('declaration_unexpected', {'kind': 'void', 'declared': ('void',)}),
                               ('kind_mismatch', {'kind': 'approve', 'declared': None})]:
            with self.assertRaises(self.t.ReserveRefused) as caught:
                self.reserve(**kwargs)
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.reserve(kind='void', declared=None), 2)
        with self.assertRaises(self.t.ReserveRefused) as caught:
            self.reserve(declared=None)
        self.assertEqual(caught.exception.reason, 'past_declaration')
        entries = self.a.reservations_above(P, C, 0)
        self.assertEqual([e.position for e in entries], [0, 1])
        self.assertEqual([e.declared_kinds for e in entries], [('approve', 'void'), None])

    def test_reserve_refuses_broken_chain(self):
        self.service.folder.write_mark(P, C, b'broken-even-with-bad-checksum')
        before = self.snapshot()
        with self.assertRaises(self.t.ReserveRefused) as caught:
            self.reserve()
        self.assertEqual(caught.exception.reason, 'chain_broken')
        self.assertEqual(self.snapshot(), before)

    def test_reservations_above_unreadable_kinds(self):
        for tx in range(1, 7):
            self.reserve(tx=tx)
        v = json.loads(self.entry(2).read_bytes()); v['sha256'] = '0' * 64
        self.entry(2).write_bytes(self.jcs(v))
        self.entry(3).write_bytes(b'not-json')
        v = json.loads(self.entry(4).read_bytes()); v['entry']['chain_id'] = D
        v['sha256'] = hashlib.sha256(self.jcs(v['entry'])).hexdigest()
        self.entry(4).write_bytes(self.jcs(v)); self.entry(5).unlink()
        self.entry(7).write_bytes(self.entry(6).read_bytes())
        entries = self.a.reservations_above(P, C, 0)
        self.assertEqual([e.number for e in entries], list(range(1, 7)))
        self.assertEqual([(e.number, e.cause) for e in entries if isinstance(e, self.t.UnreadableEntry)],
                         [(2, 'checksum'), (3, 'parse'), (4, 'binding'), (5, 'missing')])
        self.assertEqual([e.number for e in self.a.reservations_above(P, C, 5)], [6])

    def test_declaration_position_after_head_sync(self):
        real = self.s._sync
        def fail(fd, **kw):
            if os.fstat(fd).st_ino == self.counter.stat().st_ino:
                raise OSError(errno.ENOSPC, 'injected')
            return real(fd, **kw)
        with mock.patch.object(self.s, '_sync', side_effect=fail):
            self.fault(lambda: self.reserve(declared=('approve', 'void')), 'counter_unusable')
        # The pwrite landed but the sync failed. The number was never returned;
        # the retry keeps declaration position zero even if its head is now one.
        n = self.reserve(declared=('approve', 'void'))
        entry = self.a.reservations_above(P, C, n - 1)[0]
        self.assertEqual((entry.position, entry.declared_kinds), (0, ('approve', 'void')))
        self.reserve(kind='void', declared=None)
        self.reserve(tx=2, declared=('approve',))
        with self.assertRaises(self.t.ReserveRefused) as caught:
            self.reserve(tx=1, declared=None)
        self.assertEqual(caught.exception.reason, 'declaration_missing')

    def test_counter_inode_fixed_across_updates(self):
        inode = self.counter.stat().st_ino
        for tx in range(1, 101):
            self.assertEqual(self.reserve(tx=tx), tx)
            self.assertEqual(self.counter.stat().st_ino, inode)

    def test_counter_restored_by_rename_refuses(self):
        for tx in range(1, 11):
            self.reserve(tx=tx)
        old = self.counter.read_bytes()
        # Copying old bytes over the existing inode is the design's stated
        # limit: the stored identity still matches, so its older head is read.
        for tx in range(11, 21):
            self.reserve(tx=tx)
        inode = self.counter.stat().st_ino
        self.counter.write_bytes(old)
        self.assertEqual(self.counter.stat().st_ino, inode)
        self.assertEqual(self.a.counter_head(P, C), 10)
        self.assertEqual(self.reserve(tx=21), 11)
        replacement = self.project / 'restored'; replacement.write_bytes(old)
        replacement.rename(self.counter)
        before = self.snapshot()
        self.fault(lambda: self.reserve(tx=22), 'counter_unusable')
        self.assertEqual(self.snapshot(), before)

    def test_torn_head_write_reuses_number(self):
        real = os.pwrite
        def torn(fd, data, offset):
            if data[:8] == b'ADTSLOT1':
                real(fd, data[:64], offset)
                raise Stopped()
            return real(fd, data, offset)
        with mock.patch.object(os, 'pwrite', side_effect=torn):
            with self.assertRaises(Stopped):
                self.reserve()
        self.diags.clear()
        self.assertEqual(self.reserve(payload=b'{"x":2}'), 1)
        self.assertEqual([(d.kind, d.slot) for d in self.diags], [('counter', 1)])
        self.assertEqual([e.number for e in self.a.reservations_above(P, C, 0)], [1])
        self.assertEqual(self.a.reservations_above(P, C, 1), ())

    def test_damaged_slot_after_commit_refuses(self):
        self.assertEqual(self.reserve(), 1)  # The caller's commit is outside this seal-side test.
        raw = bytearray(self.counter.read_bytes()); raw[65536] ^= 1
        self.counter.write_bytes(raw); self.diags.clear()
        self.assertEqual(self.a.counter_head(P, C), 0)
        self.assertEqual([(d.kind, d.slot) for d in self.diags], [('counter', 1)])
        # Authority owns refusal from its ledger number 1 exceeding this head.
        self.assertTrue(1 > self.a.counter_head(P, C))

    def test_creation_stop_before_link(self):
        real = os.link
        def stopped(src, dst, **kw):
            if str(dst) == D + '.counter':
                raise Stopped()
            return real(src, dst, **kw)
        with mock.patch.object(os, 'link', side_effect=stopped):
            with self.assertRaises(Stopped):
                self.a.set_current(P, D, ledger_fd=self.ledger, expected=C, bind='first')
        leftovers = list(self.project.glob('.creating-' + D + '.counter.*'))
        self.assertEqual(len(leftovers), 1)
        self.fault(lambda: self.reserve(chain=D), 'ledger_chain_mismatch', C)
        self.a.set_current(P, D, ledger_fd=self.ledger, expected=C, bind='first')
        self.assertFalse(leftovers[0].exists())
        self.assertEqual(self.reserve(chain=D), 1)

    def test_rewrite_and_reserve_together(self):
        other = os.open(self.base / 'other', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        barrier = threading.Barrier(3); errors = []
        def run(call):
            try:
                barrier.wait(timeout=30); call()
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(lambda: self.reserve(),)),
                   threading.Thread(target=run, args=(lambda: self.a.set_current(
                       P, C, ledger_fd=other, expected=C, bind='rebind'),))]
        for t in threads:t.start()
        barrier.wait(timeout=30)
        for t in threads:t.join(30); self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.a.counter_head(P, C), 1)
        self.assertTrue(self.a.ledger_matches(P, C, other))
        self.assertEqual(self.diags, [])

    def test_restore_records_staged_copy(self):
        for correct in (True, False):
            chain = D if correct else Q
            staged = self.base / ('staged-' + chain)
            fd = os.open(staged, os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, fd)
            expected = self.a.current_chain(P)
            self.a.set_current(P, chain, ledger_fd=fd if correct else self.ledger,
                               expected=expected, bind='first')
            staged.replace(self.base / 'ledger')
            served = os.open(self.base / 'ledger', os.O_RDONLY)
            try:
                self.assertEqual(self.a.ledger_matches(P, chain, served), correct)
                if correct:self.assertEqual(self.reserve(chain=chain), 1)
            finally:
                os.close(served)

    def test_restore_reversal_keeps_identity(self):
        before = self.counter.read_bytes()
        staged = os.open(self.base / 'staged', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, staged)
        self.a.set_current(P, D, ledger_fd=staged, expected=C, bind='first')
        self.a.return_current(P, C, expected=D)
        self.assertEqual(self.counter.read_bytes(), before)
        self.assertTrue(self.a.ledger_matches(P, C, self.ledger))
        self.assertEqual(self.reserve(), 1)

    def test_folder_state_present_partial(self):
        self.counter.unlink(); (self.project / (C + '.high-water')).unlink()
        self.assertEqual(self.a.folder_state(P), 'present')
        self.fault(lambda: self.reserve(), 'counter_unusable')

    def test_entry_seal_follows_chain_key(self):
        self.assertEqual(self.reserve(), 1)
        value = json.loads(self.entry(1).read_bytes()); key = self.service.folder.usable_keys()[0]
        body = self.jcs(value['entry'])
        self.assertEqual(value['seal']['mac'], self.f.mac(key, b'adt-approval-entry-v1\0', body))
        self.assertNotEqual(value['seal']['mac'], self.f.mac(key, b'adt-approval-record-v1\0', body))
        self.assertTrue(self.a.reservations_above(P, C, 0)[0].sealed)
        value['seal']['mac'] = '0' * 64
        value['sha256'] = hashlib.sha256(self.jcs(value['entry'])).hexdigest()
        self.entry(1).write_bytes(self.jcs(value))
        self.assertEqual(self.a.reservations_above(P, C, 0)[0], self.t.UnreadableEntry(1, 'seal'))
        self.publish(0)
        path = self.project / 'records' / C / '0'
        path.write_bytes(b'edited')
        self.assertEqual(self.reserve(tx=2), 2)
        self.assertIsNone(json.loads(self.entry(2).read_bytes())['seal'])
        keypath = self.root / 'keys' / '1'; keypath.chmod(0o640)
        self.assertEqual(self.reserve(tx=3), 3)
        self.assertFalse(self.a.reservations_above(P, C, 2)[0].sealed)
        self.assertIsNone(json.loads(self.entry(3).read_bytes())['seal'])

    def test_both_slots_invalid_refuses_and_reports(self):
        self.counter.write_bytes(bytes(65792))
        before = self.counter.read_bytes()
        for call in (lambda: self.reserve(), lambda: self.a.rendered_through(P, C)):
            self.reports.clear(); self.fault(call, 'counter_unusable')
            self.assertEqual(self.reports, [self.t.SealReport(P, C, 'counter_unusable', 50)])
        self.assertEqual(self.counter.read_bytes(), before)
        self.assertFalse(self.entry(1).exists())

    def test_reserve_enospc_never_advances_head(self):
        for target in ('entry', 'head'):
            real = os.pwrite; failed = []
            def write(fd, data, offset):
                matched = data.startswith(b'{"entry":') if target == 'entry' else data[:8] == b'ADTSLOT1'
                if matched and not failed:
                    failed.append(True); raise OSError(errno.ENOSPC, 'injected')
                return real(fd, data, offset)
            before = self.a.counter_head(P, C); tx = before + 1
            with mock.patch.object(os, 'pwrite', side_effect=write):
                self.fault(lambda: self.reserve(tx=tx), 'counter_unusable')
            self.assertEqual(failed, [True])
            self.assertEqual(self.a.counter_head(P, C), before)
            self.assertEqual(self.reserve(tx=tx), before + 1)
            self.assertEqual(self.a.reservations_above(P, C, before)[0].position, 0)

    def test_reserve_refuses_float_payload(self):
        with mock.patch.object(self.service.folder, 'lock', side_effect=AssertionError('locked')):
            with self.assertRaises(ValueError):
                self.reserve(payload=b'{"n":1.5}')

    def test_mark_broken_first_wins(self):
        self.a.mark_broken(P, C, cause='counter_behind', number=6)
        path = self.project / (C + '.broken')
        original, inode = path.read_bytes(), path.stat().st_ino
        value = json.loads(original); mark = value['mark']
        self.assertEqual(mark, {'format': 'adt-approval-broken-v1', 'project_id': P,
                                'chain_id': C, 'cause': 'counter_behind', 'number': 6, 'at_ms': 50})
        self.assertEqual(value['sha256'], hashlib.sha256(self.jcs(mark)).hexdigest())
        self.a.mark_broken(P, C, cause='record_conflict', number=7)
        self.assertEqual((path.read_bytes(), path.stat().st_ino), (original, inode))
        self.assertTrue(self.a.is_broken(P, C))
        path.write_bytes(b'bad-checksum')
        self.assertTrue(self.a.is_broken(P, C))
        self.a.mark_broken(P, C, cause='record_conflict', number=8)
        self.assertEqual(path.read_bytes(), b'bad-checksum')

    def test_drain_through_type_checks(self):
        renderer = mock.Mock(); renderer.drain.return_value = 7
        self.service.attach_renderer(renderer)
        before = self.snapshot()
        with mock.patch.object(self.service.folder, 'lock', side_effect=AssertionError('locked')):
            for through in (-1, True, 6.0):
                with self.assertRaises(ValueError):
                    self.a.drain(P, C, through=through)
            renderer.drain.assert_not_called()
            self.assertEqual(self.a.drain(P, C, through=6), 7)
        renderer.drain.assert_called_once_with(P, C, through=6)
        self.assertEqual(self.snapshot(), before)

    def test_per_chain_names_never_unlinked(self):
        seen = {}
        def retained():
            current = {p.relative_to(self.project): p.stat().st_ino
                       for p in self.project.rglob('*') if not p.name.startswith('.creating-')}
            self.assertTrue(seen.keys() <= current.keys())
            self.assertTrue(all(current[p] == inode for p, inode in seen.items()))
            seen.update(current)
        retained(); self.reserve(); retained()
        real = os.pwrite
        def stopped(fd, data, offset):
            if data[:8] == b'ADTSLOT1':
                raise Stopped()
            return real(fd, data, offset)
        with mock.patch.object(os, 'pwrite', side_effect=stopped):
            with self.assertRaises(Stopped):
                self.reserve(tx=2)
        retained(); self.reserve(tx=2, payload=b'{"x":2}'); retained()
        self.publish(); retained()
        with self.service.folder.lock(P, exclusive=True):
            with self.service._open(P, C + '.high-water', write=True) as fd:
                state = self.s.read_slots(fd, kind=self.s.ItemKind.HIGH_WATER,
                                          binding=(P, C), ledger_identity=None)
                self.s.write_next(fd, state, value=2)
        retained()
        self.a.mark_broken(P, C, cause='counter_behind', number=2); retained()
        self.a.set_current(P, D, ledger_fd=self.ledger, expected=C, bind='first'); retained()
        self.reserve(tx=3, chain=D); retained()

    def test_committed_never_blocks(self):
        before = self.snapshot()
        with mock.patch.object(self.service.folder, 'lock', side_effect=AssertionError('locked')):
            self.assertIsNone(self.a.committed(P, C))
            renderer = mock.Mock()
            self.service.attach_renderer(renderer)
            self.assertIsNone(self.a.committed(P, C))
        renderer.committed.assert_called_once_with(P, C)
        self.assertEqual(self.snapshot(), before)

    def test_drain_without_renderer_raises(self):
        before = self.snapshot()
        with mock.patch.object(self.service.folder, 'lock', side_effect=AssertionError('locked')):
            with self.assertRaises(RuntimeError):
                self.a.drain(P, C, through=7)
        self.assertEqual(self.snapshot(), before)
