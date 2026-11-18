"""Restore cancellation through admission-backed slips and claims."""
import importlib
import unittest

from relay_engine import slips
from relay_engine.tests.test_slips import SlipStore, with_repo
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
    def test_cancel_rolls_back_on_failure_after_first_write(self):
        # RC-M16: without rollback, the earlier revocations and events remain.
        import sqlite3
        self.build()
        self.cancel('job-0')
        self.release()
        _, _, first = self.build(bundle='b1')
        _, _, second = self.build(bundle='b2')
        ids = (self.issued(first), self.issued(second))
        before = self.saved()
        for event in ('INSERT', 'UPDATE'):
            name = 'fail_cancel_' + event.lower()
            self.conn.execute('CREATE TEMP TRIGGER ' + name + ' BEFORE ' + event
                              + " ON restore_cancels BEGIN SELECT RAISE(ABORT, 'injected'); END")
            self.addCleanup(self.conn.execute, 'DROP TRIGGER IF EXISTS ' + name)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected'):
            self.rc().cancel_all(self.conn, 'p', 'job-b', self.clock)
        self.assertEqual(self.saved(), before)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(self.rc().held(self.conn, 'p'))
        for event in ('insert', 'update'):
            self.conn.execute('DROP TRIGGER fail_cancel_' + event)
        result = self.cancel('job-b')
        self.assertIsInstance(result, self.rc().Cancelled)
        self.assertEqual(set(result.revoked), set(ids))
        self.assertFalse(result.already)

    def test_release_rolls_back_on_failure_after_its_write(self):
        # RC-M17: ABORT undoes the UPDATE, but only rollback ends its transaction.
        import sqlite3
        self.cancel('job-a')
        before = self.saved()
        self.conn.execute("CREATE TEMP TRIGGER fail_release AFTER UPDATE ON restore_cancels"
                          " BEGIN SELECT RAISE(ABORT, 'injected'); END")
        self.addCleanup(self.conn.execute, 'DROP TRIGGER IF EXISTS fail_release')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected'):
            self.rc().release(self.conn, 'p', 'operator', self.clock)
        self.assertEqual(self.saved(), before)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.rc().held(self.conn, 'p').job_id, 'job-a')
        self.conn.execute('DROP TRIGGER fail_release')
        self.release()
        self.assertIsNone(self.rc().held(self.conn, 'p'))

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


