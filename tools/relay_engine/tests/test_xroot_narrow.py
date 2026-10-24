"""Narrow reads preserve candidate errors while ignoring other carrier bytes."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from relay_engine import rules, ledger
from relay_engine.tests import xroot_reference as ref
from relay_engine.tests.xroot_b9_cases import _object_tree
import xrootfixgen as xf


class TestXrootNarrow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b9-narrow-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        xf._init_repo(self.repo)
        token = rules._XROOT_BUDGET.set((time.monotonic()+600, []))
        self.addCleanup(rules._XROOT_BUDGET.reset, token)

    def tree(self, files):
        return _object_tree(self.repo, files)

    def raw_tree(self, entries):
        return xf._literal_object(self.repo, 'tree', b''.join(
            xf._tree_entry(mode, name, oid) for mode, name, oid in entries))

    def blob(self, raw=b'DESIGN_DOC_ID: d1\n'):
        return xf._literal_object(self.repo, 'blob', raw)

    def outcome(self, tree, walk=None):
        try:
            rows = (walk or rules.xroot_relays)(self.repo, tree)
            return (None, [(r.name, r.fields, r.text, r.stamp, r.stamp_raw) for r in rows])
        except rules.XrootAxisError as exc:
            return (exc.axis, exc.detail)

    def compare(self, tree):
        actual = self.outcome(tree)
        expected = self.outcome(tree, ref.xroot_reference_relays)
        self.assertEqual(expected, actual)
        return actual

    def test_candidate_predicate_bytes(self):
        self.assertTrue(hasattr(rules, 'xroot_is_candidate'), 'candidate filter missing')
        for name in (b'DESIGN-x.md', b'DESIGN-\xff.md'):
            self.assertTrue(rules.xroot_is_candidate(name))
        for name in (b'design-x.md', b'DESIGN-x.MD', b'DESIGN-.mdx', b'PLAN-x.md', b'INDEX.md', b'DESIGN-x.md.txt'):
            self.assertFalse(rules.xroot_is_candidate(name))

    def test_predicate_is_on_entry_name_not_path(self):
        tree = self.tree({'DESIGN-lane/x.md': b'\xff', 'lane/DESIGN-x.md': 'DESIGN_DOC_ID: d1\n'})
        result = self.compare(tree)
        self.assertIsNone(result[0])
        self.assertEqual(['lane/DESIGN-x.md'], [r[0] for r in result[1]])

    def test_engine_rendered_design_names_are_candidates(self):
        self.assertTrue(hasattr(rules, 'xroot_is_candidate'), 'candidate filter missing')
        for role in ('Pair Planner', 'Pair Implementer', 'Orchestrator Planner', 'Orchestrator Reviewer'):
            for phase in ('DESIGN', 'DESIGN-REVIEW', 'PLAN', 'SITREP'):
                e = SimpleNamespace(dispatch_id='r-b', run_id='r', role=role, phase=phase)
                name = ledger._rendered_path(e, '20260801-100000').split('/')[-1].encode()
                self.assertEqual(phase in ('DESIGN', 'DESIGN-REVIEW'), rules.xroot_is_candidate(name))

    def hygiene(self, is_candidate):
        blob = self.blob(b'\xff')
        for folder in ('a', 'z'):
            name = 'DESIGN-x.md' if is_candidate else 'SITREP-x.md'
            for mode, oid in ((b'100644', blob), (b'120000', blob), (b'160000', '3'*40)):
                yield self.tree({folder+'/'+name: (mode, oid), 'm/DESIGN-ok.md': 'DESIGN_DOC_ID: d1\n'})
            badname = (b'DESIGN-' if is_candidate else b'SITREP-')+b'\xff.md'
            child = self.raw_tree([(b'100644', badname, blob)])
            yield self.tree({folder: (b'40000', child), 'm/DESIGN-ok.md': 'DESIGN_DOC_ID: d1\n'})

    def test_noncandidate_hygiene_rows_allow(self):
        for tree in self.hygiene(False):
            with self.subTest(tree=tree):
                self.assertIsNone(self.compare(tree)[0])

    def test_candidate_hygiene_rows_refuse(self):
        for tree in self.hygiene(True):
            with self.subTest(tree=tree):
                self.assertEqual('authority-population', self.compare(tree)[0])

    def test_e30_noncandidate_allows_candidate_refuses(self):
        from relay_engine.tests.test_authority_fold10 import TestAuthorityFold10
        f = TestAuthorityFold10()
        for direct in (False, True):
            for name, reasons in (('SITREP-x-20260801-120000.md', []),
                                  ('DESIGN-x-20260801-120000.md', ['authority.lineage-incomplete'])):
                with self.subTest(direct=direct, name=name):
                    f.assert_case([] if direct else reasons,
                                  unrelated={name: 'PHASE: SITREP\nDiscuss d1.\f'}, direct=direct)

    def test_candidate_limits_count_candidates(self):
        for limit, value, valid, forbidden in (
            ('XROOT_MAX_RELAYS', 1, {'SITREP-a.md': 'x', 'DESIGN-a.md': 'x'}, {'DESIGN-a.md': 'x', 'DESIGN-b.md': 'x'}),
            ('XROOT_MAX_RELAY_BYTES', 2, {'SITREP-a.md': 'xxxx', 'DESIGN-a.md': 'xx'}, {'DESIGN-a.md': 'xxx'}),
            ('XROOT_MAX_TREES', 1, {'x': 'x'}, {'d/x': 'x'}),
            ('XROOT_MAX_ENTRIES', 1, {'x': 'x'}, {'x': 'x', 'y': 'x'})):
            with self.subTest(limit=limit), mock.patch.object(rules, limit, value):
                self.assertIsNone(self.compare(self.tree(valid))[0])
                result = self.compare(self.tree(forbidden))
                self.assertEqual('capacity', result[0]); self.assertIn(limit, result[1])
        # A real 20,001-candidate root exercises the shared fixture's lint caller.
        from relay_engine.tests.capture_xroot_baseline import LiftedBudget
        def prepare(_root, repo, fields):
            original = xf._git(repo, 'rev-parse', 'HEAD:relays')
            raw = xf._git(repo, 'cat-file', 'tree', original, text=False)
            blob = xf._literal_object(repo, 'blob', b'PHASE: SITREP\n')
            raw += b''.join(xf._tree_entry(b'100644', f'DESIGN-filler-{i:05d}.md'.encode(), blob)
                            for i in range(19999))
            relays = xf._literal_object(repo, 'tree', raw)
            designs = xf._git(repo, 'rev-parse', 'HEAD:designs')
            tree = xf._literal_object(repo, 'tree', xf._tree_entry(b'40000', b'designs', designs)
                                     + xf._tree_entry(b'40000', b'relays', relays))
            xf._replace(fields, 'DESIGN_SOURCE_COMMIT', xf._literal_commit(repo, tree))
        _, root, _, _ = xf._build_case(self.repo/'capacity', 'candidate-count', prepare=prepare)
        with mock.patch.object(rules, '_XROOT_BUDGET', LiftedBudget(rules._XROOT_BUDGET)):
            result = rules.lint_relay_root(root)
        self.assertEqual(1, len(result.errors))
        self.assertIn('verification: capacity:', result.errors[0])
        self.assertIn('XROOT_MAX_RELAYS of 20000', result.errors[0])

    def candidate_tie(self, review):
        from relay_engine.tests.test_authority_fold10 import TestAuthorityFold10, DESIGN, REVIEW
        source = xf.REVIEW_NAME if review else xf.ORIGIN_NAME
        target = ('DESIGN-REVIEW-alt-reviewer-20260801-110000.md' if review
                  else 'DESIGN-alt-planner-20260801-100000.md')
        detail = ('multiple latest DESIGN-REVIEW carriers share timestamp 20260801-110000' if review
                  else 'multiple latest DESIGN origins share timestamp 20260801-100000')
        _, root, _, _ = xf._build_case(self.repo/'ties', 'review' if review else 'origin',
                                      mutate_foreign=lambda repo: xf._copy_relay(repo, source, target))
        result = rules.lint_relay_root(root)
        self.assertEqual([xf._xerror('ambiguity', detail)], result.errors)
        TestAuthorityFold10().assert_case(['authority.lineage-incomplete'],
                                         unrelated={target: REVIEW if review else DESIGN})

    def test_candidate_origin_tie_refuses(self):
        self.candidate_tie(False)

    def test_candidate_review_tie_refuses(self):
        self.candidate_tie(True)

    def test_retained_text_counts_candidates(self):
        text = 'DESIGN_DOC_ID: d1\n'
        with mock.patch.object(rules, 'XROOT_MAX_RETAINED_BYTES', len(text)):
            self.assertIsNone(self.compare(self.tree({'SITREP-a.md': text*3, 'DESIGN-a.md': text}))[0])
            result = self.compare(self.tree({'DESIGN-a.md': text, 'DESIGN-b.md': text}))
            self.assertEqual('capacity', result[0]); self.assertIn('XROOT_MAX_RETAINED_BYTES', result[1])
        # More than the real cap, not merely a patched-counter example: all
        # non-candidates name the design identity, yet none is retained/read.
        blob = self.blob((text+('x'*(1024*1024-len(text)))).encode())
        tree = self.raw_tree([(b'100644', f'SITREP-{i:03d}.md'.encode(), blob) for i in range(257)]
                             + [(b'100644', b'DESIGN-x.md', self.blob(text.encode()))])
        self.assertEqual(['DESIGN-x.md'], [r[0] for r in self.compare(tree)[1]])

    def test_full_candidate_count_and_byte_twins(self):
        from relay_engine.tests import xroot_b9_cases as cases
        from relay_engine.tests import xroot_narrow_corpus as corpus
        def renamed(repo, tree):
            entries = []
            for name, (kind, oid) in rules.xroot_tree_entries(repo, tree).items():
                if kind == 'tree':
                    oid = renamed(repo, oid)
                elif name.endswith(b'.md') and not name.startswith(b'DESIGN-'):
                    name = b'DESIGN-'+name
                entries.append((b'40000' if kind=='tree' else b'100644', name, oid))
            raw = b''.join(xf._tree_entry(mode, name, oid) for mode,name,oid in sorted(entries,key=lambda e:e[1]))
            return xf._literal_object(repo, 'tree', raw)
        labels = {'C1-over-count-20001':'XROOT_MAX_RELAYS', 'C2-over-bytes-300MiB':'XROOT_MAX_RELAY_BYTES'}
        for label, case in cases.materialize(self.repo/'full-limits', only=labels).items():
            with self.subTest(case=label), corpus.budget(rules):
                self.assertIsNone(corpus.verify(rules, self.repo, case.declaration)['axis'])
                tree, _ = rules.xroot_paths(case.repository, case.commit, case.declaration)
                twin = renamed(case.repository, tree)
                designs = xf._git(case.repository, 'rev-parse', case.commit+':designs')
                root = xf._literal_object(case.repository, 'tree',
                    xf._tree_entry(b'40000', b'designs', designs)+xf._tree_entry(b'40000', b'relays', twin))
                declaration = dict(case.declaration, DESIGN_SOURCE_COMMIT=xf._literal_commit(case.repository, root))
                actual = corpus.verify(rules, self.repo, declaration)
                with ref.xroot_reference_walk():
                    expected = corpus.verify(rules, self.repo, declaration)
                self.assertEqual(expected, actual)
                self.assertEqual('capacity', actual['axis'])
                self.assertIn(labels[label], actual['detail'])

    def test_mixed_faults_candidate_names(self):
        deep = self.tree({'/'.join(['d']*257)+'/x': 'x'})
        for later in ('3'*40, xf._literal_object(self.repo, 'tree', b'broken'), deep):
            result = self.compare(self.tree({'DESIGN-a.md': b'\xff', 'z': (b'40000', later)}))
            self.assertEqual(('authority-population', 'authority carrier DESIGN-a.md is not UTF-8'), result)
        tree = self.raw_tree([(b'100644', b'DESIGN-a.md', self.blob(b'\xff')),
                              (b'100644', b'DESIGN-\xff.md', self.blob())])
        self.assertEqual(('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename'), self.compare(tree))
        result = self.compare(self.tree({'a': (b'40000', '3'*40), 'b/DESIGN-x.md': b'\xff'}))
        self.assertEqual(('repository', 'Git object query failed'), result)
        result = self.compare(self.tree({'DESIGN-a.md': 'x', 'DESIGN-g.md': (b'160000', '3'*40), 'DESIGN-z.md': b'\xff'}))
        self.assertEqual(('authority-population', 'Git object query failed'), result)
        for raw in (b'valid', b'\xff'):
            tree = self.raw_tree([(b'100644', b'DESIGN-a.md', self.blob(raw)),
                                  (b'100644', b'DESIGN-\xff.md', self.blob())])
            self.assertEqual(('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename'), self.compare(tree))
        child = self.raw_tree([(b'100644', b'DESIGN-\xff.md', self.blob())])
        self.assertEqual(('authority-population', 'authority carrier DESIGN-a.md is not UTF-8'),
                         self.compare(self.tree({'DESIGN-a.md': b'\xff', 'z': (b'40000', child)})))
        blob = self.blob(b'x')
        over = self.raw_tree([(b'100644', f'DESIGN-{i:05d}.md'.encode(), blob) for i in range(20001)])
        self.assertEqual(('authority-population', 'DESIGN_SOURCE_ROOT contains symlink carrier a/DESIGN-x.md'),
                         self.compare(self.tree({'a/DESIGN-x.md': (b'120000', blob), 'b': (b'40000', over)})))
        with mock.patch.object(rules, 'XROOT_MAX_ENTRIES', 2):
            self.assertEqual(('authority-population', 'authority carrier DESIGN-a.md is not UTF-8'),
                             self.compare(self.tree({'DESIGN-a.md': b'\xff', 'z/x': 'x'})))

    def position_tree(self, earlier=None, candidate=True):
        child = self.tree({('DESIGN-x.md' if candidate else 'x.md'): 'DESIGN_DOC_ID: d1\n'})
        entries = [(b'40000', b'\xff', child)]
        if earlier == 'missing': entries.insert(0, (b'40000', b'a', '3'*40))
        if earlier == 'invalid': entries.insert(0, (b'100644', b'DESIGN-a.md', self.blob(b'\xff')))
        return self.raw_tree(entries)

    def test_position_change_first_refusal_identity(self):
        capture = json.loads((Path(__file__).parents[2]/'relay-engine-fixtures/xroot/baseline-b8d87eb-narrow.json').read_text())
        for earlier, expected in (
            ('missing', ('repository', 'Git object query failed')),
            ('invalid', ('authority-population', 'authority carrier DESIGN-a.md is not UTF-8')),
            (None, ('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename'))):
            tree = self.position_tree(earlier)
            self.assertEqual(expected, self.compare(tree))
            old = capture['cases'][f'position-{earlier}']
            self.assertEqual(('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename'),
                             (old['axis'], old['detail']))
            answer = self.full_tree_answer(tree)
            reason = 'authority.could-not-evaluate' if earlier=='missing' else 'authority.lineage-incomplete'
            for path in ('pair', 'direct'):
                self.assertEqual([reason], answer['authority'][path]['reasons'])

    def test_index_and_all_tree_controls(self):
        for dirname in ('INDEX.md', 'DESIGN-directory.md'):
            tree = self.tree({dirname+'/DESIGN-x.md': 'DESIGN_DOC_ID: d1\n'})
            self.assertEqual([dirname+'/DESIGN-x.md'], [r[0] for r in self.compare(tree)[1]])
        self.assertEqual((None, []), self.compare(self.tree({'INDEX.md': (b'120000', self.blob())})))
        for oid in ('3'*40, xf._literal_object(self.repo, 'tree', b'broken')):
            self.assertEqual('repository', self.compare(self.tree({'z': (b'40000', oid)}))[0])
        self.assertEqual('repository', self.compare(self.tree({'/'.join(['d']*258)+'/x': 'x'}))[0])
        empty = self.raw_tree([])
        bad_directory = self.raw_tree([(b'40000', b'DESIGN-\xff.md', empty)])
        self.assertEqual(('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename'),
                         self.compare(bad_directory))

    def test_old_name_controls(self):
        self.assertEqual((None, []), self.compare(self.position_tree(candidate=False)))
        self.assertEqual((None, []), self.compare(self.tree({'a.md': b'\xff', 'x.md': (b'160000', '3'*40)})))
        self.assertEqual((None, []), self.compare(self.tree({'a/x.md': (b'120000', self.blob()),
                                                            'g.md': (b'160000', '3'*40)})))

    def test_blocked_tree_candidate_and_old_name(self):
        import os
        from contextlib import nullcontext
        subtree = self.tree({'x': 'x'})
        path = self.repo/'.git/objects'/subtree[:2]/subtree[2:]
        path.unlink(); os.mkfifo(path)
        for name in ('DESIGN-a.md', 'a.md'):
            tree = self.tree({name: b'\xff', 'z': (b'40000', subtree)})
            outcomes = []
            requested = []
            original_reader = rules.XrootBatchReader
            class Observed(original_reader):
                def request(self, spec):
                    requested.append(spec)
                    return super().request(spec)
            for walk in (rules.xroot_relays, ref.xroot_reference_relays):
                token = rules._XROOT_BUDGET.set((time.monotonic()+0.2, []))
                try:
                    with mock.patch.object(rules, 'XrootBatchReader', Observed) if walk is rules.xroot_relays else nullcontext():
                        outcomes.append(self.outcome(tree, walk))
                finally:
                    rules._XROOT_BUDGET.reset(token)
            with self.subTest(name=name):
                self.assertEqual(outcomes[0], outcomes[1])
                if name.startswith('DESIGN-'):
                    self.assertEqual(('authority-population', 'authority carrier DESIGN-a.md is not UTF-8'), outcomes[0])
                    self.assertEqual([tree+'^{tree}', self.blob(b'\xff')+'^{blob}', subtree+'^{tree}'], requested)
                else:
                    self.assertEqual('capacity', outcomes[0][0])

    def test_old_domain_membership_controls(self):
        for files, expected in (({'lane/DESIGN-x.md': 'x'}, 'inside'),
                                ({'INDEX.md': (b'120000', self.blob())}, 'inside'),
                                ({'x.md': 'x'}, 'outside'),
                                ({'x': (b'120000', self.blob())}, 'outside')):
            self.assertEqual(expected, ref.xroot_old_domain(self.repo, self.tree(files)).kind)
        self.assertEqual('outside', ref.xroot_old_domain(self.repo, self.position_tree(candidate=False)).kind)
        from relay_engine.tests.xroot_b9_cases import ORCH, ORCH_REV
        peer = self.tree({'DESIGN-planner-20260801-100000.md': ORCH,
                          'DESIGN-REVIEW-reviewer-20260801-110000.md': ORCH_REV})
        self.assertEqual(ref.XrootDomainVerdict('inside'), ref.xroot_old_domain(self.repo, peer))

    def test_old_reader_within_domain(self):
        capture = json.loads((Path(__file__).parents[2]/'relay-engine-fixtures/xroot/baseline-b8d87eb-narrow.json').read_text())
        trees = {f'position-{kind}': self.position_tree(kind) for kind in (None, 'missing', 'invalid')}
        trees.update({'noncandidate-directory': self.position_tree(candidate=False),
                      'lane-candidate': self.tree({'lane/DESIGN-x.md': 'DESIGN_DOC_ID: d1\n'}),
                      'missing-tree': self.tree({'z': (b'40000', '3'*40)}),
                      'early-candidate-later-missing': self.tree({'DESIGN-a.md': b'\xff', 'z': (b'40000', '3'*40)})})
        for candidate in (False, True):
            for i, tree in enumerate(self.hygiene(candidate)):
                trees[f'hygiene-{candidate}-{i}'] = tree
        self.assertEqual(set(capture['cases']), set(trees))
        for label, tree in trees.items():
            with self.subTest(case=label):
                verdict = ref.xroot_old_domain(self.repo, tree)
                print('old-domain', label, verdict, flush=True)
                actual = self.compare(tree)
                if verdict.kind == 'outside':
                    continue
                old = capture['cases'][label]
                self.assertEqual(old['axis'], actual[0])
                if old['axis']:
                    self.assertEqual(old['detail'], actual[1])
                else:
                    self.assertEqual(old['population'], [r[0] for r in actual[1]])

    def test_old_domain_structural_fault_only(self):
        for tree in (self.tree({'z': (b'40000', '3'*40)}),
                     self.tree({'z': (b'40000', xf._literal_object(self.repo, 'tree', b'broken'))}),
                     self.tree({'/'.join(['d']*258)+'/x': 'x'})):
            verdict = ref.xroot_old_domain(self.repo, tree)
            self.assertEqual('structural', verdict.kind)
            self.assertEqual((verdict.axis, verdict.detail), self.compare(tree))

    def test_old_domain_violation_before_structural_fault(self):
        tree = self.tree({'a': (b'120000', self.blob()), 'z': (b'40000', '3'*40)})
        self.assertEqual(ref.XrootDomainVerdict('outside', 'a'), ref.xroot_old_domain(self.repo, tree))
        self.assertEqual('repository', self.compare(tree)[0])
        self.assertEqual('outside', ref.xroot_old_domain(self.repo, self.position_tree('missing')).kind)

    def test_old_domain_structural_fault_before_violation(self):
        tree = self.tree({'a': (b'40000', '3'*40), 'z': (b'120000', self.blob())})
        verdict = ref.xroot_old_domain(self.repo, tree)
        self.assertEqual('structural', verdict.kind)
        self.assertEqual((verdict.axis, verdict.detail), self.compare(tree))

    def test_old_domain_never_swallows(self):
        tree = self.tree({'z': (b'40000', '3'*40)})
        def comparison():
            verdict = ref.xroot_old_domain(self.repo, tree)
            self.assertEqual('structural', verdict.kind)
            return self.compare(tree)
        self.assertEqual(('repository', 'Git object query failed'), comparison())
        with mock.patch.object(ref, 'xroot_old_domain', side_effect=rules.XrootAxisError('repository', 'injected')):
            with self.assertRaisesRegex(rules.XrootAxisError, 'injected'):
                comparison()

    def test_reference_walk_equals_new_reader(self):
        for files in ({'lane/DESIGN-x.md': 'DESIGN_DOC_ID: d1\n'}, {'SITREP-x.md': b'\xff'},
                      {'DESIGN-x.md': b'\xff'}, {'z': (b'40000', '3'*40)}):
            self.compare(self.tree(files))

    def declaration_for_tree(self, relays):
        import hashlib
        designs = self.tree({'d1.md': b'design'})
        root = self.raw_tree([(b'40000', b'designs', designs), (b'40000', b'relays', relays)])
        commit = xf._literal_commit(self.repo, root)
        return dict(DESIGN_LOCK_ID='d1', DESIGN_RECORD_KIND='design-doc', DESIGN_DOC_ID='d1',
                    DESIGN_OWNER='f', DESIGN_SOURCE_REPO=str(self.repo), DESIGN_SOURCE_COMMIT=commit,
                    DESIGN_SOURCE_ROOT='relays', DESIGN_SOURCE_PATH='designs/d1.md',
                    DESIGN_SHA256=hashlib.sha256(b'design').hexdigest())

    def full_tree_answer(self, tree):
        from relay_engine.tests import xroot_narrow_corpus as corpus
        declaration = self.declaration_for_tree(tree)
        with corpus.budget(rules):
            actual = dict(verify=corpus.verify(rules, self.repo, declaration),
                          authority=corpus.authority_answers(declaration))
            with ref.xroot_reference_walk():
                expected = dict(verify=corpus.verify(rules, self.repo, declaration),
                                authority=corpus.authority_answers(declaration))
        self.assertEqual(expected, actual)
        return actual

    def test_hygiene_complete_lineage_both_positions(self):
        from relay_engine.tests.test_authority_fold10 import DESIGN, REVIEW
        lineage = {'m/DESIGN-planner-20260801-100000.md': DESIGN,
                   'm/DESIGN-REVIEW-implementer-20260801-110000.md': REVIEW}
        for candidate in (False, True):
            for folder in ('a', 'z'):
                prefix = b'DESIGN-' if candidate else b'SITREP-'
                for fault in ('symlink', 'blob', 'name', 'gitlink', 'phase'):
                    with self.subTest(candidate=candidate, folder=folder, fault=fault):
                        name = prefix+(b'\xff.md' if fault=='name' else b'x.md')
                        raw = b'\xff' if fault=='blob' else b'PHASE: DESIGN\nPHASE: SITREP\nDESIGN_DOC_ID: d1\n'
                        mode = b'120000' if fault=='symlink' else b'160000' if fault=='gitlink' else b'100644'
                        child = self.raw_tree([(mode, name, '3'*40 if fault=='gitlink' else self.blob(raw))])
                        answer = self.full_tree_answer(self.tree({**lineage, folder: (b'40000', child)}))
                        self.assertEqual('authority-population' if candidate else None, answer['verify']['axis'])
                        for path in ('pair', 'direct'):
                            self.assertEqual(not candidate, answer['authority'][path]['allowed'])
                            self.assertEqual(['authority.lineage-incomplete'] if candidate else [],
                                             answer['authority'][path]['reasons'])

    def test_candidate_phase_conflicts_and_stage1_precedence(self):
        from relay_engine.tests.test_authority_fold10 import DESIGN, REVIEW
        lineage = {'b/DESIGN-planner-20260801-100000.md': DESIGN,
                   'b/DESIGN-REVIEW-implementer-20260801-110000.md': REVIEW}
        for folder in ('a', 'c'):
            for candidate in (False, True):
                name = folder+'/'+('DESIGN-' if candidate else 'PLAN-')+'x.md'
                tree = self.tree({**lineage, name: 'PHASE: DESIGN\nPHASE: SITREP\n'})
                with self.subTest(folder=folder, candidate=candidate):
                    answer = self.full_tree_answer(tree)
                    self.assertEqual('authority-population' if candidate else None, answer['verify']['axis'])
        tree = self.tree({**lineage, 'DESIGN-a.md': b'\xff',
                          'z/DESIGN-x.md': 'PHASE: DESIGN\nPHASE: SITREP\n'})
        self.assertEqual(dict(axis='authority-population', detail='authority carrier DESIGN-a.md is not UTF-8'),
                         self.full_tree_answer(tree)['verify'])

    def test_candidate_pinned_tree_controls(self):
        from relay_engine.tests.test_authority_fold10 import TestAuthorityFold10
        fixture = TestAuthorityFold10()
        name = 'DESIGN-x-20260801-120000.md'
        for original, replacement, expected in [('d1 clean', 'd1 prose\f tail', []),
                                                 ('d1 prose\f tail', 'd1 clean', ['authority.lineage-incomplete'])]:
            with self.subTest(original=original), fixture.foreign_case(unrelated={name: original}) as (f,g,repo,_):
                (repo/'relays'/name).write_text(replacement)
                fixture.git(repo, 'add', '-A'); fixture.git(repo, 'commit', '-qm', 'later head')
                self.assertEqual(expected, f.check(g)['reasons'])

    def test_candidate_e30_control_variants(self):
        from relay_engine.tests.test_authority_fold10 import TestAuthorityFold10
        f = TestAuthorityFold10()
        variants = ['PHASE: SITREP\n\nThis discusses d1.\f',
                    'PHASE: SITREP\n\nAn example PHASE: elsewhere.\f',
                    'PHASE: SITREP\n\nDiscuss d\x011.', 'PHASE: SITREP\n\nDiscuss D1.\f']
        variants += ['PHASE: SITREP\n\nDiscuss d1.'+c for c in ['\v','\0','\x7f','\x85','\u2028']]
        for text in variants:
            with self.subTest(text=text):
                f.assert_case(['authority.lineage-incomplete'], unrelated={'DESIGN-x-20260801-120000.md': text})

    def test_reference_comparison_has_teeth(self):
        tree = self.tree({'DESIGN-x.md': 'DESIGN_DOC_ID: d1\n'})
        with mock.patch.object(ref, 'candidate', return_value=False):
            with self.assertRaises(AssertionError): self.compare(tree)
        with ref.xroot_reference_walk(), mock.patch.object(ref, 'candidate', return_value=False):
            self.assertEqual([], rules.xroot_relays(self.repo, tree))
