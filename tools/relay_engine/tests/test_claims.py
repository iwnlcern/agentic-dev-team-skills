"""Claim verifications through real admission; only identity rows are fixtures."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock

from relay_engine import claims, errors, header, queries, submit
from relay_engine.tests.test_submit import Store, PLANNER, IMPL, ORCH, OTHER, SEATS, draft

REVIEWER = submit.SeatRef('seat-reviewer', 'v3.orchestrator-reviewer',
                          'orchestrator-reviewer', 't3', 'p')
SECOND = submit.SeatRef('seat-reviewer-2', 'v3-other.implementer', 'implementer', 't2', 'p')


class CommitFault:
    """Keep real writes and guards, but fail the writer's final COMMIT."""
    def __init__(self, conn):
        self.conn = conn

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def set_authorizer(self, callback):
        self.conn.set_authorizer(callback)

    def execute(self, sql, parameters=()):
        if sql == 'COMMIT':
            raise sqlite3.DatabaseError('injected commit fault after writes')
        return self.conn.execute(sql, parameters)


class ClaimStore(Store):
    def setUp(self):
        super().setUp()
        for seat in (REVIEWER, SECOND):
            self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                              " VALUES (?, ?, ?, 'occupied')",
                              (seat.seat_id, seat.team_instance_id, seat.role))
            self.conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier,"
                              " issued_at) VALUES (?, ?, ?, 'v', 1)",
                              ('o-' + seat.seat_id, seat.seat_id, 'k-' + seat.seat_id))
        self.conn.commit()
        original = self.directory.resolve
        self.directory.resolve = lambda address, sender: {
            REVIEWER.address: REVIEWER, SECOND.address: SECOND
        }.get(address) or original(address, sender)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = Path(self.temp.name).resolve() / 'plan.md'
        self.design = self.plan.with_name('design.md')
        self.plan.write_bytes(b'plan X\n')
        self.design.write_bytes(b'design X\n')

    def ctx(self, sender=PLANNER, **kwargs):
        return replace(super().ctx(sender, **kwargs), after_admit=claims.on_admitted_claims,
                       after_admit_writes=claims.CLAIM_WRITES)

    def claim(self, kind='plan', paths=None, bundle='b1', parent=None, verdict='approve'):
        paths = (self.plan if kind == 'plan' else self.design,) if paths is None else paths
        refs = tuple('file:' + str(p) for p in paths)
        request = self.submit(draft(kind=kind.upper(), needs='review', to=IMPL.address,
                                    refs=refs, bundle=bundle, parent=parent))
        reply = self.submit(draft(kind=kind.upper() + '-REVIEW', verdict=verdict,
                                  to=PLANNER.address, parent=request.relay_id, refs=refs), IMPL)
        return request, reply

    def request(self, claim):
        return self.conn.execute('SELECT DISTINCT request_relay_id, reviewer_seat_id'
                                 ' FROM verification_requests WHERE claim_relay_id = ?',
                                 (claim.relay_id,)).fetchone()

    def verify(self, claim, verdict='approve', sender=REVIEWER):
        request = self.request(claim)
        return self.submit(draft(kind='REVIEW', parent=request[0], verdict=verdict,
                                 to=PLANNER.address), sender)

    def state(self, claim):
        with submit.read_guard(self.conn):
            return claims.verification_state(self.conn, claim.relay_id)


