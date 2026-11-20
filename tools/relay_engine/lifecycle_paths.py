"""Runtime paths, bounded diagnostic locator reads, and Console target selection."""
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Literal

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


def console_target(root: str) -> ConsoleTarget | ConsoleTargetUnavailable:
    """Resolve the selected Store's ordinary ready path after the held probe."""
    from relay_engine.lifecycle_state import probe_held_diagnostic

    observed = probe_held_diagnostic(root)
    if observed.kind == 'verified':
        return ConsoleTargetUnavailable('held_diagnostic')
    if observed.kind not in ('absent', 'stale_safe'):
        return ConsoleTargetUnavailable('diagnostic_unavailable')

    import sqlite3
    from relay_engine import errors, store_layout

    try:
        store_id = store_layout.read_store_id(root)
        ready_path = ready_file_path(store_id, platform=sys.platform, environ=os.environ,
                                     home=os.path.expanduser('~'))
    except (store_layout.LayoutFault, errors.EngineError, sqlite3.Error, OSError, ValueError) as fault:
        return ConsoleTargetUnavailable('identity_unavailable', fault)
    return ConsoleTarget(ready_path)
