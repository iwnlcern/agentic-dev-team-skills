"""v3 tables (DD-v3-engine § 4, § 8, § 9): one schema module, STRICT tables.

A store holds one ``schema_version`` row (the version and a digest of the
statements that built it).  ``init_v3`` creates the tables in an empty file,
opens a matching store unchanged, and refuses anything else without writing.

Enum checks are generated from ``header``'s registry, so the field values
have one source.  Platform tables (projects, repositories, teams, seats)
arrive with the joint lock (plan Task 6); until then ``*_id`` columns that
name them carry no foreign key.
"""

import hashlib
import sqlite3

from relay_engine import errors
from relay_engine import header


SCHEMA_V3 = "3"
_MINIMUM = (3, 37, 0)  # STRICT tables


def _in(column, values, nullable=False):
    listed = ", ".join("'%s'" % value for value in sorted(values))
    check = "%s IN (%s)" % (column, listed)
    return "CHECK (%s IS NULL OR %s)" % (column, check) if nullable else "CHECK (%s)" % check


_FINDING_STATUS = ("open", "fixed", "fix-accepted", "withdrawn")
_SHA256 = "CHECK (length(%s) = 64 AND %s NOT GLOB '*[^0-9a-f]*')"


def _object_id(column):
    """A Git object id: 40 lowercase hex (SHA-1) or 64 (SHA-256)."""
    return "(length(%s) IN (40, 64) AND %s NOT GLOB '*[^0-9a-f]*')" % (column, column)


def _hex64(column):
    """A sha256 digest: 64 lowercase hex, as a bare predicate."""
    return "(length(%s) = 64 AND %s NOT GLOB '*[^0-9a-f]*')" % (column, column)


def _sha(column, nullable=False):
    check = _SHA256 % (column, column)
    return check.replace("CHECK (", "CHECK (%s IS NULL OR " % column, 1) if nullable else check


def _verdict_pairs():
    """A verdict only on its own kind, generated from header.VERDICTS."""
    pairs = " OR ".join(
        "(kind = '%s' AND verdict IN (%s))" % (
            kind, ", ".join("'%s'" % v for v in sorted(values)))
        for kind, values in sorted(header.VERDICTS.items()))
    return "CHECK (verdict IS NULL OR %s)" % pairs

def _arc(prefix, specials):
    """An exclusive arc: a seat by foreign key, or one of the specials this
    column may hold, never both and never neither (planner Q1)."""
    seat, special = prefix + "seat_id", prefix + "special"
    return ("%s TEXT REFERENCES seats (id),\n    %s TEXT %s\n"
            "        CHECK ((%s IS NULL) <> (%s IS NULL))"
            % (seat, special, _in(special, specials, nullable=True), seat, special))


SPECIALS = frozenset({"operator", "system"})  # J2: never team seats
HOSTS = frozenset({"claude-code", "codex", "frank", "other"})  # J3 seat_sessions.host
_LEVELS = frozenset({"master", "domain", "orchestrator", "pair"})
_SEAT_STATES = frozenset({"vacant", "occupied", "stood-down"})

