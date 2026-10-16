#!/usr/bin/env python3
"""relay-monitor: kit-side relay auto-delivery adapter (DD-v295-b2 r7).

Modes: follow, bind, drain, session-start, status, operator, replay.
Reads the run's rendered INDEX.md; never touches the engine record.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import contextvars
import errno
import fcntl
import json
import os
import platform
import re
import select
import shlex
import subprocess
import stat
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

ROOT_BUDGET = 1.0
INDEX_MAX_BYTES = 16 * 1024 * 1024
FOLLOWER_EXIT = frozenset({"transport-failed", "follower-error"})
INDEX_UNAVAILABLE = frozenset({"index-missing", "index-malformed", "index-unreadable", "root-stalled"})
SIGNATURE_STALLED = object()
NOTE_VERSION = 1
RING_SIZE = 64
FOLLOW_OUTPUT_DEADLINE = 2.0
DRAIN_DEADLINE = 5.0
PACE_LINES = int(os.environ.get("ADT_PACE_LINES") or 100)
PACE_WINDOW = float(os.environ.get("ADT_PACE_WINDOW") or 10.0)
POLL_INDEX = 1.0
POLL_NOTE = 2.0
BACKOFF = 5.0
LOCK_STEP = 0.01
TEST_CRASH_AFTER_FLUSH = os.environ.get("ADT_TEST_CRASH_AFTER_FLUSH") == "1"
TEST_CRASH_BEFORE_OUTPUT = os.environ.get("ADT_TEST_CRASH_BEFORE_OUTPUT") == "1"
TEST_CRASH_AFTER_OUTPUT = os.environ.get("ADT_TEST_CRASH_AFTER_OUTPUT") == "1"
TEST_SINK_ACCEPT = int(os.environ.get("ADT_TEST_SINK_ACCEPT") or 0)
TEST_PAUSE_BEFORE_CLEANUP = os.environ.get("ADT_TEST_PAUSE_BEFORE_CLEANUP") or None
CLAUDE_PAYLOAD_FIELDS = (
    "prompt_id",
    "effort",
    "scratchpad_dir",
    "agent_id",
    "agent_type",
)
INDEX_HEADERS = (
    "time",
    "phase",
    "role",
    "dispatch",
    "parent",
    "from",
    "to",
    "cc",
    "status",
    "file",
)
SENTINEL = {"file": None, "position": 0}


class LockTimeout(Exception):
    pass


class TransportFailed(Exception):
    pass


def valid_session_id(session) -> bool:
    return (
        isinstance(session, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session) is not None
        and ".." not in session
    )


def note_dir() -> Path:
    d = Path(os.environ.get("TMPDIR") or "/tmp") / "adt-relay-monitor"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def default_note() -> dict:
    return {
        "version": NOTE_VERSION,
        "host": None,
        "session": None,
        "seat": None,
        "root": None,
        "anchor": None,
        "bound_at": None,
        "binding_source": None,
        "bind_observation": None,
        "observation_seq": 0,
        "handled_calls": [],
        "binding_gen": 0,
        "leader": None,
        "standbys": [],
        "phase": "not-started",
        "reason": None,
        "reason_detail": None,
        "exit": None,
        "progress": {},
        "floors": [],
        "printed": {},
        "frame": None,
        "client_identity": None,
        "daemon_identity": None,
        "updated": None,
    }


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def set_reason(note: dict, reason: str | None, detail: str | None = None) -> None:
    previous = note.get("exit")
    current = note.get("reason")
    if previous and current in FOLLOWER_EXIT and current != previous.get("reason"):
        note["exit"] = dict(reason=current, detail=None, at=None, instance=None, inferred=True)
    note["reason"] = reason
    note["reason_detail"] = detail


def record_exit(note: dict, *, reason: str, detail: str | None, instance: str) -> None:
    # Only follow originates an exit; this bypasses legacy-reason inference.
    note["exit"] = dict(reason=reason, detail=detail, at=now_stamp(), instance=instance)
    note.update(phase="unavailable", reason=reason, reason_detail=None, leader=None)


def clear_exit(note: dict) -> None:
    # Only the follower that has reached following clears its prior exit.
    note["exit"] = None
    if note.get("reason") in FOLLOWER_EXIT | INDEX_UNAVAILABLE:
        note["reason"] = None
        note["reason_detail"] = None


def shown_exit_reason(note: dict) -> str | None:
    previous = note.get("exit")
    if not previous:
        return None
    current = note.get("reason")
    if current in FOLLOWER_EXIT and current != previous.get("reason"):
        return current
    return previous.get("reason")


class NoteStore:
    def __init__(self, session: str):
        if not valid_session_id(session):
            raise ValueError("invalid-session-id")
        self.session = session
        self.path = note_dir() / f"{session}.json"
        self._state_path = note_dir() / f"{session}.state"
        self.observer = None

    def owner_lock_path(self, instance: str) -> Path:
        return note_dir() / f"{self.session}.owner.{instance}"

    @contextlib.contextmanager
    def state_lock(self, timeout: float | None = None):
        if self.observer is not None:
            self.observer("lock-attempt")
        fd = os.open(self._state_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                end = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            raise LockTimeout()
                        time.sleep(LOCK_STEP)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load(self) -> dict:
        note = default_note()
        try:
            note.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            pass
        note["session"] = self.session
        return note

    def save(self, note: dict) -> None:
        note["updated"] = now_stamp()
        self._write(note)

    def _write(self, note: dict) -> None:
        """Persist under the caller's state lock without changing note fields."""
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.session}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(note, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def update(self, fn, timeout: float | None = None) -> dict:
        with self.state_lock(timeout):
            note = self.load()
            fn(note)
            self.save(note)
        return note

    def mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None


def process_start_time(pid: int) -> str | None:
    try:
        if platform.system() == "Linux":
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                return handle.read().rsplit(")", 1)[1].split()[19]
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def leader_record() -> dict:
    version, rc = run_relay(None, "version")
    identity = None
    if rc == 0 and isinstance(version, dict) and version.get("kit") and version.get("fingerprint"):
        identity = {"kit": version["kit"], "fp": version["fingerprint"]}
    return {
        "pid": os.getpid(),
        "start_time": process_start_time(os.getpid()),
        "instance": str(uuid.uuid4()),
        "identity": identity,
    }


PROC_ALIVE, PROC_DEAD, PROC_UNKNOWN = "alive", "dead", "unknown"


def process_probe(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except OverflowError:
        return PROC_DEAD
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return PROC_DEAD
        if exc.errno == errno.EPERM:
            return PROC_ALIVE
        return PROC_UNKNOWN
    return PROC_ALIVE


def process_liveness(record: dict | None) -> str:
    if not isinstance(record, dict):
        return PROC_DEAD
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return PROC_DEAD
    probe = process_probe(pid)
    if probe == PROC_DEAD:
        return PROC_DEAD
    recorded = record.get("start_time")
    observed = process_start_time(pid)
    if recorded is not None and observed is not None:
        return PROC_ALIVE if recorded == observed else PROC_DEAD
    return probe


class LeaderLock:
    def __init__(self, session: str):
        if not valid_session_id(session):
            raise ValueError("invalid-session-id")
        self._path = note_dir() / f"{session}.leader"
        self._fd = None

    def acquire(self, blocking: bool) -> bool:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def stamp(self, instance: str) -> None:
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, instance.encode("utf-8"))
        os.fsync(self._fd)

    def holder_instance(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, 0)
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class FdSink:
    accepted_total = 0  # process-wide, for TEST_SINK_ACCEPT only

    def __init__(self, fd: int):
        self.fd = fd
        with contextlib.suppress(OSError):
            os.set_blocking(fd, False)

    def write(self, data: bytes) -> int:
        if TEST_SINK_ACCEPT:
            room = TEST_SINK_ACCEPT - FdSink.accepted_total
            if room <= 0:
                return 0
            data = data[:room]
        try:
            n = os.write(self.fd, data)
            FdSink.accepted_total += n
            return n
        except BlockingIOError:
            return 0
        except InterruptedError:
            return 0

    def wait_writable(self, timeout: float) -> bool:
        _, w, _ = select.select([], [self.fd], [], max(0.0, timeout))
        return bool(w)

    def flush(self) -> None:
        return None


class ByteSink:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data: bytes) -> int:
        self.buf += data
        return len(data)

    def wait_writable(self, timeout: float) -> bool:
        return True

    def flush(self) -> None:
        return None


def resolve_session(args, payload=None) -> str | None:
    if payload is not None and "session_id" in payload:
        return payload["session_id"]
    session = getattr(args, "session", None)
    if session is not None:
        return session
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        if name in os.environ:
            return os.environ[name]
    return None


def decide_host(args, payload, environ) -> str:
    flag = getattr(args, "host", None)
    if flag and flag != "auto":
        return flag
    if environ.get("ADT_HOST") in ("codex", "claude-code"):
        return environ["ADT_HOST"]
    payload = payload or {}
    has_claude = any(k in payload for k in CLAUDE_PAYLOAD_FIELDS)
    if "turn_id" in payload and has_claude:
        return "host-ambiguous"
    env_codex = bool(
        environ.get("PLUGIN_ROOT")
        # Codex-native: the pinned plugin route exports PLUGIN_ROOT beside the CLAUDE_*
        # aliases (discovery.rs:265-270); Claude sets no PLUGIN_ROOT (measured)
    )
    env_claude = (
        bool(environ.get("CLAUDE_PLUGIN_ROOT")) and not environ.get("PLUGIN_ROOT")
        # Claude Code exports CLAUDE_PLUGIN_ROOT to plugin hooks (measured 2026-09-14,
        # v2.0.26 SessionStart); CLAUDE_PROJECT_DIR was absent there and is not
        # consulted
    )
    pay_codex = "turn_id" in payload
    pay_claude = has_claude and "turn_id" not in payload
    votes = set()
    if env_codex or pay_codex:
        votes.add("codex")
    if env_claude or pay_claude:
        votes.add("claude-code")
    if len(votes) == 2:
        return "host-ambiguous"
    return votes.pop() if votes else "host-unknown"


MODES: dict = {}


