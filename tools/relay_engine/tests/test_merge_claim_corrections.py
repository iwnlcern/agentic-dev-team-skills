"""Merge-claim prose boundaries retain operational claims."""
from pathlib import Path
import tempfile
import unittest

from relay_engine import rules
from relay_engine.tests.test_parity import _standalone
from relay_engine.tests.test_merge_claim import MERGE_ERROR


CASES = (
    ('ACTIONS_GIT_REF: none — read-only reads of merged abc\n', False),
    ('ACTIONS_GIT_REF: none\n— merged abc\n', True),
    ('SUBJECT: merge == x\n', False),
    ("SUBJECT: merge = the operator's separate gate\n", False),
    ('SUBJECT: merge=abc123\n', False),
    ('ACTIONS_GIT_REF: merge=abc123\n', True),
    ('Body\nmerge=abc123\n', True),
    ('ESCALATION_SCAN: merge=abc123\n', False),
    ('```\nmerge=abc123\n```\n', False),
    ('`merge=abc123`\n', False),
)


class TestMergeClaimCorrections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.linters = (rules, _standalone())

    def check_cases(self, cases):
        for module in self.linters:
            for text, expected in cases:
                with self.subTest(module=module.__name__, text=text):
                    self.assertIs(module.merge_claimed(text, module.header_fields(text)), expected)

    def test_one_line_no_action_exemption_holds(self):
        self.check_cases(CASES[:1])

    def test_continuation_line_claim_is_not_exempt(self):
        self.check_cases(CASES[1:2])
        header = ('ROLE: Pair Planner\nPHASE: SITREP\nDISPATCH_ID: claim\n'
                  'FROM: p.planner\nTO: p.implementer\nSUBJECT: status\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'SITREP-pair-planner-20261026-010000.md').write_text(header + CASES[1][0])
            for module in self.linters:
                self.assertTrue(any(MERGE_ERROR in e for e in module.lint_relay_root(root).errors))

    def test_subject_prose_is_not_a_claim(self):
        self.check_cases(CASES[2:5])

    def test_real_claims_still_claim(self):
        self.check_cases(CASES[5:7])

    def test_inert_places_stay_inert(self):
        self.check_cases(CASES[7:])

    def test_hand_and_engine_merge_claimed_agree(self):
        for text, _ in CASES:
            self.assertEqual(*(m.merge_claimed(text, m.header_fields(text)) for m in self.linters))
