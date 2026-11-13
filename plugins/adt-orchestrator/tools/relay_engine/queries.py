"""v3 queries and metrics (DD-v3-engine § 7, § 8, § 9, § 11; plan Task 5).

Every check after submit is a query: reads and read state, the bounded
lineage walk, parent candidates, blocking findings, unopened-relay flags,
reference edges both ways, cited-range changes, session and token records,
bundle metrics, loop flags and refusal counts.  Lookups go through the
schema's indexes; no query reads every body.
"""

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Mapping
import difflib
import hashlib
import io
import os
from pathlib import Path
import secrets
import stat
import sqlite3
from typing import Callable, NamedTuple, Sequence

from relay_engine import addresses
from relay_engine import errors
from relay_engine import refs
from relay_engine import schema
from relay_engine import submit
from relay_engine import strings
from relay_engine.submit import OPERATOR_SEAT_ID, new_uuid7, party_sql

UNOPENED_MS = 900_000  # amendment 4 A4.4 (operator P9): 15 minutes from admission
IDLE_MS = 3_600_000  # 60 minutes without activity while holding open must-act work
TOKEN_SOURCES = frozenset({"host-log", "host-reported"})
BLOCKING_STATUSES = ("open", "fixed")


@dataclass(frozen=True)
class RelayRow:
    id: str
    handle: int
    kind: str
    sender_seat_id: str
    parent_id: str | None
    bundle_id: str | None
    created_at: int
    verdict: str | None
    needs: str
    subject: str


@dataclass(frozen=True)
class FindingRow:
    id: str
    handle: int
    severity: str
    status: str
    summary: str
    raised_in: str
    raiser_seat_id: str
    fixed_commit: str | None


@dataclass(frozen=True)
class EdgeRow:
    id: str
    from_relay_id: str
    kind: str
    ordinal: int
    target_type: str
    spelling: str
    target_relay_id: str | None
    target_repository_name: str | None
    target_repository_id: str | None
    target_rev: str | None
    target_commit: str | None
    target_path: str | None
    target_line_start: int | None
    target_line_end: int | None
    digest_at_submit: str | None


@dataclass(frozen=True)
class Flag:
    rule: str  # "unopened", or "idle" naming the seat's newest open must-act relay
    relay_id: str
    handle: int
    seat_id: str
    created_at: int


@dataclass(frozen=True)
class TokenCounts:
    input: int
    output: int
    cache_read: int
    cache_write: int


@dataclass(frozen=True)
class BundleMetrics:
    bytes_by_kind: dict
    rounds: int
    cited_check_share: float | None


@dataclass(frozen=True)
class SnapshotRow:
    handle: int
    relay_id: str
    edge_id: str
    artifact: str
    content_sha256: str
    bytes_len: int
    has_bytes: bool
    locked_by: str | None
    locked_at: int | None


@dataclass(frozen=True)
class ReadResult:
    body: bytes
    envelope: dict
    meta: dict | None
    unopened: list


@dataclass(frozen=True)
class ForeignReadTarget:
    relay_id: str
    body_sha256: str
    source_project_id: str
    destination_project_id: str
    recipient_seat_id: str
    recipient_role: str


ForeignReleaseCheck = Callable[[sqlite3.Connection, ForeignReadTarget], bool] | None


_RELAY_FIELDS = ("id", "handle", "kind", None, "parent_id", "bundle_id", "created_at", "verdict",
                 "needs", "subject")  # None: the sender, read from its arc


def _relay_columns(alias):
    return ", ".join(party_sql(alias + "sender_") if name is None else alias + name
                     for name in _RELAY_FIELDS)

_FINDING_COLUMNS = ("id, handle, severity, status, summary, raised_in, raiser_seat_id,"
                    " fixed_commit")
_EDGE_COLUMNS = ("id, from_relay_id, kind, ordinal, target_type, spelling, target_relay_id,"
                 " target_repository_name, target_repository_id, target_rev, target_commit, target_path,"
                 " target_line_start, target_line_end, digest_at_submit")

# one row per step up parent_id, from the relay itself, at most ? rows
_LINEAGE_SQL = (
    "WITH RECURSIVE walk(id, depth) AS ("
    " SELECT id, 1 FROM relays WHERE id = ?"
    " UNION ALL SELECT r.parent_id, w.depth + 1 FROM walk w JOIN relays r ON r.id = w.id"
    " WHERE r.parent_id IS NOT NULL AND w.depth < ?)"
    " SELECT " + _relay_columns("r.") +
    " FROM walk w JOIN relays r ON r.id = w.id ORDER BY w.depth")
_BLOCKING_SQL = (
    "SELECT " + _FINDING_COLUMNS + " FROM findings WHERE bundle_id = ? AND severity = 'blocking'"
    " AND status IN ('open', 'fixed') ORDER BY handle")


