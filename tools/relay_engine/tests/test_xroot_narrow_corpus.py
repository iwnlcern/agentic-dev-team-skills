"""Whole-answer differential over the shared and synthetic foreign corpus."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from relay_engine import rules
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests import xroot_reference as reference
from relay_engine.tests import xroot_b9_cases as cases
import xrootfixgen as xf


class TestInjectedReference(unittest.TestCase):
    def test_injected_deadlines_equal_reference(self):
        from relay_engine.tests.capture_xroot_baseline import INJECTED_TIMEOUTS
        with tempfile.TemporaryDirectory(prefix='narrow-injected-') as temp:
            for label, case in cases.materialize(Path(temp), only=INJECTED_TIMEOUTS).items():
                with self.subTest(case=label):
                    original_reader = rules.XrootBatchReader
                    review_oid = xf._git(case.repository, 'rev-parse', case.commit+':'+cases.R)

                    class ExpiredReview(original_reader):
                        def __init__(self, *args, **kwargs):
                            self.requests = []
                            super().__init__(*args, **kwargs)

                        def request(self, spec):
                            self.requests.append(spec)
                            return super().request(spec)

                        def next_response(self):
                            # Expire at the same blob-read event as the serial
                            # oracle, after request-ahead has completed.
                            if self.requests.pop(0) == review_oid+'^{blob}':
                                self.deadline = time.monotonic()-1
                            return super().next_response()

                    answers = []
                    for use_reference in (False, True):
                        fault = (mock.patch.object(rules, 'XrootBatchReader', ExpiredReview)
                                 if label=='T1b-timeout-population-blob' and not use_reference
                                 else cases.deadline_fault(case, rules))
                        with corpus.budget(rules), fault:
                            with reference.xroot_reference_walk() if use_reference else nullcontext():
                                answer = dict(verify=corpus.verify(rules, case.repository, case.declaration),
                                              authority=corpus.authority_answers(case.declaration))
                        answers.append(answer)
                    self.assertEqual(answers[0], answers[1])
                    self.assertEqual('capacity', answers[0]['verify']['axis'])
                    for path in ('pair', 'direct'):
                        self.assertFalse(answers[0]['authority'][path]['allowed'])


class TestXrootNarrowCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='narrow-corpus-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.rows = corpus.materialize(Path(cls.temp.name))

    def observed(self, label, root, declaration, reference_walk=False):
        with corpus.budget(rules, 5 if label=='xroot/X43-fifo-object-timeout' else 600):
            with reference.xroot_reference_walk() if reference_walk else nullcontext():
                answer = dict(verify=corpus.verify(rules, root, declaration),
                              authority=corpus.authority_answers(declaration))
                if label.startswith('xroot/'):
                    result = rules.lint_relay_root(root)
                    answer['lint'] = dict(errors=sorted(result.errors), warnings=sorted(result.warnings))
                return corpus.normalize(answer, root, declaration)

    def test_reference_walk_equals_new_reader(self):
        for label, root, declaration in self.rows:
            with self.subTest(case=label):
                self.assertEqual(self.observed(label, root, declaration, True),
                                 self.observed(label, root, declaration), label)

    def test_reference_comparison_has_teeth(self):
        label, root, declaration = next(row for row in self.rows if row[0]=='control-allow')
        actual = self.observed(label, root, declaration)
        with mock.patch.object(reference, 'candidate', return_value=False):
            with self.assertRaises(AssertionError):
                self.assertEqual(self.observed(label, root, declaration, True), actual, label)

    def test_old_reader_within_domain(self):
        self.capture = json.loads((Path(__file__).parents[2]/
            'relay-engine-fixtures/xroot/baseline-b8d87eb-corpus.json').read_text())
        self.assertEqual('b8d87ebe15fbd3f5e06f1d0112fccad832acd1b1', self.capture['source_commit'])
        self.assertEqual({row[0] for row in self.rows}, set(self.capture['cases']))
        for label, root, declaration in self.rows:
            with self.subTest(case=label), corpus.budget(rules, 5 if label=='xroot/X43-fifo-object-timeout' else 600):
                verdict = corpus.domain(root, declaration, reference.xroot_old_domain)
                print('old-domain', label, json.dumps(verdict, sort_keys=True), flush=True)
                actual = corpus.verify(rules, root, declaration)
                old = self.capture['cases'][label]
                if 'representation' in self.capture:
                    for row in actual.get('population', []):
                        row['text_sha256'] = hashlib.sha256(row.pop('text').encode()).hexdigest()
                if verdict['kind']=='outside':
                    continue
                if label in ('L7-orchestrator-peer', 'xroot/B9-peer-orchestrator-planner-orchestrator-reviewer'):
                    # R107's orchestrator peer, the established authority-population move.
                    self.assertEqual('authority-population', old['axis'])
                    self.assertIsNone(actual['axis'])
                elif label=='xroot/X43-fifo-object-timeout':
                    # Established budget-axis change, captured before B9's reader.
                    self.assertEqual('commit', old['axis'])
                    self.assertEqual('capacity', actual['axis'])
                elif label in ('M1-over-trees-20001', 'M2-over-entries-200001'):
                    # DD 6.2 envelope rule takes precedence over a lifted allow.
                    self.assertIsNone(old['axis'])
                    self.assertEqual('capacity', actual['axis'])
                    self.assertIn('XROOT_MAX_TREES' if label.startswith('M1-') else 'XROOT_MAX_ENTRIES', actual['detail'])
                elif verdict['kind']=='structural':
                    expected = (verdict['axis'], verdict['detail'])
                    self.assertEqual(expected, (old['axis'], old['detail']))
                    self.assertEqual(expected, (actual['axis'], actual['detail']))
                else:
                    self.assertEqual(old, actual)
