"""Runtime paths, bounded diagnostic locator reads, and Console target selection."""
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from typing import Literal
from uuid import UUID

_CAUSES = ('store_job_open', 'catalogue_unopenable', 'catalogue_faulted',
           'start_fault', 'ingest_fault')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_PID = re.compile(r'[1-9][0-9]{0,19}\Z')
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True, slots=True)
class ReadyFields:
    store_state: Literal['serving', 'held']
    held_causes: tuple[str, ...]

    def __post_init__(self):
        causes = self.held_causes
        if (self.store_state not in ('serving', 'held') or type(causes) is not tuple
                or any(type(c) is not str or c not in _CAUSES for c in causes)
                or tuple(c for c in _CAUSES if c in causes) != causes
                or (self.store_state == 'serving') != (not causes)):
            raise ValueError('invalid_ready_fields')


@dataclass(frozen=True, slots=True)
class ConsoleTarget:
    ready_path: str


@dataclass(frozen=True, slots=True)
class ConsoleTargetUnavailable:
    reason: Literal['held_diagnostic', 'identity_unavailable', 'diagnostic_unavailable']
    # Retained for the local caller only. The refusal's display contains only its reason.
    identity_fault: Exception | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.reason) is not str or self.reason not in (
                'held_diagnostic', 'identity_unavailable', 'diagnostic_unavailable')):
            raise ValueError('invalid_console_target_reason')
        if self.reason == 'identity_unavailable':
            if not isinstance(self.identity_fault, Exception):
                raise ValueError('missing_console_identity_fault')
        elif self.identity_fault is not None:
            raise ValueError('unexpected_console_identity_fault')


class HeldMetadataUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class HeldLocatorPaths:
    root_key: str
    runtime_instance_id: str
    socket_path: str
    descriptor_path: str
    pointer_path: str


@dataclass(frozen=True, slots=True)
class HeldLocatorRead:
    """File observations only: build/schema/pid here are NOT verified or live."""
    paths: HeldLocatorPaths
    build_id: str
    schema_digest: str
    pid: int
    held_causes: tuple[str, ...]


def canonical_locator_root(root: str) -> str:
    if type(root) is not str or '\0' in root:
        raise ValueError('invalid_locator_root')
    # fsencode accepts surrogateescape text, but rejects unencodable surrogates.
    os.fsencode(root)
    resolved = Path(root).resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    result = str(resolved)
    os.fsencode(result)
    return result


def _private_directory(status):
    return (stat.S_ISDIR(status.st_mode) and status.st_uid == os.getuid()
            and stat.S_IMODE(status.st_mode) == 0o700)


def _runtime_base(*, platform: str, environ: Mapping[str, str], home: str):
    if platform not in ('darwin', 'linux'):
        raise ValueError('unsupported_platform')
    if type(home) is not str or '\0' in home or not Path(home).is_absolute():
        raise ValueError('invalid_runtime_home')
    os.fsencode(home)
    xdg = environ.get('XDG_RUNTIME_DIR') if platform == 'linux' else None
    if type(xdg) is str and xdg and '\0' not in xdg and Path(xdg).is_absolute():
        try:
            if _private_directory(os.lstat(xdg)):
                return Path(xdg), ('adt',)
        except (OSError, UnicodeError):
            pass
    return Path(home), ('.adt', 'run')


def _base_path(anchor, components):
    return anchor.joinpath(*components)


def _inspect_directories(anchor, components):
    """Check existing runtime components; missing components are not created."""
    for component in components:
        anchor = anchor / component
        try:
            status = anchor.lstat()
        except FileNotFoundError:
            return
        if not _private_directory(status):
            raise ValueError('runtime_dir_unsafe')


def runtime_dir(store_id: str, *, platform: str, environ: Mapping[str, str], home: str) -> str:
    # Store ids are canonical UUID text; the hex form is also useful to pure callers.
    if type(store_id) is not str:
        raise ValueError('invalid_store_id')
    identity = store_id.replace('-', '')
    if (not _HEX32.fullmatch(identity) or
            (store_id != identity and store_id != '-'.join((identity[:8], identity[8:12],
                identity[12:16], identity[16:20], identity[20:])))):
        raise ValueError('invalid_store_id')
    anchor, components = _runtime_base(platform=platform, environ=environ, home=home)
    components = (*components, identity[:16])
    _inspect_directories(anchor, components)
    return str(anchor.joinpath(*components))


