"""Append-only relay ledger and atomic admission transactions."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
import hashlib
import os
import re
import sqlite3
import sys
import time
import uuid

from relay_engine import errors
from relay_engine.envelope import (Envelope, body_sha256, content_hash,
                                   parse_draft, valid_run_id)
from relay_engine.jcs import commissioned_by_value, jcs_encode, parse_record


SCHEMA_VERSION = "1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_DDL = """
CREATE TABLE relays(
  seq INTEGER PRIMARY KEY,
  submission_id TEXT, stamp TEXT NOT NULL, rendered_path TEXT UNIQUE NOT NULL,
  admits_against_seq INTEGER REFERENCES relays(seq),
  phase TEXT NOT NULL, role TEXT NOT NULL, dispatch_id TEXT NOT NULL,
  parent_dispatch_id TEXT, run_id TEXT, from_seat TEXT NOT NULL,
  to_seats TEXT, cc_seats TEXT, status TEXT, subject TEXT,
  headers_json TEXT NOT NULL, body BLOB NOT NULL,
  body_sha256 TEXT NOT NULL, content_hash TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('daemon','hand','adopted')),
  occupancy_ref INTEGER, advisories_json TEXT,
  UNIQUE(from_seat, submission_id));
CREATE UNIQUE INDEX ux_relays_stamp_daemon ON relays(stamp) WHERE origin='daemon';
CREATE TABLE seat_events(seq INTEGER PRIMARY KEY, address TEXT NOT NULL,
  event TEXT NOT NULL CHECK(event IN ('declared','booted','occupied','replaced','offline','stood-down')),
  occupant_id TEXT, key_id TEXT,
  boot_relay_seq INTEGER REFERENCES relays(seq),
  cause_seq INTEGER, detail TEXT);
CREATE TABLE cycle_events(seq INTEGER PRIMARY KEY, dispatch_id TEXT NOT NULL,
  event TEXT NOT NULL CHECK(event IN ('open','close','reopen')),
  commissioning_seq INTEGER, cause_seq INTEGER);
CREATE TABLE supersession_edges(seq INTEGER PRIMARY KEY, target_seq INTEGER NOT NULL,
  ruling_seq INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('local','adopted')),
  applied INTEGER NOT NULL);
CREATE TABLE projection_events(seq INTEGER PRIMARY KEY, relay_seq INTEGER REFERENCES relays(seq),
  target TEXT NOT NULL CHECK(target IN ('relay','index','seats','foreign-path')),
  event TEXT NOT NULL CHECK(event IN ('rendered','missing','modified','conflict','divergence','failed','repaired')),
  digest TEXT, detail TEXT,
  CHECK((target='relay') = (relay_seq IS NOT NULL)));
CREATE TABLE migration_events(seq INTEGER PRIMARY KEY,
  event TEXT NOT NULL CHECK(event IN ('epoch-open','census','cutover','rollback')), digest TEXT, detail TEXT);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, commissioned_by TEXT NOT NULL);
