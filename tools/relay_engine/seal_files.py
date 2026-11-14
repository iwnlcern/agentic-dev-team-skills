"""Seal-folder I/O. Item callers hold the project's flock across read/write."""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import weakref

from . import seal_slots
from .seal_types import RecordUnreadable, require_canonical_id


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class Key:
    seq: int
    key_id: str


_KEY_BYTES = weakref.WeakKeyDictionary()
_TAG = b'Signature: 8a477f597d28d172789f06886806bc55\n'
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def mac(key, context: bytes, data: bytes) -> str:
    return hmac.new(_KEY_BYTES[key], context + data, hashlib.sha256).hexdigest()


def _number(value):
    if type(value) is not int or value < 0:
        raise ValueError('noncanonical_number')
    return str(value)


def _decimal(value, *, positive=False):
    return re.fullmatch(r'[1-9][0-9]*' if positive else r'(?:0|[1-9][0-9]*)', value) is not None


def _read(path):
    fd = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ELOOP, 'not_regular')
        parts = []
        while True:
            part = os.read(fd, 65536)
            if not part:
                return b''.join(parts)
            parts.append(part)
    finally:
        os.close(fd)


def _publish(path, data, *, compare=False, keep_conflict=False):
    """Link complete bytes; return false on an existing, differing final name."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    temporary = '.creating-' + path.name + '.' + secrets.token_hex(8)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW |
                 os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        if os.pwrite(fd, data, 0) != len(data):
            raise OSError(errno.EIO, 'short_write')
        seal_slots._sync(fd, changing_length=True)
        try:
            os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            published = True
        except FileExistsError:
            published = compare and _read(path) == data
        if published or not keep_conflict:
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        if sys.platform == 'darwin':
            seal_slots._sync(fd, changing_length=True)
        return published
    finally:
        os.close(fd)
        os.close(directory)


def _make_dirs(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        parent = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            seal_slots._sync(parent, changing_length=True)
        finally:
            os.close(parent)


class SealFolder:
    def __init__(self, root: os.PathLike):
        self.root = Path(root)

    def _root(self):
        _make_dirs(self.root)

    def _project(self, project_id):
        require_canonical_id(project_id)
        return self.root / 'projects' / project_id

    def _item(self, project_id, chain_id, suffix):
        require_canonical_id(chain_id)
        return self._project(project_id) / (chain_id + suffix)

    def project_dir(self, project_id):
        path = self._project(project_id)
        self._root()
        _make_dirs(self.root / 'projects')
        _make_dirs(path)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    @contextmanager
    def lock(self, project_id, *, exclusive):
        require_canonical_id(project_id)
        directory = self.project_dir(project_id)
        try:
            fd = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, 'not_regular')
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _names(self, project_id):
        path = self._project(project_id)
        try:
            names = os.listdir(path)
        except FileNotFoundError:
            return (), ()
        try:
            records = os.listdir(path / 'records')
        except FileNotFoundError:
            records = ()
        return names, records

    @staticmethod
    def _chain(name):
        if len(name) < 36:
            return None
        chain, suffix = name[:36], name[36:]
        try:
            require_canonical_id(chain)
        except ValueError:
            return None
        if suffix in ('.counter', '.high-water', '.broken'):
            return chain
        if suffix.startswith('.entry.') and _decimal(suffix[7:]):
            return chain
        return None

    def _listed_chains(self, names, records):
        found = {chain for name in names if (chain := self._chain(name)) is not None}
        for name in records:
            try:
                found.add(require_canonical_id(name))
            except ValueError:
                pass
        return tuple(sorted(found))

    def chains(self, project_id):
        return self._listed_chains(*self._names(project_id))

    def folder_state(self, project_id):
        require_canonical_id(project_id)
        try:
            names, records = self._names(project_id)
            return 'present' if 'pointer' in names or self._listed_chains(names, records) else 'absent'
        except OSError:
            return 'unreadable'

    def write_entry(self, project_id, chain_id, number, data):
        path = self._item(project_id, chain_id, '.entry.' + _number(number))
        if _publish(path, data):
            return
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, 'not_regular')
            if os.pwrite(fd, data, 0) != len(data):
                raise OSError(errno.EIO, 'short_write')
            os.ftruncate(fd, len(data))
            seal_slots._sync(fd, changing_length=True)
        finally:
            os.close(fd)

    def read_entries(self, project_id, chain_id, *, after=-1, through=None):
        path = self._item(project_id, chain_id, '')
        try:
            names = os.listdir(path.parent)
        except FileNotFoundError:
            return ()
        prefix = chain_id + '.entry.'
        numbers = sorted(int(name[len(prefix):]) for name in names
                         if name.startswith(prefix) and _decimal(name[len(prefix):]))
        result = []
        for number in numbers:
            if number <= after or through is not None and number > through:
                continue
            try:
                data = _read(path.parent / (prefix + str(number)))
            except OSError:
                data = None
            result.append((number, data))
        return tuple(result)

    def write_mark(self, project_id, chain_id, data):
        _publish(self._item(project_id, chain_id, '.broken'), data)

    def mark_exists(self, project_id, chain_id):
        path = self._item(project_id, chain_id, '.broken')
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False

    def usable_keys(self):
        try:
            names = os.listdir(self.root / 'keys')
        except FileNotFoundError:
            return []
        result = []
        for name in sorted((n for n in names if _decimal(n, positive=True)), key=int):
            fd = None
            try:
                fd = os.open(self.root / 'keys' / name, _READ_FLAGS)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 32):
                    continue
                data = os.read(fd, 33)
                if len(data) != 32:
                    continue
                key = Key(int(name), hashlib.sha256(data).hexdigest()[:32])
                _KEY_BYTES[key] = data
                result.append(key)
            except OSError:
                continue
            finally:
                if fd is not None:
                    os.close(fd)
        return result

    def create_key(self):
        self._root()
        directory = self.root / 'keys'
        _make_dirs(directory)
        sequence = max((int(n) for n in os.listdir(directory) if _decimal(n, positive=True)), default=0) + 1
        data = secrets.token_bytes(32)
        if _publish(directory / str(sequence), data):
            key = Key(sequence, hashlib.sha256(data).hexdigest()[:32])
            _KEY_BYTES[key] = data
            return key
        keys = self.usable_keys()
        if keys:
            return keys[-1]
        raise OSError(errno.EIO, 'key_race_unusable')

    def publish_record(self, project_id, chain_id, number, data):
        require_canonical_id(project_id)
        require_canonical_id(chain_id)
        name = _number(number)
        directory = self._project(project_id) / 'records'
        _make_dirs(directory)
        directory = directory / chain_id
        _make_dirs(directory)
        return _publish(directory / name, data, compare=True, keep_conflict=True)

    def record_head(self, project_id, chain_id):
        require_canonical_id(project_id)
        require_canonical_id(chain_id)
        try:
            names = os.listdir(self._project(project_id) / 'records' / chain_id)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RecordUnreadable(0, 'not_regular' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 'io') from None
        numbers = {int(name) for name in names if _decimal(name)}
        number = 0
        while number in numbers:
            number += 1
        return number - 1 if number else None

    def open_record_file(self, project_id, chain_id, number):
        require_canonical_id(project_id)
        require_canonical_id(chain_id)
        name = _number(number)
        try:
            return _read(self._project(project_id) / 'records' / chain_id / name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RecordUnreadable(number, 'not_regular' if exc.errno == errno.ELOOP else 'io') from None

    def apply_backup_exclusion(self, run):
        self._root()
        if sys.platform == 'darwin':
            if run(['tmutil', 'addexclusion', str(self.root)]).returncode != 0:
                return 'add_failed'
            checked = run(['tmutil', 'isexcluded', str(self.root)])
            if checked.returncode != 0:
                return 'check_failed'
            return 'excluded' if checked.stdout.splitlines()[:1] and checked.stdout.splitlines()[0].startswith(b'[Excluded]') else 'not_excluded'
        try:
            _publish(self.root / 'CACHEDIR.TAG', _TAG)
        except OSError:
            return 'add_failed'
        try:
            contents = _read(self.root / 'CACHEDIR.TAG')
        except OSError:
            return 'check_failed'
        return 'excluded' if contents.startswith(_TAG) else 'not_excluded'