def read(conn, relay_id, seat_id, at_ms, meta=False, *, foreign_release: ForeignReleaseCheck=None):
    """The body and DD § 7's minimal envelope; meta adds digests, edges, read
    state and git state.  Eligibility and a recipient's first-open commit together.

    Platform authenticates the actor before calling in its serialized writer job;
    foreign_release is trusted owner code reading current evidence, not client proof.
    """
    if conn.in_transaction:
        raise ValueError("read helper needs a clean connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _read_in_transaction(conn, relay_id, seat_id, at_ms, meta, foreign_release)
        conn.execute("COMMIT")
        return result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _read_in_transaction(conn, relay_id, seat_id, at_ms, meta, foreign_release):
    row = conn.execute(
        "SELECT r.handle, r.kind, " + party_sql("r.sender_") + ", r.subject, p.handle, r.verdict, r.needs,"
        " r.created_at, b.body, r.body_sha256, r.meta_sha256, r.git_state_id, r.project_id, r.id"
        " FROM relays r JOIN bodies b ON b.relay_id = r.id"
        " LEFT JOIN relays p ON p.id = r.parent_id WHERE r.id = ?", (relay_id,)).fetchone()
    if row is None:
        raise KeyError(relay_id)
    if seat_id != OPERATOR_SEAT_ID:
        reader = conn.execute("SELECT s.id, t.project_id FROM seats s JOIN team_instances t"
                              " ON t.id = s.team_instance_id WHERE s.id = ?",
                              (seat_id,)).fetchone()
        if reader is None:
            raise ValueError("read needs a project seat or the operator")
        if reader[1] != row[12]:
            recipient = conn.execute("SELECT seat_id, role FROM recipients"
                                     " WHERE relay_id = ? AND seat_id = ?",
                                     (relay_id, reader[0])).fetchone()
            if recipient is None or foreign_release is None:
                raise ValueError("foreign read is not released")
            target = ForeignReadTarget(row[13], row[9], row[12], reader[1],
                                       recipient[0], recipient[1])
            _check_foreign_release(conn, target, foreign_release)
    _note_activity(conn, seat_id, at_ms)
    _mark_opened(conn, relay_id, seat_id, at_ms)
    seats = conn.execute("SELECT " + party_sql() + ", role FROM recipients WHERE relay_id = ?"
                         " ORDER BY rowid",
                         (relay_id,)).fetchall()
    envelope = {
        "handle": row[0], "kind": row[1], "sender": row[2],
        "to": [seat for seat, role in seats if role == "to"],
        "cc": [seat for seat, role in seats if role == "cc"],
        "subject": row[3], "parent": row[4], "verdict": row[5], "needs": row[6],
        "created_at": row[7],
    }
    unopened = (unopened_in_lineage(conn, relay_id, seat_id)
                if row[6] == "review" and (seat_id, "to") in seats else [])
    extra = None
    if meta:
        extra = {
            "digests": {"body_sha256": row[9], "meta_sha256": row[10]},
            "edges": points_to(conn, relay_id),
            "read_state": [
                {"seat": seat, "role": role, "delivered_at": delivered, "opened_at": opened,
                 "answered_by": answered}
                for seat, role, delivered, opened, answered in conn.execute(
                    "SELECT " + party_sql() + ", role, delivered_at, opened_at, answered_by"
                    " FROM recipients"
                    " WHERE relay_id = ? ORDER BY rowid", (relay_id,))],
            "git_state": _git_state(conn, row[11]),
        }
    return ReadResult(bytes(row[8]), envelope, extra, unopened)


def _check_foreign_release(conn, target, foreign_release):
    """Use the Engine-owned authorizer slot only for trusted read-only owner code."""
    allowed = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
               sqlite3.SQLITE_RECURSIVE}
    denied = False

    def authorize(action, arg1, arg2, database, trigger):
        nonlocal denied
        if action in allowed:
            return sqlite3.SQLITE_OK
        denied = True
        return sqlite3.SQLITE_DENY

    # Entry/exit reset also invalidates permissions on previously prepared statements.
    conn.set_authorizer(authorize)
    try:
        try:
            released = foreign_release(conn, target)
        except Exception:
            raise ValueError("foreign read is not released") from None
        if denied or not conn.in_transaction or released is not True:
            raise ValueError("foreign read is not released")
    finally:
        conn.set_authorizer(None)


def _git_state(conn, state_id):
    if state_id is None:
        return None
    names = ("repository_id", "head_commit", "branch", "dirty_paths", "porcelain_sha256",
             "measured_at")
    row = conn.execute("SELECT %s FROM git_states WHERE id = ?" % ", ".join(names),
                       (state_id,)).fetchone()
    return dict(zip(names, row))


def lineage(conn, relay_id, max_depth=256):
    return [RelayRow(*row) for row in conn.execute(_LINEAGE_SQL, (relay_id, max_depth))]


def parent_candidates(conn, seat_id, bundle_id=None, kind=None, *, project_id=None):
    """Handles of the relays this seat may name as PARENT (DD E-OP2), in one
    project; the operator's queue spans projects, so it names one (PR-3 R4)."""
    if seat_id == OPERATOR_SEAT_ID and project_id is None:
        raise ValueError("the operator's candidates need a project_id: handles repeat across"
                         " projects")
    sql = ("SELECT handle FROM relays WHERE (" + party_sql("sender_") + " = ? OR id IN"
           " (SELECT relay_id FROM recipients WHERE " + party_sql() + " = ?))")
    params = [seat_id, seat_id]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if bundle_id is not None:
        sql += " AND bundle_id = ?"
        params.append(bundle_id)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    return [handle for (handle,) in conn.execute(sql + " ORDER BY created_at", params)]


def blocking_findings(conn, bundle_id):
    return [FindingRow(*row) for row in conn.execute(_BLOCKING_SQL, (bundle_id,))]


