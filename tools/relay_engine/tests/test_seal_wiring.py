from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from relay_engine import seal_files, seal_wiring
from relay_engine.seal_types import RunResult


class ExclusionTests(unittest.TestCase):
    def test_runner_launches_tmutil_only(self):
        custody = seal_wiring.TmutilCustody(stopping=lambda: False)
        with mock.patch.object(subprocess, 'Popen', autospec=True) as launch:
            for argv in (['echo', 'hello'], ['/usr/bin/tmutil', 'isexcluded', '/scratch'], []):
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    custody.run(argv)
            launch.assert_not_called()
            launch.return_value.communicate.return_value = (b'[Excluded]', b'')
            launch.return_value.returncode = 0
            result = custody.run(['tmutil', 'isexcluded', '/scratch/seal name'])
            self.assertEqual((result.returncode, result.stdout), (0, b'[Excluded]'))
            launch.assert_called_once_with(
                ['/usr/bin/tmutil', 'isexcluded', '/scratch/seal name'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, start_new_session=True,
            )
            launch.return_value.communicate.assert_called_once_with(timeout=10)

    def test_exclusion_raise_and_timeout_read_check_failed(self):
        exceptions = (subprocess.TimeoutExpired(['tmutil'], 10), OSError('injected step failure'))
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'seal'
            for error in exceptions:
                with self.subTest(error=type(error).__name__):
                    included = RunResult(0, b'[Included]')
                    with mock.patch.object(seal_files, '_EXCLUSION_KIND', 'tmutil', create=True):
                        for final, expected in ((RunResult(0, b'[Excluded]'), 'excluded'),
                                                (error, 'check_failed')):
                            run = mock.Mock(side_effect=[included, error, final])
                            self.assertEqual(seal_wiring.apply_exclusion(root, run), expected)
                            self.assertEqual(run.call_args_list, [
                                mock.call(['tmutil', verb, str(root)]) for verb in
                                ('isexcluded', 'addexclusion', 'isexcluded')
                            ])
                    if sys.platform != 'darwin':
                        run = mock.Mock(side_effect=error)
                        with mock.patch.object(seal_files.SealFolder, 'apply_backup_exclusion', side_effect=error):
                            self.assertEqual(seal_wiring.apply_exclusion(root, run), 'check_failed')
                        run.assert_not_called()

    def test_isolated_run_one_check(self):
        published = threading.Event()
        custody = seal_wiring.TmutilCustody(stopping=published.is_set)
        with (tempfile.TemporaryDirectory() as scratch,
              mock.patch.object(seal_wiring, '_isolated_calls', []),
              mock.patch.object(seal_wiring, '_spawn_tmutil',
                                side_effect=AssertionError('unexpected launch')) as launch,
              mock.patch.object(seal_files, '_EXCLUSION_KIND', 'tmutil')):
            root = Path(scratch) / 'seal'
            self.assertEqual(seal_wiring.apply_exclusion(root, custody.run_isolated), 'excluded')
            self.assertEqual(seal_wiring._isolated_calls, [('tmutil', 'isexcluded', str(root))])
            self.assertFalse((root / 'CACHEDIR.TAG').exists())
            published.set()
            with self.assertRaises(seal_wiring.ExclusionStopped):
                custody.run_isolated(['tmutil', 'addexclusion', str(root)])
            self.assertEqual(seal_wiring._isolated_calls, [('tmutil', 'isexcluded', str(root))])
            launch.assert_not_called()


class CustodyTests(unittest.TestCase):
    check = ('tmutil', 'isexcluded', '/scratch/seal')
    add = ('tmutil', 'addexclusion', '/scratch/seal')

    def setUp(self):
        self.published = threading.Event()
        self.custody = seal_wiring.TmutilCustody(stopping=self.published.is_set)
        self.child = mock.Mock(returncode=0)
        self.child.communicate.return_value = (b'[Included]', b'')
        self.launch = self.enterContext(mock.patch.object(seal_wiring, '_spawn_tmutil',
                                                         return_value=self.child))

    def thread_call(self, action):
        results = []
        def call():
            try:
                results.append(action())
            except BaseException as exc:
                results.append(exc)
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        def collect():
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            return results[0]
        self.addCleanup(collect)
        return collect

    def expect_event(self, event):
        if not event.wait(3):
            raise AssertionError('fixture event did not arrive')

    def wait_for_kill(self, *, hold_reap=False):
        started = threading.Event()
        killed = threading.Event()
        reaping = threading.Event()
        def communicate(timeout):
            self.assertEqual(timeout, 10)
            started.set()
            self.expect_event(killed)
            if hold_reap:
                self.expect_event(reaping)
            self.child.returncode = -9
            return b'', b''
        self.child.communicate.side_effect = communicate
        self.child.kill.side_effect = killed.set
        return started, killed, reaping

    def test_tmutil_custody_stop_in_flight(self):
        started, killed, _ = self.wait_for_kill()
        collect = self.thread_call(lambda: self.custody.run(self.check))
        self.addCleanup(killed.set)
        self.expect_event(started)
        self.assertIs(self.custody.close(time.monotonic() + 2), True)
        self.assertIsInstance(collect(), seal_wiring.ExclusionKilled)
        self.child.kill.assert_called_once_with()
        self.assertEqual(self.child.returncode, -9)
        self.assertIs(self.custody.close(time.monotonic() + 2), True)

    def test_tmutil_custody_stop_between_calls(self):
        self.custody.run(self.check)
        self.assertIs(self.custody.close(time.monotonic() + 1), True)
        with self.assertRaises(seal_wiring.ExclusionStopped):
            self.custody.run(self.add)
        self.launch.assert_called_once_with(self.check)

    def test_tmutil_custody_stop_before_first_launch(self):
        self.published.set()
        with self.assertRaises(seal_wiring.ExclusionStopped):
            self.custody.run(self.check)
        self.launch.assert_not_called()

    def test_tmutil_custody_stop_between_calls_fencer_delayed(self):
        self.custody.run(self.check)
        self.published.set()
        with self.assertRaises(seal_wiring.ExclusionStopped):
            self.custody.run(self.add)
        self.launch.assert_called_once_with(self.check)
        self.assertIs(self.custody.close(time.monotonic() + 1), True)

    def blocked_launch(self):
        entered, release = threading.Event(), threading.Event()
        def launch(argv):
            entered.set()
            self.expect_event(release)
            return self.child
        self.launch.side_effect = launch
        return entered, release

    def test_tmutil_custody_close_waits_for_launch(self):
        entered, release = self.blocked_launch()
        _, killed, _ = self.wait_for_kill()
        run = self.thread_call(lambda: self.custody.run(self.check))
        self.addCleanup(release.set)
        self.addCleanup(killed.set)
        self.expect_event(entered)
        closing, returned = threading.Event(), threading.Event()
        def close():
            closing.set()
            try:
                return self.custody.close(time.monotonic() + 2)
            finally:
                returned.set()
        finish_close = self.thread_call(close)
        self.addCleanup(release.set)
        self.expect_event(closing)
        self.assertFalse(returned.wait(0.04))
        self.child.kill.assert_not_called()
        release.set()
        self.assertIs(finish_close(), True)
        self.assertIsInstance(run(), seal_wiring.ExclusionKilled)
        self.child.kill.assert_called_once_with()

    def test_tmutil_custody_late_handle_killed(self):
        entered, release = self.blocked_launch()
        run = self.thread_call(lambda: self.custody.run(self.check))
        self.addCleanup(release.set)
        self.expect_event(entered)
        self.published.set()
        fence = self.custody.close(time.monotonic() + 0.04)
        self.assertIs(fence, False)
        self.child.kill.assert_not_called()
        release.set()
        self.assertIsInstance(run(), seal_wiring.ExclusionKilled)
        self.assertEqual(self.child.mock_calls, [
            mock.call.kill(), mock.call.wait(), mock.call.stdout.close(), mock.call.stderr.close(),
        ])
        self.assertIs(fence, False)

    def test_tmutil_custody_unreaped_by_deadline(self):
        started, killed, reap = self.wait_for_kill(hold_reap=True)
        run = self.thread_call(lambda: self.custody.run(self.check))
        self.addCleanup(killed.set)
        self.addCleanup(reap.set)
        self.expect_event(started)
        self.assertIs(self.custody.close(time.monotonic() + 0.04), False)
        self.child.kill.assert_called_once_with()
        self.assertEqual(self.child.returncode, 0)
        reap.set()
        self.assertIsInstance(run(), seal_wiring.ExclusionKilled)
        self.assertIs(self.custody.close(time.monotonic() + 1), True)
