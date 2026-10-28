"""Permission seam fixtures never lend mutable modes to other runs."""
import contextlib
import io
import importlib.util
import os
from pathlib import Path
import stat
import unittest
from unittest import mock

from relay_engine.tests.test_root_lint_corrections import cwd


class TestFixtureSeamCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[2] / 'check-relay-lint-fixtures.py'
        spec = importlib.util.spec_from_file_location('fixture_seam_runner', source)
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def shared_modes(self):
        return {rel: stat.S_IMODE((self.runner.FIXTURES / path).stat().st_mode)
                for rel, path in self.runner.A5_SEAM.items()}

    def test_private_seam_never_touches_the_shared_tree(self):
        self.assertTrue(callable(getattr(self.runner, 'a5_private_seam', None)))
        expected = self.shared_modes()
        self.assertEqual(self.shared_modes(), expected)
        for rel in self.runner.A5_SEAM:
            with self.subTest(rel=rel):
                with self.runner.a5_private_seam(rel) as (target, working):
                    private = target.parents[len(Path(rel).parts) - 1]
                    seam = private / self.runner.A5_SEAM[rel]
                    self.assertNotEqual(target, self.runner.FIXTURES / rel)
                    self.assertEqual(self.shared_modes(), expected)
                    self.assertFalse(os.access(seam, os.R_OK))
                    self.assertEqual(stat.S_IMODE(seam.stat().st_mode), 0)
                    self.assertEqual(working, private / self.runner.A5_CWD[rel])
                    self.assertTrue(working.is_dir())
                self.assertFalse(private.exists())
                self.assertEqual(self.shared_modes(), expected)

    def test_seam_rows_keep_their_error_sets(self):
        self.assertTrue(callable(getattr(self.runner, 'a5_private_seam', None)))
        lint = self.runner.load_linter()
        kinds = {rel: kind for kind, rel, _ in self.runner.EXPECTED}
        for rel in self.runner.A5_SEAM:
            with self.subTest(rel=rel), self.runner.a5_private_seam(rel) as (target, working):
                with cwd(working):
                    result = lint.lint_file(target) if kinds[rel] == 'file' else lint.lint_relay_root(target)
                self.assertEqual(sorted(result.errors), sorted(self.runner.EXPECTED_ERROR_SET[rel]))

    def test_both_loops_never_chmod_the_shared_tree(self):
        import mtbattery
        rows = [row for row in self.runner.EXPECTED if row[1] in self.runner.A5_SEAM]
        self.assertEqual(len(rows), 2)
        shared = self.runner.FIXTURES.resolve()
        chmod = os.chmod
        before = self.shared_modes()
        def guard(path, *args, **kwargs):
            if Path(path).absolute().is_relative_to(shared):
                raise AssertionError('chmod touched the shared fixture tree: '+str(path))
            return chmod(path, *args, **kwargs)
        output = io.StringIO()
        with mock.patch.object(self.runner, 'EXPECTED', rows), \
                mock.patch.object(self.runner, 'xroot_cases', return_value=()), \
                mock.patch.object(os, 'chmod', guard), contextlib.redirect_stdout(output):
            self.assertEqual(self.runner.main(), 0)
            actual = mtbattery.exact_error_sets(self.runner)
        self.assertEqual(actual, {(kind, rel): frozenset(self.runner.EXPECTED_ERROR_SET[rel])
                                  for kind, rel, _ in rows})
        for _, rel, _ in rows:
            self.assertIn(rel+': expected=1 observed=1 PASS', output.getvalue())
        self.assertEqual(self.shared_modes(), before)