def unopened_in_lineage(conn, relay_id, seat_id):
    """Handles of the other relays up this relay's lineage that were
    addressed to the seat (TO or CC) and that it has not opened."""
    walk = [row.id for row in lineage(conn, relay_id)[1:]]
    if not walk:
        return []
    marks = ",".join("?" * len(walk))
    rows = dict(conn.execute(
        "SELECT r.id, r.handle FROM relays r JOIN recipients c ON c.relay_id = r.id"
        " WHERE " + party_sql("c.") + " = ? AND c.opened_at IS NULL AND r.id IN (%s)" % marks,
        [seat_id, *walk]).fetchall())
    return [rows[rid] for rid in reversed(walk) if rid in rows]


def mark_delivered(conn, relay_id, seat_id, at_ms):
    """The delivery path's mark; a CC row is never delivered (DD § 4)."""
    conn.execute("UPDATE recipients SET delivered_at = ? WHERE relay_id = ? AND " + party_sql()
                 + " = ? AND role = 'to' AND delivered_at IS NULL", (at_ms, relay_id, seat_id))
    conn.commit()


def mark_opened(conn, relay_id, seat_id, at_ms):
    _mark_opened(conn, relay_id, seat_id, at_ms)
    conn.commit()


def _mark_opened(conn, relay_id, seat_id, at_ms):
    conn.execute("UPDATE recipients SET opened_at = ? WHERE relay_id = ? AND " + party_sql()
                 + " = ? AND opened_at IS NULL", (at_ms, relay_id, seat_id))


def note_activity(conn, seat_id, at_ms):
    """A seat's latest engine request; a special (the operator) is not a seat and records none."""
    if seat_id in schema.SPECIALS:
        return
    _note_activity(conn, seat_id, at_ms)
    conn.commit()


def _note_activity(conn, seat_id, at_ms):
    if seat_id in schema.SPECIALS:
        return
    conn.execute("INSERT INTO seat_activity (seat_id, last_request_at) VALUES (?, ?)"
                 " ON CONFLICT (seat_id) DO UPDATE SET last_request_at ="
                 " max(last_request_at, excluded.last_request_at)", (seat_id, at_ms))


def unopened_flags(conn, project_id, now_ms, unopened_ms=UNOPENED_MS, idle_ms=IDLE_MS,
                   *, operator_waiting: frozenset[str] | None = None):
    """P9 liveness flags for the orchestrator (amendment 4 A4.4): a TO addressee
    of a relay with NEEDS other than none that has not opened it unopened_ms
    after admission, delivered or not and active or not ("unopened"); and a
    seat holding open must-act work (act or answer, unanswered) with no
    activity for idle_ms, counted from the later of its last activity and the
    admission of its newest open must-act relay ("idle", naming that relay).
    A seat with an unanswered QUESTION to the operator is never flagged; CC
    and the specials, which are not seats, never flag."""
    if operator_waiting is not None and not isinstance(operator_waiting, frozenset):
        raise ValueError("operator_waiting is a frozenset or None")
    # Both rules start from the open TO rows (recipients_open, a partial index),
    # so their cost follows open work, not the project's history (PR-3 R2);
    # CROSS JOIN keeps that join order.  A QUESTION exempts its own project (R12).
    exempt = ("NOT EXISTS (SELECT 1 FROM relays q JOIN recipients o ON o.relay_id = q.id"
              " WHERE q.sender_seat_id = c.seat_id AND q.kind = 'QUESTION'"
              " AND q.project_id = r.project_id AND o.role = 'to'"
              " AND o.answered_by IS NULL) AND c.special IS NULL")
    if operator_waiting is not None:
        exempt = "c.special IS NULL"
    flags = [Flag("unopened", *row) for row in conn.execute(
        "SELECT r.id, r.handle, " + party_sql("c.") + ", r.created_at FROM recipients c"
        " CROSS JOIN relays r ON r.id = c.relay_id"
        " WHERE r.project_id = ? AND c.role = 'to' AND r.needs != 'none'"
        " AND c.opened_at IS NULL AND c.answered_by IS NULL AND r.created_at <= ? AND "
        + exempt, (project_id, now_ms - unopened_ms))]
    newest = {}  # seat -> (created_at, handle, relay id, last activity) of its newest open must-act
    for relay_id, handle, seat_id, created, active in conn.execute(
            "SELECT r.id, r.handle, " + party_sql("c.") + ", r.created_at, a.last_request_at"
            " FROM recipients c"
            " CROSS JOIN relays r ON r.id = c.relay_id"
            " LEFT JOIN seat_activity a ON a.seat_id = c.seat_id"
            " WHERE r.project_id = ? AND c.role = 'to' AND r.needs IN ('act', 'answer')"
            " AND c.answered_by IS NULL AND " + exempt, (project_id,)):
        newest[seat_id] = max(newest.get(seat_id, (created, handle, relay_id, active)),
                              (created, handle, relay_id, active))
    flags += [Flag("idle", relay_id, handle, seat_id, created)
              for seat_id, (created, handle, relay_id, active) in newest.items()
              if max(created, active or 0) <= now_ms - idle_ms]
    if operator_waiting is not None:
        flags = [flag for flag in flags if flag.seat_id not in operator_waiting]
    return sorted(flags, key=lambda f: (f.created_at, f.seat_id, f.rule))


def points_to(conn, relay_id):
    return [EdgeRow(*row) for row in conn.execute(
        "SELECT " + _EDGE_COLUMNS + " FROM edges WHERE from_relay_id = ?"
        " ORDER BY kind, ordinal", (relay_id,))]


