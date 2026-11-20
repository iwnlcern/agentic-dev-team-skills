"""Explicit foreground API serving for one project, with bounded request reads."""
from collections.abc import Callable, Sequence
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import uuid

from relay_engine import api_compose, api_platform, api_protocol, api_registry, api_schema
from relay_engine import api_wire, errors, lifecycle_paths, store_layout, version
from relay_engine.api_compose import ApiComponent
from relay_engine.api_submit import ServiceContext
from relay_engine.api_writer import StoreWriter
from relay_engine.operator_queue import ingest, notifier, records, saga

REQUEST_BUDGET_UNBOUND = 16384
REQUEST_BUDGET_BOUND = 262144
MAX_CONNECTIONS = 64
QUEUE_INTERVAL = 1.0
BRIDGE_STOP_DRAIN = 0.25


class ServiceStartRefused(Exception):
    def __init__(self, reason):
        if reason not in {'store_locked', 'ledger_unavailable', 'port_missing',
                          'port_unrouted', 'runtime_dir_unsafe', 'bridge_port_unavailable'}:
            raise ValueError('unknown service start refusal')
        self.reason = reason
        super().__init__(reason)


class _DiscoveryConnection:
    def __init__(self, path):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.settimeout(BRIDGE_STOP_DRAIN)
            self._socket.connect(path)
        except BaseException:
            self.close()
            raise

    def exchange(self, frame):
        self._socket.sendall(frame)
        prefix = ApiService._exact(self._socket, 4)
        if prefix is None:
            raise ConnectionError('discovery closed')
        body = ApiService._exact(self._socket, int.from_bytes(prefix, 'big'))
        if body is None:
            raise ConnectionError('discovery closed')
        return prefix + body

    def close(self):
        self._socket.close()


