"""Test-only ACL fixtures and readers, independent of relay_engine.fileacl (OB-SNAP-ACL-1).

macOS: real extended ACLs through ctypes, freeing every native object it
allocates.  Linux: POSIX ACLs as `system.posix_acl_access` and
`system.posix_acl_default` extended attributes, encoded and decoded here.
On Linux every folder that sets or reads an ACL lives under
RELAY_ENGINE_ACL_DIR; when it is unset, or its filesystem refuses a default
ACL, the calling test fails with a message naming the variable, never skips.
"""
import ctypes
import errno
import os
import shutil
import struct
import sys
import tempfile

DARWIN = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
ACL_DIR_VARIABLE = "RELAY_ENGINE_ACL_DIR"

_ACL_TYPE_EXTENDED = 0x00000100
_ACCESS = "system.posix_acl_access"
_DEFAULT = "system.posix_acl_default"
_VERSION = 2
USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
_UNDEFINED = 0xFFFFFFFF
_ENTRY = struct.Struct("<HHI")


def _darwin_group(gid, name):
    return "group:ABCDEFAB-CDEF-ABCD-EFAB-CDEF%08X:%s:%d" % (gid, name, gid)


# macOS entry texts used by the tests, in acl_to_text's own form.
EVERYONE_INHERITED_READ = _darwin_group(12, "everyone") + ":allow,file_inherit,directory_inherit:read"
STAFF_DENY_WRITE = _darwin_group(20, "staff") + ":deny:write"

_libc = None


def _lib():
    global _libc
    if _libc is None:
        libc = ctypes.CDLL(None, use_errno=True)
        p = ctypes.c_void_p
        for name, restype, argtypes in (
                ("acl_from_text", p, [ctypes.c_char_p]),
                ("acl_to_text", p, [p, ctypes.POINTER(ctypes.c_ssize_t)]),
                ("acl_init", p, [ctypes.c_int]),
                ("acl_free", ctypes.c_int, [p]),
                ("acl_get_link_np", p, [ctypes.c_char_p, ctypes.c_int]),
                ("acl_get_fd_np", p, [ctypes.c_int, ctypes.c_int]),
                ("acl_set_link_np", ctypes.c_int, [ctypes.c_char_p, ctypes.c_int, p])):
            function = getattr(libc, name)
            function.restype, function.argtypes = restype, argtypes
        _libc = libc
    return _libc


def _darwin_raise(name, path=None):
    code = ctypes.get_errno()
    raise OSError(code, "%s: %s" % (name, os.strerror(code)), *(() if path is None else (os.fspath(path),)))


def _darwin_set(path, entries):
    libc = _lib()
    if entries:
        acl = libc.acl_from_text(("!#acl 1\n" + "".join(e + "\n" for e in entries)).encode())
    else:
        acl = libc.acl_init(0)
    if not acl:
        _darwin_raise("acl_from_text")
    try:
        if libc.acl_set_link_np(os.fsencode(path), _ACL_TYPE_EXTENDED, acl) != 0:
            _darwin_raise("acl_set_link_np", path)
    finally:
        libc.acl_free(acl)


def _darwin_text(acl):
    libc = _lib()
    try:
        size = ctypes.c_ssize_t()
        text = libc.acl_to_text(acl, ctypes.byref(size))
        if not text:
            _darwin_raise("acl_to_text")
        try:
            lines = ctypes.string_at(text).decode().splitlines()
        finally:
            libc.acl_free(text)
    finally:
        libc.acl_free(acl)
    return [line for line in lines if line and not line.startswith("!#acl")]


