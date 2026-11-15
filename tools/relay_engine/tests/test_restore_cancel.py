"""Restore cancellation through admission-backed slips and claims."""
import importlib
import unittest

from relay_engine import slips
from relay_engine.tests.test_slips import SlipStore
from relay_engine.tests.test_submit import PLANNER, draft


class RestoreStore(SlipStore):
    def rc(self):
        try:
            return importlib.import_module('relay_engine.restore_cancel')
        except ModuleNotFoundError as error:
            if error.name != 'relay_engine.restore_cancel':
                raise
            self.fail('restore cancellation is not implemented')

    def cancel(self, job='job-a', project='p'):
        return self.rc().cancel_all(self.conn, project, job, self.clock)

    def release(self):
        self.rc().release(self.conn, 'p', 'operator', self.clock)

    def saved(self):
        return tuple((table, self.conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall())
                     for table in ('restore_cancels', 'slips', 'slip_events'))


class TestCancel(RestoreStore):
    def test_cancel_all_revokes_every_live_slip(self):
        # RC-M3: a consumed slip must not be revoked again.
        _, _, consumed = self.build(bundle='b1')
        consumed_id = self.issued(consumed)
        # No merge-slip consumer exists on this base; seed its already-consumed state.
        self.conn.execute("UPDATE slips SET state='consumed', ended_at=?, end_reason='merged',"
                          ' revision=revision+1 WHERE id=?', (self.clock, consumed_id))
        self.conn.commit()
        old = self.conn.execute('SELECT * FROM slips WHERE id=?', (consumed_id,)).fetchone()
        _, _, first = self.build(bundle='b1')
        _, _, second = self.build(bundle='b2')
        ids = (self.issued(first), self.issued(second))
        result = self.cancel()
        self.assertEqual(set(result.revoked), set(ids))
        self.assertFalse(result.already)
        for sid in ids:
            self.assertEqual(self.conn.execute('SELECT state, end_reason, ended_at, revision'
                                              ' FROM slips WHERE id=?', (sid,)).fetchone(),
                             ('revoked', 'revoked', self.clock, 2))
            self.assertEqual(self.conn.execute("SELECT event, by_seat_id, by_special, detail"
                                              " FROM slip_events WHERE slip_id=? AND revision=2",
                                              (sid,)).fetchone(),
                             ('revoke', None, 'operator', 'restore:job-a'))
        self.assertEqual(self.conn.execute('SELECT * FROM slips WHERE id=?', (consumed_id,)).fetchone(), old)
        empty = self.cancel('empty', 'q')
        self.assertEqual(empty.revoked, ())
        self.assertEqual(self.rc().held(self.conn, 'q').job_id, 'empty')

    def test_cancel_all_replay_is_a_no_op(self):
        # RC-M1: a replay cannot add writes, even at a later timestamp.
        self.build()
        first = self.cancel()
        before = self.saved()
        result = self.rc().cancel_all(self.conn, 'p', 'job-a', self.clock + 100)
        self.assertEqual(self.saved(), before)
        self.assertTrue(result.already)
        self.assertEqual(result.revoked, ())
        self.assertEqual(result.cut_handle, first.cut_handle)

    def test_cancel_all_second_restore_raises_the_cut(self):
        # RC-M2: the later project's relays must advance the next cut.
        self.build()
        first = self.cancel()
        self.release()
        self.submit(draft(kind='SITREP'))
        result = self.cancel('job-b')
        self.assertGreater(result.cut_handle, first.cut_handle)
        self.assertEqual(self.rc().held(self.conn, 'p').job_id, 'job-b')
        self.assertFalse(result.already)

    def test_cancel_all_needs_clean_connection(self):
        rc = self.rc()
        before = self.saved()
        self.conn.execute('BEGIN')
        with self.assertRaises(ValueError):
            rc.cancel_all(self.conn, 'p', 'job-a', self.clock)
        self.conn.rollback()
        self.assertEqual(self.saved(), before)
        with self.assertRaisesRegex(ValueError, 'unknown project'):
            rc.cancel_all(self.conn, 'missing', 'job-a', self.clock)
        self.assertEqual(self.saved(), before)

    def test_release_is_operator_only(self):
        # RC-M15: a seat cannot release a hold or change any retained row.
        self.cancel()
        before = self.saved()
        with self.assertRaisesRegex(ValueError, 'operator only'):
            self.rc().release(self.conn, 'p', PLANNER.seat_id, self.clock)
        self.assertEqual(self.saved(), before)
        with self.assertRaisesRegex(ValueError, 'no restore hold'):
            self.rc().release(self.conn, 'q', 'operator', self.clock)
        self.assertEqual(self.saved(), before)
        self.release()
        self.assertIsNone(self.rc().held(self.conn, 'p'))
        self.assertEqual(self.conn.execute('SELECT released_at,released_by FROM restore_cancels'
                                          " WHERE project_id='p'").fetchone(), (self.clock, 'operator'))
        after = self.saved()
        with self.assertRaisesRegex(ValueError, 'no restore hold'):
            self.release()
        self.assertEqual(self.saved(), after)
        self.assertIsNotNone(self.rc().cut_of(self.conn, 'p'))

    def test_cut_of_absent_and_zero(self):
        rc = self.rc()
        self.assertIsNone(rc.cut_of(self.conn, 'q'))
        result = self.cancel('empty', 'q')
        self.assertEqual(result.cut_handle, 0)
        self.assertEqual(rc.cut_of(self.conn, 'q'), 0)
