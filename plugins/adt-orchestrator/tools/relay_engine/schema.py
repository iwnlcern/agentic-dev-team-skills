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
    last_seen_at INTEGER NOT NULL
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
)

TABLE_OWNERS: dict[str, str] = {
    "projects": "platform",
    "repositories": "platform",
    "repository_checkouts": "platform",
    "team_instances": "platform",
    "seats": "platform",
    "seat_occupants": "platform",
    "seat_sessions": "platform",
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
}

_TABLES = _PLATFORM_TABLES + (
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
)


_TRIGGERS = tuple(
    """CREATE TRIGGER IF NOT EXISTS %s_handle_fixed BEFORE UPDATE OF handle ON %s
WHEN NEW.handle IS NOT OLD.handle
BEGIN SELECT RAISE(ABORT, 'a handle never changes'); END""" % (table, table)
    for table in ("relays", "findings"))


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
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
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
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
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
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


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
