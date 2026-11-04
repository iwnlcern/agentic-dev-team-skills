"""v3 queries and metrics (DD-v3-engine § 7, § 8, § 9, § 11; plan Task 5).

Every check after submit is a query: reads and read state, the bounded
lineage walk, parent candidates, blocking findings, unopened-relay flags,
reference edges both ways, cited-range changes, session and token records,
bundle metrics, loop flags and refusal counts.  Lookups go through the
schema's indexes; no query reads every body.
"""

from dataclasses import dataclass
from pathlib import Path

from relay_engine import errors
from relay_engine import refs
from relay_engine.submit import OPERATOR_SEAT_ID

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
class ReadResult:
    body: bytes
    envelope: dict
    meta: dict | None
    unopened: list


_RELAY_COLUMNS = ("id, handle, kind, sender_seat_id, parent_id, bundle_id, created_at, verdict,"
                  " needs, subject")
_FINDING_COLUMNS = ("id, handle, severity, status, summary, raised_in, raiser_seat_id,"
                    " fixed_commit")
_EDGE_COLUMNS = ("id, from_relay_id, kind, ordinal, target_type, spelling, target_relay_id,"
                 " target_repository_id, target_rev, target_commit, target_path,"
                 " target_line_start, target_line_end, digest_at_submit")

# one row per step up parent_id, from the relay itself, at most ? rows
_LINEAGE_SQL = (
    "WITH RECURSIVE walk(id, depth) AS ("
    " SELECT id, 1 FROM relays WHERE id = ?"
    " UNION ALL SELECT r.parent_id, w.depth + 1 FROM walk w JOIN relays r ON r.id = w.id"
    " WHERE r.parent_id IS NOT NULL AND w.depth < ?)"
    " SELECT " + ", ".join("r." + c.strip() for c in _RELAY_COLUMNS.split(",")) +
    " FROM walk w JOIN relays r ON r.id = w.id ORDER BY w.depth")
_BLOCKING_SQL = (
    "SELECT " + _FINDING_COLUMNS + " FROM findings WHERE bundle_id = ? AND severity = 'blocking'"
    " AND status IN ('open', 'fixed') ORDER BY handle")


def read(conn, relay_id, seat_id, at_ms, meta=False):
    """The body and DD § 7's minimal envelope; meta adds digests, edges, read
    state and git state.  A recipient's first read sets its opened_at."""
    row = conn.execute(
        "SELECT r.handle, r.kind, r.sender_seat_id, r.subject, p.handle, r.verdict, r.needs,"
        " r.created_at, b.body, r.body_sha256, r.meta_sha256, r.git_state_id"
        " FROM relays r JOIN bodies b ON b.relay_id = r.id"
        " LEFT JOIN relays p ON p.id = r.parent_id WHERE r.id = ?", (relay_id,)).fetchone()
    if row is None:
        raise KeyError(relay_id)
    note_activity(conn, seat_id, at_ms)
    mark_opened(conn, relay_id, seat_id, at_ms)
    seats = conn.execute("SELECT seat_id, role FROM recipients WHERE relay_id = ? ORDER BY rowid",
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
                    "SELECT seat_id, role, delivered_at, opened_at, answered_by FROM recipients"
                    " WHERE relay_id = ? ORDER BY rowid", (relay_id,))],
            "git_state": _git_state(conn, row[11]),
        }
    conn.commit()
    return ReadResult(bytes(row[8]), envelope, extra, unopened)


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


def parent_candidates(conn, seat_id, bundle_id=None, kind=None):
    """Handles of the relays this seat may name as PARENT (DD E-OP2)."""
    sql = ("SELECT handle FROM relays WHERE (sender_seat_id = ? OR id IN"
           " (SELECT relay_id FROM recipients WHERE seat_id = ?))")
    params = [seat_id, seat_id]
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
        " WHERE c.seat_id = ? AND c.opened_at IS NULL AND r.id IN (%s)" % marks,
        [seat_id, *walk]).fetchall())
    return [rows[rid] for rid in reversed(walk) if rid in rows]


def mark_delivered(conn, relay_id, seat_id, at_ms):
    """The delivery path's mark; a CC row is never delivered (DD § 4)."""
    conn.execute("UPDATE recipients SET delivered_at = ? WHERE relay_id = ? AND seat_id = ?"
                 " AND role = 'to' AND delivered_at IS NULL", (at_ms, relay_id, seat_id))
    conn.commit()


def mark_opened(conn, relay_id, seat_id, at_ms):
    conn.execute("UPDATE recipients SET opened_at = ? WHERE relay_id = ? AND seat_id = ?"
                 " AND opened_at IS NULL", (at_ms, relay_id, seat_id))
    conn.commit()


def note_activity(conn, seat_id, at_ms):
    conn.execute("INSERT INTO seat_activity (seat_id, last_request_at) VALUES (?, ?)"
                 " ON CONFLICT (seat_id) DO UPDATE SET last_request_at ="
                 " max(last_request_at, excluded.last_request_at)", (seat_id, at_ms))
    conn.commit()


