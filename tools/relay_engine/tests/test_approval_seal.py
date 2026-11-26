"""Service and crash-boundary witnesses, on explicit temporary seal roots."""
from contextlib import ExitStack
import copy
from dataclasses import replace
import errno
import fcntl
import hashlib
import importlib
import os
from pathlib import Path
import pickle
import pwd
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

P = '10000000-0000-4000-8000-000000000001'
C = '20000000-0000-4000-8000-000000000002'
D = '30000000-0000-4000-8000-000000000003'
R = '40000000-0000-4000-8000-000000000004'


class Stopped(BaseException):
    pass


class ApprovalSealTests(unittest.TestCase):
    def setUp(self):
        self.a = importlib.import_module('relay_engine.approval_seal')
        self.s = importlib.import_module('relay_engine.seal_slots')
        self.t = importlib.import_module('relay_engine.seal_types')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'seal'
        self.ledger = os.open(self.base / 'ledger', os.O_CREAT | os.O_RDWR, 0o600)
        self.other = os.open(self.base / 'other', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.ledger)
        self.addCleanup(os.close, self.other)
        self.reports = []
        self.service = self.a.SealService(self.root, runtime_instance_id=R,
                                        clock_ms=lambda: 1234, report=self.reports.append)
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(self.a.install(self.service))
        self.diags = []
        from relay_engine import strings
        strings.set_diagnostic_sink(self.diags.append)
        self.addCleanup(strings.set_diagnostic_sink, None)

    def set_chain(self, chain=C, *, expected=None, fd=None, bind='first'):
        self.a.set_current(P, chain, ledger_fd=self.ledger if fd is None else fd,
                           expected=expected, bind=bind)

    @property
    def project(self):
        return self.root / 'projects' / P

    @property
    def pointer(self):
        return self.project / 'pointer'

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.stat().st_ino, p.read_bytes())
                for p in self.root.rglob('*') if p.is_file()}

    def fault(self, call, cause, chain=None):
        with self.assertRaises(self.t.SealUnusable) as caught:
            call()
        self.assertEqual((caught.exception.cause, caught.exception.chain_id), (cause, chain))

    def edit_slot(self, data, *, seq=None, value=None, chain=None, inode=None):
        b = bytearray(data)
        if seq is not None:
            struct.pack_into('<Q', b, 16, seq)
        if value is not None:
            struct.pack_into('<Q', b, 24, value)
        if chain is not None:
            b[112:148] = chain.encode()
        if inode is not None:
            struct.pack_into('<Q', b, 64, inode)
        b[224:] = hashlib.sha256(b[:224]).digest()
        return bytes(b)

    def damage(self, form):
        raw = self.pointer.read_bytes()
        slot = raw[:256]
        if form == 'checksums':
            raw = bytes(len(raw))
        elif form in (65791, 65793, 256):
            raw = (raw + b'\0')[:form]
        elif form == 'binding':
            b = bytearray(slot); b[32:64] = bytes(32)
            b[224:] = hashlib.sha256(b[:224]).digest()
            raw = bytes(b) + bytes(65280) + bytes(b)
        elif form == 'copy':
            copy = self.project / 'copy'
            copy.write_bytes(raw)
            os.replace(copy, self.pointer)  # External damage, never production's repair.
            return
        elif form == 'equal':
            raw = self.edit_slot(slot, seq=2, chain=C) + bytes(65280) + self.edit_slot(slot, seq=2, chain=D)
        elif form == 'decrease':
            raw = self.edit_slot(slot, seq=2, value=0) + bytes(65280) + self.edit_slot(slot, seq=1, value=1)
        elif form == 'self':
            raw = self.edit_slot(slot, seq=2, inode=1) + bytes(65280) + self.edit_slot(slot, seq=1, chain=D)
        self.pointer.write_bytes(raw)

    def fresh_damaged(self, form):
        # A new root for each independent stop; no production-root lookup.
        self.root = self.base / str(uuid.uuid4())
        self.service.folder.root = self.root
        self.set_chain()
        self.damage(form)

    def test_pointer_layout_and_torn_write(self):
        self.set_chain()
        raw = self.pointer.read_bytes()
        self.assertEqual(len(raw), 65792)
        self.assertEqual(raw[10], 3)
        self.assertTrue(raw[11] & 4)
        self.assertEqual(raw[88:112], bytes(24))
        self.assertEqual(raw[112:148], C.encode())
        self.assertEqual(raw[148:224], bytes(76))
        self.assertEqual(self.a.current_chain(P), C)
        self.a.return_current(P, D, expected=C)
        new = bytearray(self.pointer.read_bytes())
        new[65536] ^= 1  # The sequence-1 write is torn; the prior chain survives.
        self.pointer.write_bytes(new)
        self.assertEqual(self.a.current_chain(P), C)
        self.assertEqual([(x.kind, x.project_id, x.chain_id, x.slot) for x in self.diags],
                         [('pointer', P, None, 1)])
        new[:256] = bytes(256)
        self.pointer.write_bytes(new)
        self.fault(lambda: self.a.current_chain(P), 'pointer_damaged')

    def test_pointer_compare_and_set(self):
        self.set_chain()
        for call in (lambda: self.set_chain(D, expected=D),
                     lambda: self.a.return_current(P, D, expected=D),
                     lambda: self.a.clear_current(P, expected=D)):
            before = self.snapshot()
            with self.assertRaises(self.t.PointerMoved) as caught:
                call()
            self.assertEqual((caught.exception.expected, caught.exception.found), (D, C))
            self.assertEqual(self.snapshot(), before)
        self.reports.clear()
        self.set_chain(D, expected=C)
        self.assertEqual(self.reports, [self.t.SealReport(P, D, None, 1234)])
        self.a.clear_current(P, expected=D)
        self.assertIsNone(self.a.current_chain(P))
        self.assertEqual(self.reports[-1], self.t.SealReport(P, None, None, 1234))

    def test_set_current_first_bind_refuses_other_file(self):
        self.set_chain()
        before = self.snapshot()
        with self.assertRaises(self.t.LedgerRebindRefused):
            self.set_chain(expected=C, fd=self.other)
        self.assertEqual(self.snapshot(), before)
        self.set_chain(expected=C)
        self.assertEqual((self.project / (C + '.counter')).read_bytes(),
                         before['projects/' + P + '/' + C + '.counter'][1])
        self.set_chain(expected=C, fd=self.other, bind='rebind')
        self.assertTrue(self.a.ledger_matches(P, C, self.other))
        self.assertFalse(self.a.ledger_matches(P, C, self.ledger))
        with mock.patch.object(self.s, 'rewrite_identity', side_effect=self.s.CannotRecord(7)):
            self.fault(lambda: self.set_chain(expected=C, bind='rebind'), 'counter_unusable', C)

    def test_return_current_writes_no_identity(self):
        self.set_chain()
        before = (self.project / (C + '.counter')).read_bytes()
        self.set_chain(D, expected=C, fd=self.other)
        self.a.return_current(P, C, expected=D)
        self.assertEqual((self.project / (C + '.counter')).read_bytes(), before)
        self.assertTrue(self.a.ledger_matches(P, C, self.ledger))
        self.a.return_current(P, None, expected=C)
        self.assertIsNone(self.a.current_chain(P))
        with mock.patch.object(os, 'open', side_effect=AssertionError('opened')):
            with self.assertRaises(TypeError):
                self.a.return_current(P, C, expected=self.a.POINTER_UNREADABLE)

    def test_after_unlink_check_only_before_pointer_moves(self):
        self.set_chain()
        self.a.clear_current(P, expected=C)
        before = self.snapshot()
        self.fault(lambda: self.set_chain(fd=self.other), 'counter_unusable', C)
        self.assertEqual(self.snapshot(), before)
        self.a.return_current(P, C, expected=None)
        with self.assertRaises(self.t.LedgerRebindRefused):
            self.set_chain(expected=C, fd=self.other)
        self.set_chain(expected=C, fd=self.other, bind='rebind')
        self.assertTrue(self.a.ledger_matches(P, C, self.other))

    def test_set_current_resumes_each_item(self):
        real_link = os.link
        for target in (C + '.counter', C + '.high-water', '1'):
            self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
            def stop(src, dst, **kw):
                real_link(src, dst, **kw)
                if str(dst) == target:
                    raise Stopped()
            with mock.patch.object(os, 'link', side_effect=stop):
                with self.assertRaises(Stopped):
                    self.set_chain()
            self.set_chain()
            self.assertEqual(self.a.current_chain(P), C)
            self.assertEqual(self.a.counter_head(P, C), 0)
            self.assertEqual(self.a.rendered_through(P, C), 0)
            self.assertTrue(self.service.folder.usable_keys())

    def repair(self, *, clear=False):
        if clear:
            self.a.clear_current(P, expected=self.a.POINTER_UNREADABLE)
        else:
            self.set_chain(D, expected=self.a.POINTER_UNREADABLE, bind='rebind')

    def damage_matrix(self, *, clear):
        forms = ('checksums', 65791, 65793, 256, 'binding', 'copy', 'equal', 'decrease', 'self')
        for form in forms:
            with self.subTest(form=form, clear=clear):
                self.fresh_damaged(form)
                before = self.snapshot()
                for expected in (None, C):
                    self.fault(lambda: self.set_chain(D, expected=expected), 'pointer_damaged')
                    self.assertEqual(self.snapshot(), before)
                self.reports.clear()
                inode = self.pointer.stat().st_ino
                self.repair(clear=clear)
                found = None if clear else D
                self.assertEqual(self.a.current_chain(P), found)
                self.assertEqual(self.pointer.stat().st_ino, inode)
                raw = self.pointer.read_bytes()
                self.assertEqual(len(raw), 65792)
                self.assertEqual(raw[:256], raw[65536:])
                self.assertEqual(struct.unpack_from('<Q', raw, 16)[0], 0)
                self.assertEqual(bool(raw[11] & 4), not clear)
                self.assertEqual(self.reports, [self.t.SealReport(P, found, None, 1234)])
                if not clear:
                    self.assertTrue(self.a.ledger_matches(P, D, self.ledger))
                else:
                    self.assertEqual(set(self.snapshot()), set(before))
                # Six crash boundaries on every damaged form.
                for point in range(6):
                    self.fresh_damaged(form)
                    # Create the new chain's items before the repair hook.
                    if not clear:
                        real_truncate = os.ftruncate
                        def before_repair(fd, size):
                            if size == 65793:
                                raise Stopped()
                            return real_truncate(fd, size)
                        with mock.patch.object(os, 'ftruncate', side_effect=before_repair):
                            with self.assertRaises(Stopped):
                                self.repair(clear=False)
                    fd_inode = self.pointer.stat().st_ino
                    real_truncate, real_write, real_sync = os.ftruncate, os.pwrite, self.s._sync
                    sync_count = [0]
                    def truncate(fd, size):
                        if os.fstat(fd).st_ino != fd_inode:
                            return real_truncate(fd, size)
                        if point == 0 and size == 65793:
                            raise Stopped()
                        result = real_truncate(fd, size)
                        if point == 5 and size == 65792:
                            raise Stopped()
                        return result
                    def write(fd, data, offset):
                        if os.fstat(fd).st_ino == fd_inode:
                            if point == 3 and offset == 65536:
                                real_write(fd, data[:64], offset)
                                raise Stopped()
                            result = real_write(fd, data, offset)
                            if point == 2 and offset == 0:
                                raise Stopped()
                            return result
                        return real_write(fd, data, offset)
                    def sync(fd, **kw):
                        result = real_sync(fd, **kw)
                        if os.fstat(fd).st_ino == fd_inode:
                            sync_count[0] += 1
                            if (point == 1 and sync_count[0] == 1) or (point == 4 and sync_count[0] == 2):
                                raise Stopped()
                        return result
                    with mock.patch.object(os, 'ftruncate', side_effect=truncate), \
                            mock.patch.object(os, 'pwrite', side_effect=write), \
                            mock.patch.object(self.s, '_sync', side_effect=sync):
                        with self.assertRaises(Stopped):
                            self.repair(clear=clear)
                    try:
                        self.assertEqual(self.a.current_chain(P), found)
                    except self.t.SealUnusable as exc:
                        self.assertEqual(exc.cause, 'pointer_damaged')
                    try:
                        self.repair(clear=clear)
                    except self.t.PointerMoved as exc:
                        self.assertIs(exc.expected, self.a.POINTER_UNREADABLE)
                        self.assertEqual(exc.found, found)
                    self.assertEqual(self.a.current_chain(P), found)
        for found in (C, None):
            self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
            self.set_chain()
            if found is None:
                self.a.clear_current(P, expected=C)
            before = self.snapshot()
            with self.assertRaises(self.t.PointerMoved) as caught:
                self.repair(clear=clear)
            self.assertEqual(caught.exception.found, found)
            self.assertEqual(self.snapshot(), before)
        self.pointer.chmod(0)
        try:
            self.fault(lambda: self.repair(clear=clear), 'folder_unusable')
        finally:
            self.pointer.chmod(0o600)

    def test_set_current_over_unreadable_pointer(self):
        self.damage_matrix(clear=False)

    def test_clear_current_over_unreadable_pointer(self):
        self.damage_matrix(clear=True)

    def test_ledger_matches_rule(self):
        self.set_chain()
        recorded = self.s.file_identity(self.ledger)
        for current, answer in [(recorded, True),
                                (replace(recorded, inode=recorded.inode + 1), False),
                                (replace(recorded, uuid=b'x' * 16), recorded.uuid in (None, b'x' * 16))]:
            real = self.s.file_identity
            def identity(fd):
                return current if fd == self.ledger else real(fd)
            with mock.patch.object(self.s, 'file_identity', side_effect=identity):
                self.assertEqual(self.a.ledger_matches(P, C, self.ledger), answer)

    def test_identity_uuid_missing_one_side(self):
        for recorded_uuid, current_uuid in [(b'x' * 16, None), (None, b'x' * 16)]:
            self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
            real = self.s.file_identity
            def recorded(fd):
                value = real(fd)
                return replace(value, uuid=recorded_uuid) if fd == self.ledger else value
            with mock.patch.object(self.s, 'file_identity', side_effect=recorded):
                self.set_chain()
            self.service._identity_warned.clear()
            self.diags.clear()
            def current(fd):
                value = real(fd)
                return replace(value, uuid=current_uuid) if fd == self.ledger else value
            with mock.patch.object(self.s, 'file_identity', side_effect=current):
                for _ in range(10):
                    self.assertTrue(self.a.ledger_matches(P, C, self.ledger))
            self.assertEqual([(type(x).__name__, x.project_id, x.chain_id) for x in self.diags],
                             [('SealIdentityOneSided', P, C)])



    def assert_errno_refusal(self, exc, cause, chain=None, *, report=True):
        self.assertEqual((exc.cause, exc.chain_id), (cause, chain))
        self.assertEqual(exc.errno_name, 'ENOSPC')
        self.assertEqual(str(exc), f"{(cause, chain)!r} errno=ENOSPC")
        expected = [self.t.SealReport(P, chain, cause, 1234, 'ENOSPC')] if report else []
        self.assertEqual(self.reports, expected)

    def missing_project_refusal(self):
        with self.assertRaises(self.t.SealUnusable) as caught:
            with self.service._locked(P, exclusive=True, create=False):
                self.fail('missing project entered the lock')
        return caught.exception

    def test_refusal_without_errno_keeps_its_text(self):
        exc = self.missing_project_refusal()
        self.assertEqual(exc.args, ('folder_unusable', None))
        self.assertEqual(str(exc), "('folder_unusable', None)")
        self.assertEqual([(r.project_id, r.chain_id, r.cause, r.at_ms) for r in self.reports],
                         [(P, None, 'folder_unusable', 1234)])

    def test_refusal_without_errno_has_no_name(self):
        exc = self.missing_project_refusal()
        self.assertIsNone(exc.errno_name)
        self.assertIsNone(self.reports[0].errno_name)

    def test_cannot_record_has_no_name(self):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(self.s, 'write_next', side_effect=self.s.CannotRecord(7)):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service._write_pointer(P, D, damaged=False)
        self.assertEqual(caught.exception.cause, 'folder_unusable')
        self.assertIsNone(caught.exception.errno_name)
        self.assertEqual(self.reports, [self.t.SealReport(P, None, 'folder_unusable', 1234)])

    def test_locked_names_errno(self):
        with self.assertRaises(self.t.SealUnusable) as caught:
            with self.service._locked(P, exclusive=True, create=True):
                raise OSError(errno.ENOSPC, 'forced')
        self.assert_errno_refusal(caught.exception, 'folder_unusable')

    def test_item_read_names_errno(self):
        self.set_chain()
        real_open = self.service._open

        def open_item(project, name, **kwargs):
            if name.endswith('.counter'):
                raise OSError(errno.ENOSPC, 'forced')
            return real_open(project, name, **kwargs)

        for chain, report in ((C, True), (D, False)):
            with self.subTest(chain=chain):
                self.reports.clear()
                with mock.patch.object(self.service, '_open', side_effect=open_item):
                    with self.assertRaises(self.t.SealUnusable) as caught:
                        self.service._item(P, chain, self.s.ItemKind.COUNTER)
                self.assert_errno_refusal(caught.exception, 'counter_unusable', chain, report=report)

    def test_abandoned_check_names_errno(self):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(os, 'pread', side_effect=OSError(errno.ENOSPC, 'forced')):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service._abandoned(P, C, self.s.ItemKind.COUNTER, self.ledger)
        self.assert_errno_refusal(caught.exception, 'counter_unusable', C)

    def test_new_pointer_names_errno(self):
        with self.service._locked(P, exclusive=True, create=True):
            with mock.patch.object(os, 'ftruncate', side_effect=OSError(errno.ENOSPC, 'forced')):
                with self.assertRaises(self.t.SealUnusable) as caught:
                    self.service._new_pointer(P, C)
        self.assert_errno_refusal(caught.exception, 'folder_unusable')

    def test_write_pointer_names_errno(self):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(self.s, 'write_next', side_effect=OSError(errno.ENOSPC, 'forced')):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service._write_pointer(P, D, damaged=False)
        self.assert_errno_refusal(caught.exception, 'folder_unusable')

    def test_write_pointer_names_errno_through_cause(self):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(os, 'pwrite', side_effect=OSError(errno.ENOSPC, 'forced')):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service._write_pointer(P, D, damaged=False)
        self.assert_errno_refusal(caught.exception, 'folder_unusable')

    def test_rebind_write_names_errno_through_cause(self):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(os, 'pwrite', side_effect=OSError(errno.ENOSPC, 'forced')):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.set_chain(expected=C, fd=self.other, bind='rebind')
        self.assert_errno_refusal(caught.exception, 'counter_unusable', C)

    def assert_write_cannot_record_nameless(self, write):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(self.s, 'write_next', side_effect=write):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service._write_pointer(P, D, damaged=False)
        self.assertEqual(caught.exception.cause, 'folder_unusable')
        self.assertIsNone(caught.exception.errno_name)
        self.assertEqual(self.reports, [self.t.SealReport(P, None, 'folder_unusable', 1234)])

    def test_context_only_has_no_name(self):
        def write(*args, **kwargs):
            try:
                raise OSError(errno.ENOSPC, 'forced')
            except OSError:
                raise self.s.CannotRecord(7)

        self.assert_write_cannot_record_nameless(write)

    def test_indirect_cause_has_no_name(self):
        def write(*args, **kwargs):
            try:
                try:
                    raise OSError(errno.ENOSPC, 'forced')
                except OSError as error:
                    raise ValueError('fixture') from error
            except ValueError as error:
                raise self.s.CannotRecord(7) from error

        self.assert_write_cannot_record_nameless(write)

    def test_chains_names_errno(self):
        with mock.patch.object(type(self.service.folder), 'chains',
                               side_effect=OSError(errno.ENOSPC, 'forced')):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service.chains(P)
        self.assert_errno_refusal(caught.exception, 'folder_unusable')

    def test_reserve_counter_write_names_errno(self):
        self.set_chain()
        self.reports.clear()
        with mock.patch.object(self.s, '_write', side_effect=OSError(errno.ENOSPC, 'forced')):
            with self.assertRaises(self.t.SealUnusable) as caught:
                self.service.reserve(P, C, kind='approve', subject='subject', payload=b'{}',
                                     transaction=1, declared_kinds=('approve',))
        self.assert_errno_refusal(caught.exception, 'counter_unusable', C)

    def test_unknown_errno_number_is_kept(self):
        with self.assertRaises(self.t.SealUnusable) as caught:
            with self.service._locked(P, exclusive=True, create=True):
                raise OSError(99999, 'forced')
        self.assertEqual(caught.exception.errno_name, '99999')
        self.assertEqual(str(caught.exception), "('folder_unusable', None) errno=99999")

    def test_refusal_round_trips_without_errno(self):
        exc = self.t.SealUnusable('folder_unusable', None)
        for rebuild in (copy.copy, lambda e: pickle.loads(pickle.dumps(e))):
            with self.subTest(rebuild=rebuild):
                rebuilt = rebuild(exc)
                self.assertEqual(rebuilt.args, ('folder_unusable', None))
                self.assertEqual((rebuilt.cause, rebuilt.chain_id), ('folder_unusable', None))
                self.assertEqual(str(rebuilt), "('folder_unusable', None)")

    def test_refusal_round_trips_with_errno(self):
        exc = self.t.SealUnusable('folder_unusable', None, 'ENOSPC')
        for rebuild in (copy.copy, lambda e: pickle.loads(pickle.dumps(e))):
            with self.subTest(rebuild=rebuild):
                rebuilt = rebuild(exc)
                self.assertEqual(rebuilt.args, ('folder_unusable', None, 'ENOSPC'))
                self.assertEqual((rebuilt.cause, rebuilt.chain_id), ('folder_unusable', None))
                self.assertEqual(rebuilt.errno_name, 'ENOSPC')
                self.assertEqual(str(rebuilt), "('folder_unusable', None) errno=ENOSPC")

    def test_pointer_write_failure_is_folder_unusable(self):
        self.set_chain()
        for clear in (False, True):
            before = self.pointer.read_bytes(); self.reports.clear()
            with mock.patch.object(os, 'pwrite', side_effect=OSError(errno.ENOSPC, 'full')):
                self.fault(lambda: self.a.clear_current(P, expected=C) if clear
                           else self.set_chain(expected=C), 'folder_unusable')
            self.assertEqual(self.pointer.read_bytes(), before)
            self.assertEqual(self.reports, [self.t.SealReport(P, None, 'folder_unusable', 1234, 'ENOSPC')])
        for failure in (OSError(errno.ENOSPC, 'full'), self.s.CannotRecord(7)):
            self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
            self.reports.clear()
            original = self.service._new_pointer
            def new(*args):
                with mock.patch.object(self.s, '_write', side_effect=failure):
                    return original(*args)
            with mock.patch.object(self.service, '_new_pointer', side_effect=new):
                self.fault(lambda: self.set_chain(), 'folder_unusable')
            self.assertFalse(self.pointer.exists())
            expected = (self.t.SealReport(P, None, 'folder_unusable', 1234, 'ENOSPC')
                        if isinstance(failure, OSError)
                        else self.t.SealReport(P, None, 'folder_unusable', 1234))
            self.assertEqual(self.reports, [expected])

    def test_bad_ledger_descriptor_is_callers_fault(self):
        self.set_chain(); self.reports.clear()
        fd = os.dup(self.ledger); os.close(fd)
        for call in (lambda: self.a.ledger_matches(P, C, fd),
                     lambda: self.a.set_current(P, C, ledger_fd=fd, expected=C, bind='rebind')):
            try:
                with self.assertRaises(OSError) as caught:
                    call()
            except self.t.SealUnusable:
                self.fail('bad descriptor was mapped to folder_unusable')
            self.assertEqual(caught.exception.errno, errno.EBADF)
            self.assertEqual(self.reports, [])

    def test_seal_root_never_from_environment(self):
        with mock.patch.dict(os.environ, {'HOME': '/wrong', 'XDG_DATA_HOME': '/wrong',
                                          'ADT_APPROVAL_SEAL_ROOT': '/wrong'}), \
                mock.patch.object(os, 'open', side_effect=AssertionError('opened')), \
                mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir='/password-home')) as passwd:
            home = Path('/password-home')
            suffix = 'Library/Application Support/adt-approval-seal' if sys.platform == 'darwin' else '.local/share/adt-approval-seal'
            self.assertEqual(self.a.production_root(), home / suffix)
            self.assertEqual(passwd.call_args_list, [mock.call(os.getuid())])
            self.assertEqual(self.a.runtime_instance_id(), R)
        self.assertFalse(self.root.exists())
        renderer = object()
        self.service.attach_renderer(renderer)
        with self.assertRaises(RuntimeError):
            self.service.attach_renderer(renderer)

    def stop_creation(self, *, between):
        real = os.unlink
        def stop(path, **kw):
            if str(path).startswith('.creating-' + C + '.counter.'):
                if not between:
                    real(path, **kw)
                raise Stopped()
            return real(path, **kw)
        with mock.patch.object(os, 'unlink', side_effect=stop):
            with self.assertRaises(Stopped):
                self.set_chain()
        counter = self.project / (C + '.counter')
        inode = counter.stat().st_ino
        self.assertEqual(counter.stat().st_nlink, 2 if between else 1)
        self.set_chain()
        self.assertEqual(counter.stat().st_ino, inode)
        self.assertEqual(counter.stat().st_nlink, 1)
        self.assertFalse(list(self.project.glob('.creating-*')))
        self.assertEqual(self.a.current_chain(P), C)
        for offset in (0, 65536):
            self.assertEqual(struct.unpack_from('<Q', counter.read_bytes(), offset + 64)[0], inode)
        self.a.clear_current(P, expected=C)
        fd = os.open(counter, os.O_RDWR)
        try:
            state = self.s.read_slots(fd, kind=self.s.ItemKind.COUNTER,
                                      binding=(P, C), ledger_identity=None)
            self.s.write_next(fd, state, value=1)
        finally:
            os.close(fd)
        before = self.snapshot()
        self.fault(lambda: self.set_chain(), 'counter_unusable', C)
        self.assertEqual(self.snapshot(), before)

    def test_creation_stop_after_unlink(self):
        self.stop_creation(between=False)
        # An abandoned creation must be the complete sequence-zero pair,
        # for both item kinds, rather than merely one readable surviving slot.
        for kind in ('counter', 'high-water'):
            self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
            self.set_chain()
            self.a.clear_current(P, expected=C)
            item = self.project / (C + '.' + kind)
            raw = bytearray(item.read_bytes()); raw[65536] ^= 1
            item.write_bytes(raw)
            before = self.snapshot()
            self.fault(lambda: self.set_chain(), 'counter_unusable' if kind == 'counter' else 'high_water_unusable', C)
            self.assertEqual(self.snapshot(), before)

    def test_creation_stop_between_link_and_unlink(self):
        self.stop_creation(between=True)

    def blocked_flock(self):
        from relay_engine import seal_files
        fcntl = seal_files.fcntl
        original = fcntl.flock
        condition, blocked = threading.Condition(), []
        def flock(fd, operation):
            if operation & fcntl.LOCK_UN or operation & fcntl.LOCK_NB:
                return original(fd, operation)
            try:
                return original(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                with condition:
                    blocked.append((threading.get_ident(), operation))
                    condition.notify_all()
                return original(fd, operation)
        def witnessed(thread, operation):
            with condition:
                self.assertTrue(condition.wait_for(
                    lambda: (thread.ident, operation) in blocked, timeout=30),
                    'no blocked flock witness')
        return mock.patch.object(fcntl, 'flock', side_effect=flock), witnessed

    def concurrent(self, reader_first):
        self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
        self.set_chain()
        entered, release, attempted, finished = (threading.Event() for _ in range(4))
        errors = []
        self.addCleanup(release.set)
        original = self.s.write_next
        def held(fd, state, **kw):
            if state.kind == self.s.ItemKind.POINTER:
                entered.set()
                if not release.wait(60):
                    raise AssertionError('release missing')
            return original(fd, state, **kw)
        def writer():
            try:
                attempted.set()
                self.set_chain(D, expected=C)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        flock_patch, witnessed = self.blocked_flock()
        with flock_patch, mock.patch.object(self.s, 'write_next', side_effect=held):
            if reader_first:
                with self.service.folder.lock(P, exclusive=False):
                    t = threading.Thread(target=writer); t.start()
                    self.addCleanup(t.join, 30); self.addCleanup(release.set)
                    self.assertTrue(attempted.wait(30))
                    witnessed(t, fcntl.LOCK_EX)
                    self.assertFalse(entered.is_set())
                self.assertTrue(entered.wait(30)); release.set(); t.join(30)
            else:
                t = threading.Thread(target=writer); t.start()
                self.addCleanup(t.join, 30); self.addCleanup(release.set)
                self.assertTrue(entered.wait(30))
                result = []
                def read():
                    result.append(self.a.current_chain(P))
                r = threading.Thread(target=read); r.start()
                self.addCleanup(r.join, 30); self.addCleanup(release.set)
                witnessed(r, fcntl.LOCK_SH)
                release.set(); t.join(30); r.join(30)
                self.assertEqual(result, [D])
        self.assertTrue(finished.is_set()); self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

    def test_pointer_write_takes_exclusive_lock(self):
        self.concurrent(reader_first=True)
        self.concurrent(reader_first=False)

    def test_creation_lock_serializes(self):
        real_link = os.link
        published, release = threading.Event(), threading.Event()
        failures = []
        def link(src, dst, **kw):
            real_link(src, dst, **kw)
            if str(dst) == C + '.counter':
                published.set()
                if not release.wait(60):
                    raise AssertionError('no release')
        def creator():
            try:
                self.set_chain()
            except BaseException as exc:
                failures.append(exc)
        flock_patch, witnessed = self.blocked_flock()
        self.addCleanup(release.set)
        with flock_patch, mock.patch.object(os, 'link', side_effect=link):
            first = threading.Thread(target=creator); first.start()
            self.addCleanup(first.join, 30); self.addCleanup(release.set)
            self.assertTrue(published.wait(30))
            leftovers = list(self.project.glob('.creating-*'))
            second = threading.Thread(target=creator); second.start()
            self.addCleanup(second.join, 30); self.addCleanup(release.set)
            witnessed(second, fcntl.LOCK_EX)
            self.assertEqual(list(self.project.glob('.creating-*')), leftovers)
            release.set(); first.join(30); second.join(30)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], self.t.PointerMoved)
        self.assertEqual(self.a.current_chain(P), C)

    def test_lock_file_never_replaced(self):
        self.set_chain()
        lock = self.project / 'lock'
        inode = lock.stat().st_ino
        lock.write_bytes(b'retained')
        self.set_chain(D, expected=C)
        self.a.clear_current(P, expected=D)
        self.a.return_current(P, C, expected=None)
        self.assertEqual((lock.stat().st_ino, lock.read_bytes()), (inode, b'retained'))

    def test_link_refused_is_folder_unusable(self):
        for error in (errno.EPERM, errno.ENOTSUP):
            self.root = self.base / str(uuid.uuid4()); self.service.folder.root = self.root
            with mock.patch.object(os, 'link', side_effect=OSError(error, 'refused')), \
                    mock.patch.object(os, 'rename', side_effect=AssertionError('rename')):
                self.fault(lambda: self.set_chain(), 'folder_unusable')
            self.assertFalse((self.project / (C + '.counter')).exists())
            self.assertFalse(self.pointer.exists())
            self.set_chain()  # Removes the before-link leftover and starts afresh.
            self.assertFalse(list(self.project.glob('.creating-*')))

    def test_folder_unwritable_after_creation(self):
        self.set_chain()
        before = self.snapshot()
        self.project.chmod(0o500)
        try:
            self.fault(lambda: self.set_chain(D, expected=C), 'folder_unusable')
            self.assertEqual(self.snapshot(), before)
        finally:
            self.project.chmod(0o700)
        self.set_chain(D, expected=C)
        self.assertEqual(self.a.current_chain(P), D)

    def test_calls_refuse_without_install(self):
        self.context.close()
        calls = [lambda: self.a.current_chain(P), lambda: self.a.counter_head(P, C),
                 lambda: self.a.rendered_through(P, C), lambda: self.a.is_broken(P, C),
                 lambda: self.a.ledger_matches(P, C, self.ledger), self.a.runtime_instance_id,
                 lambda: self.a.folder_state(P), lambda: self.a.chains(P),
                 lambda: self.set_chain(), lambda: self.a.return_current(P, C, expected=None),
                 lambda: self.a.clear_current(P, expected=C)]
        for call in calls:
            with self.assertRaises(RuntimeError):
                call()
        self.assertFalse(self.root.exists())

    def test_second_install_refuses(self):
        with self.assertRaises(RuntimeError):
            with self.a.install(self.service):
                self.fail('installed twice')
        self.assertEqual(self.a.runtime_instance_id(), R)