# Platform's identity tables: J3 with amendment J-A1 (DD-v3-platform § Joint lock,
# frozen copy sha256 6e202276…); platform owns their semantics, the engine their DDL.
_PLATFORM_TABLES = (
    """CREATE TABLE IF NOT EXISTS projects (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    top_seat_id TEXT REFERENCES seats (id)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS repositories (
    id TEXT NOT NULL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    name TEXT NOT NULL,
    UNIQUE (project_id, name)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS repository_checkouts (
    repository_id TEXT NOT NULL REFERENCES repositories (id),
    path TEXT NOT NULL PRIMARY KEY,
    last_seen_at INTEGER NOT NULL,
    checkout_id TEXT NOT NULL,
    route_created_at INTEGER NOT NULL,
    ended_at INTEGER
) STRICT""",
    """CREATE TABLE IF NOT EXISTS team_instances (
    id TEXT NOT NULL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    name TEXT NOT NULL,
    level TEXT NOT NULL %s,
    parent_instance_id TEXT,
    opened_at INTEGER NOT NULL,
    opened_by TEXT NOT NULL,
    closed_at INTEGER,
    UNIQUE (id, project_id),
    FOREIGN KEY (parent_instance_id, project_id) REFERENCES team_instances (id, project_id)
) STRICT""" % _in("level", _LEVELS),
    """CREATE TABLE IF NOT EXISTS seats (
    id TEXT NOT NULL PRIMARY KEY,
    team_instance_id TEXT NOT NULL REFERENCES team_instances (id),
    role TEXT NOT NULL,
    state TEXT NOT NULL %s,
    UNIQUE (team_instance_id, role)
) STRICT""" % _in("state", _SEAT_STATES),
    """CREATE TABLE IF NOT EXISTS seat_occupants (
    id TEXT NOT NULL PRIMARY KEY,
    seat_id TEXT NOT NULL REFERENCES seats (id),
    key_id TEXT NOT NULL UNIQUE,
    key_verifier TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    ended_at INTEGER,
    UNIQUE (id, seat_id)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS seat_sessions (
    id TEXT NOT NULL PRIMARY KEY,
    seat_id TEXT NOT NULL REFERENCES seats (id),
    occupant_id TEXT NOT NULL,
    host TEXT NOT NULL %s,
    host_session_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    signed_in_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    ended_at INTEGER,
    UNIQUE (seat_id, generation),
    FOREIGN KEY (occupant_id, seat_id) REFERENCES seat_occupants (id, seat_id)
) STRICT""" % _in("host", HOSTS),
    """CREATE TABLE IF NOT EXISTS mutation_receipts (
    principal_kind TEXT NOT NULL CHECK (principal_kind = 'occupant'),
    principal_id TEXT NOT NULL REFERENCES seat_occupants (id),
    operation TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (%s),
    result TEXT NOT NULL,
    committed_at INTEGER NOT NULL,
    UNIQUE (principal_kind, principal_id, operation, operation_key)
) STRICT""" % _hex64("request_digest"),
)

TABLE_OWNERS: dict[str, str] = {
    "project_settings": "engine",
    "settings_history": "engine",
    "projects": "platform",
    "repositories": "platform",
    "repository_checkouts": "platform",
    "team_instances": "platform",
    "seats": "platform",
    "seat_occupants": "platform",
    "seat_sessions": "platform",
    "mutation_receipts": "platform",
    "git_states": "engine",
    "relays": "engine",
    "bodies": "engine",
    "recipients": "engine",
    "edges": "engine",
    "bundles": "engine",
    "findings": "engine",
    "finding_events": "engine",
    "refusals": "engine",
    "seat_activity": "engine",
    "token_counts": "engine",
    "schema_version": "engine",
    "snapshots": "engine",
    "lifecycle_outbox": "console",
    "lifecycle_outbox_events": "console",
}

