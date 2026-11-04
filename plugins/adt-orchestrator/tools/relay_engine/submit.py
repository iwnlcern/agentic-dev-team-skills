"""v3 submit pipeline (DD-v3-engine § 4, § 5, § 6, § 8, E-OP2; plan Task 4, erratum 1).

Order: size and header (``header.parse``) -> addresses through the
``Directory`` -> ``PARENT`` membership -> references resolved and digested,
all before the write lock (relays are append-only, so nothing resolved here
can vanish) -> then one write transaction: ``FIXES`` and ``RESOLVE`` rules in
header line order -> id, handle and time -> bundle (roots open or join one,
children inherit) -> rows, with the sender's activity.
A refused submit writes exactly one ``refusals`` row and nothing else; an
engine fault writes nothing.  No rule reads the body.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import pwd
import re
import secrets
import sqlite3
from typing import Callable, Protocol

from relay_engine import errors
from relay_engine import header
from relay_engine import jcs
from relay_engine import refs
from relay_engine import schema

SYSTEM = "system"  # the reserved sender of engine-written relays
ORCHESTRATOR_ROLE = "orchestrator-planner"
OPERATOR_SEAT_ID = "operator"  # J2's machine-wide special: one queue, no team seat


@dataclass(frozen=True)
class SeatRef:
    seat_id: str
    address: str
    role: str
    team_instance_id: str
    project_id: str


class Directory(Protocol):
    def resolve(self, address: str, sender: SeatRef) -> SeatRef:
        """The seat an address names for this sender; raise
        header.AddressRefused with a reason when there is none."""

    def checkout(self, repo: str) -> Path | None:
        """The checkout a Git reference's <repo> names, or None."""


@dataclass(frozen=True)
class GitState:
    repository_id: str | None
    head_commit: str | None
    branch: str | None
    dirty_paths: int
    porcelain_sha256: str


_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _check_git_state(state):
    """A caller error is a ValueError here, never a raw IntegrityError at the
    write (PR-3 fold S6, as O5 rules for other caller errors)."""
    if state is None:
        return
    if not isinstance(state, GitState):
        raise ValueError("git_state is a GitState or None")
    if not (isinstance(state.porcelain_sha256, str) and _SHA256.fullmatch(state.porcelain_sha256)):
        raise ValueError("git_state.porcelain_sha256 is 64 lowercase hex digits")
    count = state.dirty_paths
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("git_state.dirty_paths is a count")
    if state.repository_id is None:
        if (state.head_commit, state.branch, count) != (None, None, 0):
            raise ValueError("a git_state with no repository has no head, branch or dirty paths")
        return
    if not (isinstance(state.repository_id, str) and state.repository_id):
        raise ValueError("git_state.repository_id is a non-empty string or None")
    if state.head_commit is not None and not (isinstance(state.head_commit, str)
                                              and _OBJECT_ID.fullmatch(state.head_commit)):
        raise ValueError("git_state.head_commit is a full lowercase object id or None")
    if state.branch is not None and not (isinstance(state.branch, str) and state.branch):
        raise ValueError("git_state.branch is a non-empty string or None")


@dataclass(frozen=True)
class SubmitContext:
    sender: SeatRef
    git_state: GitState | None
    now_ms: Callable[[], int]
    directory: Directory

    def __post_init__(self):
        _check_git_state(self.git_state)


@dataclass(frozen=True)
class Admitted:
    relay_id: str
    handle: int
    created_at_ms: int


def open_store(path):
    """A store connection with foreign keys on (O5) and the v3 schema."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        schema.init_v3(conn)
    except BaseException:
        conn.close()
        raise
    return conn


_COUNTER_BITS = 74  # rand_a (12 bits) and rand_b (62 bits) as one counter


def new_uuid7(now_ms, last=None):
    """A UUIDv7 for now_ms, strictly greater than last when last is given:
    within or behind last's millisecond the counter steps on from last's."""
    if not isinstance(now_ms, int) or not 0 <= now_ms < 1 << 48:
        raise ValueError("now_ms out of range")
    counter = secrets.randbits(_COUNTER_BITS)
    if last is not None:
        value = int(last.replace("-", ""), 16)
        last_ms = value >> 80
        last_counter = ((value >> 64) & 0xFFF) << 62 | (value & ((1 << 62) - 1))
        if now_ms <= last_ms:
            now_ms, counter = last_ms, last_counter + 1
            if counter >> _COUNTER_BITS:
                now_ms, counter = last_ms + 1, 0
    value = ((now_ms << 80) | (0x7 << 76) | ((counter >> 62) << 64) | (0b10 << 62)
             | (counter & ((1 << 62) - 1)))
    text = "%032x" % value
    return "-".join((text[:8], text[8:12], text[12:16], text[16:20], text[20:]))


def _home():
    """The engine user's home from the password database, not $HOME (R8)."""
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


