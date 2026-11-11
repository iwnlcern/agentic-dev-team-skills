"""The machine catalogue's independent STRICT schema and refusal policy."""
import hashlib
import sqlite3

from relay_engine import errors, schema


TABLE_OWNERS: dict[str, str] = {
    'catalogue_identity': 'store',
    'project_routes': 'store',
    'key_routes': 'store',
    'schema_version': 'store',
    'engine_stops': 'store',
    'engine_stop_events': 'store',
    'checkout_routes': 'store',
}

_TABLES = (
    """CREATE TABLE catalogue_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    store_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('production', 'isolated')),
    created_at INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE project_routes (
    project_id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('creating', 'active')),
    created_at INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE key_routes (
    key_id TEXT NOT NULL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project_routes (project_id),
    created_at INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version TEXT NOT NULL,
    ddl_sha256 TEXT NOT NULL
) STRICT""",
    """CREATE TABLE engine_stops (
    row_id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('store', 'project')),
    project_id TEXT REFERENCES project_routes (project_id),
    stopped_at TEXT NOT NULL,
    stopped_by TEXT NOT NULL,
    ended_at TEXT,
    ended_by TEXT,
    CHECK ((scope = 'store') = (project_id IS NULL)),
    CHECK ((ended_at IS NULL) = (ended_by IS NULL))
) STRICT""",
    """CREATE UNIQUE INDEX engine_stops_one_open
ON engine_stops (scope, ifnull(project_id, '')) WHERE ended_at IS NULL""",
    """CREATE TABLE engine_stop_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id INTEGER NOT NULL REFERENCES engine_stops (row_id),
    kind TEXT NOT NULL CHECK (kind IN ('stop', 'start')),
    at TEXT NOT NULL,
    by TEXT NOT NULL
) STRICT""",
    """CREATE TRIGGER engine_stops_guard_update BEFORE UPDATE ON engine_stops
WHEN NOT (OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL AND NEW.ended_by IS NOT NULL
          AND NEW.row_id IS OLD.row_id AND NEW.scope IS OLD.scope
          AND NEW.project_id IS OLD.project_id AND NEW.stopped_at IS OLD.stopped_at
          AND NEW.stopped_by IS OLD.stopped_by)
BEGIN SELECT RAISE(ABORT, 'engine_stops is immutable except its first end'); END""",
    """CREATE TRIGGER engine_stops_guard_delete BEFORE DELETE ON engine_stops
BEGIN SELECT RAISE(ABORT, 'engine_stops cannot be deleted'); END""",
    """CREATE TRIGGER engine_stop_events_guard_update BEFORE UPDATE ON engine_stop_events
BEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be updated'); END""",
    """CREATE TRIGGER engine_stop_events_guard_delete BEFORE DELETE ON engine_stop_events
BEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be deleted'); END""",
    """CREATE TABLE checkout_routes (
    checkout_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project_routes (project_id),
    repository_id TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    dev INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    ended_at INTEGER,
    end_reason TEXT,
    ledger_synced_at INTEGER,
    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),
    CHECK (end_reason IN ('matched', 'unmatched'))
) STRICT""",
    """CREATE UNIQUE INDEX checkout_routes_one_open
ON checkout_routes (dev, ino) WHERE ended_at IS NULL""",
)


def ddl_digest() -> str:
    return hashlib.sha256('\n'.join(_TABLES).encode('utf-8')).hexdigest()


def _rows(conn, sql):
    cursor = conn.execute(sql)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _definitions(conn):
    return _rows(conn, "SELECT type, name, sql FROM sqlite_master "
                 "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name")


def _require_matching(conn):
    expected_conn = sqlite3.connect(':memory:')
    try:
        for statement in _TABLES:
            expected_conn.execute(statement).close()
        expected = _definitions(expected_conn)
    finally:
        expected_conn.close()
    if _definitions(conn) != expected:
        raise errors.error_for('E-SCHEMA-MISMATCH', found='other catalogue definitions')
    if _rows(conn, 'SELECT id, version, ddl_sha256 FROM schema_version') != [
            (1, '1', ddl_digest())]:
        raise errors.error_for('E-SCHEMA-MISMATCH', found='other catalogue version record')


def init_catalogue(conn) -> None:
    """Build or verify the schema inside the caller's write transaction."""
    schema.require_sqlite()
    if _definitions(conn):
        _require_matching(conn)
        return
    for statement in _TABLES:
        conn.execute(statement).close()
    conn.execute('INSERT INTO schema_version VALUES (1, ?, ?)',
                 ('1', ddl_digest())).close()