class ApiService:
    def __init__(self, store_root: str, project_id: str, *, clock: Callable[[], int],
                 components: Sequence[ApiComponent] | None = None,
                 bridge_port: int | None = None,
                 queue_notifier: tuple[str, Callable[[list[str]], None]] | None = None):
        self.store_root = str(Path(store_root).resolve())
        self.project_id = project_id
        self.clock = clock
        self.components = None if components is None else tuple(components)
        self.bridge_port = bridge_port
        self.queue_notifier = queue_notifier
        self.writer = None
        self._bridge = None
        self._queue = None
        self._runtime_inode = None
        self._runtime_files = {}
        self.ready_path = None
        self.socket_path = None
        self.runtime_dir = None
        self._listener = None
        self._lock_fd = None
        self._stopping = threading.Event()
        self._signal_requested = False
        self._condition = threading.Condition()
        self._clients = set()
        self._threads = set()
        self._signals = {}
        self._stop_lock = threading.Lock()

    def _take_lock(self):
        try:
            fd = os.open(Path(self.store_root) / 'api-service.lock',
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
        except OSError:
            raise ServiceStartRefused('store_locked') from None
        self._lock_fd = fd

    def start(self) -> str:
        if self._listener is not None:
            return self.ready_path
        build_id = version.fingerprint(Path(__file__).resolve().parents[1])
        if not os.path.isdir(self.store_root):
            raise store_layout.LayoutFault('missing')
        self._take_lock()
        try:
            self._stopping.clear()
            self._signal_requested = False
            instance_id = uuid.uuid4().hex
            self.writer = StoreWriter(self.store_root, runtime_instance_id=instance_id)
            self.writer.start()
            try:
                self.writer.open_project(self.project_id)
            except (store_layout.LayoutFault, OSError):
                raise ServiceStartRefused('ledger_unavailable') from None
            if self.components is None:
                build = api_platform.build_platform()
            else:
                composition = api_compose.compose_components(self.components)
                build = api_platform.PlatformBuild(api_registry.compile_registry(
                    composition.definitions, composition.handlers, (errors.api_error_catalog(),)), composition)
            self.registry = build.registry
            context = ServiceContext(self.writer, self.clock, self.store_root, self.project_id)
            owner_ports = {owner: factory(context)
                           for owner, factory in build.composition.port_factories.items()}
            self._queue = owner_ports.get('console') if self.bridge_port is not None else None
            operations = set(self.registry.operations) - {'protocol.connect', 'protocol.schema'}
            self.operation_ports = {}
            for operation in operations:
                owner = build.composition.owners.get(operation)
                if owner is None:
                    raise ServiceStartRefused('port_unrouted')
                port = owner_ports.get(owner)
                if port is None:
                    raise ServiceStartRefused('port_missing')
                self.operation_ports[operation] = port
            if set(self.operation_ports) != operations:
                raise ServiceStartRefused('port_unrouted')
            store_id = self.writer.store_id()
            catalogue = store_layout.open_catalogue_readonly(self.store_root)
            try:
                kind = catalogue.execute('SELECT kind FROM catalogue_identity WHERE id=1').fetchone()[0]
            finally:
                catalogue.close()
            artifact = api_schema.export_schema(self.registry, '3.0')
            self.runtime = api_protocol.RuntimeContext(store_id, kind, instance_id, '3.0.0',
                                                       build_id, True, {'3.0': artifact})
            self._create_runtime(store_id)
            self.socket_path = str(self.runtime_dir / 'api.sock')
            self.ready_path = str(self.runtime_dir / 'ready.json')
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(self.socket_path)
            self._runtime_files['api.sock'] = self._inode(Path(self.socket_path))
            self._listener.listen(MAX_CONNECTIONS)
            self._listener.settimeout(0.1)
            ready = {'format': 'adt-ready-v1', 'socket': self.socket_path, 'store_id': store_id,
                     'build_id': build_id, 'schema_digest': artifact.digest, 'pid': os.getpid()}
            if self.bridge_port is not None:
                ready['bridge_origin'] = self._start_bridge(instance_id, store_id, artifact)
            pending = self.runtime_dir / 'ready.pending'
            with open(pending, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                status = os.fstat(stream.fileno())
                self._runtime_files['ready.pending'] = (status.st_dev, status.st_ino)
                json.dump(ready, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            if self.bridge_port is None:
                os.replace(pending, self.ready_path)
            else:
                try:
                    os.link(pending, self.ready_path, follow_symlinks=False)
                except FileExistsError:
                    raise ServiceStartRefused('runtime_dir_unsafe') from None
                self._runtime_files['ready.json'] = self._runtime_files['ready.pending']
                pending.unlink()
            self._runtime_files['ready.json'] = self._runtime_files.pop('ready.pending')
            if threading.current_thread() is threading.main_thread():
                for name in (signal.SIGTERM, signal.SIGINT):
                    self._signals[name] = signal.getsignal(name)
                    signal.signal(name, self._signal)
            return self.ready_path
        except BaseException:
            self.stop()
            raise

    @staticmethod
    def _inode(path):
        try:
            status = path.lstat()
        except FileNotFoundError:
            return None
        return status.st_dev, status.st_ino

    def _create_runtime(self, store_id):
        try:
            if self.bridge_port is None:
                self.runtime_dir = Path(tempfile.mkdtemp(prefix='adt-' + store_id[:8] + '-'))
            else:
                self.runtime_dir = lifecycle_paths.create_runtime_dir(
                    store_id, platform=sys.platform, environ=os.environ, home=os.path.expanduser('~'))
            status = self.runtime_dir.lstat()
            if (not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid()
                    or stat.S_IMODE(status.st_mode) != 0o700):
                raise ValueError('unsafe_directory')
            self._runtime_inode = (status.st_dev, status.st_ino)
        except (OSError, ValueError, lifecycle_paths.HeldMetadataUnavailable):
            raise ServiceStartRefused('runtime_dir_unsafe') from None

    def _dispatch(self, state, request, *, principal=None):
        return api_protocol.dispatch_api(state, request, self.runtime,
                                         self.registry, self.operation_ports, principal=principal)

    def _page_dispatch(self, state, request, *, principal=None):
        if self._is_stopping():
            raise ConnectionError('bridge is stopping')
        return self._dispatch(state, request, principal=principal)

    def _start_bridge(self, instance_id, store_id, artifact):
        from relay_engine import api_operator_bridge

        self._bridge = api_operator_bridge._new_operator_bridge(
            port=self.bridge_port, run_id=instance_id, store_id=store_id, clock_ms=self.clock,
            dispatch=self._page_dispatch, supervisor_post=None,
            open_backend=lambda: _DiscoveryConnection(self.socket_path), runtime_dir=self.runtime_dir,
            page_registry=self.registry, page_state=lambda: api_protocol.ConnectionState(
                'connected', None, artifact.protocol, artifact.digest),
            drain_seconds=BRIDGE_STOP_DRAIN)
        return self._bridge.start()

    def _queue_pass(self):
        queue = self._queue
        try:
            ingest.question_ingest_pass(queue.port, (self.project_id,), self.clock())
            saga.reconcile_pass(queue.port, queue.runtime, self.clock, stopping=self._is_stopping)
            if self.queue_notifier is not None and not self._is_stopping():
                program, deliver = self.queue_notifier
                notifier.notify_pass(queue.port, deliver, self.clock(), notifier=program)
        except records.JobUnavailable:
            queue.runtime.ready = False
            if not self._is_stopping():
                raise

    def _is_stopping(self):
        return self._signal_requested or self._stopping.is_set()

    def _signal(self, signum, frame):
        if self.bridge_port is not None:
            self._signal_requested = True
            return
        if not self._stop_lock.acquire(blocking=False):
            return
        try:
            self._stop_owned()
        finally:
            self._stop_lock.release()

    def serve_forever(self) -> None:
        next_queue_pass = 0.0
        while not self._is_stopping():
            if self._queue is not None and time.monotonic() >= next_queue_pass:
                self._queue_pass()
                next_queue_pass = time.monotonic() + QUEUE_INTERVAL
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except (OSError, AttributeError):
                if self._is_stopping():
                    break
                raise
            with self._condition:
                if self._is_stopping() or len(self._clients) >= MAX_CONNECTIONS:
                    client.close()
                    continue
                self._clients.add(client)
                thread = threading.Thread(target=self._connection, args=(client,))
                self._threads.add(thread)
                thread.start()

    @staticmethod
    def _exact(client, size):
        data = bytearray()
        while len(data) < size:
            part = client.recv(size - len(data))
            if not part:
                return None
            data.extend(part)
        return bytes(data)

    def _read_request(self, client, state):
        prefix = self._exact(client, 4)
        if prefix is None:
            return None
        size = int.from_bytes(prefix, 'big')
        budget = REQUEST_BUDGET_BOUND if state.binding is not None else REQUEST_BUDGET_UNBOUND
        if size == 0 or size > budget:
            return api_wire.ProtocolRefusal(None, 'malformed_frame', True)
        body = self._exact(client, size)
        return None if body is None else api_wire.decode_api_frame(body, self.registry)

    def _connection(self, client, state=None):
        state = api_protocol.initial_connection_state() if state is None else state
        try:
            while not self._is_stopping():
                request = self._read_request(client, state)
                if request is None:
                    break
                answer = self._dispatch(state, request)
                if answer.response is not None:
                    operation = request.op if isinstance(request, api_wire.ApiRequest) else None
                    context = None
                    if (operation == 'protocol.schema' and 'result' in answer.response):
                        context = api_registry.DiscoveryValidationContext('schema_reply',
                            answer.state.negotiated_protocol, answer.state.negotiated_schema_digest)
                    client.sendall(api_wire.encode_api_response(answer.response, self.registry, operation, context))
                state = answer.state
                if answer.close:
                    break
        except OSError:
            pass
        finally:
            client.close()
            with self._condition:
                self._clients.discard(client)
                self._threads.discard(threading.current_thread())
                self._condition.notify_all()

    def stop(self) -> None:
        with self._stop_lock:
            self._stop_owned()

    def _stop_owned(self) -> None:
        self._stopping.set()
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._condition:
            clients = tuple(self._clients)
            threads = tuple(self._threads)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join()
        if self.writer is not None:
            writer, self.writer = self.writer, None
            writer.close()
        if self.runtime_dir is not None and self._runtime_inode is not None:
            if self._inode(self.runtime_dir) == self._runtime_inode:
                for name in ('ready.json', 'api.sock', 'ready.pending'):
                    inode = self._runtime_files.get(name)
                    path = self.runtime_dir / name
                    if inode is not None and self._inode(path) == inode:
                        path.unlink(missing_ok=True)
                try:
                    self.runtime_dir.rmdir()
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY:
                        raise
        self._runtime_inode = self.runtime_dir = None
        self._runtime_files.clear()
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._signals and threading.current_thread() is threading.main_thread():
            for name, handler in self._signals.items():
                signal.signal(name, handler)
            self._signals.clear()