_TABLES = _PLATFORM_TABLES + (
    """CREATE TABLE IF NOT EXISTS project_settings (
    project_id TEXT NOT NULL REFERENCES projects (id),
    key TEXT NOT NULL CHECK (key IN ('jev_record', 'jev_act', 'doorbell_off_minutes')),
    value TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS settings_history (
    project_id TEXT NOT NULL REFERENCES projects (id),
    revision INTEGER NOT NULL,
    key TEXT NOT NULL,
    old TEXT,
    new TEXT NOT NULL,
    by_principal TEXT NOT NULL CHECK (by_principal = 'operator'),
    at INTEGER NOT NULL,
    PRIMARY KEY (project_id, revision)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS git_states (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories (id),
    head_commit TEXT CHECK (head_commit IS NULL OR %s),
    branch TEXT,
    dirty_paths INTEGER NOT NULL CHECK (dirty_paths >= 0),
    porcelain_sha256 TEXT NOT NULL %s,
    measured_at INTEGER NOT NULL
) STRICT""" % (_object_id("head_commit"), _sha("porcelain_sha256")),
    """CREATE TABLE IF NOT EXISTS relays (
    id TEXT PRIMARY KEY,
    handle INTEGER NOT NULL CHECK (handle >= 1),
    project_id TEXT NOT NULL REFERENCES projects (id),
    team_instance_id TEXT REFERENCES team_instances (id),
    bundle_id TEXT REFERENCES bundles (id),
    kind TEXT NOT NULL %s,
    verdict TEXT,
    needs TEXT NOT NULL %s,
    subject TEXT NOT NULL,
    parent_id TEXT REFERENCES relays (id),
    %s,
    sender_role TEXT,
    created_at INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL %s,
    meta_sha256 TEXT NOT NULL %s,
    body_bytes INTEGER NOT NULL CHECK (body_bytes >= 0),
    header_bytes INTEGER NOT NULL CHECK (header_bytes >= 0),
    git_state_id TEXT REFERENCES git_states (id),
    submission_key TEXT CHECK (submission_key IS NULL OR length(submission_key) > 0),
    draft_sha256 TEXT %s,
    CHECK ((submission_key IS NULL) = (draft_sha256 IS NULL)),
    UNIQUE (project_id, handle),
    UNIQUE (project_id, created_at),
    %s,
    CHECK (kind != 'BOOT' OR sender_special IS 'system')
) STRICT""" % (_in("kind", header.KINDS | {"BOOT"}),
               _in("needs", header.NEEDS), _arc("sender_", SPECIALS),
               _SHA256 % ("body_sha256", "body_sha256"),
               _SHA256 % ("meta_sha256", "meta_sha256"),
               _sha("draft_sha256", nullable=True), _verdict_pairs()),
    """CREATE TABLE IF NOT EXISTS bodies (
    relay_id TEXT PRIMARY KEY REFERENCES relays (id),
    body BLOB NOT NULL
) STRICT""",
    """CREATE TABLE IF NOT EXISTS recipients (
    relay_id TEXT NOT NULL REFERENCES relays (id),
    %s,
    role TEXT NOT NULL CHECK (role IN ('to', 'cc')),
    delivered_at INTEGER,
    opened_at INTEGER,
    answered_by TEXT REFERENCES relays (id),
    UNIQUE (relay_id, seat_id),
    UNIQUE (relay_id, special)
) STRICT""" % _arc("", {"operator"}),
    """CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    from_relay_id TEXT NOT NULL REFERENCES relays (id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('parent', 'ref')),
    target_type TEXT NOT NULL CHECK (target_type IN ('relay', 'git', 'path')),
    spelling TEXT NOT NULL,
    target_relay_id TEXT REFERENCES relays (id),
    target_repository_name TEXT,
    target_repository_id TEXT REFERENCES repositories (id),
    target_rev TEXT,
    target_commit TEXT,
    target_path TEXT,
    target_line_start INTEGER,
    target_line_end INTEGER,
    digest_at_submit TEXT CHECK (digest_at_submit IS NULL OR %s
        OR (target_type = 'git' AND %s)),
    UNIQUE (from_relay_id, kind, ordinal),
    CHECK ((target_repository_name IS NULL) = (target_repository_id IS NULL)),
    CHECK ((target_type = 'relay' AND target_relay_id IS NOT NULL
            AND target_repository_id IS NULL AND target_path IS NULL)
        OR (target_type = 'git' AND target_relay_id IS NULL
            AND target_repository_id IS NOT NULL AND target_commit IS NOT NULL
            AND target_path IS NOT NULL)
        OR (target_type = 'path' AND target_relay_id IS NULL
            AND target_repository_id IS NULL AND target_path IS NOT NULL))
) STRICT""" % (_hex64("digest_at_submit"), _object_id("digest_at_submit")),
    """CREATE TABLE IF NOT EXISTS bundles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    team_instance_id TEXT REFERENCES team_instances (id),
    name TEXT NOT NULL,
    %s,
    closed_at INTEGER,
    UNIQUE (project_id, name)
) STRICT""" % _arc("opened_by_", SPECIALS),
    """CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    handle INTEGER NOT NULL CHECK (handle >= 1),
    project_id TEXT NOT NULL REFERENCES projects (id),
    bundle_id TEXT REFERENCES bundles (id),
    raised_in TEXT NOT NULL REFERENCES relays (id),
    raiser_seat_id TEXT NOT NULL REFERENCES seats (id),
    severity TEXT NOT NULL %s,
    check_edge_id TEXT REFERENCES edges (id),
    summary TEXT NOT NULL CHECK (length(summary) <= 200),
    status TEXT NOT NULL %s,
    fixed_commit TEXT,
    resolved_in TEXT REFERENCES relays (id),
    reason TEXT,
    UNIQUE (project_id, handle)
) STRICT""" % (_in("severity", header.SEVERITIES), _in("status", _FINDING_STATUS)),
    """CREATE TABLE IF NOT EXISTS finding_events (
    id INTEGER PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES findings (id),
    relay_id TEXT NOT NULL REFERENCES relays (id),
    transition TEXT NOT NULL %s,
    from_status TEXT NOT NULL %s,
    to_status TEXT NOT NULL %s,
    fixed_commit TEXT,
    reason TEXT,
    at INTEGER NOT NULL
) STRICT""" % (_in("transition", {"raised", "fixes"} | header.RESOLUTIONS),
               _in("from_status", _FINDING_STATUS), _in("to_status", _FINDING_STATUS)),
    """CREATE TABLE IF NOT EXISTS refusals (
    id INTEGER PRIMARY KEY,
    project_id TEXT REFERENCES projects (id),
    %s,
    code TEXT NOT NULL,
    line INTEGER,
    at INTEGER NOT NULL,
    draft_sha256 TEXT NOT NULL %s
) STRICT""" % (_arc("", SPECIALS), _sha("draft_sha256")),
    """CREATE TABLE IF NOT EXISTS seat_activity (
    seat_id TEXT NOT NULL PRIMARY KEY REFERENCES seats (id),
    last_request_at INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE IF NOT EXISTS token_counts (
    id INTEGER PRIMARY KEY,
    seat_id TEXT NOT NULL REFERENCES seats (id),
    bundle_id TEXT REFERENCES bundles (id),
    source TEXT NOT NULL CHECK (source IN ('host-log', 'host-reported')),
    input INTEGER NOT NULL CHECK (input >= 0),
    output INTEGER NOT NULL CHECK (output >= 0),
    cache_read INTEGER NOT NULL CHECK (cache_read >= 0),
    cache_write INTEGER NOT NULL CHECK (cache_write >= 0),
    copied_at INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    handle INTEGER NOT NULL UNIQUE CHECK (handle >= 1),
    relay_id TEXT NOT NULL REFERENCES relays (id),
    edge_id TEXT NOT NULL UNIQUE REFERENCES edges (id),
    artifact TEXT NOT NULL,
    content_sha256 TEXT NOT NULL %s,
    bytes_len INTEGER NOT NULL CHECK (bytes_len >= 0),
    has_bytes INTEGER NOT NULL CHECK (has_bytes IN (0, 1)),
    locked_by TEXT REFERENCES relays (id),
    locked_at INTEGER,
    CHECK ((locked_by IS NULL) = (locked_at IS NULL)),
    CHECK (has_bytes = 0 OR bytes_len <= 1048576)
) STRICT""" % _sha("content_sha256"),
    """CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version TEXT NOT NULL,
    ddl_sha256 TEXT NOT NULL %s
) STRICT""" % _sha("ddl_sha256"),
    """CREATE TABLE IF NOT EXISTS lifecycle_outbox (
    project_id TEXT NOT NULL REFERENCES projects (id),
    position INTEGER NOT NULL CHECK (position >= 1),
    row_id TEXT NOT NULL UNIQUE CHECK (length(row_id) = 32 AND row_id NOT GLOB '*[^0-9a-f]*'),
    subkind TEXT NOT NULL CHECK (subkind IN ('seat_registered', 'seat_replaced', 'seat_stood_down',
                                             'settlement_finished', 'settlement_undone',
                                             'engine_restart_requested', 'operator_act_carried',
                                             'merge_target_changed', 'seat_unseated')),
    committed_at_ms INTEGER NOT NULL CHECK (committed_at_ms >= 0),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('occupant', 'operator', 'program')),
    actor_occupant_id TEXT REFERENCES seat_occupants (id),
    seat_id TEXT REFERENCES seats (id),
    old_occupant_id TEXT,
    new_occupant_id TEXT,
    chain_id TEXT CHECK (chain_id IS NULL OR length(chain_id) >= 1),
    runtime_instance_id TEXT CHECK (runtime_instance_id IS NULL OR length(runtime_instance_id) >= 1),
    transaction_ordinal INTEGER CHECK (transaction_ordinal IS NULL OR transaction_ordinal >= 0),
    occasion TEXT CHECK (occasion IS NULL OR occasion IN ('start', 'running', 'stop', 'resolve')),
    act_kind TEXT CHECK (act_kind IS NULL OR length(act_kind) >= 1),
    instruction_relay_id TEXT CHECK (instruction_relay_id IS NULL OR length(instruction_relay_id) >= 1),
    event_number INTEGER CHECK (event_number IS NULL OR event_number >= 1),
    slip_id TEXT CHECK (slip_id IS NULL OR length(slip_id) >= 1),
    pr INTEGER CHECK (pr IS NULL OR pr >= 1),
    merge_commit TEXT CHECK (merge_commit IS NULL OR %s),
    allowed_target TEXT CHECK (allowed_target IS NULL OR length(allowed_target) >= 1),
    landed_target TEXT CHECK (landed_target IS NULL OR length(landed_target) >= 1),
    generation INTEGER CHECK (generation IS NULL OR generation >= 1),
    surface TEXT CHECK (surface IS NULL OR surface IN ('terminal', 'page')),
    carrying_seat_id TEXT REFERENCES seats (id),
    PRIMARY KEY (project_id, position),
    CHECK ((actor_kind = 'occupant') = (actor_occupant_id IS NOT NULL)),
    CHECK ((subkind IN ('settlement_finished', 'settlement_undone', 'merge_target_changed')) = (actor_kind = 'program')),
    CHECK ((subkind = 'engine_restart_requested') <= (actor_kind = 'occupant')),
    CHECK ((subkind = 'operator_act_carried') = (actor_kind = 'operator' AND act_kind IS NOT NULL)),
    CHECK (occasion IS NULL OR occasion = 'start' OR subkind = 'settlement_undone'),
    CHECK (CASE subkind
        WHEN 'seat_registered' THEN seat_id IS NOT NULL AND old_occupant_id IS NULL AND new_occupant_id IS NOT NULL
        WHEN 'seat_replaced' THEN seat_id IS NOT NULL AND old_occupant_id IS NOT NULL AND new_occupant_id IS NOT NULL
                                  AND old_occupant_id <> new_occupant_id
        WHEN 'seat_stood_down' THEN seat_id IS NOT NULL AND new_occupant_id IS NULL
        WHEN 'settlement_finished' THEN seat_id IS NULL AND old_occupant_id IS NULL AND new_occupant_id IS NULL
        WHEN 'settlement_undone' THEN seat_id IS NULL AND old_occupant_id IS NULL AND new_occupant_id IS NULL
        WHEN 'engine_restart_requested' THEN seat_id IS NOT NULL AND old_occupant_id IS NULL AND new_occupant_id IS NULL
        WHEN 'operator_act_carried' THEN seat_id IS NOT NULL AND old_occupant_id IS NULL AND new_occupant_id IS NULL
        WHEN 'merge_target_changed' THEN seat_id IS NOT NULL AND old_occupant_id IS NULL AND new_occupant_id IS NULL
        WHEN 'seat_unseated' THEN seat_id IS NOT NULL AND old_occupant_id IS NULL AND new_occupant_id IS NULL
        ELSE 0 END),
    CHECK ((subkind IN ('settlement_finished', 'settlement_undone'))
           = (runtime_instance_id IS NOT NULL AND transaction_ordinal IS NOT NULL AND occasion IS NOT NULL)),
    CHECK ((subkind IN ('settlement_finished', 'settlement_undone', 'operator_act_carried')) = (chain_id IS NOT NULL)),
    CHECK (subkind IN ('settlement_finished', 'settlement_undone')
           OR (runtime_instance_id IS NULL AND transaction_ordinal IS NULL AND occasion IS NULL)),
    CHECK ((subkind = 'operator_act_carried')
           = (act_kind IS NOT NULL AND instruction_relay_id IS NOT NULL AND event_number IS NOT NULL)),
    CHECK (subkind = 'operator_act_carried' OR (act_kind IS NULL AND event_number IS NULL)),
    CHECK (subkind IN ('operator_act_carried', 'seat_unseated') OR instruction_relay_id IS NULL),
    CHECK ((subkind = 'seat_unseated') = (generation IS NOT NULL)),
    CHECK ((subkind = 'seat_unseated') <= (actor_kind = 'operator')),
    CHECK (subkind = 'seat_unseated' OR (surface IS NULL AND carrying_seat_id IS NULL)),
    CHECK (subkind <> 'seat_unseated' OR ((surface IS NULL) <> (carrying_seat_id IS NULL))),
    CHECK (subkind <> 'seat_unseated' OR ((carrying_seat_id IS NULL) = (instruction_relay_id IS NULL))),
    CHECK ((subkind = 'merge_target_changed')
           = (slip_id IS NOT NULL AND pr IS NOT NULL AND merge_commit IS NOT NULL
              AND allowed_target IS NOT NULL AND landed_target IS NOT NULL)),
    CHECK (subkind = 'merge_target_changed'
           OR (slip_id IS NULL AND pr IS NULL AND merge_commit IS NULL
               AND allowed_target IS NULL AND landed_target IS NULL)),
    CHECK (landed_target IS NULL OR landed_target <> allowed_target),
    FOREIGN KEY (old_occupant_id, seat_id) REFERENCES seat_occupants (id, seat_id),
    FOREIGN KEY (new_occupant_id, seat_id) REFERENCES seat_occupants (id, seat_id)
) STRICT""" % _object_id("merge_commit"),
    """CREATE TABLE IF NOT EXISTS lifecycle_outbox_events (
    project_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    event_index INTEGER NOT NULL CHECK (event_index >= 1),
    number INTEGER NOT NULL CHECK (number >= 1),
    kind TEXT NOT NULL CHECK (length(kind) >= 1),
    subject TEXT NOT NULL CHECK (length(subject) >= 1),
    PRIMARY KEY (project_id, position, event_index),
    FOREIGN KEY (project_id, position) REFERENCES lifecycle_outbox (project_id, position)
) STRICT""",
)

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS relays_submission_party ON relays"
    " (coalesce(sender_seat_id, sender_special), submission_key)"
    " WHERE submission_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS relays_parent ON relays (parent_id)",
    "CREATE INDEX IF NOT EXISTS relays_project_id ON relays (project_id, id)",
    "CREATE INDEX IF NOT EXISTS relays_bundle ON relays (bundle_id)",
    "CREATE INDEX IF NOT EXISTS relays_sender_kind ON relays (sender_seat_id, kind)",
    "CREATE INDEX IF NOT EXISTS recipients_seat ON recipients (seat_id)",
    "CREATE INDEX IF NOT EXISTS recipients_undelivered ON recipients (seat_id)"
    " WHERE role = 'to' AND delivered_at IS NULL AND opened_at IS NULL",
    "CREATE INDEX IF NOT EXISTS recipients_open ON recipients (relay_id)"
    " WHERE role = 'to' AND answered_by IS NULL",
    "CREATE INDEX IF NOT EXISTS edges_from ON edges (from_relay_id)",
    "CREATE INDEX IF NOT EXISTS edges_target_relay ON edges (target_relay_id)",
    "CREATE INDEX IF NOT EXISTS edges_target_git ON edges (target_repository_id, target_path)",
    "CREATE INDEX IF NOT EXISTS edges_target_path ON edges (target_path)",
    "CREATE INDEX IF NOT EXISTS findings_bundle_status ON findings (bundle_id, status)",
    "CREATE INDEX IF NOT EXISTS findings_raised_in ON findings (raised_in)",
    "CREATE INDEX IF NOT EXISTS findings_resolved_in ON findings (resolved_in)",
    "CREATE INDEX IF NOT EXISTS finding_events_finding ON finding_events (finding_id)",
    "CREATE INDEX IF NOT EXISTS refusals_project ON refusals (project_id, code)",
    "CREATE INDEX IF NOT EXISTS seat_sessions_seat ON seat_sessions (seat_id)",
    "CREATE INDEX IF NOT EXISTS token_counts_bundle ON token_counts (bundle_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS team_instances_open_name ON team_instances"
    " (project_id, name) WHERE closed_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS seat_occupants_open ON seat_occupants (seat_id)"
    " WHERE ended_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS seat_sessions_open ON seat_sessions (seat_id)"
    " WHERE ended_at IS NULL",
    "CREATE INDEX IF NOT EXISTS repositories_project ON repositories (project_id)",
    "CREATE INDEX IF NOT EXISTS snapshots_relay ON snapshots (relay_id)",
    "CREATE INDEX IF NOT EXISTS snapshots_artifact ON snapshots (artifact, handle)",
    "CREATE INDEX IF NOT EXISTS snapshots_content ON snapshots (content_sha256)",
    # evidence_pointed's probe stops at the first stored row of a digest (slice 2a D3).
    "CREATE INDEX IF NOT EXISTS snapshots_content_bytes ON snapshots (content_sha256)"
    " WHERE has_bytes = 1",
)


