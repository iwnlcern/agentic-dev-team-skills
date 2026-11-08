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

from relay_engine import claims, errors, queries, slips, submit
from relay_engine.tests.test_claims import ClaimStore, CommitFault, REVIEWER, SECOND
from relay_engine.tests.test_submit import PLANNER, IMPL, ORCH, OTHER, FOREIGN, draft
from relay_engine.tests.test_refs import Repo, clean_git_environment

MASTER = submit.SeatRef('seat-master', 'v3-master.master-planner', 'master-planner', 'tm', 'p')
UNRELATED = submit.SeatRef('seat-master-other', 'v3-unrelated.master-planner', 'master-planner', 'tu', 'p')
QIMPL = submit.SeatRef('seat-q-impl', 'v3-engine.implementer@q', 'implementer', 't9', 'q')
QREVIEWER = submit.SeatRef('seat-q-reviewer', 'v3.orchestrator-reviewer@q', 'orchestrator-reviewer', 'tq', 'q')


class SlipStore(ClaimStore):
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

    def issue(self, parent, issuer=PLANNER, **kwargs):
        return self.submit(draft(kind='IMPL', needs='act', parent=parent.relay_id,
                                 to=IMPL.address), issuer, **kwargs)

    def slip(self, relay):
        return self.conn.execute('SELECT id FROM slips WHERE issued_in = ?', (relay.relay_id,)).fetchone()

    def act(self, seat=IMPL, repo='repo-1', branch='work', digest=None, kind='commit', path=None):
        def read(file):
            try:
                return hashlib.sha256(Path(file).read_bytes()).hexdigest()
            except OSError:
                return None
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
        impl = self.submit(draft(kind='IMPL', needs='act', to=IMPL.address, bundle='operator'), operator)
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
        data = draft(kind='IMPL', needs='act', parent=pc.relay_id, to=IMPL.address)
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
        request = self.submit(draft(kind='PLAN', needs='review', to=QIMPL.address, refs=refs), FOREIGN)
        self.submit(draft(kind='PLAN-REVIEW', verdict='approve', to=FOREIGN.address,
                          parent=request.relay_id, refs=refs), QIMPL)
        request = self.submit(draft(kind='PLAN', needs='act', to=PLANNER.address), ORCH)
        impl = self.issue(request)
        self.assertIsNone(self.slip(impl))
        self.code('lineage-incomplete')

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
        wrong = self.plan.with_name('plan.md'); wrong.write_bytes(b'git plan\n')
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
        self.submit(draft(kind='IMPL', needs='act', to=SECOND.address), OTHER)
        self.submit(draft(kind='IMPL', needs='act', to=IMPL.address), FOREIGN)
        self.submit(draft())
        self.code('slip-missing')
        self.build()
        self.code('slip-invalid', repo='repo-2')

    def test_unreferenced_approve_issues_nothing(self):
        pc = self.approved('plan', refs=())
        self.issue(pc, ORCH)
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
        _, _, impl = self.build(); sid = self.slip(impl)[0]
        self.code('slip-invalid', repo='repo-2')
        self.act(branch='owned')
        self.assertIn('seat', self.code('slip-invalid', seat=SECOND, branch='owned').reason)
        slips.revoke(self.conn, sid, 'operator', 'stop', self.clock)
        self.assertIn('revoked', self.code('slip-invalid').reason)

    def test_merge_kinds_have_no_slip(self):
        self.build()
        for kind in ('pr-merge', 'api-ref-write'):
            answer = self.code('slip-missing', kind=kind)
            self.assertIn('merge', answer.reason)
        self.assertIn('guardrail', self.code('check-unavailable', kind='protected-push').reason)

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
        self.build(); self.assertEqual(self.act().outcome, 'allow')
        with submit.read_guard(self.conn), self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute('SELECT body FROM bodies').fetchall()

    def test_answer_shape(self):
        missing = self.code('slip-missing')
        _, _, impl = self.build(verified=False)
        held = self.code('verification-pending')
        self.assertEqual(held.slip_id, self.slip(impl)[0]); self.assertEqual(held.revision, 1)
        self.assertIsNone(missing.slip_id)

    def test_no_direct_authority_rows(self):
        tables = {'slips', 'slip_events', 'branch_bindings', 'reviewer_assignments',
                  'verification_requests', 'waivers', 'relays', 'edges', 'snapshots'}
        pattern = re.compile(r'^\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)', re.I)
        for name in ('test_claims.py', 'test_slips.py', 'test_authority_v3.py'):
            path = Path(__file__).with_name(name)
            if path.is_file():
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        match = pattern.match(node.value)
                        self.assertFalse(match and match.group(1).lower() in tables, (name, node.lineno))
