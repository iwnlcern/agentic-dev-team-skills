"""Explicit foreground API serving for one project, with bounded request reads."""
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import errno
import io
import fcntl
import json
import os
from pathlib import Path
import select
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
from relay_engine.api_writer import StoreWriter, WriterUnavailable
from relay_engine.operator_queue import ingest, notifier, records, saga

REQUEST_BUDGET_UNBOUND = 16384
REQUEST_BUDGET_BOUND = 262144
MAX_CONNECTIONS = 64
QUEUE_INTERVAL = 1.0
BRIDGE_STOP_DRAIN = 0.25
QueueNotifier = tuple[str, Callable[[list[str]], None]]


@dataclass(frozen=True)
class _StopAttempt:
    origin: float

    @property
    def active(self):
        return self.origin + 1.5

    @property
    def work(self):
        return self.origin + 3.0

    @property
    def diagnostic(self):
        return self.origin + 3.5

    @property
    def hard(self):
        return self.origin + 4.0


@dataclass(frozen=True)
class _RetirementReceipt:
    attempt: _StopAttempt
    holder: tuple[int, int] | None


class _ForegroundEpoch:
    def __init__(self, fence_writer):
        self._initial = {}
        self._stop = {}
        self._terminal = {}
        self._finished = False
        self.retiring = False
        self.holder = None
        self._fence_writer = fence_writer
        self._stopped = threading.Event()
        self._fence_complete = False
        self._fencer = threading.Thread(target=self._begin_retirement, name='adt-store-fence', daemon=True)
        self._fencer.start()
        self.read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        self._wake_pipe = io.FileIO(write_fd, 'wb')
        self._observer = threading.Thread(target=self._observe, name='adt-stop-observer', daemon=True)
        self._observer.start()

    @property
    def attempt(self):
        return self._stop.get('attempt')

    def _wake(self):
        # FileIO is unbuffered. A full nonblocking pipe already carries a wake.
        self._wake_pipe.write(b'\x00')

    def publish_stop(self, observed_at):
        offered = _StopAttempt(observed_at)
        attempt = self._stop.setdefault('attempt', offered)
        self._initial.setdefault('winner', attempt)
        self._wake()
        return attempt

    def acknowledge_ready(self):
        return self._initial.setdefault('winner', 'ready') == 'ready' and self.attempt is None

    def admits(self):
        return self._initial.get('winner') == 'ready' and self.attempt is None

    def try_seal_clean(self, receipt):
        attempt = receipt.attempt
        if self.attempt is attempt and self.holder == receipt.holder and time.monotonic() < attempt.hard:
            return self._terminal.setdefault('status', 0) == 0
        return False

    def _observe(self):
        while not self._finished:
            attempt = self.attempt
            if attempt is not None and not self._stopped.is_set():
                self._stopped.set()
            remaining = None if attempt is None else max(0, attempt.hard - time.monotonic())
            readable, _, _ = select.select([self.read_fd], [], [], remaining)
            if readable:
                os.read(self.read_fd, 4096)
            if not self._finished and attempt is not None and time.monotonic() >= attempt.hard:
                self.seal_at_hard_cutoff(attempt)

    def _begin_retirement(self):
        self._stopped.wait()
        self._fence_complete = self._fence_writer(self.attempt)

    def join_fencer(self, attempt):
        self._fencer.join(max(0, attempt.work - time.monotonic()))
        return not self._fencer.is_alive() and self._fence_complete

    def seal_at_hard_cutoff(self, attempt):
        if self.attempt is attempt:
            os._exit(self._terminal.setdefault('status', 75))

    def finish(self):
        self._finished = True
        self._wake()
        self._observer.join()
        os.close(self.read_fd)
        self._wake_pipe.close()


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
                 queue_notifier: QueueNotifier | None = None,
                 stop_report: Callable[[str | None, float, tuple[int, int] | None, str], None] | None = None):
        self.store_root = str(Path(store_root).resolve())
        self.project_id = project_id
        self.clock = clock
        self.components = None if components is None else tuple(components)
        self.bridge_port = bridge_port
        self.queue_notifier = queue_notifier
        self._stop_report = stop_report
        self._instance_id = None
        self.writer = None
        self._bridge = None
        self._queue = None
        self._notify_thread = None
        self._notify_error = None
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
        self._foreground = None
        self._acceptor_running = False
        self._lock_identity = None

    def _install_signals(self):
        if threading.current_thread() is threading.main_thread() and not self._signals:
            for name in (signal.SIGTERM, signal.SIGINT):
                self._signals[name] = signal.getsignal(name)
                signal.signal(name, self._signal)

    def _restore_signals(self):
        if threading.current_thread() is threading.main_thread():
            for name, handler in self._signals.items():
                signal.signal(name, handler)
            self._signals.clear()

    def arm_foreground(self):
        if self._foreground is None:
            self._foreground = _ForegroundEpoch(self._fence_writer)
            self._install_signals()

    def _fence_writer(self, attempt):
        writer = self.writer
        return writer is None or writer.fence_for_stop(attempt)

    def _admits(self):
        return not self.is_stopping() and (self._foreground is None or self._foreground.admits())

    def _take_lock(self):
        try:
            path = Path(self.store_root) / 'api-service.lock'
            fd = os.open(path,
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = os.fstat(fd)
                identity = (held.st_dev, held.st_ino)
                if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1 or self._inode(path) != identity:
                    raise OSError('invalid service holder')
            except BaseException:
                os.close(fd)
                raise
        except OSError:
            raise ServiceStartRefused('store_locked') from None
        self._lock_fd = fd
        self._lock_identity = identity
        if self._foreground is not None:
            self._foreground.holder = identity

    def start(self) -> str | None:
        if self._listener is not None:
            return self.ready_path
        build_id = version.fingerprint(Path(__file__).resolve().parents[1])
        if not os.path.isdir(self.store_root):
            raise store_layout.LayoutFault('missing')
        if self.is_stopping():
            return None
        self._take_lock()
        try:
            if self.is_stopping():
                return None
            self._stopping.clear()
            self._signal_requested = False
            self._notify_error = None
            instance_id = self._instance_id = uuid.uuid4().hex
            writer = StoreWriter(self.store_root, runtime_instance_id=instance_id)
            if self._foreground is not None:
                holder = (self._lock_fd, Path(self.store_root) / 'api-service.lock', self._lock_identity)
                writer.bind_service_owner(holder, self._foreground)
            self.writer = writer
            self.writer.start()
            try:
                self.writer.open_project(self.project_id)
            except (store_layout.LayoutFault, OSError):
                raise ServiceStartRefused('ledger_unavailable') from None
            if self.is_stopping():
                return None
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
            def identity(conn):
                cursor = conn.execute('SELECT store_id, kind FROM catalogue_identity WHERE id=1')
                try:
                    return cursor.fetchone()
                finally:
                    cursor.close()

            store_id, kind = writer.read_catalogue(identity)
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
            if self._foreground is not None:
                writer.verify_service_owner()
            if self.is_stopping():
                return None
            self._runtime_files['ready.json'] = self._runtime_files['ready.pending']
            if self.bridge_port is None:
                os.replace(pending, self.ready_path)
            else:
                try:
                    os.link(pending, self.ready_path, follow_symlinks=False)
                except FileExistsError:
                    raise ServiceStartRefused('runtime_dir_unsafe') from None
                pending.unlink()
            self._runtime_files.pop('ready.pending')
            if self._foreground is not None and not self._foreground.acknowledge_ready():
                return None
            self._install_signals()
            if self._queue is not None and self.queue_notifier is not None and not self.is_stopping():
                thread = threading.Thread(target=self._notify_queue, name='adt-queue-notify')
                thread.start()
                self._notify_thread = thread
            return self.ready_path
        except WriterUnavailable as error:
            if self.is_stopping() and error.reason == 'stopped':
                return None
            raise
        except BaseException:
            if self._foreground is None:
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
        if not self._admits():
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
            drain_seconds=BRIDGE_STOP_DRAIN, admission=self._admits)
        return self._bridge.start()

    def _queue_pass(self):
        queue = self._queue
        try:
            ingest.question_ingest_pass(queue.port, (self.project_id,), self.clock())
            saga.reconcile_pass(queue.port, queue.runtime, self.clock, stopping=self.is_stopping)
        except records.JobUnavailable:
            queue.runtime.ready = False
            if not self.is_stopping():
                raise

    def _notify_queue(self):
        program, deliver = self.queue_notifier

        def send(argv):
            if self.is_stopping():
                raise notifier.DeliveryCancelled()
            deliver(argv)

        try:
            while not self.is_stopping():
                notifier.notify_pass(self._queue.port, send, self.clock(), notifier=program)
                self._stopping.wait(QUEUE_INTERVAL)
        except Exception as error:
            self._notify_error = error

    def is_stopping(self):
        return (self._signal_requested or self._stopping.is_set()
                or (self._foreground is not None and self._foreground.attempt is not None))

    def _signal(self, signum, frame):
        if self._foreground is not None:
            self._foreground.publish_stop(time.monotonic())
            return
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
        with self._condition:
            if not self._admits() or self._acceptor_running:
                return
            self._acceptor_running = True
        try:
            self._serve_loop()
        finally:
            with self._condition:
                self._acceptor_running = False
                self._condition.notify_all()

    def _serve_loop(self):
        next_queue_pass = 0.0
        while self._admits():
            if self._notify_error is not None:
                if isinstance(self._notify_error, records.JobUnavailable):
                    self._queue.runtime.ready = False
                raise self._notify_error
            if self._queue is not None and time.monotonic() >= next_queue_pass:
                self._queue_pass()
                next_queue_pass = time.monotonic() + QUEUE_INTERVAL
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except (OSError, AttributeError):
                if self.is_stopping():
                    break
                raise
            with self._condition:
                self._clients.add(client)
                if not self._admits() or len(self._clients) > MAX_CONNECTIONS:
                    client.close()
                    self._clients.remove(client)
                    continue
                thread = threading.Thread(target=self._connection, args=(client,))
                try:
                    thread.start()
                except BaseException:
                    client.close()
                    self._clients.remove(client)
                    raise
                self._threads.add(thread)

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
            while not self.is_stopping():
                request = self._read_request(client, state)
                if request is None or not self._admits():
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
        if self._foreground is not None:
            return self._stop_foreground()
        with self._stop_lock:
            self._stop_owned()

    def _stop_foreground(self):
        epoch = self._foreground
        attempt = epoch.publish_stop(time.monotonic())
        report = None
        with self._stop_lock:
            if self._foreground is not epoch:
                return 0
            if not epoch.retiring:
                epoch.retiring = True
                try:
                    receipt = self._stop_owned(attempt)
                except Exception as error:
                    report = error
                else:
                    if epoch.try_seal_clean(receipt):
                        self._restore_signals()
                        epoch.finish()
                        self._foreground = None
                        return 0
        if report is not None and self._stop_report is not None and time.monotonic() < attempt.diagnostic:
            try:
                self._stop_report(self._instance_id, attempt.origin, epoch.holder, str(report))
            except Exception:
                pass
        # The original hard observer consumes an incomplete disposition.
        threading.Event().wait()

    def _stop_owned(self, attempt=None):
        self._stopping.set()
        deadline = None if attempt is None else attempt.work
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._condition:
            if deadline is not None:
                self._condition.wait_for(lambda: not self._acceptor_running,
                                         max(0, deadline - time.monotonic()))
                if self._acceptor_running:
                    raise OSError('service acceptor retirement incomplete')
            clients = tuple(self._clients)
            threads = tuple(self._threads)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._bridge is not None:
            if not self._bridge.stop(deadline=deadline):
                raise OSError('operator transport retirement incomplete')
            self._bridge = None
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(None if deadline is None else max(0, deadline - time.monotonic()))
        if deadline is not None:
            with self._condition:
                if self._clients or self._threads:
                    raise OSError('service transport retirement incomplete')
        if self._notify_thread is not None:
            self._notify_thread.join(None if deadline is None else max(0, deadline - time.monotonic()))
            if self._notify_thread.is_alive():
                raise OSError('notifier retirement incomplete')
            self._notify_thread = None
        if attempt is not None:
            epoch = self._foreground
            if not epoch.join_fencer(attempt):
                raise OSError('store fence incomplete')
            if self.writer is not None and not self.writer.settle_service_stop(attempt):
                raise OSError('store retirement incomplete')
        elif self.writer is not None:
            self.writer.close()
        if deadline is not None and time.monotonic() >= deadline:
            raise OSError('retirement deadline reached')
        self.writer = None
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
                    if deadline is not None or error.errno != errno.ENOTEMPTY:
                        raise
            elif deadline is not None:
                raise OSError('runtime directory changed')
        self._runtime_inode = self.runtime_dir = None
        self._runtime_files.clear()
        identity = None
        if self._lock_fd is not None:
            holder = os.fstat(self._lock_fd)
            if ((holder.st_dev, holder.st_ino) != self._lock_identity
                    or self._inode(Path(self.store_root) / 'api-service.lock') != self._lock_identity):
                raise OSError('service holder changed')
            os.close(self._lock_fd)
            identity = self._lock_identity
            self._lock_fd = None
        if attempt is None:
            self._restore_signals()
            return None
        return _RetirementReceipt(attempt, identity)
