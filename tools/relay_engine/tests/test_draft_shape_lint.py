"""Public lint surfaces agree on draft-shape refusals and quote controls."""
import copy
import tempfile
import unittest
from pathlib import Path

from relay_engine import envelope, rules
from relay_engine.tests.test_draft_shape import draft


POPULATION = 'tools/relay-engine-fixtures/lint-population'


def expected_shape_sweep(baseline):
    """PL-v297-b2 tasks 2/5: exact additions and draft-warning removals.
    Positions in the copied population (plan Task 13): 248, 271 and 1080 in
    the linter's own tree, less the 8 README.md errors that preceded them."""
    expected = copy.deepcopy(baseline)
    errors = expected[POPULATION]['errors']
    for index, path, line, column in (
            (240, 'lockdigest/FB8-u2028-pseudo-closer.md', 20, 4),
            (263, 'lockdigest/FLD24-u2028-field-decoy.md', 10, 13),
            (1072, 'rootres/XR16-u2028-marker-decoy/INDEX.md', 4, 11)):
        errors.insert(index, f'{path}: line {line}, column {column}: U+2028 LINE SEPARATOR '
                      'is a line break to some readers and ordinary text to others; '
                      'use a newline there, or write the character as text, for example U+2028')
    warnings = expected[POPULATION]['warnings']
    for filename in ('draft-plan-noroot.md', 'draft-plan.md'):
        warnings.remove('plancontract/PC10-draft-explicit-root/.engine/drafts/v29-x.planner/'
                        + filename + ": plan shape: PLAN_ARTIFACT 'fixture-plan-draft' resolves at no probe root; the relay body was measured")
    return expected


def shape_messages(result):
    return [e for e in result.errors if any(s in e for s in (
        'is a line break to some readers', 'outside the header block',
        "outside this grant's header block"))]


class TestDraftShapeLint(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'SITREP-pair-planner-20261026-010000.md'

    def write(self, text, path=None):
        path = path or self.path
        path.write_bytes(text.encode('utf-8'))
        return path

    def test_lint_file_reports_each_kind_in_both_linters(self):
        for text in (draft(body='a\u2028b'), draft(body='SUPERSEDES: x'),
                     draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: no')):
            message = envelope.shape_message(envelope.draft_shape(text))
            for module in (rules,):
                for template in (False, True):
                    result = module.lint_file(self.write(text), template_mode=template)
                    self.assertEqual(shape_messages(result), [message])
                    self.assertTrue(result.errors)

    def test_files_without_a_shape_gain_no_error(self):
        for ending in ('\n', '\r\n'):
            path = self.write(draft().replace('\n', ending))
            for module in (rules,):
                self.assertEqual(shape_messages(module.lint_file(path)), [])

    def test_index_reports_separators_only(self):
        path = self.root / 'INDEX.md'
        for text in ('# INDEX\nabc\u2028tail', draft(body='SUPERSEDES: x')):
            self.write(text, path)
            shape = envelope.first_separator(text)
            expected = [] if shape is None else [envelope.shape_message(shape)]
            for module in (rules,):
                self.assertEqual(shape_messages(module.lint_relay_index(path)), expected)

    def test_root_lint_prefixes_the_shape_error(self):
        text = draft(body='abc\u2028tail')
        self.write(text)
        message = self.path.name + ': ' + envelope.shape_message(envelope.draft_shape(text))
        for module in (rules,):
            self.assertEqual(shape_messages(module.lint_relay_root(self.root)), [message])

    def test_same_line_shapes_lint_as_the_late_shape(self):
        for text in (draft(body='SUPERSEDES: x\u2028'),
                     draft(body='SUPERSEDES: x\x85'),
                     draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: no\u2029')):
            shape = envelope.draft_shape(text)
            self.assertIn(shape.kind, ('late-control', 'late-verdict'))
            for module in (rules,):
                result = module.lint_file(self.write(text))
                self.assertEqual(shape_messages(result), [envelope.shape_message(shape)])
                self.assertTrue(result.errors)

    def test_in_block_shapes_lint_with_their_texts(self):
        # Both public linters must distinguish header values from body mentions.
        remedy = 'to mention the key in the header block, put it and its colon in inline code'
        cases = (
            (draft().replace('ROLE: Pair Planner', 'ROLE: Pair Planner see SUPERSEDES: x'), 1, 'SUPERSEDES', False),
            (draft().replace('SUBJECT: shape', 'SUBJECT: s see SUPERSEDES: x'), 11, 'SUPERSEDES', False),
            (draft('MERGE-GATE', body='DISPATCH MERGE').replace('SUBJECT: shape', 'SUBJECT: s see VERDICT: no'), 11, 'VERDICT', True))
        for text, line, key, grant in cases:
            cause = (f"line {line}: {key} appears in this grant's header block text (lines 1 to 11) but is not that line's own header, so authority never reads it as a verdict" if grant else
                     f"line {line}: {key} appears in the header block's text (lines 1 to 11) but is not that line's own header, so the engine never reads it as {key}")
            for module in (rules,):
                with self.subTest(copy=module.__name__, line=line, key=key):
                    self.assertEqual(module.draft_shape_errors(text), [cause+'; '+remedy])
                    result = module.lint_file(self.write(text))
                    self.assertEqual([e for e in result.errors if 'appears in' in e], [cause+'; '+remedy])
                    outside = draft('MERGE-GATE' if grant else 'SITREP', body=('DISPATCH MERGE\n\n' if grant else '')+'see '+key+': x')
                    messages = module.draft_shape_errors(outside)
                    outside_cause = (
                        "line 15: VERDICT is outside this grant's header block (lines 1 to 11), "
                        "so authority reads the grant as carrying no verdict" if grant else
                        "line 13: SUPERSEDES is outside the header block (lines 1 to 11), "
                        "where the engine never reads it")
                    outside_remedy = (
                        "move the line into the header block, where any value but an exact "
                        "positive word denies the grant; or remove it; or quote it in code"
                        if grant else "move the line into the header block, or quote it in code")
                    suffix = '; to mention the key, put it and its colon in inline code'
                    self.assertEqual(messages, [outside_cause + '; ' + outside_remedy + suffix])
