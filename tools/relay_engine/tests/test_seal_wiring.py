from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from relay_engine import seal_files


class ExclusionTests(unittest.TestCase):
    def test_runner_launches_tmutil_only(self):
        from relay_engine import seal_wiring

        with mock.patch.object(subprocess, 'run', autospec=True) as launch:
            for argv in (['echo', 'hello'], ['/usr/bin/tmutil', 'isexcluded', '/scratch'], []):
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    seal_wiring._run_tmutil(argv)
            launch.assert_not_called()
            result = seal_wiring._run_tmutil(['tmutil', 'isexcluded', '/scratch/seal name'])
            self.assertIs(result, launch.return_value)
            launch.assert_called_once_with(
                ['/usr/bin/tmutil', 'isexcluded', '/scratch/seal name'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, timeout=10,
            )

    def test_exclusion_raise_and_timeout_read_check_failed(self):
        from relay_engine import seal_wiring

        exceptions = (subprocess.TimeoutExpired(['tmutil'], 10), OSError('injected step failure'))
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'seal'
            for error in exceptions:
                with self.subTest(error=type(error).__name__):
                    run = mock.Mock(side_effect=error)
                    if sys.platform == 'darwin':
                        self.assertEqual(seal_wiring.apply_exclusion(root, run), 'check_failed')
                        run.assert_called_once_with(['tmutil', 'addexclusion', str(root)])
                    else:
                        with mock.patch.object(seal_files.SealFolder, 'apply_backup_exclusion', side_effect=error):
                            self.assertEqual(seal_wiring.apply_exclusion(root, run), 'check_failed')
                        run.assert_not_called()

    def test_isolated_run_launches_nothing(self):
        from relay_engine import seal_wiring

        with (tempfile.TemporaryDirectory() as scratch,
              mock.patch.object(seal_wiring, '_isolated_calls', []),
              mock.patch.object(subprocess, 'run', side_effect=AssertionError('unexpected launch')) as launch):
            root = Path(scratch) / 'seal'
            self.assertEqual(seal_wiring.apply_exclusion(root, seal_wiring._isolated_run), 'excluded')
            launch.assert_not_called()
            if sys.platform == 'darwin':
                self.assertEqual(seal_wiring._isolated_calls, [
                    ('tmutil', 'addexclusion', str(root)), ('tmutil', 'isexcluded', str(root)),
                ])
                self.assertFalse((root / 'CACHEDIR.TAG').exists())
            else:
                self.assertEqual(seal_wiring._isolated_calls, [])
                self.assertEqual(
                    (root / 'CACHEDIR.TAG').read_bytes(),
                    b'Signature: 8a477f597d28d172789f06886806bc55\n',
                )
