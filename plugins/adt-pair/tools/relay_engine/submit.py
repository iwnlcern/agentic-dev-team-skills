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

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import pwd
import re
import secrets
import sqlite3
from typing import Callable, Protocol

from relay_engine import addresses
from relay_engine import errors
from relay_engine import header
from relay_engine import jcs
from relay_engine import refs
from relay_engine import schema

SNAPSHOT_KINDS = frozenset({"DESIGN", "PLAN"})
SNAPSHOT_BYTES_MAX = 1 << 20

SYSTEM = "system"  # the reserved sender of engine-written relays
ORCHESTRATOR_ROLE = "orchestrator-planner"
OPERATOR_SEAT_ID = "operator"  # J2's machine-wide special: one queue, no team seat


def party_columns(party):
    """(seat id, special) for one party: a seat id, or a special name (planner Q1's arcs)."""
    return (None, party) if party in schema.SPECIALS else (party, None)


def party_sql(prefix=""):
    """The SQL that reads an arc back as one party string."""
    return "coalesce(%sseat_id, %sspecial)" % (prefix, prefix)


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

    def has_repository(self, repo: str) -> bool:
        """Whether this project has a repository of that name."""


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


# Only Authority's tables may be named as hook-writable (slice 2a D1).
HOOK_WRITE_OWNERS = frozenset({"authority"})

_UNAVAILABLE_REASONS = frozenset({"missing", "corrupt", "unsupported-codec"})


class EvidenceUnavailable(Exception):
    """A store knows an object but cannot return its bytes; reason says why."""

    def __init__(self, reason):
        if not isinstance(reason, str) or reason not in _UNAVAILABLE_REASONS:
            raise ValueError("EvidenceUnavailable reason is missing, corrupt or unsupported-codec")
        super().__init__(reason)
        self.reason = reason


class EvidenceStore(Protocol):
    """Platform's content-addressed evidence store (DD § 17, amendment5)."""

    def put(self, data: bytes) -> str:
        """Keep bytes under their sha256 and return that key."""

    def get(self, sha256: str) -> bytes:
        """Return the bytes retained under sha256.

        Raise KeyError for an object the store never held, and
        EvidenceUnavailable for a known object it cannot return."""

    def captured_by(self, sha256: str) -> str | None:
        """Return host-observed, seat-supplied, or None for an absent record."""


def _validate_read_denials(denied):
    if not isinstance(denied, frozenset):
        raise ValueError("read denials are a frozenset of declared table names")
    if any(not isinstance(table, str) or table not in schema.TABLE_OWNERS
           for table in denied):
        raise ValueError("read denials name only declared tables")


@dataclass(frozen=True)
class SubmitContext:
    sender: SeatRef
    git_state: GitState | None
    now_ms: Callable[[], int]
    directory: Directory
    submission_key: str
    after_admit: Callable[[sqlite3.Connection, "Admitted", header.Draft, "SubmitContext"], None] | None = None
    after_admit_writes: frozenset[str] = frozenset()
    store: EvidenceStore | None = None
    after_admit_reads_denied: frozenset[str] = frozenset({"bodies"})
    sender_check: Callable[[sqlite3.Connection], None] | None = None

    def __post_init__(self):
        _check_git_state(self.git_state)
        _validate_read_denials(self.after_admit_reads_denied)
        if (not isinstance(self.submission_key, str) or not self.submission_key
                or "\x00" in self.submission_key):
            raise ValueError("submission_key is a non-empty UTF-8 string without NUL")
        try:
            key_bytes = self.submission_key.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("submission_key must be UTF-8 encodable") from None
        if len(key_bytes) > 256:
            raise ValueError("submission_key is at most 256 UTF-8 bytes")
        if self.after_admit is not None and not callable(self.after_admit):
            raise ValueError("after_admit is callable or None")
        if not isinstance(self.after_admit_writes, frozenset):
            raise ValueError("after_admit_writes is a frozenset of owned table names")
        for table in self.after_admit_writes:
            if (not isinstance(table, str) or table not in schema.TABLE_OWNERS
                    or schema.TABLE_OWNERS[table] not in HOOK_WRITE_OWNERS):
                raise ValueError("after_admit_writes names only declared hook-owned tables")
        if self.sender_check is not None and not callable(self.sender_check):
            raise ValueError("sender_check is callable or None")


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
    if conn in _READ_GUARDS:
        raise sqlite3.DatabaseError("admission is not allowed during a read guard")
    if getattr(conn, "autocommit", None) is False:
        raise ValueError("submit requires legacy transaction control or autocommit True")
    if conn in _HOOK_FRAMES:
        raise sqlite3.DatabaseError("nested admission is not allowed during a hook")
    if conn in _CHECK_FRAMES:
        raise sqlite3.DatabaseError("nested admission is not allowed during a sender check")
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise ValueError("the store connection has foreign keys off; open it with open_store")
    now = ctx.now_ms()
    try:
        if ctx.sender_check is not None:
            replay = _checked_replay(conn, ctx, data)
            if replay is not None:
                return replay
        return _admit(conn, ctx, data, now)
    except errors.EngineError as error:
        _record_refusal(conn, ctx, data, now, error)
        raise


