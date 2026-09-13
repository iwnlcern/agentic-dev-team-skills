#!/usr/bin/env python3
"""relay-monitor: kit-side relay auto-delivery adapter (DD-v295-b2 r7).

Modes: follow, bind, drain, session-start, status, operator, replay.
Reads the run's rendered INDEX.md; never touches the engine record.
"""
from __future__ import annotations

import argparse, base64, contextlib, errno, fcntl, json, os, platform, re, select, subprocess, sys, tempfile, time, uuid
from dataclasses import dataclass, field
from pathlib import Path

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
CLAUDE_PAYLOAD_FIELDS = ("prompt_id", "effort", "scratchpad_dir", "agent_id", "agent_type")
INDEX_HEADERS = ("time", "phase", "role", "dispatch", "parent", "from", "to", "cc", "status", "file")
SENTINEL = {"file": None, "position": 0}


class LockTimeout(Exception):
    pass


class TransportFailed(Exception):
    pass


def note_dir() -> Path:
    d = Path(os.environ.get("TMPDIR") or "/tmp") / "adt-relay-monitor"
    d.mkdir(mode=0o700, exist_ok=True); os.chmod(d, 0o700)
    return d


def default_note() -> dict:
    return {"version": NOTE_VERSION, "host": None, "session": None, "seat": None, "root": None, "anchor": None,
            "bound_at": None, "binding_source": None, "binding_degraded": None, "binding_gen": 0, "leader": None,
            "standbys": [], "phase": "not-started", "reason": None, "progress": {}, "floors": [],
            "printed": {}, "frame": None, "client_identity": None, "daemon_identity": None, "updated": None}


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


class NoteStore:
    def __init__(self, session: str):
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
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
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
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.session}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(note, handle, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
            os.chmod(tmp, 0o600); os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def update(self, fn, timeout: float | None = None) -> dict:
        with self.state_lock(timeout):
            note = self.load(); fn(note); self.save(note)
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
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def leader_record() -> dict:
    return {"pid": os.getpid(), "start_time": process_start_time(os.getpid()), "instance": str(uuid.uuid4())}


def process_alive(record) -> bool:
    if not record or not record.get("pid"):
        return False
    try:
        os.kill(int(record["pid"]), 0)
    except (OSError, ValueError):
        return False
    return process_start_time(int(record["pid"])) == record.get("start_time")


class LeaderLock:
    def __init__(self, session: str):
        self._path = note_dir() / f"{session}.leader"; self._fd = None

    def acquire(self, blocking: bool) -> bool:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd); return False
        self._fd = fd; return True

    def stamp(self, instance: str) -> None:
        os.ftruncate(self._fd, 0); os.lseek(self._fd, 0, os.SEEK_SET); os.write(self._fd, instance.encode("utf-8")); os.fsync(self._fd)

    def holder_instance(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, 0); fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd); self._fd = None


class FdSink:
    accepted_total = 0            # process-wide, for TEST_SINK_ACCEPT only

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
        _, w, _ = select.select([], [self.fd], [], max(0.0, timeout)); return bool(w)

    def flush(self) -> None:
        return None


class ByteSink:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data: bytes) -> int:
        self.buf += data; return len(data)

    def wait_writable(self, timeout: float) -> bool:
        return True

    def flush(self) -> None:
        return None


def resolve_session(args) -> str | None:
    return getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or None


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
    env_codex = bool(environ.get("PLUGIN_ROOT")) and not environ.get("CLAUDE_PROJECT_DIR")
    env_claude = bool(environ.get("CLAUDE_PROJECT_DIR")) and not environ.get("PLUGIN_ROOT")
    pay_codex = "turn_id" in payload
    pay_claude = has_claude and "turn_id" not in payload
    votes = set()
    if env_codex or pay_codex: votes.add("codex")
    if env_claude or pay_claude: votes.add("claude-code")
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
            out.append(text[i + 1]); i += 2
        else:
            out.append(text[i]); i += 1
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
            cur.append(ch); cur.append(body[i + 1]); i += 2; continue
        if ch == "|":
            cells.append("".join(cur)); cur = []; i += 1; continue
        cur.append(ch); i += 1
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


def read_index(path: Path) -> IndexSnapshot:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return IndexSnapshot(error="index-missing")
    complete_last = text.endswith("\n")
    lines = text.split("\n")
    if complete_last:
        lines = lines[:-1]
    header_idx = next((i for i, l in enumerate(lines) if (c := split_cells(l)) and [x.strip() for x in c] == list(INDEX_HEADERS)), None)
    if header_idx is None:
        return IndexSnapshot(error="index-malformed")
    snap, seen, position = IndexSnapshot(), set(), 0
    data = lines[header_idx + 2:]
    for j, line in enumerate(data):
        if not line.strip():
            continue
        if j == len(data) - 1 and not complete_last:
            snap.partial_tail = True; break
        position += 1
        cells = split_cells(line)
        if cells is None or len(cells) != len(INDEX_HEADERS):
            snap.error = f"index-malformed-row:{position}"; break
        decoded = {k: unescape_cell(v) for k, v in zip(INDEX_HEADERS, cells)}
        if not decoded["file"]:
            snap.error = f"index-malformed-row:{position}"; break
        if decoded["file"] in seen:
            snap.error = f"duplicate-file-cell:{position}"; break
        seen.add(decoded["file"]); snap.rows.append(Row(position, decoded, line))
    return snap


def canonical(address) -> str:
    return (address or "").strip().lower()


def address_list(cell) -> list[str]:
    return [canonical(a) for a in (cell or "").split(",") if a.strip()]


def names_seat(row: Row, seat: str) -> bool:
    seat = canonical(seat)
    return seat in address_list(row.cells["to"]) or seat in address_list(row.cells["cc"])


def is_own(row: Row, seat: str) -> bool:
    return canonical(row.cells["from"]) == canonical(seat)


def row_to_line(row: Row, root: str) -> str:
    payload = dict(row.cells); payload["root"] = root
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def locate(snapshot: IndexSnapshot, cursor: dict) -> int | None:
    if cursor.get("file") is None:
        return -1
    return next((i for i, r in enumerate(snapshot.rows) if r.cells["file"] == cursor["file"]), None)
