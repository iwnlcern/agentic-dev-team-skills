import dataclasses
import importlib
import os
import unittest
from contextlib import ExitStack
from unittest import mock

from relay_engine.guardrail import EFFECT_CLASSES


class TestValues(unittest.TestCase):
    def values(self):
        try:
            return importlib.import_module("relay_engine.slips")
        except ModuleNotFoundError as error:
            if error.name != "relay_engine.slips":
                raise
            self.fail("reviewed Answer/Effect constructors are not implemented")

    def test_answer_valid_outcomes(self):
        Answer = self.values().Answer
        self.assertEqual(tuple(f.name for f in dataclasses.fields(Answer)),
                         ("outcome", "code", "reason", "unblocker", "slip_id", "revision"))
        cases = [("allow", None, None, None, None, None),
                 ("allow", None, "reviewed", "operator", "fake-slip", 0),
                 ("deny", "authority.slip-missing", "No slip", "fake.planner", None, None),
                 ("held", "authority.plan-changed", "Changed plan", "fake.planner", "fake-slip", 3)]
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertEqual(dataclasses.astuple(Answer(*fields)), fields)

    def test_answer_unknown_outcome_rejected(self):
        Answer = self.values().Answer
        for outcome in ("unknown", "", None, [], 1):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                Answer(outcome, None, None, None, None, None)
        for outcome in ("allow", "deny", "held"):
            code = None if outcome == "allow" else "authority.synthetic"
            self.assertEqual(Answer(outcome, code, "reason", "operator", None, None).outcome, outcome)

    def test_answer_nonallow_details_required(self):
        Answer = self.values().Answer
        for outcome in ("deny", "held"):
            valid = [outcome, "authority.synthetic", "reason", "operator", None, None]
            self.assertEqual(dataclasses.astuple(Answer(*valid)), tuple(valid))
            for index in (1, 2, 3):
                for bad in (None, "", 1, []):
                    fields = valid.copy()
                    fields[index] = bad
                    with self.subTest(outcome=outcome, index=index, bad=bad), self.assertRaises(ValueError):
                        Answer(*fields)
        for code in ("authority.synthetic", "", 1):
            with self.subTest(code=code), self.assertRaises(ValueError):
                Answer("allow", code, None, None, None, None)

    def test_answer_slip_identity_is_paired(self):
        Answer = self.values().Answer
        for identity in ((None, 1), ("fake-slip", None), ("", 1), (4, 1),
                         ("fake-slip", "1"), ("fake-slip", 1.5), ("fake-slip", True)):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                Answer("allow", None, None, None, *identity)
        for identity in ((None, None), ("fake-slip", -2), ("fake-slip", 0), ("fake-slip", 7)):
            for outcome in ("allow", "deny", "held"):
                code = None if outcome == "allow" else "authority.synthetic"
                answer = Answer(outcome, code, "reason", "operator", *identity)
                self.assertEqual((answer.slip_id, answer.revision), identity)

    def test_effect_closed_kinds(self):
        Effect = self.values().Effect
        for kind in EFFECT_CLASSES:
            self.assertEqual(Effect(kind, None).kind, kind)
        for kind in ("unknown", "", None, [], 1):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                Effect(kind, None)

    def test_effect_optional_context_is_passive(self):
        Effect = self.values().Effect
        self.assertEqual(tuple(f.name for f in dataclasses.fields(Effect)),
                         ("kind", "repository_id", "ref", "pr", "head", "path"))
        self.assertEqual(dataclasses.astuple(Effect("commit", None)),
                         ("commit", None, None, None, None, None))
        fields = ("pr-merge", "fake repository", "refs/heads/fake", -1, "opaque head", "/fake/missing")
        self.assertEqual(dataclasses.astuple(Effect(*fields)), fields)
        self.assertEqual(dataclasses.astuple(Effect("commit", "")),
                         ("commit", "", None, None, None, None))

    def test_value_objects_are_immutable(self):
        values = self.values()
        for cls, args, field, replacement in (
                (values.Answer, ("allow", None, None, None, None, None), "reason", "changed"),
                (values.Effect, ("commit", None), "path", "/fake/changed")):
            first, second = cls(*args), cls(*args)
            self.assertIsNot(first, second)
            self.assertEqual(first, second)
            changed = dataclasses.replace(first, **{field: replacement})
            self.assertNotEqual(first, changed)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(first, field, replacement)
            self.assertEqual(first, second)

    def test_constructors_do_no_io(self):
        values = self.values()  # Ordinary import is outside the constructor guard.

        class NoEnvironment(dict):
            def forbidden(self, *args, **kwargs):
                raise AssertionError("constructor read environment")
            __getitem__ = __contains__ = __iter__ = __len__ = get = copy = forbidden

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "environ", NoEnvironment()))
            for target in ("builtins.open", "io.open", "os.open", "os.fdopen", "os.read",
                           "os.write", "os.close", "os.stat", "os.lstat", "os.fstat", "os.getenv",
                           "os.system", "os.popen", "subprocess.run", "subprocess.Popen",
                           "time.time", "time.monotonic", "time.perf_counter"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("constructor performed I/O")))
            answer = values.Answer("held", "authority.synthetic", "reason", "operator", "fake-slip", 2)
            effect = values.Effect("commit", "fake-repository", path="/fake/missing")
            with self.assertRaises(ValueError):
                values.Answer("unknown", None, None, None, None, None)
            with self.assertRaises(ValueError):
                values.Effect("unknown", None)
        self.assertEqual(dataclasses.astuple(answer),
                         ("held", "authority.synthetic", "reason", "operator", "fake-slip", 2))
        self.assertEqual(dataclasses.astuple(effect),
                         ("commit", "fake-repository", None, None, None, "/fake/missing"))

# Query-level issuance fixtures: every approval and slip comes from submit.
import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from relay_engine import claims, errors, header, queries, slips, submit
from relay_engine import addresses
from relay_engine.tests.test_claims import BodyReader, ClaimStore, CommitFault, REVIEWER, SECOND
from relay_engine.tests.test_submit import PLANNER, IMPL, ORCH, OTHER, FOREIGN, GIT_STATE, draft
from relay_engine.tests.test_refs import Repo, clean_git_environment, git

MASTER = submit.SeatRef('seat-master', 'v3-master.master-planner', 'master-planner', 'tm', 'p')
UNRELATED = submit.SeatRef('seat-master-other', 'v3-unrelated.master-planner', 'master-planner', 'tu', 'p')
QIMPL = submit.SeatRef('seat-q-impl', 'v3-engine.implementer@q', 'implementer', 't9', 'q')
QREVIEWER = submit.SeatRef('seat-q-reviewer', 'v3.orchestrator-reviewer@q', 'orchestrator-reviewer', 'tq', 'q')


def with_repo(text: str | bytes, name: str = 'bivpak') -> str | bytes:
    encoded = isinstance(text, bytes)
    lines = (text.decode('utf-8') if encoded else text).splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith('NEEDS:'):
            lines.insert(index + 1, 'REPO: ' + name + '\n')
            result = ''.join(lines)
            return result.encode('utf-8') if encoded else result
    raise ValueError('draft has no NEEDS line')


class SlipStore(ClaimStore):
    """Admission-backed claims/slips over hand-built project, team and seat rows.

    Base rows add master and unrelated teams, q's reviewer and a second repository.
    Subclass tests add missing/closed/stood-down reviewers, assignments and waivers.
    No real TableDirectory, authenticated key rotation, session replacement,
    provider state, or Platform lifecycle admission is built by this fixture.
    """
    def setUp(self):
        super().setUp()
        for tid, project, name, level in [('tm', 'p', 'v3-master', 'master'),
                                           ('tu', 'p', 'v3-unrelated', 'master'),
                                           ('tq', 'q', 'v3', 'orchestrator')]:
            self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                              " VALUES (?, ?, ?, ?, 1, 'operator')", (tid, project, name, level))
        self.conn.execute("UPDATE team_instances SET parent_instance_id = 'tm' WHERE id = 't1'")
        for seat in (MASTER, UNRELATED, QIMPL, QREVIEWER):
            self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state) VALUES (?, ?, ?, 'occupied')",
                              (seat.seat_id, seat.team_instance_id, seat.role))
            self.conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                              " VALUES (?, ?, ?, 'v', 1)", ('o-' + seat.seat_id, seat.seat_id, 'k-' + seat.seat_id))
        self.conn.execute("INSERT INTO repositories (id, project_id, name) VALUES ('repo-2', 'p', 'other')")
        self.conn.commit()
        original = self.directory.resolve
        def resolve(address, sender):
            if sender.project_id == 'q':
                candidates = (FOREIGN, QIMPL, QREVIEWER)
                for seat in candidates:
                    if address in (seat.address, seat.address.partition('@')[0]):
                        return seat
            for seat in (MASTER, UNRELATED, QIMPL, QREVIEWER):
                if address == seat.address:
                    return seat
            return original(address, sender)
        self.directory.resolve = resolve
        self.issuers = ', '.join(s.address for s in (ORCH, OTHER, MASTER, UNRELATED))

    def ctx(self, sender=PLANNER, **kwargs):
        return replace(super().ctx(sender, **kwargs), after_admit=slips.on_admitted,
                       after_admit_writes=slips.AFTER_ADMIT_WRITES,
                       after_admit_reads_denied=slips.AFTER_ADMIT_READS_DENIED)

    def approved(self, kind, parent=None, refs=None, bundle='b1'):
        path = self.plan if kind == 'plan' else self.design
        refs = ('file:' + str(path),) if refs is None else refs
        request = self.submit(draft(kind=kind.upper(), needs='review', to=IMPL.address,
                                    refs=refs, parent=parent, bundle=bundle))
        claim = self.submit(draft(kind=kind.upper() + '-REVIEW', verdict='approve',
                                  parent=request.relay_id, to=PLANNER.address,
                                  cc=self.issuers, refs=refs), IMPL)
        return claim

    def build(self, bundle='b1', issuer=PLANNER, delegation=True, design=True,
              verified=True, issue=True, prefix=None):
        parent = prefix
        if delegation:
            go = self.submit(draft(kind='PLAN', needs='act', to=PLANNER.address,
                                   cc=IMPL.address, bundle=bundle, parent=parent), ORCH)
            parent = go.relay_id
        dc = self.approved('design', parent=parent, bundle=bundle) if design else None
        pc = self.approved('plan', parent=dc.relay_id if dc else parent, bundle=bundle)
        if verified:
            for claim in (dc, pc):
                if claim:
                    self.verify(claim)
        issued = self.issue(pc, issuer) if issue else None
        return dc, pc, issued

    def issue(self, parent, issuer=PLANNER, refs=(), repo: str = 'bivpak', **kwargs):
        return self.submit(with_repo(draft(kind='IMPL', needs='act', parent=parent.relay_id,
                                 to=IMPL.address, refs=refs), repo), issuer, **kwargs)

    def slip(self, relay):
        return self.conn.execute('SELECT id FROM slips WHERE issued_in = ?', (relay.relay_id,)).fetchone()

    def issued(self, relay):
        row = self.slip(relay)
        self.assertIsNotNone(row, 'no slip issued in %s' % relay.relay_id)
        return row[0]

    def binds(self, relay):
        row = self.conn.execute('SELECT binds FROM slips WHERE issued_in = ?', (relay.relay_id,)).fetchone()
        self.assertIsNotNone(row, 'no slip issued in %s' % relay.relay_id)
        return json.loads(row[0])

    def claim_artifacts(self, relay, kind=None):
        return {(claim['kind'], claim['claim']): sorted(lock['artifact'] for lock in claim['locks'])
                for claim in self.binds(relay)['claims'] if kind in (None, claim['kind'])}

    def digest_artifacts(self, relay):
        return {bound['artifact']: bound['digest'] for bound in self.binds(relay).get('digests', [])}

    def go(self, bundle='b1', refs=()):
        return self.submit(draft(kind='PLAN', needs='act', to=PLANNER.address, cc=IMPL.address,
                                 bundle=bundle, refs=refs), ORCH)

    def ask(self, kind, parent, *paths):
        return self.submit(draft(kind=kind.upper(), needs='review', to=IMPL.address, parent=parent.relay_id,
                                 refs=tuple('file:' + str(path) for path in paths)))

    def answer(self, request, verdict, *paths, kind='plan', cc=None):
        return self.submit(draft(kind=kind.upper() + '-REVIEW', verdict=verdict, parent=request.relay_id,
                                 to=PLANNER.address, cc=cc or self.issuers,
                                 refs=tuple('file:' + str(path) for path in paths)), IMPL)

    def designed(self):
        go = self.go()
        dc = self.approved('design', parent=go.relay_id)
        self.verify(dc)
        return go, dc

    def act(self, seat=IMPL, repo='repo-1', branch='work', digest=None, kind='commit', path=None):
        def read(file, algorithm='sha256'):
            try:
                data = Path(file).read_bytes()
            except OSError:
                return None
            def hashed(name):
                if name == 'sha256':
                    return hashlib.sha256(data).hexdigest()
                blob = b'blob %d\0' % len(data) + data
                return {'git-sha1': hashlib.sha1, 'git-sha256': hashlib.sha256}[name](blob).hexdigest()
            return tuple(hashed(name) for name in algorithm) if isinstance(algorithm, tuple) else hashed(algorithm)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            with submit.read_guard(self.conn):
                answer = slips.act(self.conn, seat.seat_id, slips.Effect(kind, repo, branch, path=path),
                                   self.clock, digest=digest or read)
            self.conn.commit()
            return answer
        except BaseException:
            self.conn.rollback()
            raise

    def code(self, code, **kwargs):
        answer = self.act(**kwargs)
        self.assertEqual(answer.code, 'authority.' + code)
        self.assertTrue(answer.reason)
        self.assertTrue(answer.unblocker)
        return answer


class TestStanding(SlipStore):
    def standing(self, conn=None):
        return slips.standing(self.conn if conn is None else conn, IMPL.seat_id, self.clock)

    def test_standing_answer(self):
        _, _, old = self.build(bundle='replaced', design=False)
        _, _, revoked = self.build(bundle='revoked', design=False)
        self.assertEqual(slips.revoke(self.conn, self.issued(revoked), 'operator', 'stop',
                                     self.clock).outcome, 'allow')
        _, _, live = self.build(bundle='replaced', design=False)
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        other = self.submit(with_repo(draft(kind='IMPL', needs='act', bundle='another',
                                           to=SECOND.address)), operator)
        self.issued(other)
        self.assertEqual(self.conn.execute('SELECT state FROM slips WHERE id = ?',
                                          (self.issued(old),)).fetchone(), ('ended',))
        answer = self.standing()
        self.assertEqual([(slip.slip_id, slip.state, slip.kind, slip.repository_id)
                          for slip in answer.slips],
                         [(self.issued(live), 'live', 'implement', 'repo-1')])
        self.assertIsNone(answer.hold)

    def test_standing_empty_seat(self):
        answer = self.standing()
        self.assertEqual((answer.slips, answer.hold), ((), None))

    def test_standing_claim_hold_matches_act(self):
        dc, pc, _ = self.build(verified=False)
        self.verify(dc)
        pending = self.code('verification-pending')
        self.assertEqual(self.standing().slips[0].held, pending)
        self.verify(pc)
        self.assertIsNone(self.standing().slips[0].held)
        self.assertEqual(self.act().outcome, 'allow')

    def test_standing_lineage_hold(self):
        """Construct loss of delegation after issuance using only a lineage read double.

        Ordinary submissions do not remove delegation, and the authority-row
        fixture guard forbids editing its recipients directly.
        """
        delegation = self.go()
        plan = self.approved('plan', parent=delegation.relay_id)
        self.verify(plan)
        issued = self.issue(plan)
        sid = self.issued(issued)
        before = self.standing().slips
        self.assertEqual([(entry.slip_id, entry.state) for entry in before], [(sid, 'live')])
        self.assertIsNone(before[0].held)

        # Change only the observed delegation in this issuance's real walk.
        real_lineage = queries.lineage

        def without_delegation(conn, relay_id, *args, **kwargs):
            rows = real_lineage(conn, relay_id, *args, **kwargs)
            if relay_id == issued.relay_id:
                return [replace(row, needs='read') if row.id == delegation.relay_id else row
                        for row in rows]
            return rows

        with mock.patch.object(queries, 'lineage', side_effect=without_delegation):
            entry, = self.standing().slips
            self.assertEqual((entry.slip_id, entry.state), (sid, 'live'))
            self.assertIsNotNone(entry.held, 'expected a lineage hold after delegation is removed')
            self.assertEqual(entry.held, self.code('lineage-incomplete'))

    def test_standing_restore_hold_not_empty(self):
        from relay_engine import restore_cancel

        self.build()
        restore_cancel.cancel_all(self.conn, 'p', 'fixture-job-7', self.clock)
        answer = self.standing()
        self.assertEqual(answer.slips, ())
        self.assertIsNotNone(answer.hold)
        self.assertEqual((answer.hold.outcome, answer.hold.code, answer.hold.unblocker),
                         ('held', 'authority.restore-held', 'operator'))
        self.assertIn('fixture-job-7', answer.hold.reason)

    def test_act_path_mismatch_outranks_pending_claim(self):
        # Preservation: this precedence already holds before standing exists.
        self.build(verified=False)
        self.code('verification-pending')
        self.plan.write_bytes(b'changed plan\n')
        self.code('plan-changed')

    def test_standing_fault_raises_unavailable(self):
        class BrokenRead(CommitFault):
            def execute(self, sql, parameters=()):
                if 'FROM slips' in sql:
                    raise sqlite3.DatabaseError('read fault')
                return self.conn.execute(sql, parameters)

        with self.assertRaises(getattr(slips, 'QueryUnavailable', RuntimeError)):
            self.standing(BrokenRead(self.conn))


class TestScope(SlipStore):
    def scope(self, repository='repo-1', **target):
        return slips.scope(self.conn, IMPL.seat_id, repository, **target)

    def test_scope_ref_bound_to_seat(self):
        _, _, older = self.build(bundle='older', design=False)
        self.assertEqual(self.act(branch='older').slip_id, self.issued(older))
        _, _, newer = self.build(bundle='newer', design=False)
        self.issued(newer)
        self.assertEqual(self.scope(ref='older'), (self.issued(older), 1))

    def test_scope_matches_act_binding(self):
        _, _, older = self.build(bundle='older', design=False)
        _, _, newer = self.build(bundle='newer', design=False)
        self.issued(older)
        covered = self.scope(ref='not-yet-bound')
        self.assertEqual(covered, (self.issued(newer), 1))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM branch_bindings WHERE branch = 'not-yet-bound'").fetchone())
        answer = self.act(branch='not-yet-bound')
        self.assertEqual((answer.slip_id, answer.revision), covered)

    def test_scope_ref_bound_to_other_seat(self):
        _, _, ours = self.build(bundle='shared', design=False)
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        theirs = self.submit(with_repo(draft(kind='IMPL', needs='act', bundle='shared',
                                            to=SECOND.address)), operator)
        self.assertEqual(self.act(seat=SECOND, branch='theirs').slip_id, self.issued(theirs))
        self.assertEqual(self.scope(ref='open'), (self.issued(ours), 1))
        self.assertIsNone(self.scope(ref='theirs'))

    def test_scope_path_in_repository(self):
        _, _, issued = self.build()
        self.assertEqual(self.scope(path='src/sample.py'), (self.issued(issued), 1))
        self.assertIsNone(self.scope('repo-2', path='src/sample.py'))

    def test_scope_rejects_unsafe_paths(self):
        self.build()
        for path in ('/src/file', 'src/../file', ''):
            with self.subTest(path=path):
                self.assertIsNone(self.scope(path=path))

    def test_scope_needs_exactly_one_target(self):
        with self.assertRaises(ValueError):
            self.scope()
        with self.assertRaises(ValueError):
            self.scope(ref='work', path='src/file')

    def test_scope_ignores_ended_slips(self):
        _, _, issued = self.build()
        self.assertEqual(slips.revoke(self.conn, self.issued(issued), 'operator', 'stop',
                                     self.clock).outcome, 'allow')
        self.assertIsNone(self.scope(ref='work'))
        self.assertIsNone(self.scope(path='src/file'))


