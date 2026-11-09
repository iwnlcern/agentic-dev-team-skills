"""Authority entry uses real admission and leaves the caller's transaction intact."""
import hashlib
from pathlib import Path
import sqlite3
from unittest import mock

from relay_engine import authority, slips
from relay_engine.tests import test_authority as legacy_tests
from relay_engine.tests.test_slips import SlipStore
from relay_engine.tests.test_claims import SECOND
from relay_engine.tests.test_submit import IMPL


class TestAuthorityV3(SlipStore):
    def entry(self, seat=IMPL, branch='work'):
        return authority.act_entry(self.conn, seat.seat_id,
                                   slips.Effect('commit', 'repo-1', branch), self.clock)

    def test_kit_task9_answers(self):
        _, _, issued = self.build()
        sid = self.slip(issued)[0]
        self.conn.execute('BEGIN IMMEDIATE')
        allowed = self.entry()
        self.assertEqual((allowed.outcome, allowed.code, allowed.slip_id, allowed.revision),
                         ('allow', None, sid, 1))
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        missing = self.entry(SECOND, branch='missing')
        self.assertEqual((missing.outcome, missing.code), ('deny', 'authority.slip-missing'))
        self.assertTrue(missing.reason)
        self.assertTrue(missing.unblocker)
        self.conn.commit()
        slips.revoke(self.conn, sid, 'operator', 'stop', self.clock)
        self.conn.execute('BEGIN IMMEDIATE')
        revoked = self.entry()
        self.assertEqual((revoked.outcome, revoked.code, revoked.slip_id),
                         ('deny', 'authority.slip-invalid', sid))
        self.assertIn('revoked', revoked.reason)
        self.conn.commit()

    def test_working_copy_digest(self):
        self.assertEqual(authority.working_copy_digest(str(self.plan)),
                         hashlib.sha256(self.plan.read_bytes()).hexdigest())
        self.assertIsNone(authority.working_copy_digest(str(self.plan.with_name('missing'))))
        with mock.patch.object(Path, 'open', side_effect=PermissionError('unreadable working copy')):
            self.assertIsNone(authority.working_copy_digest(str(self.plan)))
        self.plan.write_bytes(b'changed working copy\n')
        self.assertEqual(authority.working_copy_digest(str(self.plan)),
                         hashlib.sha256(b'changed working copy\n').hexdigest())

    def test_act_entry_requires_open_transaction(self):
        before = self.conn.total_changes
        with mock.patch.object(slips, 'act') as act:
            with self.assertRaises(ValueError):
                self.entry()
            act.assert_not_called()
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
        closed = sqlite3.connect(':memory:')
        closed.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            authority.act_entry(closed, IMPL.seat_id, slips.Effect('commit', 'repo-1', 'work'), self.clock)

    def test_act_entry_leaves_commit_to_caller(self):
        self.build()
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.entry().outcome, 'allow')
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 1)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.entry().outcome, 'allow')
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 1)

    def test_act_entry_fault_rolls_back_own_writes(self):
        self.build()
        real = slips.act
        for mode in ('raise', 'held'):
            with self.subTest(mode=mode):
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    marker = 'marker-' + mode
                    self.conn.execute('INSERT INTO repositories (id, project_id, name) VALUES (?, ?, ?)',
                                      (marker, 'p', marker))
                    witnessed = []
                    def after_real(*args, **kwargs):
                        answer = real(*args, **kwargs)
                        count = self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0]
                        witnessed.append((answer.outcome, count, kwargs['digest'] is authority.working_copy_digest))
                        if mode == 'raise':
                            raise RuntimeError('fault after real binding')
                        return slips.Answer('held', 'authority.check-unavailable', 'injected fault', 'operator', None, None)
                    with mock.patch.object(slips, 'act', side_effect=after_real):
                        answer = self.entry(branch=mode)
                    self.assertEqual(witnessed, [('allow', 1, True)])
                    self.assertEqual((answer.outcome, answer.code), ('held', 'authority.check-unavailable'))
                    self.assertTrue(answer.reason)
                    self.assertTrue(answer.unblocker)
                    self.assertTrue(self.conn.in_transaction)
                    self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
                    self.conn.commit()
                    self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
                    self.assertEqual(self.conn.execute('SELECT id FROM repositories WHERE id = ?', (marker,)).fetchone(),
                                     (marker,))
                finally:
                    self.conn.rollback()

    def test_act_entry_reads_no_bodies(self):
        self.build()
        real = slips.act
        def guarded(*args, **kwargs):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute('SELECT body FROM bodies').fetchall()
            return real(*args, **kwargs)
        self.conn.execute('BEGIN IMMEDIATE')
        with mock.patch.object(slips, 'act', side_effect=guarded):
            self.assertEqual(self.entry().outcome, 'allow')
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        # The caller's access is restored when the entry's read guard exits.
        self.assertTrue(self.conn.execute('SELECT body FROM bodies LIMIT 1').fetchone())

    def test_legacy_reader_unchanged(self):
        fixture = legacy_tests.TestAuthority('test_valid_grant_unchanged_artifact_allows')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.base()
        self.assertTrue(fixture.check()['allowed'])
        root = Path(fixture.root.path)
        self.assertEqual(authority.read_artifact(root, 'plans/PL-base.md'), b'original')
        fixture.files['PL-base'].write_bytes(b'edited')
        self.assertIn('authority.artifact-changed', fixture.check()['reasons'])
        self.assertEqual(authority.read_artifact(root, 'plans/PL-base.md'), b'edited')
        fixture.files['PL-base'].unlink()
        with self.assertRaises(FileNotFoundError):
            authority.read_artifact(root, 'plans/PL-base.md')