class _Lines:
    """Header line numbers by key, in order, for refusals the parser cannot place."""

    def __init__(self, data, header_bytes):
        self.by_key = {}
        text = data[:header_bytes].decode("utf-8")
        for number, line in enumerate(text.split("\n"), start=1):
            if line:
                self.by_key.setdefault(line.split(":", 1)[0], []).append(number)

    def of(self, key, index=0):
        numbers = self.by_key.get(key, ())
        return numbers[index] if index < len(numbers) else None


def _at(line, error):
    """Attach the header line to a refusal whose code carries none."""
    if "line" not in error.params and line is not None:
        error.refusal_line = line
    return error


def submit(conn, ctx, data):
    """Admit one draft, or record one refusal and raise it."""
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise ValueError("the store connection has foreign keys off; open it with open_store")
    now = ctx.now_ms()
    try:
        return _admit(conn, ctx, data, now)
    except errors.EngineError as error:
        _record_refusal(conn, ctx, data, now, error)
        raise


def _record_refusal(conn, ctx, data, now, error):
    line = error.params.get("line", getattr(error, "refusal_line", None))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO refusals (project_id, seat_id, code, line, at, draft_sha256)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.sender.project_id, ctx.sender.seat_id, error.code,
             line if isinstance(line, int) else None, now,
             hashlib.sha256(data).hexdigest()))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _admit(conn, ctx, data, now):
    draft = header.parse(data)
    lines = _Lines(data, draft.header_bytes)
    sender = ctx.sender
    # Before the write lock: a slow checkout or a large file digest holds no
    # lock (PR-3 fold S2); relays and recipients rows are append-only.
    recipients = _recipients(ctx, draft, lines)
    parent = _parent(conn, sender, draft, lines)
    targets = _references(conn, ctx, draft, lines)
    transitions = _transitions(draft, lines)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _check_findings(conn, sender, transitions)
        handle, created, relay_id = _identity(conn, sender.project_id, now)
        bundle_id = (parent[1] if parent is not None
                     else _bundle(conn, sender, draft.bundle, created))
        git_state_id = _write_git_state(conn, ctx.git_state, created)
        conn.execute(
            "INSERT INTO relays (id, handle, project_id, team_instance_id, bundle_id, kind,"
            " verdict, needs, subject, parent_id, sender_seat_id, sender_role, created_at,"
            " body_sha256, meta_sha256, body_bytes, header_bytes, git_state_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (relay_id, handle, sender.project_id, sender.team_instance_id, bundle_id,
             draft.kind, draft.verdict, draft.needs, draft.subject,
             None if parent is None else parent[0], sender.seat_id, sender.role, created,
             hashlib.sha256(draft.body).hexdigest(),
             meta_digest(draft, None if parent is None else parent[0], targets), len(draft.body),
             draft.header_bytes, git_state_id))
        conn.execute("INSERT INTO bodies (relay_id, body) VALUES (?, ?)",
                     (relay_id, draft.body))
        conn.executemany(
            "INSERT INTO recipients (relay_id, seat_id, role) VALUES (?, ?, ?)",
            [(relay_id, seat_id, role) for seat_id, role in recipients])
        if parent is not None:
            conn.execute(
                "INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                " target_relay_id) VALUES (?, ?, 1, 'parent', 'relay', ?, ?)",
                (new_uuid7(created), relay_id, draft.parent, parent[0]))
        edge_ids = _write_references(conn, relay_id, created, targets)
        _write_findings(conn, sender, draft, relay_id, bundle_id, edge_ids, created)
        _write_transitions(conn, sender, transitions, relay_id, created)
        if parent is not None:
            _write_answered_by(conn, parent[0], sender.seat_id, relay_id)
        # an admission is the sender's activity (PR-3 fold R6), committed with it
        conn.execute("INSERT INTO seat_activity (seat_id, last_request_at) VALUES (?, ?)"
                     " ON CONFLICT (seat_id) DO UPDATE SET last_request_at ="
                     " max(last_request_at, excluded.last_request_at)",
                     (sender.seat_id, created))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return Admitted(relay_id, handle, created)