def unescape_cell(text: str) -> str | None:
    text = text.strip()
    if text == "—":
        return None
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in ("|", "\\"):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def split_cells(line: str) -> list[str] | None:
    body = line.rstrip("\n")
    if not body.startswith("|") or not body.endswith("|"):
        return None
    body = body[1:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            cur.append(ch)
            cur.append(body[i + 1])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    cells.append("".join(cur))
    return cells


@dataclass
class Row:
    position: int
    cells: dict
    raw: str


@dataclass
class IndexSnapshot:
    rows: list = field(default_factory=list)
    error: str | None = None
    partial_tail: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class RootResult:
    kind: str
    value: object = None
    detail: str | None = None


class RootBudget:
    def __init__(self, seconds: float = ROOT_BUDGET, cap: float | None = None):
        self._remaining = max(0.0, seconds)
        self.cap = cap

    def remaining(self) -> float:
        return self._remaining

    def spend(self, waited: float) -> None:
        self._remaining = max(0.0, self._remaining - max(0.0, waited))


_root_budget: contextvars.ContextVar[RootBudget | None] = contextvars.ContextVar(
    "relay_root_budget", default=None
)
_root_gate = threading.Lock()
_root_pending: tuple[threading.Event, list[RootResult]] | None = None


def current_budget() -> RootBudget | None:
    return _root_budget.get()


@contextlib.contextmanager
def use_budget(budget: RootBudget) -> Iterator[RootBudget]:
    token = _root_budget.set(budget)
    try:
        yield budget
    finally:
        _root_budget.reset(token)


@contextlib.contextmanager
def root_budget(seconds: float = ROOT_BUDGET, cap: float | None = None) -> Iterator[RootBudget]:
    budget = current_budget()
    if budget is None:
        with use_budget(RootBudget(seconds, cap)) as fresh:
            yield fresh
        return
    previous_cap = budget.cap
    if cap is not None:
        budget.cap = cap if previous_cap is None else min(previous_cap, cap)
    try:
        yield budget
    finally:
        budget.cap = previous_cap


def bounded_root_call(fn: Callable[[], object]) -> RootResult:
    """Wait for raw root work without retaining a note or a state lock.

    A completed event frees the single process-wide slot, even when its caller
    has already timed out. Only the caller reads a worker's immutable result.
    """
    global _root_pending
    budget = current_budget() or RootBudget()
    now = time.monotonic()
    deadline = now + budget.remaining()
    if budget.cap is not None:
        deadline = min(deadline, budget.cap)
    if deadline <= now:
        return RootResult("stalled")
    with _root_gate:
        if _root_pending is not None and not _root_pending[0].is_set():
            return RootResult("stalled")
        done = threading.Event()
        result: list[RootResult] = []

        def work():
            try:
                result.append(RootResult("ok", fn()))
            except OSError as exc:
                result.append(RootResult("failed", detail=errno.errorcode.get(exc.errno, type(exc).__name__)))
            except BaseException as exc:
                result.append(RootResult("failed", detail=type(exc).__name__))
            finally:
                done.set()

        _root_pending = (done, result)
        threading.Thread(target=work, daemon=True, name="relay-root-read").start()
    started_wait = time.monotonic()
    finished = done.wait(max(0.0, deadline - started_wait))
    budget.spend(time.monotonic() - started_wait)
    if not finished:
        return RootResult("stalled")
    return result[0]


def read_index(path: Path) -> IndexSnapshot:
    def read_raw():
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return IndexSnapshot(error="index-unreadable", detail="not-regular-file")
            if info.st_size > INDEX_MAX_BYTES:
                return IndexSnapshot(error="index-unreadable", detail="too-large")
            chunks, size = [], 0
            while size <= INDEX_MAX_BYTES:
                chunk = os.read(fd, INDEX_MAX_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > INDEX_MAX_BYTES:
                return IndexSnapshot(error="index-unreadable", detail="too-large")
            try:
                return b"".join(chunks).decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return IndexSnapshot(error="index-unreadable", detail="not-utf-8")
        finally:
            os.close(fd)

    result = bounded_root_call(read_raw)
    if result.kind == "stalled":
        return IndexSnapshot(error="root-stalled", detail="stalled")
    if result.kind == "failed":
        if result.detail == "ENOENT":
            return IndexSnapshot(error="index-missing")
        return IndexSnapshot(error="index-unreadable", detail=result.detail)
    if isinstance(result.value, IndexSnapshot):
        return result.value
    text = result.value
    complete_last = text.endswith("\n")
    lines = text.split("\n")
    if complete_last:
        lines = lines[:-1]
    header_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if (c := split_cells(line))
            and [x.strip() for x in c] == list(INDEX_HEADERS)
        ),
        None,
    )
    if header_idx is None:
        return IndexSnapshot(error="index-malformed")
    snap, seen, position = IndexSnapshot(), set(), 0
    data = lines[header_idx + 2 :]
    for j, line in enumerate(data):
        if not line.strip():
            continue
        if j == len(data) - 1 and not complete_last:
            snap.partial_tail = True
            break
        position += 1
        cells = split_cells(line)
        if cells is None or len(cells) != len(INDEX_HEADERS):
            snap.error = f"index-malformed-row:{position}"
            break
        decoded = {k: unescape_cell(v) for k, v in zip(INDEX_HEADERS, cells)}
        if not decoded["file"]:
            snap.error = f"index-malformed-row:{position}"
            break
        if decoded["file"] in seen:
            snap.error = f"duplicate-file-cell:{position}"
            break
        seen.add(decoded["file"])
        snap.rows.append(Row(position, decoded, line))
    return snap


def canonical(address) -> str:
    return (address or "").strip().lower()


def address_list(cell) -> list[str]:
    return [canonical(a) for a in (cell or "").split(",") if a.strip()]


def names_seat(row: Row, seat: str) -> bool:
    seat = canonical(seat)
    return seat in address_list(row.cells["to"]) or seat in address_list(
        row.cells["cc"]
    )


def is_own(row: Row, seat: str) -> bool:
    return canonical(row.cells["from"]) == canonical(seat)


def row_to_line(row: Row, root: str) -> str:
    payload = dict(row.cells)
    payload["root"] = root
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )


def locate(snapshot: IndexSnapshot, cursor: dict) -> int | None:
    if cursor.get("file") is None:
        return -1
    return next(
        (i for i, r in enumerate(snapshot.rows) if r.cells["file"] == cursor["file"]),
        None,
    )


def start_position(snapshot: IndexSnapshot, cursor: dict, anchor: str | None) -> int | None:
    start = locate(snapshot, cursor)
    return start if start is not None else locate(snapshot, {"file": anchor}) if anchor else None


@dataclass
class Binding:
    seat: str
    root: str
    anchor: str | None
    binding_gen: int
    source: str


@dataclass
class DeliverResult:
    emitted: int = 0
    aborted: str | None = None
    halted: str | None = None


def resolve_binding(note: dict, environ, args) -> Binding | None:
    seat, root = environ.get("ADT_SEAT"), environ.get("ADT_RELAY_ROOT")
    if seat and root:
        return Binding(
            seat,
            root,
            environ.get("ADT_RELAY_ANCHOR")
            or (getattr(args, "anchor", None) if args else None),
            int(note.get("binding_gen", 0)),
            "env",
        )
    if (seat or root) and not (seat and root):
        set_reason(note, "override-incomplete")
    if note.get("seat") and note.get("root"):
        return Binding(
            note["seat"],
            note["root"],
            note.get("anchor"),
            int(note.get("binding_gen", 0)),
            note.get("binding_source") or "hook",
        )
    return None


def record_floor(note: dict, root: str, seat: str, cursor: dict) -> None:
    """
    Append the (root, seat) floor once; never move, reorder, or remove it (erratum-1
    amendment B).
    """
    seat = canonical(seat)
    if not any(
        f["root"] == root and f["seat"] == seat for f in note.setdefault("floors", [])
    ):
        note["floors"].append(
            {
                "root": root,
                "seat": seat,
                "file": cursor.get("file"),
                "position": int(cursor.get("position", 0)),
            }
        )


def frame_identity(
    binding: Binding, leader_instance: str, row_file: str, owner: dict | None = None
) -> dict:
    identity = {
        "root": binding.root,
        "seat": canonical(binding.seat),
        "binding_gen": binding.binding_gen,
        "leader": leader_instance,
        "file": row_file,
    }
    if owner:
        identity["owner"] = {"lock": owner["lock"]}
    return identity


class OwnerLock:
    """
    A kernel flock held for the life of one Stop invocation; its release at process exit
    is the only death evidence used.
    """

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def hold(self, _between=None) -> bool:
        """
        Call only under the state lock: the sweep runs under that lock too, so the
        open-to-flock window is never observable (P6-R1).
        """
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        if _between is not None:
            _between()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self, unlink: bool) -> None:
        if self.fd is None:
            return
        if unlink:
            with contextlib.suppress(OSError):
                os.unlink(self.path)
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


def stream_owner(store: NoteStore, end: float, _between=None) -> dict | None:
    instance = f"drain-{uuid.uuid4().hex[:8]}"
    lock = OwnerLock(store.owner_lock_path(instance))
    try:
        with store.state_lock(
            _remaining(end)
        ):  # serialized with sweep_owner_files; bounded by the original budget
            if not lock.hold(_between):
                return None
    except LockTimeout:
        return None
    return {"instance": instance, "lock": str(lock.path), "_lock": lock}


OWNER_ALIVE, OWNER_DEAD, OWNER_UNKNOWN = "alive", "dead", "unknown"


def owner_state(frame: dict) -> str:
    """
    Non-blocking; call only under the state lock. Only a successful acquisition is death
    evidence (P4-R1).
    """
    owner = (frame or {}).get("identity", {}).get("owner") or {}
    path = owner.get("lock")
    if not path:
        return OWNER_UNKNOWN
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return OWNER_UNKNOWN  # missing or unreadable: not proof of death
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        return (
            OWNER_ALIVE
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
            else OWNER_UNKNOWN
        )
    with contextlib.suppress(OSError):
        fcntl.flock(
            fd,
            fcntl.LOCK_UN,
            # acquired: the holder is gone; release at once, leave the file (evidence)
            # in
            # place
        )
    os.close(fd)
    return OWNER_DEAD


