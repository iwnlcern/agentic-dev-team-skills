"""Durable per-window connection records; credential paths only, never key bytes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import stat
import tempfile


@dataclass(frozen=True)
class WindowRecord:
    window_key: str
    host: str
    host_session_id: str
    seat_id: str
    seat_address: str
    project_id: str
    key_path: str
    session_id: str
    generation: int
    ready_path: str
    connected: bool
    written_at_ms: int


def _path(window_key: str, *, home: Path) -> Path:
    if (not isinstance(window_key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", window_key) is None
            or ".." in window_key):
        raise ValueError("invalid-window-key")
    return Path(home) / ".adt" / "windows" / (window_key + ".json")


def _decode(data: bytes, window_key: str) -> WindowRecord:
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != set(WindowRecord.__dataclass_fields__):
        raise ValueError("invalid window record fields")
    strings = set(value) - {"generation", "connected", "written_at_ms"}
    if any(not isinstance(value[k], str) or not value[k] for k in strings):
        raise ValueError("invalid window record string")
    if value["window_key"] != window_key or value["host"] not in ("codex", "claude-code"):
        raise ValueError("invalid window identity")
    if (type(value["connected"]) is not bool
            or any(type(value[k]) is not int or value[k] < 0 for k in ("generation", "written_at_ms"))):
        raise ValueError("invalid window record value")
    return WindowRecord(**value)


def read(window_key: str, *, home: Path) -> WindowRecord | None:
    """An absent or unreadable record provides no connection identity."""
    path = _path(window_key, home=home)
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
        return _decode(data, window_key)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None
    finally:
        os.close(fd)


def write(record: WindowRecord, *, home: Path) -> None:
    """Replace one complete private record atomically without opening its key path."""
    path = _path(record.window_key, home=home)
    data = (json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    _decode(data, record.window_key)
    if len(data) > 65536:
        raise ValueError("window record too large")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    folder_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(folder_fd).st_uid != os.getuid():
            raise PermissionError("window record directory owner")
        os.fchmod(folder_fd, 0o700)
    finally:
        os.close(folder_fd)
    fd, name = tempfile.mkstemp(prefix=".window-", dir=path.parent)
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


def mark_not_connected(window_key: str, *, home: Path) -> WindowRecord | None:
    record = read(window_key, home=home)
    if record is None:
        return None
    disconnected = replace(record, connected=False)
    write(disconnected, home=home)
    return disconnected