CREATE TABLE commissions(child_run_id TEXT PRIMARY KEY,
  dispatch_seq INTEGER NOT NULL REFERENCES relays(seq),
  dispatch_content_digest TEXT NOT NULL, record_digest TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class InjectedFault(RuntimeError):
    def __init__(self, point, committed=False):
        self.point = point
        self.committed = committed
        super().__init__(point)


@dataclass(frozen=True)
class Admission:
    seq: int
    stamp: str
    rendered_path: str
    replay: bool
    render_state: str = "render-pending"


def _hit(fault, point, committed=False):
    if fault == point:
        raise InjectedFault(point, committed=committed)
    if callable(fault):
        fault(point)


def _ledger_path(engine_dirfd):
    if not isinstance(engine_dirfd, int):
        raise TypeError("engine directory descriptor required")
    os.fstat(engine_dirfd)
    if sys.platform == "darwin":
        buffer = fcntl.fcntl(engine_dirfd, fcntl.F_GETPATH,
                             b"\0" * 1024)
        directory = buffer.split(b"\0", 1)[0].decode("utf-8")
        return os.path.join(directory, "ledger.db")
    if sys.platform.startswith("linux"):
        return "/proc/self/fd/%d/ledger.db" % engine_dirfd
    raise OSError("held-descriptor ledger path unsupported on this host")


def _connect(engine_dirfd):
    path = _ledger_path(engine_dirfd)
    ledger = sqlite3.connect(path, isolation_level=None, timeout=30.0,
                             check_same_thread=False)
    try:
        ledger.execute("PRAGMA journal_mode=WAL")
        ledger.execute("PRAGMA synchronous=FULL")
        ledger.execute("PRAGMA foreign_keys=ON")
        return ledger
    except BaseException:
        ledger.close()
        raise


def open_ledger(engine_dirfd):
    """Open the ledger named by a held engine directory descriptor."""
    return _connect(engine_dirfd)


@contextmanager
def _transaction(ledger):
    ledger.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        ledger.execute("ROLLBACK")
        raise
    else:
        ledger.execute("COMMIT")


def _root_has_index(engine_dirfd):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open("..", flags, dir_fd=engine_dirfd)
    try:
        try:
            os.stat("INDEX.md", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(root_fd)


def init_schema(engine_dirfd, canonical_root, fault=None):
    """Initialize schema, root identity, and the initial migration epoch."""
    if not isinstance(canonical_root, str) or not os.path.isabs(canonical_root):
        raise ValueError("canonical absolute root required")
    legacy = _root_has_index(engine_dirfd)
    ledger = _connect(engine_dirfd)
    try:
        with _transaction(ledger):
            for statement in _DDL.split(";"):
                if statement.strip():
                    ledger.execute(statement)
            _hit(fault, "after-ddl")
            seeds = (("schema_version", SCHEMA_VERSION),
                     ("canonical_root", canonical_root),
                     ("root_uuid", str(uuid.uuid4())))
            ledger.executemany("INSERT INTO meta(key,value) VALUES(?,?)",
                               seeds)
            _hit(fault, "after-meta")
            ledger.execute(
                "INSERT INTO migration_events(event,digest,detail) "
                "VALUES('epoch-open',NULL,NULL)")
            if not legacy:
                ledger.execute(
                    "INSERT INTO migration_events(event,digest,detail) "
                    "VALUES('cutover',NULL,NULL)")
            _hit(fault, "after-epoch")
        return ledger
    except BaseException:
        ledger.close()
        raise


def epoch_state(ledger):
    """Return active only when the last epoch decision is cutover."""
    try:
        row = ledger.execute(
            "SELECT event FROM migration_events "
            "WHERE event IN ('epoch-open','cutover','rollback') "
            "ORDER BY seq DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return "inert"
    return "active" if row == ("cutover",) else "inert"


def _validate_commissioning(carrier, run_id):
    fields, record_digest = parse_record(carrier)
    expected_keys = {
        "v", "parent_root_uuid", "commissioning_path",
        "dispatch_content_digest", "child_run_id",
    }
    if set(fields) != expected_keys or fields["v"] != 1:
        raise ValueError("commissioning record fields mismatch")
    if fields["child_run_id"] != run_id:
        raise errors.error_for("run-id-mismatch")
    try:
        parent_uuid = str(uuid.UUID(fields["parent_root_uuid"]))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("commissioning root identity mismatch") from exc
    if parent_uuid != fields["parent_root_uuid"]:
        raise ValueError("commissioning root identity mismatch")
    path = fields["commissioning_path"]
    if (not isinstance(path, str) or not path or os.path.isabs(path) or
            any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError("commissioning path mismatch")
    dispatch_digest = fields["dispatch_content_digest"]
    if (not isinstance(dispatch_digest, str) or
            _DIGEST.fullmatch(dispatch_digest) is None):
        raise ValueError("commissioning dispatch digest mismatch")
    value = commissioned_by_value(parent_uuid, path, dispatch_digest,
                                  record_digest).decode("utf-8")
    return value


def establish_run_identity(ledger, run_id, commissioning=None, fault=None):
    """Establish immutable run identity and optional child commission."""
    if run_id is not None and not valid_run_id(run_id):
        raise errors.error_for("run-id-invalid")
    result = None
    with _transaction(ledger):
        existing = ledger.execute(
            "SELECT value FROM meta WHERE key='run_id'").fetchone()
        if existing is None:
            if run_id is None:
                raise errors.error_for("run-id-uninitialized")
            ledger.execute("INSERT INTO meta(key,value) VALUES('run_id',?)",
                           (run_id,))
            established = run_id
        else:
            established = existing[0]
            if run_id is not None and run_id != established:
                raise errors.error_for("run-id-mismatch")
        _hit(fault, "after-run-id")
        if commissioning is None:
            result = established
        else:
            persisted = _validate_commissioning(commissioning, established)
            prior = ledger.execute(
                "SELECT commissioned_by FROM runs WHERE run_id=?",
                (established,)).fetchone()
            if prior is None:
                ledger.execute(
                    "INSERT INTO runs(run_id,commissioned_by) VALUES(?,?)",
                    (established, persisted))
            elif prior[0] != persisted:
                raise errors.error_for("commission-conflict")
            result = persisted
        _hit(fault, "after-runs")
        _hit(fault, "pre-commit")
    return result


def _next_seq(ledger, table):
    if table not in {
            "relays", "seat_events", "cycle_events", "supersession_edges",
            "projection_events"}:
        raise ValueError("sequence table is not registered")
    return ledger.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM " + table).fetchone()[0]


def _parse_stamp(value):
    return datetime.strptime(value, "%Y%m%d-%H%M%S")


def _format_stamp(value):
    return value.strftime("%Y%m%d-%H%M%S")


def _role_slug(role):
    value = re.sub(r"[^A-Za-z0-9]+", "-", role.strip()).strip("-").lower()
    if not value:
        raise errors.error_for("E-ENVELOPE")
    return value


def _dispatch_lane(envelope):
    parts = envelope.dispatch_id.split("-")
    if (len(parts) >= 2 and parts[0] == envelope.run_id and
            all(_SAFE_TOKEN.fullmatch(part) for part in parts[:2])):
        return "-".join(parts[:2])
    if _SAFE_TOKEN.fullmatch(envelope.dispatch_id) is None:
        raise errors.error_for("E-ENVELOPE")
    return envelope.dispatch_id


def _rendered_path(envelope, stamp):
    return "%s/%s-%s-%s.md" % (
        _dispatch_lane(envelope), envelope.phase.upper(),
        _role_slug(envelope.role), stamp)


def _read_occupant(root, rel):
    try:
        return root.open_read(rel)
    except FileNotFoundError:
        return None


def _resolve_edge(ledger, admits_against):
    if admits_against is None:
        return None
    if (not isinstance(admits_against, str) or not admits_against or
            os.path.isabs(admits_against) or
            any(part in ("", ".", "..")
                for part in admits_against.split("/"))):
        raise errors.error_for("E-ENVELOPE")
    row = ledger.execute(
        "SELECT seq FROM relays WHERE rendered_path=?", (admits_against,)
    ).fetchone()
    if row is None:
        raise errors.error_for("E-ENVELOPE")
    return row[0]


def _server_envelope(body):
    header = re.compile(rb"^[A-Z][A-Z0-9_]*:[ \t]*")
    selected = []
    started = False
    for line in body.splitlines():
        if not started:
            if header.match(line) is None:
                continue
            started = True
        elif not line or header.match(line) is None:
            break
        selected.append(line)
    try:
        text = b"\n".join(selected).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise errors.error_for("E-ENVELOPE") from exc
    try:
        return parse_draft(text)
    except (TypeError, errors.EngineError) as exc:
        raise errors.error_for("E-ENVELOPE") from exc


def _daemon_slot(ledger, root, envelope, clock):
    previous = ledger.execute(
        "SELECT stamp FROM relays WHERE origin='daemon' "
        "ORDER BY seq DESC LIMIT 1").fetchone()
    wall = datetime.fromtimestamp(clock())
    candidate = wall
    if previous is not None:
        candidate = max(candidate, _parse_stamp(previous[0]) +
                        timedelta(seconds=1))
    foreign = []
    while True:
        stamp = _format_stamp(candidate)
        path = _rendered_path(envelope, stamp)
        occupant = _read_occupant(root, path)
        if occupant is None:
            return stamp, path, foreign
        known = ledger.execute(
            "SELECT 1 FROM relays WHERE rendered_path=?", (path,)
        ).fetchone()
        if known is None:
            foreign.append((hashlib.sha256(occupant).hexdigest(), path))
        candidate += timedelta(seconds=1)


def _replay(ledger, from_seat, submission_id, actual_hash):
    row = ledger.execute(
        "SELECT seq,stamp,rendered_path,content_hash FROM relays "
        "WHERE from_seat=? AND submission_id=?",
        (from_seat, submission_id)).fetchone()
    if row is None:
        return None
    if row[3] != actual_hash:
        raise errors.error_for("E-REPLAY-MISMATCH")
    event = ledger.execute(
        "SELECT event FROM projection_events WHERE relay_seq=? "
        "ORDER BY seq DESC LIMIT 1", (row[0],)).fetchone()
    if event is not None and event[0] in ("rendered", "repaired"):
        state = "rendered"
    elif event is not None and event[0] in ("conflict", "modified"):
        state = "render-conflict"
    else:
        state = "render-pending"
    return Admission(row[0], row[1], row[2], True, state)


def _insert_projection_divergences(ledger, foreign):
    for digest, path in foreign:
        ledger.execute(
            "INSERT INTO projection_events"
            "(seq,relay_seq,target,event,digest,detail) "
            "VALUES(?,NULL,'foreign-path','divergence',?,?)",
            (_next_seq(ledger, "projection_events"), digest, path))


def _insert_cycle_events(ledger, entries):
    for entry in entries:
        ledger.execute(
            "INSERT INTO cycle_events"
            "(seq,dispatch_id,event,commissioning_seq,cause_seq) "
            "VALUES(?,?,?,?,?)",
            (_next_seq(ledger, "cycle_events"), entry["dispatch_id"],
             entry["event"], entry.get("commissioning_seq"),
             entry.get("cause_seq")))


def _insert_supersession_edges(ledger, entries):
    for entry in entries:
        ledger.execute(
            "INSERT INTO supersession_edges"
            "(seq,target_seq,ruling_seq,source,applied) VALUES(?,?,?,?,?)",
            (_next_seq(ledger, "supersession_edges"), entry["target_seq"],
             entry["ruling_seq"], entry["source"], entry["applied"]))


def _insert_seat_events(ledger, entries, relay_seq=None):
    for entry in entries:
        boot_relay_seq = entry.get("boot_relay_seq")
        if boot_relay_seq == "current-relay":
            if relay_seq is None:
                raise ValueError("current relay is unavailable")
            boot_relay_seq = relay_seq
        ledger.execute(
            "INSERT INTO seat_events("
            "seq,address,event,occupant_id,key_id,boot_relay_seq,cause_seq,detail"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (_next_seq(ledger, "seat_events"), entry["address"],
             entry["event"], entry.get("occupant_id"), entry.get("key_id"),
             boot_relay_seq, entry.get("cause_seq"), entry.get("detail")))


def append_seat_events(ledger, entries):
    """Append a group of seat transitions in one transaction."""
    with _transaction(ledger):
        _insert_seat_events(ledger, entries)


def admit(ledger, root, envelope, body, submission_id, *,
          claimed_body_sha256=None, claimed_content_hash=None,
          admits_against=None, origin="daemon", stamp=None,
          rendered_path=None, occupancy_ref=None, advisories=(),
          cycle_events=(), supersession_edges=(), prechecks=(),
          seat_events=(),
          clock=time.time, fault=None):
    """Admit one relay and every derived effect in a single transaction."""
    if isinstance(envelope, str):
        envelope = parse_draft(envelope)
    if not isinstance(envelope, Envelope) or not isinstance(body, bytes):
        raise TypeError("parsed envelope and body bytes required")
    if _server_envelope(body) != envelope:
        raise errors.error_for("E-ENVELOPE")
    actual_body_hash = body_sha256(body)
    actual_content_hash = content_hash(envelope, body, admits_against)
    if claimed_body_sha256 is None:
        claimed_body_sha256 = actual_body_hash
    if claimed_content_hash is None:
        claimed_content_hash = actual_content_hash
    if (claimed_body_sha256 != actual_body_hash or
            claimed_content_hash != actual_content_hash):
        raise errors.error_for("E-ENVELOPE")
    if origin not in ("daemon", "hand", "adopted"):
        raise errors.error_for("E-ENVELOPE")

    answer = None
    with _transaction(ledger):
        replay = _replay(ledger, envelope.from_seat, submission_id,
                         actual_content_hash)
        if replay is not None:
            answer = replay
        else:
            edge_seq = _resolve_edge(ledger, admits_against)
            seq = _next_seq(ledger, "relays")
            if origin == "daemon":
                if stamp is not None or rendered_path is not None:
                    raise errors.error_for("E-ENVELOPE")
                allocated_stamp, allocated_path, foreign = _daemon_slot(
                    ledger, root, envelope, clock)
            else:
                if not isinstance(stamp, str) or not isinstance(
                        rendered_path, str):
                    raise errors.error_for("E-ENVELOPE")
                try:
                    _parse_stamp(stamp)
                except (TypeError, ValueError) as exc:
                    raise errors.error_for("E-ENVELOPE") from exc
                allocated_stamp, allocated_path, foreign = (
                    stamp, rendered_path, [])
            for callback in prechecks:
                callback(ledger, envelope, edge_seq)
            advisory_values = (advisories(ledger, envelope)
                               if callable(advisories) else advisories)
            advisory_json = jcs_encode(list(advisory_values)).decode("utf-8")
            ledger.execute(
                "INSERT INTO relays("
                "seq,submission_id,stamp,rendered_path,admits_against_seq,"
                "phase,role,dispatch_id,parent_dispatch_id,run_id,from_seat,"
                "to_seats,cc_seats,status,subject,headers_json,body,"
                "body_sha256,content_hash,origin,occupancy_ref,advisories_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (seq, submission_id, allocated_stamp, allocated_path,
                 edge_seq, envelope.phase, envelope.role,
                 envelope.dispatch_id, envelope.parent_dispatch_id,
                 envelope.run_id, envelope.from_seat,
                 jcs_encode(list(envelope.to_seats)).decode("utf-8"),
                 jcs_encode(list(envelope.cc_seats)).decode("utf-8"),
                 envelope.status, envelope.subject,
                 jcs_encode(envelope.headers).decode("utf-8"), body,
                 actual_body_hash, actual_content_hash, origin,
                 occupancy_ref, advisory_json))
            _hit(fault, "after-relay-insert")
            _insert_projection_divergences(ledger, foreign)
            _insert_cycle_events(ledger, cycle_events)
            _hit(fault, "after-cycle-events")
            _insert_supersession_edges(ledger, supersession_edges)
            _hit(fault, "after-supersession-edges")
            _insert_seat_events(ledger, seat_events, seq)
            _hit(fault, "after-seat-events")
            _hit(fault, "pre-commit")
            answer = Admission(seq, allocated_stamp, allocated_path, False)
    _hit(fault, "post-commit-pre-response", committed=True)
    return answer