# Engine owns this connection's authorizer and progress-handler slots for its whole life.
# A hook can change connection attributes, including isolation_level and autocommit,
# whose changes outlive the submit.
# This SQL write boundary trusts Python code in the process, including calls to
# set_authorizer, set_progress_handler and blobopen.
# Read denials cover cooperating code, not prepared statements already being stepped;
# schema objects created inside the hook can outlive its read boundary.
_HOOK_FRAMES = {}
_READ_GUARDS = {}
_CHECK_FRAMES = {}


def _hook_name(hook):
    module = getattr(hook, "__module__", type(hook).__module__)
    qualname = getattr(hook, "__qualname__", type(hook).__qualname__)
    if not isinstance(module, str) or not isinstance(qualname, str):
        return "relay_engine.submit.unknown_hook"
    name = module + "." + qualname
    return name if errors._is_hook_name(name) else "relay_engine.submit.unknown_hook"


def _transaction_state(conn):
    try:
        return "active" if conn.in_transaction else "ended"
    except sqlite3.ProgrammingError:
        return "closed"


def _run_hook(conn, ctx, admitted, draft):
    if ctx.after_admit is None:
        return
    reads = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT,
             sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
    def authorize(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table in ctx.after_admit_reads_denied:
            return sqlite3.SQLITE_DENY
        if action in reads:
            return sqlite3.SQLITE_OK
        if action in writes and database == "main" and table in ctx.after_admit_writes:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    name = _hook_name(ctx.after_admit)
    _HOOK_FRAMES[conn] = (ctx, authorize)
    try:
        conn.set_authorizer(authorize)
        # Prepared VMs are not re-authorized; stop them after any conflict rollback.
        conn.set_progress_handler(lambda: 0 if conn.in_transaction else 1, 1)
        failure = None
        try:
            ctx.after_admit(conn, admitted, draft, ctx)
        except errors.EngineError as exc:
            if _transaction_state(conn) == "closed":
                failure = sqlite3.ProgrammingError(
                    "the admission hook closed the store connection")
            else:
                failure = exc
        except Exception as exc:
            state = _transaction_state(conn)
            if state == "closed":
                failure = sqlite3.ProgrammingError(
                    "the admission hook closed the store connection")
            elif state == "ended":
                failure = errors.error_for("E-ADMISSION-HOOK", variant="ended-transaction",
                                           hook=name)
            else:
                kind = type(exc).__name__
                if not errors._is_hook_kind(kind):
                    kind = "Exception"
                failure = errors.error_for("E-ADMISSION-HOOK", hook=name, kind=kind)
        if failure is not None:
            raise failure
        state = _transaction_state(conn)
        if state == "closed":
            raise sqlite3.ProgrammingError(
                "the admission hook closed the store connection")
        if state == "ended":
            raise errors.error_for("E-ADMISSION-HOOK", variant="ended-transaction",
                                   hook=name) from None
    finally:
        _HOOK_FRAMES.pop(conn, None)
        try:
            conn.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        try:
            conn.set_authorizer(None)
        except sqlite3.Error:
            pass


def _run_sender_check(conn, ctx):
    """D7: the sender check under a read-only frame, inside the caller's transaction.

    The authorizer allows only reads, with the hook's read denials, so the
    check cannot write, begin or end a transaction, attach, detach or set a
    pragma; while it runs, submit on this connection refuses as a nested
    admission, and no hook frame exists for system filing. An EngineError it
    raises is a refusal; anything else, or a check that ends the transaction
    or closes the connection, is a fault."""
    reads = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT,
             sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    denied = ctx.after_admit_reads_denied

    def authorize(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table in denied:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in reads else sqlite3.SQLITE_DENY
    _CHECK_FRAMES[conn] = ctx
    try:
        conn.set_authorizer(authorize)
        ctx.sender_check(conn)
        state = _transaction_state(conn)
        if state == "closed":
            raise sqlite3.ProgrammingError("the sender check closed the store connection")
        if state == "ended":
            raise sqlite3.ProgrammingError("the sender check ended the transaction")
    finally:
        _CHECK_FRAMES.pop(conn, None)
        try:
            conn.set_authorizer(None)
        except sqlite3.Error:
            pass


def _checked_replay(conn, ctx, data):
    """D6 step 1: the sender check, then the replay lookup on the same read
    snapshot, in one short read transaction ended before any preparation."""
    conn.execute("BEGIN")
    try:
        _run_sender_check(conn, ctx)
        return _replay(conn, ctx, data)
    finally:
        if _transaction_state(conn) == "active":
            conn.execute("ROLLBACK")


def file_system_relay(conn, ctx, data):
    """Engine filing inside this connection's active hook; owns no transaction."""
    frame = _HOOK_FRAMES.get(conn)
    if frame is None or frame[0] is not ctx or not conn.in_transaction:
        raise RuntimeError("system filing requires this connection's active admission hook")
    system = SeatRef(SYSTEM, SYSTEM, SYSTEM, ctx.sender.team_instance_id,
                     ctx.sender.project_id)
    internal = replace(ctx, sender=system, after_admit=None,
                       after_admit_writes=frozenset(), sender_check=None)
    # Each real authorizer transition expires cached prepared statements.
    # A boolean privilege flag would let cached engine INSERTs escape later.
    conn.set_authorizer(None)
    try:
        conn.execute("SAVEPOINT engine_system_filing")
        try:
            admitted = _admit(conn, internal, data, ctx.now_ms(), _system=True)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK TO engine_system_filing")
                conn.execute("RELEASE engine_system_filing")
            raise
        conn.execute("RELEASE engine_system_filing")
        return admitted
    finally:
        conn.set_authorizer(frame[1])


def _record_refusal(conn, ctx, data, now, error):
    line = error.params.get("line", getattr(error, "refusal_line", None))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO refusals (project_id, seat_id, special, code, line, at, draft_sha256)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ctx.sender.project_id, *party_columns(ctx.sender.seat_id), error.code,
             line if isinstance(line, int) else None, now,
             hashlib.sha256(data).hexdigest()))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _replay(conn, ctx, data):
    """The persisted receipt belongs to one party and the exact input bytes."""
    row = conn.execute(
        "SELECT id, handle, created_at, draft_sha256 FROM relays"
        " WHERE coalesce(sender_seat_id, sender_special) = ? AND submission_key = ?",
        (ctx.sender.seat_id, ctx.submission_key)).fetchone()
    if row is None:
        return None
    if row[3] != hashlib.sha256(data).hexdigest():
        raise errors.error_for("E-SUBMISSION-KEY-REUSED")
    return Admitted(row[0], row[1], row[2])


