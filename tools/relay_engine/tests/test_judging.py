"""Admission evaluates only the candidate against ledger predecessors."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from relay_engine import judging, rules
from relay_engine.envelope import Envelope

TOOLS = Path(__file__).resolve().parents[2]


def body(phase='PLAN', sender='p.planner', target='p.implementer', ident='candidate', extra='', content=''):
    role = 'Pair Implementer' if sender.endswith('.implementer') else 'Pair Planner'
    authority = {'PLAN': 'plan-only', 'DESIGN': 'design-only', 'IMPL': 'implementation', 'SITREP': 'report-only',
                 'MERGE-GATE': 'merge-gated', 'DESIGN-REVIEW': 'review-only', 'PLAN-REVIEW': 'review-only'}[phase]
    return (f'ROLE: {role}\nPHASE: {phase}\nAUTHORITY: {authority}\nDISPATCH_ID: {ident}\nRUN_ID: r\n'
            f'CEREMONY_TIER: medium\nEVIDENCE_TARGET: E2\nHUMAN_GATE_REQUIRED: no\nFROM: {sender}\n'
            f'TO: {target}\nCC: r.orchestrator-reviewer\nSUBJECT: test candidate\nFINAL_GIT_STATUS_SHORT: clean\n{extra}\n{content}\n').encode()


def envelope(raw):
    f = rules.header_fields(raw.decode())
    return Envelope(f.get('PHASE', ''), f.get('ROLE', ''), f.get('AUTHORITY', ''), f.get('DISPATCH_ID', ''),
                    f.get('PARENT_DISPATCH_ID'), f.get('RUN_ID', 'r'), f.get('FROM', ''),
                    tuple(rules.split_addresses(f.get('TO'))), tuple(rules.split_addresses(f.get('CC'))),
                    f.get('STATUS'), f.get('SUBJECT', 'test'), f)


class TestJudging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '.relays/r'
        self.root.mkdir(parents=True)
        self.subject = 'PLAN-pair-planner-20261013-120000.md'

    def judge(self, raw, previous=(), deadline=None):
        corpus = judging.Corpus(tuple((i + 1, path, data) for i, (path, data) in enumerate(previous)))
        return judging.judge_candidate(corpus, self.subject, envelope(raw), raw, artifact_root=self.root,
                                       deadline=time.monotonic() + 5 if deadline is None else deadline)

    def family(self, raw, family, previous=()):
        return [f for f in self.judge(raw, previous) if f.id == family]

    def test_per_file_rules_apply_with_freshness_off(self):
        self.assertEqual(self.family(body('SITREP'), 'file.lint'), [])
        found = self.family(body('SITREP').replace(b'EVIDENCE_TARGET: E2', b'EVIDENCE_TARGET: invalid'), 'file.lint')
        self.assertEqual(len(found), 1)
        self.assertIn('EVIDENCE_TARGET', found[0].message)

    def test_impl_report_parent_not_dispatch(self):
        report = body('IMPL', 'p.implementer', 'p.planner', extra='PARENT_DISPATCH_ID: parent\nACTIONS_GIT_REF: implemented change abcdef1')
        wrong = body(ident='parent')
        found = self.family(report, 'lineage.impl-report-parent', [('parent.md', wrong)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].operands, ('parent.md',))
        self.assertIn('must be a DISPATCH IMPL', found[0].message)
        valid = body('IMPL', ident='parent', content='DISPATCH IMPL')
        self.assertEqual(self.family(report, 'lineage.impl-report-parent', [('parent.md', valid)]), [])

    def test_dispatch_lineage(self):
        invalid = body('IMPL', content='DISPATCH IMPL')
        self.assertIn('PARENT_DISPATCH_ID', self.family(invalid, 'lineage.dispatch')[0].message)
        review = body('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review', 'PARENT_DISPATCH_ID: plan', 'Verdict: approve')
        valid = body('IMPL', extra='PARENT_DISPATCH_ID: review', content='DISPATCH IMPL')
        self.assertEqual(self.family(valid, 'lineage.dispatch', [('plan.md', body(ident='plan')), ('review.md', review)]), [])

    def test_design_review_lineage(self):
        invalid = body(extra='DESIGN_LOCK_ID: DD\nDESIGN_RECORD_KIND: design-doc')
        self.assertIn('no earlier', self.family(invalid, 'lineage.design-review')[0].message)
        design = body('DESIGN', ident='design', extra='DESIGN_DOC_ID: DD')
        review = body('DESIGN-REVIEW', 'p.implementer', 'p.planner', 'review', 'PARENT_DISPATCH_ID: design\nDESIGN_DOC_ID: DD\nDESIGN_REVIEW_VERDICT: approve')
        valid = body(extra='DESIGN_LOCK_ID: DD\nDESIGN_RECORD_KIND: design-doc\nPARENT_DISPATCH_ID: review')
        self.assertEqual(self.family(valid, 'lineage.design-review', [('design.md', design), ('review.md', review)]), [])

    def test_cross_root_design(self):
        invalid = body(extra='DESIGN_SOURCE_REPO: nowhere')
        self.assertIn('declaration', self.family(invalid, 'lineage.cross-root-design')[0].message)
        self.assertEqual(self.family(body(), 'lineage.cross-root-design'), [])

    def test_merge_claim_without_gate(self):
        claim = body('SITREP', 'p.implementer', 'p.planner', 'merge', 'ACTIONS_GIT_REF: merged PR #1 at abcdef1\nFINAL_GIT_STATUS_SHORT: clean')
        self.assertIn('earlier MERGE-GATE', self.family(claim, 'lineage.merge-claim')[0].message)
        gate = body('MERGE-GATE', 'r.orchestrator-planner', 'p.implementer', 'merge', content='DISPATCH MERGE')
        self.assertEqual(self.family(claim, 'lineage.merge-claim', [('gate.md', gate)]), [])

    def test_row_truth_flip(self):
        old = body(ident='row', extra='ROW_TRUTH_CHECK: yes\nSCOPE_DIFF:\n- tools/x.py -> out')
        changed = body(ident='row', extra='ROW_TRUTH_CHECK: yes\nSCOPE_DIFF:\n- tools/x.py -> in')
        found = self.family(changed, 'lineage.row-truth', [('old.md', old)])
        self.assertEqual(found[0].operands, ('old.md',))
        self.assertEqual(self.family(old, 'lineage.row-truth', [('old.md', old)]), [])

    def test_review_visibility_counts_only_earlier_waiver(self):
        candidate = body('DESIGN', sender='r.orchestrator-planner').replace(b'CC: r.orchestrator-reviewer\n', b'')
        self.assertTrue(self.family(candidate, 'lineage.review-visibility'))
        waiver = body('SITREP', 'operator', 'p.planner', extra='OPERATOR_WAIVER: no orchestrator reviewer\nFINAL_GIT_STATUS_SHORT: clean')
        # The existing waiver grammar is an explicit operator header.
        waiver = waiver.replace(b'OPERATOR_WAIVER: no orchestrator reviewer', b'ORCH_REVIEW_WAIVER: yes')
        self.assertEqual(self.family(candidate, 'lineage.review-visibility', [('waiver.md', waiver)]), [])

    def test_lock_path_missing(self):
        raw = body(extra='PLAN_LOCK_ID: plans/absent.md')
        self.assertIn('references missing file', self.family(raw, 'lineage.lock-path')[0].message)
        (self.root / 'plans').mkdir()
        (self.root / 'plans/absent.md').write_text('present')
        self.assertEqual(self.family(raw, 'lineage.lock-path'), [])

    def test_governing_digest_against_bytes_at_admission(self):
        import hashlib
        (self.root / 'plans').mkdir()
        artifact = self.root / 'plans/P.md'
        artifact.write_bytes(b'plan')
        good = body(extra='PLAN_ARTIFACT: P\nPLAN_SHA256: ' + hashlib.sha256(b'plan').hexdigest())
        self.assertEqual(self.family(good, 'lineage.governing-digest'), [])
        artifact.write_bytes(b'changed')
        self.assertIn('does not match', self.family(good, 'lineage.governing-digest')[0].message)

    def test_lock_lifecycle_candidate_consumer(self):
        self.check_fixture('MT45-reviewer-design-origin', 'lineage.lock-lifecycle', '03-consumer.md', 'MT37-pair-route-control')

    def test_receiving_seat(self):
        invalid = body(target='r.master-planner', extra='DELEGATED_DISPATCH_AUTHORITY: yes')
        self.assertIn('never receives', self.family(invalid, 'lineage.receiving-seat')[0].message)
        self.assertEqual(self.family(body(extra='DELEGATED_DISPATCH_AUTHORITY: yes'), 'lineage.receiving-seat'), [])

    def test_commission_candidate_subject(self):
        self.check_fixture('CM188-later-approval-inert', 'lineage.commission', '03-grant.md', 'CM77-valid-chain')

    def check_fixture(self, case, family, candidate, control):
        root = TOOLS / 'relay-lint-fixtures/mastertier' / case
        files = sorted(root.glob('*.md'))
        chosen = root / candidate
        older = [(p.name, p.read_bytes()) for p in files if p.name < candidate]
        findings = self.family(chosen.read_bytes(), family, older)
        self.assertTrue(findings)
        self.assertTrue(all(f.subject == self.subject for f in findings))
        root = TOOLS / 'relay-lint-fixtures/mastertier' / control
        files = sorted(root.glob('*.md'))
        self.assertEqual(self.family(files[-1].read_bytes(), family, [(p.name, p.read_bytes()) for p in files[:-1]]), [])

    def test_commission_helpers_see_whole_predecessor_population(self):
        for name in ('CM77-valid-chain', 'CM216-malformed-latest-charter',
                     'CM211-review-same-position'):
            with self.subTest(name=name):
                root = TOOLS / 'relay-lint-fixtures/mastertier' / name
                files = sorted(root.rglob('*.md'), key=lambda p: rules.relay_order_key(p, root))
                phases = [(p, rules.relay_order_key(p, root), rules.header_fields(p.read_text()).get('PHASE', ''),
                           rules.header_fields(p.read_text()), p.read_text()) for p in files]
                candidate = phases[-1]
                # The helper retains ties in its input ordering. Ledger seq is unique;
                # sweep chronological ties are exercised directly at this boundary.
                expected = [f for f in rules.h27_commission_precompute(root, phases)
                            if f.subject == str(candidate[0].relative_to(root))]
                actual = judging._commission(root, phases, [candidate])
                self.assertEqual(actual, expected)
                if name == 'CM77-valid-chain':
                    self.assertEqual(actual, [])
                else:
                    self.assertTrue(actual)
        root = TOOLS / 'relay-lint-fixtures/mastertier/CM216-malformed-latest-charter'
        files = sorted(root.glob('*.md'))
        raw = files[-1].read_bytes()
        findings = self.family(raw, 'lineage.commission', [(p.name, p.read_bytes()) for p in files[:-1]])
        self.assertTrue(findings)

    def test_grant_shape_absent(self):
        raw = body('MERGE-GATE', 'r.orchestrator-planner', 'p.implementer')
        self.assertEqual(len(self.family(raw, 'authority.grant-shape-absent')), 1)
        for valid in (raw + b'DISPATCH MERGE\n', raw + b'MERGE_AUTHORIZATION: denied\n', raw.replace(b'TO: p.implementer', b'TO: r.orchestrator-reviewer')):
            self.assertEqual(self.family(valid, 'authority.grant-shape-absent'), [])

    def test_later_relay_never_changes_candidate_findings(self):
        import sqlite3
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE relays(seq INTEGER, rendered_path TEXT, body BLOB)')
        raw = body('IMPL', 'p.implementer', 'p.planner', extra='PARENT_DISPATCH_ID: later\nACTIONS_GIT_REF: implemented change abcdef1')
        corpus = judging.corpus_before(connection, 1)
        before = judging.judge_candidate(corpus, self.subject, envelope(raw), raw, artifact_root=self.root, deadline=time.monotonic()+5)
        connection.execute('INSERT INTO relays VALUES(?,?,?)', (2, 'later.md', body('IMPL', ident='later', content='DISPATCH IMPL')))
        after = judging.judge_candidate(judging.corpus_before(connection, 1), self.subject, envelope(raw), raw, artifact_root=self.root, deadline=time.monotonic()+5)
        self.assertEqual(before, after)

    def test_unreadable_artifact_is_could_not_evaluate(self):
        raw = body(extra='PLAN_ARTIFACT: absent\nPLAN_SHA256: ' + 'a' * 64)
        self.assertTrue(self.family(raw, 'external.could-not-evaluate'))

    def test_shared_git_deadline_bounds_the_whole_admission(self):
        import os
        import xrootfixgen
        xrootfixgen._init_repo(Path(self.temp.name))
        foreign, commit, digest = xrootfixgen._foreign_repo(self.root)
        fields = xrootfixgen._plan_fields(foreign, commit, digest)
        clean = xrootfixgen._render(fields).encode()
        from relay_engine.tests import xroot_narrow_corpus as corpus
        with corpus.budget(rules):
            self.assertFalse([f for f in self.judge(clean, deadline=float('inf'))
                              if f.id == 'external.could-not-evaluate'])
        xrootfixgen._fifo_object(self.root, foreign, fields)
        xrootfixgen._replace(fields, 'DESIGN_LOCK_ID', 'designs/d1.md')
        fields += [('PLAN_LOCK_ID', 'plans/p1.md'), ('PLAN_SOURCE_REPO', str(foreign)),
                   ('PLAN_SOURCE_COMMIT', 'a' * 40)]
        raw = xrootfixgen._render(fields).encode()
        spawned = []
        now = [0]
        real_spawn = rules._xroot_start_git
        def record(*args, **kwargs):
            process = real_spawn(*args, **kwargs)
            spawned.append(process.pid)
            if any('a'*40 in str(arg) for arg in args[0]):
                now[0] = 6
            return process
        with mock.patch.object(rules.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(rules, '_xroot_start_git', side_effect=record):
            findings = self.judge(raw, deadline=5)
        external = [f for f in findings if f.id == 'external.could-not-evaluate']
        self.assertEqual({f.params['gate'] for f in external},
                         {'cross-root-design', 'cross-root-DESIGN_LOCK_ID', 'cross-root-PLAN_LOCK_ID'})
        self.assertTrue(spawned)
        for pid in spawned:
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        # Expired budgets must prevent any Git process from being spawned.
        with mock.patch.object(rules, '_xroot_start_git', side_effect=AssertionError('expired budget spawned Git')):
            for gate in ('design', 'plan', 'artifact'):
                with self.assertRaises(rules.XrootAxisError) as raised:
                    rules.xroot_git(self.root, 'rev-parse', 'HEAD', axis=gate, deadline=time.monotonic()-1)
                self.assertIn('timed out', raised.exception.detail)
