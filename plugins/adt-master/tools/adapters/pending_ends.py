"""Private pending-end records, and their bounded replay senders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Literal


@dataclass
class PendingEnd:
    host: str
    host_session_id: str
    project_id: str
    session_id: str
    generation: int
    key_path: str
    created_ms: int
    last_attempt_ms: int | None
    credential_failed: bool
    next_eligible_ms: int
    operation_key: str
    ready_path: str


def _filename(pe: PendingEnd) -> str:
    return f"{pe.host}-{hashlib.sha256(pe.host_session_id.encode('utf-8')).hexdigest()}-{pe.generation}.json"


def _decode(data: bytes) -> PendingEnd:
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != set(PendingEnd.__dataclass_fields__):
        raise ValueError("invalid pending-end fields")
    if any(not isinstance(value[k], str) or not value[k]
           for k in ("host", "host_session_id", "project_id", "session_id", "key_path",
                     "operation_key", "ready_path")):
        raise ValueError("invalid pending-end identity")
    if value["host"] not in ("codex", "claude-code"):
        raise ValueError("invalid pending-end host")
    if any(type(value[k]) is not int or value[k] < 0
           for k in ("generation", "created_ms", "next_eligible_ms")):
        raise ValueError("invalid pending-end time or generation")
    last = value["last_attempt_ms"]
    if last is not None and (type(last) is not int or last < 0):
        raise ValueError("invalid pending-end attempt")
    if type(value["credential_failed"]) is not bool:
        raise ValueError("invalid pending-end credential state")
    return PendingEnd(**value)


def _load(path: Path) -> PendingEnd | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return None
        pe = _decode(data)
        return pe if path.name == _filename(pe) else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None
    finally:
        os.close(fd)


def _scan(*, home: Path, deadline: float | None = None,
          clock=time) -> tuple[list[PendingEnd], list[Path]]:
    folder = Path(home) / ".adt" / "pending-ends"
    records, malformed = [], []
    def budget():
        if deadline is not None and clock.monotonic() >= deadline:
            raise TimeoutError("pending-end status budget")
    budget()
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                budget()
                if not entry.name.endswith(".json"):
                    continue
                path = folder / entry.name
                pe = _load(path)
                if pe is None:
                    malformed.append(path)
                else:
                    records.append(pe)
                budget()
    except FileNotFoundError:
        return [], []
    except TimeoutError:
        raise
    except OSError:
        return [], [folder]
    return records, sorted(malformed)


def _sort_key(pe: PendingEnd):
    return (pe.last_attempt_ms is not None,
            pe.last_attempt_ms if pe.last_attempt_ms is not None else 0,
            pe.created_ms, _filename(pe))


def eligible(*, home: Path, now_ms: int) -> list[PendingEnd]:
    records, _ = _scan(home=home)
    return sorted((pe for pe in records if pe.next_eligible_ms <= now_ms), key=_sort_key)


def write(pe: PendingEnd, *, home: Path) -> Path:
    data = (json.dumps(asdict(pe), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    _decode(data)
    if len(data) > 65536:
        raise ValueError("pending-end record too large")
    folder = Path(home) / ".adt" / "pending-ends"
    path = folder / _filename(pe)
    if os.path.lexists(path) and _load(path) is None:
        raise ValueError("existing pending-end record is unreadable")
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory).st_uid != os.getuid():
            raise PermissionError("pending-end directory owner")
        os.fchmod(directory, 0o700)
    finally:
        os.close(directory)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=folder)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, path)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)
    return path


_SendOutcome = Literal["deleted", "kept", "credential-failed"]


@dataclass
class SendReport:
    attempted: int = 0
    deleted: int = 0
    kept: int = 0
    credential_failed: int = 0

    def add(self, outcome: _SendOutcome) -> None:
        self.attempted += 1
        if outcome == "deleted":
            self.deleted += 1
        elif outcome == "credential-failed":
            self.credential_failed += 1
        else:
            self.kept += 1


def _send(pe: PendingEnd, *, client, deadline_for) -> _SendOutcome:
    from relay_engine.api_client import ApiErrorValue, EndpointUnavailable, LocalRefusal, read_key_file

    try:
        key = read_key_file(pe.key_path)
    except LocalRefusal:
        return "credential-failed"

    args = {"key_id": key.key_id, "key_secret": key.key_secret, "session_id": pe.session_id}
    for op, payload in (("session.resume", args),
                        ("session.end", {**args, "operation_key": pe.operation_key})):
        deadline = deadline_for()
        if deadline is None:
            return "kept"
        try:
            result = client.call(op, payload, deadline=deadline)
        except (EndpointUnavailable, LocalRefusal):
            return "kept"
        if isinstance(result, ApiErrorValue):
            reason = result.cause.get("reason")
            if reason == "session_not_current":
                return "deleted"
            if reason in ("key_rejected", "key_replaced"):
                return "credential-failed"
            return "kept"
    # ApiClient validates the operation's result before returning it.
    return "deleted"


def send(pe: PendingEnd, *, client, deadline: float) -> _SendOutcome:
    """Try one record over a caller-owned connection without changing local state."""
    return _send(pe, client=client, deadline_for=lambda: deadline)


def _deadline(clock, end):
    now = clock.monotonic()
    return min(now + 5, end) if now < end else None


def _records(home, clock, end):
    try:
        records, _ = _scan(home=home, deadline=end, clock=clock)
    except TimeoutError:
        return []
    now = int(clock.time() * 1000)
    return sorted((record for record in records if record.next_eligible_ms <= now), key=_sort_key)


def _attempt(record, *, home, clock, deadline_for, client_for):
    if deadline_for() is None:
        return None
    path = Path(home) / ".adt" / "pending-ends" / _filename(record)
    if _load(path) != record:
        return None
    stamp = int(clock.time() * 1000)
    stamped = replace(record, last_attempt_ms=stamp)
    try:
        write(stamped, home=home)
    except (OSError, ValueError):
        return None
    client = client_for(record.ready_path)
    outcome = ("kept" if client is None else
               _send(stamped, client=client, deadline_for=deadline_for))
    if outcome != "kept" and _load(path) == stamped:
        try:
            if outcome == "deleted":
                path.unlink()
            else:
                write(replace(stamped, credential_failed=True, next_eligible_ms=stamp + 3600000), home=home)
        except (OSError, ValueError):
            return "kept"
    elif outcome != "kept":
        return "kept"
    return outcome


def drain_share(*, client, ready_path: str, home, share_s, clock) -> SendReport:
    """Send only records for the caller's endpoint without closing its connection."""
    end = clock.monotonic() + share_s
    report = SendReport()
    for record in _records(home, clock, end):
        if _deadline(clock, end) is None:
            break
        if record.ready_path != ready_path:
            continue
        outcome = _attempt(record, home=home, clock=clock, deadline_for=lambda: _deadline(clock, end),
                           client_for=lambda _: client)
        if outcome is not None:
            report.add(outcome)
    return report