def _admit(conn, ctx, data, now, *, _system=False):
    # With a sender check, submit has already run the checked replay lookup (D6 step 1).
    checked = not _system and ctx.sender_check is not None
    replay = None if _system or checked else _replay(conn, ctx, data)
    if replay is not None:
        return replay
    draft = header.parse(data)
    lines = _Lines(data, draft.header_bytes)
    sender = ctx.sender
    # Before the write lock: a slow checkout or a large file digest holds no
    # lock (PR-3 fold S2); relays and recipients rows are append-only.
    recipients = _recipients(ctx, draft, lines)
    parent = _parent(conn, sender, draft, lines)
    targets = _references(conn, ctx, draft, lines)
    transitions = _transitions(draft, lines)
    snapshot_plan = _snapshot_plan(draft, lines, targets)
    if not _system:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if checked:
            _run_sender_check(conn, ctx)  # D6 step 3: again, under the write lock
        # Reference resolution preceded the lock; another writer may have
        # committed this party/key while we were resolving that input.
        replay = None if _system else _replay(conn, ctx, data)
        if replay is not None:
            conn.execute("ROLLBACK")
            return replay
        _check_findings(conn, sender, transitions)
        locks = _lock_plan(conn, sender, draft, lines, parent, targets)
        handle, created, relay_id = _identity(conn, sender.project_id, now)
        bundle_id = (parent[1] if parent is not None
                     else _bundle(conn, sender, draft.bundle, created))
        git_state_id = _write_git_state(conn, ctx.git_state, created)
        conn.execute(
            "INSERT INTO relays (id, handle, project_id, team_instance_id, bundle_id, kind,"
            " verdict, needs, subject, parent_id, sender_seat_id, sender_special, sender_role,"
            " created_at, body_sha256, meta_sha256, body_bytes, header_bytes, git_state_id,"
            " submission_key, draft_sha256)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (relay_id, handle, sender.project_id, sender.team_instance_id, bundle_id,
             draft.kind, draft.verdict, draft.needs, draft.subject,
             None if parent is None else parent[0], *party_columns(sender.seat_id),
             sender.role, created, hashlib.sha256(draft.body).hexdigest(),
             meta_digest(draft, None if parent is None else parent[0], targets), len(draft.body),
             draft.header_bytes, git_state_id, None if _system else ctx.submission_key,
             None if _system else hashlib.sha256(data).hexdigest()))
        conn.execute("INSERT INTO bodies (relay_id, body) VALUES (?, ?)",
                     (relay_id, draft.body))
        conn.executemany(
            "INSERT INTO recipients (relay_id, seat_id, special, role) VALUES (?, ?, ?, ?)",
            [(relay_id, *party_columns(seat_id), role) for seat_id, role in recipients])
        if parent is not None:
            conn.execute(
                "INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                " target_relay_id) VALUES (?, ?, 1, 'parent', 'relay', ?, ?)",
                (new_uuid7(created), relay_id, draft.parent, parent[0]))
        edge_ids = _write_references(conn, relay_id, created, targets)
        _write_snapshots(conn, ctx.store, relay_id, created, edge_ids, snapshot_plan)
        conn.executemany("UPDATE snapshots SET locked_by = ?, locked_at = ?"
                         " WHERE handle = ? AND locked_by IS NULL",
                         [(relay_id, created, h) for h in locks])
        _write_findings(conn, sender, draft, relay_id, bundle_id, edge_ids, created)
        _write_transitions(conn, sender, transitions, relay_id, created)
        if parent is not None:
            _write_answered_by(conn, parent[0], sender.seat_id, relay_id)
        # an admission is the sender's activity (PR-3 fold R6), committed with it;
        # a special (operator, system) is not a seat and records none, as in note_activity
        if sender.seat_id not in schema.SPECIALS:
            conn.execute("INSERT INTO seat_activity (seat_id, last_request_at) VALUES (?, ?)"
                         " ON CONFLICT (seat_id) DO UPDATE SET last_request_at ="
                         " max(last_request_at, excluded.last_request_at)",
                         (sender.seat_id, created))
        if not _system:
            _run_hook(conn, ctx, Admitted(relay_id, handle, created), draft)
            conn.execute("COMMIT")
    except BaseException:
        if not _system:
            try:
                active = conn.in_transaction
            except sqlite3.ProgrammingError:
                active = False
            if active:
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
    for field, role, named in (("TO", "to", draft.to), ("CC", "cc", draft.cc)):
        for address in named:
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
        "SELECT 1 FROM relays WHERE id = ? AND " + party_sql("sender_") + " = ?"
        " UNION ALL SELECT 1 FROM recipients WHERE relay_id = ? AND " + party_sql() + " = ?",
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
    conn.execute("INSERT INTO bundles (id, project_id, team_instance_id, name, opened_by_seat_id,"
                 " opened_by_special) VALUES (?, ?, ?, ?, ?, ?)",
                 (bundle_id, sender.project_id, sender.team_instance_id, name,
                  *party_columns(sender.seat_id)))
    return bundle_id


