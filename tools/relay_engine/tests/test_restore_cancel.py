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


class TestCut(RestoreStore):
    def standing(self):
        import hashlib
        from relay_engine import claims
        return claims.standing_objection(self.conn, 'p', str(self.plan),
                                         hashlib.sha256(self.plan.read_bytes()).hexdigest())

    def imported_approve(self, claim):
        import hashlib
        from relay_engine import imports
        from relay_engine.tests.test_claims import REVIEWER
        self.clock = self.conn.execute("SELECT coalesce(max(created_at), 0) + 1 FROM relays WHERE project_id='p'").fetchone()[0]
        relay = imports.ImportedRelay(
            created_at=self.clock, kind='REVIEW', verdict='approve', subject='Imported approval',
            sender=REVIEWER.seat_id, sender_role=REVIEWER.role,
            team_instance_id=REVIEWER.team_instance_id, to=(PLANNER.seat_id,), cc=(),
            parent_id=self.request(claim)[0], parent_spelling=self.request(claim)[0],
            body=b'imported approval', header_bytes=0, header_sha256=hashlib.sha256(b'').hexdigest())
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            rid = imports.import_relay_step(self.conn, 'p', relay, marked_at_ms=self.clock)
            # Store's provenance row accompanies the real Engine import step.
            self.conn.execute('INSERT INTO imported_relays (relay_id, root_uuid, seq, stamp, zone,'
                              ' utc_offset_s, rendered_path, origin, headers_json, source_sha256,'
                              ' delivery_marked_at) VALUES (?, ?, 1, ?, ?, 0, ?, ?, ?, ?, ?)',
                              (rid, rid, '20261115-000000', 'UTC', 'old/review.md', '2.9.7',
                               '{}', hashlib.sha256(relay.body).hexdigest(), self.clock))
            self.conn.commit()
            return rid
        except BaseException:
            self.conn.rollback()
            raise

    def test_pre_cut_approve_does_not_count(self):
        # RC-M4: the maximum handle belongs to the cut, not the post-cut side.
        from relay_engine import claims
        from relay_engine.tests.test_claims import REVIEWER
        _, old = self.claim()
        self.verify(old)
        self.conn.execute("UPDATE seats SET state='stood-down' WHERE id=?", (REVIEWER.seat_id,))
        self.conn.commit()
        _, boundary = self.claim()
        cut = self.cancel().cut_handle
        handle = self.conn.execute('SELECT handle FROM relays WHERE id=?', (boundary.relay_id,)).fetchone()[0]
        self.assertEqual(handle, cut)
        self.assertTrue(self.rc().claim_is_cut(cut, handle))
        self.assertEqual(self.state(old), 'pending')
        self.conn.execute("UPDATE seats SET state='occupied' WHERE id=?", (REVIEWER.seat_id,))
        self.conn.commit()
        _, fresh = self.claim()
        self.verify(fresh)
        self.assertEqual(self.state(fresh), 'approved')

    def test_pre_cut_approve_does_not_lift_rule_h(self):
        # RC-M7: a pre-cut approve must be removed before choosing the reviewer's newest.
        _, old = self.claim()
        objection = self.verify(old, 'must-revise')
        self.verify(old)
        self.assertIsNone(self.standing())
        self.cancel()
        self.assertIsNotNone(self.standing())
        self.assertEqual(self.standing().review_relay_id, objection.relay_id)
        _, fresh = self.claim()
        self.verify(fresh)
        self.assertIsNone(self.standing())

    def test_late_verdict_on_old_request_does_not_count(self):
        _, old = self.claim()
        self.cancel()
        self.verify(old)
        self.assertEqual(self.state(old), 'pending')

    def test_imported_verdict_after_cut_does_not_count(self):
        # RC-M5: imported approvals above the cut never re-approve restored state.
        self.cancel()
        _, fresh = self.claim()
        objection = self.verify(fresh, 'must-revise')
        self.imported_approve(fresh)
        self.assertEqual(self.state(fresh), 'objected')
        self.assertIsNotNone(self.standing())
        self.assertEqual(self.standing().review_relay_id, objection.relay_id)
        self.verify(fresh)
        self.assertEqual(self.state(fresh), 'approved')
        self.assertIsNone(self.standing())

    def test_never_cancelled_project_counts_imported_approve(self):
        # RC-M6: absent cancellation is distinct from cut zero.
        _, old = self.claim()
        self.verify(old, 'must-revise')
        self.imported_approve(old)
        self.assertEqual(self.state(old), 'approved')
        self.assertIsNone(self.standing())

    def test_zero_cut_is_a_cancellation(self):
        self.assertEqual(self.cancel().cut_handle, 0)
        _, fresh = self.claim()
        self.imported_approve(fresh)
        self.assertEqual(self.state(fresh), 'pending')
        self.verify(fresh)
        self.assertEqual(self.state(fresh), 'approved')

    def test_objection_and_waiver_kept(self):
        from relay_engine import claims
        _, old = self.claim()
        self.verify(old, 'must-revise')
        self.cancel()
        self.assertEqual(self.state(old), 'objected')
        self.release()
        _, waived = self.claim()
        claims.waive(self.conn, waived.relay_id, 'plan', 'operator', 'kept waiver', self.clock)
        self.cancel('job-b')
        self.assertEqual(self.state(waived), 'waived')

    def reapprove(self, kind):
        dc, pc, _ = self.build(design=kind == 'design')
        old_id = (pc if kind == 'plan' else dc).relay_id
        self.cancel()
        fresh = self.approved(kind, parent=pc.relay_id)
        parent = fresh
        if kind == 'design':
            # The restore cuts the plan too; re-approve it to isolate the design check.
            parent = self.approved('plan', parent=fresh.relay_id)
            self.verify(parent)
        self.release()
        issued = self.issue(parent)
        self.assertIn((kind, fresh.relay_id), self.claim_artifacts(issued, kind))
        self.assertNotIn((kind, old_id), self.claim_artifacts(issued, kind))
        self.assertEqual(self.act().code, 'authority.verification-pending')
        request = self.conn.execute('SELECT DISTINCT request_relay_id FROM verification_requests'
                                    ' WHERE claim_relay_id=?', (old_id,)).fetchone()[0]
        from relay_engine.tests.test_claims import REVIEWER
        self.submit(draft(kind='REVIEW', parent=request, verdict='approve', to=PLANNER.address), REVIEWER)
        self.assertEqual(self.act().code, 'authority.verification-pending')
        self.verify(fresh)
        self.assertEqual(self.act().outcome, 'allow')

    def test_same_bytes_plan_reapproval_binds_fresh_claim(self):
        # RC-M8: keeping the cut same-bytes claim strands the re-approval.
        self.reapprove('plan')

    def test_same_bytes_design_reapproval_binds_fresh_claim(self):
        self.reapprove('design')

    def test_waived_cut_claim_is_kept(self):
        # RC-M9: a cut alone must not replace a waived claim.
        from relay_engine import claims
        _, pc, _ = self.build()
        claims.waive(self.conn, pc.relay_id, 'plan', 'operator', 'kept waiver', self.clock)
        self.cancel()
        fresh = self.approved('plan', parent=pc.relay_id)
        self.release()
        issued = self.issue(fresh)
        self.assertIn(('plan', pc.relay_id), self.claim_artifacts(issued, 'plan'))
        self.assertNotIn(('plan', fresh.relay_id), self.claim_artifacts(issued, 'plan'))

    def test_cut_statements(self):
        from relay_engine import claims
        _, old = self.claim()
        self.verify(old)
        traced = []
        self.conn.set_trace_callback(traced.append)
        try:
            self.standing()
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(sum('FROM restore_cancels WHERE project_id' in q for q in traced), 1)
        traced = []
        self.conn.set_trace_callback(traced.append)
        try:
            self.state(old)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(sum('LEFT JOIN restore_cancels' in q for q in traced), 1)
        plan = self.conn.execute('EXPLAIN QUERY PLAN ' + self.rc().CUT_SQL, ('p',)).fetchall()
        self.assertTrue(any('SEARCH restore_cancels' in row[3] and 'INDEX' in row[3] for row in plan), plan)