class TestHold(RestoreStore):
    def operator_issue(self, seat=None, bundle='operator'):
        from relay_engine import submit
        from relay_engine.tests.test_submit import IMPL
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        return self.submit(with_repo(draft(kind='IMPL', needs='act', to=(seat or IMPL).address,
                                 bundle=bundle)), operator)

    def refusal_rows(self):
        return self.conn.execute('SELECT relay_id, job_id FROM restore_refusals ORDER BY rowid').fetchall()

    def test_hold_refuses_planner_issue(self):
        # RC-M10: a recorded refusal must return before creating a slip.
        from relay_engine.tests.test_submit import ORCH
        _, pc, _ = self.build(issue=False, design=False)
        self.cancel()
        for issuer in (PLANNER, ORCH):
            refused = self.issue(pc, issuer)
            self.assertIsNone(self.slip(refused))
            self.assertIn((refused.relay_id, 'job-a'), self.refusal_rows())
            answer = self.act()
            self.assertEqual((answer.outcome, answer.code, answer.unblocker),
                             ('held', 'authority.restore-held', 'operator'))
            self.assertIn('job-a', answer.reason)
            self.assertIn(refused.relay_id, answer.reason)
        self.assertEqual(len(self.refusal_rows()), 2)

    def test_hold_answer_over_revoked_slip(self):
        # RC-M14: a selected revoked slip must not hide a newer hold refusal.
        _, pc, old = self.build(design=False)
        self.cancel()
        refused = self.issue(pc)
        for branch in ('work', 'bound'):
            if branch == 'bound':
                # Bind through a live operator slip, then cancel it under a new job.
                self.release()
                live = self.operator_issue(bundle='b1')
                self.assertEqual(self.act(branch=branch).outcome, 'allow')
                self.cancel('job-b')
                refused = self.issue(pc)
            answer = self.act(branch=branch)
            self.assertEqual(answer.code, 'authority.restore-held')
            self.assertEqual(answer.unblocker, 'operator')
            self.assertIn(refused.relay_id, answer.reason)
            self.assertIsNotNone(answer.slip_id)

    def test_hold_keeps_independent_answers(self):
        # RC-M11: a hold cannot record an IMPL already refused for evidence.
        from relay_engine.tests.test_submit import IMPL
        from relay_engine.tests.test_claims import SECOND
        _, pc, _ = self.build(design=False)
        self.operator_issue(SECOND, 'other')
        self.assertEqual(self.act().outcome, 'allow')
        self.cancel()
        refusal = self.answer(self.ask('plan', pc, self.plan), 'must-revise', self.plan)
        invalid = self.issue(refusal)
        self.assertEqual(self.refusal_rows(), [])
        self.assertIsNone(self.slip(invalid))
        self.assertNotEqual(self.act().code, 'authority.restore-held')
        other = self.act(seat=SECOND)
        self.assertEqual(other.code, 'authority.slip-invalid')
        self.assertIn(IMPL.seat_id, other.reason)
        self.assertEqual(self.act(seat=PLANNER).outcome, 'allow')
        self.assertEqual(self.act(kind='pr-merge').code, 'authority.slip-missing')
        self.assertEqual(self.act(kind='key-file-read').code, 'authority.check-unavailable')

    def test_hold_admits_operator_issue(self):
        # RC-M12: the operator's IMPL is checked normally while the project is held.
        self.cancel()
        issued = self.operator_issue()
        self.assertIsNotNone(self.slip(issued))
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, self.issued(issued))
        self.assertEqual(self.refusal_rows(), [])

    def test_release_ends_hold_answers(self):
        _, pc, _ = self.build(design=False)
        self.cancel()
        self.issue(pc)
        self.assertEqual(self.act().code, 'authority.restore-held')
        self.release()
        self.assertEqual(self.act().code, 'authority.slip-invalid')
        fresh = self.approved('plan', parent=pc.relay_id)
        self.verify(fresh)
        self.assertIsNotNone(self.slip(self.issue(fresh)))
        self.assertEqual(self.act().outcome, 'allow')

    def test_bound_branch_after_cancel(self):
        from relay_engine.tests.test_claims import SECOND
        _, pc, _ = self.build(design=False)
        self.operator_issue(SECOND, 'other')
        self.assertEqual(self.act().outcome, 'allow')
        self.cancel()
        self.release()
        fresh = self.approved('plan', parent=pc.relay_id)
        self.verify(fresh)
        self.issue(fresh)
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act(seat=SECOND).code, 'authority.slip-invalid')

    def test_refusal_answers_only_its_own_hold(self):
        # RC-M13: backup-carried refusal A cannot answer hold B.
        for ordinal, bound in enumerate((False, True)):
            with self.subTest(bound=bound):
                if ordinal:
                    self.doCleanups()
                    self.setUp()
                _, pc, _ = self.build(issue=bound, design=False)
                if bound:
                    self.assertEqual(self.act().outcome, 'allow')
                self.cancel()
                first = self.issue(pc)
                self.assertEqual(self.act().code, 'authority.restore-held')
                self.release()
                self.assertNotEqual(self.act().code, 'authority.restore-held')
                self.cancel('job-b')
                self.assertNotEqual(self.act().code, 'authority.restore-held')
                second = self.issue(pc)
                answer = self.act()
                self.assertEqual(answer.code, 'authority.restore-held')
                self.assertIn('job-b', answer.reason)
                self.assertIn(second.relay_id, answer.reason)
                self.assertNotIn(first.relay_id, answer.reason)
                self.release()
                self.assertNotEqual(self.act().code, 'authority.restore-held')
                self.assertEqual(self.refusal_rows(), [(first.relay_id, 'job-a'), (second.relay_id, 'job-b')])

    def test_restore_held_catalogue(self):
        from relay_engine import errors, strings
        error = errors.error_for('E-AUTHORITY-RESTORE-HELD')
        self.assertEqual(error.cause, "a restore holds this project, so no planner's go-ahead is issued")
        self.assertEqual(error.remedy, 'ask the operator to end the restore hold at their terminal, then ask your planner for a new go-ahead')
        self.assertEqual(errors.explain_for('E-AUTHORITY-RESTORE-HELD')[0],
                         'a restore cancels every go-ahead and approval, and only the operator ends its hold')
        self.assertEqual(errors.ERRORS['E-AUTHORITY-RESTORE-HELD'].cls, 'command')
        self.assertIn('E-AUTHORITY-RESTORE-HELD', strings._CODE_VALUES)
