"""Store paths and guarded per-project opening (DD-v3-store slice 1)."""
from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from pathlib import Path
import sqlite3
import sys
import time
from uuid import UUID

from relay_engine import schema, submit


class LayoutFault(Exception):
    """An unavailable store entry, with a stable cause for its caller."""

    def __init__(self, cause):
        self.cause = cause
        super().__init__(cause)


class RouteUnavailable(Exception):
    """A route is absent or its durable creation has not completed."""

    def __init__(self, cause):
        self.cause = cause
        super().__init__(cause)


@dataclass(frozen=True)
class ProjectRoute:
    project_id: str
    name: str
    state: str
    ledger_path: Path


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
        conn = submit.open_store(path)
    else:
        if not path.exists():
            raise LayoutFault('missing')
        try:
            conn = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True)
        except sqlite3.OperationalError:
            if not path.exists():
                raise LayoutFault('missing') from None
            raise
    try:
        if _row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'") is None:
            raise LayoutFault('no-project-row')
        rows = conn.execute('SELECT id FROM projects NOT INDEXED LIMIT 2')
        try:
            projects = rows.fetchall()
        finally:
            rows.close()
        if projects and projects != [(project_id,)]:
            raise LayoutFault('wrong-project')
        if not projects and not create:
            raise LayoutFault('no-project-row')
        if not create:
            _pragma(conn, 'PRAGMA foreign_keys=ON')
            schema.init_v3(conn)
        _configure(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_catalogue(root, *, kind: str | None = None):
    """Open a bound catalogue; only an explicit declaration creates production."""
    from relay_engine import catalogue

    if kind not in (None, 'production', 'isolated'):
        raise ValueError('unknown catalogue kind')
    root = Path(root)
    path = root / 'machine.sqlite3'
    projects = root / 'projects'
    has_projects = projects.exists() and any(projects.iterdir())
    if has_projects and (not path.exists() or path.stat().st_size == 0):
        raise LayoutFault('missing-catalogue')
    root.mkdir(mode=0o700, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if has_projects and _row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1") is None:
            raise LayoutFault('missing-catalogue')
        if catalogue._definitions(conn):
            catalogue._require_matching(conn)
            identity = _row(conn, 'SELECT kind FROM catalogue_identity WHERE id=1')
            if identity is None:
                raise LayoutFault('no-identity')
            if kind is not None and identity != (kind,):
                raise LayoutFault('wrong-kind')
        _configure(conn)
        _pragma(conn, 'PRAGMA foreign_keys=ON')
        _pragma(conn, 'BEGIN IMMEDIATE')
        creating = not catalogue._definitions(conn)
        catalogue.init_catalogue(conn)
        identity = _row(conn, 'SELECT kind FROM catalogue_identity WHERE id=1')
        if identity is None:
            if not creating:
                raise LayoutFault('no-identity')
            now_ms = time.time_ns() // 1_000_000
            conn.execute('INSERT INTO catalogue_identity VALUES (1, ?, ?, ?)',
                         (submit.new_uuid7(now_ms), kind or 'isolated', now_ms)).close()
        elif kind is not None and identity != (kind,):
            raise LayoutFault('wrong-kind')
        _pragma(conn, 'COMMIT')
    except BaseException:
        if conn.in_transaction:
            _pragma(conn, 'ROLLBACK')
        conn.close()
        raise
    return conn


def route(catalogue_conn, *, project_id: str | None = None, name: str | None = None):
    """Resolve one active route using the catalogue's authoritative name."""
    if (project_id is None) == (name is None):
        raise ValueError('supply exactly one route selector')
    if project_id is not None:
        row = _row(catalogue_conn, 'SELECT project_id,name,state FROM project_routes '
                   'WHERE project_id=?', (_canonical_id(project_id),))
    else:
        row = _row(catalogue_conn, 'SELECT project_id,name,state FROM project_routes '
                   'WHERE name=?', (name,))
    if row is None:
        raise RouteUnavailable('not-found')
    if row[2] == 'creating':
        raise RouteUnavailable('creating')
    cursor = catalogue_conn.execute('PRAGMA database_list')
    try:
        filename = next(path for _, database, path in cursor if database == 'main')
    finally:
        cursor.close()
    if not filename:
        raise ValueError('route requires a file-backed catalogue')
    return ProjectRoute(*row, ledger_path(Path(filename).parent, row[0]))


def route_key(catalogue_conn, key_id: str):
    """Locate a key's ledger; this does not authenticate its occupant."""
    row = _row(catalogue_conn, 'SELECT project_id FROM key_routes WHERE key_id=?',
               (_canonical_id(key_id),))
    return row[0] if row is not None else None


def reserve_project(catalogue_conn, project_id: str, name: str, now_ms: int) -> None:
    """Creation step 1, in the caller's catalogue transaction."""
    catalogue_conn.execute('INSERT INTO project_routes VALUES (?, ?, ?, ?)',
                           (_canonical_id(project_id), name, 'creating', now_ms)).close()


def activate_project(catalogue_conn, project_id: str) -> None:
    """Creation step 3, after the ledger step has committed."""
    project_id = _canonical_id(project_id)
    row = _row(catalogue_conn, 'SELECT state FROM project_routes WHERE project_id=?', (project_id,))
    if row is None:
        raise RouteUnavailable('not-found')
    if row != ('active',):
        catalogue_conn.execute("UPDATE project_routes SET state='active' WHERE project_id=?",
                               (project_id,)).close()


def add_key_route(catalogue_conn, key_id: str, project_id: str, now_ms: int) -> None:
    """Insert an immutable locator before the caller provisions the occupant."""
    catalogue_conn.execute('INSERT INTO key_routes VALUES (?, ?, ?)',
                           (_canonical_id(key_id), _canonical_id(project_id), now_ms)).close()


def declared_order(catalogue: bool, project_ids: Iterable[str]) -> tuple[str, ...]:
    """One acquisition order for every job, including overlapping declared sets."""
    projects = tuple(_canonical_id(project_id) for project_id in project_ids)
    if len(set(projects)) != len(projects):
        raise ValueError('duplicate declared project')
    return (('catalogue',) if catalogue else ()) + tuple(sorted(projects))


def rollback_declared(conns: Mapping[str, sqlite3.Connection]) -> None:
    """Release every transaction, including one whose BEGIN failed."""
    for conn in conns.values():
        if conn.in_transaction:
            _pragma(conn, 'ROLLBACK')


def begin_declared(conns: Mapping[str, sqlite3.Connection], *,
                   except_written: str | None = None) -> None:
    """Acquire F-2 locks; the caller commits only its one written file.

    Every connection must have all cursors closed and no transaction open
    before this call (DD section 5 F-2). Live cursors are not detected: after
    another connection commits to their file, they can cause BEGIN IMMEDIATE
    to fail at once with SQLITE_BUSY_SNAPSHOT, without the busy timeout.
    Skip except_written only when an Engine helper will own its BEGIN.
    On failure roll back every connection in the declared set.
    """
    try:
        order = declared_order('catalogue' in conns,
                               (name for name in conns if name != 'catalogue'))
        if except_written is not None and except_written not in conns:
            raise ValueError('written file is outside the declared set')
        if except_written is not None and except_written != order[-1]:
            raise ValueError('written file must be last in the declared order')
        if any(conn.in_transaction for conn in conns.values()):
            raise ValueError('declared connection already has a transaction')
        for name in order:
            if name != except_written:
                _pragma(conns[name], 'BEGIN IMMEDIATE')
    except BaseException:
        rollback_declared(conns)
        raise
