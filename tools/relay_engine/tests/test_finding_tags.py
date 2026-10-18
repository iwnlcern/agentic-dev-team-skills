"""Structural findings retain identity independently of their display text."""
from relay_engine.tests.test_judgment_store import base_module
import ast
from collections import Counter
from dataclasses import replace
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import rules
from relay_engine.tests.capture_order_baseline import collect, snapshot

REPO = Path(__file__).resolve().parents[3]
BASELINE = REPO / 'tools/relay-engine-fixtures/judging/base-order-cbc8f07.json'
FAMILIES = frozenset(('file.lint', 'lineage.lock-lifecycle', 'lineage.receiving-seat',
                     'lineage.commission', 'lineage.cross-root-design', 'lineage.design-review',
                     'lineage.dispatch', 'lineage.impl-report-parent', 'lineage.merge-claim',
                     'lineage.row-truth', 'lineage.review-visibility', 'lineage.lock-path',
                     'lineage.governing-digest', 'review.gate-not-heard'))
HELPERS = {'gate_checkpoint_findings', 'orchestrator_review_gate_errors', 'h27_receiving_seat_errors',
           'h27_foreign_lock_errors', 'h27_lock_lifecycle_precompute',
           'h27_commission_precompute', 'xroot_plan_gate', 'xroot_design_gate'}


def untagged_sites(source):
    tree = ast.parse(source)
    problems = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        if function.name not in HELPERS | {'lint_relay_root'}:
            continue
        structural_start = source[:source.index('    lock_routes, lock_lifecycle_errors =')].count('\n') + 1
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if function.name == 'lint_relay_root' and node.lineno < structural_start:
                continue
            target = ast.unparse(node.func)
            if target in ('result.error', 'result.warn'):
                keys = {kw.arg for kw in node.keywords}
                if not {'check_id', 'subject'} <= keys:
                    problems.append(node.lineno)
            if target in ('errors.append', 'errors.extend') and node.args:
                if isinstance(node.args[0], (ast.JoinedStr, ast.Constant)):
                    problems.append(node.lineno)
    return problems


class TestFindingTags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        snapshot(cls.root)
        cls.baseline = json.loads(BASELINE.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_family_ids_are_exactly_the_declared_set(self):
        self.assertEqual(getattr(rules, 'FAMILY_IDS', None), FAMILIES)

    def test_every_structural_message_has_a_finding(self):
        self.assertTrue(hasattr(rules.LintResult(), 'findings'))
        original_error, original_warn = rules.LintResult.error, rules.LintResult.warn
        emitted = []
        source = inspect.getsource(rules)
        start = source[:source.index('    lock_routes, lock_lifecycle_errors =')].count('\n') + 1
        def observer(original, severity):
            def call(result, message, **kwargs):
                frame = inspect.currentframe().f_back
                if (frame.f_code.co_name in ('xroot_plan_gate', 'xroot_design_gate') or
                    frame.f_code.co_name == 'lint_relay_root' and frame.f_lineno >= start):
                    emitted.append((severity, message))
                return original(result, message, **kwargs)
            return call
        original_add_finding = rules.LintResult.add_finding
        def add_finding(result, finding):
            emitted.append((finding.lint_severity, finding.message))
            return original_add_finding(result, finding)
        with patch.object(rules.LintResult, 'error', observer(original_error, 'error')), \
             patch.object(rules.LintResult, 'warn', observer(original_warn, 'warn')), \
             patch.object(rules.LintResult, 'add_finding', add_finding):
            for root in self.baseline['roots']:
                emitted.clear()
                result = rules.lint_relay_root(self.root / root)
                self.assertEqual(Counter(emitted), Counter((f.lint_severity, f.message) for f in result.findings), root)
                for finding in result.findings:
                    self.assertTrue(finding.subject, root)
                    self.assertIn(finding.id, FAMILIES)

    def test_helper_returning_bare_string_is_detected(self):
        self.assertTrue(hasattr(rules.LintResult, 'add_finding'))
        root = self.root / 'tools/relay-lint-fixtures/mastertier/MT37-pair-route-control'
        with patch.object(rules, 'h27_receiving_seat_errors', return_value=['untagged branch']):
            with self.assertRaises(TypeError):
                rules.lint_relay_root(root)
        rules.lint_relay_root(root)

    def test_helper_finding_without_subject_is_detected(self):
        self.assertTrue(hasattr(rules, 'Finding'))
        finding = rules.Finding('lineage.receiving-seat', 'error', '', (), {}, 'broken metadata')
        with patch.object(rules, 'h27_receiving_seat_errors', return_value=[finding]):
            result = rules.lint_relay_root(self.root / 'tools/relay-lint-fixtures/mastertier/MT37-pair-route-control')
        with self.assertRaises(AssertionError):
            for finding in result.findings:
                self.assertTrue(finding.subject)

    def test_no_structural_site_emits_untagged(self):
        source = inspect.getsource(rules)
        self.assertEqual(untagged_sites(source), [])
        altered = source.replace('    lock_routes, lock_lifecycle_errors =',
                                 '    result.error("planted untagged site")\n    lock_routes, lock_lifecycle_errors =')
        # A helper producer is independently checked, even with a tagged wrapper.
        altered = altered.replace('    errors: List[Finding] = []',
                                  '    errors: List[Finding] = []\n    errors.append("planted helper string")', 1)
        self.assertTrue(untagged_sites(altered))

    def test_messages_and_order_unchanged(self):
        actual = collect(rules, self.root, self.baseline['roots'])
        self.assertEqual(actual, collect(base_module('rules'), self.root, self.baseline['roots']))
        values = next(v['errors'] for v in actual.values() if len(v['errors']) > 1 and v['errors'][0] != v['errors'][1])
        swapped = values.copy()
        swapped[0], swapped[1] = swapped[1], swapped[0]
        self.assertNotEqual(values, swapped)
        self.assertEqual(sorted(values), sorted(swapped))

    def test_finding_subject_is_the_relay_the_helper_judged(self):
        root = self.root / 'tools/relay-lint-fixtures/mastertier/MT45-reviewer-design-origin'
        result = rules.lint_relay_root(root)
        self.assertTrue(hasattr(result, 'findings'))
        lifecycle = [f for f in result.findings if f.id == 'lineage.lock-lifecycle']
        self.assertTrue(lifecycle)
        for finding in lifecycle:
            self.assertEqual(finding.subject, '03-consumer.md')
            self.assertIn('01-origin.md', finding.operands)
