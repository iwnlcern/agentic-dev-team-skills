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
        return Binding(seat, root, environ.get("ADT_RELAY_ANCHOR") or (getattr(args, "anchor", None) if args else None), int(note.get("binding_gen", 0)), "env")
    if (seat or root) and not (seat and root):
        note["reason"] = "override-incomplete"
    if note.get("seat") and note.get("root"):
        return Binding(note["seat"], note["root"], note.get("anchor"), int(note.get("binding_gen", 0)), note.get("binding_source") or "hook")
    return None


def record_floor(note: dict, root: str, seat: str, cursor: dict) -> None:
    """Append the (root, seat) floor once; never move, reorder, or remove it (erratum-1 amendment B)."""
    seat = canonical(seat)
    if not any(f["root"] == root and f["seat"] == seat for f in note.setdefault("floors", [])):
        note["floors"].append({"root": root, "seat": seat, "file": cursor.get("file"), "position": int(cursor.get("position", 0))})


def frame_identity(binding: Binding, leader_instance: str, row_file: str, owner: dict | None = None) -> dict:
    identity = {"root": binding.root, "seat": canonical(binding.seat), "binding_gen": binding.binding_gen, "leader": leader_instance, "file": row_file}
    if owner:
        identity["owner"] = {"lock": owner["lock"]}
    return identity


