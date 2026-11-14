"""In-place two-slot items. Callers hold the per-project flock."""
import ctypes
from dataclasses import dataclass, replace
from enum import IntEnum
import fcntl
import hashlib
import os
import secrets
import stat
import struct
import sys
import uuid

from .jcs import jcs_encode
from . import strings
from .seal_types import SealSlotDamaged

SIZE = 65792
OFFSETS = (0, 65536)


class ItemKind(IntEnum):
    COUNTER = 1
    HIGH_WATER = 2
    POINTER = 3


@dataclass(frozen=True, slots=True)
class LedgerIdentity:
    inode: int
    uuid: bytes | None


@dataclass(frozen=True, slots=True)
class SlotState:
    kind: ItemKind
    binding: tuple[str, ...]
    sequence: int
    value: int
    self_identity: LedgerIdentity
    ledger_identity: LedgerIdentity | None
    chain: bytes | None


class CannotRecord(Exception):
    def __init__(self, step: int):
        self.step = step
        super().__init__(step)


class _AttrList(ctypes.Structure):
    _fields_ = [('count', ctypes.c_ushort), ('reserved', ctypes.c_ushort),
                ('common', ctypes.c_uint32), ('volume', ctypes.c_uint32),
                ('directory', ctypes.c_uint32), ('file', ctypes.c_uint32),
                ('fork', ctypes.c_uint32)]


def _volume_uuid(fd, info):
    if sys.platform == 'darwin':
        # sys/attr.h: ATTR_VOL_UUID, gated by ATTR_VOL_INFO; fgetattrlist
        # reads the volume of this descriptor, without resolving a ledger path.
        attrs = _AttrList(5, 0, 0, 0x80040000, 0, 0, 0)
        buffer = ctypes.create_string_buffer(20)
        read = ctypes.CDLL(None, use_errno=True).fgetattrlist
        read.argtypes = [ctypes.c_int, ctypes.POINTER(_AttrList), ctypes.c_void_p,
                         ctypes.c_size_t, ctypes.c_ulong]
        read.restype = ctypes.c_int
        if read(fd, ctypes.byref(attrs), buffer, len(buffer), 0) == 0:
            if struct.unpack_from('=I', buffer.raw)[0] == 20:
                return buffer.raw[4:20]
        return None
    # A container may expose no block-device UUIDs. Absence stays absence;
    # neither st_dev nor f_fsid is used as a substitute UUID for comparison.
    try:
        for name in os.listdir('/dev/disk/by-uuid'):
            try:
                if os.stat('/dev/disk/by-uuid/' + name).st_rdev == info.st_dev:
                    return uuid.UUID(name).bytes
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


def file_identity(fd) -> LedgerIdentity:
    info = os.fstat(fd)
    return LedgerIdentity(info.st_ino, _volume_uuid(fd, info))


def identities_match(recorded, current):
    return (recorded.inode == current.inode and
            (recorded.uuid is None or current.uuid is None or recorded.uuid == current.uuid))


def _slot(state):
    data = bytearray(256)
    data[:8] = b'ADTSLOT1'
    struct.pack_into('<H', data, 8, 1)
    data[10] = state.kind
    data[11] = (1 if state.self_identity.uuid is not None else 0)
    if state.ledger_identity is not None and state.ledger_identity.uuid is not None:
        data[11] |= 2
    if state.chain is not None:
        data[11] |= 4
        data[112:148] = state.chain
    struct.pack_into('<QQ', data, 16, state.sequence, state.value)
    data[32:64] = hashlib.sha256(jcs_encode(list(state.binding))).digest()
    struct.pack_into('<Q', data, 64, state.self_identity.inode)
    if state.self_identity.uuid is not None:
        data[72:88] = state.self_identity.uuid
    if state.ledger_identity is not None:
        struct.pack_into('<Q', data, 88, state.ledger_identity.inode)
        if state.ledger_identity.uuid is not None:
            data[96:112] = state.ledger_identity.uuid
    data[224:] = hashlib.sha256(data[:224]).digest()
    return bytes(data)


def pair_bytes(kind, binding, self_identity, value, chain):
    state = SlotState(kind, tuple(binding), 0, value, self_identity, None, chain)
    slot = _slot(state)
    return slot + bytes(65280) + slot