_DIGEST_TARGET = {"relay": ("target_relay_id",), "path": ("target_path",),
                  "git": ("target_repository_id", "target_commit", "target_path",
                          "target_line_start", "target_line_end")}


def meta_digest(draft, parent_id=None, targets=()):
    """sha256 of the parsed fields in JCS form (DD § 5, erratum E1): list
    whitespace is gone after parsing, and addresses are never case-folded.
    PARENT and each REF enter as their resolved targets, so equivalent
    spellings converge (PR-3 fold S10): #<n>, #<n>@<project> and the UUID of
    one relay; ~/ and the path it expands to; a branch and its commit."""
    fields = {
        "kind": draft.kind, "to": list(draft.to), "cc": list(draft.cc),
        "parent": parent_id, "bundle": draft.bundle, "verdict": draft.verdict,
        "needs": draft.needs, "subject": draft.subject,
        "refs": [{"type": target["target_type"],
                  **{column: target.get(column) for column in _DIGEST_TARGET[target["target_type"]]}}
                 for _, target in targets],
        "findings": [{"severity": f.severity, "check": f.check_index, "summary": f.summary}
                     for f in draft.findings],
        "fixes": [list(fix) for fix in draft.fixes],
        "resolves": [list(resolve) for resolve in draft.resolves],
    }
    return hashlib.sha256(jcs.jcs_encode(fields)).hexdigest()


def _recipients(ctx, draft, lines):
    result, seats = [], set()
    for field, role, addresses in (("TO", "to", draft.to), ("CC", "cc", draft.cc)):
        for address in addresses:
            try:
                seat = ctx.directory.resolve(address, ctx.sender)
            except header.AddressRefused as refused:
                raise errors.error_for(
                    "E-ADDRESS", line=lines.of(field), field=field,
                    value=header._display(address), reason=refused.reason) from None
            if seat.seat_id in seats:
                raise errors.error_for(
                    "E-ADDRESS", line=lines.of(field), field=field,
                    value=header._display(address),
                    reason="names the same seat as an earlier address")
            seats.add(seat.seat_id)
            result.append((seat.seat_id, role))
    return result


def _find_relay(conn, project_id, spelling):
    """(id, bundle_id) of a relay of the project by #<n> or UUID, or None."""
    if spelling.startswith("#"):
        row = conn.execute("SELECT id, bundle_id FROM relays WHERE project_id = ? AND handle = ?",
                           (project_id, int(spelling[1:]))).fetchone()
    else:
        row = conn.execute("SELECT id, bundle_id FROM relays WHERE project_id = ? AND id = ?",
                           (project_id, spelling)).fetchone()
    return row


def _parent(conn, sender, draft, lines):
    if draft.parent is None:
        return None
    line = lines.of("PARENT")
    row = _find_relay(conn, sender.project_id, draft.parent)
    if row is None:
        raise errors.error_for("E-PARENT-UNKNOWN", line=line,
                               value=header._display(draft.parent))
    member = conn.execute(
        "SELECT 1 FROM relays WHERE id = ? AND sender_seat_id = ?"
        " UNION ALL SELECT 1 FROM recipients WHERE relay_id = ? AND seat_id = ?",
        (row[0], sender.seat_id, row[0], sender.seat_id)).fetchone()
    if member is None:
        raise errors.error_for("E-PARENT-NOT-MEMBER", line=line,
                               value=header._display(draft.parent))
    return row


def _bundle(conn, sender, name, created):
    """A root opens the named bundle of its project, or joins it (A2)."""
    row = conn.execute("SELECT id FROM bundles WHERE project_id = ? AND name = ?",
                       (sender.project_id, name)).fetchone()
    if row is not None:
        return row[0]
    bundle_id = new_uuid7(created)
    conn.execute("INSERT INTO bundles (id, project_id, team_instance_id, name, opened_by)"
                 " VALUES (?, ?, ?, ?, ?)",
                 (bundle_id, sender.project_id, sender.team_instance_id, name, sender.seat_id))
    return bundle_id


