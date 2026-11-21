"""Store paths and guarded per-project opening (DD-v3-store slice 1)."""
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Literal
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
import errno
import fcntl
from hashlib import sha256
import json
import os
import re
import sqlite3
import stat
import sys
import time
from uuid import UUID, uuid4

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


class RouteConflict(sqlite3.IntegrityError):
    """A permanent route identity or name is already reserved."""


class NameTaken(RouteConflict):
    def __init__(self, name, holder_project_id):
        self.name = name
        self.holder_project_id = holder_project_id
        super().__init__(f'name {name} is reserved by project {holder_project_id}')


class ProjectExists(RouteConflict):
    def __init__(self, project_id):
        self.project_id = project_id
        super().__init__(f'project {project_id} already has a route')


class HoldConflict(Exception):
    def __init__(self, holder_job_id, scope):
        self.holder_job_id = holder_job_id
        self.scope = scope
        super().__init__(f'{scope} is held by job {holder_job_id}')


class ReservationChanged(Exception):
    """The route is no longer in the state its job expected."""


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


def boot_path(root, project_id: str, seat_id: str, key_id: str) -> Path:
    """Delta 8's boot file beside the occupant's key, with canonical ids."""
    project_id = _canonical_id(project_id)
    seat_id = _canonical_id(seat_id)
    key_id = _canonical_id(key_id)
    return Path(root) / 'projects' / project_id / 'keys' / seat_id / (key_id + '.boot')


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


def open_ledger(root, project_id: str, *, create: bool = False, _check_admission=None):
    """Open exactly the named project's current store; creation is explicit."""
    path = ledger_path(root, project_id)
    if create:
        for directory in [Path(root), Path(root) / 'projects', path.parent]:
            directory.mkdir(mode=0o700, exist_ok=True)
        conn = submit.open_store(path)
    else:
        if _check_admission is not None:
            _check_admission()
        if not path.exists():
            raise LayoutFault('missing')
        try:
            conn = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True)
        except sqlite3.OperationalError:
            if not path.exists():
                raise LayoutFault('missing') from None
            raise
    try:
        if _check_admission is not None:
            _check_admission()
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
    return _open_catalogue(root, kind)


def _require_admission_identity(conn, store, project):
    if (_row(conn, 'SELECT store_id FROM catalogue_identity WHERE id=1') != (store,)
            or route_state(conn, project) != 'active'):
        raise LayoutFault('member-unproved')


