"""The machine catalogue's independent STRICT schema and refusal policy."""
import hashlib
import sqlite3

from relay_engine import errors, schema


TABLE_OWNERS: dict[str, str] = {
    'catalogue_identity': 'store',
    'project_routes': 'store',
    'key_routes': 'store',
    'schema_version': 'store',
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
                 "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")


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
    """Build an empty catalogue or verify its exact schema without writing."""
    schema.require_sqlite()
    conn.execute('BEGIN IMMEDIATE').close()
    try:
        if _definitions(conn):
            _require_matching(conn)
            conn.execute('ROLLBACK').close()
            return
        for statement in _TABLES:
            conn.execute(statement).close()
        conn.execute('INSERT INTO schema_version VALUES (1, ?, ?)',
                     ('1', ddl_digest())).close()
        conn.execute('COMMIT').close()
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK').close()
        raise
