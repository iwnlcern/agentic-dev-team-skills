"""Store paths and guarded per-project opening (DD-v3-store slice 1)."""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Literal
from collections.abc import Iterable, Mapping
from pathlib import Path
import os
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


def catalogue_path(root):
    return Path(root) / 'machine.sqlite3'


def resolve_root(root):
    """The one spelling of a store root, taken once by its starter (delta 1 D4)."""
    return Path(os.path.realpath(root))


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
    path = catalogue_path(root)
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


def _open_readonly(path, check):
    """Read a file the writer holds; refuse rather than create or write (delta 1 D1)."""
    if not path.exists():
        raise LayoutFault('missing')
    for suffix in ('-wal', '-shm'):
        if not Path(str(path) + suffix).exists():
            raise LayoutFault('no-writer')
    conn = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, isolation_level=None)
    try:
        try:
            _pragma(conn, 'PRAGMA query_only=ON')
            schema.require_sqlite()
            if _row(conn, 'PRAGMA journal_mode') != ('wal',):
                raise LayoutFault('not-wal')
            check(conn)
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorname', None) == 'SQLITE_READONLY_DIRECTORY':
                raise LayoutFault('no-writer') from None
            raise
    except BaseException:
        conn.close()
        raise
    return conn


def open_ledger_readonly(root, project_id: str):
    """A read-only connection to a ledger the writer holds."""
    def check(conn):
        if _row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'") is None:
            raise LayoutFault('no-project-row')
        rows = conn.execute('SELECT id FROM projects NOT INDEXED LIMIT 2')
        try:
            projects = rows.fetchall()
        finally:
            rows.close()
        if projects and projects != [(project_id,)]:
            raise LayoutFault('wrong-project')
        if not projects:
            raise LayoutFault('no-project-row')
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")
        try:
            names = {name for (name,) in cursor}
        finally:
            cursor.close()
        schema._require_matching(conn, names)

    return _open_readonly(ledger_path(root, project_id), check)


def open_catalogue_readonly(root):
    """A read-only connection to the catalogue the writer holds."""
    from relay_engine import catalogue

    def check(conn):
        catalogue._require_matching(conn)
        if _row(conn, 'SELECT kind FROM catalogue_identity WHERE id=1') is None:
            raise LayoutFault('no-identity')

    return _open_readonly(catalogue_path(root), check)


def read_store_id(root) -> str:
    """Delta 5: read the store id; a read racing an engine start is not guaranteed."""
    from relay_engine import catalogue
    path = catalogue_path(root)
    if not path.exists():
        raise LayoutFault('missing')
    if Path(str(path) + '-wal').exists():
        conn = open_catalogue_readonly(root)
        try:
            return _row(conn, 'SELECT store_id FROM catalogue_identity WHERE id=1')[0]
        finally:
            conn.close()
    conn = sqlite3.connect(path.absolute().as_uri() + '?mode=ro&immutable=1',
                           uri=True, isolation_level=None)
    try:
        _pragma(conn, 'PRAGMA query_only=ON')
        schema.require_sqlite()
        catalogue._require_matching(conn)
        if _row(conn, 'SELECT kind FROM catalogue_identity WHERE id=1') is None:
            raise LayoutFault('no-identity')
        return _row(conn, 'SELECT store_id FROM catalogue_identity WHERE id=1')[0]
    finally:
        conn.close()


def route_state(catalogue_conn, project_id: str):
    """'active', 'creating' or 'absent', never RouteUnavailable (delta 1 D2)."""
    row = _row(catalogue_conn, 'SELECT state FROM project_routes WHERE project_id=?',
               (_canonical_id(project_id),))
    return 'absent' if row is None else row[0]


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