def ready_file_path(store_id: str, *, platform: str, environ: Mapping[str, str], home: str) -> str:
    return str(Path(runtime_dir(store_id, platform=platform, environ=environ, home=home)) / 'ready.json')


def _socket_bound(path, platform):
    if len(os.fsencode(path)) + 1 > (104 if platform == 'darwin' else 108):
        raise ValueError('socket_path_unsupported')


def socket_path(store_id: str, *, platform: str, environ: Mapping[str, str], home: str) -> str:
    path = str(Path(runtime_dir(store_id, platform=platform, environ=environ, home=home)) / 'api.sock')
    _socket_bound(path, platform)
    return path


def create_runtime_dir(store_id: str, *, platform: str, environ: Mapping[str, str], home: str) -> Path:
    path = Path(socket_path(store_id, platform=platform, environ=environ, home=home)).parent
    anchor, components = _runtime_base(platform=platform, environ=environ, home=home)
    with ExitStack() as stack:
        fd = _open_directory(stack, str(anchor), private=False)
        for component in components:
            try:
                os.mkdir(component, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            fd = _open_directory(stack, component, parent=fd)
        os.mkdir(path.name, mode=0o700, dir_fd=fd)
    return path


def _paths(root_key, runtime_instance_id, base, platform):
    if type(runtime_instance_id) is not str or not _HEX32.fullmatch(runtime_instance_id):
        raise HeldMetadataUnavailable('diagnostic_metadata_invalid')
    directory = base / ('diagnostic-' + root_key[:16])
    run = directory / runtime_instance_id
    socket = str(run / 'api.sock')
    try:
        _socket_bound(socket, platform)
    except (ValueError, UnicodeError):
        raise HeldMetadataUnavailable('diagnostic_path_unsupported') from None
    return HeldLocatorPaths(root_key, runtime_instance_id, socket,
                            str(run / 'held.json'), str(directory / 'current.json'))


def _open_directory(stack, name, *, parent=None, private=True):
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    if private and not _private_directory(os.fstat(fd)):
        raise HeldMetadataUnavailable('diagnostic_metadata_unsafe')
    return fd


def _open_base(stack, anchor, components):
    fd = _open_directory(stack, str(anchor), private=False)
    for component in components:
        fd = _open_directory(stack, component, parent=fd)
    return fd


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_metadata_key')
        result[key] = value
    return result


def _read_metadata(parent, name, limit):
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        status = os.fstat(fd)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600):
            raise HeldMetadataUnavailable('diagnostic_metadata_unsafe')
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, limit + 1 - len(data))
            if not part:
                break
            data.extend(part)
        if len(data) > limit:
            raise HeldMetadataUnavailable('diagnostic_metadata_oversized')
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if type(value) is not dict:
            raise ValueError('metadata_object_required')
        return value
    finally:
        os.close(fd)


def _pointer(data, root_key):
    if (set(data) != {'format', 'root_key', 'runtime_instance_id'}
            or data['format'] != 'adt-held-locator-v1'
            or type(data['root_key']) is not str or not _HEX64.fullmatch(data['root_key'])
            or type(data['runtime_instance_id']) is not str
            or not _HEX32.fullmatch(data['runtime_instance_id'])):
        raise HeldMetadataUnavailable('diagnostic_metadata_invalid')
    if data['root_key'] != root_key:
        raise HeldMetadataUnavailable('diagnostic_root_collision')
    return data['runtime_instance_id']


def _selection(root):
    key = hashlib.sha256(os.fsencode(canonical_locator_root(root))).hexdigest()
    anchor, components = _runtime_base(platform=sys.platform, environ=os.environ,
                                      home=os.path.expanduser('~'))
    return key, anchor, components


def held_locator_paths(root: str, runtime_instance_id: str) -> HeldLocatorPaths:
    key, anchor, components = _selection(root)
    paths = _paths(key, runtime_instance_id, _base_path(anchor, components), sys.platform)
    try:
        with ExitStack() as stack:
            base = _open_base(stack, anchor, components)
            directory = _open_directory(stack, 'diagnostic-' + key[:16], parent=base)
            try:
                data = _read_metadata(directory, 'current.json', 1024)
            except FileNotFoundError:
                pass
            else:
                _pointer(data, key)
            try:
                _open_directory(stack, runtime_instance_id, parent=directory)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    except HeldMetadataUnavailable:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise HeldMetadataUnavailable('diagnostic_metadata_unsafe') from None
    return paths