def _references(conn, ctx, draft, lines):
    """Each REF resolved and digested, before anything is written."""
    targets = []
    for index, text in enumerate(draft.refs):
        line = lines.of("REF", index)
        try:
            targets.append((text, _resolve(
                conn, ctx, refs.parse_ref(text),
                keep=SNAPSHOT_BYTES_MAX if draft.kind in SNAPSHOT_KINDS and draft.needs == "review" else 0)))
        except errors.EngineError as error:
            raise _at(line, error)
    return targets


def _resolve(conn, ctx, spec, *, keep=0):
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
        digest, length, kept = refs.read_file(Path(path), keep)
        return {"target_type": "path", "target_path": path,
                "digest_at_submit": digest, "_length": length, "_bytes": kept}
    known = conn.execute("SELECT id FROM repositories WHERE project_id = ? AND name = ?",
                         (ctx.sender.project_id, spec.repo)).fetchone()
    checkout = None if known is None else ctx.directory.checkout(spec.repo)
    if checkout is None:
        raise errors.error_for("E-REF-UNRESOLVED", value=header._display(spec.text),
                               reason=("repository %s is not known to this project"
                                       if known is None else
                                       "repository %s has no registered checkout in this project")
                               % header._display(spec.repo, 40))
    resolved = refs.resolve_git(checkout, spec)
    return {"target_type": "git", "target_repository_name": spec.repo,
            "target_repository_id": known[0], "target_rev": spec.rev,
            "target_commit": resolved.commit, "target_path": spec.path,
            "target_line_start": spec.line_start, "target_line_end": spec.line_end,
            "digest_at_submit": resolved.blob_sha, "_checkout": checkout}