class TestQueries(SlipStore):
    def test_planner_issues_after_approved_plan(self):
        _, _, impl = self.build()
        answer = self.act()
        self.assertEqual(answer.outcome, 'allow')
        self.assertEqual(answer.slip_id, self.slip(impl)[0])

    def test_verification_pending_holds(self):
        dc, pc, impl = self.build(verified=False)
        self.verify(dc)
        pending = self.code('verification-pending')
        self.assertIn('reviewer', pending.unblocker)
        self.verify(pc)
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, pending.slip_id)
        self.assertEqual(self.slip(impl)[0], pending.slip_id)

    def test_issuer_lineage(self):
        self.build(delegation=False)
        self.code('lineage-incomplete')
        request = self.submit(draft(to=ORCH.address, bundle='empty'))
        impl = self.issue(request, ORCH)
        self.assertIsNone(self.slip(impl))
        self.code('lineage-incomplete')
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        impl = self.submit(with_repo(draft(kind='IMPL', needs='act', to=IMPL.address, bundle='operator')), operator)
        row = self.conn.execute('SELECT binds FROM slips WHERE issued_in = ?', (impl.relay_id,)).fetchone()
        self.assertTrue(json.loads(row[0])['waived'])
        self.assertEqual(self.act().outcome, 'allow')

    def test_master_planner_issues(self):
        _, pc, _ = self.build(issue=False)
        valid = self.issue(pc, MASTER)
        self.assertEqual(self.act().slip_id, self.slip(valid)[0])
        invalid = self.issue(pc, UNRELATED)
        self.assertIsNone(self.slip(invalid))
        slips.revoke(self.conn, self.slip(valid)[0], 'operator', 'stop', self.clock)
        # A fresh fixture without an existing slip shows the failed issuer diagnostic.
        self.conn.close()
        self.setUp()
        _, pc, _ = self.build(issue=False)
        self.issue(pc, UNRELATED)
        self.code('not-issuer')

    def test_plan_changed_holds(self):
        self.build()
        self.plan.write_bytes(b'changed\n')
        changed = self.code('plan-changed')
        self.assertIn('planner', changed.unblocker)
        self.plan.write_bytes(b'plan X\n')
        self.assertEqual(self.act().outcome, 'allow')
        self.design.write_bytes(b'changed\n')
        self.code('design-changed')

    def test_missing_working_copy_holds(self):
        self.build()
        self.plan.unlink()
        self.assertIn(str(self.plan), self.code('plan-changed').reason)

    def test_design_verification_gates(self):
        dc, pc, _ = self.build(verified=False)
        self.verify(pc)
        self.code('verification-pending')
        self.verify(dc, 'must-revise')
        self.code('verification-objected')
        self.verify(dc)
        self.assertEqual(self.act().outcome, 'allow')
        self.verify(dc, 'must-revise')
        self.code('verification-objected')
        claims.waive(self.conn, dc.relay_id, 'design', 'operator', 'skip this check', self.clock)
        self.assertEqual(self.act().outcome, 'allow')

    def test_commit_by_planner_passes(self):
        self.assertEqual(self.act(seat=PLANNER).outcome, 'allow')
        self.assertIsNone(self.act(seat=PLANNER).slip_id)

    def test_commit_without_slip(self):
        self.code('slip-missing')

    def test_two_bundles_one_repo(self):
        _, pc, a1 = self.build(bundle='a')
        a1id = self.slip(a1)[0]
        self.assertEqual(self.act(branch='a').slip_id, a1id)
        self.plan.write_bytes(b'plan Y\n')
        self.code('plan-changed', branch='a')
        pc2 = self.approved('plan', parent=pc.relay_id)
        self.verify(pc2)
        a2 = self.issue(pc2)
        self.assertEqual(self.act(branch='a').slip_id, self.slip(a2)[0])
        other = self.plan.with_name('bundle-b.md'); other.write_bytes(b'B\n')
        saved = self.plan; self.plan = other
        _, _, b = self.build(bundle='b')
        self.plan = saved
        self.assertEqual(self.act(branch='a').slip_id, self.slip(a2)[0])
        self.assertEqual(self.act(branch='b').slip_id, self.slip(b)[0])
        saved.write_bytes(b'new A\n')
        self.code('plan-changed', branch='a')
        self.assertEqual(self.act(branch='b').outcome, 'allow')
        self.code('slip-invalid', repo='repo-2')
        for sid in (a1id, self.slip(a2)[0]):
            slips.revoke(self.conn, sid, 'operator', 'stop A', self.clock)
        self.assertIn('revoked', self.code('slip-invalid', branch='a').reason)
        self.assertEqual(self.act(branch='b').outcome, 'allow')

    def test_issue_is_atomic_with_admission(self):
        _, pc, _ = self.build(issue=False)
        data = with_repo(draft(kind='IMPL', needs='act', parent=pc.relay_id, to=IMPL.address))
        original = slips.on_admitted
        for fault in (RuntimeError('fault'), errors.error_for('E-SUBMISSION-KEY-REUSED')):
            def hook(conn, admitted, parsed, ctx):
                original(conn, admitted, parsed, ctx)
                raise fault
            before = self.conn.execute('SELECT count(*) FROM relays').fetchone()
            with self.assertRaises(errors.EngineError):
                submit.submit(self.conn, replace(self.ctx(), after_admit=hook), data)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM relays').fetchone(), before)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM slips').fetchone()[0], 0)
        ctx = self.ctx(); admitted = submit.submit(self.conn, ctx, data)
        self.assertEqual(submit.submit(self.conn, ctx, data), admitted)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM slips').fetchone()[0], 1)
        with self.assertRaises(errors.EngineError) as caught:
            submit.submit(self.conn, ctx, data.replace(b'SUBJECT: s', b'SUBJECT: changed'))
        self.assertEqual(caught.exception.code, 'E-SUBMISSION-KEY-REUSED')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM slips').fetchone()[0], 1)

    def test_other_project_lock_ignored(self):
        refs = ('file:' + str(self.plan),)
        _, dc = self.designed()
        own = self.ask('plan', dc, self.plan)
        foreign = self.submit(draft(kind='PLAN', needs='review', to=QIMPL.address, refs=refs), FOREIGN)
        # Past every relay's time, so the foreign review and the own approve take the same one.
        self.clock = self.conn.execute('SELECT max(created_at) FROM relays').fetchone()[0] + 1
        self.submit(draft(kind='PLAN-REVIEW', verdict='approve', to=FOREIGN.address,
                          parent=foreign.relay_id, refs=refs), QIMPL)
        approval = self.answer(own, 'approve', self.plan)
        # The premise: equal lock times and the foreign snapshot's handle the higher, so only
        # the project filter keeps that lock from being read as the newest.
        locks = {project: (locked_at, handle) for project, locked_at, handle in self.conn.execute(
            'SELECT r.project_id, s.locked_at, s.handle FROM snapshots s JOIN relays r ON r.id = s.relay_id'
            ' WHERE s.artifact = ? AND s.locked_by IS NOT NULL', (str(self.plan),))}
        self.assertEqual(sorted(locks), sorted((PLANNER.project_id, FOREIGN.project_id)))
        self.assertEqual(locks[PLANNER.project_id][0], locks[FOREIGN.project_id][0],
                         'the two locks carry different times')
        self.assertGreater(locks[FOREIGN.project_id][1], locks[PLANNER.project_id][1],
                           'the foreign snapshot does not carry the higher handle')
        self.assertIsNotNone(self.request(approval), 'the approval filed no claim')
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', approval.relay_id): [str(self.plan)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_lock_identity_not_borrowed(self):
        _, a, _ = self.build(bundle='a', verified=False, issue=False)
        self.plan.write_bytes(b'plan Y\n')
        _, b, _ = self.build(bundle='b', verified=False, issue=False)
        impl = self.issue(a)
        self.verify(b)
        self.plan.write_bytes(b'plan X\n')
        self.code('verification-pending')
        # Verify every claim in A's actual lineage; B's approvals do not count.
        for row in queries.lineage(self.conn, a.relay_id):
            if self.request(type('Claim', (), {'relay_id': row.id})()):
                self.verify(type('Claim', (), {'relay_id': row.id})())
        self.assertEqual(self.act().slip_id, self.slip(impl)[0])
        self.plan.write_bytes(b'plan Y\n')
        self.code('plan-changed')

    def test_issued_slip_keeps_bound_digest(self):
        _, a, first = self.build(bundle='a')
        self.plan.write_bytes(b'plan Y\n')
        newer = self.approved('plan', bundle='b')
        self.verify(newer)
        self.plan.write_bytes(b'plan X\n')
        self.assertEqual(self.act().slip_id, self.slip(first)[0])
        self.plan.write_bytes(b'plan Y\n')
        self.code('plan-changed')
        renewal = self.approved('plan', parent=a.relay_id)
        self.verify(renewal)
        last = self.issue(renewal)
        self.assertEqual(self.act().slip_id, self.slip(last)[0])

    def test_git_artifact_working_copy(self):
        clean_git_environment(self)
        repo = Repo({'plan.md': 'git plan\n'}); self.addCleanup(repo.close)
        self.directory.checkouts['bivpak'] = repo.path
        pc = self.approved('plan', refs=('bivpak@main:plan.md',))
        self.verify(pc); self.issue(pc, ORCH)
        self.assertEqual(self.act(path=str(repo.path)).outcome, 'allow')
        path = Path(repo.path) / 'plan.md'; path.write_bytes(b'edit\n')
        self.code('plan-changed', path=str(repo.path))
        path.unlink(); self.code('plan-changed', path=str(repo.path))
        # A second checkout of the committing repository holding the locked bytes is not read:
        # the commit's own worktree is the working copy.
        second = Path(self.temp.name) / 'second-checkout'; second.mkdir()
        (second / 'plan.md').write_bytes(b'git plan\n')
        self.conn.execute("INSERT INTO repository_checkouts (repository_id, path, last_seen_at, checkout_id, route_created_at) VALUES (?, ?, 1, 'a2-checkout', 1)",
                          ('repo-1', str(second)))
        self.conn.commit()
        self.code('plan-changed', path=str(repo.path))
        self.assertIn('repo-1', self.code('slip-invalid', repo='repo-2').reason)
        # A slip for the committing repository may cite a plan kept in another one.
        other_repo = Repo({'plan.md': 'other plan\n'}); self.addCleanup(other_repo.close)
        self.directory.checkouts['other'] = other_repo.path
        pc2 = self.approved('plan', refs=('other@main:plan.md',), bundle='other-plan')
        self.verify(pc2); self.issue(pc2, ORCH)
        self.code('plan-changed', branch='other-plan', path=str(repo.path))
        self.conn.execute("INSERT INTO repository_checkouts (repository_id, path, last_seen_at, checkout_id, route_created_at) VALUES (?, ?, 1, 'a2-checkout', 1)",
                          ('repo-2', str(other_repo.path)))
        self.conn.commit()
        self.assertEqual(self.act(branch='other-plan', path=str(repo.path)).outcome, 'allow')
        self.conn.execute("INSERT INTO repository_checkouts (repository_id, path, last_seen_at, checkout_id, route_created_at) VALUES (?, ?, 1, 'a2-checkout', 1)",
                          ('repo-2', self.temp.name))
        self.conn.commit()
        self.code('plan-changed', branch='other-plan', path=str(repo.path))
        self.assertIsNone(slips.working_path(self.conn, 'repo-2:plan.md', slips.Effect('commit', 'repo-1')))
        self.assertEqual(slips.working_path(self.conn, str(self.plan), slips.Effect('commit', 'repo-1')), str(self.plan))

    def test_own_repository_needs_commit_path(self):
        # M1: the committing repository's working copy is the commit's own worktree, never a checkout row.
        clean_git_environment(self)
        repo = Repo({'plan.md': 'git plan\n'}); self.addCleanup(repo.close)
        copy = Repo({'plan.md': 'git plan\n'}); self.addCleanup(copy.close)
        self.directory.checkouts['bivpak'] = repo.path
        pc = self.approved('plan', refs=('bivpak@main:plan.md',))
        self.verify(pc); self.issue(pc, ORCH)
        self.conn.execute("INSERT INTO repository_checkouts (repository_id, path, last_seen_at, checkout_id, route_created_at) VALUES (?, ?, 1, 'a2-checkout', 1)",
                          ('repo-1', str(copy.path)))
        self.conn.commit()
        self.assertIsNone(slips.working_path(self.conn, 'repo-1:plan.md', slips.Effect('commit', 'repo-1')))
        self.assertIn('repo-1:plan.md', self.code('plan-changed').reason)
        self.assertEqual(self.act(path=str(repo.path)).outcome, 'allow')

    def test_working_path_reads_open_checkouts_only(self):
        ended = Path(self.temp.name) / 'ended-checkout'
        opened = Path(self.temp.name) / 'open-checkout'
        self.conn.execute(
            'INSERT INTO repository_checkouts '
            '(repository_id, path, last_seen_at, checkout_id, route_created_at, ended_at) '
            'VALUES (?, ?, 1, ?, 1, 2)',
            ('repo-2', str(ended), 't6-ended'))
        self.conn.commit()
        self.assertIsNone(slips.working_path(
            self.conn, 'repo-2:plan.md', slips.Effect('commit', 'repo-1')))
        self.conn.execute(
            'INSERT INTO repository_checkouts '
            '(repository_id, path, last_seen_at, checkout_id, route_created_at) '
            'VALUES (?, ?, 1, ?, 1)',
            ('repo-2', str(opened), 't6-open'))
        self.conn.commit()
        self.assertEqual(slips.working_path(
            self.conn, 'repo-2:plan.md', slips.Effect('commit', 'repo-1')),
            str(opened / 'plan.md'))

    def test_lineage_truncation_fails_closed(self):
        for extra, prefix in ((250, False), (251, False), (250, True)):
            with self.subTest(extra=extra, prefix=prefix):
                self.conn.close(); self.setUp()
                old = self.submit(draft(cc=ORCH.address)) if prefix else None
                _, pc, _ = self.build(issue=False, prefix=old.relay_id if old else None)
                parent = pc
                for _ in range(extra):
                    parent = self.submit(draft(parent=parent.relay_id, to=PLANNER.address,
                                               cc=ORCH.address))
                impl = self.issue(parent)
                if extra == 250 and not prefix:
                    self.assertEqual(len(queries.lineage(self.conn, impl.relay_id)), 256)
                    self.assertEqual(self.act().outcome, 'allow')
                else:
                    self.assertIsNone(self.slip(impl))
                    answer = self.code('lineage-incomplete')
                    self.assertIn('256', answer.reason)

    def test_impl_diagnostic_scope(self):
        self.code('slip-missing')
        self.submit(with_repo(draft(kind='IMPL', needs='act', to=SECOND.address)), OTHER)
        self.submit(draft())
        # Each relay below differs from a diagnostic IMPL by one filter: kind, NEEDS, the TO role, the project.
        self.submit(draft(kind='SITREP', needs='act', to=IMPL.address))
        self.submit(draft(kind='IMPL', needs='none', to=IMPL.address))
        self.submit(with_repo(draft(kind='IMPL', needs='act', to=PLANNER.address, cc=IMPL.address)))
        resolve = self.directory.resolve
        self.directory.resolve = lambda address, sender: (IMPL if address == 'v3-cross.implementer'
                                                          else resolve(address, sender))
        crossing = self.submit(with_repo(draft(kind='IMPL', needs='act', to='v3-cross.implementer')), FOREIGN)
        self.directory.resolve = resolve
        self.assertEqual(self.conn.execute("SELECT seat_id FROM recipients WHERE relay_id = ? AND role = 'to'",
                                           (crossing.relay_id,)).fetchone(), (IMPL.seat_id,))
        self.code('slip-missing')
        self.build()
        self.code('slip-invalid', repo='repo-2')

    def test_unreferenced_approve_issues_nothing(self):
        request = self.submit(draft(kind='PLAN', needs='review', to=IMPL.address, refs=('file:' + str(self.plan),)))
        with self.assertRaises(errors.EngineError) as refused:
            self.submit(draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                              to=PLANNER.address, cc=self.issuers), IMPL)
        self.assertEqual(refused.exception.code, 'E-SNAPSHOT-NO-REF')
        # Engine refuses an approve naming none of its request's snapshots, so the unreferenced
        # approve that admits is one whose request referenced nothing: it locks and claims nothing.
        pc = self.approved('plan', refs=())
        self.assertIsNone(self.request(pc))
        impl = self.issue(pc, ORCH)
        self.assertIsNone(self.slip(impl))
        self.code('lineage-incomplete')

    def test_revoke(self):
        _, _, impl = self.build(); sid = self.slip(impl)[0]
        refused = slips.revoke(self.conn, sid, OTHER.seat_id, 'stop', self.clock)
        self.assertEqual(refused.code, 'authority.not-issuer')
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(slips.revoke(self.conn, sid, PLANNER.seat_id, 'stop', self.clock).outcome, 'allow')
        self.assertIn('revoked', self.code('slip-invalid').reason)

    def test_slip_codes_one_meaning(self):
        self.code('slip-missing')
        # S1: a seat holding no slip of the kind answers slip-missing, with or without a branch.
        self.code('slip-missing', branch=None)
        _, _, impl = self.build(); sid = self.slip(impl)[0]
        self.code('slip-invalid', repo='repo-2')
        self.code('slip-invalid', branch=None)
        self.assertEqual(self.act(branch='owned').outcome, 'allow')
        self.code('slip-missing', seat=SECOND, branch='owned')
        # "Held by another seat" is slip-invalid for a seat that holds a slip of its own.
        refs = ('file:' + str(self.plan),)
        request = self.submit(draft(kind='PLAN', needs='review', to=SECOND.address, refs=refs, bundle='second'), ORCH)
        approval = self.submit(draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                                     to=ORCH.address, refs=refs), SECOND)
        self.verify(approval)
        second = self.submit(with_repo(draft(kind='IMPL', needs='act', parent=approval.relay_id, to=SECOND.address)), ORCH)
        self.issued(second)
        held = self.code('slip-invalid', seat=SECOND, branch='owned')
        self.assertIn('seat', held.reason)
        self.assertEqual(held.slip_id, sid)
        slips.revoke(self.conn, sid, 'operator', 'stop', self.clock)
        self.assertIn('revoked', self.code('slip-invalid').reason)

    def test_merge_kinds_have_no_slip(self):
        self.build()
        for kind in ('pr-merge', 'api-ref-write'):
            answer = self.code('slip-missing', kind=kind)
            self.assertIn('merge', answer.reason)
            self.assertEqual(answer.unblocker, ORCH.address)
        guarded = self.code('check-unavailable', kind='protected-push')
        self.assertIn('guardrail', guarded.reason)
        self.assertEqual(guarded.unblocker, ORCH.address)

    def test_fault_is_held(self):
        self.build()
        class BrokenRead(CommitFault):
            def execute(self, sql, parameters=()):
                if 'FROM slips' in sql:
                    raise sqlite3.DatabaseError('read fault')
                return self.conn.execute(sql, parameters)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            answer = slips.act(BrokenRead(self.conn), IMPL.seat_id, slips.Effect('commit', 'repo-1', 'work'),
                               self.clock, digest=lambda path: None)
            self.assertEqual(answer.code, 'authority.check-unavailable')
        finally:
            self.conn.rollback()

    def test_revoke_owns_its_transaction(self):
        _, _, impl = self.build(); sid = self.slip(impl)[0]
        self.conn.execute('BEGIN IMMEDIATE'); before = self.conn.total_changes
        with self.assertRaises(ValueError):
            slips.revoke(self.conn, sid, 'operator', 'stop', self.clock)
        self.assertEqual(self.conn.total_changes, before); self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            slips.revoke(CommitFault(self.conn), sid, 'operator', 'stop', self.clock)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT state FROM slips WHERE id = ?', (sid,)).fetchone()[0], 'live')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM slip_events WHERE slip_id = ?', (sid,)).fetchone()[0], 1)

    def test_no_body_read(self):
        self.build()
        attempts = []
        reader = BodyReader(self.conn, attempts)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            answer = slips.act(reader, IMPL.seat_id, slips.Effect('commit', 'repo-1', 'work'), self.clock,
                               digest=lambda path, algorithm='sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest())
        finally:
            self.conn.rollback()
        self.assertEqual(answer.outcome, 'allow')
        self.assertTrue(attempts)
        self.assertEqual(set(attempts), {'denied'})
        # Control: the same reading outside act reaches the bodies.
        reader.execute('SELECT 1')
        self.assertEqual(attempts[-1], 'read')

    def test_answer_shape(self):
        missing = self.code('slip-missing')
        _, _, impl = self.build(verified=False)
        held = self.code('verification-pending')
        self.assertEqual(held.slip_id, self.slip(impl)[0]); self.assertEqual(held.revision, 1)
        self.assertIsNone(missing.slip_id)

    def test_no_direct_authority_rows(self):
        tables = {'slips', 'slip_events', 'branch_bindings', 'reviewer_assignments',
                  'verification_requests', 'waivers', 'relays', 'edges', 'snapshots', 'recipients'}
        pattern = re.compile(r'^\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)', re.I)
        for name in ('test_claims.py', 'test_slips.py', 'test_authority_v3.py', 'lineage_tier.py'):
            path = Path(__file__).with_name(name)
            self.assertTrue(path.is_file(), 'missing Authority fixture module: ' + name)
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    match = pattern.match(node.value)
                    self.assertFalse(match and match.group(1).lower() in tables, (name, node.lineno))
        # The shared setup helper, run as a fixture, writes only identity tables.
        written = set()
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(lambda sql: written.update(m.group(1).lower() for m in [pattern.match(sql)] if m))
            return conn
        fixture = TestQueries('test_commit_without_slip')
        with mock.patch.object(sqlite3, 'connect', side_effect=traced):
            fixture.setUp()
        fixture.doCleanups()
        self.assertTrue(written)
        self.assertLessEqual(written, {'schema_version', 'projects', 'team_instances', 'seats',
                                       'seat_occupants', 'repositories'})


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TestErratum3(SlipStore):
    """Erratum 3 r2's B1, B2 and B3, read with erratum 4 r2, each with its control."""

    def file(self, name, data):
        path = self.plan.with_name(name)
        path.write_bytes(data)
        return path

    def test_erratum_lineage_binds_every_plan(self):
        _, base, _ = self.build(issue=False)
        erratum = self.file('erratum.md', b'erratum X\n')
        second = self.approved('plan', parent=base.relay_id, refs=('file:' + str(erratum),))
        self.verify(second)
        impl = self.issue(second)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', base.relay_id): [str(self.plan)],
                                                               ('plan', second.relay_id): [str(erratum)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.plan.write_bytes(b'rewritten base\n')
        self.assertIn(str(self.plan), self.code('plan-changed').reason)
        self.plan.write_bytes(b'plan X\n')
        self.assertEqual(self.act().outcome, 'allow')
        erratum.write_bytes(b'rewritten erratum\n')
        self.assertIn(str(erratum), self.code('plan-changed').reason)

    def test_newer_review_not_approve_issues_nothing(self):
        _, base, _ = self.build(issue=False)
        self.plan.write_bytes(b'plan Y\n')
        revising = self.ask('plan', base, self.plan)
        objection = self.answer(revising, 'must-revise', self.plan)
        impl = self.issue(objection)
        self.assertIsNone(self.slip(impl))
        # The older approval of plan X does not stand in: the bytes it locked still deny.
        self.plan.write_bytes(b'plan X\n')
        denied = self.code('lineage-incomplete')
        self.assertEqual(denied.outcome, 'deny')
        for named in (str(self.plan), objection.relay_id, revising.relay_id):
            self.assertIn(named, denied.reason)
        # Control: the same lineage ending in an approve of the revision binds the revision only.
        self.plan.write_bytes(b'plan Y\n')
        renewed = self.ask('plan', objection, self.plan)
        approval = self.answer(renewed, 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', approval.relay_id): [str(self.plan)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.plan.write_bytes(b'plan X\n')
        self.code('plan-changed')

    def test_objected_new_plan_issues_nothing(self):
        _, base, _ = self.build(issue=False)
        extra = self.file('extra.md', b'extra X\n')
        objection = self.answer(self.ask('plan', base, extra), 'must-revise', extra)
        impl = self.issue(objection)
        self.assertIsNone(self.slip(impl))
        self.assertIn(str(extra), self.code('lineage-incomplete').reason)
        # Control: the same path approved binds.
        self.conn.close(); self.setUp()
        _, base, _ = self.build(issue=False)
        extra = self.file('extra.md', b'extra X\n')
        approval = self.answer(self.ask('plan', base, extra), 'approve', extra)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        extra.write_bytes(b'extra Y\n')
        self.assertIn(str(extra), self.code('plan-changed').reason)

    def test_unreviewed_reference_binds_digest(self):
        context = self.file('context.md', b'context X\n')
        notes = self.file('notes.md', b'notes X\n')
        go = self.go(refs=('file:' + str(context),))
        _, pc, _ = self.build(issue=False, delegation=False, prefix=go.relay_id)
        impl = self.issue(pc, refs=('file:' + str(notes),))
        self.assertEqual(self.digest_artifacts(impl), {str(context): sha(b'context X\n'),
                                                       str(notes): sha(b'notes X\n')})
        bound = sorted(artifact for artifacts in self.claim_artifacts(impl).values() for artifact in artifacts)
        self.assertEqual(bound, sorted([str(self.design), str(self.plan)]))
        self.assertEqual(self.act().slip_id, self.issued(impl))
        for path, original in ((context, b'context X\n'), (notes, b'notes X\n')):
            path.write_bytes(b'edited\n')
            self.assertIn(str(path), self.code('plan-changed').reason)
            path.write_bytes(original)
            self.assertEqual(self.act().outcome, 'allow')
        # Two references to one undecided path at different digests bind the newer one.
        context.write_bytes(b'context Y\n')
        newer = self.issue(pc, refs=('file:' + str(context),))
        self.assertEqual(self.digest_artifacts(newer)[str(context)], sha(b'context Y\n'))
        self.assertEqual(self.act().slip_id, self.issued(newer))
        context.write_bytes(b'context X\n')
        self.code('plan-changed')

    def test_partial_approve_binds_rest_by_digest(self):
        context = self.file('context.md', b'context X\n')
        _, dc = self.designed()
        approval = self.answer(self.ask('plan', dc, self.plan, context), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', approval.relay_id): [str(self.plan)]})
        self.assertEqual(self.digest_artifacts(impl), {str(context): sha(b'context X\n')})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        context.write_bytes(b'context Y\n')
        self.assertIn(str(context), self.code('plan-changed').reason)
        # Plan r1 (plan and context) answered must-revise naming the plan, then plan r2
        # (revised plan, same context) approved naming the plan only: a slip issues.
        self.conn.close(); self.setUp()
        context = self.file('context.md', b'context X\n')
        _, dc = self.designed()
        objection = self.answer(self.ask('plan', dc, self.plan, context), 'must-revise', self.plan)
        self.plan.write_bytes(b'plan Y\n')
        approval = self.answer(self.ask('plan', objection, self.plan, context), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', approval.relay_id): [str(self.plan)]})
        self.assertEqual(self.digest_artifacts(impl), {str(context): sha(b'context X\n')})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # Control: r1's must-revise naming no path objects the context file too.
        self.conn.close(); self.setUp()
        context = self.file('context.md', b'context X\n')
        _, dc = self.designed()
        objection = self.answer(self.ask('plan', dc, self.plan, context), 'must-revise')
        self.plan.write_bytes(b'plan Y\n')
        approval = self.answer(self.ask('plan', objection, self.plan, context), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertIsNone(self.slip(impl))
        self.assertIn(str(context), self.code('lineage-incomplete').reason)
        # Erratum 4 r2 R1: a holder's verdictless PLAN-REVIEW request on a changed plan
        # leaves the earlier lock current.
        self.conn.close(); self.setUp()
        _, dc = self.designed()
        first = self.answer(self.ask('plan', dc, self.plan), 'approve', self.plan)
        self.verify(first)
        self.plan.write_bytes(b'plan Y\n')
        revised = self.ask('plan', first, self.plan)
        pending = self.submit(draft(kind='PLAN-REVIEW', needs='review', parent=revised.relay_id,
                                    to=PLANNER.address, cc=self.issuers,
                                    refs=('file:' + str(self.plan),)), IMPL)
        impl = self.issue(pending)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', first.relay_id): [str(self.plan)]})
        self.code('plan-changed')
        self.plan.write_bytes(b'plan X\n')
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_git_reference_binds_blob_id(self):
        clean_git_environment(self)
        repo = Repo({'notes.md': 'git notes\n'}); self.addCleanup(repo.close)
        self.directory.checkouts['bivpak'] = repo.path
        _, pc, _ = self.build(issue=False)
        impl = self.issue(pc, refs=('bivpak@main:notes.md',))
        blob = hashlib.sha1(b'blob 10\0git notes\n').hexdigest()
        self.assertEqual(blob, git(repo.path, 'rev-parse', 'main:notes.md'))
        self.assertEqual(self.digest_artifacts(impl), {'repo-1:notes.md': blob})
        self.assertEqual(self.act(path=str(repo.path)).slip_id, self.issued(impl))
        notes = Path(repo.path) / 'notes.md'
        notes.write_bytes(b'edited\n')
        self.assertIn('repo-1:notes.md', self.code('plan-changed', path=str(repo.path)).reason)
        notes.write_bytes(b'git notes\n')
        self.assertEqual(self.act(path=str(repo.path)).outcome, 'allow')
        # Erratum 4 r2 R4: no admitted relay can carry a 64-digit Git object id at this base,
        # because every `git` REF resolves through rules.xroot_repository_and_commit, which
        # refuses a commit that is not 40 lowercase hex; so the SHA-256 case is this unit
        # check of the function act calls, with the git-sha256 constant in test_working_copy_digest.
        choose = getattr(slips, 'git_digest_algorithm', None)
        self.assertIsNotNone(choose, 'slips.git_digest_algorithm is not defined')
        self.assertEqual(choose('a' * 40), 'git-sha1')
        self.assertEqual(choose('b' * 64), 'git-sha256')
        for digits in (0, 39, 41, 63, 65):
            with self.subTest(digits=digits), self.assertRaises(ValueError):
                choose('c' * digits)

    def test_digests_alone_issue_nothing(self):
        # Erratum 5 r4 S1: the lineage binds digest-bound paths only, with no locked claim at all.
        context = self.file('context.md', b'context X\n')
        go = self.go(refs=('file:' + str(context),))
        impl = self.issue(go, refs=('file:' + str(self.plan),))
        self.assertIsNone(self.slip(impl))
        self.code('lineage-incomplete')
        # Control: one holder PLAN-REVIEW lock in the same lineage lets a slip issue.
        approval = self.answer(self.ask('plan', go, self.plan), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_renewed_design_binds_newest(self):
        go = self.go()
        first = self.approved('design', parent=go.relay_id)
        self.verify(first)
        self.design.write_bytes(b'design Y\n')
        renewed = self.approved('design', parent=first.relay_id)
        self.verify(renewed)
        pc = self.approved('plan', parent=renewed.relay_id)
        self.verify(pc)
        impl = self.issue(pc)
        self.assertEqual(self.claim_artifacts(impl, 'design'), {('design', renewed.relay_id): [str(self.design)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.design.write_bytes(b'design X\n')
        self.code('design-changed')
        # Control: a single approval at X binds X.
        self.conn.close(); self.setUp()
        dc, _, impl = self.build()
        self.assertEqual(self.claim_artifacts(impl, 'design'), {('design', dc.relay_id): [str(self.design)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.design.write_bytes(b'design Y\n')
        self.code('design-changed')

    def test_plan_review_cannot_approve_design_revision(self):
        _, dc = self.designed()
        self.design.write_bytes(b'design Y\n')
        objection = self.answer(self.ask('design', dc, self.design), 'must-revise', self.design, kind='design')
        approval = self.answer(self.ask('plan', objection, self.plan, self.design), 'approve',
                               self.plan, self.design)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertIsNone(self.slip(impl))
        self.assertIn(str(self.design), self.code('lineage-incomplete').reason)
        # Control: Y approved by a DESIGN-REVIEW binds Y.
        self.conn.close(); self.setUp()
        _, dc = self.designed()
        self.design.write_bytes(b'design Y\n')
        renewed = self.approved('design', parent=dc.relay_id)
        self.verify(renewed)
        approval = self.answer(self.ask('plan', renewed, self.plan, self.design), 'approve',
                               self.plan, self.design)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl, 'design'), {('design', renewed.relay_id): [str(self.design)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_commit_role_rule_covers_roster(self):
        rule = getattr(slips, 'COMMIT_ROLE_RULE', None)
        self.assertIsNotNone(rule, 'slips.COMMIT_ROLE_RULE is not defined')
        self.assertEqual(set(rule), set(addresses.ROLE_WORDS))
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                          " VALUES ('tr', 'p', 'v3-roster', 'pair', 1, 'operator'),"
                          " ('tz', 'p', 'v3-unlisted', 'pair', 1, 'operator')")
        roster = {}
        for role in sorted(addresses.ROLE_WORDS) + ['unlisted-role']:
            # An unlisted role word cannot appear in a TO address (E-ADDRESS), so that seat is
            # reached through a parseable address whose directory entry is that seat.
            team, address = ('tz', 'v3-unlisted.implementer') if role == 'unlisted-role' else ('tr', 'v3-roster.' + role)
            seat = submit.SeatRef('seat-roster-' + role, address, role, team, 'p')
            self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state) VALUES (?, ?, ?, 'occupied')",
                              (seat.seat_id, team, role))
            self.conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                              " VALUES (?, ?, ?, 'v', 1)", ('o-' + seat.seat_id, seat.seat_id, 'k-' + seat.seat_id))
            roster[role] = seat
        self.conn.commit()
        resolve = self.directory.resolve
        self.directory.resolve = lambda address, sender: next(
            (seat for seat in roster.values() if seat.address == address), None) or resolve(address, sender)
        claims.assign_reviewer(self.conn, 't1', REVIEWER.seat_id, 'operator', self.clock)
        self.build()
        self.assertEqual(self.act(branch='bound').outcome, 'allow')
        planners = {'planner', 'orchestrator-planner', 'master-planner', 'domain-planner'}
        reviewers = {'reviewer', 'orchestrator-reviewer', 'master-reviewer', 'domain-reviewer'}
        for role, seat in roster.items():
            for branch in ('free', 'bound'):
                with self.subTest(role=role, branch=branch):
                    answer = self.act(seat=seat, branch=branch)
                    self.assertEqual(answer.code or answer.outcome,
                                     'allow' if role in planners else 'authority.slip-missing')
        for role in sorted(reviewers):
            self.submit(with_repo(draft(kind='IMPL', needs='act', to=roster[role].address, bundle='roster-' + role)))
            for branch in ('free', 'bound'):
                with self.subTest(role=role, branch=branch, addressed=True):
                    self.assertEqual(self.act(seat=roster[role], branch=branch).code, 'authority.not-issuer')
        # Erratum 5 r4 S2: the unlisted role answers slip-missing with an IMPL addressed to it too.
        self.submit(with_repo(draft(kind='IMPL', needs='act', to=roster['unlisted-role'].address, bundle='roster-unlisted')))
        for branch in ('free', 'bound'):
            with self.subTest(role='unlisted-role', branch=branch, addressed=True):
                self.assertEqual(self.act(seat=roster['unlisted-role'], branch=branch).code, 'authority.slip-missing')
        # The table decides: a planner role taken out of it answers slip-missing.
        with mock.patch.object(slips, 'COMMIT_ROLE_RULE', {k: v for k, v in rule.items() if k != 'planner'}):
            self.assertEqual(self.act(seat=roster['planner'], branch='free').code, 'authority.slip-missing')
        self.assertEqual(self.act(seat=roster['planner'], branch='free').outcome, 'allow')

    def test_plan_reference_to_approved_design_binds_design_lock(self):
        _, dc = self.designed()
        approval = self.answer(self.ask('plan', dc, self.plan, self.design), 'approve', self.plan, self.design)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl), {('design', dc.relay_id): [str(self.design)],
                                                      ('plan', approval.relay_id): [str(self.plan)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.design.write_bytes(b'design Y\n')
        self.code('design-changed')

    def test_revised_plan_after_must_revise_binds(self):
        _, dc = self.designed()
        objection = self.answer(self.ask('plan', dc, self.plan, self.design), 'must-revise')
        self.plan.write_bytes(b'plan Y\n')
        approval = self.answer(self.ask('plan', objection, self.plan, self.design), 'approve',
                               self.plan, self.design)
        self.verify(approval)
        impl = self.issue(approval)
        expected = {('design', dc.relay_id): [str(self.design)], ('plan', approval.relay_id): [str(self.plan)]}
        self.assertEqual(self.claim_artifacts(impl), expected)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # Erratum 6 r3 R-D: a refusal naming the plan at its locked bytes objects it.
        again = self.answer(self.ask('plan', approval, self.plan), 'must-revise', self.plan)
        later = self.issue(again)
        self.assertIsNone(self.slip(later))
        # Erratum 8 Y1 (reading A): a later review in another request's lineage does not end a
        # live slip (design lines 84, 91 and 92), so `act` still allows under `impl`.
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # The holder's approve naming the plan then locks it under its own claim.
        repeat = self.answer(self.ask('plan', again, self.plan), 'approve', self.plan)
        last = self.issue(repeat)
        self.assertEqual(self.claim_artifacts(last), {('design', dc.relay_id): [str(self.design)],
                                                      ('plan', repeat.relay_id): [str(self.plan)]})
        self.code('verification-pending')
        self.verify(repeat)
        self.assertEqual(self.act().slip_id, self.issued(last))

    def test_revoking_newest_slip_leaves_no_earlier_live(self):
        # Supersession prevents a revoke from exposing the earlier go-ahead.
        _, dc = self.designed()
        approval = self.answer(self.ask('plan', dc, self.plan, self.design), 'approve', self.plan, self.design)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.plan.write_bytes(b'plan Y\n')
        again = self.answer(self.ask('plan', approval, self.plan), 'approve', self.plan)
        self.verify(again)
        last = self.issue(again)
        sid = self.issued(impl)
        self.assertEqual(self.conn.execute('SELECT state, end_reason FROM slips WHERE id = ?',
                                          (sid,)).fetchone(), ('ended', 'superseded'))
        self.assertEqual(self.act().slip_id, self.issued(last))
        self.assertEqual(slips.revoke(self.conn, self.issued(last), PLANNER.seat_id, 'stop', self.clock).outcome,
                         'allow')
        for data in (b'plan Y\n', b'plan X\n'):
            self.plan.write_bytes(data)
            answer = self.code('slip-invalid')
            self.assertEqual(answer.outcome, 'deny')
            self.assertEqual(answer.slip_id, self.issued(last))
        refused = slips.revoke(self.conn, sid, PLANNER.seat_id, 'stop', self.clock)
        self.assertEqual((refused.outcome, refused.code), ('deny', 'authority.slip-invalid'))
        self.assertEqual(self.conn.execute('SELECT state, end_reason FROM slips WHERE id = ?',
                                          (sid,)).fetchone(), ('ended', 'superseded'))

    def test_operator_objection_waived_still_binds_digest(self):
        # Erratum 4 r2 R2's control.
        _, base, _ = self.build(issue=False)
        self.plan.write_bytes(b'plan Y\n')
        objection = self.answer(self.ask('plan', base, self.plan), 'must-revise', self.plan,
                                cc=self.issuers + ', operator')
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        impl = self.submit(with_repo(draft(kind='IMPL', needs='act', parent=objection.relay_id, to=IMPL.address)), operator)
        self.assertIn('objected:' + str(self.plan), self.binds(impl)['waived'])
        # Erratum 5 r4 S3: B2's plan-lock waiver stays beside it (erratum 4 r3 R2).
        self.assertIn('plan-lock', self.binds(impl)['waived'])
        self.assertEqual(self.digest_artifacts(impl), {str(self.plan): sha(b'plan Y\n')})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.plan.write_bytes(b'plan Z\n')
        self.assertIn(str(self.plan), self.code('plan-changed').reason)


class TestErratum5(SlipStore):
    """Erratum 5 r4's E1, E2 and E3, each with its control."""

    def file(self, name, data):
        path = self.plan.with_name(name)
        path.write_bytes(data)
        return path

    def refs(self, *paths):
        return tuple('file:' + str(path) for path in paths)

    def objected(self):
        """Design approved; plan.md and extra.md approved by the holder at X and verified;
        plan.md rewritten to Y and answered must-revise by the holder naming plan.md."""
        extra = self.file('extra.md', b'extra X\n')
        _, dc = self.designed()
        approval = self.answer(self.ask('plan', dc, self.plan, extra), 'approve', self.plan, extra)
        self.verify(approval)
        self.plan.write_bytes(b'plan Y\n')
        return self.answer(self.ask('plan', approval, self.plan), 'must-revise', self.plan)

    def refused(self, impl, path, review):
        self.assertIsNone(self.slip(impl), 'a slip issued over the objection')
        denied = self.code('lineage-incomplete')
        self.assertEqual(denied.outcome, 'deny')
        self.assertIn(str(path), denied.reason)
        self.assertIn(review.relay_id, denied.reason)

    def test_unqualified_design_review_decides_nothing(self):
        plan = lambda: self.refs(self.plan)
        def design_request(objection, to, needs='review'):
            return self.submit(draft(kind='DESIGN', needs=needs, parent=objection.relay_id, to=to.address,
                                     refs=plan()))
        def design_review(parent, sender, verdict='approve'):
            fields = {'verdict': verdict} if verdict else {'needs': 'review'}
            return self.submit(draft(kind='DESIGN-REVIEW', parent=parent.relay_id, to=PLANNER.address,
                                     refs=plan(), **fields), sender)
        cases = {
            # (a) a verdictless DESIGN-REVIEW from the pair Planner parented to the objection
            'a-verdictless-planner': lambda o: self.submit(draft(
                kind='DESIGN-REVIEW', needs='review', parent=o.relay_id, to=IMPL.address, refs=plan())),
            # (b) another seat's approve under a DESIGN with NEEDS review referencing plan.md at Y
            'b-other-seat': lambda o: design_review(design_request(o, SECOND), SECOND),
            # (c) the holder's approve under a SITREP referencing plan.md
            'c-under-sitrep': lambda o: design_review(self.submit(draft(
                parent=o.relay_id, to=IMPL.address, refs=plan())), IMPL),
            # (d) the holder's verdict under a DESIGN without NEEDS review, which holds no snapshot
            'd-no-snapshot': lambda o: design_review(design_request(o, IMPL, needs='none'), IMPL),
            # (e) the holder's verdictless review under a DESIGN with NEEDS review holding plan.md
            'e-verdictless-holder': lambda o: design_review(design_request(o, IMPL), IMPL, verdict=None),
            # (f) the holder's approve under a PLAN with NEEDS review holding plan.md
            'f-plan-parent': lambda o: design_review(self.ask('plan', o, self.plan), IMPL),
        }
        for name, file_review in cases.items():
            with self.subTest(case=name):
                self.conn.close(); self.setUp()
                objection = self.objected()
                self.refused(self.issue(file_review(objection)), self.plan, objection)
        # Control: the same lineage with no DESIGN-REVIEW answers the same.
        self.conn.close(); self.setUp()
        objection = self.objected()
        self.refused(self.issue(objection), self.plan, objection)
        # (g) Erratum 7 X3 (F1), on its own lineage with a path no earlier relay names: H's verdictless
        # DESIGN-REVIEW naming p under a DESIGN holding p fails `qualifies`, so p is no design path,
        # H's later approve of a PLAN holding p locks it in a plan claim, left unverified, and `act`
        # holds; under F1 p is a design path, the lock only pins it, p binds by digest and `act` allows.
        with self.subTest(case='g-verdictless-holder-own-lineage'):
            self.conn.close(); self.setUp()
            # Section E's prefix, as TestErratum6.based builds it; `answer` leaves a claim unverified.
            base = self.file('b.md', b'b X\n')
            prefix = self.answer(self.ask('plan', self.go(), base), 'approve', base)
            self.verify(prefix)
            p = self.file('p.md', b'p X\n')
            design = self.ask('design', prefix, p)
            verdictless = self.submit(draft(kind='DESIGN-REVIEW', needs='review', parent=design.relay_id,
                                            to=PLANNER.address, refs=self.refs(p)), IMPL)
            approval = self.answer(self.ask('plan', verdictless, p), 'approve', p)
            impl = self.issue(approval)
            self.assertEqual(self.claim_artifacts(impl, 'plan').get(('plan', approval.relay_id)), [str(p)])
            self.assertNotIn(str(p), self.digest_artifacts(impl))
            self.code('verification-pending')

    def test_design_review_cannot_reclassify_approved_plan(self):
        _, dc = self.designed()
        approval = self.answer(self.ask('plan', dc, self.plan), 'approve', self.plan)
        self.verify(approval)
        self.plan.write_bytes(b'plan Z\n')
        sitrep = self.submit(draft(parent=approval.relay_id, to=IMPL.address, refs=self.refs(self.plan)))
        review = self.submit(draft(kind='DESIGN-REVIEW', needs='review', parent=sitrep.relay_id,
                                   to=IMPL.address, refs=self.refs(self.plan)))
        impl = self.issue(review)
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', approval.relay_id): [str(self.plan)]})
        self.assertNotIn(str(self.plan), self.digest_artifacts(impl))
        self.assertIn(str(self.plan), self.code('plan-changed').reason)
        self.plan.write_bytes(b'plan X\n')
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def other_seat_design(self, verdict):
        """A DESIGN of the design TO a second implementer, answered by that implementer."""
        go = self.go()
        request = self.submit(draft(kind='DESIGN', needs='review', parent=go.relay_id, to=SECOND.address,
                                    refs=self.refs(self.design)))
        return self.submit(draft(kind='DESIGN-REVIEW', verdict=verdict, parent=request.relay_id,
                                 to=PLANNER.address, refs=self.refs(self.design)), SECOND)

    def test_other_seat_design_binds_as_plan_path(self):
        other = self.other_seat_design('approve')
        approval = self.answer(self.ask('plan', other, self.plan, self.design), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl), {('plan', approval.relay_id): [str(self.plan)]})
        self.assertEqual(self.digest_artifacts(impl), {str(self.design): sha(b'design X\n')})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.design.write_bytes(b'design Y\n')
        self.assertIn(str(self.design), self.code('plan-changed').reason)
        # Control: the same lineage with the holder's DESIGN-REVIEW binds the design claim.
        self.conn.close(); self.setUp()
        go = self.go()
        own = self.answer(self.ask('design', go, self.design), 'approve', self.design, kind='design')
        self.verify(own)
        approval = self.answer(self.ask('plan', own, self.plan, self.design), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.claim_artifacts(impl, 'design'), {('design', own.relay_id): [str(self.design)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.design.write_bytes(b'design Y\n')
        self.code('design-changed')

    def test_other_seat_design_objection_refuses(self):
        objection = self.other_seat_design('must-revise')
        approval = self.answer(self.ask('plan', objection, self.plan, self.design), 'approve', self.plan)
        self.verify(approval)
        # 7017c97 answered the same: any DESIGN-REVIEW with a verdict decided its paths, so no slip.
        self.refused(self.issue(approval), self.design, objection)
        # The holder's PLAN-REVIEW approve whose lock holds the design replaces nothing.
        both = self.answer(self.ask('plan', objection, self.plan, self.design), 'approve', self.plan, self.design)
        self.verify(both)
        self.refused(self.issue(both), self.design, objection)
        # Nor does a holder PLAN-REVIEW must-revise naming the design, then its approve: the second
        # implementer's objection stays the one in force (Authority Planner 151024).  7017c97 answered
        # the same, since there plan reviews never decided a path a DESIGN-REVIEW's parent referenced.
        revising = self.answer(self.ask('plan', objection, self.plan, self.design), 'must-revise', self.design)
        again = self.answer(self.ask('plan', revising, self.plan, self.design), 'approve', self.plan, self.design)
        self.verify(again)
        self.refused(self.issue(again), self.design, objection)
        # The holder's qualifying DESIGN-REVIEW approve then lets the slip issue with the design claim.
        renewed = self.answer(self.ask('design', both, self.design), 'approve', self.design, kind='design')
        self.verify(renewed)
        impl = self.issue(renewed)
        self.assertEqual(self.claim_artifacts(impl, 'design'), {('design', renewed.relay_id): [str(self.design)]})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # Control: the second implementer's approve instead binds the design by digest.
        self.conn.close(); self.setUp()
        other = self.other_seat_design('approve')
        approval = self.answer(self.ask('plan', other, self.plan, self.design), 'approve', self.plan)
        self.verify(approval)
        impl = self.issue(approval)
        self.assertEqual(self.digest_artifacts(impl), {str(self.design): sha(b'design X\n')})
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_operator_slip_checks_verification(self):
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        def lineage():
            _, dc = self.designed()
            approval = self.answer(self.ask('plan', dc, self.plan), 'approve', self.plan,
                                   cc=self.issuers + ', operator')
            impl = self.submit(with_repo(draft(kind='IMPL', needs='act', parent=approval.relay_id, to=IMPL.address)), operator)
            return approval, impl
        approval, impl = lineage()
        self.assertEqual([w for w in self.binds(impl)['waived'] if w.startswith('verification:')], [])
        self.code('verification-pending')
        self.verify(approval)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # A second operator lineage: the reviewer's must-revise holds, and only the operator's
        # waiver through claims.waive lets it allow.
        self.conn.close(); self.setUp()
        approval, impl = lineage()
        self.verify(approval, 'must-revise')
        self.code('verification-objected')
        claims.waive(self.conn, approval.relay_id, 'plan', 'operator', 'the operator accepts this plan', self.clock)
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_non_approve_naming_unheld_path_objects_all(self):
        _, base, _ = self.build(issue=False)
        extra = self.file('extra.md', b'extra X\n')
        unrelated = self.file('unrelated.md', b'unrelated X\n')
        objection = self.answer(self.ask('plan', base, extra), 'must-revise', unrelated)
        self.refused(self.issue(objection), extra, objection)
        # Control: the same must-revise naming the requested path answers the same.
        self.conn.close(); self.setUp()
        _, base, _ = self.build(issue=False)
        extra = self.file('extra.md', b'extra X\n')
        objection = self.answer(self.ask('plan', base, extra), 'must-revise', extra)
        self.refused(self.issue(objection), extra, objection)


# Erratum 6 r5 section E: the bounded enumeration's alphabet, as count_space_r4.py and
# count_suite_r4.py spell it (the Authority Planner's sizing scripts).
import itertools
import sys

LINEAGE_REFSETS = (('p',), ('q',), ('p', 'q'))
LINEAGE_NAMESETS = ((), ('p',), ('q',), ('p', 'q'))
LINEAGE_KINDS = ('D', 'Dn', 'P', 'S', 'D2', 'PC')
LINEAGE_REQUESTS = [('R', ())] + [(kind, refs) for kind in LINEAGE_KINDS for refs in LINEAGE_REFSETS]
LINEAGE_RESPONSES = ([None] + [('H', kind, verdict, names) for kind in ('PR', 'DR')
                               for verdict in ('approve', 'must-revise') for names in LINEAGE_NAMESETS]
                     + [('V', names) for names in LINEAGE_NAMESETS]
                     + [('O', 'DR', verdict, names) for verdict in ('approve', 'must-revise')
                        for names in LINEAGE_NAMESETS]
                     # A-2 plan r6 Task 4 (r6 erratum 1 r2 E1): plain reviews, `KIND: REVIEW`, by H and O.
                     + [(who, 'RV', verdict, names) for who in ('H', 'O') for verdict in ('approve', 'must-revise')
                        for names in LINEAGE_NAMESETS])
LINEAGE_SNAPSHOT_TO = {'D': 'H', 'P': 'H', 'D2': 'O', 'PC': 'H'}
# Who besides the Planner is a member of each request, so may reply under it (E-PARENT-NOT-MEMBER).
LINEAGE_MEMBERS = {'D': {'H'}, 'Dn': {'H'}, 'P': {'H'}, 'S': {'H'}, 'D2': {'O'}, 'PC': {'H', 'O'}}


def lineage_sender(response):
    """H sends its own reviews and the verdictless review (erratum 6 r5 E); O is the second implementer."""
    return 'H' if response[0] in ('H', 'V') else 'O'


def lineage_admissible(request, response, members_only=True):
    """count_space_r4.py's pruning: a reply with no response is a no-op; an approve under a
    request with snapshots comes from its TO seat and names at least one held reference; and a
    response to a new request comes from one of its members.  A reply to the last relay from a
    non-member is kept and refused at submit."""
    kind, refs = request
    if kind == 'R' and response is None:
        return False
    if members_only and response is not None and kind != 'R' and lineage_sender(response) not in LINEAGE_MEMBERS[kind]:
        return False
    if response is None or response[0] == 'V' or response[2] != 'approve':
        return True
    if kind in LINEAGE_SNAPSHOT_TO:
        return (LINEAGE_SNAPSHOT_TO[kind] == response[0] and len(response[3]) > 0
                and set(response[3]) <= set(refs))
    return True


def lineage_rounds(edits, requests, responses, members_only=True):
    return [(edit, request, response) for edit in edits for request in requests for response in responses
            if lineage_admissible(request, response, members_only)]


_SUITE_REQUESTS = [('R', ()), ('D', ('p',)), ('D', ('p', 'q')), ('Dn', ('p',)), ('P', ('p',)), ('S', ('p',)),
                   ('D2', ('p',)), ('PC', ('p',))]
_SUITE_RESPONSES = [None, ('H', 'PR', 'approve', ('p',)), ('H', 'PR', 'must-revise', ('p',)),
                    ('H', 'PR', 'must-revise', ()), ('H', 'DR', 'approve', ('p',)), ('H', 'DR', 'approve', ('q',)),
                    ('H', 'DR', 'must-revise', ('p',)), ('H', 'DR', 'must-revise', ('q',)),
                    ('H', 'DR', 'must-revise', ()), ('V', ('p',)), ('V', ()), ('O', 'DR', 'approve', ('p',)),
                    ('O', 'DR', 'must-revise', ('p',)), ('O', 'DR', 'must-revise', ())] + [
                    (who, 'RV', verdict, names) for who in ('H', 'O') for verdict in ('approve', 'must-revise')
                    for names in LINEAGE_NAMESETS]
LINEAGE_FULL_ROUNDS = lineage_rounds((None, 'p', 'q'), LINEAGE_REQUESTS, LINEAGE_RESPONSES)
LINEAGE_FULL_UNPRUNED = lineage_rounds((None, 'p', 'q'), LINEAGE_REQUESTS, LINEAGE_RESPONSES, members_only=False)
LINEAGE_SUITE_ROUNDS = lineage_rounds((None, 'p'), _SUITE_REQUESTS, _SUITE_RESPONSES)
LINEAGE_SUITE_UNPRUNED = lineage_rounds((None, 'p'), _SUITE_REQUESTS, _SUITE_RESPONSES, members_only=False)
_D, _P, _S, _PC = ('D', ('p', 'q')), ('P', ('p',)), ('S', ('p',)), ('PC', ('p',))
LINEAGE_NAMED_WORDS = {
    'P1': ((None, _P, ('H', 'PR', 'approve', ('p',))), ('p', _D, ('H', 'DR', 'approve', ('q',)))),
    'P2': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, _D, ('H', 'DR', 'approve', ('q',)))),
    'P4': ((None, _D, ('H', 'DR', 'approve', ('q',))), ('p', _P, ('H', 'PR', 'must-revise', ('p',)))),
    'P5': ((None, _S, ('H', 'PR', 'must-revise', ('p',))),),
    'item4': ((None, _D, ('H', 'DR', 'must-revise', ())), (None, _P, ('H', 'PR', 'approve', ('p',)))),
    'RD': ((None, _P, ('H', 'PR', 'approve', ('p',))), (None, _P, ('H', 'PR', 'must-revise', ('p',)))),
    'a': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, ('R', ()), ('V', ()))),
    'b': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, ('D2', ('p',)), ('O', 'DR', 'approve', ('p',)))),
    'c': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, _S, ('H', 'DR', 'approve', ('p',)))),
    'd': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, ('Dn', ('p',)), ('H', 'DR', 'approve', ('p',)))),
    'e': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, ('D', ('p',)), ('V', ('p',)))),
    'f': (('p', _P, ('H', 'PR', 'must-revise', ('p',))), (None, _P, ('H', 'DR', 'approve', ('p',)))),
    'PIN': ((None, _P, ('H', 'DR', 'approve', ('p',))), ('p', ('D', ('p',)), None)),
    'PIN-V': ((None, _P, ('H', 'DR', 'approve', ('p',))), ('p', ('R', ()), ('V', ('p',)))),
    'RC': ((None, _PC, ('O', 'DR', 'must-revise', ('p',))),),
    'RC-none': ((None, _PC, ('O', 'DR', 'must-revise', ())),),
    'OPIN': ((None, ('D2', ('p',)), ('O', 'DR', 'approve', ('p',))), ('p', _S, None)),
    # A-2 plan r6 Task 4's three words.
    'RV-PIN': ((None, _P, ('H', 'RV', 'approve', ('p',))), ('p', _S, None)),
    'RV-OBJ': ((None, _P, ('H', 'RV', 'must-revise', ('p',))),),
    'RV-DEC': ((None, _P, ('H', 'PR', 'approve', ('p',))), ('p', _P, ('H', 'RV', 'approve', ('p',)))),
}
# Erratum 6 r5's control prediction at aa84dbc: which oracles flag each named word.
LINEAGE_CONTROL_PREDICTION = {
    **{name: 'O1+O2' for name in ('P1', 'P2', 'P4', 'P5', 'RD', 'PIN', 'PIN-V')},
    **{name: 'O1' for name in ('RC', 'RC-none', 'OPIN')},
    **{name: None for name in ('a', 'b', 'c', 'd', 'e', 'f', 'item4')},
}
# A-2 plan r6 Task 4's control prediction at 0bbbfdf under the widened oracles: only the two plain-review
# words violate; every earlier word, which 0bbbfdf already keeps, and RV-DEC do not.
LINEAGE_TASK4_CONTROL_PREDICTION = {
    **{name: None for name in LINEAGE_NAMED_WORDS},
    'RV-PIN': 'O1+O2', 'RV-OBJ': 'O1+O2',
}
# The oracles' review kinds, derived from the engine's own verdict table (r6 erratum 1 r2 E1): every kind
# whose verdicts include `approve` can lock, so the oracles read it; MERGE-GATE and LIVE-VERIFY carry other
# verdicts, never lock and review no path, so they are left out.
_REVIEW_KINDS6 = tuple(sorted(kind for kind, verdicts in header.VERDICTS.items() if 'approve' in verdicts))
_REVIEW_KINDS_LEFT_OUT = {'MERGE-GATE': 'no approve verdict: never locks and reviews no path',
                          'LIVE-VERIFY': 'no approve verdict: never locks and reviews no path'}


def lineage_canonical(word):
    """p and q are symmetric: a lineage is kept only when the first free file it mentions is p."""
    for edit, (_, refs), response in word:
        for name in ([edit] if edit else []) + list(refs) + (list(response[-1]) if response else []):
            return name == 'p'
    return True


def _lineage_first_mention(round_):
    edit, (_, refs), response = round_
    for name in ([edit] if edit else []) + list(refs) + (list(response[-1]) if response else []):
        return name
    return None


def lineage_word_count(rounds, canonical):
    """The number of words of at most two rounds over `rounds`, without building any."""
    if not canonical:
        return 1 + len(rounds) + len(rounds) ** 2
    first = [_lineage_first_mention(round_) for round_ in rounds]
    alone = sum(1 for name in first if name in (None, 'p'))
    return 1 + alone + sum(len(rounds) if name == 'p' else alone if name is None else 0 for name in first)


class LineageStore:
    """What the oracles read, by SQL alone: relays, snapshots, locks and reference edges."""

    def __init__(self, conn, impl_id):
        rows, relay = [], impl_id
        while relay is not None:
            row = conn.execute('SELECT id, kind, verdict, sender_seat_id, parent_id FROM relays WHERE id = ?',
                               (relay,)).fetchone()
            rows.append(row)
            relay = row[4]
        self.rows = rows[::-1][:-1]          # oldest first, the IMPL itself left out
        self.kind = {row[0]: row[1] for row in self.rows}
        self.held = {row[0]: dict(conn.execute('SELECT artifact, content_sha256 FROM snapshots WHERE relay_id = ?',
                                               (row[0],))) for row in self.rows}
        self.locked = {row[0]: dict(conn.execute('SELECT artifact, content_sha256 FROM snapshots'
                                                 ' WHERE locked_by = ?', (row[0],))) for row in self.rows}
        self.named = {row[0]: [(path, digest) for path, digest in conn.execute(
            "SELECT target_path, digest_at_submit FROM edges WHERE from_relay_id = ? AND kind = 'ref'"
            " AND target_type = 'path' ORDER BY ordinal", (row[0],))] for row in self.rows}
        # A verification reply, a REVIEW whose parent is a verification request, decides and pins nothing.
        requests = {row[0] for row in conn.execute('SELECT request_relay_id FROM verification_requests')}
        self.reviews = {row[0] for row in self.rows
                        if row[1] in _REVIEW_KINDS6 and not (row[1] == 'REVIEW' and row[4] in requests)}
        self.newest = {}
        for row in self.rows:
            for path, digest in self.named[row[0]]:
                self.newest[path] = digest

    def held_by_parent(self, row):
        return self.held.get(row[4], {})


def lineage_model(store, holder):
    """O1: erratum 6 r5's sections D and R, computed from the relays alone.  Returns
    (issues, expected) where expected maps each path the lineage references to the digest
    P1 lets `act` allow it at: a decided path's lock, else its newest holder pin, else its
    newest pin by any seat, else the digest at its newest reference."""
    def qualifying(row):
        return (row[1] == 'DESIGN-REVIEW' and row[2] is not None and row[3] == holder
                and store.kind.get(row[4]) == 'DESIGN')

    def reaches(row):
        held, named = store.held_by_parent(row), dict(store.named[row[0]])
        out = {}
        if held:
            inside = [path for path in named if path in held]
            for path in inside:
                out[path] = (held[path], False)
            if not inside:
                for path, digest in held.items():
                    out[path] = (digest, True)
        for path, digest in named.items():
            if path not in held:
                out[path] = (digest, False)
        return out

    def counted(row):
        return row[3] == holder or row[1] == 'DESIGN-REVIEW'

    def run(design):
        state, block, fallback_design = {}, False, set()
        for row in store.rows:
            if row[0] not in store.reviews or row[2] is None:
                continue
            if row[2] == 'approve':
                if row[1] == 'PLAN-REVIEW' and row[3] == holder:
                    deciding, source = {p: d for p, d in store.locked[row[0]].items() if p not in design}, 'plan'
                elif qualifying(row):
                    deciding, source = store.locked[row[0]], 'design'
                else:
                    continue
                if deciding:
                    block = False
                for path, digest in deciding.items():
                    current = state.get(path)
                    if current and current[0] == 'objected' and source != 'design' and (
                            current[1] == 'design' or path in design):
                        continue
                    if current and current[0] == 'locked' and current[1] == digest:
                        continue
                    state[path] = ('locked', digest, source)
                continue
            if not counted(row):
                continue
            if row[3] == holder and not store.held_by_parent(row) and not store.named[row[0]]:
                block = True
            for path, (digest, fallback) in reaches(row).items():
                current = state.get(path)
                if fallback and current and current[0] == 'locked' and current[1] == digest:
                    continue
                if current and current[0] == 'objected' and current[1] == 'design' and row[1] != 'DESIGN-REVIEW':
                    continue
                state[path] = ('objected', 'design' if row[1] == 'DESIGN-REVIEW' else 'plan')
                if fallback and qualifying(row):
                    fallback_design.add(path)
        return state, block, fallback_design

    design = set()
    for row in store.rows:
        if qualifying(row):
            if row[2] == 'approve':
                design |= set(store.locked[row[0]])
            else:
                design |= {path for path, (_, fallback) in reaches(row).items() if not fallback}
    while True:
        state, block, fallback_design = run(design)
        if fallback_design <= design:
            break
        design |= fallback_design
    issues = (not block and not any(entry[0] == 'objected' for entry in state.values())
              and any(entry[0] == 'locked' and entry[2] == 'plan' for entry in state.values()))
    pins = {}
    for row in store.rows:
        if row[0] in store.reviews and row[2] == 'approve':
            for path, digest in store.locked[row[0]].items():
                pins.setdefault(path, {})['holder' if row[3] == holder else 'other'] = digest
    def binding(path, digest):
        if path in state:
            return state[path][1]
        pinned = pins.get(path, {})
        return pinned.get('holder', pinned.get('other', digest))
    expected = {path: binding(path, digest) for path, digest in store.newest.items()}
    return issues, expected


def lineage_holder_words(store, holder, issued, allowed):
    """O2: the holder's own words and P1's second branch, with no class or fallback logic.
    `allowed` maps each content assignment (path -> digest) that `act` allowed."""
    found = []
    reviews = [(index, row) for index, row in enumerate(store.rows) if row[0] in store.reviews]
    refusals = [(index, row) for index, row in reviews if row[3] == holder and row[2] not in (None, 'approve')]
    approvals = [(index, path, digest) for index, row in reviews if row[3] == holder and row[2] == 'approve'
                 for path, digest in store.locked[row[0]].items()]
    touched = set()
    for _, row in reviews:
        if row[2] is None:
            continue
        touched |= set(store.locked[row[0]])
        if row[2] != 'approve':
            touched |= set(store.held_by_parent(row)) | {path for path, _ in store.named[row[0]]}
    for assignment in allowed:
        for index, row in refusals:
            for path, digest in store.named[row[0]]:
                if assignment.get(path) == digest and not any(
                        later > index and p == path and d == digest for later, p, d in approvals):
                    found.append(('i', path, row[0]))
        for path in {p for _, p, _ in approvals}:
            if path in assignment and assignment[path] not in {d for _, p, d in approvals if p == path}:
                found.append(('ii', path, None))
        for path, digest in store.newest.items():
            if path not in touched and assignment.get(path) != digest:
                found.append(('iii', path, None))
    if issued:
        for index, row in refusals:
            for path, _ in store.named[row[0]]:
                if not any(later > index and p == path for later, p, _ in approvals):
                    found.append(('iv', path, row[0]))
    return sorted(set(found), key=repr)


class TestTask4PlainReview(SlipStore):
    """Plain reviews and pending-claim recovery through real admission.

    Tests build closed/reopened reviewer teams, stood-down/missing reviewers,
    holder-as-reviewer assignments and operator waivers, beyond SlipStore's rows.
    No real TableDirectory, authenticated lifecycle/sender checks, key rotation,
    concurrent reviewer replacement, or multi-project reviewer recovery is built.
    """

    def plain_review(self, request, path, sender=IMPL):
        return self.submit(draft(kind='REVIEW', verdict='approve',
                                 parent=request.relay_id, to=PLANNER.address,
                                 cc=self.issuers, refs=('file:' + str(path),)), sender)

    def test_plain_review_approve_pins_bytes(self):
        # RV-M1: ignoring a plain approve's lock would bind the later Y reference.
        _, approval, _ = self.build(design=False, issue=False)
        path = self.plan.with_name('plain-pin.md')
        path.write_bytes(b'X\n')
        pin = self.plain_review(self.ask('plan', approval, path), path)
        path.write_bytes(b'Y\n')
        later = self.submit(draft(parent=pin.relay_id, to=IMPL.address,
                                  refs=('file:' + str(path),)))
        issued = self.issue(later)
        self.assertEqual(self.digest_artifacts(issued)[str(path)],
                         hashlib.sha256(b'X\n').hexdigest())
        self.assertIn(str(path), self.code('plan-changed').reason)
        path.write_bytes(b'X\n')
        self.assertEqual(self.act().slip_id, self.issued(issued))

    def test_decided_path_keeps_its_lock_after_plain_approve(self):
        # RV-M4: treating a plain approve as a new decision would permit Y.
        for ordinal, sender in enumerate((IMPL, SECOND)):
            with self.subTest(sender=sender.address):
                if ordinal:
                    self.doCleanups()
                    self.setUp()
                self.plan.write_bytes(b'plan X\n')
                _, approval, _ = self.build(bundle='plain-decided-' + str(ordinal),
                                            design=False, issue=False)
                self.plan.write_bytes(b'plan Y\n')
                request = self.submit(draft(kind='PLAN', needs='review',
                                            parent=approval.relay_id, to=sender.address,
                                            cc=IMPL.address if sender == SECOND else SECOND.address,
                                            refs=('file:' + str(self.plan),)))
                plain = self.plain_review(request, self.plan, sender)
                issued = self.issue(plain)
                self.assertIn(('plan', approval.relay_id), self.claim_artifacts(issued))
                self.assertNotIn(str(self.plan), self.digest_artifacts(issued))
                answer = self.code('plan-changed')
                self.assertEqual(answer.outcome, 'held')
                self.assertIn(str(self.plan), answer.reason)
                current = self.answer(self.ask('plan', plain, self.plan), 'approve', self.plan)
                self.verify(current)
                fresh = self.issue(current)
                self.assertEqual(self.act().slip_id, self.issued(fresh))

    def test_plain_review_refusal_objects(self):
        # RV-M2: a holder's plain refusal must object, another seat's must not.
        _, approval, _ = self.build(design=False, issue=False)
        request = self.ask('plan', approval, self.plan)
        refusal = self.submit(draft(kind='REVIEW', verdict='must-revise',
                                    parent=request.relay_id, to=PLANNER.address,
                                    cc=self.issuers, refs=('file:' + str(self.plan),)), IMPL)
        issued = self.issue(refusal)
        self.assertIsNone(self.slip(issued), 'a slip must not issue over H plain refusal')
        answer = self.code('lineage-incomplete')
        self.assertIn(refusal.relay_id, answer.reason)
        current = self.answer(self.ask('plan', refusal, self.plan), 'approve', self.plan)
        self.verify(current)
        current_issue = self.issue(current)
        self.assertEqual(self.act().slip_id, self.issued(current_issue))
        request = self.submit(draft(kind='PLAN', needs='review', parent=current.relay_id,
                                    to=SECOND.address, cc=IMPL.address,
                                    refs=('file:' + str(self.plan),)))
        other = self.submit(draft(kind='REVIEW', verdict='must-revise', parent=request.relay_id,
                                  to=PLANNER.address, cc=self.issuers,
                                  refs=('file:' + str(self.plan),)), SECOND)
        other_issue = self.issue(other)
        self.assertEqual(self.act().slip_id, self.issued(other_issue))

    def test_verification_reply_decides_nothing(self):
        # RV-M3: H verifies a claim, but its reply must not object an unrelated p.
        # The fresh delegation and q claim descend from H's verification reply.
        claims.assign_reviewer(self.conn, 't1', IMPL.seat_id, 'operator', self.clock)
        original = self.approved('plan', parent=self.go().relay_id)
        path = self.plan.with_name('verification-p.md')
        path.write_bytes(b'undecided p\n')
        request = self.request(original)[0]
        replies = []
        for verdict in ('approve', 'must-revise'):
            reply = self.submit(draft(kind='REVIEW', verdict=verdict, parent=request,
                                      to=PLANNER.address, cc=self.issuers,
                                      refs=('file:' + str(path),)), IMPL)
            replies.append(reply)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM snapshots WHERE locked_by = ?',
                                               (reply.relay_id,)).fetchone()[0], 0)
            self.assertIsNone(self.request(reply))
            self.assertEqual(self.state(original), 'approved' if verdict == 'approve' else 'objected')
        delegation = self.submit(draft(kind='PLAN', needs='act', parent=replies[-1].relay_id,
                                       to=PLANNER.address, cc=IMPL.address), ORCH)
        current = self.approved('plan', parent=delegation.relay_id)
        self.submit(draft(kind='REVIEW', verdict='approve', parent=self.request(current)[0],
                          to=PLANNER.address), IMPL)
        issued = self.issue(current)
        self.assertIsNotNone(self.slip(issued), 'verification must-revise must not object p')
        self.assertEqual(self.act().slip_id, self.issued(issued))
        self.assertEqual(self.digest_artifacts(issued)[str(path)],
                         hashlib.sha256(b'undecided p\n').hexdigest())

    def test_alphabet_covers_engine_verdict_kinds(self):
        # RV-M5: a literal list misses a newly admitted approve-carrying kind.
        import runpy
        included = set(_REVIEW_KINDS6)
        translated = {'PR': 'PLAN-REVIEW', 'DR': 'DESIGN-REVIEW', 'RV': 'REVIEW'}
        for responses in (LINEAGE_RESPONSES, _SUITE_RESPONSES):
            represented = {translated[x[1]] for x in responses if x and x[0] in ('H', 'O')}
            self.assertEqual(represented, included)
        self.assertEqual(included | set(_REVIEW_KINDS_LEFT_OUT), set(header.VERDICTS))
        for kind, reason in _REVIEW_KINDS_LEFT_OUT.items():
            self.assertNotIn('approve', header.VERDICTS[kind])
            self.assertTrue(reason)
        with mock.patch.dict(header.VERDICTS, {'FUTURE-REVIEW': frozenset({'approve'})}):
            rebuilt = runpy.run_path(__file__)
        self.assertIn('FUTURE-REVIEW', rebuilt['_REVIEW_KINDS6'])

    def test_no_reviewer_claim_pending_for_operator(self):
        # RR-M1: no reviewer must hold the claim, never refuse admission.
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at,"
                          " opened_by, closed_at)"
                          " VALUES ('closed-reviewer-team', 'p', 'closed-reviewer', 'orchestrator',"
                          " 1, 'operator', 2)")
        self.conn.execute("UPDATE seats SET team_instance_id = 'closed-reviewer-team' WHERE id = ?",
                          (REVIEWER.seat_id,))
        self.conn.commit()
        try:
            claim = self.approved('plan', parent=self.go().relay_id)
        except errors.EngineError as error:
            self.fail('no-reviewer approving reply must be admitted, got ' + error.code)
        self.assertEqual(self.request(claim), (None, None))
        issued = self.issue(claim)
        held = self.code('verification-pending')
        self.assertEqual(held.unblocker, 'operator')
        claims.waive(self.conn, claim.relay_id, 'plan', 'operator', 'waive missing reviewer', self.clock)
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, self.issued(issued))
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        self.assertEqual(self.request(claim), (None, None))
        current = self.approved('plan', parent=claim.relay_id)
        self.assertEqual(self.request(current)[1], SECOND.seat_id)
        self.verify(current, sender=SECOND)

    def test_no_reviewer_claim_recovers_by_re_request(self):
        # T5-M1: keeping the older NULL-reviewer claim defeats the remedy.
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.commit()
        first = self.approved('plan', parent=self.go().relay_id)
        self.assertEqual(self.request(first), (None, None))
        self.issue(first)
        claims.assign_reviewer(self.conn, 't1', SECOND.seat_id, 'operator', self.clock)
        current = self.approved('plan', parent=first.relay_id)
        issued = self.issue(current)
        self.assertIn(('plan', current.relay_id), self.claim_artifacts(issued, 'plan'))
        pending = self.code('verification-pending')
        self.assertEqual(pending.outcome, 'held')
        self.assertEqual(pending.unblocker, SECOND.address)
        self.verify(current, sender=SECOND)
        allowed = self.act()
        self.assertEqual(allowed.outcome, 'allow')
        self.assertEqual(allowed.slip_id, self.issued(issued))

    def reviewer_replacement(self, state, verdict=None):
        # Identity rows only; claim, request, verification and slip use admission.
        team = 't5-' + state + ('-objected' if verdict else '-pending')
        name = 'reviewer-' + team
        old = submit.SeatRef('s-' + team, name + '.orchestrator-reviewer',
                             'orchestrator-reviewer', team, 'p')
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                          " VALUES (?, 'p', ?, 'orchestrator', 1, 'operator')", (team, name))
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES (?, ?, 'orchestrator-reviewer', 'occupied')", (old.seat_id, team))
        self.conn.commit()
        original = self.directory.resolve
        self.directory.resolve = lambda address, sender: old if address == old.address else original(address, sender)
        claims.assign_reviewer(self.conn, 't1', old.seat_id, 'operator', self.clock)
        claim = self.approved('plan', parent=self.go(bundle=team).relay_id, bundle=team)
        self.assertEqual(self.request(claim)[1], old.seat_id)
        if verdict:
            self.verify(claim, verdict, old)
        issued = self.issue(claim)
        if state == 'closed':
            self.conn.execute('UPDATE team_instances SET closed_at = 2 WHERE id = ?', (team,))
        else:
            self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (old.seat_id,))
        next_team, next_name = team + '-new', name if state == 'closed' else name + '-new'
        current = submit.SeatRef('s-' + next_team, next_name + '.orchestrator-reviewer',
                                 'orchestrator-reviewer', next_team, 'p')
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                          " VALUES (?, 'p', ?, 'orchestrator', 3, 'operator')", (next_team, next_name))
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES (?, ?, 'orchestrator-reviewer', 'occupied')", (current.seat_id, next_team))
        self.conn.commit()
        self.directory.resolve = lambda address, sender: current if address == current.address else original(address, sender)
        claims.assign_reviewer(self.conn, 't1', current.seat_id, 'operator', self.clock)
        return claim, issued, current

    def test_closed_reviewer_claim_reads_as_no_reviewer(self):
        # T5-M3/M4: a dead recorded reviewer must not be the pending unblocker.
        for state in ('closed', 'stood-down'):
            with self.subTest(state=state):
                first, old_issue, reviewer = self.reviewer_replacement(state)
                pending = self.code('verification-pending', branch=state)
                self.assertEqual(pending.unblocker, 'operator')
                self.assertEqual(pending.outcome, 'held')
                self.assertIn('assign a reviewer', pending.reason)
                current = self.approved('plan', parent=first.relay_id)
                self.assertEqual(self.request(current)[1], reviewer.seat_id)
                self.verify(current, sender=reviewer)
                issued = self.issue(current)
                self.assertIn(('plan', current.relay_id), self.claim_artifacts(issued, 'plan'))
                allowed = self.act(branch=state)
                self.assertEqual(allowed.outcome, 'allow')
                self.assertEqual(allowed.slip_id, self.issued(issued))
                self.assertIn(('plan', first.relay_id), self.claim_artifacts(old_issue, 'plan'))

    def test_objected_claim_not_replaced_after_close(self):
        # T5-M2: an unavailable reviewer does not erase a recorded objection.
        for state in ('closed', 'stood-down'):
            with self.subTest(state=state):
                first, _, reviewer = self.reviewer_replacement(state, 'must-revise')
                current = self.approved('plan', parent=first.relay_id)
                self.verify(current, sender=reviewer)
                issued = self.issue(current)
                self.assertIn(('plan', first.relay_id), self.claim_artifacts(issued, 'plan'))
                held = self.code('verification-objected', branch=state)
                self.assertEqual(held.outcome, 'held')

    def test_closed_orchestrator_neither_issues_nor_revokes(self):
        # CT-M1..4: closing an issuer is an issue-time rule, not a standing rule.
        _, direct_claim, direct = self.build(bundle='closed-direct', issuer=ORCH, design=False)
        _, pair_claim, _ = self.build(bundle='closed-pair', design=False, issue=False)
        pair = self.issue(pair_claim, repo='other', git_state=replace(GIT_STATE, repository_id='repo-2'))
        direct_slip, pair_slip = self.issued(direct), self.issued(pair)
        self.conn.execute("UPDATE team_instances SET closed_at = 2 WHERE id = 't3'")
        self.conn.commit()
        attempted = self.issue(direct_claim, ORCH)
        self.assertIsNone(self.slip(attempted), 'closed orchestrator must not issue a new slip')
        self.assertIsNone(self.slip(self.issue(pair_claim)), 'closed delegation must not issue a new slip')
        self.assertEqual(self.act(branch='direct').slip_id, direct_slip)
        self.assertEqual(self.act(repo='repo-2', branch='pair').slip_id, pair_slip)
        self.assertEqual(self.act(branch='direct').outcome, 'allow')
        self.assertEqual(self.act(repo='repo-2', branch='pair').outcome, 'allow')
        revoked = slips.revoke(self.conn, direct_slip, ORCH.seat_id, 'closed issuer', self.clock)
        self.assertEqual(revoked.code, 'authority.not-issuer')
        self.assertEqual(self.conn.execute('SELECT state FROM slips WHERE id = ?',
                                           (direct_slip,)).fetchone()[0], 'live')
        # A new open issuer can delegate on the retained lineage without changing it.
        fresh = submit.SeatRef('new-orch', 'new.orchestrator-planner', ORCH.role, 'new-orch-team', 'p')
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)"
                          " VALUES ('new-orch-team', 'p', 'new', 'orchestrator', 3, 'operator')")
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES (?, ?, ?, 'occupied')", (fresh.seat_id, fresh.team_instance_id, fresh.role))
        self.conn.commit()
        resolve = self.directory.resolve
        self.directory.resolve = lambda address, sender: fresh if address == fresh.address else resolve(address, sender)
        invitation = self.submit(draft(parent=pair_claim.relay_id, to=fresh.address,
                                       cc=PLANNER.address + ', ' + IMPL.address))
        delegation = self.submit(draft(kind='PLAN', needs='act', parent=invitation.relay_id,
                                       to=PLANNER.address, cc=IMPL.address), fresh)
        self.issued(self.issue(delegation))
        self.assertNotEqual(direct_slip, pair_slip)

    def test_human_decision_unblocks_operator(self):
        # SF-M2: a human decision cannot be unblocked by the pair Planner.
        _, claim, _ = self.build(design=False, verified=False)
        self.verify(claim, 'human-decision-required')
        answer = self.code('verification-objected')
        self.assertEqual(answer.unblocker, 'operator')
        self.verify(claim, 'must-revise')
        self.assertEqual(self.code('verification-objected').unblocker, PLANNER.address)



