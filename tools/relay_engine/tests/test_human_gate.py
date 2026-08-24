from pathlib import Path
import unittest

from relay_engine import rules


FIXTURES = Path(__file__).parents[2] / "relay-lint-fixtures" / "humangate"
WARNING = (
    "HUMAN_GATE_REQUIRED: yes carries no annotation naming its ask; "
    "annotate as 'yes — <the decision>'"
)


class TestHumanGateAnnotationWarning(unittest.TestCase):
    def test_bare_yes_warns_once_and_remains_exit_neutral(self):
        result = rules.lint_file(FIXTURES / "HG1-bare-yes.md")

        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [WARNING])
        self.assertTrue(result.ok)

    def test_annotated_yes_and_no_are_silent(self):
        for fixture in ("HG2-annotated-yes.md", "HG4-bare-no.md"):
            with self.subTest(fixture=fixture):
                result = rules.lint_file(FIXTURES / fixture)

                self.assertEqual(result.errors, [])
                self.assertEqual(result.warnings, [])
                self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
