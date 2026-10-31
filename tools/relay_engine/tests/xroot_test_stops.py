"""Failure-only real-clock stops for blocking cross-root test operations."""
from contextlib import contextmanager
from contextvars import ContextVar
import signal
import threading
import time


class TestStop(BaseException):
    """Escape production's Exception-to-domain-answer conversion."""


_DEADLINE = ContextVar('test_failure_stop', default=None)
_SEND_LOCK = threading.Lock()
_SCOPES = []


def wait_timeout(timeout):
    deadline = _DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = deadline-time.perf_counter()
    if remaining <= 0:
        raise TestStop('test-owned stop: blocking Git operation did not finish')
    return remaining if timeout is None else min(timeout, remaining)


def _harness_frame(frame):
    """Do not inject an exception into context-manager state transitions."""
    while frame is not None:
        module = frame.f_globals.get('__name__', '')
        name = frame.f_code.co_name
        if module == __name__ or (
            module == 'relay_engine.tests.xroot_narrow_corpus' and
            name in {'budget', 'unbounded_selectors', '__init__', 'get', 'set',
                     'reset', 'monotonic', '__getattr__'}
        ):
            return True
        if module == 'contextlib' and name in {'__enter__', '__exit__'}:
            manager = frame.f_locals.get('self')
            generator = getattr(manager, 'gen', None)
            code = getattr(generator, 'gi_code', None)
            if code is not None and code.co_name in {'stop', 'alarm_guard', 'budget', 'unbounded_selectors'}:
                globals_ = getattr(generator, 'gi_frame', None)
                owner = globals_.f_globals.get('__name__') if globals_ is not None else None
                if owner in {__name__, 'relay_engine.tests.xroot_narrow_corpus'}:
                    return True
        frame = frame.f_back
    return False


@contextmanager
def alarm_guard():
    """Keep kernel alarms pending while shared harness state is changing."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


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
    if threading.current_thread() is not threading.main_thread():
        token = _DEADLINE.set(deadline)
        try:
            yield
        finally:
            _DEADLINE.reset(token)
        return
    scope = {'active': False, 'fired': False, 'deadline': deadline}
    cancel = threading.Event()
    main_ident = threading.main_thread().ident
    def send():
        if cancel.wait(max(0, deadline-time.perf_counter())):
            return
        while not cancel.is_set() and not scope['fired']:
            with _SEND_LOCK:
                if scope['active'] and not scope['fired'] and not cancel.is_set():
                    signal.pthread_kill(main_ident, signal.SIGALRM)
            # A delivery during a harness transition is deferred to the body.
            if cancel.wait(0.001):
                return
    def expired(signum, frame):
        if not scope['active'] or scope['fired'] or _harness_frame(frame):
            return
        now = time.perf_counter()
        scope['fired'] = True
        for parent in _SCOPES:
            if parent['deadline'] <= now:
                parent['fired'] = True
        raise TestStop('test-owned stop: blocking Git operation did not finish')
    sender = threading.Thread(target=send, name='test-stop-alarm', daemon=True)
    with alarm_guard():
        before = signal.getsignal(signal.SIGALRM)
        token = _DEADLINE.set(deadline)
        signal.signal(signal.SIGALRM, expired)
        _SCOPES.append(scope)
        scope['active'] = True
        sender.start()
    try:
        yield
    finally:
        with alarm_guard():
            scope['active'] = False
            cancel.set()
            sender.join()
            # Unlike setitimer cancellation, joining proves the sender cannot
            # enqueue another alarm after disposition restoration.
            with _SEND_LOCK:
                signal.signal(signal.SIGALRM, signal.SIG_IGN)
                while signal.SIGALRM in signal.sigpending():
                    signal.sigwait({signal.SIGALRM})
            _SCOPES.remove(scope)
            _DEADLINE.reset(token)
        signal.signal(signal.SIGALRM, before)