class TestErratum6(SlipStore):
    """Erratum 6 r5: what decides a path (D), the property `act` keeps (P), the rules (R-A to
    R-E) and section E's bounded enumeration, judged by two oracles that share no code with
    `slips._decide`."""

    def file(self, name, data):
        path = self.plan.with_name(name)
        path.write_bytes(data)
        return path

    def refs(self, *paths):
        return tuple('file:' + str(path) for path in paths)

    def refused(self, impl, path, review):
        self.assertIsNone(self.slip(impl), 'a slip issued over the objection')
        denied = self.code('lineage-incomplete')
        self.assertEqual(denied.outcome, 'deny')
        if path is not None:
            self.assertIn(str(path), denied.reason)
        self.assertIn(review.relay_id, denied.reason)

    def based(self):
        """Section E's prefix: the delegating PLAN, a PLAN NEEDS review TO the holder holding
        b, the holder's approve of b and its verification."""
        base = self.file('b.md', b'b X\n')
        approval = self.answer(self.ask('plan', self.go(), base), 'approve', base)
        self.verify(approval)
        return approval

    def requested(self, kind, parent, *paths, to=IMPL):
        """A request from the Planner: D, Dn, P, S, D2 or PC of section E."""
        fields = {'D': ('DESIGN', 'review'), 'Dn': ('DESIGN', 'none'), 'P': ('PLAN', 'review'),
                  'S': ('SITREP', 'none'), 'D2': ('DESIGN', 'review'), 'PC': ('PLAN', 'review')}[kind]
        extra = {'cc': SECOND.address} if kind == 'PC' else {}
        return self.submit(draft(kind=fields[0], needs=fields[1], parent=parent.relay_id,
                                 to=(SECOND if kind == 'D2' else to).address, refs=self.refs(*paths), **extra))

    def reviewed(self, request, verdict, *paths, kind='plan', sender=IMPL):
        """A review; any claim it records is verified at once, off the lineage."""
        reply = self.submit(draft(kind=kind.upper() + '-REVIEW', verdict=verdict, parent=request.relay_id,
                                  to=PLANNER.address, cc=self.issuers, refs=self.refs(*paths)), sender)
        if verdict == 'approve' and self.request(reply) is not None:
            self.verify(reply)
        return reply

    def test_holder_refusal_survives_design_context(self):
        p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
        # (P4) D holding q and p answered by H's DESIGN-REVIEW approve naming q, then p flipped and
        # H's PLAN-REVIEW must-revise of it.  aa84dbc: p was a design path, the slip bound it by digest.
        design = self.reviewed(self.requested('D', self.based(), q, p), 'approve', q, kind='design')
        p.write_bytes(b'p Y\n')
        objection = self.reviewed(self.requested('P', design, p), 'must-revise', p)
        self.refused(self.issue(objection), p, objection)
        # (P2) the same two rounds in the other order.
        self.conn.close(); self.setUp()
        p, q = self.file('p.md', b'p Y\n'), self.file('q.md', b'q X\n')
        objection = self.reviewed(self.requested('P', self.based(), p), 'must-revise', p)
        design = self.reviewed(self.requested('D', objection, q, p), 'approve', q, kind='design')
        self.refused(self.issue(design), p, objection)
        # Control: D holding q alone answers the same deny, at aa84dbc and after.
        for order in ('P4', 'P2'):
            self.conn.close(); self.setUp()
            p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
            if order == 'P4':
                design = self.reviewed(self.requested('D', self.based(), q), 'approve', q, kind='design')
                p.write_bytes(b'p Y\n')
                objection = last = self.reviewed(self.requested('P', design, p), 'must-revise', p)
            else:
                p.write_bytes(b'p Y\n')
                objection = self.reviewed(self.requested('P', self.based(), p), 'must-revise', p)
                last = self.reviewed(self.requested('D', objection, q), 'approve', q, kind='design')
            with self.subTest(control=order):
                self.refused(self.issue(last), p, objection)
        # (P3, three rounds) q approved by H's DESIGN-REVIEW at X, p flipped and refused by H's
        # PLAN-REVIEW, then H's DESIGN-REVIEW must-revise naming q at its locked bytes under D
        # holding q and p.  aa84dbc: that must-revise decided nothing, p was a design path, and
        # the slip allowed p at its refused bytes (probe P3).  Under R-D it objects q, which is
        # referenced before p, so the deny names q.
        self.conn.close(); self.setUp()
        p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
        design = self.reviewed(self.requested('D', self.based(), q), 'approve', q, kind='design')
        p.write_bytes(b'p Y\n')
        objection = self.reviewed(self.requested('P', design, p), 'must-revise', p)
        revising = self.reviewed(self.requested('D', objection, q, p), 'must-revise', q, kind='design')
        self.refused(self.issue(revising), q, revising)

    def test_design_review_cannot_move_approved_plan_bytes(self):
        p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
        approval = self.reviewed(self.requested('P', self.go(), p), 'approve', p)
        p.write_bytes(b'p Y\n')
        design = self.reviewed(self.requested('D', approval, q, p), 'approve', q, kind='design')
        impl = self.issue(design)
        # aa84dbc: p, held by a DESIGN the holder approved naming q, was a design path, so the
        # slip bound p by digest at Y and `act` allowed there.
        self.assertEqual(self.claim_artifacts(impl, 'plan'), {('plan', approval.relay_id): [str(p)]})
        self.assertNotIn(str(p), self.digest_artifacts(impl))
        self.assertIn(str(p), self.code('plan-changed').reason)
        p.write_bytes(b'p X\n')
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_refusal_without_snapshot_objects_named(self):
        def build(request):
            new = self.file('new.md', b'new X\n')
            parent = self.based()
            if request == 'S':
                return new, self.requested('S', parent, new)
            return new, self.submit(draft(kind='PLAN', needs='none', parent=parent.relay_id, to=IMPL.address,
                                          refs=self.refs(new)))
        for request in ('S', 'P-without-needs'):
            with self.subTest(case=request):
                self.conn.close(); self.setUp()
                new, parent = build(request)
                objection = self.reviewed(parent, 'must-revise', new)
                # aa84dbc: a refusal whose parent holds no snapshot decided nothing.
                self.refused(self.issue(objection), new, objection)
        # A holder refusal naming nothing under a parent with no snapshot blocks issuance until a
        # later D1 or D2 approve.
        self.conn.close(); self.setUp()
        new, parent = build('S')
        block = self.reviewed(parent, 'must-revise')
        self.refused(self.issue(block), None, block)
        approval = self.reviewed(self.requested('P', block, new), 'approve', new)
        impl = self.issue(approval)
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_design_refusal_under_any_parent_objects(self):
        # A PLAN with NEEDS review TO H holding p, with the second implementer on CC so that its
        # reply is admitted; aa84dbc's line 181 let another seat's DESIGN-REVIEW refusal object
        # only under a DESIGN, so a slip issued in both cases.
        for case, named in (('names-p', True), ('names-nothing', False)):
            with self.subTest(case=case):
                self.conn.close(); self.setUp()
                p = self.file('p.md', b'p Y\n')
                request = self.requested('PC', self.based(), p)
                objection = self.reviewed(request, 'must-revise', *((p,) if named else ()),
                                          kind='design', sender=SECOND)
                self.refused(self.issue(objection), p, objection)
        # Control: without the CC, Engine refuses the second implementer's reply at submit.
        self.conn.close(); self.setUp()
        p = self.file('p.md', b'p Y\n')
        request = self.requested('P', self.based(), p)
        with self.assertRaises(errors.EngineError) as caught:
            self.reviewed(request, 'must-revise', p, kind='design', sender=SECOND)
        self.assertEqual(caught.exception.code, 'E-PARENT-NOT-MEMBER')

    def test_design_refusal_naming_nothing_holds_context_plan(self):
        p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
        refusal = self.reviewed(self.requested('D', self.based(), q, p), 'must-revise', kind='design')
        lifted = self.reviewed(self.requested('D', refusal, q), 'approve', q, kind='design')
        approval = self.reviewed(self.requested('P', lifted, p), 'approve', p)
        # A design objection is lifted only by a qualifying DESIGN-REVIEW approve: a hold the
        # Planner clears by a design request.
        self.refused(self.issue(approval), p, refusal)
        request = self.requested('D', approval, p)
        renewed = self.submit(draft(kind='DESIGN-REVIEW', verdict='approve', parent=request.relay_id,
                                    to=PLANNER.address, cc=self.issuers, refs=self.refs(p)), IMPL)
        impl = self.issue(renewed)
        self.assertIn(str(p), self.claim_artifacts(impl, 'design')[('design', renewed.relay_id)])
        self.code('verification-pending')
        self.verify(renewed)
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_plan_approve_cannot_move_design_lock(self):
        _, dc = self.designed()
        self.design.write_bytes(b'design Y\n')
        approval = self.reviewed(self.ask('plan', dc, self.plan, self.design), 'approve', self.plan, self.design)
        impl = self.issue(approval)
        # K5's witness: with `decides` answering yes for every review, the plan approve's lock at
        # Y replaced the design lock with a plan claim and `act` allowed at Y.
        self.assertEqual(self.claim_artifacts(impl), {('design', dc.relay_id): [str(self.design)],
                                                      ('plan', approval.relay_id): [str(self.plan)]})
        self.code('design-changed')
        self.design.write_bytes(b'design X\n')
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_named_refusal_of_locked_bytes_refuses(self):
        p = self.file('p.md', b'p X\n')
        approval = self.reviewed(self.requested('P', self.go(), p), 'approve', p)
        objection = self.reviewed(self.requested('P', approval, p), 'must-revise', p)
        # aa84dbc: a review of content already locked decided nothing, so the slip issued under
        # the first claim and `act` allowed.
        self.refused(self.issue(objection), p, objection)
        request = self.requested('P', objection, p)
        repeat = self.submit(draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                                   to=PLANNER.address, cc=self.issuers, refs=self.refs(p)), IMPL)
        impl = self.issue(repeat)
        self.assertEqual(self.claim_artifacts(impl), {('plan', repeat.relay_id): [str(p)]})
        self.code('verification-pending')
        self.verify(repeat)
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_refusal_of_unheld_named_path_refuses(self):
        p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
        approval = self.reviewed(self.requested('P', self.based(), p), 'approve', p)
        objection = self.reviewed(self.requested('D', approval, p), 'must-revise', q, kind='design')
        # aa84dbc denies too, naming p: there the DESIGN holding p made p a design path.
        self.refused(self.issue(objection), q, objection)

    def test_reapprove_of_locked_bytes_keeps_verified_claim(self):
        # Erratum 7 X2 (K7): H's approve of p at X is verified; a second PLAN holding p at X is
        # approved by H and left unverified (`reviewed` would verify it, so it is submitted
        # directly).  Re-approving content already locked keeps the older claim, so the slip binds
        # p in the first approve's claim and `act` allows; under K7 the unverified claim replaces it.
        p = self.file('p.md', b'p X\n')
        approval = self.reviewed(self.requested('P', self.go(), p), 'approve', p)
        request = self.requested('P', approval, p)
        again = self.submit(draft(kind='PLAN-REVIEW', verdict='approve', parent=request.relay_id,
                                  to=PLANNER.address, cc=self.issuers, refs=self.refs(p)), IMPL)
        impl = self.issue(again)
        self.assertEqual(self.claim_artifacts(impl), {('plan', approval.relay_id): [str(p)]})
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_newer_verification_objection_on_same_bytes_holds(self):
        p = self.file('p.md', b'p X\n')
        approval = self.reviewed(self.requested('P', self.go(), p), 'approve', p)
        request = self.requested('P', approval, p)
        again = self.answer(request, 'approve', p)
        self.verify(again, 'must-revise')
        self.issue(again)
        answer = self.act()
        self.assertEqual(answer.outcome, 'held',
                         'outcome=%s code=%s' % (answer.outcome, answer.code))
        self.assertEqual(answer.code, 'authority.verification-objected')

    def test_mis_kinded_approve_pins_bytes(self):
        x, y = b'p X\n', b'p Y\n'
        # (PIN) H's DESIGN-REVIEW approve under a PLAN locks p at X and decides nothing; p is then
        # flipped and referenced by a DESIGN holding p at Y with no response.  aa84dbc bound p by
        # its newest reference at Y and `act` allowed there.
        p = self.file('p.md', x)
        pin = self.reviewed(self.requested('P', self.based(), p), 'approve', p, kind='design')
        p.write_bytes(y)
        impl = self.issue(self.requested('D', pin, p))
        self.assertEqual(self.digest_artifacts(impl)[str(p)], sha(x))
        self.assertIn(str(p), self.code('plan-changed').reason)
        p.write_bytes(x)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # A pin never moves a decided path: H's PLAN-REVIEW approve locks p at X (D1), then H's
        # DESIGN-REVIEW approve under a PLAN holding p at Y locks it there and decides nothing.
        self.conn.close(); self.setUp()
        p = self.file('p.md', x)
        approval = self.reviewed(self.requested('P', self.based(), p), 'approve', p)
        p.write_bytes(y)
        impl = self.issue(self.reviewed(self.requested('P', approval, p), 'approve', p, kind='design'))
        self.assertIn(('plan', approval.relay_id), self.claim_artifacts(impl, 'plan'))
        self.assertNotIn(str(p), self.digest_artifacts(impl))
        self.assertIn(str(p), self.code('plan-changed').reason)
        p.write_bytes(x)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # Control: H's verdictless review in place of the approve locks nothing, so p binds by its
        # newest reference at Y (erratum 3 r2 B1) and `act` allows there.
        self.conn.close(); self.setUp()
        p = self.file('p.md', x)
        request = self.requested('P', self.based(), p)
        verdictless = self.submit(draft(kind='DESIGN-REVIEW', needs='review', parent=request.relay_id,
                                        to=PLANNER.address, refs=self.refs(p)), IMPL)
        p.write_bytes(y)
        impl = self.issue(self.requested('D', verdictless, p))
        self.assertEqual(self.digest_artifacts(impl)[str(p)], sha(y))
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_unlocked_approve_does_not_move_pin(self):
        # (PIN-V's shape) the pin at X, then p flipped and H's PLAN-REVIEW approve naming p at Y as
        # a reply to H's own DESIGN-REVIEW, whose parent holds no snapshot, so it locks nothing.
        # aa84dbc bound p by its newest reference at Y and `act` allowed there.
        x, y = b'p X\n', b'p Y\n'
        p = self.file('p.md', x)
        pin = self.reviewed(self.requested('P', self.based(), p), 'approve', p, kind='design')
        p.write_bytes(y)
        unlocked = self.reviewed(pin, 'approve', p)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM snapshots WHERE locked_by = ?',
                                           (unlocked.relay_id,)).fetchone()[0], 0)
        impl = self.issue(unlocked)
        self.assertIn(str(p), self.code('plan-changed').reason)
        p.write_bytes(x)
        self.assertEqual(self.act().slip_id, self.issued(impl))

    def test_other_seat_lock_pins_bytes(self):
        # (OPIN) D2 holding p at X answered approve by the second implementer, then p flipped and a
        # SITREP referencing p at Y.  aa84dbc bound p by its newest reference at Y and allowed there.
        x, y, z = b'p X\n', b'p Y\n', b'p Z\n'
        p = self.file('p.md', x)
        other = self.reviewed(self.requested('D2', self.based(), p), 'approve', p, kind='design', sender=SECOND)
        p.write_bytes(y)
        impl = self.issue(self.requested('S', other, p))
        self.assertEqual(self.digest_artifacts(impl)[str(p)], sha(x))
        self.assertIn(str(p), self.code('plan-changed').reason)
        p.write_bytes(x)
        self.assertEqual(self.act().slip_id, self.issued(impl))
        # The holder's pin outranks a newer pin by another seat: H's DESIGN-REVIEW approve under a
        # PLAN pins p at X, then p flipped to Z and the second implementer approves it under D2.
        # aa84dbc allowed at Z, the newest reference.
        self.conn.close(); self.setUp()
        p = self.file('p.md', x)
        pin = self.reviewed(self.requested('P', self.based(), p), 'approve', p, kind='design')
        p.write_bytes(z)
        impl = self.issue(self.reviewed(self.requested('D2', pin, p), 'approve', p, kind='design', sender=SECOND))
        self.assertEqual(self.digest_artifacts(impl)[str(p)], sha(x))
        self.assertIn(str(p), self.code('plan-changed').reason)
        p.write_bytes(x)
        self.assertEqual(self.act().slip_id, self.issued(impl))

    # Section E: every lineage of up to two rounds after the prefix.

    def test_named_lineages_keep_the_property(self):
        from relay_engine.tests.lineage_tier import lineage_space

        self.assertEqual(len(LINEAGE_NAMED_WORDS), 20)
        record = lineage_space(self, LINEAGE_SUITE_ROUNDS, words=LINEAGE_NAMED_WORDS.values())
        self.assertEqual(record['admitted'] + record['refused'], len(LINEAGE_NAMED_WORDS))
        self.assertEqual(record['violations'], [],
                         'named lineages violate the property: %s' % json.dumps(record, default=repr))


