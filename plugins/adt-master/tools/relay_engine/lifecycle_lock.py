"""Holder record shape and operations on an already-owned lock."""
import errno
import json
import os
from dataclasses import dataclass


HOLDERS_ENGINE = "engine"


@dataclass(frozen=True, slots=True)
class HolderRecord:
    """Supplied record shape, not evidence of a live pid or held lock."""
    pid: int
    holder: str

    def __post_init__(self):
        if type(self.pid) is not int or not 1 <= self.pid <= 2**64 - 1:
            raise ValueError('invalid_holder_pid')
        value = self.holder
        if (type(value) is not str or '\n' in value or '\r' in value
                or not (value == 'engine'
                    or (value.startswith('adt engine ') and value[len('adt engine '):].strip())
                    or (value.startswith('adt store ') and value[len('adt store '):].strip()))):
            raise ValueError('invalid_holder_name')


def write_record(fd: int, record: HolderRecord) -> None:
    """Replace the record on the caller's locked descriptor."""
    frame = json.dumps({'pid': record.pid, 'holder': record.holder},
                       separators=(',', ':')).encode('utf-8') + b'\n'
    os.ftruncate(fd, 0)
    if os.pwrite(fd, frame, 0) != len(frame):
        raise OSError(errno.EIO, 'short lifecycle holder write')


def clear_record(fd: int) -> None:
    """Clear the record while the caller still owns the lock."""
    os.ftruncate(fd, 0)


def read_record(lock_path: str) -> HolderRecord | None:
    """Return the complete holder record if its PID still exists."""
    try:
        with open(lock_path, 'rb') as stream:
            raw = stream.read()
    except FileNotFoundError:
        return None
    if not raw.endswith(b'\n') or raw.count(b'\n') != 1:
        return None
    try:
        record = HolderRecord(**json.loads(raw))
    except (TypeError, ValueError, RecursionError):
        return None

    # Darwin and Linux use a signed 32-bit pid_t. Avoid narrowing large values.
    if record.pid > 2**31 - 1:
        return None
    try:
        os.kill(record.pid, 0)
    except OverflowError:
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        if exc.errno != errno.EPERM:
            raise
    return record
