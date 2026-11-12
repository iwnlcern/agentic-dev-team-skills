"""Per-host-session sign-in records and their directory-locked transaction.

Hooks read without a lock; writers replace a complete, fsynced file atomically.
Only a sign-in caller's transaction restore removes a record that was absent.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Iterator
import uuid

TRANSACTION_WAIT_S: float = 15.0
RECORD_MAX_BYTES: int = 64 * 1024


@dataclass(frozen=True)
class SeatRecord:
    host: str
    host_session_id: str
    seat_id: str
    seat_address: str
    project_id: str
    written_at_ms: int


class SeatRecordBusy(Exception):
    """The directory transaction could not be acquired within its wait share."""


class SeatRecordUnreadable(Exception):
    def __init__(self, path: Path, cause: str):
        self.path, self.cause = path, cause
        super().__init__(f"seat record unreadable: {path}: {cause}")


def sessions_dir(*, home: Path | None = None) -> Path:
    return (Path.home() if home is None else Path(home)) / ".adt" / "sessions"


def record_path(host: str, host_session_id: str, *, home: Path | None = None) -> Path:
    if host not in ("claude-code", "codex"):
        raise ValueError("invalid host")
    if not isinstance(host_session_id, str) or not host_session_id:
        raise ValueError("empty host session id")
    digest = hashlib.sha256(host_session_id.encode("utf-8")).hexdigest()
    return sessions_dir(home=home) / f"{host}-{digest}"


def _ensure_directory(*, home: Path | None) -> Path:
    folder = sessions_dir(home=home)
    try:
        folder.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(folder.parent, 0o700)
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(folder, 0o700)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise SeatRecordUnreadable(folder, "owner")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return folder


def _read_bytes(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SeatRecordUnreadable(path, type(exc).__name__) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SeatRecordUnreadable(path, "type or owner")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(RECORD_MAX_BYTES + 1)
        if len(data) > RECORD_MAX_BYTES:
            raise SeatRecordUnreadable(path, "too large")
        return data
    except OSError as exc:
        raise SeatRecordUnreadable(path, type(exc).__name__) from exc
    finally:
        os.close(fd)


def _decode(path: Path, data: bytes, host: str, host_session_id: str) -> SeatRecord:
    if len(data) > RECORD_MAX_BYTES:
        raise SeatRecordUnreadable(path, "too large")
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != set(SeatRecord.__dataclass_fields__):
            raise ValueError("fields")
        if any(not isinstance(value[k], str) or not value[k]
               for k in ("host", "host_session_id", "seat_id", "seat_address", "project_id")):
            raise ValueError("string field")
        if type(value["written_at_ms"]) is not int or value["written_at_ms"] < 0:
            raise ValueError("written_at_ms")
        if value["host"] != host or value["host_session_id"] != host_session_id:
            raise ValueError("identity")
        return SeatRecord(**value)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise SeatRecordUnreadable(path, "invalid record") from exc


def read(host: str, host_session_id: str, *, home: Path | None = None) -> SeatRecord | None:
    path = record_path(host, host_session_id, home=home)
    data = _read_bytes(path)
    return None if data is None else _decode(path, data, host, host_session_id)


def _replace_bytes(path: Path, data: bytes) -> None:
    temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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


def write(host: str, host_session_id: str, seat_id: str, seat_address: str, project_id: str,
          *, home: Path | None = None) -> SeatRecord:
    path = record_path(host, host_session_id, home=home)
    record = SeatRecord(host, host_session_id, seat_id, seat_address, project_id, time.time_ns() // 1_000_000)
    data = (json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    _decode(path, data, host, host_session_id)
    _ensure_directory(home=home)
    _replace_bytes(path, data)
    return record


class SeatTransaction:
    def __init__(self, host: str, host_session_id: str, home: Path | None, path: Path, before: bytes | None):
        self._host, self._session, self._home = host, host_session_id, home
        self._path, self._before, self._active = path, before, True

    def _check_active(self) -> None:
        if not self._active:
            raise RuntimeError("seat transaction has ended")

    def write(self, seat_id: str, seat_address: str, project_id: str) -> SeatRecord:
        self._check_active()
        return write(self._host, self._session, seat_id, seat_address, project_id, home=self._home)

    def restore(self) -> None:
        self._check_active()
        if self._before is None:
            self._path.unlink(missing_ok=True)
        else:
            _replace_bytes(self._path, self._before)


@contextmanager
def transaction(host: str, host_session_id: str, *, home: Path | None = None,
                wait_s: float = TRANSACTION_WAIT_S) -> Iterator[SeatTransaction]:
    path = record_path(host, host_session_id, home=home)
    folder = _ensure_directory(home=home)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tx = None
    try:
        end = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = end - time.monotonic()
                if left <= 0:
                    raise SeatRecordBusy("seat record transaction busy")
                time.sleep(min(0.01, left))
        tx = SeatTransaction(host, host_session_id, home, path, _read_bytes(path))
        yield tx
    finally:
        if tx is not None:
            tx._active = False
        os.close(fd)