_TRIGGERS = tuple(
    """CREATE TRIGGER IF NOT EXISTS %s_handle_fixed BEFORE UPDATE OF handle ON %s
WHEN NEW.handle IS NOT OLD.handle
BEGIN SELECT RAISE(ABORT, 'a handle never changes'); END""" % (table, table)
    for table in ("relays", "findings")) + (
    # A closed bundle stays closed (slice 2a D4); setting it once, or to the same value, is allowed.
    """CREATE TRIGGER IF NOT EXISTS bundles_closed_at_fixed BEFORE UPDATE OF closed_at ON bundles
WHEN OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT OLD.closed_at
BEGIN SELECT RAISE(ABORT, 'a closed bundle stays closed'); END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_outbox_fixed BEFORE UPDATE ON lifecycle_outbox
BEGIN SELECT RAISE(ABORT, 'a lifecycle outbox row never changes'); END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_outbox_kept BEFORE DELETE ON lifecycle_outbox
BEGIN SELECT RAISE(ABORT, 'a lifecycle outbox row is never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_outbox_events_fixed BEFORE UPDATE ON lifecycle_outbox_events
BEGIN SELECT RAISE(ABORT, 'a lifecycle outbox event never changes'); END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_outbox_events_kept BEFORE DELETE ON lifecycle_outbox_events
BEGIN SELECT RAISE(ABORT, 'a lifecycle outbox event is never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_outbox_events_settlement_only BEFORE INSERT ON lifecycle_outbox_events
WHEN (SELECT subkind FROM lifecycle_outbox WHERE project_id = NEW.project_id AND position = NEW.position)
     NOT IN ('settlement_finished', 'settlement_undone')
BEGIN SELECT RAISE(ABORT, 'only a settlement row has events'); END""",
)


