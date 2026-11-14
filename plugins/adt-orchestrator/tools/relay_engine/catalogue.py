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
    'queue_items': 'console',
    'queue_conditions': 'console',
    'queue_events': 'console',
    'queue_answers': 'console',
    'queue_receipts': 'console',
    'queue_notifications': 'console',
    'queue_highwaters': 'console',
    'queue_notifier_health': 'console',
    'queue_store_item_sources': 'console',
    'queue_ingest_skips': 'console',
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
    """CREATE TABLE queue_items (
    item_id TEXT PRIMARY KEY,
    kind TEXT CHECK (kind IN ('question', 'cross_project_permission', 'merge_go_ahead', 'verification_held', 'notice')),
    subkind TEXT CHECK ((subkind IS NULL) = (kind <> 'notice') AND (subkind IS NULL OR subkind IN (
        'seat_registered', 'seat_replaced', 'seat_stood_down', 'seat_unseated',
        'engine_restart_requested', 'engine_updated', 'engine_update_failed',
        'doorbell_off', 'notifier_unhealthy', 'operator_act_carried', 'worker_stalled',
        'bridge_refused', 'panel_key_enrolled', 'answer_faulted', 'instruction_carried',
        'seat_record_missing', 'seat_ambiguous'))),
    project_id TEXT CHECK ((project_id IS NULL) = (
        subkind IS 'notifier_unhealthy' OR subkind IS 'worker_stalled' OR
        subkind IS 'bridge_refused' OR subkind IS 'panel_key_enrolled' OR
        subkind IS 'engine_updated' OR subkind IS 'engine_update_failed' OR
        subkind IS 'instruction_carried' OR subkind IS 'seat_record_missing' OR
        subkind IS 'seat_ambiguous')),
    raiser TEXT NOT NULL,
    asking_seat_id TEXT,
    actor_address TEXT,
    fields TEXT,
    answer_to TEXT CHECK (answer_to IS NULL OR kind = 'question'),
    source_handle INTEGER CHECK (source_handle IS NULL OR kind = 'question'),
    source_relay_id TEXT UNIQUE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    state TEXT CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),
    texts TEXT NOT NULL,
    popup_text TEXT,
    choices TEXT,
    choices_reason TEXT,
    pins TEXT NOT NULL,
    condition_key TEXT NOT NULL,
    raised_position INTEGER NOT NULL,
    raised_at_ms INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE queue_conditions (
    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),
    kind TEXT,
    subkind TEXT,
    project_id TEXT,
    condition_key TEXT,
    raiser TEXT,
    raised_at_ms INTEGER,
    clear_requested INTEGER NOT NULL DEFAULT 0 CHECK (clear_requested IN (0, 1)),
    closed_at_ms INTEGER,
    closed_position INTEGER
) STRICT""",
    """CREATE UNIQUE INDEX queue_conditions_open_key
ON queue_conditions (kind, COALESCE(subkind, ''), COALESCE(project_id, ''), condition_key)
WHERE closed_at_ms IS NULL""",
    """CREATE TABLE queue_events (
    position INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES queue_items (item_id),
    revision INTEGER,
    state TEXT CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),
    event_kind TEXT CHECK (event_kind IN ('raised', 'answered', 'applied', 'superseded', 'refused', 'faulted', 'withdrawn', 'opened', 'dismissed', 'dismissed_all', 'cleared', 'clear_requested', 'condition_closed', 'retired')),
    principal_kind TEXT,
    principal_id TEXT,
    receipt_id TEXT,
    detail TEXT,
    at_ms INTEGER
) STRICT""",
    """CREATE TABLE queue_answers (
    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),
    answer_id TEXT UNIQUE,
    answer TEXT NOT NULL,
    answered_at_ms INTEGER NOT NULL CHECK (answered_at_ms >= 0),
    apply_key TEXT UNIQUE NOT NULL,
    draft BLOB,
    call_args TEXT,
    project_id TEXT NOT NULL,
    phase TEXT CHECK (phase IN ('recorded', 'applied', 'confirmed', 'faulted')),
    last_reason TEXT,
    outcome TEXT
) STRICT""",
    """CREATE TABLE queue_receipts (
    receipt_id TEXT PRIMARY KEY,
    principal_kind TEXT,
    principal_id TEXT,
    operation TEXT,
    operation_key TEXT,
    request_digest TEXT CHECK (length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'),
    result TEXT NOT NULL,
    committed_at_ms INTEGER,
    UNIQUE (principal_kind, principal_id, operation, operation_key)
) STRICT""",
    """CREATE TABLE queue_notifications (
    item_id TEXT REFERENCES queue_items (item_id),
    revision INTEGER,
    state TEXT CHECK (state IN ('pending', 'delivered', 'retired')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_class TEXT,
    next_attempt_at_ms INTEGER,
    PRIMARY KEY (item_id, revision)
) STRICT""",
    """CREATE TABLE queue_highwaters (
    project_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('question', 'lifecycle', 'lifecycle_log', 'stop_events', 'store_items')),
    position INTEGER NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    anchor_id TEXT,
    CHECK ((project_id IS NULL) = (source IN ('lifecycle_log', 'stop_events', 'store_items'))),
    CHECK (source NOT IN ('question', 'lifecycle', 'lifecycle_log') OR (position = 0) = (anchor_id IS NULL)),
    CHECK (source IN ('question', 'lifecycle', 'lifecycle_log') OR anchor_id IS NULL)
) STRICT""",
    """CREATE UNIQUE INDEX queue_highwaters_key
ON queue_highwaters (COALESCE(project_id, ''), source)""",
    """CREATE TABLE queue_notifier_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    updated_at_ms INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE queue_store_item_sources (
    raise_row_id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES queue_items (item_id)
) STRICT""",
    """CREATE TABLE queue_ingest_skips (
    source TEXT NOT NULL CHECK (source IN ('store_items', 'lifecycle_log')),
    row_ref TEXT NOT NULL CHECK (length(row_ref) >= 1),
    reason TEXT NOT NULL CHECK (length(reason) >= 1),
    skipped_at_ms INTEGER NOT NULL CHECK (skipped_at_ms >= 0),
    UNIQUE (source, row_ref)
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