def pointed_from(conn, target, project_id=None):
    """Edges that point at a target, given as a parsed reference.  A Git
    target's repository name maps to J3's repository id (within project_id
    when given), so a rename keeps matching."""
    if target.kind == "relay":
        handle, _, project = target.relay.partition("@")
        if handle.startswith("#") and not project:
            raise ValueError("a bare #<n> names no project; give #<n>@<project> or the id")
        if handle.startswith("#"):
            row = conn.execute("SELECT id FROM relays WHERE project_id = ? AND handle = ?",
                               (project, int(handle[1:]))).fetchone()
            relay_id = None if row is None else row[0]
        else:
            relay_id = handle
        sql, params = "target_relay_id = ?", [relay_id]
    elif target.kind == "git":
        named = "SELECT id FROM repositories WHERE name = ?"
        params = [target.repo]
        if project_id is not None:
            named += " AND project_id = ?"
            params.append(project_id)
        sql = "target_repository_id IN (" + named + ") AND target_path = ?"
        params.append(target.path)
        if target.line_start is not None:
            sql += (" AND (target_line_start IS NULL OR"
                    " (target_line_start <= ? AND target_line_end >= ?))")
            params += [target.line_end, target.line_start]
    else:
        path = target.path
        if path.startswith("~/"):
            from relay_engine import submit
            path = str(Path(submit._home(), path[2:]))
        sql, params = "target_type = 'path' AND target_path = ?", [path]
    return [EdgeRow(*row) for row in conn.execute(
        "SELECT " + _EDGE_COLUMNS + " FROM edges WHERE " + sql +
        " ORDER BY from_relay_id, ordinal", params)]


def cited_range_changed(conn, edge_id, commit, checkout):
    """Whether a Git edge's cited lines differ at commit from the commit it
    cited (the whole file when it cites no range); a file gone or too short
    for the range has changed."""
    row = conn.execute(
        "SELECT target_type, target_repository_name, target_path, target_line_start,"
        " target_line_end, digest_at_submit FROM edges WHERE id = ?", (edge_id,)).fetchone()
    if row is None or row[0] != "git":
        raise ValueError("cited_range_changed takes a Git edge")
    _, repo, path, start, end, cited_blob = row
    spec = refs.RefSpec("git", "%s@%s:%s" % (repo, commit, path), None, repo, commit, path,
                        None, None)
    try:
        now_blob = refs.resolve_git(checkout, spec).blob_sha
    except errors.EngineError as error:
        if error.code in ("E-REF-UNRESOLVED", "E-REF-RANGE"):
            return True
        raise
    if now_blob == cited_blob:
        return False
    if start is None:
        return True
    before = refs.read_blob(checkout, cited_blob).split(b"\n")
    after = refs.read_blob(checkout, now_blob).split(b"\n")
    return _range_touched(before, after, start - 1, end)


def _range_touched(before, after, low, high):
    """Whether the diff from before to after changes lines [low, high) of
    before: a hunk that replaces or removes one of them, or inserts between
    two of them.  The range maps through hunks elsewhere, so a line inserted
    above it moves it but does not change it (PR-3 fold R5)."""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:  # a pure insertion before line i1
            if low < i1 < high:
                return True
        elif i1 < high and i2 > low:
            return True
    return False


def record_token_counts(conn, seat_id, bundle_id, source, counts, *, at_ms):
    """at_ms is the caller's clock, as every other write here takes it (PR-3 R13)."""
    if source not in TOKEN_SOURCES:
        raise ValueError("token source must be one of %s" % ", ".join(sorted(TOKEN_SOURCES)))
    conn.execute(
        "INSERT INTO token_counts (seat_id, bundle_id, source, input, output, cache_read,"
        " cache_write, copied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (seat_id, bundle_id, source, counts.input, counts.output, counts.cache_read,
         counts.cache_write, at_ms))
    conn.commit()


def record_session_step(conn, session_id: str, host: str, seat_id: str, start_ms: int) -> str:
    """Record a session inside the caller's open transaction, without ending it.

    The closing UPDATE takes the write lock before reading the occupant, even
    in a deferred transaction. A refusal rolls back only this step's writes.
    It never begins, commits or rolls back the caller's transaction, and it
    cannot run inside an admission hook, whose authorizer denies SAVEPOINT.
    """
    if host not in schema.HOSTS:
        raise ValueError("unknown host %r" % (host,))
    if not conn.in_transaction:
        raise ValueError("session step needs an open transaction")
    conn.execute("SAVEPOINT engine_record_session")
    try:
        conn.execute("UPDATE seat_sessions SET ended_at = ? WHERE seat_id = ? AND ended_at IS NULL",
                     (start_ms, seat_id))
        occupant = conn.execute("SELECT id FROM seat_occupants WHERE seat_id = ? AND ended_at IS NULL",
                                (seat_id,)).fetchone()
        if occupant is None:
            raise ValueError("seat %s has no open occupant" % seat_id)
        generation = conn.execute("SELECT coalesce(max(generation), 0) + 1 FROM seat_sessions"
                                  " WHERE seat_id = ?", (seat_id,)).fetchone()[0]
        internal_id = new_uuid7(start_ms)
        conn.execute(
            "INSERT INTO seat_sessions (id, seat_id, occupant_id, host, host_session_id,"
            " generation, signed_in_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (internal_id, seat_id, occupant[0], host, session_id, generation, start_ms,
             start_ms))
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO engine_record_session")
            conn.execute("RELEASE engine_record_session")
        raise
    try:
        conn.execute("RELEASE engine_record_session")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO engine_record_session")
            conn.execute("RELEASE engine_record_session")
        raise
    return internal_id