def _parse(data, kind, binding):
    if (len(data) != 256 or data[:8] != b'ADTSLOT1' or
            struct.unpack_from('<H', data, 8)[0] != 1 or data[10] != kind or
            data[32:64] != hashlib.sha256(jcs_encode(list(binding))).digest() or
            hashlib.sha256(data[:224]).digest() != data[224:]):
        return None
    sequence, value = struct.unpack_from('<QQ', data, 16)
    own = LedgerIdentity(struct.unpack_from('<Q', data, 64)[0],
                         data[72:88] if data[11] & 1 else None)
    ledger = (LedgerIdentity(struct.unpack_from('<Q', data, 88)[0],
                            data[96:112] if data[11] & 2 else None)
              if kind == ItemKind.COUNTER else None)
    return SlotState(kind, tuple(binding), sequence, value, own, ledger,
                     data[112:148] if kind == ItemKind.POINTER and data[11] & 4 else None)


def read_slots(fd, *, kind, binding, ledger_identity):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size != SIZE:
        raise CannotRecord(2)
    raw = [os.pread(fd, 256, offset) for offset in OFFSETS]
    slots = [_parse(data, kind, binding) for data in raw]
    # Erratum 2: equal well-formed sequences must reach step 5 BEFORE parity.
    if all(slots) and slots[0].sequence == slots[1].sequence:
        if slots[0].sequence != 0 or raw[0] != raw[1]:
            raise CannotRecord(5)
        current = slots[0]
    else:
        kept = [(index, slot) for index, slot in enumerate(slots)
                if slot is not None and slot.sequence % 2 == index]
        if not kept:
            raise CannotRecord(7)
        if len(kept) == 2:
            older, current = sorted((item[1] for item in kept), key=lambda item: item.sequence)
            if current.value < older.value:
                raise CannotRecord(4)
        else:
            index, current = kept[0]
            strings.report_diagnostic(SealSlotDamaged(
                {1: 'counter', 2: 'high-water', 3: 'pointer'}[kind],
                binding[0], binding[1] if len(binding) > 1 else None, 1 - index))
    if not identities_match(current.self_identity, file_identity(fd)):
        raise CannotRecord(8)
    if kind == ItemKind.COUNTER and ledger_identity is not None:
        if not identities_match(current.ledger_identity, ledger_identity):
            raise CannotRecord(8)
    return current


def _sync(fd, *, changing_length=False):
    if sys.platform == 'darwin':
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    elif changing_length:
        os.fsync(fd)
    else:
        os.fdatasync(fd)


def _write(fd, data, offset):
    if os.pwrite(fd, data, offset) != len(data):
        raise CannotRecord(7)


def write_next(fd, state, *, value, chain=None):
    # Re-read so an identity replacement cannot be hidden by a retained state.
    current = read_slots(fd, kind=state.kind, binding=state.binding,
                         ledger_identity=state.ledger_identity)
    updated = replace(current, sequence=current.sequence + 1, value=value,
                      chain=chain if state.kind == ItemKind.POINTER else None)
    try:
        _write(fd, _slot(updated), OFFSETS[updated.sequence % 2])
        _sync(fd)
    except OSError as exc:
        raise CannotRecord(7) from exc


def rewrite_identity(fd, state, identity):
    current = read_slots(fd, kind=state.kind, binding=state.binding, ledger_identity=None)
    updated = replace(current, sequence=current.sequence + 1, ledger_identity=identity)
    try:
        _write(fd, _slot(updated), OFFSETS[updated.sequence % 2])
        _sync(fd)
    except OSError as exc:
        raise CannotRecord(7) from exc


def create(dir_fd, name, *, kind, binding, value, identity_from):
    temporary = '.creating-' + name + '.' + secrets.token_hex(8)
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
    try:
        state = SlotState(kind, tuple(binding), 0, value, file_identity(fd),
                          file_identity(identity_from) if identity_from is not None else None, None)
        slot = _slot(state)
        os.ftruncate(fd, SIZE)
        _write(fd, slot, 0)
        _write(fd, slot, 65536)
        _sync(fd, changing_length=True)
        os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=dir_fd)
        os.fsync(dir_fd)
        if sys.platform == 'darwin':
            _sync(fd, changing_length=True)
        return fd
    except BaseException:
        os.close(fd)
        raise
