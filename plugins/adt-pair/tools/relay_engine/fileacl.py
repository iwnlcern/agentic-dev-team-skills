"""Private creation and access copying for an overwritten snapshot copy (OB-SNAP-ACL-1).

`create_private` opens a new file at mode 0o600 that carries no inherited
granting ACL entry from its creation onward, and `copy_access` makes a
descriptor's ACL equal to a source file's (DD-v3-engine-snapshot-acl D2 to D4).

macOS: `openx_np` with a filesec holding mode 0o600 and an empty ACL flagged
ACL_FLAG_NO_INHERIT, and `acl_get_link_np` / `acl_set_fd_np` for the extended
ACL; libc is bound on first use, and the native function objects are module
attributes so tests can wrap them.  Linux: `os.open` at 0o600, whose creation
mode masks a default ACL's named entries to nothing, and the
`system.posix_acl_access` extended attribute.  Other platforms: `os.open` at
0o600 and no ACL handling.  Not covered: NFSv4 ACLs on Linux, other platforms'
ACLs, and a filesystem that accepts ACL_FLAG_NO_INHERIT but ignores it.
"""
import ctypes
import errno
import os
import sys

_DARWIN = sys.platform == "darwin"
_LINUX = sys.platform.startswith("linux")

_FILESEC_MODE = 4
_FILESEC_ACL = 5
_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FLAG_NO_INHERIT = 1 << 17
_LINUX_ACCESS_ACL = "system.posix_acl_access"
# The replaced file's filesystem holds no ACL for it, or the stage holds none.
_NO_ACL = frozenset({errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENODATA})

# macOS native functions, bound by _bind() on first use.
_bound = False
_openx_np = None
_filesec_init = None
_filesec_free = None
_filesec_set_property = None
_acl_init = None
_acl_free = None
_acl_get_flagset_np = None
_acl_add_flag_np = None
_acl_set_flagset_np = None
_acl_get_link_np = None
_acl_set_fd_np = None


def _bind():
    """Bind libc's filesec and ACL functions once; no-op off macOS."""
    global _bound, _openx_np, _filesec_init, _filesec_free, _filesec_set_property
    global _acl_init, _acl_free, _acl_get_flagset_np, _acl_add_flag_np, _acl_set_flagset_np
    global _acl_get_link_np, _acl_set_fd_np
    if _bound or not _DARWIN:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    p, c_int = ctypes.c_void_p, ctypes.c_int

    def typed(function, restype, *argtypes):
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    _openx_np = typed(libc.openx_np, c_int, ctypes.c_char_p, c_int, p)
    _filesec_init = typed(libc.filesec_init, p)
    _filesec_free = typed(libc.filesec_free, None, p)
    _filesec_set_property = typed(libc.filesec_set_property, c_int, p, c_int, p)
    _acl_init = typed(libc.acl_init, p, c_int)
    _acl_free = typed(libc.acl_free, c_int, p)
    _acl_get_flagset_np = typed(libc.acl_get_flagset_np, c_int, p, ctypes.POINTER(p))
    _acl_add_flag_np = typed(libc.acl_add_flag_np, c_int, p, c_int)
    _acl_set_flagset_np = typed(libc.acl_set_flagset_np, c_int, p, p)
    _acl_get_link_np = typed(libc.acl_get_link_np, p, ctypes.c_char_p, c_int)
    _acl_set_fd_np = typed(libc.acl_set_fd_np, c_int, c_int, p, c_int)
    _bound = True


def _native_error(name, path=None):
    code = ctypes.get_errno()
    if path is None:
        return OSError(code, "%s: %s" % (name, os.strerror(code)))
    return OSError(code, os.strerror(code), os.fspath(path))


def _empty_acl(no_inherit):
    """A new empty ACL, flagged ACL_FLAG_NO_INHERIT when asked; the caller frees it."""
    acl = _acl_init(0)
    if not acl:
        raise _native_error("acl_init")
    try:
        if no_inherit:
            flagset = ctypes.c_void_p()
            if _acl_get_flagset_np(acl, ctypes.byref(flagset)) != 0:
                raise _native_error("acl_get_flagset_np")
            if _acl_add_flag_np(flagset, _ACL_FLAG_NO_INHERIT) != 0:
                raise _native_error("acl_add_flag_np")
            if _acl_set_flagset_np(acl, flagset) != 0:
                raise _native_error("acl_set_flagset_np")
    except BaseException:
        _acl_free(acl)
        raise
    return acl


def create_private(path, flags):
    """A descriptor for a new file at mode 0o600 that inherits no granting ACL entry.

    The creating call's OSError propagates unchanged; there is no fallback (D4).
    """
    if not _DARWIN:
        return os.open(path, flags, 0o600)
    _bind()
    fsec = _filesec_init()
    if not fsec:
        raise _native_error("filesec_init")
    try:
        mode = ctypes.c_uint16(0o600)
        if _filesec_set_property(fsec, _FILESEC_MODE, ctypes.byref(mode)) != 0:
            raise _native_error("filesec_set_property")
        acl = _empty_acl(no_inherit=True)
        try:
            pointer = ctypes.c_void_p(acl)
            if _filesec_set_property(fsec, _FILESEC_ACL, ctypes.byref(pointer)) != 0:
                raise _native_error("filesec_set_property")
        finally:
            _acl_free(acl)
        fd = _openx_np(os.fsencode(path), flags, fsec)
        if fd < 0:
            raise _native_error("openx_np", path)
        return fd
    finally:
        _filesec_free(fsec)


def copy_access(source_path, fd):
    """Make fd's ACL equal to source_path's, read without following a symlink.

    When the source has no ACL, fd's ACL is removed.  The mode is never changed.
    """
    if _DARWIN:
        _bind()
        acl = _acl_get_link_np(os.fsencode(source_path), _ACL_TYPE_EXTENDED)
        if not acl:
            code = ctypes.get_errno()
            if code != errno.ENOENT and code not in _NO_ACL:
                raise OSError(code, os.strerror(code), os.fspath(source_path))
            acl = _empty_acl(no_inherit=False)
            try:
                if _acl_set_fd_np(fd, acl, _ACL_TYPE_EXTENDED) != 0:
                    code = ctypes.get_errno()
                    if code not in _NO_ACL:
                        raise OSError(code, "acl_set_fd_np: " + os.strerror(code))
            finally:
                _acl_free(acl)
            return
        try:
            if _acl_set_fd_np(fd, acl, _ACL_TYPE_EXTENDED) != 0:
                raise _native_error("acl_set_fd_np")
        finally:
            _acl_free(acl)
        return
    if _LINUX:
        try:
            value = os.getxattr(source_path, _LINUX_ACCESS_ACL, follow_symlinks=False)
        except OSError as error:
            if error.errno not in _NO_ACL:
                raise
            value = None
        if value is not None:
            os.setxattr(fd, _LINUX_ACCESS_ACL, value)
            return
        try:
            os.removexattr(fd, _LINUX_ACCESS_ACL)
        except OSError as error:
            if error.errno not in _NO_ACL:
                raise