def frame_is_stale(frame: dict) -> bool:
    return owner_state(frame) == OWNER_DEAD


def reclaim_frame(store: NoteStore, note: dict, frame: dict) -> None:
    """
    Under the state lock, after frame_is_stale: clear, persist, then remove the owner
    file (P5-R1 ordering).
    """
    path = frame.get("identity", {}).get("owner", {}).get("lock")
    note["frame"] = None
    store.save(note)  # durable first
    if path:
        with contextlib.suppress(OSError):
            os.unlink(
                path
            )  # a crash before this line leaves only an unreferenced file for the sweep


def sweep_owner_files(store: NoteStore) -> int:
    """
    Under the state lock: remove unreferenced owner files whose flock is acquirable.
    Never touches the referenced file.
    """
    removed = 0
    referenced = (
        (store.load().get("frame") or {}).get("identity", {}).get("owner") or {}
    ).get("lock")
    for path in note_dir().glob(f"{store.session}.owner.*"):
        if str(path) == referenced:
            continue
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue  # held: a live owner
        with contextlib.suppress(OSError):
            os.unlink(path)
            removed += 1
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return removed


class Pacer:
    def __init__(self, lines: int = PACE_LINES, window: float = PACE_WINDOW):
        self.lines, self.window, self.stamps = lines, window, []

    def _trim(self):
        now = time.monotonic()
        self.stamps = [t for t in self.stamps if now - t < self.window]

    def allow(self) -> bool:
        self._trim()
        return len(self.stamps) < self.lines

    def record(self) -> None:
        self.stamps.append(time.monotonic())

    def wait_time(self) -> float:
        self._trim()
        return (
            0.0
            if len(self.stamps) < self.lines
            else max(0.0, self.window - (time.monotonic() - self.stamps[0]))
        )


class FrameBusy(Exception):
    pass


def write_frame(sink, note: dict, identity: dict, data: bytes, end: float) -> bool:
    frame = note.get("frame")
    if frame:
        if frame.get("identity", {}).get("leader") != identity["leader"]:
            raise FrameBusy()
        if frame.get("identity") != identity:
            raise TransportFailed("frame-invalidated")
        accepted = int(frame["accepted"])
        data = bytes.fromhex(frame["data"])
    else:
        accepted = 0
    while accepted < len(data) and time.monotonic() < end:
        try:
            n = sink.write(data[accepted:])
        except OSError as exc:
            raise TransportFailed(str(exc)) from exc
        if not n:
            break  # zero progress: never wait under the state lock (P2b)
        accepted += int(n)
    if accepted < len(data):
        note["frame"] = {"identity": identity, "data": data.hex(), "accepted": accepted}
        return False
    try:
        sink.flush()
    except OSError as exc:
        raise TransportFailed(str(exc)) from exc
    note["frame"] = None
    return True


def _remaining(end: float) -> float:
    return max(0.0, end - time.monotonic())


def _bounded_update(store: NoteStore, fn, end: float, result: DeliverResult) -> bool:
    try:
        store.update(fn, timeout=_remaining(end))
        return True
    except LockTimeout:
        result.aborted = "lock-timeout"
        return False


def deliver_once(
    store: NoteStore,
    sink,
    binding: Binding | None,
    snapshot: IndexSnapshot,
    *,
    end: float,
    leader_instance: str,
    owner: dict | None = None,
    limit: int | None = None,
    pacer: Pacer | None = None,
    encode=None,
    start_cursor: dict | None = None,
) -> DeliverResult:
    result = DeliverResult()
    if binding is None:
        return result
    root_key, encode = binding.root, encode or (lambda b: b)
    if snapshot.error in INDEX_UNAVAILABLE:
        _bounded_update(
            store,
            lambda n: (n.update(phase="unavailable"), set_reason(n, snapshot.error, snapshot.detail)),
            end,
            result,
        )
        result.halted = result.halted or snapshot.error
        return result
    try:
        with store.state_lock(_remaining(end)):
            note = store.load()
            frame = note.get("frame")
            if frame and frame.get("identity", {}).get("leader") == leader_instance:
                ident = frame["identity"]
                if (ident.get("root"), ident.get("seat"), ident.get("binding_gen")) != (
                    binding.root,
                    canonical(binding.seat),
                    binding.binding_gen,
                ):
                    raise TransportFailed(
                        "frame-invalidated"
                        # own partial stream, binding gone: abandon even with nothing
                        # pending (R2)
                    )
            cursor = (
                dict(start_cursor)
                if start_cursor is not None
                else dict(note["progress"].get(root_key) or SENTINEL)
            )
            observed = dict(
                note["progress"].get(root_key) or SENTINEL
                # the progress this scan is based on; a rebase may replace exactly this
                # (final review FR2)
            )
    except LockTimeout:
        result.aborted = "lock-timeout"
        return result
    start = start_position(snapshot, cursor, binding.anchor)
    if locate(snapshot, cursor) is None:
        if start is None:
            _bounded_update(
                store,
                lambda n: (n.update(phase="unavailable"), set_reason(n, "anchor-not-found")),
                end,
                result,
            )
            result.halted = "anchor-not-found"
            return result
        cursor = {
            "file": snapshot.rows[start].cells["file"],
            "position": snapshot.rows[start].position,
        }
        try:
            with store.state_lock(
                _remaining(end)
                # compare-and-update: a rebase never overwrites a newer binding's or
                # another
                # consumer's cursor (final review FR2)
            ):
                n = store.load()
                live_seat = (
                    canonical(n.get("seat"))
                    if binding.source != "env"
                    else canonical(binding.seat)
                )
                live_root = n.get("root") if binding.source != "env" else binding.root
                frame = n.get("frame")
                mine = (
                    bool(frame)
                    and frame.get("identity", {}).get("leader") == leader_instance
                )
                if (
                    int(n.get("binding_gen", 0)) != binding.binding_gen
                    or live_seat != canonical(binding.seat)
                    or live_root != binding.root
                ):
                    if mine:
                        raise TransportFailed("frame-invalidated")
                    result.aborted = "binding-changed"
                    return result
                if dict(n["progress"].get(root_key) or SENTINEL) != observed:
                    if mine:
                        raise TransportFailed("frame-invalidated")
                    result.aborted = "progress-moved"
                    return result
                n["progress"][root_key] = dict(cursor)
                set_reason(n, "rebased")
                store.save(n)
        except LockTimeout:
            result.aborted = "lock-timeout"
            return result
    for offset, row_ in enumerate(snapshot.rows[start + 1 :]):
        if limit is not None and result.emitted >= limit:
            return result
        if time.monotonic() >= end:
            result.aborted = "deadline"
            return result
        prev_file = (
            snapshot.rows[start + offset].cells["file"] if start + offset >= 0 else None
        )
        addressed = names_seat(row_, binding.seat) and not is_own(row_, binding.seat)
        if addressed and pacer is not None and not pacer.allow():
            result.aborted = "paced"
            return result
        try:
            lock = store.state_lock(_remaining(end))
            lock.__enter__()
        except LockTimeout:
            result.aborted = "lock-timeout"
            return result
        try:
            note = store.load()
            frame = note.get("frame")
            if (
                frame
                and frame.get("identity", {}).get("leader") != leader_instance
                and frame_is_stale(frame)
            ):
                reclaim_frame(store, note, frame)
                # dead owner: clear, save, then unlink; the row restarts at byte zero
                # (R1, P5-R1)
                frame = None
            mine = (
                bool(frame)
                and frame.get("identity", {}).get("leader") == leader_instance
            )
            live_seat = (
                canonical(note.get("seat"))
                if binding.source != "env"
                else canonical(binding.seat)
            )
            live_root = note.get("root") if binding.source != "env" else binding.root
            if (
                int(note.get("binding_gen", 0)) != binding.binding_gen
                or live_seat != canonical(binding.seat)
                or live_root != binding.root
            ):
                if mine:
                    raise TransportFailed("frame-invalidated")
                result.aborted = "binding-changed"
                return result
            cur = note["progress"].get(root_key) or SENTINEL
            if (
                cur.get("file") is not None
                and int(cur.get("position", 0)) >= row_.position
            ):
                if mine:
                    raise TransportFailed("frame-invalidated")
                result.aborted = "progress-moved"
                return result
            if (cur.get("file") or None) != prev_file:
                if mine:
                    raise TransportFailed("frame-invalidated")
                result.aborted = "snapshot-order"
                return result
            if addressed:
                identity = frame_identity(
                    binding, leader_instance, row_.cells["file"], owner
                )
                if frame and not mine:
                    raise FrameBusy()
                if mine and frame.get("identity") != identity:
                    raise TransportFailed("frame-invalidated")
                if TEST_CRASH_BEFORE_OUTPUT:
                    os._exit(9)
                if not write_frame(
                    sink,
                    note,
                    identity,
                    encode(row_to_line(row_, binding.root).encode("utf-8")),
                    end,
                ):
                    note.update(phase="unavailable")
                    set_reason(note, "output-stalled")
                    store.save(note)
                    result.aborted = "output-stalled"
                    return result
                if TEST_CRASH_AFTER_FLUSH or TEST_CRASH_AFTER_OUTPUT:
                    os._exit(9)
                ring = note["printed"].setdefault(root_key, [])
                ring.append(row_.cells["file"])
                del ring[:-RING_SIZE]
                result.emitted += 1
                if pacer is not None:
                    pacer.record()
            elif frame and not mine:
                raise FrameBusy()
            note["progress"][root_key] = {
                "file": row_.cells["file"],
                "position": row_.position,
            }
            if note.get("reason") in ("output-stalled", "anchor-not-found"):
                set_reason(note, None)  # `rebased` stays visible until the next binding
            note["phase"] = "following"
            store.save(note)
        except FrameBusy:
            result.aborted = "frame-busy"
            return result
        finally:
            lock.__exit__(None, None, None)
    if snapshot.error:
        _bounded_update(
            store,
            lambda n: (n.update(phase="unavailable"), set_reason(n, snapshot.error, snapshot.detail)),
            end,
            result,
        )
        result.halted = snapshot.error
    if (
        binding is not None and not result.aborted and not result.halted
        # a completed scan, even with no candidate row, is healthy following (final
        # review
        # FR1)
    ):
        try:
            with store.state_lock(_remaining(end)):
                n = store.load()
                live_seat = (
                    canonical(n.get("seat"))
                    if binding.source != "env"
                    else canonical(binding.seat)
                )
                live_root = n.get("root") if binding.source != "env" else binding.root
                frame = n.get("frame")
                foreign = (
                    bool(frame)
                    and frame.get("identity", {}).get("leader") != leader_instance
                )
                if (
                    int(n.get("binding_gen", 0)) == binding.binding_gen
                    and live_seat == canonical(binding.seat)
                    and live_root == binding.root
                    and not foreign
                    and n.get("phase") != "following"
                ):
                    n["phase"] = "following"
                    store.save(n)
        except LockTimeout:
            result.aborted = "lock-timeout"
    return result