def read_held_locator(root: str) -> HeldLocatorRead | None:
    key, anchor, components = _selection(root)
    try:
        with ExitStack() as stack:
            # Safe base/diagnostic/pointer absence is the sole None outcome.
            try:
                base = _open_base(stack, anchor, components)
                directory = _open_directory(stack, 'diagnostic-' + key[:16], parent=base)
                pointer = _read_metadata(directory, 'current.json', 1024)
            except FileNotFoundError:
                return None
            instance = _pointer(pointer, key)
            paths = _paths(key, instance, _base_path(anchor, components), sys.platform)
            run = _open_directory(stack, instance, parent=directory)
            data = _read_metadata(run, 'held.json', 4096)
            if (set(data) != {'format', 'root_key', 'runtime_instance_id', 'socket',
                             'build_id', 'schema_digest', 'pid', 'held_causes'}
                    or data['format'] != 'adt-held-diagnostic-v1'
                    or data['root_key'] != key or data['runtime_instance_id'] != instance
                    or data['socket'] != paths.socket_path
                    or type(data['build_id']) is not str or not _HEX64.fullmatch(data['build_id'])
                    or type(data['schema_digest']) is not str or not _HEX64.fullmatch(data['schema_digest'])
                    or type(data['pid']) is not str or not _PID.fullmatch(data['pid'])
                    or int(data['pid']) > 2**64 - 1
                    or data['held_causes'] != ['catalogue_unopenable']):
                raise HeldMetadataUnavailable('diagnostic_metadata_invalid')
            return HeldLocatorRead(paths, data['build_id'], data['schema_digest'],
                                   int(data['pid']), ('catalogue_unopenable',))
    except HeldMetadataUnavailable:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise HeldMetadataUnavailable('diagnostic_metadata_unsafe') from None


_LIVE_LOCATOR = '.adt-store-live.json'


def _native_identity(pid):
    """Return full native process birth and boot in the caller's local view."""
    if type(pid) is not int or not 0 < pid < 2**31:
        raise ValueError('invalid process')
    if sys.platform == 'linux':
        boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        process = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        if process.partition(' (')[0] != str(pid):
            raise ValueError('wrong process')
        birth = process.rsplit(')', 1)[1].split()[19]
        if not birth.isascii() or not birth.isdecimal():
            raise ValueError('invalid Linux birth')
    elif sys.platform == 'darwin':
        import ctypes

        class BsdInfo(ctypes.Structure):
            _fields_ = [(field, ctypes.c_uint32) for field in (
                'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid',
                'svuid', 'svgid', 'rfu')] + [
                ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
                (field, ctypes.c_uint32) for field in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')] + [
                ('nice', ctypes.c_int32), ('seconds', ctypes.c_uint64), ('microseconds', ctypes.c_uint64)]

        lib = ctypes.CDLL('/usr/lib/libproc.dylib')
        info = BsdInfo()
        size = ctypes.sizeof(info)
        if lib.proc_pidinfo(pid, 3, ctypes.c_uint64(0), ctypes.byref(info), size) != size or info.pid != pid:
            raise ValueError('unverified Darwin process')
        if not info.seconds or info.microseconds >= 1_000_000:
            raise ValueError('invalid Darwin birth')
        birth = f'{info.seconds}.{info.microseconds:06d}'
        boot = subprocess.check_output(('/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'),
                                       timeout=1, encoding='ascii').strip()
    else:
        raise ValueError('native identity unavailable')
    canonical_boot = str(UUID(boot))
    if canonical_boot != boot.lower():
        raise ValueError('invalid boot identity')
    return birth, canonical_boot


def _peer_pid(peer):
    if sys.platform == 'linux':
        credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, uid, _ = struct.unpack('=iII', credentials)
        if uid != os.geteuid():
            raise ValueError('wrong peer owner')
        return pid
    if sys.platform == 'darwin':
        return peer.getsockopt(0, 2)  # SOL_LOCAL, LOCAL_PEERPID
    raise ValueError('native peer identity unavailable')