def ddl_digest():
    """sha256 of every statement that builds the store, in order."""
    return hashlib.sha256(
        "\n".join(_TABLES + _INDEXES + _TRIGGERS).encode("utf-8")).hexdigest()


def require_sqlite(minimum=_MINIMUM):
    found = tuple(sqlite3.sqlite_version_info)
    if found < tuple(minimum):
        raise errors.error_for(
            "E-SQLITE-VERSION", found=".".join(map(str, found)),
            minimum=".".join(map(str, minimum)))


def init_v3(conn):
    """Create the v3 store in an empty file, in one transaction; open a
    matching store unchanged; refuse anything else and write nothing."""
    require_sqlite()
    conn.execute("BEGIN IMMEDIATE")
    try:
        names = {name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}
        if names:
            _require_matching(conn, names)
            conn.execute("ROLLBACK")
            return
        for statement in _TABLES + _INDEXES + _TRIGGERS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (id, version, ddl_sha256) VALUES (1, ?, ?)",
            (SCHEMA_V3, ddl_digest()))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _require_matching(conn, names):
    tables = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}
    row = None
    if "schema_version" in tables:
        row = conn.execute(
            "SELECT version, ddl_sha256 FROM schema_version WHERE id = 1").fetchone()
    if row is None:
        count = len(tables)
        found = "%d table%s and no v3 version record" % (count, "" if count == 1 else "s")
    elif row[0] != SCHEMA_V3:
        found = "v3 schema version %s" % header._display(str(row[0]), 40)
    elif row[1] != ddl_digest():
        found = "v3 schema version %s with other table definitions" % SCHEMA_V3
    else:
        difference = _definition_difference(conn)
        if difference is None:
            return
        found = "v3 schema version %s with %s" % (SCHEMA_V3, difference)
    raise errors.error_for("E-SCHEMA-MISMATCH", found=found)


def _definitions(conn):
    return {(kind, name): sql for kind, name, sql in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}


def _definition_difference(conn):
    """The first way the file's own definitions differ from the ones this
    engine builds (A13), or None; the version record alone is not trusted."""
    expected_conn = sqlite3.connect(":memory:")
    try:
        for statement in _TABLES + _INDEXES + _TRIGGERS:
            expected_conn.execute(statement)
        expected = _definitions(expected_conn)
    finally:
        expected_conn.close()
    found = _definitions(conn)
    for (kind, name) in sorted(set(found) - set(expected)):
        return "an extra %s %s" % (kind, header._display(name, 60))
    for (kind, name) in sorted(set(expected) - set(found)):
        return "no %s %s" % (kind, header._display(name, 60))
    for key in sorted(expected):
        if found[key] != expected[key]:
            return "a changed %s %s" % (key[0], header._display(key[1], 60))
    return None