def index_signature(path: Path):
    result = bounded_root_call(path.stat)
    if result.kind == "ok":
        st = result.value
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    if result.kind == "failed" and result.detail == "ENOENT":
        return None
    return SIGNATURE_STALLED


def follow(args) -> int:
    session = resolve_session(args)
    if not valid_session_id(session):
        print("relay-monitor: invalid-session-id", file=sys.stderr)
        return 2
    host = decide_host(args, None, os.environ)
    store, leader, me = NoteStore(session), LeaderLock(session), leader_record()
    if not leader.acquire(blocking=False):
        store.update(
            lambda n: n["standbys"].append(me)
        )  # standbys register themselves only; `phase` is leader-owned (plan-19)
        leader.acquire(blocking=True)
        store.update(
            lambda n: n.update(
                {
                    "standbys": [
                        s for s in n["standbys"] if s.get("instance") != me["instance"]
                    ]
                }
            )
        )
    leader.stamp(me["instance"])
    store.update(
        lambda n: n.update(
            {
                "leader": me,
                "host": host if host in ("claude-code", "codex") else n.get("host"),
                "phase": "waiting-for-binding",
                "frame": None,
            }
        )
    )  # a new leader's stream starts at byte zero
    with store.state_lock():
        sweep_owner_files(store)  # eventual cleanup of unreferenced owner files (P5-R1)
    sink, pacer = FdSink(sys.stdout.fileno()), Pacer()
    last_sig = last_note = None
    last_error = None
    pending = True
    backoff_until = 0.0
    try:
        while True:
            with root_budget():
                note = store.load()
                binding = resolve_binding(note, os.environ, args)
                if binding is None:
                    store.update(lambda n: n.update({"phase": "waiting-for-binding"}))
                    time.sleep(POLL_NOTE)
                    continue
                if binding.source == "env":
                    if (
                        binding.root not in note["progress"]
                    ):  # per-root progress initialization (once per root)
                        snap = read_index(Path(binding.root) / "INDEX.md")
                        if snap.error in INDEX_UNAVAILABLE and (binding.anchor or snap.error != "index-missing"):
                            last_error = snap.error
                            store.update(lambda n: (n.update(phase="unavailable"), set_reason(n, snap.error, snap.detail)))
                            time.sleep(POLL_INDEX)
                            continue
                        anchor = (
                            binding.anchor
                            if binding.anchor
                            else (snap.rows[-1].cells["file"] if snap.rows else None)
                        )
                        pos = next(
                            (r.position for r in snap.rows if r.cells["file"] == anchor), 0
                        )
                        cur = {"file": anchor, "position": pos}
                        store.update(
                            lambda n: (
                                n["progress"].__setitem__(binding.root, dict(cur)),
                                n.update({"anchor": anchor}),
                            )
                        )
                        note = store.load()
                        pending = True
                    env_seat = canonical(
                        binding.seat
                    )  # per-pair floor and binding metadata (once per pair; D8-R1)
                    has_floor = any(
                        f["root"] == binding.root and f["seat"] == env_seat
                        for f in note.get("floors", [])
                    )
                    if (
                        not has_floor
                        or canonical(note.get("seat")) != env_seat
                        or note.get("root") != binding.root
                        or note.get("binding_source") != "env"
                    ):
                        cur = dict(
                            note["progress"][binding.root]
                            # existing progress is preserved; the new pair's floor is the
                            # current cursor
                        )
                        store.update(
                            lambda n: (
                                record_floor(n, binding.root, binding.seat, cur),
                                n.update(
                                    {
                                        "seat": binding.seat,
                                        "root": binding.root,
                                        "binding_source": "env",
                                        "anchor": binding.anchor or n.get("anchor"),
                                    }
                                ),
                            )
                        )
                        note = store.load()
                        pending = True
                sig, nsig = index_signature(Path(binding.root) / "INDEX.md"), store.mtime()
                wake = (
                    pending
                    or last_error in INDEX_UNAVAILABLE
                    or sig is SIGNATURE_STALLED
                    or sig != last_sig
                    or nsig != last_note
                    or bool(note.get("frame"))
                    or (backoff_until and time.monotonic() >= backoff_until)
                )
                if not wake:
                    time.sleep(min(POLL_INDEX, max(0.05, pacer.wait_time() or POLL_INDEX)))
                    continue
                if time.monotonic() < backoff_until:
                    time.sleep(min(POLL_INDEX, backoff_until - time.monotonic()))
                    continue
                last_sig, last_note, backoff_until, pending = sig, nsig, 0.0, False
                snap = read_index(Path(binding.root) / "INDEX.md")
                last_error = snap.error
                result = deliver_once(
                    store,
                    sink,
                    binding,
                    snap,
                    end=time.monotonic() + FOLLOW_OUTPUT_DEADLINE,
                    leader_instance=me["instance"],
                    pacer=pacer,
                )
                with store.state_lock():
                    recovered = store.load()
                    if (
                        recovered.get("phase") == "following"
                        and (recovered.get("leader") or {}).get("instance") == me["instance"]
                        and (recovered.get("exit") or recovered.get("reason") in FOLLOWER_EXIT | INDEX_UNAVAILABLE)
                    ):
                        clear_exit(recovered)
                        store.save(recovered)
                if result.aborted == "paced":
                    pending = True
                    time.sleep(min(POLL_INDEX, pacer.wait_time()))
                elif result.aborted == "output-stalled":
                    pending = True
                    sink.wait_writable(
                        BACKOFF
                    )  # wait for the sink outside the lock, then retry the same frame
                elif result.aborted in (
                    "binding-changed",
                    "progress-moved",
                    "snapshot-order",
                    "lock-timeout",
                    "frame-busy",
                    "deadline",
                ):
                    pending = True
                    time.sleep(0.1)
                elif result.halted:
                    time.sleep(POLL_INDEX)
    except TransportFailed as exc:
        store.update(
            lambda n: record_exit(n, reason="transport-failed", detail=str(exc), instance=me["instance"])
        )
        leader.release()
        print(f"relay-monitor: transport failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        try:
            store.update(lambda n: record_exit(n, reason="follower-error", detail=type(exc).__name__, instance=me["instance"]), timeout=2.0)
        except Exception as failure:
            print(f"relay-monitor: follower error: {type(exc).__name__} (not recorded: {type(failure).__name__})", file=sys.stderr)
        else:
            print(f"relay-monitor: follower error: {type(exc).__name__}", file=sys.stderr)
        finally:
            leader.release()
        raise


MODES["follow"] = follow


@dataclass
class SubmitParse:
    ok: bool
    reason: str | None = None
    key: str | None = None
    root_operand: str | None = None
    cd_prefix: str | None = None


RELAY_WORDS = ("relay", "tools/relay")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT_OPS = (">", ">>", ">|", "<>")
UNSUPPORTED_OPS = ("|", "||", "&", "(", ")", "<", "<<")


def _shell_lex():
    import importlib.util

    path = Path(__file__).resolve().parent / "shell_lex.py"
    spec = importlib.util.spec_from_file_location("adt_shell_lex", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("adt_shell_lex", module)
    spec.loader.exec_module(module)
    return module


def is_assignment_word(sl, text: str, quoted) -> bool:
    m = ASSIGN_RE.match(text)
    return bool(m) and m.end() <= sl.unquoted_prefix(text, quoted)


def is_interpreter_word(text: str, quoted) -> bool:
    return (
        not quoted
        and re.fullmatch(r"(?:python|python3(?:\.[0-9]+)?|py)", text.rsplit("/", 1)[-1])
        is not None
    )


def parse_submit_command(command: str) -> SubmitParse:
    sl = _shell_lex()
    uncertain = False
    try:
        command = sl.strip_heredoc_bodies(command)
    except sl.Uncertain:
        # Inspect raw command positions only when the pre-pass cannot decide.
        uncertain = True
    tokens = sl.lex_spans(command)
    malformed = tokens is None
    if malformed:
        # Close only the unfinished quoted word to inspect preceding commands.
        tokens = sl.lex_spans(command + "\n'")
        if tokens is None:
            tokens = sl.lex_spans(command + '\n"')
        tokens = tokens or []
    # Remove fd-duplication redirections before interpreting submit operands.
    # Offsets distinguish an IO number from a separate numeric argument.
    words_with_spans = []
    duplication_error = False
    j = 0
    while j < len(tokens):
        token = tokens[j]
        if token[2] and token[0] == ">&":
            if words_with_spans:
                previous = words_with_spans[-1]
                if (not previous[1] and not previous[2]
                        and re.fullmatch(r"[0-9]+", previous[0])
                        and previous[4] == token[3]):
                    words_with_spans.pop()
            operand = tokens[j + 1] if j + 1 < len(tokens) else None
            if (operand is None or operand[2]
                    or re.fullmatch(r"(?:[0-9]+|-)", operand[0]) is None):
                duplication_error = True
                j += 1
            else:
                j += 2
            continue
        words_with_spans.append(token)
        j += 1
    tokens = [token[:3] for token in words_with_spans]
    unsupported = (
        duplication_error
        or uncertain
        or malformed
        or any(op and text in UNSUPPORTED_OPS for text, _quoted, op in tokens)
    )
    boundaries = (*sl.SEPARATORS, "|", "||", "&", "(", ")")
    commands, current = [], []
    for token in tokens:
        if token[2] and token[0] in boundaries:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    cd_prefix, submit, count, near_miss = None, None, 0, False
    for i, cmd in enumerate(commands):
        words = [(t, q) for t, q, op in cmd if not op or t in REDIRECT_OPS]
        assigns = []
        while words and is_assignment_word(sl, *words[0]):
            assigns.append(words[0][0])
            words = words[1:]
        texts = [t for t, _q in words]
        if texts and texts[0] == "cd" and not words[0][1]:
            if i != 0:
                unsupported = True
            cd_prefix = texts[1] if len(texts) > 1 else None
            continue
        if words and is_interpreter_word(*words[0]):
            candidate = words[1:]
        else:
            candidate = words
        if (
            len(candidate) >= 2
            and not candidate[0][1]
            and (candidate[0][0] in RELAY_WORDS or candidate[0][0].endswith("/relay"))
            and candidate[1][0] == "submit"
            and not candidate[1][1]
        ):
            count += 1
            submit = (candidate, assigns)
        elif words:
            # Locate the script operand for these named launchers only; arbitrary
            # wrappers, aliases and shell runtime resolution are not inferred.
            pending = words
            wrapped = False
            if not pending[0][1] and pending[0][0].rsplit("/", 1)[-1] == "env":
                wrapped = True
                pending = pending[1:]
                while pending:
                    option = pending[0][0]
                    if option in ("-i", "--ignore-environment"):
                        pending = pending[1:]
                    elif option in ("-u", "--unset") and len(pending) > 1:
                        pending = pending[2:]
                    elif option.startswith("--unset="):
                        pending = pending[1:]
                    elif option == "--":
                        pending = pending[1:]
                        break
                    elif is_assignment_word(sl, *pending[0]):
                        pending = pending[1:]
                    else:
                        break
            if (
                len(pending) >= 2
                and not pending[0][1]
                and pending[0][0].rsplit("/", 1)[-1] == "uv"
                and pending[1] == ("run", False)
            ):
                wrapped = True
                pending = pending[2:]
            if pending and is_interpreter_word(*pending[0]):
                wrapped = True
                pending = pending[1:]
                while pending and pending[0][0].startswith("-"):
                    option = pending[0][0]
                    pending = pending[1:]
                    if option == "--":
                        break
                    # c/m consume command/module data; h/V terminate. Unknown
                    # options cannot establish that a script will be executed.
                    flags = option[1:]
                    if not flags or flags.startswith("-"):
                        pending = []
                        break
                    for index, flag in enumerate(flags):
                        if flag in "cmhV" or flag not in "bBdEiIOPqRsSuvxWX":
                            pending = []
                            break
                        if flag in "WX":
                            if index == len(flags) - 1:
                                pending = pending[1:]
                            break
            if (
                wrapped
                and len(pending) >= 2
                and not pending[0][1]
                and (pending[0][0] in RELAY_WORDS or pending[0][0].endswith("/relay"))
                and pending[1][0] == "submit"
                and not pending[1][1]
            ):
                near_miss = True
    if count == 0:
        return SubmitParse(False, "submit-near-miss" if near_miss else "no-submit")
    if unsupported:
        return SubmitParse(False, "unsupported-syntax")
    if count > 1:
        return SubmitParse(False, "ambiguous-command")
    words, assigns = submit
    key = root = None
    j = 2
    while j < len(words):
        t, q = words[j]
        if (
            not q
            and t in REDIRECT_OPS
            or (
                not q
                and re.match(r"^\d+$", t)
                and j + 1 < len(words)
                and words[j + 1][0] in REDIRECT_OPS
            )
        ):
            j += 2 if t in REDIRECT_OPS else 3
            continue
        if t == "--key" and not q and j + 1 < len(words):
            key = words[j + 1][0]
            j += 2
            continue
        if t == "--root" and not q and j + 1 < len(words):
            root = words[j + 1][0]
            j += 2
            continue
        j += 1
    for a in assigns:
        if a.startswith("RELAY_KEY=") and key is None:
            key = a.split("=", 1)[1]
    return SubmitParse(True, None, key, root, cd_prefix)


def response_strings(response) -> list[str]:
    if isinstance(response, str):
        return [response]
    out = []
    stdout_first = isinstance(response, dict) and isinstance(
        response.get("stdout"), str
    )
    if stdout_first:
        out.append(response["stdout"])  # the prioritized leaf, once, by structure

    def walk(v, top=False):
        if isinstance(v, str):
            out.append(
                v
                # every occurrence; equal strings at distinct leaves stay distinct
                # (plan-22)
            )
        elif isinstance(v, dict):
            for k, x in v.items():  # insertion order == document order; never sorted
                if top and stdout_first and k == "stdout":
                    continue  # already emitted first; skipped by key, not by value
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(response, top=True)
    return out


def extract_receipts(response) -> list[dict]:
    receipts, decoder = [], json.JSONDecoder()
    for text in response_strings(response):
        i = 0
        while True:
            i = text.find("{", i)
            if i < 0:
                break
            try:
                obj, end = decoder.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            if isinstance(obj, dict) and {"path", "render_state", "duplicate"} <= set(
                obj
            ):
                receipts.append(obj)
            i = end
    return receipts


def engine_client(plugin_root: str | None):
    import importlib

    candidates = ([Path(plugin_root) / "tools"] if plugin_root else []) + [
        Path(__file__).resolve().parent.parent,
        Path.home() / ".claude" / "skills" / "tools",
    ]
    for c in candidates:
        if (c / "relay_engine" / "client.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            return importlib.import_module("relay_engine.client")
    return None


def discover_root_like_cli(
    start: str | None, cwd: str, plugin_root: str | None = None
) -> str | None:
    try:
        base = (
            os.path.join(cwd, start) if start and not os.path.isabs(start) else start
        ) or cwd
        client = engine_client(plugin_root)
        if client is not None:
            try:
                return client.discover_root(base)
            except FileNotFoundError:
                return None
        current = os.path.realpath(base)
        while True:
            if os.path.isdir(os.path.join(current, ".engine")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
    except (OSError, ValueError):
        return None


def seat_from_key(key: str | None) -> str | None:
    if not key:
        return None
    parts = Path(key).parts
    if ".engine" in parts and "seats" in parts:
        i = parts.index("seats")
        return parts[i + 1] if i + 1 < len(parts) else None
    return None


def path_under_root(root: str, rel: str) -> bool:
    try:
        if not rel or os.path.isabs(rel) or ".." in Path(rel).parts:
            return False
        target = os.path.realpath(os.path.join(root, rel))
        return target.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(target)
    except (OSError, ValueError):
        return False


def call_identity(session: str, payload: dict) -> str | None:
    value = payload.get("tool_use_id")
    return json.dumps([session, value], separators=(",", ":")) if isinstance(value, str) and value else None


def names_submit_verb(command: str) -> bool:
    sl = _shell_lex()
    try:
        command = sl.strip_heredoc_bodies(command)
    except sl.Uncertain:
        # Match the grammar's recovery; uncertainty does not prove no filing.
        pass
    tokens = sl.lex(command)
    if tokens is None:
        tokens = sl.lex(command + "\n'") or sl.lex(command + '\n"') or []
    return any(
        not quoted and not op and (word in RELAY_WORDS or word.endswith("/relay"))
        and following == ("submit", False, False)
        for (word, quoted, op), following in zip(tokens, tokens[1:])
    )


def root_indications(command: str) -> tuple[list[tuple[str, str, bool]], bool]:
    """Read contrary root evidence without extending the submit grammar."""
    sl = _shell_lex()
    try:
        command = sl.strip_heredoc_bodies(command)
    except sl.Uncertain:
        pass
    tokens = sl.lex_spans(command)
    if tokens is None:
        tokens = sl.lex_spans(command + "\n'") or sl.lex_spans(command + '\n"') or []

    def operand(raw):
        quote, dynamic, i = None, False, 0
        while i < len(raw):
            char = raw[i]
            if char == "\\" and quote != "'":
                i += 2
                continue
            if quote:
                if char == quote:
                    quote = None
                elif quote == '"' and char in "$`":
                    dynamic = True
            elif char in "'\"":
                quote = char
            elif char in "$`*?[":
                dynamic = True
            i += 1
        try:
            decoded = shlex.split(raw)
        except ValueError:
            return raw, raw, True
        return raw, decoded[0] if len(decoded) == 1 else "", dynamic or len(decoded) != 1

    roots, directory = [], False
    for i, (word, quoted, operator, start, end) in enumerate(tokens):
        if operator:
            continue
        if not quoted and word in ("cd", "pushd"):
            directory = True
        if not quoted and word == "--root":
            if i + 1 < len(tokens) and not tokens[i + 1][2]:
                following = tokens[i + 1]
                roots.append(operand(command[following[3]:following[4]]))
        elif word.startswith("--root=") and sl.unquoted_prefix(word, quoted) >= 7:
            roots.append(operand(command[start + 7:end]))
    return roots, directory


def receipt_row_lookup(root: str, receipt: str) -> str:
    try:
        snap = read_index(Path(root) / "INDEX.md")
    except (OSError, ValueError):
        return "unreadable"
    if snap.error:
        return "unreadable"
    return "match" if any(row.cells["file"] == receipt for row in snap.rows) else "no-match"


def attribute_root(parsed: SubmitParse, cwd: str, note: dict, receipts: list[dict], plugin_root: str | None, indications: tuple[list[tuple[str, str, bool]], bool]) -> tuple[str | None, str]:
    if parsed.cd_prefix:
        cwd = os.path.join(cwd, parsed.cd_prefix)
    if parsed.root_operand:
        root = discover_root_like_cli(parsed.root_operand, cwd, plugin_root)
        if root:
            return root, "command"
        return None, "none"
    if len(receipts) != 1:
        return None, "none"
    candidates = {root for root in (discover_root_like_cli(None, cwd, plugin_root), note.get("root")) if root}
    matches = []
    for root in candidates:
        lookup = receipt_row_lookup(root, receipts[0]["path"])
        if lookup == "unreadable":
            return None, "none"
        if lookup == "match":
            matches.append(root)
    if len(matches) != 1:
        return None, "none"
    operands, directory = indications
    if directory or len({value for _, value, _ in operands}) > 1:
        return None, "none"
    candidate = os.path.realpath(matches[0])
    for _, value, dynamic in operands:
        if dynamic or not value or os.path.realpath(os.path.join(cwd, value)) != candidate:
            return None, "none"
    return matches[0], "inferred"


def _remember_call(note: dict, call: str | None) -> None:
    if call:
        note["handled_calls"] = (note.get("handled_calls", []) + [call])[-8:]


def binding_key(note: dict) -> tuple:
    return (canonical(note.get("seat")), note.get("root"), note.get("anchor"),
            note.get("binding_source"), int(note.get("binding_gen") or 0))


def record_observation(note: dict, *, reason: str, receipts: list[str], root: str | None, provenance: str, call: str | None, command_roots: list[str] | None = None) -> bool:
    if call and call in note.get("handled_calls", []):
        return False
    record = dict(reason=reason, receipts=receipts, root=root, root_provenance=provenance, call=call)
    if command_roots:
        record["command_roots"] = [text[:200] for text in command_roots]
    old = note.get("bind_observation") or {}
    if call is None and all(old.get(k) == v for k, v in record.items()) and old.get("command_roots", []) == record.get("command_roots", []):
        return False
    note["observation_seq"] = int(note.get("observation_seq") or 0) + 1
    note["bind_observation"] = dict(record, stamp=now_stamp(), seq=note["observation_seq"])
    _remember_call(note, call)
    return True


def clear_observations(note: dict) -> None:
    note["observation_seq"] = int(note.get("observation_seq") or 0) + 1
    note["bind_observation"] = None
    for key in ("binding_degraded", "submit_miss", "submit_miss_binding_gen"):
        note.pop(key, None)


def bind_from_receipt(
    store: NoteStore, *, root: str, receipt_path: str, key_seat: str | None, source: str, call: str | None = None
) -> tuple[bool, str]:
    if not isinstance(receipt_path, str) or not path_under_root(root, receipt_path):
        return False, "receipt-mismatch"
    try:
        snap = read_index(Path(root) / "INDEX.md")
    except (OSError, ValueError):
        return False, "receipt-mismatch"
    row_ = next((r for r in snap.rows if r.cells["file"] == receipt_path), None)
    if snap.error or row_ is None or not row_.cells["from"]:
        return False, "receipt-mismatch"
    seat = row_.cells["from"]
    if key_seat and canonical(key_seat) != canonical(seat):
        return False, "key-mismatch"
    cursor = {"file": receipt_path, "position": row_.position}
    with store.state_lock():
        n = store.load()
        if source == "hook" and call and call in n.get("handled_calls", []):
            return True, "handled"
        fresh = not (canonical(n.get("seat")) == canonical(seat) and n.get("root") == root)
        changed = fresh or n.get("anchor") != receipt_path or root not in n["progress"]
        if source == "hook" and not changed:
            if call:
                _remember_call(n, call)
                store._write(n)
            return True, "unchanged"
        clear_observations(n)
        n.update(seat=seat, root=root, anchor=receipt_path, bound_at=now_stamp(), binding_source=source)
        if changed:
            n["binding_gen"] = int(n.get("binding_gen", 0)) + 1
        if fresh or root not in n["progress"]:
            n["progress"][root] = dict(cursor)
            # A follower retains ownership of its partially written frame.
            n["printed"][root] = []
        record_floor(n, root, seat, cursor)
        _remember_call(n, call)
        store.save(n)
    return True, "bound"


def hook_context(line: str) -> bytes:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": line}}, ensure_ascii=True).encode("utf-8")


def bind(args) -> int:
    if getattr(args, "manual", False):
        session = resolve_session(args)
        if not valid_session_id(session):
            print("relay-monitor: invalid-session-id", file=sys.stderr)
            return 2
        if not args.root or not args.anchor:
            print("relay-monitor: manual bind needs --session/--root/--anchor", file=sys.stderr)
            return 2
        root = discover_root_like_cli(args.root, os.getcwd(), args.plugin_root) or args.root
        bound, reason = bind_from_receipt(NoteStore(session), root=root, receipt_path=args.anchor, key_seat=None, source="manual")
        if not bound:
            print("relay-monitor: manual bind refused: " + printable_text(reason), file=sys.stderr)
            return 1
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError, RecursionError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session = resolve_session(args, payload)
    if not valid_session_id(session):
        return 0
    if payload.get("hook_event_name") != "PostToolUse" or "tool_response" not in payload:
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str):
        return 0
    parsed = parse_submit_command(command)
    if not parsed.ok and not names_submit_verb(command):
        return 0
    receipts = extract_receipts(payload.get("tool_response"))
    receipts = [r for r in receipts if isinstance(r.get("path"), str)]
    if not receipts:
        return 0
    indications = root_indications(command)
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        cwd = os.getcwd()
    store = NoteStore(session)
    call = call_identity(session, payload)
    reason = parsed.reason if parsed.reason != "no-submit" else "submit-near-miss"
    if parsed.ok:
        base = os.path.join(cwd, parsed.cd_prefix) if parsed.cd_prefix else cwd
        root = discover_root_like_cli(parsed.root_operand, base, args.plugin_root)
        if root is None:
            reason = "no-root"
        elif len(receipts) != 1:
            reason = "ambiguous-receipt"
        else:
            ok, reason = bind_from_receipt(store, root=root, receipt_path=receipts[0]["path"], key_seat=seat_from_key(parsed.key), source="hook", call=call)
            if ok:
                return 0
    observed = store.load()
    before = binding_key(observed)
    root, provenance = attribute_root(parsed, cwd, observed, receipts, args.plugin_root, indications)
    with store.state_lock():
        note = store.load()
        if call and call in note.get("handled_calls", []):
            return 0
        if binding_key(note) != before:
            _remember_call(note, call)
            store.save(note)
            return 0
        if not record_observation(note, reason=reason, receipts=[r["path"] for r in receipts], root=root, provenance=provenance, call=call, command_roots=[raw for raw, _, _ in indications[0]]):
            return 0
        store.save(note)
        seq = note["observation_seq"]
    hint = rebind_line(note, session)
    with store.state_lock():
        current = store.load()
        if binding_key(current) != before or (current.get("bind_observation") or {}).get("seq") != seq:
            return 0
    if hint:
        print(hook_context(hint).decode("utf-8"), flush=True)
    return 0



MODES["bind"] = bind


def read_payload() -> dict:
    try:
        return json.load(sys.stdin) or {}
    except (ValueError, OSError, RecursionError):
        return {}


def emit_json(doc: dict) -> int:
    try:
        sys.stdout.write(json.dumps(doc))
        sys.stdout.flush()
    except OSError:
        return 1
    return 0


def stop_document(line: bytes) -> bytes:
    text = line.decode("utf-8").rstrip("\n")
    return json.dumps(
        {
            "decision": "block",
            "reason": "Relay arrival delivered by relay-monitor; read the f"
            "ile before acting:\n" + text,
        }
    ).encode("utf-8")


def drain(args) -> int:
    end = time.monotonic() + float(
        getattr(args, "deadline", DRAIN_DEADLINE)
    )  # one absolute deadline for everything below (P2b)
    payload = read_payload()
    if decide_host(args, payload, os.environ) != "codex":
        return emit_json({})
    session = resolve_session(args, payload)
    if not valid_session_id(session):
        return emit_json({})
    store = NoteStore(session)
    note = store.load()
    binding = resolve_binding(note, os.environ, args)
    if binding is None or time.monotonic() >= end:
        return emit_json({})
    snap = read_index(Path(binding.root) / "INDEX.md")
    if time.monotonic() >= end:
        return emit_json({})
    sink = FdSink(sys.stdout.fileno())
    me = stream_owner(store, end)
    if me is None:
        return emit_json({})
    try:
        return _drain_locked(store, sink, binding, snap, end, me)
    finally:
        me["_lock"].release(unlink=me.get("cleared", True))


def _drain_locked(
    store: NoteStore, sink, binding: Binding, snap: IndexSnapshot, end: float, me: dict
) -> int:
    try:
        result = deliver_once(
            store,
            sink,
            binding,
            snap,
            end=end,
            leader_instance=me["instance"],
            owner=me,
            limit=1,
            encode=stop_document,
        )
    except TransportFailed:
        me["cleared"] = False
        return 1
    if result.emitted == 1:
        return 0
    if result.aborted == "output-stalled":
        if TEST_PAUSE_BEFORE_CLEANUP:
            pause_until = time.monotonic() + 10.0
            while (
                not os.path.exists(TEST_PAUSE_BEFORE_CLEANUP)
                and time.monotonic() < pause_until
            ):
                time.sleep(0.01)

        def clear_own(n):
            if (
                n.get("frame")
                and n["frame"].get("identity", {}).get("leader") == me["instance"]
            ):
                n["frame"] = None
                set_reason(n, None)

        try:
            store.update(
                clear_own, timeout=_remaining(end)
            )  # only the remaining original budget (R3)
        except LockTimeout:
            me["cleared"] = (
                # the frame and its owner-lock file stay; the flock dies with this
                # process, so the next consumer finds `dead` (P4-R1)
                False
            )
        return 1  # a partial Stop document is abandoned; nothing more is written
    return emit_json({})


MODES["drain"] = drain


def run_relay(plugin_root: str | None, *argv, timeout=10.0):
    relay = (
        Path(plugin_root) / "tools" / "relay"
        if plugin_root
        else Path(__file__).resolve().parent.parent / "relay"
    )
    try:
        proc = subprocess.run(
            ["python3", str(relay), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"code": "E-EXEC", "detail": str(exc)}, 1
    text = proc.stdout.strip() or proc.stderr.strip()
    try:
        return json.loads(text), proc.returncode
    except ValueError:
        return {"raw": proc.stderr.strip() or text}, proc.returncode


def check_identity(plugin_root: str | None, root: str | None):
    client, rc = run_relay(plugin_root, "version")
    client = client if isinstance(client, dict) else {}
    client_id = (
        {"kit": client.get("kit"), "fp": client.get("fingerprint")}
        if rc == 0
        else {"kit": None, "fp": None}
    )
    if not root or not os.path.isdir(os.path.join(root, ".engine")):
        return "hand-root", client_id, None
    status_doc, rc = run_relay(plugin_root, "status", "--root", root)
    status_doc = status_doc if isinstance(status_doc, dict) else {}
    if rc != 0:
        raw = status_doc.get("raw") or ""
        lines = raw.splitlines() if isinstance(raw, str) else []
        code = status_doc.get("code") or (lines[0] if lines else "")
        reason = (
            code[2:].lower()
            if isinstance(code, str) and re.fullmatch(r"E-[A-Z0-9-]+", code)
            else "unknown"
        )
        return reason, client_id, status_doc
    daemon_doc = status_doc.get("daemon")
    ident = daemon_doc.get("identity") if isinstance(daemon_doc, dict) else None
    ident = ident if isinstance(ident, dict) else {}
    daemon = {"kit": ident.get("kit"), "fp": ident.get("fp")}
    if not all(
        isinstance(value, str) and value
        for identity in (client_id, daemon)
        for value in identity.values()
    ):
        return "unknown", client_id, daemon
    return ("ok" if daemon == client_id else "version-mismatch"), client_id, daemon


def readiness(store: NoteStore, _between=None):
    probe = LeaderLock(store.session)
    held = not probe.acquire(blocking=False)
    if not held:
        probe.release()
    if _between is not None:
        _between()
    holder = probe.holder_instance() if held else None
    with store.state_lock():
        note = store.load()
    if not held:
        return (
            "standby-only"
            if any(process_liveness(s) != PROC_DEAD for s in note.get("standbys", []))
            else "unavailable" if note.get("leader") is None and note.get("exit") else "not-started"
        ), note
    leader = note.get("leader")
    if (
        not leader
        or not holder
        or leader.get("instance") != holder
        or process_liveness(leader) == PROC_DEAD
    ):
        return "starting", note
    if note.get("phase") == "unavailable":
        return "unavailable", note
    if (
        note.get("phase") == "following"
        and note.get("root")
        and note["progress"].get(note["root"]) is not None
    ):
        return "armed", note
    return "waiting-for-binding", note


def tiers_mixed(note: dict) -> bool:
    identities = set()
    for record in [note.get("leader"), *note.get("standbys", [])]:
        if record is None or process_liveness(record) == PROC_DEAD:
            continue
        identity = record.get("identity")
        identities.add((identity.get("kit"), identity.get("fp")) if isinstance(identity, dict) else None)
    return len(identities) > 1


def printable_text(text: str) -> str:
    """Render untrusted diagnostic text without terminal or line controls."""
    return "".join(char if 32 <= ord(char) <= 126 else ascii(char)[1:-1] for char in text)


def rebind_line(note: dict, session: str) -> str | None:
    record = note.get("bind_observation")
    if not record and not note.get("binding_degraded") and not note.get("submit_miss"):
        return None
    record = record or {}
    receipts = record.get("receipts") or []
    root, provenance = record.get("root"), record.get("root_provenance")
    if root and len(receipts) == 1 and provenance in ("command", "inferred") and receipt_row_lookup(root, receipts[0]) == "match":
        prefix = "rebind, may need operator approval: " if provenance == "command" else "rebind (root inferred, check it, may need operator approval): "
        command = shlex.join(["python3", str(Path(__file__).resolve()), "bind", "--manual", "--session", session, "--root", root, "--anchor", receipts[0]])
        if len(prefix + command) <= 1024 and all(32 <= ord(char) <= 126 for char in command):
            return prefix + command
    # No command separator even when an interactive shell disables comments.
    prefix = "# rebind: incomplete, supply --root and --anchor"
    seen = [shlex.quote(printable_text(value)) for value in receipts]
    operands = [shlex.quote(printable_text(value)) for value in record.get("command_roots") or []]
    parts = [prefix]
    if seen:
        parts += [" seen=" + seen[0], *(" " + value for value in seen[1:])]
    if operands:
        parts += [" command text=" + operands[0], *(" " + value for value in operands[1:])]
    length = sum(map(len, parts))
    dropped = 0
    suffix = ""
    while length + len(suffix) > 1024:
        length -= len(parts.pop())
        dropped += 1
        suffix = f" dropped={dropped}"
    return "".join(parts) + suffix


def status_line(store: NoteStore, plugin_root: str | None) -> str:
    state, note = readiness(store)
    ident, client_id, daemon = "unchecked", {}, None
    if note.get("root"):
        ident, client_id, daemon = check_identity(plugin_root, note.get("root"))
        store.update(
            lambda n: n.update(
                {"client_identity": client_id, "daemon_identity": daemon}
            )
        )
    reason = shown_exit_reason(note) if state == "unavailable" and note.get("leader") is None and note.get("exit") else note.get("reason")
    suffix = f": {reason}" if state == "unavailable" and reason else ""
    binding = note.get("binding_source") or "none"
    cur = (note["progress"].get(note.get("root") or "") or {}).get("file") or "-"

    def fp(d):
        return (
            f"{d.get('kit')}@{(d.get('fp') or '')[:8]}" if d and d.get("kit") else "-"
        )

    daemon_txt = {
        "hand-root": "hand-root",
        "daemon-down": "down",
        "unchecked": "-",
    }.get(ident, fp(daemon))
    record = note.get("bind_observation") or {}
    observed = record.get("reason")
    miss = "submit-near-miss" if observed == "submit-near-miss" else note.get("submit_miss")
    last_bind = observed if observed and observed != "submit-near-miss" else (note.get("binding_degraded") or {}).get("reason")
    line = (
        f"relay-monitor: {state}{suffix} "
        f"seat={note.get('seat') or '-'} "
        f"root={note.get('root') or '-'} cursor={cur} "
        f"binding={binding} client={fp(client_id)} "
        f"daemon={daemon_txt} "
        f"updated={note.get('updated') or '-'}"
        f"{' miss=' + miss if miss else ''}"
        f"{' last-bind=' + last_bind if last_bind else ''}"
        f"{' tiers=mixed' if tiers_mixed(note) else ''}"
        f"{' identity=mismatch' if ident == 'version-mismatch' else ''}"
    )
    raw = (daemon or {}).get("raw") or ""
    remedy = next(
        (text for text in raw.splitlines() if text.startswith("remedy:")), None
    )
    if ident == "version-mismatch" and not remedy:
        remedy = "remedy: align the relay client and daemon installs before filing; delivery remains available"
    hint = rebind_line(note, store.session)
    return "\n".join(printable_text(text) for text in (line, remedy, hint) if text)


def status(args) -> int:
    session = resolve_session(args)
    if not valid_session_id(session):
        print("relay-monitor: invalid-session-id", file=sys.stderr)
        return 2
    store = NoteStore(session)
    with store.state_lock():
        sweep_owner_files(store)  # eventual cleanup; never the referenced file
    print(status_line(store, args.plugin_root))
    return 0


FALLBACK_OVERRIDE_TEXT = (
    "relay-monitor: fallback: pointer (ADT_CODEX_FORK=0);"
    " relays reach this seat by the operator's pointer bl"
    "ock."
)


def arming_text(plugin_root: str, session: str, resume: bool) -> str:
    cmd = (
        f"python3 "
        f'"{plugin_root}/tools/adapters/relay-monitor.py" '
        f"follow --host codex --session {session} "
        f"--plugin-root "
        f'"{plugin_root}"'
    )
    head = (
        "This session resumed. First call the `monitor` tool "
        "with action `list`; only if no watch is running the "
        "relay-monitor command below, "
        if resume
        else "On this boot turn, "
    )
    return (
        "If the `monitor` tool is present in your advertised "
        "tool surface (the complete capability surface expose"
        "d to you, including nested code-mode definitions): "
        + head
        + f"call the `monitor` tool with action `start`, "
        f"command `{cmd}`, and description `Relay arrivals "
        f"for the seat bound to this session`; "
        f"then call the `monitor` tool with action `list` to "
        f"confirm the watch id; then run `python3 "
        f'"{plugin_root}/tools/adapters/relay-monitor.py" '
        f"status --session {session}` "
        "through the shell and quote its line as the delivery"
        " state in your acknowledgment. "
        "If `monitor` is absent from your advertised tool sur"
        "face, make no call against it: report `unavailable: "
        "monitor-tool` as the delivery state and `fallback: p"
        "ointer` in your acknowledgment, and rely on the oper"
        "ator's pointer block."
    )


def session_start(args) -> int:
    payload = read_payload()
    session = resolve_session(args, payload)
    if not valid_session_id(session):
        reason = "no-session-identity" if session is None else "invalid-session-id"
        return emit_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": f"relay-monitor: {reason}",
                }
            }
        )
    store = NoteStore(session)
    host = decide_host(args, payload, os.environ)
    plugin_root = (
        args.plugin_root
        or os.environ.get("CLAUDE_PLUGIN_ROOT")
        or os.environ.get("PLUGIN_ROOT")
        or str(Path(__file__).resolve().parents[2])
    )
    lines = [status_line(store, plugin_root)]
    if host in ("host-ambiguous", "host-unknown"):
        store.update(lambda n: set_reason(n, host))
    elif host == "codex":
        override = (
            os.environ.get("ADT_CODEX_FORK") == "0"
        )  # the only stock signal the kit honours (erratum-1 A)
        store.update(
            lambda n: n.update({"host": "codex", "codex_fork_override": override})
        )
        lines.append(
            FALLBACK_OVERRIDE_TEXT
            if override
            else arming_text(plugin_root, session, payload.get("source") == "resume")
        )
    else:
        store.update(lambda n: n.update({"host": "claude-code"}))
    return emit_json(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(lines),
            }
        }
    )