class TestClaims(ClaimStore):
    def test_triggers(self):
        for kind in ('plan', 'design'):
            _, reply = self.claim(kind)
            self.assertEqual(self.request(reply)[1], REVIEWER.seat_id)
        before = self.conn.execute('SELECT count(*) FROM verification_requests').fetchone()[0]
        self.submit(draft())
        self.claim('design', verdict='must-revise')
        self.claim(paths=())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM verification_requests').fetchone()[0], before)

    def test_request_names_checks(self):
        _, reply = self.claim()
        request = self.request(reply)[0]
        refs = queries.points_to(self.conn, request)
        self.assertIn(reply.relay_id, [edge.target_relay_id for edge in refs])
        self.assertIn(str(self.plan), [edge.target_path for edge in refs])
        body = self.conn.execute('SELECT body FROM bodies WHERE relay_id = ?', (request,)).fetchone()[0].decode()
        for check in claims.CLAIM_CHECKS['plan']:
            self.assertIn(check, body)
        self.assertIn('independent', body)

    def test_claim_records_lock_identity(self):
        self.claim()
        self.plan.write_bytes(b'plan Y\n')
        request, reply = self.claim()
        with submit.read_guard(self.conn):
            locks = claims.claim_locks(self.conn, reply.relay_id)
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        self.assertEqual((lock.artifact, lock.request_relay_id, lock.content_sha256),
                         (str(self.plan), request.relay_id, hashlib.sha256(b'plan Y\n').hexdigest()))
        snapshot = self.conn.execute('SELECT handle FROM snapshots WHERE locked_by = ?',
                                     (reply.relay_id,)).fetchone()[0]
        self.assertEqual(lock.snapshot_handle, snapshot)

    def test_multi_artifact_claim(self):
        other = self.plan.with_name('second.md')
        other.write_bytes(b'second plan\n')
        _, claim = self.claim(paths=(self.plan, other))
        rows = self.conn.execute('SELECT request_relay_id FROM verification_requests WHERE claim_relay_id = ?',
                                  (claim.relay_id,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(set(rows)), 1)
        refs = queries.points_to(self.conn, rows[0][0])
        self.assertEqual({edge.target_path for edge in refs if edge.target_type == 'path'},
                         {str(self.plan), str(other)})
        for verdict, state in ((None, 'pending'), ('must-revise', 'objected'), ('approve', 'approved')):
            if verdict:
                self.verify(claim, verdict)
            self.assertEqual(self.state(claim), state)
        request = self.submit(draft(kind='PLAN', needs='review', to=IMPL.address,
                                   refs=('file:' + str(self.plan), 'file:' + str(other))))
        data = draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                     refs=('file:' + str(self.plan), 'file:' + str(other)))
        ctx = self.ctx(IMPL)
        admitted = submit.submit(self.conn, ctx, data)
        before = self.conn.total_changes
        self.assertEqual(submit.submit(self.conn, ctx, data), admitted)
        self.assertEqual(self.conn.total_changes, before)
        before = self.counts()
        original = claims.on_admitted_claims
        def fault(conn, admitted, draft, ctx):
            original(conn, admitted, draft, ctx)
            raise RuntimeError('after filing')
        with self.assertRaises(errors.EngineError):
            submit.submit(self.conn, replace(self.ctx(IMPL), after_admit=fault), data)
        after = self.counts()
        after.pop('refusals'); before.pop('refusals')
        self.assertEqual(after, before)

    def test_states(self):
        _, claim = self.claim()
        with self.assertRaises(errors.EngineError) as refused:
            self.verify(claim, sender=OTHER)
        self.assertEqual(refused.exception.code, 'E-PARENT-NOT-MEMBER')
        self.assertEqual(self.request(claim)[1], REVIEWER.seat_id)
        self.assertEqual(self.state(claim), 'pending')
        self.verify(claim)
        self.assertEqual(self.state(claim), 'approved')
        self.verify(claim, 'must-revise')
        self.assertEqual(self.state(claim), 'objected')
        claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'skip verification', self.clock)
        self.assertEqual(self.state(claim), 'waived')

    def test_reassignment_keeps_pending(self):
        _, claim = self.claim()
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        self.assertEqual(self.request(claim)[1], REVIEWER.seat_id)
        self.assertEqual(self.state(claim), 'pending')
        with self.assertRaises(errors.EngineError) as refused:
            self.verify(claim, sender=SECOND)
        self.assertEqual(refused.exception.code, 'E-PARENT-NOT-MEMBER')
        self.assertEqual(self.request(claim)[1], REVIEWER.seat_id)
        self.assertEqual(self.state(claim), 'pending')
        self.verify(claim)
        self.assertEqual(self.state(claim), 'approved')

    def test_default_reviewer(self):
        with submit.read_guard(self.conn):
            self.assertEqual(claims.reviewer_for(self.conn, 't1'), REVIEWER.seat_id)

    def test_reassign_routes_next(self):
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        _, claim = self.claim()
        self.assertEqual(self.request(claim)[1], SECOND.seat_id)
        self.verify(claim, sender=SECOND)
        self.assertEqual(self.state(claim), 'approved')

    def test_waive_and_assign_operator_only(self):
        _, claim = self.claim()
        for call in (lambda: claims.waive(self.conn, claim.relay_id, 'plan', PLANNER.seat_id, 'skip', self.clock),
                     lambda: claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, PLANNER.seat_id, self.clock)):
            before = self.conn.total_changes
            with self.assertRaises(ValueError):
                call()
            self.assertEqual(self.conn.total_changes, before)
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'skip', self.clock)
        self.assertEqual(self.state(claim), 'waived')

    def test_claim_filing_is_atomic(self):
        self.test_multi_artifact_claim()

    def test_claims_read_no_bodies(self):
        _, claim = self.claim()
        self.assertEqual(self.state(claim), 'pending')
        self.verify(claim)
        self.assertEqual(self.state(claim), 'approved')
        with submit.read_guard(self.conn), self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute('SELECT body FROM bodies').fetchall()

    def test_own_filing_refusal_is_a_hook_fault(self):
        req = self.submit(draft(kind='PLAN', needs='review', to=IMPL.address,
                                refs=('file:' + str(self.plan),)))
        data = draft(kind='PLAN-REVIEW', verdict='approve', parent=req.relay_id,
                     refs=('file:' + str(self.plan),))
        before = self.counts()
        with mock.patch.object(submit, 'file_system_relay', side_effect=
                               errors.error_for('E-SNAPSHOT-NO-BYTES', handle=991, reason='no evidence store was configured when the request was submitted')):
            with self.assertRaises(errors.EngineError) as caught:
                self.submit(data, IMPL)
        self.assertEqual(caught.exception.code, 'E-ADMISSION-HOOK')
        self.assertEqual(caught.exception.params['kind'], 'AuthorityFilingFault')
        self.assertNotIn('handle', caught.exception.params)
        self.assertNotIn('reason', caught.exception.params)
        self.assertNotIn('no evidence store was configured', str(caught.exception.params))
        self.assertNotIn('991', str(caught.exception.params))
        after = self.counts()
        self.assertEqual(after.pop('refusals'), before.pop('refusals') + 1)
        self.assertEqual(after, before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM verification_requests').fetchone()[0], 0)
        self.assertIsNotNone(self.submit(data, IMPL))

    def test_writers_own_their_transaction(self):
        _, claim = self.claim()
        calls = (lambda: claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'skip', self.clock),
                 lambda: claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock))
        for call, table in zip(calls, ('waivers', 'reviewer_assignments')):
            self.conn.execute('BEGIN IMMEDIATE')
            before = self.conn.total_changes
            with self.assertRaises(ValueError):
                call()
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.total_changes, before)
            self.conn.rollback()
            # The proxy keeps the actual transaction and first write intact.
            with self.assertRaises(sqlite3.DatabaseError):
                if table == 'waivers':
                    claims.waive(CommitFault(self.conn), claim.relay_id, 'plan', 'operator', 'skip', self.clock)
                else:
                    claims.assign_reviewer(CommitFault(self.conn), 't1', SECOND.seat_id, 'operator', self.clock)
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
            call()
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
