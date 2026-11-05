"""Structural findings retain identity independently of their display text."""
from relay_engine.tests.test_judgment_store import base_module
import ast
from collections import Counter
import inspect
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import rules
from relay_engine.tests.test_draft_shape_lint import expected_shape_sweep

REPO = Path(__file__).resolve().parents[3]
BASELINE = REPO / 'tools/relay-engine-fixtures/judging/base-order-cbc8f07.json'
BASE = 'cbc8f071cd593af4c48994ec30b0775b3c919ad6'
# The linter's fixture tree, copied for these tests (DD-v3-tests § 3.4, plan Task 13):
# its files at HEAD 2b4f83e less README.md and lockpath/LP2-new-form, which name it.
POPULATION = 'tools/relay-engine-fixtures/lint-population'
# Cases added after cbc8f07: they pin merge-claim errors that cbc8f07's rules do not
# emit, so the order check, which compares the current rules with those, leaves them
# out (ruling v3-engine-pr9-ruling-2); test_judging_agreement reads them.
LATER_CASES = ('merge-token/MT10-field-grant-to-planner', 'merge-token/MT11-token-beside-denial')


def snapshot(destination):
    """The order population: tools/relay-engine-fixtures as at BASE, from Git,
    with the copied lint population beside it."""
    archive = subprocess.check_output(['git', 'archive', BASE, 'tools/relay-engine-fixtures'], cwd=REPO)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(destination, filter='data')
    shutil.copytree(REPO / POPULATION, Path(destination) / POPULATION)
    for case in LATER_CASES:
        shutil.rmtree(Path(destination) / POPULATION / case)


def root_list(root):
    paths = [POPULATION]
    for parent in ('tools/relay-engine-fixtures/corpora', 'tools/relay-engine-fixtures/record-root'):
        paths.extend(str(p.relative_to(root)) for p in sorted((root / parent).iterdir()) if p.is_dir())
    return paths


def collect(module, root, paths):
    results = {}
    for name in paths:
        result = module.lint_relay_root(root / name)
        results[name] = {key: [msg.replace(str(root), '<fixture-root>') for msg in getattr(result, key)]
                         for key in ('errors', 'warnings')}
    return results
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

    def test_later_cases_stay_out_of_the_order_population(self):
        # the control for LATER_CASES: on them the two rule sets disagree
        base = base_module('rules')
        for case in LATER_CASES:
            with self.subTest(case=case):
                self.assertFalse((self.root / POPULATION / case).exists())
                root = REPO / POPULATION / case
                self.assertNotEqual(rules.lint_relay_root(root).errors, base.lint_relay_root(root).errors)

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
        root = self.root / POPULATION / 'mastertier/MT37-pair-route-control'
        with patch.object(rules, 'h27_receiving_seat_errors', return_value=['untagged branch']):
            with self.assertRaises(TypeError):
                rules.lint_relay_root(root)
        rules.lint_relay_root(root)

    def test_helper_finding_without_subject_is_detected(self):
        self.assertTrue(hasattr(rules, 'Finding'))
        finding = rules.Finding('lineage.receiving-seat', 'error', '', (), {}, 'broken metadata')
        with patch.object(rules, 'h27_receiving_seat_errors', return_value=[finding]):
            result = rules.lint_relay_root(self.root / POPULATION / 'mastertier/MT37-pair-route-control')
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
        self.assertEqual(actual, expected_shape_sweep(
            collect(base_module('rules'), self.root, self.baseline['roots'])))
        values = next(v['errors'] for v in actual.values() if len(v['errors']) > 1 and v['errors'][0] != v['errors'][1])
        swapped = values.copy()
        swapped[0], swapped[1] = swapped[1], swapped[0]
        self.assertNotEqual(values, swapped)
        self.assertEqual(sorted(values), sorted(swapped))

    def test_finding_subject_is_the_relay_the_helper_judged(self):
        root = self.root / POPULATION / 'mastertier/MT45-reviewer-design-origin'
        result = rules.lint_relay_root(root)
        self.assertTrue(hasattr(result, 'findings'))
        lifecycle = [f for f in result.findings if f.id == 'lineage.lock-lifecycle']
        self.assertTrue(lifecycle)
        for finding in lifecycle:
            self.assertEqual(finding.subject, '03-consumer.md')
            self.assertIn('01-origin.md', finding.operands)
