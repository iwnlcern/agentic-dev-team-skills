"""Slot-format witnesses, slots r3 with its reading-order erratum 2."""
import hashlib
import importlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

P = '10000000-0000-4000-8000-000000000001'
C = '20000000-0000-4000-8000-000000000002'


class SealSlotsTests(unittest.TestCase):
    def setUp(self):
        self.s = importlib.import_module('relay_engine.seal_slots')
        self.t = importlib.import_module('relay_engine.seal_types')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dirfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, self.dirfd)
        self.ledger = os.open(self.root / 'ledger', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.ledger)
        self.binding = (P, C)
        self.fd = self.s.create(self.dirfd, 'counter', kind=self.s.ItemKind.COUNTER,
                               binding=self.binding, value=0, identity_from=self.ledger)
        self.addCleanup(os.close, self.fd)
        self.initial_slot = os.pread(self.fd, 256, 0)

    def read(self, fd=None, **kw):
        return self.s.read_slots(self.fd if fd is None else fd,
                                kind=self.s.ItemKind.COUNTER, binding=self.binding,
                                ledger_identity=self.s.file_identity(self.ledger), **kw)

    def slot(self, sequence, value, *, inode=None):
        data = bytearray(self.initial_slot)
        struct.pack_into('<QQ', data, 16, sequence, value)
        if inode is not None:
            struct.pack_into('<Q', data, 64, inode)
        data[224:] = hashlib.sha256(data[:224]).digest()
        return bytes(data)

    def pair(self, left, right):
        os.pwrite(self.fd, left, 0)
        os.pwrite(self.fd, right, 65536)

    def refused(self, step):
        with self.assertRaises(self.s.CannotRecord) as caught:
            self.read()
        self.assertEqual(caught.exception.step, step)

    def test_slot_layout(self):
        data = os.pread(self.fd, 65793, 0)
        self.assertEqual(len(data), 65792)
        self.assertEqual(data[:256], data[65536:])
        self.assertEqual(data[256:65536], bytes(65280))
        self.assertEqual(data[:8], b'ADTSLOT1')
        self.assertEqual(struct.unpack_from('<H', data, 8)[0], 1)
        self.assertEqual(data[10], 1)
        self.assertEqual(data[12:16], bytes(4))
        self.assertEqual(data[32:64], hashlib.sha256(
            ('["' + P + '","' + C + '"]').encode()).digest())
        self.assertEqual(struct.unpack_from('<Q', data, 64)[0], os.fstat(self.fd).st_ino)
        self.assertEqual(struct.unpack_from('<Q', data, 88)[0], os.fstat(self.ledger).st_ino)
        self.assertEqual(data[112:224], bytes(112))
        self.assertEqual(data[224:256], hashlib.sha256(data[:224]).digest())
        self.assertEqual(self.read().value, 0)
        # Pointer fields differ only where r7 section 2.2 says so.
        identity = self.s.file_identity(self.fd)
        pointer = self.s.pair_bytes(self.s.ItemKind.POINTER, (P,), identity, 0, C.encode())
        self.assertEqual(pointer[10], 3)
        self.assertTrue(pointer[11] & 4)
        self.assertEqual(pointer[88:112], bytes(24))
        self.assertEqual(pointer[112:148], C.encode())
        self.assertEqual(pointer[148:224], bytes(76))

    def test_sequence_rules(self):
        for seq in (0, 2):
            with self.subTest(equal=seq):
                self.pair(self.slot(seq, 3), self.slot(seq, 4))
                self.refused(5)
        self.pair(self.slot(2, 3), self.slot(2, 3))
        self.refused(5)  # An identical nonzero pair is not creation.
        self.pair(self.slot(2, 3), self.slot(1, 4))
        self.refused(4)
        self.pair(bytes(256), self.slot(2, 3))
        self.refused(7)  # The sole well-formed slot has the wrong index.
        reports = []
        from relay_engine import strings
        strings.set_diagnostic_sink(reports.append)
        self.addCleanup(strings.set_diagnostic_sink, None)
        self.pair(self.slot(4, 8), self.slot(2, 9))
        self.assertEqual(self.read().value, 8)
        self.assertEqual([(x.project_id, x.chain_id, x.slot) for x in reports], [(P, C, 1)])
        # Creation remains a valid control after the conflicting-zero fixture.
        self.pair(self.slot(0, 0), self.slot(0, 0))
        self.assertEqual(self.read().sequence, 0)

    def test_wrong_length_holds(self):
        for size in (256, 65793):
            with self.subTest(size=size):
                os.ftruncate(self.fd, size)
                self.refused(2)

    def test_ledger_identity_rewrite(self):
        for n in range(1, 8):
            self.s.write_next(self.fd, self.read(), value=n)
        old = self.s.file_identity(self.ledger)
        replacement = os.open(self.root / 'other', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, replacement)
        new = self.s.file_identity(replacement)
        self.s.rewrite_identity(self.fd, self.read(), new)
        state = self.s.read_slots(self.fd, kind=self.s.ItemKind.COUNTER,
                                  binding=self.binding, ledger_identity=new)
        self.assertEqual((state.value, state.ledger_identity), (7, new))
        self.refused(8)
        with self.assertRaises(self.s.CannotRecord) as caught:
            self.s.read_slots(self.fd, kind=self.s.ItemKind.COUNTER,
                              binding=self.binding, ledger_identity=old)
        self.assertEqual(caught.exception.step, 8)
        self.pair(self.slot(8, 7, inode=1), self.slot(9, 7, inode=1))
        before = os.pread(self.fd, 65792, 0)
        with self.assertRaises(self.s.CannotRecord):
            self.s.rewrite_identity(self.fd, state, new)
        self.assertEqual(os.pread(self.fd, 65792, 0), before)

    def test_link_identity_on_target_fs(self):
        # The published name and the still-open creation descriptor name one inode.
        self.assertEqual(os.stat(self.root / 'counter').st_ino, os.fstat(self.fd).st_ino)
        for offset in (0, 65536):
            data = os.pread(self.fd, 256, offset)
            self.assertEqual(struct.unpack_from('<Q', data, 64)[0], os.fstat(self.fd).st_ino)
        self.assertEqual(os.fstat(self.fd).st_nlink, 1)
        self.assertFalse(list(self.root.glob('.creating-*')))

    def test_sync_call_per_platform(self):
        events = []
        real_pwrite = os.pwrite
        def write(fd, data, offset):
            events.append(('write', offset))
            return real_pwrite(fd, data, offset)
        with mock.patch.object(self.s.os, 'pwrite', side_effect=write), \
                mock.patch.object(self.s.os, 'fsync', side_effect=lambda fd: events.append(('fsync', fd))), \
                mock.patch.object(self.s.fcntl, 'fcntl', side_effect=lambda *a: events.append(('full', a[0]))):
            if sys.platform == 'darwin':
                self.s.write_next(self.fd, self.read(), value=1)
                self.assertEqual([x[0] for x in events], ['write', 'full'])
                events.clear()
                fd = self.s.create(self.dirfd, 'new', kind=self.s.ItemKind.HIGH_WATER,
                                   binding=self.binding, value=0, identity_from=None)
                os.close(fd)
                self.assertEqual([x[0] for x in events], ['write', 'write', 'full', 'fsync', 'full'])
            else:
                with mock.patch.object(self.s.os, 'fdatasync', side_effect=lambda fd: events.append(('data', fd))):
                    self.s.write_next(self.fd, self.read(), value=1)
                    self.assertEqual([x[0] for x in events], ['write', 'data'])
                events.clear()
                fd = self.s.create(self.dirfd, 'new', kind=self.s.ItemKind.HIGH_WATER,
                                   binding=self.binding, value=0, identity_from=None)
                os.close(fd)
                self.assertEqual([x[0] for x in events], ['write', 'write', 'fsync', 'fsync'])

    def test_cannot_record_names_its_step(self):
        for step in (2, 4, 5, 7, 8):
            with self.subTest(step=step):
                os.ftruncate(self.fd, 65792)
                if step == 2:
                    os.ftruncate(self.fd, 256)
                elif step == 4:
                    self.pair(self.slot(2, 1), self.slot(1, 2))
                elif step == 5:
                    self.pair(self.slot(2, 1), self.slot(2, 2))
                elif step == 7:
                    self.pair(bytes(256), bytes(256))
                else:
                    self.pair(self.slot(2, 1, inode=1), self.slot(3, 2, inode=1))
                self.refused(step)

    def test_one_kept_slot_reports_damage(self):
        self.s.write_next(self.fd, self.read(), value=1)
        data = bytearray(os.pread(self.fd, 256, 65536))
        data[224] ^= 1
        os.pwrite(self.fd, data, 65536)
        from relay_engine import strings
        reports = []
        strings.set_diagnostic_sink(reports.append)
        self.addCleanup(strings.set_diagnostic_sink, None)
        self.assertEqual(self.read().value, 0)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertIsInstance(report, self.t.SealSlotDamaged)
        self.assertEqual((report.kind, report.project_id, report.chain_id, report.slot), ('counter', P, C, 1))

    def test_diagnostics_carry_no_path(self):
        key_path = self.root / 'keys' / '1'
        secret = b'unique-secret-key-bytes-for-no-leak'
        faults = [self.t.SealUnusable('counter_unusable', C), self.t.ReserveRefused('chain_broken'),
                  self.t.PointerMoved(None, C), self.t.LedgerRebindRefused(),
                  self.t.RecordInvalid('mac'), self.t.RecordUnreadable(2, 'io'),
                  self.t.SealSlotDamaged('counter', P, C, 1),
                  self.t.SealIdentityOneSided(P, C), self.t.SealReportDropped(P, 'counter_unusable'),
                  self.t.SealCalleeFailed('queue', 'RuntimeError', P)]
        for fault in faults:
            self.assertNotIn(str(key_path), str(fault))
            self.assertNotIn('keys/', str(fault))
            self.assertNotIn(secret.decode(), str(fault))
        row = self.t.EventRow(P, C, 0, 'chain-start', {}, 1)
        with self.assertRaises((AttributeError, TypeError)):
            row.number = 3
        self.assertFalse(hasattr(row, '__dict__'))
        self.assertEqual(self.t.require_canonical_id(P), P)
        for invalid in (P.upper().replace('1', 'A', 1), '{' + P + '}', ' ' + P, 3):
            with self.assertRaises(ValueError):
                self.t.require_canonical_id(invalid)