def end_session_step(conn, session_id: str, end_ms: int) -> None:
    """End an internal session in the caller's open transaction; never commit it."""
    if not conn.in_transaction:
        raise ValueError("session step needs an open transaction")
    cursor = conn.execute("UPDATE seat_sessions SET ended_at = ? WHERE id = ?"
                          " AND ended_at IS NULL", (end_ms, session_id))
    if cursor.rowcount != 1:
        raise ValueError("no open session %s" % session_id)


def record_session(conn, session_id, host, seat_id, start_ms):
    """A sign-in (J3 with J-A1): session_id is the host's session id; the
    session takes the seat's open occupant and the next generation of the
    seat, and closes the seat's previous open session, in one transaction."""
    if host not in schema.HOSTS:
        raise ValueError("unknown host %r" % (host,))
    if conn.in_transaction:
        raise ValueError("session helper needs a clean connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        internal_id = record_session_step(conn, session_id, host, seat_id, start_ms)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return internal_id


def end_session(conn, session_id, end_ms):
    """End exactly the open internal session id returned by record_session."""
    if conn.in_transaction:
        raise ValueError("session helper needs a clean connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        end_session_step(conn, session_id, end_ms)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def bundle_metrics(conn, bundle_id):
    """Relay bytes by kind, review rounds (must-revise replies) and the share
    of findings that cite a check (DD § 9)."""
    sizes = dict(conn.execute(
        "SELECT kind, sum(body_bytes + header_bytes) FROM relays WHERE bundle_id = ?"
        " GROUP BY kind", (bundle_id,)).fetchall())
    rounds = conn.execute("SELECT count(*) FROM relays WHERE bundle_id = ?"
                          " AND verdict = 'must-revise'", (bundle_id,)).fetchone()[0]
    total, cited = conn.execute("SELECT count(*), count(check_edge_id) FROM findings"
                                " WHERE bundle_id = ?", (bundle_id,)).fetchone()
    return BundleMetrics(sizes, rounds, None if total == 0 else cited / total)


def loop_flags(conn, bundle_id, checkout):
    """Finding handles whose check or subject path overlaps the paths changed
    by the previous round's fixing commits (DD § 8); this refuses nothing.
    A finding's check path is its check REF's Git path; its subject paths are
    the Git paths its relay's other REF lines name, those no finding of the
    relay cites as its check (PR-3 fold R3)."""
    rounds = [rid for (rid,) in conn.execute(
        "SELECT DISTINCT r.id FROM relays r JOIN findings f ON f.raised_in = r.id"
        " WHERE r.bundle_id = ? ORDER BY r.created_at", (bundle_id,))]
    flagged = []
    for previous, current in zip(rounds, rounds[1:]):
        changed = set()
        for (commit,) in conn.execute("SELECT DISTINCT fixed_commit FROM findings"
                                      " WHERE raised_in = ? AND fixed_commit IS NOT NULL",
                                      (previous,)):
            changed |= refs.changed_paths(checkout, commit)
        subjects = {path for (path,) in conn.execute(
            "SELECT target_path FROM edges WHERE from_relay_id = ? AND kind = 'ref'"
            " AND target_type = 'git' AND id NOT IN (SELECT check_edge_id FROM findings"
            " WHERE raised_in = ? AND check_edge_id IS NOT NULL)", (current, current))}
        for handle, path in conn.execute(
                "SELECT f.handle, e.target_path FROM findings f"
                " LEFT JOIN edges e ON e.id = f.check_edge_id AND e.target_type = 'git'"
                " WHERE f.raised_in = ? ORDER BY f.handle", (current,)):
            if (subjects | ({path} - {None})) & changed:
                flagged.append(handle)
    return flagged


def refusal_counts(conn, project_id):
    return dict(conn.execute("SELECT code, count(*) FROM refusals WHERE project_id = ?"
                             " GROUP BY code ORDER BY code", (project_id,)).fetchall())


# The storage pair's sweep reads (slice 2a D3, D4): bounded, pure, and never reading bodies.
_SWEEP_LIMIT = 500
_HEX = frozenset("0123456789abcdef")


def evidence_pointed(conn, project_id: str, candidates: Sequence[str]) -> frozenset[str]:
    """The candidate digests a snapshot with stored bytes in this project points at.

    At most 500 candidates, each 64 lowercase hexadecimal characters, else
    ValueError before any read; duplicates are answered once. Each distinct
    candidate is one EXISTS probe that stops at its first qualifying row,
    through the partial index snapshots_content_bytes, so digest-only rows
    cost nothing. It runs SELECTs only: no transaction control and no write.
    """
    candidates = list(candidates)
    if len(candidates) > _SWEEP_LIMIT:
        raise ValueError("evidence_pointed takes at most %d candidates" % _SWEEP_LIMIT)
    for digest in candidates:
        if not (isinstance(digest, str) and len(digest) == 64 and set(digest) <= _HEX):
            raise ValueError("each candidate is a 64-character lowercase hexadecimal digest")
    pointed = set()
    for digest in dict.fromkeys(candidates):
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM snapshots s JOIN relays r ON r.id = s.relay_id"
            " WHERE s.content_sha256 = ? AND s.has_bytes = 1 AND r.project_id = ?)",
            (digest, project_id)).fetchone()
        if row[0]:
            pointed.add(digest)
    return frozenset(pointed)


def bundle_closed_at(conn, project_id: str, bundle_ids: Sequence[str]) -> dict[str, int | None]:
    """Each distinct bundle id of this project mapped to its closed_at, None while open.

    At most 500 ids, each a non-empty string, else ValueError before any read;
    an id that names no bundle of this project raises ValueError stating how
    many such ids there were, never the ids. A closed bundle stays closed: the
    bundles_closed_at_fixed trigger refuses clearing or changing a set
    closed_at, so a sweep may rely on an answer it has seen. Any way to reopen
    a bundle is a design change for the storage pair first. It runs SELECTs
    only: no transaction control and no write.
    """
    bundle_ids = list(bundle_ids)
    if len(bundle_ids) > _SWEEP_LIMIT:
        raise ValueError("bundle_closed_at takes at most %d bundle ids" % _SWEEP_LIMIT)
    if any(not isinstance(bundle_id, str) or not bundle_id for bundle_id in bundle_ids):
        raise ValueError("each bundle id is a non-empty string")
    distinct = list(dict.fromkeys(bundle_ids))
    answers = {}
    for bundle_id in distinct:
        row = conn.execute("SELECT closed_at FROM bundles WHERE id = ? AND project_id = ?",
                           (bundle_id, project_id)).fetchone()
        if row is not None:
            answers[bundle_id] = row[0]
    missing = len(distinct) - len(answers)
    if missing == 1:
        raise ValueError("1 bundle id names no bundle of this project")
    if missing:
        raise ValueError("%d bundle ids name no bundle of this project" % missing)
    return answers


_SNAPSHOT_COLUMNS = ("handle, relay_id, edge_id, artifact, content_sha256, bytes_len, has_bytes,"
                     " locked_by, locked_at")
_SNAPSHOT_JOIN_COLUMNS = ", ".join("s." + name.strip() for name in _SNAPSHOT_COLUMNS.split(","))


def _snapshot_row(row):
    return SnapshotRow(*row[:6], bool(row[6]), *row[7:])


def locked_snapshot(conn, artifact, *, project_id):
    """Latest approval lock of the exact artifact in this project."""
    row = conn.execute(
        "SELECT " + _SNAPSHOT_JOIN_COLUMNS + " FROM snapshots s JOIN relays r ON r.id = s.relay_id"
        " WHERE s.artifact = ? AND r.project_id = ? AND s.locked_by IS NOT NULL"
        " ORDER BY s.locked_at DESC, s.handle DESC LIMIT 1", (artifact, project_id)).fetchone()
    return None if row is None else _snapshot_row(row)


def _snapshot(conn, handle, project_id):
    row = conn.execute(
        "SELECT " + _SNAPSHOT_JOIN_COLUMNS + " FROM snapshots s JOIN relays r ON r.id = s.relay_id"
        " WHERE s.handle = ? AND r.project_id = ?", (handle, project_id)).fetchone()
    if row is None:
        raise KeyError(handle)
    return _snapshot_row(row)


_UNAVAILABLE_TEXT = {"missing": "the evidence store reports its bytes missing",
                     "corrupt": "the evidence store reports its bytes corrupt",
                     "unsupported-codec": "the evidence store cannot decode its bytes"}


def _snapshot_bytes(conn, snapshot, store, directory):
    if snapshot.has_bytes:
        if store is None:
            reason = "no evidence store was given to read its bytes"
        else:
            try:
                data = store.get(snapshot.content_sha256)
            except KeyError:
                reason = "the evidence store does not hold its bytes"
            except submit.EvidenceUnavailable as unavailable:
                reason = _UNAVAILABLE_TEXT[unavailable.reason]
            else:
                if hashlib.sha256(data).hexdigest() != snapshot.content_sha256:
                    raise ValueError("stored bytes no longer match snapshot %d" % snapshot.handle)
                return data
        raise errors.error_for("E-SNAPSHOT-NO-BYTES", handle=snapshot.handle, reason=reason)
    edge = conn.execute("SELECT target_type, target_repository_name, digest_at_submit FROM edges"
                        " WHERE id = ?", (snapshot.edge_id,)).fetchone()
    if edge[0] != "git":
        reason = ("the file was over 1 MiB when the request was submitted"
                  if snapshot.bytes_len > submit.SNAPSHOT_BYTES_MAX else
                  "no evidence store was configured when the request was submitted")
    elif directory is None:
        reason = "a Git snapshot is read from its repository, which was not given"
    else:
        checkout = directory.checkout(edge[1])
        if checkout is None:
            reason = ("a Git snapshot's repository has no registered checkout in this project"
                      if directory.has_repository(edge[1]) else
                      "a Git snapshot's repository is not known to this project")
        else:
            try:
                data = refs.read_blob(checkout, edge[2])
            except errors.EngineError as error:
                if error.code != "E-REF-UNRESOLVED":
                    raise
                reason = "the blob cannot be read from its checkout"
            else:
                if hashlib.sha256(data).hexdigest() != snapshot.content_sha256:
                    raise ValueError("blob %s no longer matches snapshot %d" %
                                     (edge[2], snapshot.handle))
                return data
    raise errors.error_for("E-SNAPSHOT-NO-BYTES", handle=snapshot.handle, reason=reason)


def snapshot_diff(conn, a_handle, b_handle, store, directory=None, *, project_id):
    """The unified line difference between two snapshots of one artifact."""
    first, second = _snapshot(conn, a_handle, project_id), _snapshot(conn, b_handle, project_id)
    if first.artifact != second.artifact:
        raise ValueError("snapshots %d and %d are of different artifacts" % (a_handle, b_handle))
    texts = []
    for snapshot in (first, second):
        data = _snapshot_bytes(conn, snapshot, store, directory)
        try:
            texts.append(data.decode("utf-8"))
        except UnicodeDecodeError:
            raise errors.error_for("E-SNAPSHOT-NOT-TEXT", handle=snapshot.handle) from None
    return "".join(difflib.unified_diff(
        texts[0].splitlines(keepends=True), texts[1].splitlines(keepends=True),
        fromfile="snapshot %d" % a_handle, tofile="snapshot %d" % b_handle))


def write_snapshot(conn, handle, out, store, overwrite=False, *, directory=None, project_id):
    """Publish exact bytes through a private file beside the output.

    Creating a copy needs same-directory hard links; a filesystem that refuses
    them raises its OSError after staging cleanup. Overwrite uses os.replace
    and keeps an existing regular file's permission bits, else the umask mode.
    """
    snapshot = _snapshot(conn, handle, project_id)
    data = _snapshot_bytes(conn, snapshot, store, directory)
    mode = None
    if overwrite:
        try:
            observed = os.lstat(out).st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(observed):
                mode = observed
    open_mode = 0o600 if mode is not None else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    for attempt in range(100):
        target = Path(out).parent / (".snapshot-" + secrets.token_hex(8))
        try:
            fd = os.open(target, flags, open_mode)
        except FileExistsError:
            if attempt == 99:
                raise
        else:
            break
    try:
        with io.FileIO(fd, mode="wb", closefd=True) as stream:
            view = memoryview(data)
            while view:
                written = os.write(stream.fileno(), view)
                if written <= 0:
                    raise OSError("snapshot write made no progress")
                view = view[written:]
            if mode is not None:
                os.fchmod(stream.fileno(), stat.S_IMODE(mode) & 0o777)
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(target, out)
        else:
            try:
                os.link(target, out)
            except FileExistsError:
                shown = os.fspath(out)
                raise errors.error_for(
                    "E-OUTPUT-EXISTS",
                    path=shown if strings._is_show_text(shown) and len(shown) <= 300
                    else errors._rejected_identity(shown)) from None
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass


class QuestionRow(NamedTuple):
    relay_id: str
    handle: int
    created_at_ms: int
    sender_seat_id: str | None
    parent_id: str | None
    needs: str
    subject: str
    body: bytes
    answered: bool
    sender_address: str | None


class DoorbellCondition(NamedTuple):
    seat_id: str
    generation: int
    relay_id: str
    handle: int
    created_at_ms: int
    seat_address: str | None


def operator_questions(conn, project_id: str, after_handle: int, limit: int) -> list[QuestionRow]:
    """Page questions and their answer/address fields in the caller's one snapshot.

    An operator child answers regardless of kind or recipient marks. A closed
    or special sender has no routing address; rendering corrupt stored names
    is allowed to fail. This read changes no state or transaction boundary.
    """
    if type(after_handle) is not int or after_handle < 0:
        raise ValueError("after_handle is an integer of at least zero")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit is an integer from 1 to 500")
    rows = conn.execute(
        "SELECT r.id, r.handle, r.created_at, r.sender_seat_id, r.parent_id, r.needs,"
        " r.subject, b.body, EXISTS (SELECT 1 FROM relays child"
        " WHERE child.parent_id = r.id AND child.project_id = r.project_id"
        " AND child.sender_special = 'operator'),"
        " ti.name, r.sender_role, ti.closed_at, r.sender_special"
        " FROM relays r JOIN bodies b ON b.relay_id = r.id"
        " LEFT JOIN team_instances ti ON ti.id = r.team_instance_id"
        " WHERE r.project_id = ? AND r.kind = 'QUESTION' AND r.handle > ?"
        " ORDER BY r.handle LIMIT ?", (project_id, after_handle, limit)).fetchall()
    page = []
    for row in rows:
        name, role, closed_at, special = row[9:]
        address = (addresses.render(addresses.Address(team=name, role=role))
                   if special is None and name is not None and role is not None
                   and closed_at is None else None)
        page.append(QuestionRow(*row[:8], bool(row[8]), address))
    return page


def operator_admission(conn, submission_key: str, data: bytes) -> submit.Admitted | None:
    """Read the operator party's replay receipt for the exact persisted draft bytes.

    The party/key is store-wide, as in submit replay; team and recipient
    lifecycle state cannot change an already admitted receipt.
    """
    if (not isinstance(submission_key, str) or not submission_key
            or "\x00" in submission_key):
        raise ValueError("submission_key is a non-empty UTF-8 string without NUL")
    try:
        key_bytes = submission_key.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("submission_key must be UTF-8 encodable") from None
    if len(key_bytes) > 256:
        raise ValueError("submission_key is at most 256 UTF-8 bytes")
    if not isinstance(data, bytes):
        raise ValueError("data must be bytes")
    row = conn.execute(
        "SELECT id, handle, created_at, draft_sha256 FROM relays WHERE "
        + party_sql("sender_") + " = ? AND submission_key = ?",
        (OPERATOR_SEAT_ID, submission_key)).fetchone()
    if row is None:
        return None
    if row[3] != hashlib.sha256(data).hexdigest():
        raise errors.error_for("E-SUBMISSION-KEY-REUSED")
    return submit.Admitted(row[0], row[1], row[2])


def doorbell_off(conn, project_id: str, now_ms: int, threshold_ms: int) -> list[DoorbellCondition]:
    """Oldest unopened, undelivered TO arrival for each seat with an open session.

    Age is admission age, independent of sign-in. The partial recipient
    index excludes delivered and opened history before the joins. Addresses
    are display-only and disappear when the seat's own team closes.
    """
    if type(threshold_ms) is not int or threshold_ms <= 0:
        raise ValueError("threshold_ms is a positive integer")
    rows = conn.execute(
        "SELECT c.seat_id, ss.generation, r.id, r.handle, r.created_at,"
        " ti.name, s.role, ti.closed_at"
        " FROM seat_sessions ss CROSS JOIN recipients c ON c.seat_id = ss.seat_id"
        " CROSS JOIN relays r ON r.id = c.relay_id"
        " JOIN seats s ON s.id = c.seat_id"
        " JOIN team_instances ti ON ti.id = s.team_instance_id"
        " WHERE ss.ended_at IS NULL AND ss.generation = (SELECT max(latest.generation)"
        " FROM seat_sessions latest WHERE latest.seat_id = ss.seat_id AND latest.ended_at IS NULL)"
        " AND c.role = 'to' AND c.delivered_at IS NULL AND c.opened_at IS NULL"
        " AND r.project_id = ? ORDER BY c.seat_id, r.created_at, r.handle", (project_id,)).fetchall()
    conditions = []
    seen = set()
    for row in rows:
        seat_id = row[0]
        if seat_id in seen:
            continue
        seen.add(seat_id)
        if now_ms - row[4] < threshold_ms:
            continue
        address = (addresses.render(addresses.Address(team=row[5], role=row[6]))
                   if row[7] is None else None)
        conditions.append(DoorbellCondition(*row[:5], address))
    return conditions


@dataclass(frozen=True)
class SettingSpec:
    value_type: type
    default: bool | int
    minimum: int | None = None
    maximum: int | None = None


SETTINGS: Mapping[str, SettingSpec] = MappingProxyType({
    "jev_record": SettingSpec(bool, False),
    "jev_act": SettingSpec(bool, False),
    "doorbell_off_minutes": SettingSpec(int, 15, 1, 1440),
})


class ProjectSettings(NamedTuple):
    values: dict[str, bool | int]
    revision: int


class SettingChange(NamedTuple):
    revision: int
    key: str
    old: bool | int | None
    new: bool | int
    by_principal: str
    at_ms: int


def _settings_project(conn, project_id):
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise ValueError("unknown project")


def project_settings(conn, project_id: str) -> ProjectSettings:
    """Read every registered setting and the project's current history revision."""
    _settings_project(conn, project_id)
    values = {key: spec.default for key, spec in SETTINGS.items()}
    values.update((key, json.loads(value)) for key, value in conn.execute(
        "SELECT key, value FROM project_settings WHERE project_id = ?", (project_id,)))
    revision = conn.execute("SELECT coalesce(max(revision), 0) FROM settings_history"
                            " WHERE project_id = ?", (project_id,)).fetchone()[0]
    return ProjectSettings(values, revision)


def settings_history(conn, project_id: str, limit: int) -> list[SettingChange]:
    """Read the newest setting changes without changing the caller's transaction."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    _settings_project(conn, project_id)
    return [SettingChange(revision, key, None if old is None else json.loads(old),
                          json.loads(new), by, at)
            for revision, key, old, new, by, at in conn.execute(
                "SELECT revision, key, old, new, by_principal, at FROM settings_history"
                " WHERE project_id = ? ORDER BY revision DESC LIMIT ?", (project_id, limit))]


def set_project_setting_step(conn, project_id: str, key: str, value: bool | int,
                             *, principal: str, now_ms: int) -> int:
    """Apply one operator change inside the caller's transaction, without committing."""
    if not conn.in_transaction:
        raise ValueError("setting step requires a caller transaction")
    _settings_project(conn, project_id)
    if principal != "operator":
        raise errors.error_for("E-SETTING-OPERATOR-ONLY")
    if not isinstance(key, str) or key not in SETTINGS:
        raise errors.error_for("E-SETTING-UNKNOWN")
    spec = SETTINGS[key]
    if (type(value) is not spec.value_type or
            (spec.minimum is not None and not spec.minimum <= value <= spec.maximum)):
        raise errors.error_for("E-SETTING-VALUE")
    current = project_settings(conn, project_id)
    if ((key == "jev_act" and value is True and not current.values["jev_record"]) or
            (key == "jev_record" and value is False and current.values["jev_act"])):
        raise errors.error_for("E-JEV-ACT-NEEDS-RECORD")
    if value == current.values[key]:
        return current.revision
    stored = conn.execute("SELECT value FROM project_settings WHERE project_id = ? AND key = ?",
                          (project_id, key)).fetchone()
    old = None if stored is None else stored[0]
    new = json.dumps(value, separators=(",", ":"))
    revision = current.revision + 1
    conn.execute("INSERT INTO project_settings (project_id, key, value) VALUES (?, ?, ?)"
                 " ON CONFLICT(project_id, key) DO UPDATE SET value = excluded.value",
                 (project_id, key, new))
    conn.execute("INSERT INTO settings_history"
                 " (project_id, revision, key, old, new, by_principal, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (project_id, revision, key, old, new, principal, now_ms))
    return revision
