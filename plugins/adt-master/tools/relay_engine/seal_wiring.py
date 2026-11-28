"""Approval seal startup exclusion and its two fixed runners."""

from os import fsencode
import subprocess
from types import SimpleNamespace
from typing import Literal

from .seal_files import SealFolder


TMUTIL_BOUND_S: int = 10
_isolated_calls: list[tuple[str, ...]] = []


def _run_tmutil(argv):
    if not argv or argv[0] != 'tmutil':
        raise ValueError('tmutil runner requires tmutil argv')
    return subprocess.run(
        ['/usr/bin/tmutil', *argv[1:]],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, timeout=TMUTIL_BOUND_S,
    )


def _isolated_run(argv):
    _isolated_calls.append(tuple(argv))
    executable, operation, root = argv
    if executable != 'tmutil':
        raise ValueError('isolated runner requires tmutil argv')
    if operation == 'addexclusion':
        return SimpleNamespace(returncode=0, stdout=b'')
    if operation == 'isexcluded':
        return SimpleNamespace(returncode=0, stdout=b'[Excluded] ' + fsencode(root) + b'\n')
    raise ValueError('unsupported exclusion operation')


def apply_exclusion(seal_root, run) -> Literal['excluded', 'add_failed', 'check_failed', 'not_excluded']:
    try:
        return SealFolder(seal_root).apply_backup_exclusion(run)
    except Exception:
        return 'check_failed'
