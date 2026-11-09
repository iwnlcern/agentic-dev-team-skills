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
from relay_engine import addresses
from relay_engine.tests.test_claims import BodyReader, ClaimStore, CommitFault, REVIEWER, SECOND
from relay_engine.tests.test_submit import PLANNER, IMPL, ORCH, OTHER, FOREIGN, draft
from relay_engine.tests.test_refs import Repo, clean_git_environment, git

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

    def issue(self, parent, issuer=PLANNER, refs=(), **kwargs):
        return self.submit(draft(kind='IMPL', needs='act', parent=parent.relay_id,
                                 to=IMPL.address, refs=refs), issuer, **kwargs)

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
            if algorithm == 'sha256':
                return hashlib.sha256(data).hexdigest()
            blob = b'blob %d\0' % len(data) + data
            return {'git-sha1': hashlib.sha1, 'git-sha256': hashlib.sha256}[algorithm](blob).hexdigest()
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
        self.submit(draft())
        # Each relay below differs from a diagnostic IMPL by one filter: kind, NEEDS, the TO role, the project.
        self.submit(draft(kind='SITREP', needs='act', to=IMPL.address))
        self.submit(draft(kind='IMPL', needs='none', to=IMPL.address))
        self.submit(draft(kind='IMPL', needs='act', to=PLANNER.address, cc=IMPL.address))
        resolve = self.directory.resolve
        self.directory.resolve = lambda address, sender: (IMPL if address == 'v3-cross.implementer'
                                                          else resolve(address, sender))
        crossing = self.submit(draft(kind='IMPL', needs='act', to='v3-cross.implementer'), FOREIGN)
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
        second = self.submit(draft(kind='IMPL', needs='act', parent=approval.relay_id, to=SECOND.address), ORCH)
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
                  'verification_requests', 'waivers', 'relays', 'edges', 'snapshots'}
        pattern = re.compile(r'^\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)', re.I)
        for name in ('test_claims.py', 'test_slips.py', 'test_authority_v3.py'):
            path = Path(__file__).with_name(name)
            if path.is_file():
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
        context = self.file('context.md', b'context X\n')
        go = self.go(refs=('file:' + str(context),))
        dc = self.approved('design', parent=go.relay_id)
        self.verify(dc)
        impl = self.issue(dc, refs=('file:' + str(self.plan),))
        self.assertIsNone(self.slip(impl))
        self.code('lineage-incomplete')
        # Control: one holder PLAN-REVIEW lock in the same lineage lets a slip issue.
        approval = self.answer(self.ask('plan', dc, self.plan), 'approve', self.plan)
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
                          " VALUES ('tr', 'p', 'v3-roster', 'pair', 1, 'operator')")
        roster = {}
        for role in sorted(addresses.ROLE_WORDS) + ['unlisted-role']:
            seat = submit.SeatRef('seat-roster-' + role, 'v3-roster.' + role, role, 'tr', 'p')
            self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state) VALUES (?, 'tr', ?, 'occupied')",
                              (seat.seat_id, role))
            self.conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                              " VALUES (?, ?, ?, 'v', 1)", ('o-' + seat.seat_id, seat.seat_id, 'k-' + seat.seat_id))
            roster[role] = seat
        self.conn.commit()
        resolve = self.directory.resolve
        self.directory.resolve = lambda address, sender: next(
            (seat for seat in roster.values() if seat.address == address), None) or resolve(address, sender)
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
            self.submit(draft(kind='IMPL', needs='act', to=roster[role].address, bundle='roster-' + role))
            for branch in ('free', 'bound'):
                with self.subTest(role=role, branch=branch, addressed=True):
                    self.assertEqual(self.act(seat=roster[role], branch=branch).code, 'authority.not-issuer')
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
        # A review of content already locked decides nothing for that path, whatever its verdict.
        again = self.answer(self.ask('plan', approval, self.plan), 'must-revise', self.plan)
        later = self.issue(again)
        self.assertEqual(self.claim_artifacts(later), expected)
        repeat = self.answer(self.ask('plan', again, self.plan), 'approve', self.plan)
        last = self.issue(repeat)
        self.assertEqual(self.claim_artifacts(last), expected)
        self.assertEqual(self.act().slip_id, self.issued(last))

    def test_operator_objection_waived_still_binds_digest(self):
        # Erratum 4 r2 R2's control.
        _, base, _ = self.build(issue=False)
        self.plan.write_bytes(b'plan Y\n')
        objection = self.answer(self.ask('plan', base, self.plan), 'must-revise', self.plan,
                                cc=self.issuers + ', operator')
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        impl = self.submit(draft(kind='IMPL', needs='act', parent=objection.relay_id, to=IMPL.address), operator)
        self.assertIn('objected:' + str(self.plan), self.binds(impl)['waived'])
        self.assertEqual(self.digest_artifacts(impl), {str(self.plan): sha(b'plan Y\n')})
        self.assertEqual(self.act().slip_id, self.issued(impl))
        self.plan.write_bytes(b'plan Z\n')
        self.assertIn(str(self.plan), self.code('plan-changed').reason)