class OwnerLock:
    """A kernel flock held for the life of one Stop invocation; its release at process exit is the only death evidence used."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def hold(self, _between=None) -> bool:
        """Call only under the state lock: the sweep runs under that lock too, so the open-to-flock window is never observable (P6-R1)."""
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        if _between is not None:
            _between()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd); return False
        self.fd = fd; return True

    def release(self, unlink: bool) -> None:
        if self.fd is None:
            return
        if unlink:
            with contextlib.suppress(OSError):
                os.unlink(self.path)
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd); self.fd = None


def stream_owner(store: NoteStore, end: float, _between=None) -> dict | None:
    instance = f"drain-{uuid.uuid4().hex[:8]}"
    lock = OwnerLock(store.owner_lock_path(instance))
    try:
        with store.state_lock(_remaining(end)):                # serialized with sweep_owner_files; bounded by the original budget
            if not lock.hold(_between):
                return None
    except LockTimeout:
        return None
    return {"instance": instance, "lock": str(lock.path), "_lock": lock}


OWNER_ALIVE, OWNER_DEAD, OWNER_UNKNOWN = "alive", "dead", "unknown"


def owner_state(frame: dict) -> str:
    """Non-blocking; call only under the state lock. Only a successful acquisition is death evidence (P4-R1)."""
    owner = (frame or {}).get("identity", {}).get("owner") or {}
    path = owner.get("lock")
    if not path:
        return OWNER_UNKNOWN
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return OWNER_UNKNOWN                                   # missing or unreadable: not proof of death
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        return OWNER_ALIVE if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else OWNER_UNKNOWN
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)                         # acquired: the holder is gone; release at once, leave the file (evidence) in place
    os.close(fd)
    return OWNER_DEAD


def frame_is_stale(frame: dict) -> bool:
    return owner_state(frame) == OWNER_DEAD


def reclaim_frame(store: NoteStore, note: dict, frame: dict) -> None:
    """Under the state lock, after frame_is_stale: clear, persist, then remove the owner file (P5-R1 ordering)."""
    path = frame.get("identity", {}).get("owner", {}).get("lock")
    note["frame"] = None
    store.save(note)                                           # durable first
    if path:
        with contextlib.suppress(OSError):
            os.unlink(path)                                    # a crash before this line leaves only an unreferenced file for the sweep


def sweep_owner_files(store: NoteStore) -> int:
    """Under the state lock: remove unreferenced owner files whose flock is acquirable. Never touches the referenced file."""
    removed = 0
    referenced = ((store.load().get("frame") or {}).get("identity", {}).get("owner") or {}).get("lock")
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
            os.close(fd); continue                             # held: a live owner
        with contextlib.suppress(OSError):
            os.unlink(path); removed += 1
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return removed


class Pacer:
    def __init__(self, lines: int = PACE_LINES, window: float = PACE_WINDOW):
        self.lines, self.window, self.stamps = lines, window, []

    def _trim(self):
        now = time.monotonic(); self.stamps = [t for t in self.stamps if now - t < self.window]

    def allow(self) -> bool:
        self._trim(); return len(self.stamps) < self.lines

    def record(self) -> None:
        self.stamps.append(time.monotonic())

    def wait_time(self) -> float:
        self._trim(); return 0.0 if len(self.stamps) < self.lines else max(0.0, self.window - (time.monotonic() - self.stamps[0]))


class FrameBusy(Exception):
    pass


def write_frame(sink, note: dict, identity: dict, data: bytes, end: float) -> bool:
    frame = note.get("frame")
    if frame:
        if frame.get("identity", {}).get("leader") != identity["leader"]:
            raise FrameBusy()
        if frame.get("identity") != identity:
            raise TransportFailed("frame-invalidated")
        accepted = int(frame["accepted"]); data = bytes.fromhex(frame["data"])
    else:
        accepted = 0
    while accepted < len(data) and time.monotonic() < end:
        try:
            n = sink.write(data[accepted:])
        except OSError as exc:
            raise TransportFailed(str(exc)) from exc
        if not n:
            break                      # zero progress: never wait under the state lock (P2b)
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
        store.update(fn, timeout=_remaining(end)); return True
    except LockTimeout:
        result.aborted = "lock-timeout"; return False


def deliver_once(store: NoteStore, sink, binding: Binding | None, snapshot: IndexSnapshot, *, end: float, leader_instance: str, owner: dict | None = None,
                 limit: int | None = None, pacer: Pacer | None = None, encode=None, start_cursor: dict | None = None) -> DeliverResult:
    result = DeliverResult()
    if binding is None:
        return result
    root_key, encode = binding.root, encode or (lambda b: b)
    if snapshot.error in ("index-missing", "index-malformed"):
        _bounded_update(store, lambda n: n.update({"phase": "unavailable", "reason": snapshot.error}), end, result)
        result.halted = result.halted or snapshot.error; return result
    try:
        with store.state_lock(_remaining(end)):
            note = store.load()
            frame = note.get("frame")
            if frame and frame.get("identity", {}).get("leader") == leader_instance:
                ident = frame["identity"]
                if (ident.get("root"), ident.get("seat"), ident.get("binding_gen")) != (binding.root, canonical(binding.seat), binding.binding_gen):
                    raise TransportFailed("frame-invalidated")          # own partial stream, binding gone: abandon even with nothing pending (R2)
            cursor = dict(start_cursor) if start_cursor is not None else dict(note["progress"].get(root_key) or SENTINEL)
    except LockTimeout:
        result.aborted = "lock-timeout"; return result
    start = locate(snapshot, cursor)
    if start is None:
        anchor_idx = locate(snapshot, {"file": binding.anchor}) if binding.anchor else None
        if anchor_idx is None:
            _bounded_update(store, lambda n: n.update({"phase": "unavailable", "reason": "anchor-not-found"}), end, result)
            result.halted = "anchor-not-found"; return result
        start = anchor_idx; cursor = {"file": snapshot.rows[start].cells["file"], "position": snapshot.rows[start].position}
        if not _bounded_update(store, lambda n: (n["progress"].__setitem__(root_key, dict(cursor)), n.update({"reason": "rebased"})), end, result):
            return result
    for offset, row_ in enumerate(snapshot.rows[start + 1:]):
        if limit is not None and result.emitted >= limit:
            return result
        if time.monotonic() >= end:
            result.aborted = "deadline"; return result
        prev_file = snapshot.rows[start + offset].cells["file"] if start + offset >= 0 else None
        addressed = names_seat(row_, binding.seat) and not is_own(row_, binding.seat)
        if addressed and pacer is not None and not pacer.allow():
            result.aborted = "paced"; return result
        try:
            lock = store.state_lock(_remaining(end)); lock.__enter__()
        except LockTimeout:
            result.aborted = "lock-timeout"; return result
        try:
            note = store.load()
            frame = note.get("frame")
            if frame and frame.get("identity", {}).get("leader") != leader_instance and frame_is_stale(frame):
                reclaim_frame(store, note, frame); frame = None          # dead owner: clear, save, then unlink; the row restarts at byte zero (R1, P5-R1)
            mine = bool(frame) and frame.get("identity", {}).get("leader") == leader_instance
            live_seat = canonical(note.get("seat")) if binding.source != "env" else canonical(binding.seat)
            live_root = note.get("root") if binding.source != "env" else binding.root
            if int(note.get("binding_gen", 0)) != binding.binding_gen or live_seat != canonical(binding.seat) or live_root != binding.root:
                if mine:
                    raise TransportFailed("frame-invalidated")
                result.aborted = "binding-changed"; return result
            cur = note["progress"].get(root_key) or SENTINEL
            if cur.get("file") is not None and int(cur.get("position", 0)) >= row_.position:
                if mine:
                    raise TransportFailed("frame-invalidated")
                result.aborted = "progress-moved"; return result
            if (cur.get("file") or None) != prev_file:
                if mine:
                    raise TransportFailed("frame-invalidated")
                result.aborted = "snapshot-order"; return result
            if addressed:
                identity = frame_identity(binding, leader_instance, row_.cells["file"], owner)
                if frame and not mine:
                    raise FrameBusy()
                if mine and frame.get("identity") != identity:
                    raise TransportFailed("frame-invalidated")
                if TEST_CRASH_BEFORE_OUTPUT:
                    os._exit(9)
                if not write_frame(sink, note, identity, encode(row_to_line(row_, binding.root).encode("utf-8")), end):
                    note.update({"phase": "unavailable", "reason": "output-stalled"}); store.save(note); result.aborted = "output-stalled"; return result
                if TEST_CRASH_AFTER_FLUSH or TEST_CRASH_AFTER_OUTPUT:
                    os._exit(9)
                ring = note["printed"].setdefault(root_key, []); ring.append(row_.cells["file"]); del ring[:-RING_SIZE]
                result.emitted += 1
                if pacer is not None:
                    pacer.record()
            elif frame and not mine:
                raise FrameBusy()
            note["progress"][root_key] = {"file": row_.cells["file"], "position": row_.position}
            if note.get("reason") in ("output-stalled", "anchor-not-found"):
                note["reason"] = None   # `rebased` stays visible until the next binding
            note["phase"] = "following"; store.save(note)
        except FrameBusy:
            result.aborted = "frame-busy"; return result
        finally:
            lock.__exit__(None, None, None)
    if snapshot.error:
        _bounded_update(store, lambda n: n.update({"phase": "unavailable", "reason": snapshot.error}), end, result)
        result.halted = snapshot.error
    return result


def index_signature(path: Path):
    try:
        st = path.stat(); return (st.st_mtime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        return None


def follow(args) -> int:
    session = resolve_session(args)
    if not session:
        print("relay-monitor: no session identity", file=sys.stderr); return 2
    host = decide_host(args, None, os.environ)
    store, leader, me = NoteStore(session), LeaderLock(session), leader_record()
    if not leader.acquire(blocking=False):
        store.update(lambda n: n["standbys"].append(me))                              # standbys register themselves only; `phase` is leader-owned (plan-19)
        leader.acquire(blocking=True)
        store.update(lambda n: n.update({"standbys": [s for s in n["standbys"] if s.get("instance") != me["instance"]]}))
    leader.stamp(me["instance"])
    store.update(lambda n: n.update({"leader": me, "host": host if host in ("claude-code", "codex") else n.get("host"), "phase": "waiting-for-binding", "frame": None}))   # a new leader's stream starts at byte zero
    with store.state_lock():
        sweep_owner_files(store)                                                          # eventual cleanup of unreferenced owner files (P5-R1)
    sink, pacer = FdSink(sys.stdout.fileno()), Pacer()
    last_sig = last_note = None; pending = True; backoff_until = 0.0
    try:
        while True:
            note = store.load(); binding = resolve_binding(note, os.environ, args)
            if binding is None:
                store.update(lambda n: n.update({"phase": "waiting-for-binding"})); time.sleep(POLL_NOTE); continue
            if binding.source == "env":
                if binding.root not in note["progress"]:                                          # per-root progress initialization (once per root)
                    snap = read_index(Path(binding.root) / "INDEX.md")
                    anchor = binding.anchor if binding.anchor else (snap.rows[-1].cells["file"] if snap.rows else None)
                    pos = next((r.position for r in snap.rows if r.cells["file"] == anchor), 0)
                    cur = {"file": anchor, "position": pos}
                    store.update(lambda n: (n["progress"].__setitem__(binding.root, dict(cur)), n.update({"anchor": anchor})))
                    note = store.load(); pending = True
                env_seat = canonical(binding.seat)                                                  # per-pair floor and binding metadata (once per pair; D8-R1)
                has_floor = any(f["root"] == binding.root and f["seat"] == env_seat for f in note.get("floors", []))
                if not has_floor or canonical(note.get("seat")) != env_seat or note.get("root") != binding.root or note.get("binding_source") != "env":
                    cur = dict(note["progress"][binding.root])                                     # existing progress is preserved; the new pair's floor is the current cursor
                    store.update(lambda n: (record_floor(n, binding.root, binding.seat, cur), n.update({"seat": binding.seat, "root": binding.root, "binding_source": "env", "anchor": binding.anchor or n.get("anchor")})))
                    note = store.load(); pending = True
            sig, nsig = index_signature(Path(binding.root) / "INDEX.md"), store.mtime()
            wake = pending or sig != last_sig or nsig != last_note or bool(note.get("frame")) or (backoff_until and time.monotonic() >= backoff_until)
            if not wake:
                time.sleep(min(POLL_INDEX, max(0.05, pacer.wait_time() or POLL_INDEX))); continue
            if time.monotonic() < backoff_until:
                time.sleep(min(POLL_INDEX, backoff_until - time.monotonic())); continue
            last_sig, last_note, backoff_until, pending = sig, nsig, 0.0, False
            snap = read_index(Path(binding.root) / "INDEX.md")
            result = deliver_once(store, sink, binding, snap, end=time.monotonic() + FOLLOW_OUTPUT_DEADLINE, leader_instance=me["instance"], pacer=pacer)
            last_note = store.mtime()
            if result.aborted == "paced":
                pending = True; time.sleep(min(POLL_INDEX, pacer.wait_time()))
            elif result.aborted == "output-stalled":
                pending = True; sink.wait_writable(BACKOFF)                       # wait for the sink outside the lock, then retry the same frame
            elif result.aborted in ("binding-changed", "progress-moved", "snapshot-order", "lock-timeout", "frame-busy", "deadline"):
                pending = True; time.sleep(0.1)
            elif result.halted:
                time.sleep(POLL_INDEX)
    except TransportFailed as exc:
        store.update(lambda n: n.update({"phase": "unavailable", "reason": "transport-failed", "leader": None}))
        leader.release()
        print(f"relay-monitor: transport failed: {exc}", file=sys.stderr)
        return 1


MODES["follow"] = follow


# ---- entrypoint: keep these the last lines of the file; later tasks insert above `def build_parser()` ----
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay-monitor")
    sub = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="auto"); common.add_argument("--session"); common.add_argument("--plugin-root")
    p = sub.add_parser("follow", parents=[common]); p.add_argument("--anchor")
    p = sub.add_parser("bind", parents=[common]); p.add_argument("--manual", action="store_true"); p.add_argument("--root"); p.add_argument("--anchor")
    p = sub.add_parser("drain", parents=[common]); p.add_argument("--deadline", type=float, default=DRAIN_DEADLINE)
    sub.add_parser("session-start", parents=[common]); sub.add_parser("status", parents=[common])
    p = sub.add_parser("operator", parents=[common]); p.add_argument("--root", action="append", default=[]); p.add_argument("--since"); p.add_argument("--notifier", choices=("osascript", "terminal-notifier", "none"), default="osascript")
    p = sub.add_parser("replay", parents=[common]); p.add_argument("--after"); p.add_argument("--limit", type=int, default=50); p.add_argument("--continue", dest="token")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handler = MODES.get(args.mode)
    if handler is None:
        print(f"relay-monitor: mode {args.mode} not implemented", file=sys.stderr); return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