_EDGE_COLUMNS = ("target_type", "target_relay_id", "target_repository_name",
                 "target_repository_id", "target_rev",
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


def _artifact(target):
    """The artifact a file or Git target names, as snapshots key it."""
    if target["target_type"] == "path":
        return target["target_path"]
    if target["target_type"] == "git":
        return "%s:%s" % (target["target_repository_id"], target["target_path"])
    return None


def _snapshot_plan(draft, lines, targets):
    """(ref index, artifact, sha256, length, bytes or None) for each file or
    Git target of a review request (DD § 17)."""
    if draft.kind not in SNAPSHOT_KINDS or draft.needs != "review":
        return []
    plan = []
    for index, (_, target) in enumerate(targets):
        artifact = _artifact(target)
        if artifact is None:
            continue
        if target["target_type"] == "path":
            plan.append((index, artifact, target["digest_at_submit"], target["_length"],
                         target["_bytes"]))
            continue
        try:
            blob = refs.read_blob(target["_checkout"], target["digest_at_submit"])
        except errors.EngineError as error:
            raise _at(lines.of("REF", index), error)
        plan.append((index, artifact, hashlib.sha256(blob).hexdigest(), len(blob), None))
    return plan


def _lock_plan(conn, sender, draft, lines, parent, targets):
    """Handles of the request's snapshots an approve locks; an approve from a
    non-TO seat, naming none of them, or naming a changed one is refused."""
    if draft.verdict != "approve" or parent is None:
        return []
    snapshots = conn.execute(
        "SELECT s.handle, s.artifact, e.digest_at_submit FROM snapshots s"
        " JOIN edges e ON e.id = s.edge_id WHERE s.relay_id = ? ORDER BY s.handle",
        (parent[0],)).fetchall()
    if not snapshots:
        return []
    request = conn.execute("SELECT handle FROM relays WHERE id = ?", (parent[0],)).fetchone()[0]
    line = lines.of("VERDICT")
    if conn.execute("SELECT 1 FROM recipients WHERE relay_id = ? AND coalesce(seat_id, special) = ? AND role = 'to'",
                    (parent[0], sender.seat_id)).fetchone() is None:
        raise errors.error_for("E-SNAPSHOT-NOT-TO", line=line, handle=request)
    named = {}
    for index, (_, target) in enumerate(targets):
        artifact = _artifact(target)
        if artifact is not None:
            named.setdefault(artifact, []).append((index, target["digest_at_submit"]))
    referenced = [row for row in snapshots if row[1] in named]
    if not referenced:
        raise errors.error_for("E-SNAPSHOT-NO-REF", line=line, handle=request)
    for artifact, citations in named.items():
        digests = {digest for _, name, digest in referenced if name == artifact}
        if not digests:
            continue
        for index, now in citations:
            if now not in digests:
                raise errors.error_for("E-SNAPSHOT-STALE", line=lines.of("REF", index),
                                       value=header._display(artifact), handle=request)
    return [handle for handle, artifact, digest in referenced
            if any(now == digest for _, now in named[artifact])]


def _write_snapshots(conn, store, relay_id, created, edge_ids, plan):
    """Snapshot rows, and their bytes in the evidence store once per digest."""
    last = conn.execute("SELECT coalesce(max(handle), 0) FROM snapshots").fetchone()[0]
    for handle, (index, artifact, sha, length, data) in enumerate(plan, start=last + 1):
        if store is None:
            data = None
        if data is not None and store.captured_by(sha) is None:
            key = store.put(data)
            if key != sha:
                raise ValueError("the evidence store keyed the bytes as %r, not their sha256"
                                 % (key,))
        conn.execute(
            "INSERT INTO snapshots (id, handle, relay_id, edge_id, artifact, content_sha256,"
            " bytes_len, has_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_uuid7(created), handle, relay_id, edge_ids[index], artifact, sha, length,
             int(data is not None)))


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
    conn.execute("UPDATE recipients SET answered_by = ? WHERE relay_id = ? AND " + party_sql()
                 + " = ? AND answered_by IS NULL", (relay_id, parent_id, seat_id))