MODES["session-start"] = session_start
MODES["status"] = status


def notify(title: str, text: str, notifier: str = "osascript") -> None:
    text = text.encode("utf-8")[:200].decode("utf-8", "ignore")
    if notifier == "none":
        return
    argv = (
        ["terminal-notifier", "-title", title, "-message", text]
        if notifier == "terminal-notifier"
        else [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            text,
        ]
    )
    subprocess.run(argv, check=True, timeout=10, capture_output=True)


def operator_scan(roots, *, notifier: str, since: str | None = None) -> int:
    store, fired = NoteStore("operator"), 0
    for root in roots:
        with root_budget():
            snap = read_index(Path(root) / "INDEX.md")
        if snap.error in INDEX_UNAVAILABLE:
            continue
        cursor = store.load()["progress"].get(root)
        if cursor is None:
            if since:
                cursor = next(
                    (
                        {"file": r.cells["file"], "position": r.position}
                        for r in snap.rows
                        if r.cells["file"] == since
                    ),
                    None,
                )
            if cursor is None:
                cursor = (
                    {
                        "file": snap.rows[-1].cells["file"],
                        "position": snap.rows[-1].position,
                    }
                    if snap.rows
                    else dict(SENTINEL)
                )
            store.update(lambda n: n["progress"].__setitem__(root, cursor))
        start = locate(snap, cursor)
        if start is None:
            store.update(lambda n: set_reason(n, f"cursor-not-found:{root}"))
            continue
        for row_ in snap.rows[start + 1 :]:
            if names_seat(row_, "operator"):
                line = row_to_line(row_, root)
                sys.stdout.write(line)
                sys.stdout.flush()
                try:
                    notify(
                        f"relay: {row_.cells['from']} -> operator "
                        f"({row_.cells['phase']})",
                        line.strip(),
                        notifier,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    reason = f"notify-failed: {exc}"
                    store.update(lambda n: set_reason(n, reason))
                fired += 1
            store.update(
                lambda n: n["progress"].__setitem__(
                    root, {"file": row_.cells["file"], "position": row_.position}
                )
            )
    return fired


def operator(args) -> int:
    roots, last = (args.root or [os.getcwd()]), None
    while True:
        sig = tuple(index_signature(Path(r) / "INDEX.md") for r in roots)
        if sig != last:
            operator_scan(roots, notifier=args.notifier, since=args.since)
            last = sig
        time.sleep(POLL_INDEX)


def _replay_error(root: str) -> None:
    print(json.dumps({"error": "cannot-establish-recovery", "root": root}))


def replay(args) -> int:
    session = resolve_session(args)
    if not valid_session_id(session):
        print("relay-monitor: invalid-session-id", file=sys.stderr)
        return 2
    budgets: dict[str, RootBudget] = {}

    def read_root(root: str) -> IndexSnapshot:
        if root not in budgets:
            budgets[root] = RootBudget()
        with use_budget(budgets[root]):
            return read_index(Path(root) / "INDEX.md")

    note = NoteStore(session).load()
    floors = list(note.get("floors") or [])
    if args.after:  # single-binding optimization: the current binding only
        root, seat = note.get("root"), note.get("seat")
        if not root or not seat:
            print(json.dumps({"error": "unbound"}))
            return 3
        snap0 = read_root(root)
        hit = (
            None
            if snap0.error in INDEX_UNAVAILABLE
            else next((r for r in snap0.rows if r.cells["file"] == args.after), None)
        )
        if hit is None:
            _replay_error(root)
            print(json.dumps({"done": True}))
            return 3  # the --after row must exist; its position is frozen here (D10-R1)
        floors = [
            {"root": root, "seat": seat, "file": args.after, "position": hit.position}
        ]
    if not floors:
        print(json.dumps({"error": "unbound"}))
        return 3
    known = {(f["root"], canonical(f["seat"])) for f in floors}
    if args.token:
        try:
            tok = json.loads(base64.b64decode(args.token).decode("utf-8"))
            plan, index, next_pos, carried_failed = (
                tok["floors"],
                tok["index"],
                tok["next_position"],
                tok["failed"],
            )
            if (
                not isinstance(plan, list)
                or not plan
                or not isinstance(index, int)
                or not isinstance(next_pos, int)
                or not isinstance(carried_failed, bool)
            ):
                raise ValueError("token shape")
            if (
                not (0 <= index < len(plan))
                or type(next_pos) is not int
                or next_pos < 1
            ):
                raise ValueError(
                    "token bounds"
                    # an index at or past the end can only claim a walk it never made
                    # (D8-R2)
                )

            def _pos(v):  # strict: booleans are not positions (D9-R1)
                return type(v) is int and v >= 0

            def _cell(v):
                return v is None or (isinstance(v, str) and v != "")

            for f in plan:
                if (f["root"], canonical(f["seat"])) not in known:
                    raise ValueError("unknown root")
                if (
                    not _pos(f.get("floor_position"))
                    or not _cell(f.get("floor_file"))
                    or ((f["floor_file"] is None) != (f["floor_position"] == 0))
                ):
                    raise ValueError(
                        "floor fields"
                    )  # the sentinel is exactly (None, 0)
                unavailable = f.get("unavailable", False)
                if type(unavailable) is not bool:
                    raise ValueError("unavailable marker")
                if unavailable:
                    if (
                        f.get("upper_file") is not None
                        or f.get("upper_position") is not None
                    ):
                        raise ValueError("unavailable bounds")
                elif (
                    not _pos(f.get("upper_position"))
                    or not _cell(f.get("upper_file"))
                    or ((f["upper_file"] is None) != (f["upper_position"] == 0))
                    or f["upper_position"] < f["floor_position"]
                ):
                    raise ValueError("upper fields")
            cur = plan[index]
            if cur.get("unavailable") or not (
                cur["floor_position"] < next_pos <= cur["upper_position"]
            ):
                raise ValueError(
                    "cursor outside the frozen interval"
                    # a token this implementation emits always points inside the
                    # interval
                    # (D9-R1)
                )
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError):
            print(json.dumps({"error": "cannot-establish-recovery", "root": None}))
            print(json.dumps({"done": True}))
            return 3
    else:
        plan, index, next_pos, carried_failed = [], 0, None, False
        for f in floors:  # fix every root's upper bound at the first call
            snap = read_root(f["root"])
            upper = note["progress"].get(f["root"])
            if snap.error in INDEX_UNAVAILABLE:
                plan.append(
                    {
                        "root": f["root"],
                        "seat": f["seat"],
                        "upper_file": None,
                        "upper_position": None,
                        "floor_file": f["file"],
                        "floor_position": f["position"],
                        "unavailable": True,
                    }
                )
                continue
            if upper is None:
                upper = (
                    {
                        "file": snap.rows[-1].cells["file"],
                        "position": snap.rows[-1].position,
                    }
                    if snap.rows
                    else dict(SENTINEL)
                )
            plan.append(
                {
                    "root": f["root"],
                    "seat": f["seat"],
                    "upper_file": upper["file"],
                    "upper_position": upper["position"],
                    "floor_file": f["file"],
                    "floor_position": f["position"],
                }
            )
    emitted, failed = (
        0,
        carried_failed,
    )  # an earlier page's failure survives the walk (delta review)
    while index < len(plan):
        entry = plan[index]
        root, seat = entry["root"], canonical(entry["seat"])
        snap = read_root(root)
        if entry.get("unavailable") or snap.error in INDEX_UNAVAILABLE:
            _replay_error(root)
            failed = True
            index += 1
            next_pos = None
            continue
        start_cur = (
            {"file": entry["floor_file"], "position": entry["floor_position"]}
            if entry["floor_file"] is not None
            else dict(SENTINEL)
        )
        s_ = locate(
            snap,
            start_cur,
            # every visit, continuation pages included: the floor row must sit at its
            # frozen
            # position
        )
        if s_ is None or (
            s_ >= 0 and snap.rows[s_].position != entry["floor_position"]
        ):
            _replay_error(root)
            failed = True
            index += 1
            next_pos = None
            continue
        if next_pos is None:
            next_pos = (snap.rows[s_].position + 1) if s_ >= 0 else 1
        u = (
            locate(
                snap, {"file": entry["upper_file"], "position": entry["upper_position"]}
            )
            if entry["upper_file"] is not None
            else -1
        )
        if (
            u is None or (u >= 0 and snap.rows[u].position != entry["upper_position"])
            # the upper row must sit at its frozen position; positions are dense, so
            # every
            # cursor at or below it names a row
        ):
            _replay_error(root)
            failed = True
            index += 1
            next_pos = None
            continue
        upper_pos = snap.rows[u].position if u >= 0 else 0
        for row_ in snap.rows:
            if row_.position < next_pos or row_.position > upper_pos:
                continue
            if emitted >= args.limit:
                token = base64.b64encode(
                    json.dumps(
                        {
                            "floors": [
                                {
                                    k: v
                                    for k, v in e.items()
                                    if k
                                    in (
                                        "root",
                                        "seat",
                                        "upper_file",
                                        "upper_position",
                                        "floor_file",
                                        "floor_position",
                                        "unavailable",
                                    )
                                }
                                for e in plan
                            ],
                            "index": index,
                            "next_position": row_.position,
                            "failed": failed,
                        }
                    ).encode()
                ).decode()
                print(json.dumps({"continue": token}))
                return 0
            if names_seat(row_, seat) and not is_own(row_, seat):
                sys.stdout.write(row_to_line(row_, root))
                emitted += 1
        index += 1
        next_pos = None
    print(json.dumps({"done": True}))
    return 3 if failed else 0


