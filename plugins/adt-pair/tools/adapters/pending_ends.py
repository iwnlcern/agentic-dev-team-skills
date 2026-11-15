"""Private pending-end records, ordered fairly without contacting the service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time


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


def _filename(pe: PendingEnd) -> str:
    return f"{pe.host}-{hashlib.sha256(pe.host_session_id.encode('utf-8')).hexdigest()}-{pe.generation}.json"


def _decode(data: bytes) -> PendingEnd:
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != set(PendingEnd.__dataclass_fields__):
        raise ValueError("invalid pending-end fields")
    if any(not isinstance(value[k], str) or not value[k]
           for k in ("host", "host_session_id", "project_id", "session_id", "key_path")):
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


def _scan(*, home: Path, deadline: float | None = None) -> tuple[list[PendingEnd], list[Path]]:
    folder = Path(home) / ".adt" / "pending-ends"
    records, malformed = [], []
    def budget():
        if deadline is not None and time.monotonic() >= deadline:
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
