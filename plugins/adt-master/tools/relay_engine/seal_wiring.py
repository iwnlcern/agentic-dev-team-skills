"""Approval seal startup exclusion and custody of its runner."""

from os import fsencode
import subprocess
from threading import Event, Lock
from time import monotonic
from typing import Literal

from .seal_files import ExclusionKilled, ExclusionStopped, SealFolder
from .seal_types import RunResult


TMUTIL_BOUND_S: int = 10
_isolated_calls: list[tuple[str, ...]] = []


def _spawn_tmutil(argv):
    return subprocess.Popen(
        ['/usr/bin/tmutil', *argv[1:]],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, start_new_session=True,
    )


def _isolated_run(argv):
    _isolated_calls.append(tuple(argv))
    _, operation, root = argv
    if operation == 'addexclusion':
        return RunResult(0, b'')
    if operation == 'isexcluded':
        return RunResult(0, b'[Excluded] ' + fsencode(root) + b'\n')
    raise ValueError('unsupported exclusion operation')


class TmutilCustody:
    """Serialize permission, launch and stop around one owned child."""

    def __init__(self, *, stopping):
        self._lock = Lock()
        self._stopping = stopping
        self._closed = False
        self._child = None
        self._reaped = None
        self._signalled = False
        self._killed_by_stop = False

    @staticmethod
    def _check_argv(argv):
        if not argv or argv[0] != 'tmutil':
            raise ValueError('tmutil runner requires tmutil argv')

    def _check_permission(self):
        if self._closed or self._stopping():
            raise ExclusionStopped()

    def _signal(self, child):
        # With the lock held, only the first caller sends SIGKILL.
        if not self._signalled:
            child.kill()
            self._signalled = True

    def _release_reaped(self):
        # The record cannot disappear before communicate or wait reaps it.
        self._child = None
        self._reaped.set()

    @staticmethod
    def _wait_after_kill(child):
        child.wait()
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    def run(self, argv) -> RunResult:
        self._check_argv(argv)
        with self._lock:
            self._check_permission()
            if self._child is not None:
                raise RuntimeError('tmutil already running')
            reaped = Event()
            child = _spawn_tmutil(argv)
            self._child, self._reaped = child, reaped
            self._signalled = self._killed_by_stop = False
            if self._closed or self._stopping():
                self._signal(child)
                self._wait_after_kill(child)
                self._release_reaped()
                raise ExclusionKilled()

        try:
            stdout, _ = child.communicate(timeout=TMUTIL_BOUND_S)
        except BaseException:
            # This includes the runner's TimeoutExpired. Reap before letting
            # the original exception cross the runner, unless stop killed it.
            with self._lock:
                self._signal(child)
            self._wait_after_kill(child)
            with self._lock:
                self._release_reaped()
                if self._killed_by_stop:
                    raise ExclusionKilled() from None
            raise

        with self._lock:
            self._release_reaped()
            if self._killed_by_stop:
                raise ExclusionKilled()
        return RunResult(child.returncode, stdout)

    def run_isolated(self, argv) -> RunResult:
        self._check_argv(argv)
        with self._lock:
            self._check_permission()
            return _isolated_run(argv)

    def close(self, deadline) -> bool:
        if deadline is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=max(0, deadline - monotonic())):
            return False
        try:
            self._closed = True
            if self._child is None:
                return deadline is None or monotonic() <= deadline
            if not self._signalled:
                self._signal(self._child)
                self._killed_by_stop = True
            reaped = self._reaped
        finally:
            self._lock.release()
        if deadline is None:
            return reaped.wait()
        return reaped.wait(max(0, deadline - monotonic())) and monotonic() <= deadline


def apply_exclusion(seal_root, run) -> Literal['excluded', 'add_failed', 'check_failed', 'not_excluded']:
    try:
        return SealFolder(seal_root).apply_backup_exclusion(run)
    except (ExclusionStopped, ExclusionKilled):
        raise
    except Exception:
        return 'check_failed'
