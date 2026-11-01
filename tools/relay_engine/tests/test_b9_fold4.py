"""Deterministic signal lifecycle controls for the test-only stop harness."""
import inspect
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest

from relay_engine import authority, rules
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests import xroot_test_stops as stops


def snapshot():
    return (stops._DEADLINE.get(), signal.getsignal(signal.SIGALRM),
            signal.getitimer(signal.ITIMER_REAL), rules._XROOT_BUDGET,
            authority.time, selectors.DefaultSelector.select,
            selectors.PollSelector.select, corpus.time.sleep,
            dict((m, s[2]) for m, s in corpus._BUDGET_SCOPES.items()),
            corpus._SELECTOR_SCOPES, tuple(getattr(stops, '_SCOPES', [])),
            signal.pthread_sigmask(signal.SIG_BLOCK, set()))


def probe(mode):
    before = snapshot()
    if mode == 'late':
        with stops.stop(100):
            handler = signal.getsignal(signal.SIGALRM)
        handler(signal.SIGALRM, sys._getframe())
    elif mode == 'pending':
        # A pending kernel alarm belongs to the exiting scope, never SIG_DFL.
        with stops.stop(100):
            oldmask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            os.kill(os.getpid(), signal.SIGALRM)
        signal.pthread_sigmask(signal.SIG_SETMASK, oldmask)
    elif mode == 'drain-race':
        from unittest import mock
        original = signal.sigpending
        injected = []
        def pending():
            found = original()
            if not injected:
                injected.append(True)
                os.kill(os.getpid(), signal.SIGALRM)
            return found
        with mock.patch.object(signal, 'sigpending', pending):
            with stops.stop(100):
                pass
        assert injected
    elif mode == 'unrelated':
        from contextlib import contextmanager
        for point in ('enter', 'exit'):
            @contextmanager
            def operation():
                if point == 'enter':
                    signal.raise_signal(signal.SIGALRM)
                yield
                if point == 'exit':
                    signal.raise_signal(signal.SIGALRM)
            caught = False
            try:
                with stops.stop(100), operation():
                    pass
            except stops.TestStop:
                caught = True
            assert caught, ('stop escaped in unrelated context manager', point)
    elif mode == 'window':
        targets = [stops.stop.__wrapped__, corpus.budget.__wrapped__,
                   corpus.unbounded_selectors.__wrapped__, corpus.Budget.__init__,
                   corpus.Budget.get, corpus.Budget.set, corpus.Budget.reset,
                   corpus.AuthorityTime.monotonic, corpus.AuthorityTime.__getattr__]
        delivered = 0
        for target in targets:
            lines, first = inspect.getsourcelines(target)
            for lineno in range(first, first+len(lines)):
                fired = []
                def trace(frame, event, arg):
                    if frame.f_code is target.__code__ and event == 'line' and frame.f_lineno == lineno and not fired:
                        fired.append(lineno)
                        try:
                            os.kill(os.getpid(), signal.SIGALRM)
                        except stops.TestStop as exc:
                            raise AssertionError('stop raised inside harness transition') from exc
                    return trace
                try:
                    with stops.stop(100):
                        sys.settrace(trace)
                        try:
                            with corpus.budget(rules):
                                authority.time.monotonic()
                                authority.time.perf_counter()
                                token = rules._XROOT_BUDGET.set((float('inf'), []))
                                rules._XROOT_BUDGET.get()
                                rules._XROOT_BUDGET.reset(token)
                        finally:
                            sys.settrace(None)
                except stops.TestStop:
                    pass
                delivered += bool(fired)
                if snapshot() != before:
                    raise AssertionError(('state leaked at', target.__name__, lineno))
        assert delivered > 20, delivered
    elif mode == 'cascade':
        # Queue an alarm for cleanup to consume, then exercise later lifts.
        target = stops.stop.__wrapped__
        lines, first = inspect.getsourcelines(target)
        lineno = next(first+i for i, line in enumerate(lines) if 'signal.setitimer(signal.ITIMER_REAL, 0)' in line or 'signal.signal(signal.SIGALRM, signal.SIG_IGN)' in line)
        fired = []
        def trace(frame, event, arg):
            if frame.f_code is target.__code__ and event == 'line' and frame.f_lineno == lineno and not fired:
                fired.append(lineno)
                os.kill(os.getpid(), signal.SIGALRM)
            return trace
        sys.settrace(trace)
        try:
            with stops.stop(100):
                pass
        except stops.TestStop:
            pass
        finally:
            sys.settrace(None)
        assert fired
        assert snapshot() == before, 'cleanup leaked state'
        for _ in range(3):
            with corpus.budget(rules):
                assert rules._XROOT_BUDGET.get()[0] == float('inf')
    assert snapshot() == before, 'scope did not restore state'
    print(json.dumps({'mode': mode, 'restored': True}))


class TestAlarmLifecycle(unittest.TestCase):
    def check_probe(self, mode):
        child = subprocess.run([sys.executable, '-c',
            'from relay_engine.tests.test_b9_fold4 import probe; probe('+repr(mode)+')'],
            capture_output=True, text=True, timeout=600)
        self.assertEqual(0, child.returncode, child.stdout+child.stderr)
        self.assertTrue(json.loads(child.stdout)['restored'])

    def test_unrelated_context_manager_entry_and_exit_still_stop(self):
        self.check_probe('unrelated')

    def test_late_callback_is_inert(self):
        self.check_probe('late')

    def test_alarm_arriving_after_pending_check_is_inert(self):
        self.check_probe('drain-race')

    def test_pending_alarm_is_consumed_before_restoring_disposition(self):
        self.check_probe('pending')

    def test_alarm_at_each_harness_line_leaves_no_shared_state(self):
        self.check_probe('window')

    def test_cleanup_alarm_does_not_poison_later_lifts(self):
        self.check_probe('cascade')


class TestRealClockStops(unittest.TestCase):
    def test_main_wait_observes_automatic_stop(self):
        # The wait has its own bound: a broken sender fails instead of hanging.
        with self.assertRaises(stops.TestStop):
            with stops.stop(0.2):
                threading.Event().wait(5)

    def test_worker_lifted_sleep_observes_automatic_stop(self):
        def blocked():
            with corpus.budget(rules), stops.stop(0.2):
                time.sleep(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(stops.TestStop):
                pool.submit(blocked).result(timeout=10)