def project_routes(catalogue_conn) -> list[ProjectRoute]:
    """List every route in name byte order, including unfinished creation."""
    rows = _catalogue_read(
        catalogue_conn,
        'SELECT project_id, name, state FROM project_routes ORDER BY name', ())
    if not rows:
        return []
    cursor = catalogue_conn.execute('PRAGMA database_list')
    try:
        filename = next(path for _, database, path in cursor if database == 'main')
    finally:
        cursor.close()
    if not filename:
        raise ValueError('route requires a file-backed catalogue')
    root = Path(filename).parent
    return [ProjectRoute(*row, ledger_path(root, row[0])) for row in rows]


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


def rollback_declared(conns: Mapping[str, sqlite3.Connection]) -> Mapping[str, BaseException]:
    """Try every transaction in declared order and return errors by label."""
    failures = {}
    order = sorted((name for name in conns if isinstance(name, str)),
                   key=lambda name: (name != 'catalogue', name))
    order.extend(name for name in conns if not isinstance(name, str))
    for name in order:
        try:
            if conns[name].in_transaction:
                _pragma(conns[name], 'ROLLBACK')
        except BaseException as error:
            failures[name] = error
    return failures


def begin_declared(conns: Mapping[str, sqlite3.Connection], *, root,
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
        identities = []
        for name in order:
            cursor = conns[name].execute('PRAGMA database_list')
            try:
                databases = cursor.fetchall()
            finally:
                cursor.close()
            main = next((row[2] for row in databases if row[1] == 'main'), '')
            if not main:
                raise ValueError('declared connection holds another file')
            expected = catalogue_path(root) if name == 'catalogue' else ledger_path(root, name)
            try:
                actual_stat = os.stat(main)
                expected_stat = os.stat(expected)
            except FileNotFoundError:
                raise ValueError('declared connection holds another file') from None
            if (actual_stat.st_dev, actual_stat.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
                raise ValueError('declared connection holds another file')
            identities.append((actual_stat.st_dev, actual_stat.st_ino))
        if len(set(identities)) != len(identities):
            raise ValueError('declared connections hold one file')
        for name in order:
            if name != except_written:
                _pragma(conns[name], 'BEGIN IMMEDIATE')
    except BaseException as original:
        for name, error in rollback_declared(conns).items():
            original.add_note(f'{name}: {type(error).__name__}: {error}')
        raise


@dataclass(frozen=True)
class EngineStop:
    row_id: int
    scope: str
    project_id: str | None
    stopped_at: str
    stopped_by: str
    ended_at: str | None
    ended_by: str | None


@dataclass(frozen=True)
class EngineStopEvent:
    event_id: int
    row_id: int
    kind: str
    at: str
    by: str
    scope: str
    project_id: str | None


def record_stop(conn, scope: Literal['store', 'project'], project_id: str | None,
                by: str) -> int:
    """Record a stop and its event in the caller's transaction."""
    at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    cursor = conn.execute('INSERT INTO engine_stops '
                          '(scope, project_id, stopped_at, stopped_by) VALUES (?, ?, ?, ?)',
                          (scope, project_id, at, by))
    try:
        row_id = cursor.lastrowid
    finally:
        cursor.close()
    cursor = conn.execute("INSERT INTO engine_stop_events (row_id, kind, at, by) "
                          "VALUES (?, 'stop', ?, ?)", (row_id, at, by))
    try:
        return cursor.lastrowid
    finally:
        cursor.close()


def record_start(conn, scope, project_id, by: str) -> int | None:
    """End the open stop, if any, without controlling the transaction."""
    row = _row(conn, 'SELECT row_id FROM engine_stops WHERE scope=? '
               'AND project_id IS ? AND ended_at IS NULL', (scope, project_id))
    if row is None:
        return None
    at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    conn.execute('UPDATE engine_stops SET ended_at=?, ended_by=? WHERE row_id=?',
                 (at, by, row[0])).close()
    cursor = conn.execute("INSERT INTO engine_stop_events (row_id, kind, at, by) "
                          "VALUES (?, 'start', ?, ?)", (row[0], at, by))
    try:
        return cursor.lastrowid
    finally:
        cursor.close()


def open_stops(conn) -> list[EngineStop]:
    """Read open stops in row order."""
    cursor = conn.execute('SELECT row_id, scope, project_id, stopped_at, stopped_by, '
                          'ended_at, ended_by FROM engine_stops '
                          'WHERE ended_at IS NULL ORDER BY row_id')
    try:
        return [EngineStop(*row) for row in cursor]
    finally:
        cursor.close()


def stop_events_after(conn, event_id: int) -> list[EngineStopEvent]:
    """Read ordered events and their immutable scope in one statement."""
    cursor = conn.execute('SELECT e.event_id, e.row_id, e.kind, e.at, e.by, '
                          's.scope, s.project_id FROM engine_stop_events AS e '
                          'JOIN engine_stops AS s ON s.row_id=e.row_id '
                          'WHERE e.event_id > ? ORDER BY e.event_id', (event_id,))
    try:
        return [EngineStopEvent(*row) for row in cursor]
    finally:
        cursor.close()


@dataclass(frozen=True)
class CheckoutRoute:
    checkout_id: str
    project_id: str
    repository_id: str
    git_common_dir: str
    worktree_path: str
    dev: int
    ino: int
    created_at: int
    ended_at: int | None
    end_reason: str | None
    ledger_synced_at: int | None


_CHECKOUT_ROUTE_COLUMNS = ', '.join(field.name for field in fields(CheckoutRoute))


def _catalogue_read(catalogue_conn, sql: str, parameters: tuple) -> list[tuple]:
    """Fetch one query, controlling only a transaction this read starts."""
    owns_transaction = not catalogue_conn.in_transaction
    if owns_transaction:
        _pragma(catalogue_conn, 'BEGIN')
    try:
        cursor = catalogue_conn.execute(sql, parameters)
        try:
            result = cursor.fetchall()
        finally:
            cursor.close()
        if owns_transaction:
            _pragma(catalogue_conn, 'COMMIT')
        return result
    finally:
        if owns_transaction and catalogue_conn.in_transaction:
            _pragma(catalogue_conn, 'ROLLBACK')


def checkout_routes(catalogue_conn, *, dev: int, ino: int) -> list[CheckoutRoute]:
    """Read inode history, controlling only a transaction this read starts."""
    rows = _catalogue_read(
        catalogue_conn,
        'SELECT ' + _CHECKOUT_ROUTE_COLUMNS + ' FROM checkout_routes WHERE dev=? AND ino=? ORDER BY created_at', (dev, ino))
    return [CheckoutRoute(*row) for row in rows]


def checkout_route(catalogue_conn, checkout_id: str) -> CheckoutRoute | None:
    """Read an open or ended route by its id, regardless of inode."""
    rows = _catalogue_read(
        catalogue_conn,
        'SELECT ' + _CHECKOUT_ROUTE_COLUMNS + ' FROM checkout_routes WHERE checkout_id = ?', (checkout_id,))
    return CheckoutRoute(*rows[0]) if rows else None


def open_checkout_routes(catalogue_conn) -> list[CheckoutRoute]:
    """Delta 6: read open routes in project, path and checkout-id order."""
    rows = _catalogue_read(
        catalogue_conn,
        'SELECT ' + _CHECKOUT_ROUTE_COLUMNS +
        ' FROM checkout_routes WHERE ended_at IS NULL ORDER BY project_id, worktree_path, checkout_id', ())
    return [CheckoutRoute(*row) for row in rows]


def owed_ended_routes(catalogue_conn, project_id: str) -> list[CheckoutRoute]:
    """Delta 7 D3: read owed ends oldest first, breaking ties by checkout id."""
    rows = _catalogue_read(
        catalogue_conn,
        'SELECT ' + _CHECKOUT_ROUTE_COLUMNS +
        ' FROM checkout_routes WHERE project_id = ? AND ended_at IS NOT NULL '
        'AND ledger_synced_at IS NULL ORDER BY created_at, checkout_id', (project_id,))
    return [CheckoutRoute(*row) for row in rows]