def _references(conn, ctx, draft, lines):
    """Each REF resolved and digested, before anything is written."""
    targets = []
    for index, text in enumerate(draft.refs):
        line = lines.of("REF", index)
        try:
            targets.append((text, _resolve(conn, ctx, refs.parse_ref(text))))
        except errors.EngineError as error:
            raise _at(line, error)
    return targets


def _resolve(conn, ctx, spec):
    if spec.kind == "relay":
        handle, _, project = spec.relay.partition("@")
        row = _find_relay(conn, project or ctx.sender.project_id, handle)
        if row is None:
            raise errors.error_for("E-REF-UNRESOLVED", value=header._display(spec.text),
                                   reason="no relay %s in project %s" % (
                                       header._display(handle, 40),
                                       header._display(project or ctx.sender.project_id, 40)))
        return {"target_type": "relay", "target_relay_id": row[0]}
    if spec.kind == "path":
        path = spec.path
        if path.startswith("~/"):
            try:
                home = _home()
            except KeyError:  # a uid with no password entry is a real host (PR-3 fold Q1)
                raise errors.error_for(
                    "E-REF-UNRESOLVED", value=header._display(spec.text),
                    reason="this user has no home directory in the password database") from None
            path = os.path.join(home, path[2:])
        return {"target_type": "path", "target_path": path,
                "digest_at_submit": refs.file_digest(Path(path))}
    checkout = ctx.directory.checkout(spec.repo)
    if checkout is None:
        raise errors.error_for("E-REF-UNRESOLVED", value=header._display(spec.text),
                               reason="repository %s is not known to this project"
                               % header._display(spec.repo, 40))
    resolved = refs.resolve_git(checkout, spec)
    return {"target_type": "git", "target_repository_id": spec.repo, "target_rev": spec.rev,
            "target_commit": resolved.commit, "target_path": spec.path,
            "target_line_start": spec.line_start, "target_line_end": spec.line_end,
            "digest_at_submit": resolved.blob_sha}


_EDGE_COLUMNS = ("target_type", "target_relay_id", "target_repository_id", "target_rev",
                 "target_commit", "target_path", "target_line_start", "target_line_end",
                 "digest_at_submit")


def _write_references(conn, relay_id, created, targets):
    edge_ids = []
    for ordinal, (spelling, target) in enumerate(targets, start=1):
        edge_id = new_uuid7(created)
        conn.execute(
            "INSERT INTO edges (id, from_relay_id, ordinal, kind, spelling, %s)"
            " VALUES (?, ?, ?, 'ref', ?, %s)" % (", ".join(_EDGE_COLUMNS),
                                                 ", ".join("?" * len(_EDGE_COLUMNS))),
            (edge_id, relay_id, ordinal, spelling,
             *[target.get(column) for column in _EDGE_COLUMNS]))
        edge_ids.append(edge_id)
    return edge_ids


def _identity(conn, project_id, now):
    """Handle max+1, time strictly after the project's last, id after its last.
    One aggregate per statement, so each reads one end of its index (S7)."""
    last_handle = conn.execute("SELECT max(handle) FROM relays WHERE project_id = ?",
                               (project_id,)).fetchone()[0]
    last_created = conn.execute("SELECT max(created_at) FROM relays WHERE project_id = ?",
                                (project_id,)).fetchone()[0]
    created = now if last_created is None else max(now, last_created + 1)
    last_id = conn.execute("SELECT max(id) FROM relays WHERE project_id = ?",
                           (project_id,)).fetchone()[0]
    return (last_handle or 0) + 1, created, new_uuid7(created, last_id)


def _write_git_state(conn, state, created):
    if state is None or state.repository_id is None:
        return None
    state_id = new_uuid7(created)
    conn.execute(
        "INSERT INTO git_states (id, repository_id, head_commit, branch, dirty_paths,"
        " porcelain_sha256, measured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (state_id, state.repository_id, state.head_commit, state.branch, state.dirty_paths,
         state.porcelain_sha256, created))
    return state_id


def _finding(conn, project_id, handle_text):
    return conn.execute(
        "SELECT id, status, raiser_seat_id, fixed_commit FROM findings"
        " WHERE project_id = ? AND handle = ?", (project_id, int(handle_text[1:]))).fetchone()


# DD § 8: each transition's allowed from-statuses, and its to-status
_FROM = {"fixes": ("open",), "fix-accepted": ("fixed",), "fix-refused": ("fixed",),
         "withdrawn": ("open", "fixed")}