def _read_live_locator(root):
    from relay_engine import api_client, store_layout

    def inode(info):
        return [info.st_dev, info.st_ino]

    with ExitStack() as stack:
        directory = _open_directory(stack, root)
        data = _read_metadata(directory, _LIVE_LOCATOR, 8192)
        if set(data) != {'arm', 'runtime', 'socket', 'ready', 'build', 'schema', 'instance'}:
            raise ValueError('invalid live locator')
        arm = store_layout.IncumbentGuard._parse_record(store_layout._canonical_frame(data['arm']))
        record = _read_metadata(directory, store_layout._INCUMBENT, store_layout._INCUMBENT_LIMIT)
        if record != data['arm'] or arm.phase != 'armed' or list(arm.root) != inode(os.fstat(directory)):
            raise ValueError('wrong live epoch')
        lock = os.stat(arm.lock_name, dir_fd=directory, follow_symlinks=False)
        if store_layout._owned_inode(lock) != arm.lock:
            raise ValueError('wrong live lock')
        owner = arm.owner
        if UUID(owner.epoch).hex != data['instance']:
            raise ValueError('wrong live instance')
        runtime = _open_directory(stack, owner.runtime)
        if inode(os.fstat(runtime)) != data['runtime']:
            raise ValueError('wrong live runtime')
        ready_info = os.stat('ready.json', dir_fd=runtime, follow_symlinks=False)
        sock_info = os.stat('api.sock', dir_fd=runtime, follow_symlinks=False)
        if (inode(ready_info) != data['ready'] or inode(sock_info) != data['socket']
                or not stat.S_ISSOCK(sock_info.st_mode) or sock_info.st_uid != os.geteuid()):
            raise ValueError('wrong live publication')
        ready = api_client.read_ready_file(str(Path(owner.runtime) / 'ready.json'))
        path = str(Path(owner.runtime) / 'api.sock')
        if ((ready.store_id, ready.pid, ready.socket, ready.build_id, ready.schema_digest) !=
                (arm.store, owner.pid, path, data['build'], data['schema'])):
            raise ValueError('wrong live endpoint')
        registry, digest = api_client._registry_and_digest()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            deadline = time.monotonic() + 0.5
            peer.settimeout(0.5)
            peer.connect(path)
            if _peer_pid(peer) != owner.pid or _native_identity(owner.pid) != (owner.birth, owner.boot):
                raise ValueError('wrong live endpoint')
            api_client.ApiClient(peer, registry, digest)._discover(ready, instance=data['instance'], deadline=deadline)
        if (_read_metadata(directory, _LIVE_LOCATOR, 8192) != data
                or _read_metadata(directory, store_layout._INCUMBENT, store_layout._INCUMBENT_LIMIT) != record
                or inode(os.stat(root, follow_symlinks=False)) != list(arm.root)
                or inode(os.stat(owner.runtime, follow_symlinks=False)) != data['runtime']
                or inode(os.stat('ready.json', dir_fd=runtime, follow_symlinks=False)) != data['ready']
                or inode(os.stat('api.sock', dir_fd=runtime, follow_symlinks=False)) != data['socket']):
            raise ValueError('changed live endpoint')
        return ready.path


def console_target(root: str) -> ConsoleTarget | ConsoleTargetUnavailable:
    """Resolve the selected Store through its checked live epoch."""
    from relay_engine import api_client, store_layout
    from relay_engine.lifecycle_state import probe_held_diagnostic

    observed = probe_held_diagnostic(root)
    if observed.kind == 'verified':
        return ConsoleTargetUnavailable('held_diagnostic')
    if observed.kind not in ('absent', 'stale_safe'):
        return ConsoleTargetUnavailable('diagnostic_unavailable')
    try:
        return ConsoleTarget(_read_live_locator(str(Path(root).absolute())))
    except (HeldMetadataUnavailable, store_layout.LayoutFault, api_client.LocalRefusal, api_client.EndpointUnavailable,
            OSError, ValueError, TypeError, IndexError, subprocess.SubprocessError) as fault:
        return ConsoleTargetUnavailable('identity_unavailable', fault)
