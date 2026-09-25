import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from relay_engine import rules


SCRIPT = Path(__file__).parents[2] / "relay-lint.py"
MERGE_ERROR = "relay claims a merge/merge commit without an earlier MERGE-GATE authorization"


def _standalone():
    spec = importlib.util.spec_from_file_location("task5_relay_lint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMergeClaim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.linters = (rules, _standalone())

    def assert_claim(self, body, expected):
        for module in self.linters:
            with self.subTest(module=module.__name__, body=body):
                self.assertIs(module.merge_claimed(body, module.header_fields(body)), expected)

    def test_no_action_ref_is_not_a_claim(self):
        self.assert_claim(
            "ACTIONS_GIT_REF: none — read-only reads of the merged adapter on main\n", False
        )
        self.assert_claim(
            "ACTIONS_GIT_REF: read-only reads of the merged adapter on main\n", True
        )

        self.assert_claim("ACTIONS_GIT_REF: none merge commit abc123\n", True)
        self.assert_claim("ACTIONS_GIT_REF: none: merge commit abc123\n", True)
        self.assert_claim("ACTIONS_GIT_REF: none—merge commit abc123\n", True)
        self.assert_claim(
            "ACTIONS_GIT_REF: nonexistent branch, merge commit abc123\n", True
        )
        self.assert_claim(
            "ACTIONS_GIT_REF: merge commit abc123 on main\n"
            "  superseding an earlier ACTIONS_GIT_REF: none — draft value\n", True
        )
        self.assert_claim(
            "ACTIONS_GIT_REF: NONE — read-only reads of merge commit abc123\n", False
        )

    def test_no_action_ref_with_machine_form_still_claims(self):
        self.assert_claim(
            "ACTIONS_GIT_REF: none — read-only reads of the merged adapter on main\n"
            "MERGE_RECORD: merge=abc123\n", True
        )

    def test_genuine_claims_still_claim(self):
        self.assert_claim("ACTIONS_GIT_REF: merge=abc123\n", True)
        self.assert_claim("ACTIONS_GIT_REF: commit abc123 — merged to main\n", True)

    def test_negated_phrases_unchanged(self):
        self.assert_claim(
            "ACTIONS_GIT_REF: NOT merged to main; awaiting MERGE-GATE authorization\n",
            False,
        )

    def test_root_check_consumes_the_rule(self):
        header = (
            "# SITREP\n\n"
            "ROLE: Pair Planner\nPHASE: SITREP\n"
            "DISPATCH_ID: task5-sitrep\nFROM: task5.planner\n"
            "TO: task5.implementer\nSUBJECT: status\n"
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relay = root / "SITREP-pair-planner-20260924-120000.md"
            for action, expected in (
                ("none — read-only reads of the merged adapter on main", False),
                ("merge=abc123", True),
            ):
                relay.write_text(header + "ACTIONS_GIT_REF: " + action + "\n")
                for module in self.linters:
                    with self.subTest(module=module.__name__, action=action):
                        merge_errors = [error for error in module.lint_relay_root(root).errors
                                        if MERGE_ERROR in error]
                        self.assertIs(bool(merge_errors), expected)