class TableDirectory:
    """A Directory over platform's tables (J2, J3): <team>.<role> in the sender's
    project or <team>.<role>@<project> names the seat of that role in the open
    team instance of that name; operator names the special."""

    def __init__(self, conn, project_id):
        self.conn, self.project_id = conn, project_id

    def resolve(self, address, sender):
        try:  # the grammar and the parts are platform's validator's alone (J1, J2, J-T1)
            parsed = addresses.parse_address(address)
        except addresses.AddressRefused as refused:
            raise header.address_refused(refused) from None
        if parsed.special is not None:
            return SeatRef(OPERATOR_SEAT_ID, address, parsed.special, None, sender.project_id)
        team, role, project_name = parsed.team, parsed.role, parsed.project
        if project_name:
            row = self.conn.execute("SELECT id FROM projects WHERE name = ?",
                                    (project_name,)).fetchone()
            if row is None:
                raise header.AddressRefused("no project is named %s" % project_name)
            project_id = row[0]
        else:
            project_id = sender.project_id
        instance = self.conn.execute(
            "SELECT id FROM team_instances WHERE project_id = ? AND name = ? AND closed_at IS NULL",
            (project_id, team)).fetchone()
        if instance is None:
            raise header.AddressRefused("no open team %s in that project" % team)
        row = self.conn.execute("SELECT id, state FROM seats WHERE team_instance_id = ? AND role = ?",
                                (instance[0], role)).fetchone()
        if row is None:
            raise header.AddressRefused("no seat %s in team %s" % (role, team))
        if row[1] == "stood-down":
            raise header.AddressRefused("the seat %s.%s is stood down" % (team, role))
        return SeatRef(row[0], address, role, instance[0], project_id)

    def checkout(self, repo):
        """The most recently seen open checkout of this project's repository named repo."""
        row = self.conn.execute(
            "SELECT c.path FROM repositories r JOIN repository_checkouts c ON c.repository_id = r.id"
            " WHERE r.project_id = ? AND r.name = ? AND c.ended_at IS NULL"
            " ORDER BY c.last_seen_at DESC LIMIT 1",
            (self.project_id, repo)).fetchone()
        return None if row is None else Path(row[0])

    def has_repository(self, repo):
        return self.conn.execute(
            "SELECT 1 FROM repositories WHERE project_id = ? AND name = ?",
            (self.project_id, repo)).fetchone() is not None


@contextmanager
def read_guard(conn, denied=frozenset({"bodies"})):
    """Deny cooperating code's table reads, retaining an outer guard or hook.

    This is not a sandbox: statements prepared before the guard keep their
    authorization while stepped, blobopen and other in-process Python can
    read any table, and schema objects created inside a guard outlive it.
    """
    _validate_read_denials(denied)
    frame = _HOOK_FRAMES.get(conn)
    if frame is not None:
        if not denied <= frame[0].after_admit_reads_denied:
            raise RuntimeError("a read guard inside a hook cannot deny more than the hook's own read denials")
        yield
        return
    previous = _READ_GUARDS.get(conn)
    if previous is not None and denied <= previous[0]:
        yield
        return
    active = denied if previous is None else previous[0] | denied
    def authorize(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table in active:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    _READ_GUARDS[conn] = (active, authorize)
    try:
        conn.set_authorizer(authorize)
        yield
    finally:
        _READ_GUARDS.pop(conn, None)
        if previous is not None:
            _READ_GUARDS[conn] = previous
        failure = None
        try:
            conn.set_authorizer(None if previous is None else previous[1])
        except sqlite3.ProgrammingError as error:
            if _transaction_state(conn) != "closed":
                failure = error
        if failure is not None:
            raise failure
