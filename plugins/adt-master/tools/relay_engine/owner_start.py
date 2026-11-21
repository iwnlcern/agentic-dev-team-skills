"""Cold operator proof for one selected Store and one native direct child."""
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import tempfile
from threading import Lock
import time
from typing import NamedTuple
from uuid import uuid4

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
        except (ValueError, OSError, IndexError) as error:
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

    def consume_production_birth(self, canonical_root: Path, store: str, project: str,
                                 parent_identity: tuple[int, int]) -> None:
        selection = self.selection
        if (type(parent_identity) is not tuple or len(parent_identity) != 2
                or any(type(part) is not int for part in parent_identity)
                or not isinstance(canonical_root, Path) or canonical_root.name != selection.root.name
                or (canonical_root, store, project, parent_identity)
                != (selection.root, selection.store, selection.project, selection.identity)):
            raise store_layout.LayoutFault('birth-unproved')
        try:
            self.consume_birth(lambda: None)
        except StartupRefused as error:
            if error.reason == 'startup_expired':
                raise
            raise store_layout.LayoutFault('birth-unproved') from error

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


def _startup_readable(channel, deadline, check):
    while True:
        if check is not None:
            check()
        remaining = _remaining(deadline)
        if select.select([channel], [], [], min(remaining, 0.05) if check else remaining)[0]:
            return


def _receive(channel, count, deadline, check=None):
    data = bytearray()
    while len(data) < count:
        if check is not None:
            _startup_readable(channel, deadline, check)
        channel.settimeout(_remaining(deadline))
        chunk = channel.recv(count - len(data))
        if not chunk:
            raise StartupRefused('startup_frame')
        data.extend(chunk)
    return bytes(data)


def _read_frame(channel, deadline, check=None):
    length = int.from_bytes(_receive(channel, 4, deadline, check), 'big')
    if not 0 < length <= _MAX_FRAME:
        raise StartupRefused('startup_frame')
    encoded = _receive(channel, length, deadline, check)
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


def offer_child_start(listener, child, attempt: SignedStartup, *, deadline: float, check=None):
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
        _startup_readable(listener, deadline, check)
        listener.settimeout(_remaining(deadline))
        peer, _ = listener.accept()
        native_child = _require_child(peer, child)
        listener.close()
        challenge = _read_frame(peer, deadline, check)
        nonce = challenge.get('nonce')
        if set(challenge) != {'nonce'} or type(nonce) is not str or not _HEX_VALUE.fullmatch(nonce):
            raise StartupRefused('startup_frame')
        if _require_child(peer, child) != native_child:
            raise StartupRefused('startup_peer')
        if check is not None:
            check()
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

    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        channel.settimeout(_remaining(deadline))
        channel.connect(path)
        verified = _verify_child(channel, require_selection, deadline=deadline)
        return verified, channel, deadline
    except BaseException as error:
        channel.close()
        if isinstance(error, OSError):
            raise StartupRefused('startup_peer') from error
        raise


@dataclass(frozen=True)
class ChildEpochWitness:
    root: tuple[int, int]
    lock: tuple[int, int]
    store: str
    project: str
    owner: store_layout._IncumbentOwner


def _epoch_frame(witness):
    return {'root': [str(part) for part in witness.root],
            'lock': [str(part) for part in witness.lock],
            'store': witness.store, 'project': witness.project, 'owner': asdict(witness.owner)}


def _epoch_ack(frame):
    return {'ack_epoch': sha256(jcs.jcs_encode(frame)).hexdigest()}


def witness_child_epoch(channel: socket.socket, idle: store_layout.IncumbentRecord,
                        owner: store_layout._IncumbentOwner, *, deadline: float) -> None:
    frame = _epoch_frame(ChildEpochWitness(idle.root, idle.lock, idle.store, idle.project, owner))
    try:
        _send_frame(channel, frame, deadline)
        if _read_frame(channel, deadline) != _epoch_ack(frame):
            raise StartupRefused('startup_witness')
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise StartupRefused('startup_witness') from error