class TestRuleH(SlipStore):
    """Real admissions for project/path/content standing objections and their cost.

    Waivers use the operator writer; no Authority rows are fixture-inserted.
    Platform sender admission and the records slice remain outside this fixture.
    """
    file = TestErratum6.file
    refs = TestErratum6.refs
    requested = TestErratum6.requested
    reviewed = TestErratum6.reviewed

    def digest_objection_fixture(self, case, *, pending_holder=False):
        """An off-lineage claim and a real slip binding its bytes by digest."""
        claims.assign_reviewer(self.conn, SECOND.team_instance_id, REVIEWER.seat_id,
                               'operator', self.clock)
        if case == 'git':
            clean_git_environment(self)
            repo = Repo({'p.md': 'p X\n'})
            self.addCleanup(repo.close)
            self.directory.checkouts['bivpak'] = repo.path
            p = Path(repo.path) / 'p.md'
            references = ('bivpak@main:p.md',)
            request = self.submit(draft(kind='PLAN', needs='review', to=SECOND.address,
                                        refs=references, bundle='other-git'))
            claim = self.submit(draft(kind='PLAN-REVIEW', verdict='approve',
                                      parent=request.relay_id, to=PLANNER.address,
                                      cc=self.issuers, refs=references), SECOND)
            self.verify(claim)
            q = self.file('q.md', b'q X\n')
            own = self.reviewed(self.requested('P', self.go(), q), 'approve', q)
            issued = self.issue(own, refs=references)
            self.assertEqual(self.digest_artifacts(issued)['repo-1:p.md'],
                             git(repo.path, 'rev-parse', 'main:p.md'))
            options, artifact = {'path': str(repo.path)}, 'repo-1:p.md'
        else:
            p, q = self.file('p.md', b'p X\n'), self.file('q.md', b'q X\n')
            review = self.answer if pending_holder else self.reviewed
            own = review(self.requested('P', self.go(), q), 'approve', q)
            if case == 'pin':
                claim = self.reviewed(self.requested('D2', own, p), 'approve', p,
                                      kind='design', sender=SECOND)
                issued = self.issue(claim)
            else:
                claim = None
                issued = self.issue(own, refs=self.refs(p))
            options, artifact = {}, str(p)
        self.assertIn(artifact, self.digest_artifacts(issued))
        self.assertNotIn(artifact, [lock for locks in self.claim_artifacts(issued).values()
                                    for lock in locks])
        return p, claim, options

    def test_digest_bound_human_decision_unblocks_operator(self):
        # D-UNBLOCK: an off-lineage human decision names the operator.
        _, claim, options = self.digest_objection_fixture('pin')
        objection = self.verify(claim, 'human-decision-required')
        answer = self.act(**options)
        self.assertEqual(answer.outcome, 'held')
        self.assertEqual(answer.code, 'authority.verification-objected')
        self.assertIn(objection.relay_id, answer.reason)
        self.assertEqual(answer.unblocker, 'operator')

    def test_digest_objection_named_before_pending_claim(self):
        # D-AFTER: the digest objection precedes a bound claim's pending state.
        _, claim, options = self.digest_objection_fixture('pin', pending_holder=True)
        objection = self.verify(claim, 'must-revise')
        answer = self.act(**options)
        self.assertEqual(answer.outcome, 'held')
        self.assertEqual(answer.code, 'authority.verification-objected')
        self.assertIn(objection.relay_id, answer.reason)

    def test_act_entry_traces_one_question_per_digest_path(self):
        # D-TWICE: the real reader and entry ask each H statement once per path.
        from collections import Counter
        from relay_engine import authority
        p, _, _ = self.digest_objection_fixture('pin')
        q = p.with_name('q.md')
        def traced(call):
            statements = []
            self.conn.set_trace_callback(statements.append)
            try:
                return call(), statements
            finally:
                self.conn.set_trace_callback(None)
        expected = []
        for path in (q, p):
            _, statements = traced(lambda: claims.standing_objection(
                self.conn, 'p', str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
            self.assertEqual(len(statements), 3, statements)
            expected.extend(statements)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            answer, statements = traced(lambda: authority.act_entry(
                self.conn, IMPL.seat_id, slips.Effect('commit', 'repo-1', 'work'), self.clock))
            self.assertEqual(answer.outcome, 'allow', statements)
            actual = Counter(statements)
            for sql, count in Counter(expected).items():
                self.assertEqual(actual[sql], count, statements)
        finally:
            self.conn.rollback()

    def test_objection_reaches_digest_bound_path(self):
        # D-SKIP / D-GIT: the set must reach pins and Git-spelled digest bindings.
        for ordinal, case in enumerate(('pin', 'git', 'no-claim')):
            with self.subTest(case=case):
                if ordinal:
                    self.doCleanups(); self.setUp()
                p, claim, options = self.digest_objection_fixture(case)
                if claim is None:
                    self.assertEqual(self.act(**options).outcome, 'allow')
                    continue
                objection = self.verify(claim, 'must-revise')
                answer = self.act(**options)
                self.assertEqual(answer.outcome, 'held')
                self.assertEqual(answer.code, 'authority.verification-objected')
                self.assertIn(objection.relay_id, answer.reason)
                self.assertEqual(answer.unblocker, PLANNER.address)
                self.verify(claim)
                self.assertEqual(self.act(**options).outcome, 'allow')
                if case == 'git':
                    p.unlink()
                    self.assertIn('repo-1:p.md', self.code('plan-changed', **options).reason)

    def test_rule_h_reads_one_observation(self):
        # D-SPLIT: X passes the check; a later observation Y cannot choose H's set.
        for ordinal, case in enumerate(('pin', 'git')):
            with self.subTest(case=case):
                if ordinal:
                    self.doCleanups(); self.setUp()
                p, claim, options = self.digest_objection_fixture(case)
                self.verify(claim, 'must-revise')
                calls = []
                def read(path, algorithm='sha256'):
                    if Path(path) == p:
                        calls.append(algorithm)
                        data = b'p X\n' if len(calls) == 1 else b'p Y\n'
                    else:
                        data = Path(path).read_bytes()
                    def hashed(name):
                        if name == 'sha256':
                            return hashlib.sha256(data).hexdigest()
                        blob = b'blob %d\0' % len(data) + data
                        return {'git-sha1': hashlib.sha1, 'git-sha256': hashlib.sha256}[name](blob).hexdigest()
                    return tuple(hashed(name) for name in algorithm) if isinstance(algorithm, tuple) else hashed(algorithm)
                answer = self.act(digest=read, **options)
                self.assertEqual(answer.outcome, 'held')
                self.assertEqual(answer.code, 'authority.verification-objected')
                self.assertEqual(len(calls), 1)

    def test_rule_h_stays_in_its_project(self):
        # S-PROJ-W / S-PROJ-V: foreign frontiers cannot lift, nor verdicts hold.
        for ordinal, case in enumerate(('waiver', 'verdict')):
            with self.subTest(case=case):
                if ordinal:
                    self.doCleanups(); self.setUp()
                _, own, _ = self.build(design=False)
                if case == 'waiver':
                    self.verify(own, 'must-revise')
                references = self.refs(self.plan)
                request = self.submit(draft(kind='PLAN', needs='review', to=QIMPL.address,
                                            refs=references), FOREIGN)
                foreign = self.submit(draft(kind='PLAN-REVIEW', verdict='approve',
                                            parent=request.relay_id, to=FOREIGN.address,
                                            refs=references), QIMPL)
                if case == 'waiver':
                    for _ in range(20):
                        self.submit(draft(to=FOREIGN.address), FOREIGN)
                    highest = {project: handle for project, handle in self.conn.execute(
                        'SELECT project_id, max(handle) FROM relays GROUP BY project_id')}
                    self.assertGreater(highest['q'], highest['p'])
                    claims.waive(self.conn, foreign.relay_id, 'plan', 'operator', 'foreign', self.clock)
                    self.assertEqual(self.act().outcome, 'held')
                else:
                    self.verify(foreign, 'must-revise', sender=QREVIEWER)
                    self.assertEqual(self.act().outcome, 'allow')

    def test_unblocker_follows_the_objecting_claim(self):
        # H-UNBLOCK-BOUND: the approved bound claim supplies no objection verdict.
        for ordinal, verdict in enumerate(('human-decision-required', 'must-revise')):
            with self.subTest(verdict=verdict):
                if ordinal:
                    self.doCleanups(); self.setUp()
                _, own, _ = self.build(design=False)
                newer = self.answer(self.requested('P', own, self.plan), 'approve', self.plan)
                self.verify(newer, verdict)
                answer = self.code('verification-objected')
                self.assertEqual(answer.unblocker, 'operator' if ordinal == 0 else PLANNER.address)

    def test_waiver_of_older_claim_after_newer_objection_lifts(self):
        # H-ORDER-CLAIM / H-GE: the waiver's frontier equals the objection handle.
        _, first, _ = self.build(design=False)
        newer = self.answer(self.requested('P', first, self.plan), 'approve', self.plan)
        self.verify(newer, 'must-revise')
        claims.waive(self.conn, first.relay_id, 'plan', 'operator', 'later waiver', self.clock)
        self.assertEqual(self.act().outcome, 'allow')

    def test_newest_waiver_sets_the_frontier(self):
        # H-MIN-FRONTIER: the later of two waivers lifts the intervening objection.
        _, first, _ = self.build(design=False)
        claims.waive(self.conn, first.relay_id, 'plan', 'operator', 'first waiver', self.clock)
        second = self.answer(self.requested('P', first, self.plan), 'approve', self.plan)
        self.verify(second, 'must-revise')
        third = self.answer(self.requested('P', second, self.plan), 'approve', self.plan)
        claims.waive(self.conn, third.relay_id, 'plan', 'operator', 'last waiver', self.clock)
        self.assertEqual(self.act().outcome, 'allow')

    def test_newest_of_two_standing_objections_is_named(self):
        # H-OLDEST: name the newest standing review across two different seats.
        _, first, _ = self.build(design=False)
        self.verify(first, 'must-revise')
        claims.assign_reviewer(self.conn, IMPL.team_instance_id, SECOND.seat_id, 'operator', self.clock)
        second = self.answer(self.requested('P', first, self.plan), 'approve', self.plan)
        newest = self.verify(second, 'human-decision-required', sender=SECOND)
        answer = self.code('verification-objected')
        self.assertIn(newest.relay_id, answer.reason)
        self.assertEqual(answer.unblocker, 'operator')

    def test_no_reviewer_reason_names_its_claim(self):
        # NR-REQUEST: a non-answering recorded reviewer leaves the claim to name.
        _, claim, _ = self.build(design=False, verified=False)
        request = self.request(claim)[0]
        self.assertIsNotNone(request)
        self.assertNotEqual(request, claim.relay_id)
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (REVIEWER.seat_id,))
        self.conn.commit()
        answer = self.code('verification-pending')
        self.assertIn(claim.relay_id, answer.reason)

    def test_non_reviewer_verdict_does_not_lift(self):
        # H-SENDER: membership allows admission, but only the reviewer decides H.
        file_system = submit.file_system_relay
        def with_member(conn, ctx, data):
            return file_system(conn, ctx, data.replace(b'NEEDS: review\n',
                               ('CC: ' + OTHER.address + '\nNEEDS: review\n').encode()))
        with mock.patch.object(submit, 'file_system_relay', side_effect=with_member):
            _, claim, _ = self.build(design=False)
        self.verify(claim, 'must-revise')
        self.verify(claim, sender=OTHER)
        self.assertEqual(self.act().outcome, 'held')

    def test_reviewer_approve_after_own_objection_lifts(self):
        # M1: newest effective verdict is per reviewer across same-byte claims.
        p = self.file('p.md', b'p X\n')
        first = self.answer(self.requested('P', self.go(), p), 'approve', p)
        self.verify(first, 'must-revise')
        newer = self.reviewed(self.requested('P', first, p), 'approve', p)
        self.issue(newer)
        self.assertEqual(self.act().outcome, 'allow')

    def test_waiver_older_than_objection_does_not_lift(self):
        # M2: an older waiver cannot dismiss a later objection.
        p = self.file('p.md', b'p X\n')
        first = self.answer(self.requested('P', self.go(), p), 'approve', p)
        claims.waive(self.conn, first.relay_id, 'plan', 'operator', 'waive first', self.clock)
        newer = self.answer(self.requested('P', first, p), 'approve', p)
        self.verify(newer, 'must-revise')
        self.issue(newer)
        answer = self.act()
        self.assertEqual(answer.outcome, 'held')
        self.assertEqual(answer.code, 'authority.verification-objected')

    def test_waiver_newer_than_objection_lifts(self):
        # M4: position, not the fixed raw waiver clock, dismisses the old objection.
        p = self.file('p.md', b'p X\n')
        first = self.answer(self.requested('P', self.go(), p), 'approve', p)
        objection = self.verify(first, 'must-revise')
        newer = self.answer(self.requested('P', first, p), 'approve', p)
        objection_time = self.conn.execute('SELECT created_at FROM relays WHERE id = ?',
                                           (objection.relay_id,)).fetchone()[0]
        self.assertLessEqual(self.clock, objection_time)
        claims.waive(self.conn, newer.relay_id, 'plan', 'operator', 'waive newer', self.clock)
        self.issue(newer)
        self.assertEqual(self.act().outcome, 'allow')

    def test_objection_to_non_deciding_approve_holds(self):
        # IN: a non-holder's recorded claim remains in the same-byte set.
        p = self.file('p.md', b'p X\n')
        other = self.reviewed(self.requested('P', self.go(), p, to=SECOND), 'approve', p, sender=SECOND)
        holder = self.reviewed(self.requested('P', other, p), 'approve', p)
        objection = self.verify(other, 'must-revise')
        self.issue(holder)
        answer = self.act()
        self.assertEqual(answer.outcome, 'held')
        self.assertEqual(answer.code, 'authority.verification-objected')
        self.assertIn(objection.relay_id, answer.reason)

    def test_objection_filed_after_issue_holds(self):
        # LX: answer-time reads include objections filed after the slip issued.
        p = self.file('p.md', b'p X\n')
        first = self.reviewed(self.requested('P', self.go(), p), 'approve', p)
        issued = self.issue(first)
        newer = self.answer(self.requested('P', first, p), 'approve', p)
        self.verify(newer, 'must-revise')
        self.assertEqual(self.act().outcome, 'held')
        self.verify(newer)
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, self.issued(issued))

    def test_objection_on_waived_claim_does_not_mask_other_objection(self):
        # Excluding waived claims must precede newest-per-reviewer selection.
        for control in (False, True):
            with self.subTest(control=control):
                if control:
                    self.conn.close()
                    self.setUp()
                p = self.file('p.md', b'p X\n')
                first = self.answer(self.requested('P', self.go(), p), 'approve', p)
                claims.waive(self.conn, first.relay_id, 'plan', 'operator', 'waive first', self.clock)
                newer = self.answer(self.requested('P', first, p), 'approve', p)
                objection = self.verify(newer, 'must-revise')
                self.issue(newer)
                self.assertEqual(self.act().outcome, 'held')
                if control:
                    self.verify(newer)
                    self.assertEqual(self.act().outcome, 'allow')
                else:
                    self.verify(first, 'must-revise')
                    answer = self.act()
                    self.assertEqual(answer.outcome, 'held')
                    self.assertIn(objection.relay_id, answer.reason)

    def test_held_reasons_name_their_relay(self):
        # A correct code with no actionable relay in its reason is insufficient.
        p = self.file('p.md', b'p X\n')
        first = self.answer(self.requested('P', self.go(), p), 'approve', p)
        objection = self.verify(first, 'must-revise')
        self.issue(first)
        self.assertIn(objection.relay_id, self.act().reason)
        self.conn.close()
        self.setUp()
        p = self.file('p.md', b'p X\n')
        pending = self.answer(self.requested('P', self.go(), p), 'approve', p)
        self.issue(pending)
        self.assertIn(pending.relay_id, self.act().reason)

    def test_objection_elsewhere_leaves_answer(self):
        # H-PATH/H-CONTENT: neither coordinate alone identifies the locked bytes.
        for other_path in (False, True):
            with self.subTest(other_path=other_path):
                if other_path:
                    self.conn.close()
                    self.setUp()
                p = self.file('p.md', b'p X\n')
                first = self.reviewed(self.requested('P', self.go(), p), 'approve', p)
                self.issue(first)
                q = self.file('q.md', b'p X\n') if other_path else p
                if not other_path:
                    p.write_bytes(b'p Y\n')
                newer = self.answer(self.requested('P', first, q), 'approve', q)
                self.verify(newer, 'must-revise')
                p.write_bytes(b'p X\n')
                self.assertEqual(self.act().outcome, 'allow')

    def test_waiver_outranks_later_verdict_on_its_claim(self):
        # H-OWN: later verdicts on the waived claim cannot stand.
        p = self.file('p.md', b'p X\n')
        first = self.answer(self.requested('P', self.go(), p), 'approve', p)
        claims.waive(self.conn, first.relay_id, 'plan', 'operator', 'waive', self.clock)
        self.verify(first, 'must-revise')
        issued = self.issue(first)
        self.assertEqual(self.act().outcome, 'allow')
        self.assertEqual(self.act().slip_id, self.issued(issued))

    def test_act_traces_rule_h_reads_once_per_lock(self):
        # H-REPEAT: correct answers must not hide two calls for each lock.
        from collections import Counter
        for count in (1, 2):
            with self.subTest(locks=count):
                if count == 2:
                    self.conn.close()
                    self.setUp()
                paths = [self.file('p%d.md' % i, b'X\n') for i in range(count)]
                claim = self.reviewed(self.requested('P', self.go(), *paths), 'approve', *paths)
                self.issue(claim)
                def traced(call):
                    statements = []
                    self.conn.set_trace_callback(statements.append)
                    try:
                        value = call()
                    finally:
                        self.conn.set_trace_callback(None)
                    return value, statements
                expected_h = []
                for path in paths:
                    _, statements = traced(lambda: claims.standing_objection(
                        self.conn, 'p', str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
                    self.assertEqual(len(statements), 3, statements)
                    expected_h.extend(statements)
                _, expected_state = traced(lambda: claims.verification_state(self.conn, claim.relay_id))
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    answer, whole = traced(lambda: slips.act(
                        self.conn, IMPL.seat_id, slips.Effect('commit', 'repo-1', 'work'), self.clock,
                        digest=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()))
                    self.assertEqual(answer.outcome, 'allow', whole)
                    actual = Counter(whole)
                    for statement, occurrences in Counter(expected_h).items():
                        self.assertEqual(actual[statement], occurrences, whole)
                    for statement, occurrences in Counter(expected_state).items():
                        self.assertEqual(actual[statement], occurrences, whole)
                finally:
                    self.conn.rollback()

    def test_project_orchestrator_revokes_slip_it_did_not_issue(self):
        # RV-ORCH/RV-PROJ: same-project delegation, with a foreign-project control.
        _, _, issued = self.build(design=False)
        slip_id = self.issued(issued)
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES ('seat-q-orch', 'tq', 'orchestrator-planner', 'occupied')")
        self.conn.commit()
        refused = slips.revoke(self.conn, slip_id, 'seat-q-orch', 'foreign', self.clock)
        self.assertEqual(refused.code, 'authority.not-issuer')
        allowed = slips.revoke(self.conn, slip_id, ORCH.seat_id, 'project orchestrator', self.clock)
        self.assertEqual(allowed.outcome, 'allow')
        answer = self.act()
        self.assertEqual(answer.outcome, 'deny')
        self.assertEqual(answer.code, 'authority.slip-invalid')


class TestSupersede(SlipStore):
    """Issuance replaces only the holder's live implement slips of the same key."""

    def state(self, sid):
        return self.conn.execute('SELECT state, end_reason, ended_at, revision FROM slips WHERE id = ?',
                                 (sid,)).fetchone()

    def events(self, sid):
        return self.conn.execute('SELECT revision, event, at, by_seat_id, by_special, detail'
                                 ' FROM slip_events WHERE slip_id = ? ORDER BY revision', (sid,)).fetchall()

    def test_issue_supersedes_same_bundle_and_repository(self):
        _, claim, impl = self.build(design=False)
        first = self.issued(impl)
        self.assertEqual(self.act().slip_id, first)
        self.plan.write_bytes(b'plan Y\n')
        fresh = self.answer(self.ask('plan', claim, self.plan), 'approve', self.plan)
        self.verify(fresh)
        last = self.issue(fresh)
        newest = self.issued(last)
        at = self.conn.execute('SELECT issued_at FROM slips WHERE id = ?', (newest,)).fetchone()[0]
        self.assertEqual(self.state(first), ('ended', 'superseded', at, 2))
        rows = self.events(first)
        self.assertEqual([(r[0], r[1]) for r in rows], [(1, 'issue'), (2, 'end')])
        self.assertEqual(rows[1], (2, 'end', at, PLANNER.seat_id, None,
                                  json.dumps({'superseded_by': newest}, sort_keys=True, separators=(',', ':'))))
        self.assertEqual(self.state(newest), ('live', None, None, 1))
        self.assertEqual([(r[0], r[1]) for r in self.events(newest)], [(1, 'issue')])
        answer = self.act()
        self.assertEqual((answer.outcome, answer.slip_id), ('allow', newest))
        self.assertEqual(slips.revoke(self.conn, newest, PLANNER.seat_id, 'stop', self.clock).outcome, 'allow')
        self.plan.write_bytes(b'plan X\n')
        answer = self.code('slip-invalid')
        self.assertEqual((answer.outcome, answer.slip_id), ('deny', newest))

    def test_issue_keeps_other_bundle_and_repository_live(self):
        _, _, other_bundle = self.build(bundle='other', design=False)
        _, claim, _ = self.build(bundle='same', design=False, issue=False)
        other_repo = self.issue(claim, repo='other', git_state=replace(GIT_STATE, repository_id='repo-2'))
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        other_seat = self.submit(with_repo(draft(kind='IMPL', needs='act', bundle='same',
                                       to=SECOND.address)), operator)
        saved = [self.issued(r) for r in (other_bundle, other_repo, other_seat)]
        last = self.issue(claim)
        self.issued(last)
        for sid in saved:
            with self.subTest(sid=sid):
                self.assertEqual(self.state(sid), ('live', None, None, 1))
                self.assertEqual([(r[0], r[1]) for r in self.events(sid)], [(1, 'issue')])

    def test_issue_leaves_revoked_slip_revoked(self):
        _, claim, impl = self.build(design=False)
        sid = self.issued(impl)
        self.assertEqual(slips.revoke(self.conn, sid, PLANNER.seat_id, 'stop', self.clock).outcome, 'allow')
        before = self.state(sid), self.events(sid)
        last = self.issue(claim)
        self.assertEqual(self.state(sid), before[0])
        self.assertEqual(self.events(sid), before[1])
        self.assertEqual(self.state(sid)[:2], ('revoked', 'revoked'))
        self.assertEqual(self.state(sid)[3], 2)
        self.assertEqual([(r[0], r[1]) for r in self.events(sid)], [(1, 'issue'), (2, 'revoke')])
        self.assertEqual(self.state(self.issued(last)), ('live', None, None, 1))

    def test_refused_issuance_supersedes_nothing(self):
        for ordinal, case in enumerate(('evidence', 'closed-issuer', 'closed-delegation')):
            with self.subTest(case=case):
                if ordinal:
                    self.doCleanups()
                    self.setUp()
                issuer = ORCH if case == 'closed-issuer' else PLANNER
                _, claim, impl = self.build(issuer=issuer, design=False)
                sid = self.issued(impl)
                if case == 'evidence':
                    refusal = self.answer(self.ask('plan', claim, self.plan), 'must-revise', self.plan)
                    refused = self.issue(refusal)
                else:
                    self.conn.execute("UPDATE team_instances SET closed_at = 2 WHERE id = 't3'")
                    self.conn.commit()
                    refused = self.issue(claim, issuer)
                self.assertIsNone(self.slip(refused))
                self.assertEqual(self.state(sid), ('live', None, None, 1))
                self.assertEqual([(r[0], r[1]) for r in self.events(sid)], [(1, 'issue')])
                answer = self.act()
                self.assertEqual((answer.outcome, answer.slip_id), ('allow', sid))


class TestRepositoryRule(SlipStore):
    def test_issue_binds_named_repository(self):
        # RR-M1: submit's ambient git state cannot choose the slip repository.
        for state in (GIT_STATE, None):
            with self.subTest(git_state=state):
                self.doCleanups()
                self.setUp()
                _, claim, _ = self.build(issue=False, design=False)
                issued = self.issue(claim, repo='other', git_state=state)
                row = self.conn.execute('SELECT id, repository_id FROM slips WHERE issued_in = ?',
                                        (issued.relay_id,)).fetchone()
                repository = row[1] if row else None
                self.assertEqual(repository, 'repo-2')
                sid = row[0]
                answer = self.act(repo='repo-2')
                self.assertEqual((answer.outcome, answer.slip_id), ('allow', sid))
                wrong = self.act(repo='repo-1')
                self.assertEqual((wrong.outcome, wrong.code), ('deny', 'authority.slip-invalid'))
                self.assertIn('repo-2', wrong.reason)

    def test_issue_ignores_git_state(self):
        _, claim, _ = self.build(issue=False, design=False)
        issued = self.issue(claim, git_state=replace(GIT_STATE, repository_id='repo-2'))
        sid = self.issued(issued)
        repository = self.conn.execute('SELECT repository_id FROM slips WHERE id = ?',
                                       (sid,)).fetchone()[0]
        self.assertEqual(repository, 'repo-1')

    def test_imported_impl_issues_no_slip(self):
        # RR-M2/3 select an imported IMPL and name its missing repository;
        # RR-M4 must leave an imported REVIEW outside that diagnostic.
        from relay_engine import imports
        for kind in ('REVIEW', 'native-none', 'IMPL'):
            with self.subTest(kind=kind):
                self.doCleanups()
                self.setUp()
                _, claim, _ = self.build(issue=False, design=False)
                if kind == 'native-none':
                    rid = self.submit(draft(kind='IMPL', needs='none', parent=claim.relay_id,
                                            to=IMPL.address)).relay_id
                else:
                    self.clock = self.conn.execute("SELECT max(created_at) + 1 FROM relays WHERE project_id='p'").fetchone()[0]
                    relay = imports.ImportedRelay(
                        created_at=self.clock, kind=kind, verdict=None, subject='Imported go-ahead',
                        sender=PLANNER.seat_id, sender_role=PLANNER.role,
                        team_instance_id=PLANNER.team_instance_id, to=(IMPL.seat_id,), cc=(),
                        parent_id=claim.relay_id, parent_spelling=claim.relay_id,
                        body=b'imported go-ahead', header_bytes=0,
                        header_sha256=hashlib.sha256(b'').hexdigest())
                    self.conn.execute('BEGIN IMMEDIATE')
                    try:
                        rid = imports.import_relay_step(self.conn, 'p', relay, marked_at_ms=self.clock)
                        self.conn.execute('INSERT INTO imported_relays (relay_id, root_uuid, seq, stamp, zone,'
                                          ' utc_offset_s, rendered_path, origin, headers_json, source_sha256,'
                                          ' delivery_marked_at) VALUES (?, ?, 1, ?, ?, 0, ?, ?, ?, ?, ?)',
                                          (rid, rid, '20261115-000000', 'UTC', 'old/go-ahead.md', '2.9.7',
                                           '{}', hashlib.sha256(relay.body).hexdigest(), self.clock))
                        self.conn.commit()
                    except BaseException:
                        self.conn.rollback()
                        raise
                row = self.conn.execute('SELECT needs, repository_id FROM relays WHERE id = ?',
                                        (rid,)).fetchone()
                self.assertEqual(row, ('none', None))
                self.assertIsNone(self.conn.execute('SELECT id FROM slips WHERE issued_in = ?',
                                                   (rid,)).fetchone())
                answer = self.act()
                self.assertEqual((answer.outcome, answer.code, answer.unblocker),
                                 ('deny', 'authority.slip-missing', PLANNER.address))
                if kind == 'IMPL':
                    self.assertEqual(answer.reason, 'no implement slip was issued because the IMPL names no repository; an IMPL imported from 2.9.7 never issues one')
                else:
                    self.assertEqual(answer.reason, 'no implement slip exists for this seat')


class TestAnswerCodesR22(unittest.TestCase):
    def test_answer_codes_r22(self):
        from relay_engine import slips
        expected = {
            'slip-missing': 'deny', 'slip-invalid': 'deny',
            'lineage-incomplete': 'deny', 'not-issuer': 'deny',
            'plan-changed': 'held', 'design-changed': 'held',
            'verification-pending': 'held', 'verification-objected': 'held',
            'check-unavailable': 'held', 'restore-held': 'held',
            'project-stopped': 'held', 'cross-project-commit': 'deny',
            'session-not-current': 'held', 'protected-ref-push': 'deny',
            'api-merge-refused': 'deny', 'merge-head-unpinned': 'deny',
            'merge-flag-refused': 'deny', 'hook-bypass': 'deny',
            'hook-disable': 'deny', 'key-file-read': 'deny',
            'operator-endpoint': 'deny', 'operator-only': 'deny',
            'checkout-unregistered': 'deny',
        }
        self.assertEqual(slips.ANSWER_CODES,
                         {'authority.' + code: outcome for code, outcome in expected.items()})

    def test_retired_codes_absent(self):
        from relay_engine import slips, errors
        for code in ('merge-no-seat', 'checkout-not-active', 'continuation-pending'):
            with self.subTest(code=code):
                self.assertNotIn('authority.' + code, slips.ANSWER_CODES)
                self.assertNotIn('E-AUTHORITY-' + code.upper(), errors.ERRORS)


class TestAnswerWordsR22(unittest.TestCase):
    def test_word_families_match_the_inventory_exactly(self):
        from relay_engine import slips, strings

        for stem, attribute in (
            ('authority-unavailable-', 'UNAVAILABLE_CODES'),
            ('authority-not-current-', 'NOT_CURRENT_CAUSES'),
        ):
            with self.subTest(stem=stem):
                self.assertTrue(hasattr(slips, attribute), attribute)
                codes = getattr(slips, attribute)
                expected = {f'{stem}{code}-{part}' for code in codes for part in ('reason', 'remedy')}
                actual = {key for key in strings.INVENTORY if key.startswith(stem)}
                self.assertEqual(expected, actual)

    def test_unavailable_words(self):
        from relay_engine import slips
        for code, reason, remedy in [['no-service', 'the engine is not running, so this act could not be checked', 'start the engine, then try again'], ['connect-refused', 'the engine refused the connection, so this act could not be checked', 'run adt engine status, then try again'], ['deadline', 'the engine did not answer in time, so this act could not be checked', 'try again; if it repeats, run adt engine status'], ['fault', 'the check failed inside the engine', 'run adt engine status and tell the operator the fault it shows'], ['seat-found-unavailable', 'this session is a seat, and its check could not finish', "try again; a seat's act is never passed unchecked"], ['unanswerable', 'the kit could not tell whose session this is, so this act could not be checked', 'start the session again from its host, or sign it in with adt connect; the operator can run the act from their own terminal']]:
            with self.subTest(code=code):
                self.assertEqual(slips.unavailable_words(code), (reason, remedy))
        with self.assertRaises(KeyError):
            slips.unavailable_words('unknown')

    def test_session_not_current_words(self):
        from relay_engine import slips
        for cause, reason, remedy in [['ended', "this session's sign-in has ended: a newer sign-in took its seat, or the operator unseated it", "sign this session in as its seat, or commit from the seat's current session"], ['not-occupant', "the seat's occupant has changed since this session signed in", "sign this session in as its seat, or commit from the seat's current session"], ['stood-down', "this session's seat has been stood down", "sign this session in as its seat, or commit from the seat's current session"], ['team-closed', "this session's team is closed", "sign this session in as its seat, or commit from the seat's current session"], ['record-other-seat', "this session's sign-in record names another seat than the one it signed in as", "sign this session in as its seat, or commit from the seat's current session"], ['record-without-row', "this session's sign-in record was written, but its sign-in never completed", "sign this session in as its seat, or commit from the seat's current session"], ['project-unreadable', "this session's current sign-in may be in project {project}, which could not be read", 'commit again once project {project} can be read; if it is faulted, ask the operator to rejoin it']]:
            project = 'p-2' if cause == 'project-unreadable' else None
            with self.subTest(cause=cause):
                self.assertEqual(slips.session_not_current_words(cause, project),
                                 (reason.replace('{project}', 'p-2'), remedy.replace('{project}', 'p-2')))
        for cause, project in [('project-unreadable', None), ('ended', 'p-2')]:
            with self.subTest(cause=cause), self.assertRaises(ValueError):
                slips.session_not_current_words(cause, project)
        with self.assertRaises(KeyError):
            slips.session_not_current_words('unknown', None)

    def test_project_stopped_answer(self):
        from relay_engine import slips
        self.assertEqual(slips.project_stopped_answer(),
                         slips.Answer('held', 'authority.project-stopped',
                                      'this project is stopped', 'operator', None, None))