_TO = {"fixes": "fixed", "fix-accepted": "fix-accepted", "fix-refused": "open",
       "withdrawn": "withdrawn"}


@dataclass(frozen=True)
class _Transition:
    line: int
    field: str  # FIXES or RESOLVE
    handle_text: str
    transition: str  # fixes, fix-accepted, fix-refused or withdrawn
    commit: str | None
    reason: str | None


def _transitions(draft, lines):
    """FIXES and RESOLVE lines as one list in header line order (PR-3 fold S8)."""
    result = [_Transition(lines.of("FIXES", index), "FIXES", handle_text, "fixes", commit, None)
              for index, (handle_text, commit) in enumerate(draft.fixes)]
    result += [_Transition(lines.of("RESOLVE", index), "RESOLVE", handle_text, transition, None,
                           reason)
               for index, (handle_text, transition, reason) in enumerate(draft.resolves)]
    return sorted(result, key=lambda item: item.line)


def _check_findings(conn, sender, transitions):
    """FIXES and RESOLVE rules against current statuses, in header line order,
    as if each earlier line of the same draft had applied."""
    status = {}
    for item in transitions:
        row = _known(conn, sender, item.handle_text, item.field, item.line)
        if (item.field == "RESOLVE" and row[2] != sender.seat_id
                and not (sender.role == ORCHESTRATOR_ROLE)):
            raise errors.error_for("E-RESOLVE-NOT-RAISER", line=item.line, value=item.handle_text)
        current = status.get(row[0], row[1])
        if current not in _FROM[item.transition]:
            raise errors.error_for("E-RESOLVE-STATE", line=item.line, field=item.field,
                                   transition=item.transition, value=item.handle_text,
                                   status=current)
        status[row[0]] = _TO[item.transition]


def _known(conn, sender, handle_text, field, line):
    row = _finding(conn, sender.project_id, handle_text)
    if row is None:
        raise errors.error_for("E-FINDING-UNKNOWN", line=line, field=field, value=handle_text)
    return row


def _write_findings(conn, sender, draft, relay_id, bundle_id, edge_ids, created):
    if not draft.findings:
        return
    next_handle = (conn.execute("SELECT max(handle) FROM findings WHERE project_id = ?",
                                (sender.project_id,)).fetchone()[0] or 0) + 1
    for offset, finding in enumerate(draft.findings):
        finding_id = new_uuid7(created)
        check_edge = (None if finding.check_index is None
                      else edge_ids[finding.check_index - 1])
        conn.execute(
            "INSERT INTO findings (id, handle, project_id, bundle_id, raised_in, raiser_seat_id,"
            " severity, check_edge_id, summary, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (finding_id, next_handle + offset, sender.project_id, bundle_id, relay_id,
             sender.seat_id, finding.severity, check_edge, finding.summary))
        _event(conn, finding_id, relay_id, "raised", "open", "open", None, None, created)


def _write_transitions(conn, sender, transitions, relay_id, created):
    """Each line's update and event, in header line order; each reads the
    status the line before it wrote."""
    for item in transitions:
        row = _finding(conn, sender.project_id, item.handle_text)
        to_status = _TO[item.transition]
        if item.field == "FIXES":
            conn.execute("UPDATE findings SET status = 'fixed', fixed_commit = ? WHERE id = ?",
                         (item.commit, row[0]))
        else:
            closed = to_status in ("fix-accepted", "withdrawn")
            conn.execute("UPDATE findings SET status = ?, resolved_in = ?, reason = ?"
                         " WHERE id = ?", (to_status, relay_id if closed else None, item.reason,
                                           row[0]))
        _event(conn, row[0], relay_id, item.transition, row[1], to_status, item.commit,
               item.reason, created)


def _event(conn, finding_id, relay_id, transition, from_status, to_status, commit, reason,
           created):
    conn.execute(
        "INSERT INTO finding_events (finding_id, relay_id, transition, from_status, to_status,"
        " fixed_commit, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (finding_id, relay_id, transition, from_status, to_status, commit, reason, created))


def _write_answered_by(conn, parent_id, seat_id, relay_id):
    """The first child a recipient files answers the parent for that seat."""
    conn.execute("UPDATE recipients SET answered_by = ? WHERE relay_id = ? AND seat_id = ?"
                 " AND answered_by IS NULL", (relay_id, parent_id, seat_id))