def _epoch_identity(data):
    if type(data) is not list or len(data) != 2:
        raise StartupRefused('startup_witness')
    dev, ino = (_decimal(part, _MAX_NATIVE_ID, 'startup_witness') for part in data)
    if not ino:
        raise StartupRefused('startup_witness')
    return dev, ino


def read_child_epoch(channel: socket.socket, child, *, deadline: float,
                     idle: store_layout.IncumbentRecord | None = None, check=None) -> ChildEpochWitness:
    try:
        native = _require_child(channel, child)
        frame = _read_frame(channel, deadline, check)
        if set(frame) != {'root', 'lock', 'store', 'project', 'owner'}:
            raise StartupRefused('startup_witness')
        owner = store_layout._IncumbentOwner(**frame['owner'])
        witness = ChildEpochWitness(_epoch_identity(frame['root']), _epoch_identity(frame['lock']),
                                    store_layout._canonical_id(frame['store']),
                                    store_layout._canonical_id(frame['project']), owner)
        store_layout._canonical_id(owner.epoch)
        if (type(owner.pid) is not int or (owner.pid, owner.birth, owner.boot) != native
                or owner.notifier not in ('off', 'configured')
                or not store_layout._bounded_text(owner.runtime) or not Path(owner.runtime).is_absolute()):
            raise StartupRefused('startup_witness')
        if idle is not None and (witness.root, witness.lock, witness.store, witness.project) != (
                idle.root, idle.lock, idle.store, idle.project):
            raise StartupRefused('startup_witness')
        if _require_child(channel, child) != native:
            raise StartupRefused('startup_witness')
        if check is not None:
            check()
        _send_frame(channel, _epoch_ack(frame), deadline)
        return witness
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise StartupRefused('startup_witness') from error


@contextmanager
def _receipt_directory(root):
    home = root.parent
    try:
        with ExitStack() as stack:
            home_fd = os.open(home, store_layout._DIRECTORY)
            stack.callback(os.close, home_fd)
            ids = [(None, home, home_fd, store_layout._inode(os.fstat(home_fd)))]
            for name in ('.adt-owner', 'receipts'):
                parent = ids[-1][2]
                fd = os.open(name, store_layout._DIRECTORY, dir_fd=parent)
                stack.callback(os.close, fd)
                identity = store_layout._owned_inode(os.fstat(fd), directory=True)
                ids.append((parent, name, fd, identity))

            def check():
                for parent, name, fd, expected in ids:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    opened = os.fstat(fd)
                    if (info.st_uid != os.geteuid() or info.st_mode & (0o022 if parent is None else 0o077)
                            or not stat.S_ISDIR(info.st_mode)
                            or store_layout._inode(info) != expected or not os.path.samestat(opened, info)):
                        raise StartupRefused('receipt-directory-unproved')

            check()
            yield ids[-1][2], check
    except (OSError, store_layout.LayoutFault) as error:
        raise StartupRefused('receipt-directory-unproved') from error


def _receipt_name(root, project):
    value = os.fsencode(root) + b'\0' + store_layout._canonical_id(project).encode('ascii')
    return sha256(value).hexdigest() + '.receipt'


def _receipt_stat(directory, name):
    return store_layout._owned_inode(os.stat(name, dir_fd=directory, follow_symlinks=False))