class ReplayWorker:
    """One independent replay thread, with a deadline-aware factory per endpoint."""

    def __init__(self, *, client_factory, home, clock):
        self._factory = client_factory
        self._home = home
        self._clock = clock
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="adt-pending-end-replay", daemon=True)
        self._started = False

    def start(self) -> None:
        with self._lifecycle:
            if not self._started and not self._stop.is_set():
                self._thread.start()
                self._started = True

    def cancel(self) -> None:
        with self._lifecycle:
            self._stop.set()
            started = self._started
        if started:
            self._thread.join()

    def _pass(self) -> SendReport:
        from relay_engine.api_client import EndpointUnavailable, LocalRefusal, read_ready_file

        clock, home = self._clock, self._home
        end = clock.monotonic() + 10
        clients = {}
        report = SendReport()

        def deadline_for():
            return None if self._stop.is_set() else _deadline(clock, end)

        def client_for(path):
            if path not in clients:
                clients[path] = None
                try:
                    ready = read_ready_file(path)
                    deadline = deadline_for()
                    if deadline is not None:
                        clients[path] = self._factory(ready, deadline=deadline)
                except (EndpointUnavailable, LocalRefusal):
                    pass
            return clients[path]

        try:
            for record in _records(home, clock, end):
                if deadline_for() is None:
                    break
                outcome = _attempt(record, home=home, clock=clock,
                                   deadline_for=deadline_for, client_for=client_for)
                if outcome is not None:
                    report.add(outcome)
        finally:
            for client in clients.values():
                if client is not None:
                    client.close()
        return report

    def _run(self) -> None:
        delay = 30
        while not self._stop.is_set():
            report = self._pass()
            # Check even ineligible records so failed credentials still back off.
            scan_end = self._clock.monotonic() + 1
            try:
                records, _ = _scan(home=self._home, deadline=scan_end, clock=self._clock)
                pending = bool(records)
            except TimeoutError:
                pending = True
            if report.deleted or not pending:
                delay = 30
            if self._stop.wait(delay):
                return
            if pending:
                delay = min(delay * 2, 300)
