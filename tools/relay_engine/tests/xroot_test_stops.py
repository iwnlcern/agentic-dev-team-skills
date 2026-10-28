"""Failure-only real-clock stops for blocking cross-root test operations."""
from contextlib import contextmanager
from contextvars import ContextVar
import signal
import threading
import time


class TestStop(BaseException):
    """Escape production's Exception-to-domain-answer conversion."""


_DEADLINE = ContextVar('test_failure_stop', default=None)


def wait_timeout(timeout):
    deadline = _DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = deadline-time.perf_counter()
    if remaining <= 0:
        raise TestStop('test-owned stop: blocking Git operation did not finish')
    return remaining if timeout is None else min(timeout, remaining)


@contextmanager
def stop(seconds=120):
    """Bound a test without supplying a domain answer or a successful verdict.

    Main-thread non-selector waits are interrupted by SIGALRM.
    Lifted selectors and sleeps consult the ContextVar in every worker too.
    The real clock is independent of the monotonic speed probe.
    """
    started = time.perf_counter()
    outer = _DEADLINE.get()
    deadline = min(started+seconds, outer) if outer is not None else started+seconds
    token = _DEADLINE.set(deadline)
    if threading.current_thread() is not threading.main_thread():
        try:
            yield
        finally:
            _DEADLINE.reset(token)
        return
    before = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    def expired(signum, frame):
        raise TestStop('test-owned stop: blocking Git operation did not finish')
    signal.signal(signal.SIGALRM, expired)
    delay = max(0.000001, deadline-started)
    signal.setitimer(signal.ITIMER_REAL, min(delay, timer[0]) if timer[0] else delay)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)
        _DEADLINE.reset(token)
        remaining = timer[0]-(time.perf_counter()-started)
        # A consumed outer stop must not fire again during child cleanup.
        if timer[0] and remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, remaining, timer[1])