def _read_receipt(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | store_layout._NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        identity = store_layout._owned_inode(os.fstat(fd))
        frame = stream.read(store_layout._IDLE_RECEIPT_LIMIT + 1)
        if (not 0 < len(frame) <= store_layout._IDLE_RECEIPT_LIMIT
                or _receipt_stat(directory, name) != identity):
            raise StartupRefused('receipt-unproved')
        return frame, identity


def _read_selected_receipt(root, project):
    with _receipt_directory(root) as (directory, check):
        try:
            name = _receipt_name(root, project)
            _require_receipt_settled(directory, name)
            frame, _ = _read_receipt(directory, name)
            _require_receipt_settled(directory, name)
        except FileNotFoundError as error:
            raise StartupRefused('receipt-missing') from error
        except (OSError, store_layout.LayoutFault) as error:
            raise StartupRefused('receipt-unproved') from error
        check()
        return frame


def _selected_frame(selection, frame):
    idle, proof = store_layout._decode_idle_receipt(frame)
    root_id = _selection_identity(selection.root, False)
    if ((idle.root, idle.store, idle.project) != (root_id, selection.store, selection.project)
            or (not selection.initialize and root_id != selection.identity)):
        raise StartupRefused('receipt-unproved')
    return idle, proof


def read_owner_receipt(selection: StartSelection) -> bytes:
    _selection_args(selection)
    frame = _read_selected_receipt(selection.root, selection.project)
    _selected_frame(selection, frame)
    return frame


def _stage_receipt(directory, frame):
    temporary = uuid4().hex + '.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | store_layout._NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            identity = store_layout._owned_inode(os.fstat(fd))
            stream.write(frame)
            stream.flush()
            os.fsync(fd)
        if _read_receipt(directory, temporary) != (frame, identity):
            raise StartupRefused('receipt-retention-unproved')
        return temporary, identity
    except BaseException:
        os.unlink(temporary, dir_fd=directory)
        raise


def _named_receipt(directory, name):
    try:
        return _receipt_stat(directory, name)
    except FileNotFoundError:
        return None


def _require_receipt_settled(directory, name):
    if _named_receipt(directory, name + '.pending') is not None:
        raise StartupRefused('receipt-retention-unproved')


def retain_owner_receipt(selection: StartSelection, frame: bytes, *, check_idle=None) -> None:
    _selection_args(selection)
    _selected_frame(selection, frame)
    name = _receipt_name(selection.root, selection.project)
    pending = name + '.pending'
    with _receipt_directory(selection.root) as (directory, check):
        candidate = backup = None
        try:
            _require_receipt_settled(directory, name)
            try:
                prior, prior_id = _read_receipt(directory, name)
            except FileNotFoundError:
                prior, prior_id = None, None
            candidate, identity = _stage_receipt(directory, frame)
            backup, _ = _stage_receipt(directory, frame if prior is None else prior)
            check()
            if _named_receipt(directory, name) != prior_id:
                raise StartupRefused('receipt-retention-unproved')
            os.replace(backup, pending, src_dir_fd=directory, dst_dir_fd=directory)
            backup = None
            # A failed restoration must leave the previous bytes in a named
            # artifact and prevent intake from trusting the new visible file.
            published = False
            try:
                os.fsync(directory)
                if check_idle is not None:
                    check_idle()
                os.replace(candidate, name, src_dir_fd=directory, dst_dir_fd=directory)
                candidate = None
                published = True
                os.fsync(directory)
                check()
                if _read_receipt(directory, name) != (frame, identity):
                    raise StartupRefused('receipt-retention-unproved')
                if check_idle is not None:
                    check_idle()
            except (OSError, ValueError, store_layout.LayoutFault):
                if published:
                    if _named_receipt(directory, name) != identity:
                        raise
                    if prior is not None:
                        os.replace(pending, name, src_dir_fd=directory, dst_dir_fd=directory)
                    else:
                        os.unlink(name, dir_fd=directory)
                        os.unlink(pending, dir_fd=directory)
                else:
                    os.unlink(pending, dir_fd=directory)
                os.fsync(directory)
                raise
            # The final receipt is already durable. A crash that resurrects
            # this marker fails closed on the next intake.
            os.unlink(pending, dir_fd=directory)
        except (OSError, ValueError, store_layout.LayoutFault) as error:
            raise StartupRefused('receipt-retention-unproved') from error
        finally:
            for temporary in (candidate, backup):
                if temporary is not None:
                    os.unlink(temporary, dir_fd=directory)


def _checked_receipt(selection, frame, *, retain=False):
    idle, proof = _selected_frame(selection, frame)
    with store_layout.IncumbentGuard.acquire(selection.root, expected_lock=idle.lock) as guard:
        guard.verify_idle(idle, proof)
        if retain:
            retain_owner_receipt(selection, frame, check_idle=lambda: guard._current(idle))
    return idle


def retain_child_retirement(selection, frame, witness):
    try:
        idle, proof = _selected_frame(selection, frame)
        evidence = json.loads(proof.evidence)
        if (proof.kind != 'retirement' or set(evidence) != {'arm', 'settlement_sha256', 'nonce'}
                or store_layout._canonical_frame(evidence) != proof.evidence
                or not _HEX_VALUE.fullmatch(evidence['settlement_sha256'])
                or not re.fullmatch(r'[0-9a-f]{32}', evidence['nonce'])):
            raise StartupRefused('retirement-unproved')
        arm = store_layout.IncumbentGuard._parse_record(store_layout._canonical_frame(evidence['arm']))
        if (arm.phase != 'armed' or proof.prior_epoch != witness.owner.epoch
                or (arm.root, arm.lock, arm.store, arm.project, arm.owner)
                != (witness.root, witness.lock, witness.store, witness.project, witness.owner)
                or (idle.root, idle.lock, idle.store, idle.project)
                != (witness.root, witness.lock, witness.store, witness.project)):
            raise StartupRefused('retirement-unproved')
        _checked_receipt(selection, frame, retain=True)
    except (OSError, ValueError, store_layout.LayoutFault, TypeError, KeyError, RecursionError) as error:
        raise StartupRefused('retirement-unproved') from error


def production_birth(selection, consume):
    """Use the separately reviewed Store provider when the joined package contains it."""
    if getattr(store_layout.IncumbentGuard, 'ACCEPTS_PRODUCTION_BIRTH', False) is not True:
        raise StartupRefused('birth-provider-unavailable')
    create = store_layout.IncumbentGuard._create_new
    guard, enrollment = create(selection.root, selection.store, selection.project, kind='production',
                               consume_production_birth=consume)
    with guard:
        idle, _ = guard.enroll_new(enrollment)
        native = NativeIdentity.current()
        owner = store_layout._IncumbentOwner(str(uuid4()), *native, str(selection.root.parent), 'off')
        armed = guard.arm(idle, owner)
        guard._materialize_birth(armed, project_name=selection.project_name, now_ms=time.time_ns() // 1_000_000)
        proof = guard._retirement_proof(armed, b'guarded first birth settled')
        idle = guard.retire(armed, proof)
        return store_layout._encode_idle_receipt(idle, proof)


class _OwnerStop:
    def __init__(self):
        self.signum = None
        self.forwarded_at = None

    def request(self, signum=signal.SIGTERM, _frame=None):
        if self.signum is None:
            self.signum = signum

    def forward(self, child):
        if self.signum is not None and self.forwarded_at is None and child.poll() is None:
            self.forwarded_at = time.monotonic()
            child.send_signal(self.signum)

    def require_running(self):
        if self.signum is not None:
            raise StartupRefused('startup-stopped')


@contextmanager
def _owner_stop():
    stop = _OwnerStop()
    with ExitStack() as signals:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(signum, stop.request)
            signals.callback(signal.signal, signum, previous)
        yield stop


def _collect_retirement(child, retirement, stop):
    status = None
    exited_at = None
    eof = False
    data = bytearray()
    while status is None or not eof:
        stop.forward(child)
        status = child.poll()
        now = time.monotonic()
        if status is not None and exited_at is None:
            exited_at = now
        if ((exited_at is not None and now >= exited_at + _IO_SECONDS)
                or (stop.forwarded_at is not None and now >= stop.forwarded_at + 6)):
            raise StartupRefused('retirement-incomplete')
        if eof:
            time.sleep(0.05)
        elif select.select([retirement], [], [], 0.05)[0]:
            part = os.read(retirement, store_layout._IDLE_RECEIPT_LIMIT + 1 - len(data))
            data.extend(part)
            eof = not part
            if len(data) > store_layout._IDLE_RECEIPT_LIMIT:
                raise StartupRefused('retirement-incomplete')
    if status != 0 or not data:
        raise StartupRefused('retirement-incomplete')
    return bytes(data)


def _reap_failed_start(child, stop):
    stop.request()
    stop.forward(child)
    deadline = time.monotonic() + 5
    while child.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            child.kill()
            child.wait()
            return
        time.sleep(min(0.05, remaining))


def _run_child(attempt, frame, idle, launch, stop):
    from relay_engine.api_service import _write_private_frame

    selection = attempt.selection
    command = ['engine', 'serve', '--store-root', str(selection.root), '--project-id', selection.project]
    if selection.bridge_port is not None:
        command += ['--bridge-port', str(selection.bridge_port)]
    with startup_listener() as (listener, path), ExitStack() as pipes:
        idle_read, idle_write = os.pipe()
        idle_input = pipes.enter_context(os.fdopen(idle_read, 'rb', buffering=0))
        idle_output = pipes.enter_context(os.fdopen(idle_write, 'wb', buffering=0))
        retire_read, retire_write = os.pipe()
        retirement = pipes.enter_context(os.fdopen(retire_read, 'rb', buffering=0))
        retire_output = pipes.enter_context(os.fdopen(retire_write, 'wb', buffering=0))
        environment = dict(os.environ, ADT_PRIVATE_IDLE_FD=str(idle_read),
                           ADT_PRIVATE_RETIREMENT_FD=str(retire_write), ADT_PRIVATE_START_SOCKET=str(path))

        def spawn():
            stop.require_running()
            return launch(command, env=environment, pass_fds=(idle_read, retire_write),
                          start_new_session=True)

        child = attempt.spawn(spawn)

        def check_starting():
            stop.forward(child)
            stop.require_running()

        idle_input.close()
        retire_output.close()
        deadline = time.monotonic() + _IO_SECONDS
        try:
            check_starting()
            try:
                _write_private_frame(os.dup(idle_write), frame, deadline)
            finally:
                idle_output.close()
            with offer_child_start(listener, child, attempt, deadline=deadline, check=check_starting) as peer:
                witness = read_child_epoch(peer, child, deadline=deadline, idle=idle, check=check_starting)
            return _collect_retirement(child, retirement.fileno(), stop), witness
        except (OSError, ValueError, store_layout.LayoutFault) as error:
            if child.poll() is None:
                _reap_failed_start(child, stop)
            raise StartupRefused('retirement-incomplete') from error


def run_owner(root, project, *, initialize, project_name, bridge_port, authorize,
              launch, birth_provider=production_birth, show):
    root = Path(root)
    if root != _production_root():
        raise StartupRefused('startup_selection')
    if initialize != (project_name is not None):
        raise StartupRefused('startup_selection')
    store_layout._canonical_id(project)
    with _owner_stop() as stop:
        if initialize:
            with _receipt_directory(root):
                pass
            store = str(uuid4())
        else:
            frame = _read_selected_receipt(root, project)
            store = store_layout._decode_idle_receipt(frame)[0].store
        selection = StartSelection.checked(root, store, project, initialize=initialize,
                                           project_name=project_name, bridge_port=bridge_port)
        if not initialize:
            idle = _checked_receipt(selection, frame)
        stop.require_running()
        attempt = authorize(selection)
        if attempt.selection != selection:
            raise StartupRefused('startup_selection')
        stop.require_running()
        if initialize:
            def consume(canonical_root, store, project, parent_identity):
                stop.require_running()
                attempt.consume_production_birth(canonical_root, store, project, parent_identity)

            frame = birth_provider(selection, consume)
            idle = _checked_receipt(selection, frame, retain=True)
        stop.require_running()
        frame, witness = _run_child(attempt, frame, idle, launch, stop)
        retain_child_retirement(selection, frame, witness)
        show('The Store stopped cleanly and its verified restart receipt was retained.')
        return 0