def _open_catalogue(root, kind, *, reserved=None, admission=None, check_admission=None):
    from relay_engine import catalogue

    if kind not in (None, 'production', 'isolated'):
        raise ValueError('unknown catalogue kind')
    root = Path(root)
    path = catalogue_path(root)
    projects = root / 'projects'
    has_projects = projects.exists() and any(projects.iterdir())
    if reserved is None and has_projects and (not path.exists() or path.stat().st_size == 0):
        raise LayoutFault('missing-catalogue')
    if admission is None:
        root.mkdir(mode=0o700, exist_ok=True)
    else:
        check_admission()
    conn = (sqlite3.connect(path) if reserved is None and admission is None else
            sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True))
    try:
        if admission is not None:
            check_admission()
            _require_admission_identity(conn, *admission)
        if (reserved is None and has_projects
                and _row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1") is None):
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
        if reserved is not None and not creating:
            raise LayoutFault('birth-members-present')
        if admission is not None and creating:
            raise LayoutFault('missing-catalogue')
        catalogue.init_catalogue(conn)
        identity = _row(conn, 'SELECT kind FROM catalogue_identity WHERE id=1')
        if identity is None:
            if not creating:
                raise LayoutFault('no-identity')
            if reserved is None:
                now_ms = time.time_ns() // 1_000_000
                store_id = submit.new_uuid7(now_ms)
            else:
                store_id, now_ms = reserved
            conn.execute('INSERT INTO catalogue_identity VALUES (1, ?, ?, ?)',
                         (store_id, kind or 'isolated', now_ms)).close()
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


def _canonical_job(value):
    value = _canonical_id(value)
    if UUID(value).version != 7:
        raise ValueError('job id must be a canonical UUIDv7')
    return value


def project_scope(project_id: str) -> str:
    return 'project:' + _canonical_id(project_id)


def root_scope(root_uuid: str) -> str:
    return 'root:' + _canonical_id(root_uuid)


def take_holds(catalogue_conn, job_id: str, kind: str,
               scopes: Sequence[str], now_ms: int) -> None:
    """Check the whole span before inserting, inside the caller's transaction."""
    job_id = _canonical_job(job_id)
    if kind not in ('cutover', 'undo', 'unmark'):
        raise ValueError('unknown hold kind')
    scopes = tuple(dict.fromkeys(scopes))
    for scope in scopes:
        prefix, separator, value = scope.partition(':')
        if not separator or prefix not in ('project', 'root'):
            raise ValueError('unknown hold scope')
        _canonical_id(value)
    for scope in scopes:
        held = _row(catalogue_conn, 'SELECT job_id FROM maintenance_holds WHERE scope=?',
                    (scope,))
        if held is not None:
            raise HoldConflict(held[0], scope)
    for scope in scopes:
        catalogue_conn.execute('INSERT INTO maintenance_holds VALUES (?, ?, ?, ?)',
                               (scope, job_id, kind, now_ms)).close()


def release_holds(catalogue_conn, job_id: str) -> int:
    cursor = catalogue_conn.execute('DELETE FROM maintenance_holds WHERE job_id=?',
                                    (_canonical_job(job_id),))
    try:
        return cursor.rowcount
    finally:
        cursor.close()


def holds_of(catalogue_conn, job_id: str) -> frozenset[str]:
    cursor = catalogue_conn.execute('SELECT scope FROM maintenance_holds WHERE job_id=?',
                                    (_canonical_job(job_id),))
    try:
        return frozenset(scope for (scope,) in cursor)
    finally:
        cursor.close()


def any_hold(catalogue_conn) -> tuple[str, str] | None:
    return _row(catalogue_conn,
                'SELECT scope, job_id FROM maintenance_holds ORDER BY scope LIMIT 1')


def _reservation_conflict(conn, project_id, name):
    if _row(conn, 'SELECT 1 FROM project_routes WHERE project_id=?', (project_id,)):
        raise ProjectExists(project_id)
    holder = _row(conn, 'SELECT project_id FROM project_routes WHERE name=?', (name,))
    if holder is not None:
        raise NameTaken(name, holder[0])


def reserve_project(catalogue_conn, project_id: str, name: str, now_ms: int,
                    *, held_by_job: str | None = None) -> None:
    """Creation step 1, in the caller's catalogue transaction."""
    project_id = _canonical_id(project_id)
    if held_by_job is not None:
        held_by_job = _canonical_job(held_by_job)
    _reservation_conflict(catalogue_conn, project_id, name)
    try:
        catalogue_conn.execute(
            'INSERT INTO project_routes (project_id, name, state, created_at, held_by_job) '
            'VALUES (?, ?, ?, ?, ?)',
            (project_id, name, 'creating', now_ms, held_by_job)).close()
    except sqlite3.IntegrityError:
        _reservation_conflict(catalogue_conn, project_id, name)
        raise


def take_over_reservation(catalogue_conn, project_id: str, expected_job: str,
                          new_job: str, ledger_present: bool) -> None:
    project_id = _canonical_id(project_id)
    expected_job, new_job = _canonical_job(expected_job), _canonical_job(new_job)
    if ledger_present:
        raise ReservationChanged('a present file prevents takeover')
    cursor = catalogue_conn.execute(
        "UPDATE project_routes SET held_by_job=? WHERE project_id=? "
        "AND state='creating' AND held_by_job=?", (new_job, project_id, expected_job))
    try:
        if cursor.rowcount != 1:
            raise ReservationChanged('reservation owner or state changed')
    finally:
        cursor.close()


def return_to_creating(catalogue_conn, project_id: str, job_id: str) -> None:
    cursor = catalogue_conn.execute(
        "UPDATE project_routes SET state='creating' WHERE project_id=? "
        "AND state='active' AND held_by_job=?",
        (_canonical_id(project_id), _canonical_job(job_id)))
    try:
        if cursor.rowcount != 1:
            raise ReservationChanged('active reservation owner or state changed')
    finally:
        cursor.close()


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


_INCUMBENT = '.adt-store-incumbent.json'
_INCUMBENT_LOCK = 'api-service.lock'
_INCUMBENT_LIMIT = 16 * 1024
_IDLE_RECEIPT_LIMIT = 64 * 1024
_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW


def _inode(info):
    return info.st_dev, info.st_ino


def _owned_inode(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or (not directory and info.st_nlink != 1)):
        raise LayoutFault('incumbent-invalid')
    return _inode(info)


def _canonical_frame(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _bounded_text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable()


@dataclass(frozen=True)
class _IncumbentOwner:
    epoch: str
    pid: int
    birth: str
    boot: str
    runtime: str
    notifier: Literal['off', 'configured', 'unknown']


@dataclass(frozen=True)
class _IncumbentDisposition:
    kind: Literal['enrollment', 'retirement', 'recovery']
    store: str
    project: str
    root: tuple[int, int]
    lock: tuple[int, int]
    prior_epoch: str | None
    digest: str


@dataclass(frozen=True)
class IncumbentRecord:
    version: int
    root: tuple[int, int]
    lock_name: str
    lock: tuple[int, int]
    store: str
    project: str
    phase: Literal['idle', 'armed']
    owner: _IncumbentOwner | None
    disposition: _IncumbentDisposition | None


class IncumbentGuard:
    """Private Store owner seam. This guard never opens an ordinary Store."""

    ACCEPTS_PRODUCTION_BIRTH = True

    def __init__(self, root, root_fd, lock_fd, root_identity, lock_identity):
        self._path = root
        self._holder_pid = os.getpid()
        self._root_fd = root_fd
        self._lock_fd = lock_fd
        self._root_identity = root_identity
        self._lock_identity = lock_identity
        self._record_identity = None
        self._verified_idle = None
        self._owned_arm = None
        self._birth = None
        self._birth_idle = None
        self._birth_arm = None
        self._publication = None

    @property
    def lock_identity(self):
        return self._lock_identity

    @classmethod
    def acquire(cls, root, *, expected_lock=None):
        root = Path(root).absolute()
        root_fd = lock_fd = None
        try:
            root_fd = os.open(root, _DIRECTORY)
            root_identity = _owned_inode(os.fstat(root_fd), directory=True)
            lock_fd = os.open(_INCUMBENT_LOCK, os.O_RDWR | _NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=root_fd)
            lock_identity = _owned_inode(os.fstat(lock_fd))
            guard = cls(root, root_fd, lock_fd, root_identity, lock_identity)
            guard._check_lock()
            if expected_lock is not None and lock_identity != expected_lock:
                raise LayoutFault('incumbent-invalid')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise LayoutFault('store-locked') from None
                raise
            guard._check_lock()
            root_fd = lock_fd = None
            return guard
        except OSError as exc:
            raise LayoutFault('incumbent-invalid') from exc
        finally:
            for fd in (lock_fd, root_fd):
                if fd is not None:
                    os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._lock_fd is not None:
            lock_fd, root_fd = self._lock_fd, self._root_fd
            self._lock_fd = self._root_fd = None
            self._verified_idle = self._owned_arm = self._birth = None
            self._birth_idle = self._birth_arm = self._publication = None
            try:
                os.close(lock_fd)
            finally:
                os.close(root_fd)

    def _check_lock(self):
        if self._lock_fd is None or self._holder_pid != os.getpid():
            raise LayoutFault('incumbent-invalid')
        try:
            root_info = os.stat(self._path, follow_symlinks=False)
            lock_info = os.stat(_INCUMBENT_LOCK, dir_fd=self._root_fd, follow_symlinks=False)
            if (_owned_inode(root_info, directory=True) != self._root_identity
                    or _owned_inode(os.fstat(self._root_fd), directory=True) != self._root_identity
                    or _owned_inode(lock_info) != self.lock_identity
                    or _owned_inode(os.fstat(self._lock_fd)) != self.lock_identity):
                raise LayoutFault('incumbent-invalid')
        except OSError as exc:
            raise LayoutFault('incumbent-invalid') from exc

    def _named_record(self):
        info = os.stat(_INCUMBENT, dir_fd=self._root_fd, follow_symlinks=False)
        return _owned_inode(info)

    def _read_frame(self, *, sync=False):
        self._check_lock()
        try:
            fd = os.open(_INCUMBENT, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=self._root_fd)
        except FileNotFoundError as exc:
            if self._record_identity is None:
                raise LayoutFault('enrollment-required') from exc
            raise LayoutFault('incumbent-invalid') from exc
        except OSError as exc:
            raise LayoutFault('incumbent-invalid') from exc
        with os.fdopen(fd, 'rb') as source:
            identity = _owned_inode(os.fstat(fd))
            if self._record_identity is not None and self._record_identity != identity:
                raise LayoutFault('incumbent-invalid')
            if sync:
                os.fsync(fd)
                os.fsync(self._root_fd)
            data = source.read(_INCUMBENT_LIMIT + 1)
            self._check_lock()
            if (not data or len(data) > _INCUMBENT_LIMIT
                    or _owned_inode(os.fstat(fd)) != identity or self._named_record() != identity):
                raise LayoutFault('incumbent-invalid')
        frame = self._decode(data)
        self._record_identity = identity
        return frame, data

    def _decode(self, data):
        frame = self._parse_record(data)
        if (frame.root, frame.lock) != (self._root_identity, self.lock_identity):
            raise LayoutFault('incumbent-invalid')
        return frame

    @staticmethod
    def _parse_record(data):
        try:
            value = json.loads(data)
            if _canonical_frame(value) != data:
                raise ValueError('noncanonical')
            value['root'] = tuple(value['root'])
            value['lock'] = tuple(value['lock'])
            if value['owner'] is not None:
                value['owner'] = _IncumbentOwner(**value['owner'])
            if value['disposition'] is not None:
                disposition = value['disposition']
                disposition['root'] = tuple(disposition['root'])
                disposition['lock'] = tuple(disposition['lock'])
                value['disposition'] = _IncumbentDisposition(**disposition)
            frame = IncumbentRecord(**value)
            IncumbentGuard._validate_frame(frame)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
            raise LayoutFault('incumbent-invalid') from exc
        return frame

    @staticmethod
    def _validate_frame(frame):
        _canonical_id(frame.store)
        _canonical_id(frame.project)
        if (type(frame.version) is not int or frame.version != 1
                or frame.lock_name != _INCUMBENT_LOCK
                or any(len(identity) != 2 or any(type(part) is not int for part in identity)
                       or identity[0] < 0 or identity[1] <= 0 for identity in (frame.root, frame.lock))):
            raise ValueError('identity')
        if frame.phase == 'idle' and frame.owner is None:
            proof = frame.disposition
            if (not isinstance(proof, _IncumbentDisposition)
                    or (proof.store, proof.project, proof.root, proof.lock)
                    != (frame.store, frame.project, frame.root, frame.lock)
                    or any(type(part) is not int for part in proof.root + proof.lock)
                    or proof.kind not in ('enrollment', 'retirement', 'recovery')
                    or (proof.kind == 'enrollment') != (proof.prior_epoch is None)
                    or not re.fullmatch(r'[0-9a-f]{64}', proof.digest)):
                raise ValueError('disposition')
            if proof.prior_epoch is not None:
                _canonical_id(proof.prior_epoch)
            return
        if frame.phase != 'armed' or frame.disposition is not None:
            raise ValueError('phase')
        owner = frame.owner
        if (not isinstance(owner, _IncumbentOwner) or type(owner.pid) is not int
                or not 0 < owner.pid < 2**31
                or owner.notifier not in ('off', 'configured', 'unknown')
                or not all(_bounded_text(text) for text in (owner.birth, owner.boot, owner.runtime))):
            raise ValueError('owner')
        _canonical_id(owner.epoch)

    def read(self):
        return self._read_frame()[0]

    def _current(self, expected):
        try:
            actual = self.read()
        except LayoutFault as exc:
            if exc.cause != 'enrollment-required' or expected is not None:
                raise
            return
        if actual != expected:
            raise LayoutFault('incumbent-changed')

    def _replace(self, expected, frame):
        data = _canonical_frame(asdict(frame))
        if len(data) > _INCUMBENT_LIMIT or self._decode(data) != frame:
            raise LayoutFault('incumbent-invalid')
        self._current(expected)
        temp = f'{_INCUMBENT}.{uuid4().hex}.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                     0o600, dir_fd=self._root_fd)
        try:
            with os.fdopen(fd, 'wb') as target:
                identity = _owned_inode(os.fstat(target.fileno()))
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
                self._current(expected)
                if _owned_inode(os.stat(temp, dir_fd=self._root_fd, follow_symlinks=False)) != identity:
                    raise LayoutFault('incumbent-invalid')
                os.replace(temp, _INCUMBENT, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
                temp = None
            self._record_identity = identity
            os.fsync(self._root_fd)
            if self._read_frame() != (frame, data):
                raise LayoutFault('incumbent-invalid')
        finally:
            if temp is not None:
                os.unlink(temp, dir_fd=self._root_fd)
        return frame

    def _disposition(self, kind, store, project, prior_epoch, digest):
        return _IncumbentDisposition(kind, store, project, self._root_identity,
                                     self.lock_identity, prior_epoch, digest)

    def _publish_idle(self, expected, proof):
        self._verified_idle = None
        idle = IncumbentRecord(1, proof.root, _INCUMBENT_LOCK, proof.lock, proof.store,
                               proof.project, 'idle', None, proof)
        self._replace(expected, idle)
        self._verified_idle = idle
        return idle

    def _verify_idle(self, expected, proof):
        self._verified_idle = None
        if expected.phase != 'idle' or proof is None or expected.disposition != proof:
            raise LayoutFault('idle-unverified')
        if self._read_frame(sync=True)[0] != expected:
            raise LayoutFault('incumbent-changed')
        self._verified_idle = expected
        return expected

    def arm(self, expected_idle, owner):
        if expected_idle.phase != 'idle':
            raise LayoutFault('retirement-pending')
        verified, self._verified_idle = self._verified_idle, None
        birth_idle, self._birth_idle = self._birth_idle, None
        if verified != expected_idle:
            raise LayoutFault('idle-unverified')
        if not isinstance(owner, _IncumbentOwner) or owner.pid != os.getpid():
            raise LayoutFault('incumbent-invalid')
        if owner.epoch == expected_idle.disposition.prior_epoch:
            raise LayoutFault('incumbent-changed')
        armed = IncumbentRecord(1, expected_idle.root, expected_idle.lock_name,
                                expected_idle.lock, expected_idle.store, expected_idle.project,
                                'armed', owner, None)
        self._owned_arm = self._replace(expected_idle, armed)
        self._birth_arm = None
        if birth_idle is not None and birth_idle[0] == expected_idle:
            self._birth_arm = (armed, birth_idle[1])
        return armed

    @classmethod
    def _create_new(cls, root, store, project, *, kind: Literal['isolated', 'production'] = 'isolated',
                    consume_production_birth: Callable[[Path, str, str, tuple[int, int]], None] | None = None
                    ) -> tuple['IncumbentGuard', '_RetainedIncumbentDisposition']:
        """Reserve a checked new Store before any provider can open SQLite."""
        _canonical_id(store)
        _canonical_id(project)
        root = Path(root)
        canonical_root = production_root()
        if kind == 'production':
            if root != canonical_root:
                raise LayoutFault('wrong-kind')
            if consume_production_birth is None:
                raise LayoutFault('birth-unproved')
        elif (kind != 'isolated' or root == canonical_root
              or resolve_root(root) == resolve_root(canonical_root)):
            raise LayoutFault('wrong-kind')
        root = root.absolute()
        with closing(_DirectoryHandle(os.open(root.parent, _DIRECTORY))) as parent:
            # An absent leaf has no inode to resolve on a case-insensitive volume.
            if (kind == 'isolated' and root.name.casefold() == canonical_root.name.casefold()
                    and os.path.samestat(os.fstat(parent.fd), canonical_root.parent.stat())):
                raise LayoutFault('wrong-kind')
            if kind == 'production':
                consume_production_birth(canonical_root, store, project, _inode(os.fstat(parent.fd)))
            os.mkdir(root.name, mode=0o700, dir_fd=parent.fd)
            os.fsync(parent.fd)
            descriptor = os.open(root.name, _DIRECTORY, dir_fd=parent.fd)
        with closing(_DirectoryHandle(descriptor)) as directory:
            root_identity = _owned_inode(os.fstat(directory.fd), directory=True)
            fd = os.open(_INCUMBENT_LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                         0o600, dir_fd=directory.fd)
            try:
                lock_identity = _owned_inode(os.fstat(fd))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory.fd)
        guard = cls.acquire(root, expected_lock=lock_identity)
        try:
            if guard._root_identity != root_identity:
                raise LayoutFault('incumbent-invalid')
            proof = guard._retain('enrollment', store, project, None,
                                  uuid4().hex.encode('ascii'))
            guard._birth = (proof, kind)
        except BaseException:
            guard.close()
            raise
        return guard, proof

    def _retain(self, kind, store, project, prior_epoch, evidence):
        if not isinstance(evidence, bytes) or not 0 < len(evidence) <= _INCUMBENT_LIMIT:
            raise LayoutFault('disposition-unproved')
        return _RetainedIncumbentDisposition(kind, store, project, self._root_identity,
                                             self.lock_identity, prior_epoch, evidence)

    def _retirement_proof(self, armed, evidence):
        """Owner receipt seam after attributable settlement and handle closure."""
        if self._owned_arm != armed:
            raise LayoutFault('disposition-unproved')
        self._current(armed)
        if not isinstance(evidence, bytes) or not 0 < len(evidence) <= _INCUMBENT_LIMIT:
            raise LayoutFault('disposition-unproved')
        evidence = _canonical_frame({
            'arm': asdict(armed),
            'settlement_sha256': sha256(evidence).hexdigest(),
            'nonce': uuid4().hex,
        })
        return self._retain('retirement', armed.store, armed.project, armed.owner.epoch, evidence)

    def verify_idle(self, expected, proof):
        """Re-establish a visible frame using a separately retained owner receipt."""
        self._verified_idle = None
        if not isinstance(proof, _RetainedIncumbentDisposition):
            raise LayoutFault('idle-unverified')
        return self._verify_idle(expected, proof.summary())

    def enroll_new(self, proof):
        self._check_lock()
        if (self._birth is None or self._birth[0] is not proof
                or set(os.listdir(self._root_fd)) != {_INCUMBENT_LOCK}):
            raise LayoutFault('enrollment-unproved')
        _, kind = self._birth
        self._birth = None
        idle = self._publish_idle(None, proof.summary())
        self._birth_idle = (idle, kind)
        return idle, proof

    def _materialize_birth(self, armed, *, project_name, now_ms):
        """Create only the members reserved by this guard's first verified birth arm."""
        from relay_engine import store_create, store_steps

        birth_arm, self._birth_arm = self._birth_arm, None
        if birth_arm is None or birth_arm[0] != armed or self._owned_arm != armed:
            raise LayoutFault('birth-unproved')
        _, kind = birth_arm
        self._current(armed)
        if set(os.listdir(self._root_fd)) != {_INCUMBENT_LOCK, _INCUMBENT}:
            raise LayoutFault('birth-members-present')

        def exclusive(parent, name, *, directory=False):
            if directory:
                os.mkdir(name, mode=0o700, dir_fd=parent)
                return os.open(name, _DIRECTORY, dir_fd=parent)
            return os.open(name, os.O_RDONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                           0o600, dir_fd=parent)

        catalogue_name = catalogue_path(self._path).name
        ledger_name = ledger_path(self._path, armed.project).name
        with ExitStack() as stack:
            try:
                catalog_fd = stack.enter_context(closing(_DirectoryHandle(
                    exclusive(self._root_fd, catalogue_name)))).fd
                projects_fd = stack.enter_context(closing(_DirectoryHandle(
                    exclusive(self._root_fd, 'projects', directory=True)))).fd
                project_fd = stack.enter_context(closing(_DirectoryHandle(
                    exclusive(projects_fd, armed.project, directory=True)))).fd
                ledger_fd = stack.enter_context(closing(_DirectoryHandle(
                    exclusive(project_fd, ledger_name)))).fd
            except FileExistsError as exc:
                raise LayoutFault('birth-members-present') from exc
            members = tuple(_inode(os.fstat(fd)) for fd in (projects_fd, project_fd, catalog_fd, ledger_fd))

            def check_members():
                self._current(armed)
                if self._member_identities(armed.project) != members:
                    raise LayoutFault('member-changed')

            class BirthTurns(store_steps.DirectTurns):
                def run(turns, declared, body, *, fence_project=None):
                    check_members()
                    return super().run(declared, body, fence_project=fence_project)

            check_members()
            with closing(_open_catalogue(self._path, kind, reserved=(armed.store, now_ms))) as conn:
                _pragma(conn, 'BEGIN IMMEDIATE')
                reserve_project(conn, armed.project, project_name, now_ms)
                conn.commit()
            store_create.finish_creation(BirthTurns(self._path), armed.project)
            for fd in (catalog_fd, ledger_fd, project_fd, projects_fd, self._root_fd):
                os.fsync(fd)
            check_members()
            with closing(_immutable_incumbent_read(catalogue_path(self._path))) as catalog:
                if _row(catalog, 'SELECT kind FROM catalogue_identity WHERE id=1') != (kind,):
                    raise LayoutFault('wrong-kind')
            return self._protected_identity(armed)

    def _offline_checkpoint(self, store, project, provenance):
        """Capture member custody under an already authorized offline owner."""
        _canonical_id(store)
        _canonical_id(project)
        retained = self._retain('enrollment', store, project, None, provenance)
        self._current(None)
        return _OfflineIncumbentEnrollment(retained, self._member_identities(project))

    def enroll_existing(self, proof):
        if not isinstance(proof, _OfflineIncumbentEnrollment):
            raise LayoutFault('enrollment-unproved')
        retained = proof.retained
        if (retained.kind != 'enrollment' or retained.prior_epoch is not None
                or (retained.root, retained.lock) != (self._root_identity, self.lock_identity)):
            raise LayoutFault('enrollment-unproved')
        self._current(None)
        self._protected_read(retained.store, retained.project, proof.members)
        return self._publish_idle(None, retained.summary()), retained

    def retire(self, expected_arm, proof):
        if (not isinstance(proof, _RetainedIncumbentDisposition)
                or self._owned_arm != expected_arm or proof.kind != 'retirement'
                or (proof.store, proof.project, proof.root, proof.lock, proof.prior_epoch)
                != (*self._arm_identity(expected_arm), expected_arm.owner.epoch)):
            raise LayoutFault('disposition-unproved')
        self._current(expected_arm)
        self._protected_identity(expected_arm)
        self._owned_arm = None
        return self._publish_idle(expected_arm, proof.summary())

    @staticmethod
    def _arm_identity(armed):
        return armed.store, armed.project, armed.root, armed.lock

    def _checkpoint(self, armed, operation):
        """Original owner checkpoint after the sole operation and its handles settle."""
        self._current(armed)
        if self._owned_arm != armed or not isinstance(operation, _IncumbentOperation):
            raise LayoutFault('operation-unproved')
        return _IncumbentCheckpoint(armed, self._member_identities(armed.project), operation)

    def _retain_recovery_publication(self, armed, evidence):
        """Bind a checked publication observation to this particular acquisition."""
        self._current(armed)
        if (armed.phase != 'armed' or not isinstance(evidence, bytes)
                or not 0 < len(evidence) <= _INCUMBENT_LIMIT):
            raise LayoutFault('publication-unresolved')
        self._publication = _IncumbentPublication(armed, evidence)
        return self._publication

    def recover(self, expected_arm, child, checkpoint, *, publication):
        self._current(expected_arm)
        if expected_arm.phase != 'armed':
            raise LayoutFault('incumbent-invalid')
        if expected_arm.owner.notifier != 'off':
            raise LayoutFault('notifier-unresolved')
        if not isinstance(child, _IncumbentChild) or child.armed != expected_arm:
            raise LayoutFault('exit-unproved')
        if child.process.exitcode is None or child.process.exitcode == 0:
            raise LayoutFault('exit-unproved')
        if (not isinstance(checkpoint, _IncumbentCheckpoint) or checkpoint.armed != expected_arm
                or not isinstance(checkpoint.operation, _IncumbentOperation)):
            raise LayoutFault('operation-unproved')
        if (not isinstance(publication, _IncumbentPublication) or self._publication is not publication
                or publication.armed != expected_arm):
            raise LayoutFault('publication-unresolved')
        operation = checkpoint.operation
        self._protected_read(expected_arm.store, expected_arm.project, checkpoint.members, operation)
        evidence = _canonical_frame({
            'arm': asdict(expected_arm),
            'exit': child.process.exitcode,
            'members': checkpoint.members,
            'key': operation.key,
            'draft_sha256': sha256(operation.draft).hexdigest(),
            'relay_id': operation.admitted.relay_id,
            'handle': operation.admitted.handle,
            'publication_sha256': sha256(publication.evidence).hexdigest(),
            'nonce': uuid4().hex,
        })
        proof = self._retain('recovery', expected_arm.store, expected_arm.project,
                             expected_arm.owner.epoch, evidence)
        return self._publish_idle(expected_arm, proof.summary()), proof

    def _protected_identity(self, armed):
        self._admit_members(armed)
        return armed.store

    def _admit_members(self, armed):
        self._current(armed)
        members = self._member_identities(armed.project)
        self._protected_read(armed.store, armed.project, members)
        self._current(armed)
        return members

    def _check_admitted_members(self, armed, members):
        self._current(armed)
        if self._member_identities(armed.project, sidecar_free=False) != members:
            raise LayoutFault('member-changed')

    def _member_identities(self, project, *, sidecar_free=True):
        self._check_lock()
        _canonical_id(project)
        with ExitStack() as stack:
            try:
                projects = stack.enter_context(closing(_DirectoryHandle(
                    os.open('projects', _DIRECTORY, dir_fd=self._root_fd)))).fd
                selected = stack.enter_context(closing(_DirectoryHandle(
                    os.open(project, _DIRECTORY, dir_fd=projects)))).fd
                parent_ids = []
                for parent, name, fd in ((self._root_fd, 'projects', projects),
                                         (projects, project, selected)):
                    identity = _owned_inode(os.fstat(fd), directory=True)
                    if identity != _owned_inode(os.stat(name, dir_fd=parent, follow_symlinks=False),
                                                directory=True):
                        raise LayoutFault('member-changed')
                    parent_ids.append(identity)
                def member(directory, name):
                    if sidecar_free:
                        return _sidecar_free_member(directory, name)
                    return _owned_inode(os.stat(name, dir_fd=directory, follow_symlinks=False))

                catalogue = member(self._root_fd, catalogue_path(self._path).name)
                ledger = member(selected, ledger_path(self._path, project).name)
            except OSError as exc:
                raise LayoutFault('member-unproved') from exc
        self._check_lock()
        return (*parent_ids, catalogue, ledger)

    def _protected_read(self, store, project, members, operation=None):
        from relay_engine import catalogue, errors, queries, restore_cancel

        if self._member_identities(project) != members:
            raise LayoutFault('member-changed')
        schema.require_sqlite()
        try:
            with ExitStack() as stack:
                catalog = stack.enter_context(closing(_immutable_incumbent_read(catalogue_path(self._path))))
                ledger = stack.enter_context(closing(_immutable_incumbent_read(ledger_path(self._path, project))))
                catalogue._require_matching(catalog)
                _require_admission_identity(catalog, store, project)
                with closing(ledger.execute('SELECT id FROM projects NOT INDEXED LIMIT 2')) as projects:
                    if projects.fetchall() != [(project,)]:
                        raise LayoutFault('member-unproved')
                with closing(ledger.execute(
                        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")) as tables:
                    schema._require_matching(ledger, {name for (name,) in tables})
                if (any_hold(catalog) is not None
                        or any(stop.scope == 'store' or stop.project_id == project
                               for stop in open_stops(catalog))
                        or restore_cancel.held(ledger, project) is not None):
                    raise LayoutFault('incumbent-hold')
                if operation is not None:
                    observed = queries.operator_admission(ledger, operation.key, operation.draft)
                    if observed is None or observed != operation.admitted:
                        raise LayoutFault('operation-unproved')
        except (ValueError, sqlite3.Error, errors.EngineError) as exc:
            raise LayoutFault('protected-read-unproved') from exc
        if self._member_identities(project) != members:
            raise LayoutFault('member-changed')


@dataclass(frozen=True)
class _RetainedIncumbentDisposition:
    kind: Literal['enrollment', 'retirement', 'recovery']
    store: str
    project: str
    root: tuple[int, int]
    lock: tuple[int, int]
    prior_epoch: str | None
    evidence: bytes

    def summary(self):
        if not isinstance(self.evidence, bytes) or not 0 < len(self.evidence) <= _INCUMBENT_LIMIT:
            raise LayoutFault('disposition-unproved')
        return _IncumbentDisposition(self.kind, self.store, self.project, self.root, self.lock,
                                     self.prior_epoch, sha256(self.evidence).hexdigest())


def _encode_idle_receipt(idle, proof):
    frame = _canonical_frame({'idle': asdict(idle), 'evidence': proof.evidence.hex()})
    if _decode_idle_receipt(frame) != (idle, proof):
        raise LayoutFault('idle-receipt-invalid')
    return frame


def _decode_idle_receipt(payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= _IDLE_RECEIPT_LIMIT:
        raise LayoutFault('idle-receipt-invalid')
    try:
        value = json.loads(payload)
        if set(value) != {'idle', 'evidence'} or _canonical_frame(value) != payload:
            raise ValueError('receipt frame')
        idle = IncumbentGuard._parse_record(_canonical_frame(value['idle']))
        if idle.phase != 'idle':
            raise ValueError('receipt must be idle')
        encoded = value['evidence']
        evidence = bytes.fromhex(encoded)
        if evidence.hex() != encoded:
            raise ValueError('receipt encoding')
        proof = _RetainedIncumbentDisposition(idle.disposition.kind, idle.store, idle.project,
                                             idle.root, idle.lock, idle.disposition.prior_epoch, evidence)
        if proof.summary() != idle.disposition:
            raise ValueError('receipt digest')
    except (LayoutFault, KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise LayoutFault('idle-receipt-invalid') from exc
    return idle, proof


@dataclass(frozen=True)
class _IncumbentPublication:
    armed: IncumbentRecord
    evidence: bytes


@dataclass(frozen=True)
class _OfflineIncumbentEnrollment:
    retained: _RetainedIncumbentDisposition
    members: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _IncumbentOperation:
    key: str
    draft: bytes
    admitted: submit.Admitted


@dataclass(frozen=True)
class _IncumbentCheckpoint:
    armed: IncumbentRecord
    members: tuple[tuple[int, int], ...]
    operation: _IncumbentOperation


@dataclass(frozen=True)
class _IncumbentChild:
    process: object
    armed: IncumbentRecord

    @classmethod
    def observe(cls, child, armed, birth, boot):
        """Bind a retained direct child before exit using observed birth and boot."""
        if (armed.phase != 'armed' or not child.is_alive()
                or (child.pid, birth, boot) != (armed.owner.pid, armed.owner.birth, armed.owner.boot)):
            raise LayoutFault('exit-unproved')
        return cls(child, armed)


@dataclass(frozen=True)
class _DirectoryHandle:
    fd: int

    def close(self):
        os.close(self.fd)


def _sidecar_free_member(directory, name):
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            os.stat(name + suffix, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise LayoutFault('journal-unresolved')
    return _owned_inode(os.stat(name, dir_fd=directory, follow_symlinks=False))


def _immutable_incumbent_read(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, isolation_level=None)
