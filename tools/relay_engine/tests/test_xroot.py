from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from relay_engine import rules
import xrootfixgen


TOOLS = Path(__file__).parents[2]
SCRIPT = TOOLS / "relay-lint.py"
TIMEOUT_CASE = "xroot/X43-fifo-object-timeout"


def _standalone():
    spec = importlib.util.spec_from_file_location(
        "v291_xroot_relay_lint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verdict(result):
    return (sorted(result.errors), sorted(result.warnings),
            1 if result.errors else 0)


def _fd_census():
    return set(os.listdir("/dev/fd"))


class TestXroot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="native-xroot-")
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.cases = xrootfixgen.materialize(Path(cls._temporary.name))
        cls.by_name = {label: (root, expected_exit, expected_errors)
                       for label, root, expected_exit, expected_errors
                       in cls.cases}
        cls.live = _standalone()

    @contextmanager
    def case_budget(self, module, label):
        from relay_engine.tests import xroot_narrow_corpus as corpus
        if label != TIMEOUT_CASE:
            with corpus.budget(module):
                yield
            return
        now = [0]
        original = module._xroot_start_git
        def launch(command, **kwargs):
            process = original(command, **kwargs)
            if any('a'*40 in str(arg) for arg in command):
                now[0] = 6
            return process
        # Start the FIFO query, then expire at that exact process boundary.
        # Earlier classification work cannot consume the test's deadline.
        with mock.patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(module, '_xroot_start_git', side_effect=launch):
            yield

    def test_truth_matches_full_generator_census(self):
        self.assertEqual(130, len(self.cases))
        for label, root, expected_exit, expected_errors in self.cases:
            with self.subTest(label=label), self.case_budget(rules, label):
                result = rules.lint_relay_root(root)
                self.assertEqual(expected_exit, 1 if result.errors else 0)
                self.assertEqual(sorted(expected_errors), sorted(result.errors))

    def test_native_and_standalone_verdicts_are_equivalent(self):
        for label, root, _expected_exit, _expected_errors in self.cases:
            with self.subTest(label=label):
                with self.case_budget(self.live, label):
                    expected = _verdict(self.live.lint_relay_root(root))
                with self.case_budget(rules, label):
                    actual = _verdict(rules.lint_relay_root(root))
                self.assertEqual(expected, actual)

    def test_missing_git_maps_to_repository_axis(self):
        root, _expected_exit, _expected_errors = self.by_name[
            "xroot/XV-valid-edge"]
        with tempfile.TemporaryDirectory(prefix="no-git-path-") as empty_path:
            with mock.patch.dict(os.environ, {"PATH": empty_path}):
                with self.case_budget(self.live, 'missing-git'):
                    expected = _verdict(self.live.lint_relay_root(root))
                with self.case_budget(rules, 'missing-git'):
                    actual = _verdict(rules.lint_relay_root(root))
        self.assertEqual(expected, actual)
        self.assertEqual(
            ["PLAN-pair-planner-20260817-120000.md: "
             "declared design edge failed verification: repository: "
             "git executable is unavailable"],
            actual[0],
        )

    def test_nonzero_binary_and_text_queries_match_standalone(self):
        labels = (
            "xroot/X9-unknown-commit",
            "xroot/X18b-bytes-preserving-name",
            "xroot/X19-digest-mismatch",
        )
        for label in labels:
            root, _expected_exit, _expected_errors = self.by_name[label]
            with self.subTest(label=label):
                with self.case_budget(self.live, label):
                    expected = _verdict(self.live.lint_relay_root(root))
                with self.case_budget(rules, label):
                    actual = _verdict(rules.lint_relay_root(root))
                self.assertEqual(expected, actual)

    def test_fifo_timeout_is_bounded_reaped_and_descriptor_clean(self):
        root, expected_exit, expected_errors = self.by_name[TIMEOUT_CASE]
        spawned = []
        real_spawn = rules._xroot_start_git

        def recording_spawn(*args, **kwargs):
            process = real_spawn(*args, **kwargs)
            spawned.append(process.pid)
            return process

        before = _fd_census()
        with mock.patch.object(rules, "_xroot_start_git", side_effect=recording_spawn), self.case_budget(rules, TIMEOUT_CASE):
            result = rules.lint_relay_root(root)
        after = _fd_census()

        self.assertEqual(expected_exit, 1 if result.errors else 0)
        self.assertEqual(sorted(expected_errors), sorted(result.errors))
        self.assertTrue(spawned)
        for pid in spawned:
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
