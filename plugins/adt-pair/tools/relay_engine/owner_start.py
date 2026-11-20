"""Cold operator proof for one selected Store and one native direct child."""
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
from subprocess import SubprocessError
import tempfile
from threading import Lock
import time
from typing import NamedTuple

from relay_engine import jcs, lifecycle_paths, operator_terminal, store_layout


_WINDOW_NS = 120_000_000_000
_MAX_NS = 2**63 - 1
_MAX_NATIVE_ID = 2**64 - 1
_MAX_FRAME = 16 * 1024
_IO_SECONDS = 2
_DECIMAL = re.compile(r'0|[1-9][0-9]*')
_HEX_VALUE = re.compile(r'[0-9a-f]{64}')
_clock_ns = time.monotonic_ns


class StartupRefused(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NativeIdentity(NamedTuple):
    pid: int
    birth: str
    boot: str

    @classmethod
    def read(cls, pid: int):
        try:
            return cls(pid, *lifecycle_paths._native_identity(pid))
        except (ValueError, OSError, IndexError, SubprocessError) as error:
            raise StartupRefused('startup_owner') from error

    @classmethod
    def current(cls):
        return cls.read(os.getpid())


@dataclass(frozen=True)
class StartSelection:
    root: Path
    identity: tuple[int, int]
    store: str
    project: str
    initialize: bool
    project_name: str | None
    bridge_port: int | None

    @classmethod
    def checked(cls, root, store: str, project: str, *, initialize: bool = False,
                project_name: str | None = None, bridge_port: int | None = None):
        try:
            root = Path(root).resolve()
            selection = cls(root, _selection_identity(root, initialize), store, project,
                            initialize, project_name, bridge_port)
            _selection_args(selection)
            if initialize and (root.exists() or root.is_symlink()):
                raise StartupRefused('startup_selection')
            return selection
        except (OSError, ValueError, RuntimeError) as error:
            raise StartupRefused('startup_selection') from error


def _production_root():
    root = store_layout.production_root()
    return root.parent.resolve() / root.name


def _selection_identity(root: Path, initialize: bool):
    path = root.parent if initialize else root
    try:
        fd = os.open(path, store_layout._DIRECTORY)
        try:
            actual, visible = os.fstat(fd), path.lstat()
            if not os.path.samestat(actual, visible):
                raise StartupRefused('startup_selection')
            if not initialize:
                return store_layout._owned_inode(actual, directory=True)
            if (actual.st_uid != os.geteuid() or actual.st_mode & 0o022
                    or not stat.S_ISDIR(visible.st_mode)):
                raise StartupRefused('startup_selection')
            return store_layout._inode(actual)
        finally:
            os.close(fd)
    except (OSError, store_layout.LayoutFault) as error:
        raise StartupRefused('startup_selection') from error


def _decimal(value, ceiling: int, reason: str) -> int:
    if type(value) is not str or len(value) > len(str(ceiling)) or not _DECIMAL.fullmatch(value):
        raise StartupRefused(reason)
    number = int(value)
    if number > ceiling:
        raise StartupRefused(reason)
    return number


def _selection_args(selection: StartSelection):
    root = selection.root
    try:
        store_layout._canonical_id(selection.store)
        store_layout._canonical_id(selection.project)
        if (not isinstance(root, Path) or root != _production_root()
                or root.resolve() != root or not root.name or len(os.fsencode(root)) > 1024
                or not str(root).isprintable() or type(selection.initialize) is not bool):
            raise StartupRefused('startup_selection')
        port = selection.bridge_port
        if port is not None and (type(port) is not int or not 0 <= port <= 65535):
            raise StartupRefused('startup_selection')
        name = selection.project_name
        if (selection.initialize and not store_layout._bounded_text(name)
                or not selection.initialize and name is not None):
            raise StartupRefused('startup_selection')
        identity = selection.identity
        if (type(identity) is not tuple or len(identity) != 2
                or any(type(part) is not int or not 0 <= part <= _MAX_NATIVE_ID for part in identity)
                or not identity[1]):
            raise StartupRefused('startup_selection')
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        raise StartupRefused('startup_selection') from error
    return {
        'intent': 'initialize' if selection.initialize else 'restart',
        'root': str(root),
        'identity': [str(part) for part in identity],
        'leaf': root.name if selection.initialize else None,
        'store': selection.store,
        'project': selection.project,
        'project_name': name,
        'bridge_port': port,
    }


def _clock_bounds(arguments):
    issued = _decimal(arguments.get('issued_monotonic_ns'), _MAX_NS, 'startup_clock')
    expires = _decimal(arguments.get('expires_monotonic_ns'), _MAX_NS, 'startup_clock')
    if issued + _WINDOW_NS != expires:
        raise StartupRefused('startup_clock')
    return issued, expires


def _require_window(arguments):
    issued, expires = _clock_bounds(arguments)
    _require_interval(issued, expires)
    return issued, expires


def _require_interval(issued, expires):
    now = _clock_ns()
    if type(now) is not int or not issued <= now < expires:
        raise StartupRefused('startup_expired')


def startup_act(selection: StartSelection) -> str:
    if selection.initialize:
        return (f'Initialize Store {selection.root} and start project '
                f'{selection.project_name} ({selection.project})')
    return f'Start Store {selection.root}, project {selection.project}'


def _verify(message, signature):
    try:
        operator_terminal._verify_signature(message, signature)
    except operator_terminal.OperatorCheckRefused as error:
        raise StartupRefused(error.reason) from error


@dataclass
class _StartupUses:
    birth: bool = False
    launch: bool = False
    offered: bool = False
    child: object = None
    lock: Lock = field(default_factory=Lock)


@dataclass(frozen=True)
class SignedStartup:
    selection: StartSelection
    owner: NativeIdentity
    value: bytes
    message: bytes
    signature: bytes
    _context: bytes = field(repr=False)
    _uses: _StartupUses = field(default_factory=_StartupUses, init=False, repr=False, compare=False)

    @property
    def arguments(self):
        # Return a fresh object so no callback can change the signed context.
        return json.loads(self._context)

    def _live_owner(self):
        if NativeIdentity.current() != self.owner:
            raise StartupRefused('startup_owner')

    def _live_selection(self):
        if _selection_identity(self.selection.root, self.selection.initialize) != self.selection.identity:
            raise StartupRefused('startup_selection')

    def consume_birth(self, consume: Callable[[], object]):
        with self._uses.lock:
            if not self.selection.initialize or self._uses.birth or self._uses.launch:
                raise StartupRefused('startup_used')
            self._live_owner()
            self._live_selection()
            self._require_unexpired()
            self._uses.birth = True
        return consume()

    def spawn(self, spawn: Callable[[], object]):
        with self._uses.lock:
            if self._uses.launch:
                raise StartupRefused('startup_used')
            self._live_owner()
            self._live_selection()
            self._require_unexpired()
            self._uses.launch = True
        child = spawn()
        with self._uses.lock:
            self._uses.child = child
        return child

    def _require_unexpired(self):
        _require_window(self.arguments)


@dataclass(frozen=True)
class VerifiedStartup:
    selection: StartSelection
    owner: NativeIdentity
    issued_monotonic_ns: int
    expires_monotonic_ns: int

    def recheck_before_arm(self):
        _require_live_parent(self.owner)
        _require_interval(self.issued_monotonic_ns, self.expires_monotonic_ns)


def authorize_owner_start(selection: StartSelection, owner: NativeIdentity, *,
                          prove: Callable[[bytes], bytes], show: Callable[[str], None]) -> SignedStartup:
    arguments = _selection_args(selection)
    if NativeIdentity.current() != owner:
        raise StartupRefused('startup_owner')
    if (_selection_identity(selection.root, selection.initialize) != selection.identity
            or selection.initialize and (selection.root.exists() or selection.root.is_symlink())):
        raise StartupRefused('startup_selection')
    issued = _clock_ns()
    if type(issued) is not int or not 0 <= issued <= _MAX_NS - _WINDOW_NS:
        raise StartupRefused('startup_clock')
    arguments.update(owner_pid=owner.pid, owner_birth=owner.birth, owner_boot=owner.boot,
                     issued_monotonic_ns=str(issued), expires_monotonic_ns=str(issued + _WINDOW_NS))
    value = secrets.token_bytes(32)
    context = jcs.jcs_encode(arguments)
    message = operator_terminal.build_message('engine_start', arguments, store_id=selection.store, value=value)
    show(startup_act(selection))
    signature = prove(message)
    _verify(message, signature)
    if NativeIdentity.current() != owner:
        raise StartupRefused('startup_owner')
    _require_window(arguments)
    return SignedStartup(selection, owner, value, message, signature, context)


@contextmanager
def startup_listener():
    # Use the native local temp base, independent of the selected Store.
    # A short path fits sockaddr_un's smaller Darwin limit.
    with tempfile.TemporaryDirectory(prefix='adt-start-', dir='/tmp') as directory:
        path = Path(directory) / 's'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(os.fspath(path))
            listener.listen(1)
            yield listener, path


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise StartupRefused('startup_timeout')
    return remaining


def _receive(channel, count, deadline):
    data = bytearray()
    while len(data) < count:
        channel.settimeout(_remaining(deadline))
        chunk = channel.recv(count - len(data))
        if not chunk:
            raise StartupRefused('startup_frame')
        data.extend(chunk)
    return bytes(data)


def _read_frame(channel, deadline):
    length = int.from_bytes(_receive(channel, 4, deadline), 'big')
    if not 0 < length <= _MAX_FRAME:
        raise StartupRefused('startup_frame')
    encoded = _receive(channel, length, deadline)
    data = json.loads(encoded)
    if type(data) is not dict or jcs.jcs_encode(data) != encoded:
        raise StartupRefused('startup_frame')
    return data


def _send_frame(channel, frame, deadline):
    body = jcs.jcs_encode(frame)
    if len(body) > _MAX_FRAME:
        raise StartupRefused('startup_frame')
    channel.settimeout(_remaining(deadline))
    channel.sendall(len(body).to_bytes(4, 'big') + body)


def _peer(channel):
    try:
        return lifecycle_paths._peer_pid(channel)
    except (OSError, ValueError) as error:
        raise StartupRefused('startup_peer') from error


def _require_child(channel, child):
    if child.poll() is not None or _peer(channel) != child.pid:
        raise StartupRefused('startup_peer')
    return NativeIdentity.read(child.pid)


def offer_child_start(listener, child, attempt: SignedStartup, *, deadline: float):
    """Bind and answer one verified child challenge. The caller owns the returned channel."""
    peer = None
    try:
        with attempt._uses.lock:
            if attempt._uses.offered:
                raise StartupRefused('startup_used')
            if child is None or attempt._uses.child is not child:
                raise StartupRefused('startup_peer')
            attempt._live_owner()
            # Claim the response slot before any blocking I/O. Any failed
            # exchange also requires a new operator attempt.
            attempt._uses.offered = True
        listener.settimeout(_remaining(deadline))
        peer, _ = listener.accept()
        native_child = _require_child(peer, child)
        listener.close()
        challenge = _read_frame(peer, deadline)
        nonce = challenge.get('nonce')
        if set(challenge) != {'nonce'} or type(nonce) is not str or not _HEX_VALUE.fullmatch(nonce):
            raise StartupRefused('startup_frame')
        if _require_child(peer, child) != native_child:
            raise StartupRefused('startup_peer')
        _send_frame(peer, {'nonce': nonce, 'arguments': attempt.arguments,
                           'value': attempt.value.hex(), 'signature': attempt.signature.hex()}, deadline)
        accepted, peer = peer, None
        return accepted
    except TimeoutError as error:
        raise StartupRefused('startup_timeout') from error
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, StartupRefused):
            raise
        raise StartupRefused('startup_frame') from error
    finally:
        listener.close()
        if peer is not None:
            peer.close()


