"""v3 tables (DD-v3-engine § 4, § 8, § 9): one schema module, STRICT tables.

Enum checks are generated from ``header``'s registry, so the field values
have one source.  Platform tables (projects, repositories, teams, seats)
arrive with the joint lock (plan Task 6); until then ``*_id`` columns that
name them carry no foreign key.
"""

import sqlite3

from relay_engine import errors
from relay_engine import header


SCHEMA_V3 = "3"
_MINIMUM = (3, 37, 0)  # STRICT tables


def _in(column, values, nullable=False):
    listed = ", ".join("'%s'" % value for value in sorted(values))
    check = "%s IN (%s)" % (column, listed)
    return "CHECK (%s IS NULL OR %s)" % (column, check) if nullable else "CHECK (%s)" % check


_VERDICTS = set().union(*header.VERDICTS.values())
_FINDING_STATUS = ("open", "fixed", "fix-accepted", "withdrawn")
_SHA256 = "CHECK (length(%s) = 64)"

_TABLES = (
    """CREATE TABLE IF NOT EXISTS git_states (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    head_commit TEXT NOT NULL,
    branch TEXT,
    dirty_paths TEXT NOT NULL,
    porcelain_sha256 TEXT NOT NULL %s,
    measured_at INTEGER NOT NULL
) STRICT""" % (_SHA256 % "porcelain_sha256"),
    """CREATE TABLE IF NOT EXISTS relays (
    id TEXT PRIMARY KEY,
    handle INTEGER NOT NULL CHECK (handle >= 1),
    project_id TEXT NOT NULL,
    team_instance_id TEXT,
    bundle_id TEXT REFERENCES bundles (id),
    kind TEXT NOT NULL %s,
    verdict TEXT %s,
    needs TEXT NOT NULL %s,
    subject TEXT NOT NULL,
    parent_id TEXT REFERENCES relays (id),
    sender_seat_id TEXT NOT NULL,
    sender_role TEXT,
    created_at INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL %s,
    body_bytes INTEGER NOT NULL CHECK (body_bytes >= 0),
    header_bytes INTEGER NOT NULL CHECK (header_bytes >= 0),
    git_state_id TEXT REFERENCES git_states (id),
    UNIQUE (project_id, handle),
    UNIQUE (project_id, created_at)
) STRICT""" % (_in("kind", header.KINDS | {"BOOT"}),
               _in("verdict", _VERDICTS, nullable=True),
               _in("needs", header.NEEDS), _SHA256 % "body_sha256"),
    """CREATE TABLE IF NOT EXISTS bodies (
    relay_id TEXT PRIMARY KEY REFERENCES relays (id),
    body BLOB NOT NULL
) STRICT""",
    """CREATE TABLE IF NOT EXISTS recipients (
    relay_id TEXT NOT NULL REFERENCES relays (id),
    seat_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('to', 'cc')),
    delivered_at INTEGER,
    opened_at INTEGER,
    answered_by TEXT REFERENCES relays (id),
    PRIMARY KEY (relay_id, seat_id)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    from_relay_id TEXT NOT NULL REFERENCES relays (id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('parent', 'ref')),
    target_type TEXT NOT NULL CHECK (target_type IN ('relay', 'git', 'path')),
    spelling TEXT NOT NULL,
    target_relay_id TEXT REFERENCES relays (id),
    target_repository_id TEXT,
    target_rev TEXT,
    target_commit TEXT,
    target_path TEXT,
    target_line_start INTEGER,
    target_line_end INTEGER,
    digest_at_submit TEXT,
    UNIQUE (from_relay_id, kind, ordinal)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS bundles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    team_instance_id TEXT,
    name TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    closed_at INTEGER,
    UNIQUE (project_id, name)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    handle INTEGER NOT NULL CHECK (handle >= 1),
    project_id TEXT NOT NULL,
    bundle_id TEXT REFERENCES bundles (id),
    raised_in TEXT NOT NULL REFERENCES relays (id),
    raiser_seat_id TEXT NOT NULL,
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
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    fixed_commit TEXT,
    reason TEXT,
    at INTEGER NOT NULL
) STRICT""" % _in("transition", {"raised", "fixes"} | header.RESOLUTIONS),
    """CREATE TABLE IF NOT EXISTS refusals (
    id INTEGER PRIMARY KEY,
    project_id TEXT,
    seat_id TEXT,
    code TEXT NOT NULL,
    line INTEGER,
    at INTEGER NOT NULL,
    draft_sha256 TEXT NOT NULL
) STRICT""",
    """CREATE TABLE IF NOT EXISTS seat_activity (
    seat_id TEXT PRIMARY KEY,
    last_request_at INTEGER NOT NULL
) STRICT""",
    """CREATE TABLE IF NOT EXISTS seat_sessions (
    session_id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    seat_id TEXT NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER
) STRICT""",
    """CREATE TABLE IF NOT EXISTS token_counts (
    id INTEGER PRIMARY KEY,
    seat_id TEXT NOT NULL,
    bundle_id TEXT REFERENCES bundles (id),
    source TEXT NOT NULL CHECK (source IN ('host-log', 'host-reported')),
    input INTEGER NOT NULL CHECK (input >= 0),
    output INTEGER NOT NULL CHECK (output >= 0),
    cache_read INTEGER NOT NULL CHECK (cache_read >= 0),
    cache_write INTEGER NOT NULL CHECK (cache_write >= 0),
    copied_at INTEGER NOT NULL
) STRICT""",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS relays_parent ON relays (parent_id)",
    "CREATE INDEX IF NOT EXISTS relays_bundle ON relays (bundle_id)",
    "CREATE INDEX IF NOT EXISTS recipients_seat ON recipients (seat_id)",
    "CREATE INDEX IF NOT EXISTS edges_from ON edges (from_relay_id)",
    "CREATE INDEX IF NOT EXISTS edges_target_relay ON edges (target_relay_id)",
    "CREATE INDEX IF NOT EXISTS edges_target_git ON edges (target_repository_id, target_path)",
    "CREATE INDEX IF NOT EXISTS edges_target_path ON edges (target_path)",
    "CREATE INDEX IF NOT EXISTS findings_bundle_status ON findings (bundle_id, status)",
    "CREATE INDEX IF NOT EXISTS finding_events_finding ON finding_events (finding_id)",
    "CREATE INDEX IF NOT EXISTS refusals_project ON refusals (project_id, code)",
    "CREATE INDEX IF NOT EXISTS seat_sessions_seat ON seat_sessions (seat_id)",
    "CREATE INDEX IF NOT EXISTS token_counts_bundle ON token_counts (bundle_id)",
)


def require_sqlite(minimum=_MINIMUM):
    found = tuple(sqlite3.sqlite_version_info)
    if found < tuple(minimum):
        raise errors.error_for(
            "E-SQLITE-VERSION", found=".".join(map(str, found)),
            minimum=".".join(map(str, minimum)))


def init_v3(conn):
    """Create the v3 tables and indexes; a second call changes nothing."""
    require_sqlite()
    with conn:
        for statement in _TABLES + _INDEXES:
            conn.execute(statement)
