"""Root-confined filesystem operations anchored on held descriptors."""

import errno
import io
import os
import re
import secrets
import stat
import uuid
import ctypes


_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_SID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n")
_LIBC = ctypes.CDLL(None, use_errno=True)


def _fdatasync(fd):
    if _LIBC.fdatasync(fd) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _parts(rel):
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        raise ValueError("root-relative path required")
    pieces = rel.split("/")
    if any(piece in ("", ".", "..") for piece in pieces):
        raise ValueError("path traversal refused")
    return pieces


def _open_parent(root_fd, rel):
    pieces = _parts(rel)
    current = os.dup(root_fd)
    try:
        for piece in pieces[:-1]:
            following = os.open(piece, _OPEN_DIR, dir_fd=current)
            os.close(current)
            current = following
        return current, pieces[-1]
    except BaseException:
        os.close(current)
        raise


def _read_regular_at(parent_fd, name):
    fd = os.open(name, _OPEN_FILE, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "regular file required", name)
        data = os.read(fd, info.st_size + 1)
        if len(data) != info.st_size:
            raise OSError(errno.EIO, "file changed during read", name)
        return data
    finally:
        os.close(fd)


class Root:
    def __init__(self, path):
        canonical = os.path.realpath(os.path.abspath(path))
        self.dirfd = os.open(canonical, _OPEN_DIR)
        if not stat.S_ISDIR(os.fstat(self.dirfd).st_mode):
            os.close(self.dirfd)
            raise NotADirectoryError(canonical)
        self.path = canonical

    def close(self):
        if self.dirfd is not None:
            os.close(self.dirfd)
            self.dirfd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def open_read(self, rel):
        parent, name = _open_parent(self.dirfd, rel)
        try:
            return _read_regular_at(parent, name)
        finally:
            os.close(parent)

    def resolve_inside(self, rel):
        parent, name = _open_parent(self.dirfd, rel)
        try:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and stat.S_ISLNK(info.st_mode):
                raise OSError(errno.ELOOP, "symbolic link refused", name)
            return os.path.join(self.path, *_parts(rel))
        finally:
            os.close(parent)


class TempWrite:
    def __init__(self, root, rel):
        if not isinstance(root, Root) or root.dirfd is None:
            raise TypeError("open Root required")
        parent, name = _open_parent(root.dirfd, rel)
        self._initialize(parent, name)

    @classmethod
    def _from_parent(cls, parent_fd, name):
        if "/" in name or name in ("", ".", ".."):
            raise ValueError("single destination name required")
        instance = cls.__new__(cls)
        instance._initialize(os.dup(parent_fd), name)
        return instance

    def _initialize(self, parent_fd, name):
        self.parent_fd = parent_fd
        self.destination = name
        self.temp_name = None
        self.fd = None
        self._writer = None
        self._published = False
        for _ in range(32):
            candidate = ".relay-tmp-%s" % secrets.token_hex(16)
            try:
                fd = os.open(candidate,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            self.temp_name = candidate
            self.fd = fd
            self._writer = io.FileIO(fd, mode="wb", closefd=False)
            break
        if self.fd is None:
            os.close(parent_fd)
            raise FileExistsError("temporary-name collision exhaustion")

    def __enter__(self):
        return self

    def write(self, data):
        if not isinstance(data, bytes):
            raise TypeError("bytes required")
        view = memoryview(data)
        while view:
            written = self._writer.write(view)
            if not written:
                raise OSError(errno.EIO, "zero-progress write")
            view = view[written:]

    def _prepare(self):
        self._writer.flush()
        _fdatasync(self.fd)

    def rename_noclobber(self):
        self._prepare()
        os.link(self.temp_name, self.destination,
                src_dir_fd=self.parent_fd, dst_dir_fd=self.parent_fd,
                follow_symlinks=False)
        os.unlink(self.temp_name, dir_fd=self.parent_fd)
        self.temp_name = None
        os.fsync(self.parent_fd)
        self._published = True

    def rename_replace(self):
        self._prepare()
        os.rename(self.temp_name, self.destination,
                  src_dir_fd=self.parent_fd, dst_dir_fd=self.parent_fd)
        self.temp_name = None
        os.fsync(self.parent_fd)
        self._published = True

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._writer is not None:
                self._writer.close()
            if self.fd is not None:
                os.close(self.fd)
            if self.temp_name is not None:
                try:
                    os.unlink(self.temp_name, dir_fd=self.parent_fd)
                    os.fsync(self.parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(self.parent_fd)


def ensure_engine_dir(root_dirfd):
    try:
        os.mkdir(".engine", 0o700, dir_fd=root_dirfd)
        os.fsync(root_dirfd)
    except FileExistsError:
        pass
    fd = os.open(".engine", _OPEN_DIR, dir_fd=root_dirfd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotADirectoryError(".engine")
    return fd


def _draft_parent(root, draft):
    parent, name = _open_parent(root.dirfd, draft)
    try:
        fd = os.open(name, _OPEN_FILE, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "regular draft required", name)
        finally:
            os.close(fd)
        return parent, name
    except BaseException:
        os.close(parent)
        raise


def _read_sid(parent, name):
    data = _read_regular_at(parent, name)
    if _SID.fullmatch(data) is None:
        raise ValueError("submission id sidecar is malformed")
    value = data[:-1].decode("ascii")
    if str(uuid.UUID(value)) != value:
        raise ValueError("submission id is not canonical")
    return value


def sid_for(root, draft):
    parent, draft_name = _draft_parent(root, draft)
    sid_name = draft_name + ".sid"
    try:
        try:
            return _read_sid(parent, sid_name)
        except FileNotFoundError:
            pass
        value = str(uuid.uuid4())
        try:
            with TempWrite._from_parent(parent, sid_name) as pending:
                pending.write((value + "\n").encode("ascii"))
                pending.rename_noclobber()
        except FileExistsError:
            return _read_sid(parent, sid_name)
        return value
    finally:
        os.close(parent)


def clear_sid(root, draft):
    parent, draft_name = _draft_parent(root, draft)
    try:
        try:
            os.unlink(draft_name + ".sid", dir_fd=parent)
            os.fsync(parent)
        except FileNotFoundError:
            pass
    finally:
        os.close(parent)
