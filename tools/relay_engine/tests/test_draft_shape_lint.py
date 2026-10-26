"""Public lint surfaces agree on draft-shape refusals and quote controls."""
import ast
import copy
import tempfile
import unittest
from pathlib import Path

from relay_engine import envelope, rules
from relay_engine.tests.test_draft_shape import diagnostic_texts, draft
from relay_engine.tests.test_parity import _standalone


def expected_shape_sweep(baseline):
    """PL-v297-b2 tasks 2/5: exact additions and draft-warning removals."""
    expected = copy.deepcopy(baseline)
    errors = expected['tools/relay-lint-fixtures']['errors']
    for index, path, line, column in (
            (248, 'lockdigest/FB8-u2028-pseudo-closer.md', 20, 4),
            (271, 'lockdigest/FLD24-u2028-field-decoy.md', 10, 13),
            (1080, 'rootres/XR16-u2028-marker-decoy/INDEX.md', 4, 11)):
        errors.insert(index, f'{path}: line {line}, column {column}: U+2028 LINE SEPARATOR '
                      'is a line break to some readers and ordinary text to others; '
                      'use a newline there, or write the character as text, for example U+2028')
    warnings = expected['tools/relay-lint-fixtures']['warnings']
    for filename in ('draft-plan-noroot.md', 'draft-plan.md'):
        warnings.remove('plancontract/PC10-draft-explicit-root/.engine/drafts/v29-x.planner/'
                        + filename + ": plan shape: PLAN_ARTIFACT 'fixture-plan-draft' resolves at no probe root; the relay body was measured")
    return expected


def shape_messages(result):
    return [e for e in result.errors if any(s in e for s in (
        'is a line break to some readers', 'outside the header block',
        "outside this grant's header block"))]


class TestDraftShapeLint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hand = _standalone()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'SITREP-pair-planner-20261026-010000.md'

    def write(self, text, path=None):
        path = path or self.path
        path.write_bytes(text.encode('utf-8'))
        return path

    def test_hand_and_engine_messages_agree(self):
        for text in diagnostic_texts():
            with self.subTest(text=text):
                self.assertEqual(self.hand.draft_shape_errors(text),
                                 rules.draft_shape_errors(text))

    def test_lint_file_reports_each_kind_in_both_linters(self):
        for text in (draft(body='a\u2028b'), draft(body='SUPERSEDES: x'),
                     draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: no')):
            message = envelope.shape_message(envelope.draft_shape(text))
            for module in (self.hand, rules):
                for template in (False, True):
                    result = module.lint_file(self.write(text), template_mode=template)
                    self.assertEqual(shape_messages(result), [message])
                    self.assertTrue(result.errors)

    def test_files_without_a_shape_gain_no_error(self):
        for ending in ('\n', '\r\n'):
            path = self.write(draft().replace('\n', ending))
            for module in (self.hand, rules):
                self.assertEqual(shape_messages(module.lint_file(path)), [])

    def test_index_reports_separators_only(self):
        path = self.root / 'INDEX.md'
        for text in ('# INDEX\nabc\u2028tail', draft(body='SUPERSEDES: x')):
            self.write(text, path)
            shape = envelope.first_separator(text)
            expected = [] if shape is None else [envelope.shape_message(shape)]
            for module in (self.hand, rules):
                self.assertEqual(shape_messages(module.lint_relay_index(path)), expected)

    def test_root_lint_prefixes_the_shape_error(self):
        text = draft(body='abc\u2028tail')
        self.write(text)
        message = self.path.name + ': ' + envelope.shape_message(envelope.draft_shape(text))
        for module in (self.hand, rules):
            self.assertEqual(shape_messages(module.lint_relay_root(self.root)), [message])

    def test_same_line_shapes_lint_as_the_late_shape(self):
        for text in (draft(body='SUPERSEDES: x\u2028'),
                     draft(body='SUPERSEDES: x\x85'),
                     draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: no\u2029')):
            shape = envelope.draft_shape(text)
            self.assertIn(shape.kind, ('late-control', 'late-verdict'))
            for module in (self.hand, rules):
                result = module.lint_file(self.write(text))
                self.assertEqual(shape_messages(result), [envelope.shape_message(shape)])
                self.assertTrue(result.errors)

    def test_hand_linter_imports_no_engine_module(self):
        tree = ast.parse(Path(self.hand.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            self.assertFalse(any(name.startswith('relay_engine') for name in names))