def darwin_entries_path(path):
    """The path's extended ACL entries as acl_to_text lines, [] when it has none."""
    acl = _lib().acl_get_link_np(os.fsencode(path), _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return []
        _darwin_raise("acl_get_link_np", path)
    return _darwin_text(acl)


def darwin_entries_fd(fd):
    """The descriptor's extended ACL entries as acl_to_text lines, [] when it has none."""
    acl = _lib().acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return []
        _darwin_raise("acl_get_fd_np")
    return _darwin_text(acl)


def encode(entries):
    """Bytes of a POSIX ACL xattr from (tag, perm, id) entries."""
    return struct.pack("<I", _VERSION) + b"".join(_ENTRY.pack(tag, perm, ident) for tag, perm, ident in entries)


def decode(value):
    """(tag, perm, id) entries of a POSIX ACL xattr, checked as version 2."""
    if len(value) < 4 or (len(value) - 4) % _ENTRY.size:
        raise ValueError("not a POSIX ACL attribute: %r" % (value,))
    (version,) = struct.unpack_from("<I", value)
    if version != _VERSION:
        raise ValueError("POSIX ACL attribute version %d" % version)
    return [_ENTRY.unpack_from(value, offset) for offset in range(4, len(value), _ENTRY.size)]


def linux_entries(target, name=_ACCESS):
    """Decoded entries of the target's (path or fd) ACL attribute, None when it has none."""
    try:
        value = os.getxattr(target, name, follow_symlinks=False) if not isinstance(target, int) \
            else os.getxattr(target, name)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        raise
    return decode(value)


def linux_grants(entries):
    """Whether a decoded access ACL lets anyone but the owner do anything."""
    if entries is None:
        return False
    mask = next((perm for tag, perm, _ in entries if tag == MASK), None)
    for tag, perm, _ in entries:
        if tag == OTHER and perm:
            return True
        if tag in (USER, GROUP, GROUP_OBJ) and perm & (0o7 if mask is None else mask):
            return True
    return False


def named_user_read(uid=None):
    """Linux access entries: the owner rw-, the writer's own uid as named user r--, mask r--."""
    uid = os.geteuid() if uid is None else uid
    return [(USER_OBJ, 6, _UNDEFINED), (USER, 4, uid), (GROUP_OBJ, 0, _UNDEFINED),
            (MASK, 4, _UNDEFINED), (OTHER, 0, _UNDEFINED)]


def everyone_read_default(uid=None):
    """Linux default entries granting read to a named user (the writer) and to other."""
    uid = os.geteuid() if uid is None else uid
    return [(USER_OBJ, 6, _UNDEFINED), (USER, 4, uid), (GROUP_OBJ, 4, _UNDEFINED),
            (MASK, 4, _UNDEFINED), (OTHER, 4, _UNDEFINED)]


def acl_folder(test):
    """A fresh folder for one test, removed by its cleanup; under RELAY_ENGINE_ACL_DIR on Linux."""
    parent = None
    if LINUX:
        parent = os.environ.get(ACL_DIR_VARIABLE)
        if not parent:
            test.fail("%s is not set; on Linux the ACL tests need a folder on a file system "
                      "that accepts a default ACL" % ACL_DIR_VARIABLE)
    folder = tempfile.mkdtemp(prefix="acl-", dir=parent)
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


def set_inheritable_grant(test, folder):
    """Give folder an inheritable ACL that grants everyone (macOS) or other and a named user (Linux) read."""
    if DARWIN:
        _darwin_set(folder, [EVERYONE_INHERITED_READ])
    elif LINUX:
        try:
            os.setxattr(folder, _DEFAULT, encode(everyone_read_default()))
        except OSError as error:
            if error.errno in (errno.EOPNOTSUPP, errno.ENOTSUP):
                test.fail("%s=%s refuses a default ACL (%s); name a folder on a file system "
                          "that accepts one" % (ACL_DIR_VARIABLE, os.environ.get(ACL_DIR_VARIABLE),
                                                 error))
            raise
    else:
        test.fail("no ACL fixture on %s" % sys.platform)


def set_explicit_entry(path):
    """Give path its explicit narrowing entry: macOS group:staff deny write; Linux a named-user read with mask r--."""
    if DARWIN:
        _darwin_set(path, [STAFF_DENY_WRITE])
    else:
        os.setxattr(path, _ACCESS, encode(named_user_read()), follow_symlinks=False)


def clear(path):
    """Remove path's own ACL: macOS an empty ACL; Linux the access attribute."""
    if DARWIN:
        _darwin_set(path, [])
    else:
        try:
            os.removexattr(path, _ACCESS, follow_symlinks=False)
        except OSError as error:
            if error.errno != errno.ENODATA:
                raise


def entries(target):
    """The target's (path or fd) own ACL, in each platform's form: macOS text lines; Linux decoded entries or None."""
    if DARWIN:
        return darwin_entries_fd(target) if isinstance(target, int) else darwin_entries_path(target)
    return linux_entries(target)


def grants(target):
    """Whether the target's ACL grants anyone but its owner access."""
    if DARWIN:
        return any(":allow" in line for line in entries(target))
    return linux_grants(entries(target))


def inherited(target):
    """Whether the target carries an entry that came from the folder's inheritable ACL."""
    if DARWIN:
        return any(",inherited" in line.split(":")[4] for line in entries(target))
    found = entries(target)
    return found is not None and any(tag == USER for tag, _, _ in found)


def filesec_mode(fsec):
    """The FILESEC_MODE a macOS filesec carries, read independently of fileacl."""
    libc = _lib()
    function = libc.filesec_get_property
    function.restype, function.argtypes = ctypes.c_int, [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    mode = ctypes.c_uint16()
    if function(fsec, 4, ctypes.byref(mode)) != 0:
        _darwin_raise("filesec_get_property")
    return mode.value