MODES["operator"] = operator
MODES["replay"] = replay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay-monitor")
    sub = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="auto")
    common.add_argument("--session")
    common.add_argument("--plugin-root")
    p = sub.add_parser("follow", parents=[common])
    p.add_argument("--anchor")
    p = sub.add_parser("bind", parents=[common])
    p.add_argument("--manual", action="store_true")
    p.add_argument("--root")
    p.add_argument("--anchor")
    p = sub.add_parser("drain", parents=[common])
    p.add_argument("--deadline", type=float, default=DRAIN_DEADLINE)
    sub.add_parser("session-start", parents=[common])
    sub.add_parser("status", parents=[common])
    p = sub.add_parser("operator", parents=[common])
    p.add_argument("--root", action="append", default=[])
    p.add_argument("--since")
    p.add_argument(
        "--notifier",
        choices=("osascript", "terminal-notifier", "none"),
        default="osascript",
    )
    p = sub.add_parser("replay", parents=[common])
    p.add_argument("--after")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--continue", dest="token")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handler = MODES[args.mode]
    try:
        return handler(args)
    except Exception:
        # Hooks consume untrusted payloads and must not interrupt their host.
        # Other modes retain their ordinary error reporting.
        if args.mode in ("drain", "session-start") or (args.mode == "bind" and not args.manual):
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