def unopened_flags(conn, project_id, now_ms, unopened_ms=UNOPENED_MS, idle_ms=IDLE_MS):
    """P9 liveness flags for the orchestrator (amendment 4 A4.4): a TO addressee
    of a relay with NEEDS other than none that has not opened it unopened_ms
    after admission, delivered or not and active or not ("unopened"); and a
    seat holding open must-act work (act or answer, unanswered) with no
    activity for idle_ms, counted from the later of its last activity and the
    admission of its newest open must-act relay ("idle", naming that relay).
    A seat with an unanswered QUESTION to the operator is never flagged; CC
    and the operator, who is not a seat, never flag."""
    exempt = ("NOT EXISTS (SELECT 1 FROM relays q JOIN recipients o ON o.relay_id = q.id"
              " WHERE q.kind = 'QUESTION' AND q.sender_seat_id = c.seat_id AND o.role = 'to'"
              " AND o.answered_by IS NULL) AND c.seat_id != ?")
    flags = [Flag("unopened", *row) for row in conn.execute(
        "SELECT r.id, r.handle, c.seat_id, r.created_at FROM recipients c"
        " JOIN relays r ON r.id = c.relay_id"
        " WHERE r.project_id = ? AND c.role = 'to' AND r.needs != 'none'"
        " AND c.opened_at IS NULL AND c.answered_by IS NULL AND r.created_at <= ? AND "
        + exempt, (project_id, now_ms - unopened_ms, OPERATOR_SEAT_ID))]
    newest = {}  # seat -> (created_at, handle, relay id, last activity) of its newest open must-act
    for relay_id, handle, seat_id, created, active in conn.execute(
            "SELECT r.id, r.handle, c.seat_id, r.created_at, a.last_request_at FROM recipients c"
            " JOIN relays r ON r.id = c.relay_id"
            " LEFT JOIN seat_activity a ON a.seat_id = c.seat_id"
            " WHERE r.project_id = ? AND c.role = 'to' AND r.needs IN ('act', 'answer')"
            " AND c.answered_by IS NULL AND " + exempt, (project_id, OPERATOR_SEAT_ID)):
        newest[seat_id] = max(newest.get(seat_id, (created, handle, relay_id, active)),
                              (created, handle, relay_id, active))
    flags += [Flag("idle", relay_id, handle, seat_id, created)
              for seat_id, (created, handle, relay_id, active) in newest.items()
              if max(created, active or 0) <= now_ms - idle_ms]
    return sorted(flags, key=lambda f: (f.created_at, f.seat_id, f.rule))


def points_to(conn, relay_id):
    return [EdgeRow(*row) for row in conn.execute(
        "SELECT " + _EDGE_COLUMNS + " FROM edges WHERE from_relay_id = ?"
        " ORDER BY kind, ordinal", (relay_id,))]


def pointed_from(conn, target):
    """Edges that point at a target, given as a parsed reference."""
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
        sql, params = "target_repository_id = ? AND target_path = ?", [target.repo, target.path]
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
        "SELECT target_type, target_repository_id, target_path, target_line_start,"
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
    if len(after) < end:
        return True
    return before[start - 1:end] != after[start - 1:end]


def record_token_counts(conn, seat_id, bundle_id, source, counts, at_ms=None):
    if source not in TOKEN_SOURCES:
        raise ValueError("token source must be one of %s" % ", ".join(sorted(TOKEN_SOURCES)))
    if at_ms is None:
        import time
        at_ms = time.time_ns() // 1_000_000
    conn.execute(
        "INSERT INTO token_counts (seat_id, bundle_id, source, input, output, cache_read,"
        " cache_write, copied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (seat_id, bundle_id, source, counts.input, counts.output, counts.cache_read,
         counts.cache_write, at_ms))
    conn.commit()


def record_session(conn, session_id, host, seat_id, start_ms):
    conn.execute("INSERT INTO seat_sessions (session_id, host, seat_id, start_at)"
                 " VALUES (?, ?, ?, ?)", (session_id, host, seat_id, start_ms))
    conn.commit()


def end_session(conn, session_id, end_ms):
    cursor = conn.execute("UPDATE seat_sessions SET end_at = ? WHERE session_id = ?"
                          " AND end_at IS NULL", (end_ms, session_id))
    if cursor.rowcount != 1:
        conn.rollback()
        raise ValueError("no open session %s" % session_id)
    conn.commit()


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
    """Finding handles whose check path overlaps the paths changed by the
    previous round's fixing commits (DD § 8); this refuses nothing."""
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
        for handle, path in conn.execute(
                "SELECT f.handle, e.target_path FROM findings f"
                " LEFT JOIN edges e ON e.id = f.check_edge_id AND e.target_type = 'git'"
                " WHERE f.raised_in = ? ORDER BY f.handle", (current,)):
            if path is not None and path in changed:
                flagged.append(handle)
    return flagged


def refusal_counts(conn, project_id):
    return dict(conn.execute("SELECT code, count(*) FROM refusals WHERE project_id = ?"
                             " GROUP BY code ORDER BY code", (project_id,)).fetchall())