def _signed_selection(arguments):
    try:
        root = Path(arguments['root'])
        encoded_identity = arguments['identity']
        if type(encoded_identity) is not list or len(encoded_identity) != 2:
            raise StartupRefused('startup_selection')
        identity = tuple(_decimal(part, _MAX_NATIVE_ID, 'startup_selection')
                         for part in encoded_identity)
        selection = StartSelection(root, identity, arguments['store'], arguments['project'],
                                   arguments['intent'] == 'initialize', arguments['project_name'],
                                   arguments['bridge_port'])
    except (KeyError, TypeError) as error:
        raise StartupRefused('startup_selection') from error
    expected = _selection_args(selection)
    if (set(arguments) != expected.keys() | {'owner_pid', 'owner_birth', 'owner_boot',
                                             'issued_monotonic_ns', 'expires_monotonic_ns'}
            or any(arguments[key] != value for key, value in expected.items())):
        raise StartupRefused('startup_selection')
    return selection


def _require_live_parent(owner):
    if (os.getppid() != owner.pid or NativeIdentity.read(owner.pid) != owner
            or os.getppid() != owner.pid):
        raise StartupRefused('startup_peer')


def _verify_child(channel, require_selection, *, deadline):
    try:
        owner_pid = _peer(channel)
        if owner_pid <= 1 or owner_pid != os.getppid():
            raise StartupRefused('startup_peer')
        owner = NativeIdentity.read(owner_pid)
        nonce = secrets.token_hex(32)
        _send_frame(channel, {'nonce': nonce}, deadline)
        proof = _read_frame(channel, deadline)
        if set(proof) != {'nonce', 'arguments', 'value', 'signature'} or proof['nonce'] != nonce:
            raise StartupRefused('startup_frame')
        arguments, value, signature = proof['arguments'], proof['value'], proof['signature']
        if (type(arguments) is not dict or type(value) is not str or not _HEX_VALUE.fullmatch(value)
                or type(signature) is not str or not re.fullmatch(r'(?:[0-9a-f]{2}){1,4096}', signature)):
            raise StartupRefused('startup_frame')
        selection = _signed_selection(arguments)
        if (type(arguments['owner_pid']) is not int
                or (arguments['owner_pid'], arguments['owner_birth'], arguments['owner_boot']) != owner):
            raise StartupRefused('startup_owner')
        message = operator_terminal.build_message('engine_start', arguments, store_id=selection.store,
                                                  value=bytes.fromhex(value))
        _verify(message, bytes.fromhex(signature))
        require_selection(selection)
        if _peer(channel) != owner_pid:
            raise StartupRefused('startup_peer')
        _require_live_parent(owner)
        _remaining(deadline)
        return VerifiedStartup(selection, owner, *_require_window(arguments))
    except TimeoutError as error:
        raise StartupRefused('startup_timeout') from error
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, StartupRefused):
            raise
        raise StartupRefused('startup_frame') from error


def verify_child_start(channel: socket.socket, selection: StartSelection, *,
                       deadline: float) -> VerifiedStartup:
    def require_selection(signed):
        if signed != selection or _selection_identity(signed.root, signed.initialize) != signed.identity:
            raise StartupRefused('startup_selection')

    return _verify_child(channel, require_selection, deadline=deadline)


def verify_service_start(path, guard, idle, bridge_port):
    """Correlate signed startup with the private receipt and the held original lock."""
    deadline = time.monotonic() + _IO_SECONDS

    def require_selection(selection):
        expected = guard._root_identity
        if selection.initialize:
            expected = _selection_identity(selection.root, True)
        guard._current(idle)
        if ((selection.root, selection.store, selection.project, selection.bridge_port)
                != (guard._path, idle.store, idle.project, bridge_port)
                or selection.identity != expected or idle.root != guard._root_identity
                or idle.lock != guard.lock_identity):
            raise StartupRefused('startup_selection')

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(_remaining(deadline))
            channel.connect(path)
            return _verify_child(channel, require_selection, deadline=deadline)
    except OSError as error:
        raise StartupRefused('startup_peer') from error
