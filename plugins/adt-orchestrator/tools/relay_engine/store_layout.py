"""Store paths and guarded per-project opening (DD-v3-store slice 1)."""
from pathlib import Path
import sqlite3
import sys
from uuid import UUID

from relay_engine import submit


class LayoutFault(Exception):
    """An unavailable store entry, with a stable cause for its caller."""

    def __init__(self, cause):
        self.cause = cause
        super().__init__(cause)


def _canonical_id(value):
    if not isinstance(value, str):
        raise ValueError('id must be canonical lowercase UUID text')
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError('id must be canonical lowercase UUID text') from None
    if str(parsed) != value:
        raise ValueError('id must be canonical lowercase UUID text')
    return value


def production_root():
    """Use the engine user's password-database home, never ambient HOME."""
    return Path(submit._home()) / '.adt'


def ledger_path(root, project_id: str):
    return Path(root) / 'projects' / _canonical_id(project_id) / 'ledger.sqlite3'


def evidence_dir(root, project_id: str):
    return Path(root) / 'projects' / _canonical_id(project_id) / 'evidence'


def key_path(root, project_id: str, seat_id: str, key_id: str):
    return (Path(root) / 'projects' / _canonical_id(project_id) / 'keys'
            / _canonical_id(seat_id) / (_canonical_id(key_id) + '.key'))


def jev_key_path(root):
    return Path(root) / 'jev' / 'key'


def jev_log_dir(root, project_id: str):
    return Path(root) / 'projects' / _canonical_id(project_id) / 'jev'


def _row(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters) if parameters else conn.execute(sql)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _pragma(conn, sql):
    cursor = conn.execute(sql)
    cursor.close()


def _configure(conn):
    if _row(conn, 'PRAGMA journal_mode=WAL') != ('wal',):
        raise LayoutFault('not-wal')
    _pragma(conn, 'PRAGMA synchronous=FULL')
    if sys.platform == 'darwin':
        _pragma(conn, 'PRAGMA fullfsync=ON')
        _pragma(conn, 'PRAGMA checkpoint_fullfsync=ON')


def open_ledger(root, project_id: str, *, create: bool = False):
    """Open exactly the named project's current store; creation is explicit."""
    path = ledger_path(root, project_id)
    if create:
        for directory in [Path(root), Path(root) / 'projects', path.parent]:
            directory.mkdir(mode=0o700, exist_ok=True)
    else:
        try:
            probe = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True)
        except sqlite3.OperationalError:
            if not path.exists():
                raise LayoutFault('missing') from None
            raise
        probe.close()
    conn = submit.open_store(path)
    try:
        _configure(conn)
        rows = conn.execute('SELECT id FROM projects LIMIT 2')
        try:
            projects = rows.fetchall()
        finally:
            rows.close()
        if projects and projects != [(project_id,)]:
            raise LayoutFault('wrong-project')
        if not projects and not create:
            raise LayoutFault('no-project-row')
    except BaseException:
        conn.close()
        raise
    return conn
