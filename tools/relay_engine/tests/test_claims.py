"""Claim verifications through real admission; only identity rows are fixtures."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock

from relay_engine import claims, errors, header, queries, submit
from relay_engine.tests.test_submit import Store, PLANNER, IMPL, ORCH, OTHER, SEATS, draft
from relay_engine.tests.test_refs import Repo, clean_git_environment, git

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


class BodyReader:
    """Pass every statement through, first trying a read of `bodies` and noting
    whether the active guard denied it; the reading variant for the no-body rule."""
    def __init__(self, conn, attempts):
        self.conn = conn
        self.attempts = attempts

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def set_authorizer(self, callback):
        self.conn.set_authorizer(callback)

    def execute(self, sql, parameters=()):
        try:
            self.conn.execute('SELECT body FROM bodies LIMIT 1').fetchall()
            self.attempts.append('read')
        except sqlite3.DatabaseError:
            self.attempts.append('denied')
        return self.conn.execute(sql, parameters)


class ClaimStore(Store):
    """Hand-built identity rows with claim/verification writes through admission.

    setUp has open pair teams in p/q, occupied p seats and p's reviewer; no
    assignments, waivers or NULL-reviewer claims until a test explicitly adds them.
    Tests add closed/stood-down reviewers, missing defaults, alternate chains,
    same-project/foreign assignments and operator waivers. q has no default reviewer.
    No real TableDirectory, live keys/key rotation, authenticated sender/session
    replacement, provider state or Platform lifecycle API is built here.
    """
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
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        observed = []
        original = claims.on_admitted_claims
        def record(conn, admitted, data, ctx):
            original(conn, admitted, data, ctx)
            observed.append((data.kind, data.verdict, conn.in_transaction,
                             self.request(admitted)))
        original_ctx = self.ctx
        self.ctx = lambda *args, **kwargs: replace(original_ctx(*args, **kwargs), after_admit=record)
        for kind in ('plan', 'design'):
            reviews = self.conn.execute("SELECT count(*) FROM relays WHERE kind = 'REVIEW'").fetchone()[0]
            _, reply = self.claim(kind)
            rows = self.conn.execute('SELECT DISTINCT request_relay_id, reviewer_seat_id, kind'
                                     ' FROM verification_requests WHERE claim_relay_id = ?',
                                     (reply.relay_id,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1:], (SECOND.seat_id, kind))
            self.assertIn((kind.upper() + '-REVIEW', 'approve', True, rows[0][:2]), observed)
            self.assertEqual(self.conn.execute('SELECT kind, needs FROM relays WHERE id = ?',
                                               (rows[0][0],)).fetchone(), ('REVIEW', 'review'))
            self.assertEqual(self.conn.execute("SELECT count(*) FROM relays WHERE kind = 'REVIEW'").fetchone()[0],
                             reviews + 1)
        before = self.conn.execute('SELECT count(*) FROM verification_requests').fetchone()[0]
        self.submit(draft())
        self.claim('design', verdict='must-revise')
        self.assertIn(('DESIGN-REVIEW', 'must-revise', True, None), observed)
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
        # Engine admits no reply from a seat other than the recorded reviewer, since the
        # request's only member is that reviewer (121938); one CC recipient row makes OTHER
        # a member, so its approve is filed through admission and must still not count.
        # A second claim's request makes OTHER a member through real engine admission.
        # Keep the writer's real transaction, guards and recipient resolution.
        file_system = submit.file_system_relay
        def with_member(conn, ctx, data):
            return file_system(conn, ctx, data.replace(b'NEEDS: review\n',
                               ('CC: ' + OTHER.address + '\nNEEDS: review\n').encode()))
        with mock.patch.object(submit, 'file_system_relay', side_effect=with_member):
            _, claim = self.claim()
        self.verify(claim, sender=OTHER)
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
        other = self.plan.with_name('second.md')
        other.write_bytes(b'second plan\n')
        request = self.submit(draft(kind='PLAN', needs='review', to=IMPL.address,
                                    refs=('file:' + str(self.plan), 'file:' + str(other))))
        reached = []
        self.conn.create_function('saw_first_claim_row', 0, lambda: reached.append(
            self.conn.execute('SELECT count(*) FROM verification_requests').fetchone()[0]))
        artifact = str(other).replace("'", "''")
        self.conn.execute("CREATE TEMP TRIGGER fail_second_claim BEFORE INSERT ON verification_requests "
                          "WHEN NEW.artifact = '" + artifact + "' BEGIN "
                          "SELECT saw_first_claim_row(); SELECT RAISE(ABORT, 'second claim insert'); END")
        self.conn.commit()
        tables = ('relays', 'bodies', 'recipients', 'edges', 'snapshots', 'verification_requests')
        before = {t: self.conn.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
        failure = None
        try:
            self.submit(draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                              refs=('file:' + str(self.plan), 'file:' + str(other))), IMPL)
        except errors.EngineError as error:
            failure = error
        self.assertIsNotNone(failure, 'a second-lock insert failure must refuse the whole reply')
        self.assertEqual(failure.code, 'E-ADMISSION-HOOK')
        self.assertEqual(reached, [1], 'failure was injected after the first lock row')
        after = {t: self.conn.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
        self.assertEqual(after, before)

    def test_assign_reviewer_project_scope(self):
        # GC-16: removing the seat-project predicate must accept this wrong seat.
        foreign = 'foreign-reviewer'
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES (?, 't9', 'orchestrator-reviewer', 'occupied')", (foreign,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            claims.assign_reviewer(self.conn, 't1', foreign, 'operator', self.clock)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM reviewer_assignments').fetchone()[0], 0)

    def test_assign_reviewer_refuses_seat_that_cannot_answer(self):
        # T5-M5: refusing at the call must leave the existing assignment intact.
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        before = self.conn.execute('SELECT * FROM reviewer_assignments').fetchall()
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            claims.assign_reviewer(self.conn, 't1', REVIEWER.seat_id, 'operator', self.clock)
        self.assertEqual(self.conn.execute('SELECT * FROM reviewer_assignments').fetchall(), before)
        self.conn.execute("UPDATE seats SET state = 'occupied' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.execute("UPDATE team_instances SET closed_at = 2 WHERE id = ?", (REVIEWER.team_instance_id,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            claims.assign_reviewer(self.conn, 't1', REVIEWER.seat_id, 'operator', self.clock)
        self.assertEqual(self.conn.execute('SELECT * FROM reviewer_assignments').fetchall(), before)
        claims.assign_reviewer(self.conn, 't1', IMPL.seat_id, 'operator', self.clock)
        self.assertEqual(self.conn.execute('SELECT seat_id FROM reviewer_assignments WHERE team_instance_id = ?',
                                           ('t1',)).fetchone()[0], IMPL.seat_id)

    def test_reviewer_for_scope(self):
        # GC-17: all candidates are off-chain; only the project's open one counts.
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at,"
                          " opened_by, closed_at)"
                          " VALUES ('closed-p', 'p', 'closed', 'orchestrator', 3, 'operator', 4)")
        for sid, tid in (('closed-reviewer', 'closed-p'), ('foreign-reviewer', 't9')):
            self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                              " VALUES (?, ?, 'orchestrator-reviewer', 'occupied')", (sid, tid))
        self.conn.execute("UPDATE team_instances SET opened_at = 5 WHERE id = 't9'")
        self.conn.commit()
        self.assertEqual(claims.reviewer_for(self.conn, 't1'), REVIEWER.seat_id)

    def test_request_reaches_recorded_reviewer(self):
        # RR-M2: an assigned closed seat must never route to a reopened namesake.
        claims.assign_reviewer(self.conn, 't1', REVIEWER.seat_id, 'operator', self.clock)
        self.conn.execute("UPDATE team_instances SET closed_at = 2 WHERE id = 't3'")
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                          " VALUES ('reopened', 'p', 'v3', 'orchestrator', 3, 'operator')")
        new = submit.SeatRef('reopened-reviewer', REVIEWER.address, REVIEWER.role, 'reopened', 'p')
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES (?, ?, ?, 'occupied')", (new.seat_id, new.team_instance_id, new.role))
        self.conn.commit()
        resolve = self.directory.resolve
        self.directory.resolve = lambda address, sender: new if address == REVIEWER.address else resolve(address, sender)
        _, claim = self.claim()
        self.assertEqual(self.request(claim), (None, None),
                         'closed assignment must record no request, not reach a reopened seat')
        self.assertEqual(self.state(claim), 'pending')
        self.assertIsNone(claims.reviewer_for(self.conn, 't1'),
                          'a closed team assignment must select no reviewer')
        claims.assign_reviewer(self.conn, 't1', new.seat_id, 'operator', self.clock)
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (new.seat_id,))
        self.conn.commit()
        self.assertIsNone(claims.reviewer_for(self.conn, 't1'),
                          'a stood-down assignment in an open team must select no reviewer')

    def test_default_reviewer_is_the_claim_teams(self):
        # RR-M3: newest project reviewer must not override the nearest ancestor.
        self.conn.execute("UPDATE team_instances SET parent_instance_id = 't3' WHERE id = 't1'")
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                          " VALUES ('other-orch', 'p', 'other-orch', 'orchestrator', 5, 'operator')")
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES ('other-orch-reviewer', 'other-orch', 'orchestrator-reviewer', 'occupied')")
        self.conn.commit()
        self.assertEqual(claims.reviewer_for(self.conn, 't1'), REVIEWER.seat_id)
        self.conn.execute("UPDATE team_instances SET parent_instance_id = NULL WHERE id = 't1'")
        self.conn.commit()
        self.assertIsNone(claims.reviewer_for(self.conn, 't1'), 'two off-chain reviewers give no default')
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.commit()
        self.assertEqual(claims.reviewer_for(self.conn, 't1'), 'other-orch-reviewer')
        self.conn.execute("UPDATE seats SET state = 'occupied' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.commit()
        claims.assign_reviewer(self.conn, 't1', REVIEWER.seat_id, 'operator', self.clock)
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.commit()
        self.assertIsNone(claims.reviewer_for(self.conn, 't1'), 'a stood-down assignment is no reviewer')

    def test_request_names_locked_bytes(self):
        # SF-M1: copying main instead of the resolved commit makes a stale request.
        clean_git_environment(self)
        repo = Repo({'plan.md': 'git plan X\n'})
        self.addCleanup(repo.close)
        self.directory.checkouts['bivpak'] = repo.path
        locked_commit = git(repo.path, 'rev-parse', 'HEAD')
        _, claim = self.claim(paths=(), bundle='unused')
        request = self.submit(draft(kind='PLAN', needs='review', to=IMPL.address,
                                    refs=('bivpak@main:plan.md',)))
        claim = self.submit(draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                                  refs=('bivpak@main:plan.md',)), IMPL)
        verification = self.request(claim)[0]
        git_edges = [edge for edge in queries.points_to(self.conn, verification)
                     if edge.target_type == 'git']
        body = self.conn.execute('SELECT body FROM bodies WHERE relay_id = ?', (verification,)).fetchone()[0].decode()
        self.assertEqual([edge.spelling for edge in git_edges],
                         ['bivpak@' + locked_commit + ':plan.md'],
                         'verification Git REF must name the locked commit, not main')
        for lock in claims.claim_locks(self.conn, claim.relay_id):
            self.assertIn(lock.content_sha256, body)
        repo.commit_files({'plan.md': 'git plan Y\n'})
        self.assertNotEqual(git(repo.path, 'rev-parse', 'HEAD'), locked_commit)
        self.assertEqual(git_edges[0].target_commit, locked_commit)
        self.assertEqual(git(repo.path, 'show', git_edges[0].target_rev + ':plan.md'), 'git plan X')

    def test_claims_read_no_bodies(self):
        _, claim = self.claim()
        self.assertEqual(self.state(claim), 'pending')
        self.verify(claim)
        attempts = []
        reader = BodyReader(self.conn, attempts)
        self.assertEqual(claims.verification_state(reader, claim.relay_id), 'approved')
        self.assertEqual(len(claims.claim_locks(reader, claim.relay_id)), 1)
        self.assertEqual(claims.reviewer_for(reader, 't1'), REVIEWER.seat_id)
        self.assertTrue(attempts)
        self.assertEqual(set(attempts), {'denied'})
        # Control: the same reading outside the readers' guard reaches the bodies.
        reader.execute('SELECT 1')
        self.assertEqual(attempts[-1], 'read')

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
        self.assertEqual(self.conn.execute('SELECT code FROM refusals ORDER BY id DESC LIMIT 1').fetchone(),
                         ('E-ADMISSION-HOOK',))
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


class TestRuleHClaims(ClaimStore):
    """Waiver frontier and two-query H reads, with real claim admissions."""

    def test_waive_frontier_reads_own_project(self):
        # W-PROJ: another project's higher handles do not set this frontier.
        from relay_engine.tests.test_submit import FOREIGN
        _, claim = self.claim()
        for _ in range(12):
            self.submit(draft(to=FOREIGN.address), FOREIGN)
        highest = {project: value for project, value in self.conn.execute(
            'SELECT project_id, max(handle) FROM relays GROUP BY project_id')}
        self.assertGreater(highest['q'], highest['p'])
        claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'own frontier', self.clock)
        recorded = self.conn.execute('SELECT after_handle FROM waivers WHERE claim_relay_id = ?',
                                     (claim.relay_id,)).fetchone()[0]
        self.assertEqual(recorded, highest['p'])

    def test_standing_objection_statement_count_many_claims(self):
        # H-PER-CLAIM: one lookup pair, even with several matching claims.
        admitted = [self.claim()[1] for _ in range(3)]
        claims.waive(self.conn, admitted[0].relay_id, 'plan', 'operator', 'waive one', self.clock)
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            claims.standing_objection(self.conn, 'p', str(self.plan),
                                      hashlib.sha256(self.plan.read_bytes()).hexdigest())
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 2, statements)

    def test_standing_objection_executes_the_constants(self):
        # SQL-INLINE: explain and execute must refer to the same statement text.
        statements = []
        class RecordingConnection:
            def __init__(self, conn):
                self.conn = conn
            @property
            def in_transaction(self):
                return self.conn.in_transaction
            def set_authorizer(self, callback):
                self.conn.set_authorizer(callback)
            def execute(self, sql, parameters=()):
                statements.append(sql)
                return self.conn.execute(sql, parameters)
        claims.standing_objection(RecordingConnection(self.conn), 'p', str(self.plan),
                                  hashlib.sha256(self.plan.read_bytes()).hexdigest())
        self.assertEqual(statements, [claims.STANDING_VERDICTS_SQL, claims.STANDING_WAIVERS_SQL])

    def test_rule_h_statements_use_bytes_index(self):
        # IDX-DROP: both actual statements have the artifact/content access path.
        for statement in (claims.STANDING_VERDICTS_SQL, claims.STANDING_WAIVERS_SQL):
            with self.subTest(statement=statement):
                plan = self.conn.execute('EXPLAIN QUERY PLAN ' + statement,
                                         ('p', 'a', 's')).fetchall()
                import re
                self.assertEqual(len(plan), 3, plan)
                rows = []
                for row in plan:
                    self.assertTrue(row[3].startswith('SEARCH '), plan)
                    found = re.match(r'^(?:SEARCH|SCAN) (?:TABLE )?(\w+) .*?INDEX (\w+)', row[3])
                    self.assertIsNotNone(found, plan)
                    rows.append(found.groups())
                self.assertEqual(rows[0], ('v', 'verification_requests_bytes'), plan)
                if statement == claims.STANDING_VERDICTS_SQL:
                    self.assertEqual([alias for alias, _ in rows], ['v', 'c', 'r'], plan)
                    self.assertEqual(rows[2], ('r', 'relays_parent'), plan)

    def test_waive_records_after_handle(self):
        # An older wall-clock value must not replace the relay frontier.
        _, claim = self.claim()
        self.submit(draft(kind='SITREP', to=IMPL.address))
        highest = self.conn.execute('SELECT max(handle) FROM relays WHERE project_id = ?',
                                    ('p',)).fetchone()[0]
        claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'waive', self.clock)
        recorded = self.conn.execute('SELECT after_handle FROM waivers WHERE claim_relay_id = ?',
                                     (claim.relay_id,)).fetchone()[0]
        self.assertEqual(recorded, highest)

    def test_standing_objection_statement_count(self):
        # Count both H queries and ensure the frontier read is in the write TX.
        _, claim = self.claim()
        trace = []
        self.conn.set_trace_callback(trace.append)
        try:
            claims.standing_objection(self.conn, 'p', str(self.plan),
                                      hashlib.sha256(self.plan.read_bytes()).hexdigest())
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(trace), 2, trace)
        self.assertIn('verification_requests', trace[0])
        self.assertIn('waivers', trace[1])
        trace = []
        self.conn.set_trace_callback(trace.append)
        try:
            claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'waive', self.clock)
        finally:
            self.conn.set_trace_callback(None)
        begin = trace.index('BEGIN IMMEDIATE')
        end = trace.index('COMMIT')
        reads = [i for i, statement in enumerate(trace) if 'max(handle)' in statement.lower()]
        self.assertEqual(len(reads), 1, trace)
        self.assertLess(begin, reads[0], trace)
        self.assertLess(reads[0], end, trace)
