"""Owned IPv4 loopback listener for the operator bridge."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import errno
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import sys
import time
from threading import Condition, Event, Lock, Thread
from typing import TYPE_CHECKING, Protocol

from relay_engine import api_bridge, jcs
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.api_protocol import ConnectionState, ResponseAndConnectionState
from relay_engine.api_registry import FrozenApiRegistry, ValidationFailure, validate_type
from relay_engine.api_wire import ApiRequest, ProtocolRefusal, StrictJsonError, strict_json_loads

if TYPE_CHECKING:
    from relay_engine.supervision import QueueEntry


BRIDGE_DRAIN_S = 15
_HEAD_BYTES = 16384
_BODY_BYTES = 16384
_REQUEST_MS = 10000
_CONNECTIONS = 16
_JSON_CONTENT_TYPE = 'application/json; charset=utf-8'
_REFUSED = b'{"error":"refused"}'
_PAGE_DIRECTORY = Path(__file__).with_name('console_page')
_PAGE_ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/page.css': ('page.css', 'text/css; charset=utf-8'),
    '/page.js': ('page.js', 'text/javascript; charset=utf-8'),
}
_DISCOVERY = frozenset({'/api/connect', '/api/schema'})
_PAGE_OPERATION_PREFIX = '/api/op/'
_PAGE_OPERATIONS = frozenset({'queue.answer', 'queue.dismiss', 'queue.open', 'queue.dismiss-all'})
_PAGE_READ_PREFIX = '/api/read/'
_PAGE_READS = frozenset({'queue.list', 'queue.get'})
PAGE_STATUS = {
    'wire': 400,
    'auth': 401,
    'version': 409,
    'conflict': 409,
    'internal': 500,
    'unavailable': 503,
    'admission': 422,
}
_RESPONSE_HEADERS = (
    ('Cache-Control', 'no-store'),
    ('Content-Security-Policy',
     "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
     "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ('Referrer-Policy', 'no-referrer'),
    ('X-Frame-Options', 'DENY'),
    ('X-Content-Type-Options', 'nosniff'),
    ('Connection', 'close'),
)


_LAUNCH_ROTATE_MS = 60000
_LAUNCH_LIFETIME_MS = 90000
_LAUNCH_PAGE = 'open.html'
_PAGE_PREFIX = (b'<!doctype html><meta name="referrer" content="no-referrer">'
                b'<meta http-equiv="refresh" content="0;url=')
_PAGE_SUFFIX = b'">'
_PAGE_URL = re.compile(rb'http://127\.0\.0\.1:([1-9][0-9]{0,4})/#launch=[A-Za-z0-9_-]{43}')
_SECRET = re.compile(r'[A-Za-z0-9_-]{43}')


def page_answer(response: Mapping[str, object]) -> tuple[int, bytes]:
    status = 200 if 'result' in response else PAGE_STATUS.get(response['error']['class'], 500)
    return status, jcs.jcs_encode(response)


def page_query(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    if not raw.isascii() or '%' in raw or '+' in raw:
        raise ValueError('invalid_page_query')
    args = {}
    for pair in raw.split('&'):
        name, separator, value = pair.partition('=')
        if not separator or not name or '=' in value or name in args:
            raise ValueError('invalid_page_query')
        args[name] = value
    return args


def _queue_read_args(request_type, args):
    if request_type == 'QueueListArgs':
        if args.keys() - {'scope', 'state', 'kind', 'page_size', 'continuation'}:
            raise ValueError('invalid_queue_query')
        state = args.pop('state', None)
        kind = args.pop('kind', None)
        return {'state_filter': None if state is None else [state],
                'kind_filter': None if kind is None else [kind],
                'page_size': '100', 'continuation': None, **args}
    if request_type == 'QueueGetArgs':
        if args.keys() - {'item_id', 'history_page_size', 'history_continuation'}:
            raise ValueError('invalid_queue_query')
        return {'history_page_size': '100', 'history_continuation': None, **args}
    return args


def _new_secret():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode('ascii')


def _private_launch_file(status):
    return (stat.S_ISREG(status.st_mode) and status.st_uid == os.getuid()
            and not status.st_mode & 0o077)


def _read_launch_url(page):
    if not page.startswith(_PAGE_PREFIX) or not page.endswith(_PAGE_SUFFIX):
        raise ValueError('invalid_launch_page')
    address = page[len(_PAGE_PREFIX):-len(_PAGE_SUFFIX)]
    match = _PAGE_URL.fullmatch(address)
    if match is None or int(match[1]) > 65535:
        raise ValueError('invalid_launch_page')
    return address.decode('ascii')


@dataclass(frozen=True, slots=True)
class _LaunchSecret:
    value: str = field(repr=False)
    born_ms: int


class _LaunchPage:
    """Serialize rotation separately from the state used to fence retirement."""

    def __init__(self, directory, origin, clock_ms):
        self._origin = origin
        self._clock_ms = clock_ms
        self._lock = Lock()
        self._state = Lock()
        self._fenced = Event()
        self._secrets: list[_LaunchSecret] = []
        self._page_secret = None
        self._published = None
        self._pending_name = None
        self._pending_inode = None
        self._discard_name = None
        self._refresh_at = 0
        self._dir = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            status = os.fstat(self._dir)
            if status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) != 0o700:
                raise ValueError('unsafe_launch_directory')
            self._rotate(self._dir, clock_ms())
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _page_status(directory):
        try:
            return os.stat(_LAUNCH_PAGE, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _unlink(directory, name):
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass

    def _discard_stale(self, directory):
        name = '.open-discard-' + secrets.token_hex(16)
        with self._state:
            if self._dir is None or self._published is None:
                return
            if self._page_secret in self._secrets:
                return
            published = self._published
            self._discard_name = name
        try:
            status = self._page_status(directory)
            if status is not None and (status.st_dev, status.st_ino) == published:
                # Stop can fence this single rename by creating a directory at
                # its unique destination. The worker never unlinks the public name.
                os.rename(_LAUNCH_PAGE, name, src_dir_fd=directory, dst_dir_fd=directory)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            with self._state:
                if self._dir is not None:
                    raise
        finally:
            with self._state:
                retired = self._dir is None
                self._discard_name = None
            if retired:
                self._fenced.wait()
            try:
                self._unlink(directory, name)
            except IsADirectoryError:
                os.rmdir(name, dir_fd=directory)

    def _rotate(self, directory, now):
        try:
            return self._publish(directory, now)
        except (OSError, ValueError):
            with self._state:
                if self._page_secret not in self._secrets:
                    self._refresh_at = now
            self._discard_stale(directory)
            raise

    def _publish(self, directory, now):
        status = self._page_status(directory)
        if status is not None and not _private_launch_file(status):
            raise ValueError('unsafe_launch_page')
        secret = _LaunchSecret(_new_secret(), now)
        address = f'{self._origin}/#launch={secret.value}'.encode('ascii')
        temporary = '.open-' + secrets.token_hex(16)
        with self._state:
            if self._dir is None:
                return False
            self._pending_name = temporary
        created = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            created = True
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(_PAGE_PREFIX + address + _PAGE_SUFFIX)
                written = os.fstat(stream.fileno())
            candidate = written.st_dev, written.st_ino
            with self._state:
                if self._dir is None:
                    return False
                self._pending_inode = candidate
            try:
                os.replace(temporary, _LAUNCH_PAGE, src_dir_fd=directory, dst_dir_fd=directory)
            except FileNotFoundError:
                with self._state:
                    if self._dir is None:
                        return False
                raise
            created = False
            with self._state:
                if self._dir is None:
                    return False
                self._published = candidate
                self._page_secret = secret
                self._refresh_at = now + _LAUNCH_ROTATE_MS
                self._secrets = [secret, *self._secrets[:1]]
                return True
        finally:
            try:
                if created:
                    self._unlink(directory, temporary)
            finally:
                with self._state:
                    self._pending_name = None
                    self._pending_inode = None

    @contextmanager
    def _operation(self):
        with self._lock:
            with self._state:
                directory = None if self._dir is None else os.dup(self._dir)
            try:
                yield directory
            finally:
                if directory is not None:
                    os.close(directory)

    def _refresh(self, directory, now):
        with self._state:
            if self._dir is None:
                return
            self._secrets = [secret for secret in self._secrets
                             if now - secret.born_ms < _LAUNCH_LIFETIME_MS]
            due = self._page_secret not in self._secrets or now >= self._refresh_at
        if due:
            self._rotate(directory, now)

    def refresh_due(self):
        now = self._clock_ms()
        with self._state:
            return self._dir is not None and now >= self._refresh_at

    def refresh(self):
        with self._operation() as directory:
            if directory is not None:
                self._refresh(directory, self._clock_ms())

    def consume(self, value):
        if type(value) is not str or not _SECRET.fullmatch(value):
            return False
        with self._operation() as directory:
            if directory is None:
                return False
            now = self._clock_ms()
            self._refresh(directory, now)
            with self._state:
                for index, candidate in enumerate(self._secrets):
                    if compare_digest(candidate.value, value):
                        self._secrets.pop(index)
                        break
                else:
                    return False
            return self._rotate(directory, now)

    def close(self):
        with self._state:
            directory = self._dir
            if directory is None:
                return
            self._dir = None
            self._secrets.clear()
            self._page_secret = None
            source, candidate = self._pending_name, self._pending_inode
            published, discard = self._published, self._discard_name
        try:
            if source is not None:
                self._unlink(directory, source)
            if discard is not None:
                try:
                    os.mkdir(discard, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    # The one rename already ran. The worker owns that file.
                    pass
            status = self._page_status(directory)
            if status is not None and (status.st_dev, status.st_ino) in (published, candidate):
                self._unlink(directory, _LAUNCH_PAGE)
        finally:
            try:
                os.close(directory)
            finally:
                self._fenced.set()


class OperatorDispatch(Protocol):
    def __call__(self, state: ConnectionState, request: ApiRequest | ProtocolRefusal, *,
                 principal: OperatorPrincipal | None = None) -> ResponseAndConnectionState: ...


class OperatorBridge:
    def __init__(self, *, port: int, run_id: str, store_id: str, clock_ms: Callable[[], int],
                 dispatch: OperatorDispatch, supervisor_post: Callable[[QueueEntry], bool] | None):
        self._port = port
        self._run_id = run_id
        self._store_id = store_id
        self._clock_ms = clock_ms
        self._dispatch = dispatch
        self._page_registry: FrozenApiRegistry | None = None
        self._page_state: Callable[[], ConnectionState] | None = None
        self._supervisor_post = supervisor_post
        self._open_discovery_backend: Callable[[], api_bridge.BackendConnection] | None = None
        self._lifecycle = Lock()
        self._drain_seconds = None
        self._counts_lock = Lock()
        self._counts: Counter[str] = Counter()
        self._server: _Server | None = None
        self._thread: Thread | None = None
        self._launch_runtime_dir: os.PathLike[str] | str | None = None
        self._admission: Callable[[], bool] = lambda: True

    def start(self) -> str:
        with self._lifecycle:
            if self._open_discovery_backend is None:
                raise RuntimeError('operator bridge discovery backend is unbound')
            if self._server is not None:
                return self._server.context.advertised_origin
            try:
                server = _Server(self, self._open_discovery_backend)
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                from relay_engine.api_service import ServiceStartRefused
                raise ServiceStartRefused('bridge_port_unavailable') from None
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},
                            name='adt-operator-bridge', daemon=True)
            try:
                if self._launch_runtime_dir is not None:
                    server.launch_page = _LaunchPage(self._launch_runtime_dir,
                                                     server.context.advertised_origin, self._clock_ms)
                thread.start()
            except BaseException:
                try:
                    if server.launch_page is not None:
                        server.launch_page.close()
                finally:
                    server.server_close()
                raise
            self._server = server
            self._thread = thread
            return server.context.advertised_origin

    def stop(self, *, deadline=None) -> bool:
        with self._lifecycle:
            if self._server is None:
                return True
            server = self._server
            settled = server.stop(deadline)
            self._thread.join(None if deadline is None else max(0, deadline - time.monotonic()))
            if deadline is not None and (not settled or self._thread.is_alive()):
                return False
            self._server = None
            self._thread = None
            if server.launch_page is not None:
                server.launch_page.close()
            return True

    def consume_launch(self, value: str) -> bool:
        server = self._server
        return server is not None and server.launch_page is not None and server.launch_page.consume(value)

    def status(self) -> Mapping[str, int]:
        with self._counts_lock:
            return dict(self._counts)

    def _record(self, kind: str) -> None:
        with self._counts_lock:
            self._counts[kind] += 1


def _new_operator_bridge(*, port: int, run_id: str, store_id: str, clock_ms: Callable[[], int],
                         dispatch: OperatorDispatch, supervisor_post: Callable[[QueueEntry], bool] | None,
                         open_backend: Callable[[], api_bridge.BackendConnection],
                         runtime_dir: os.PathLike[str] | str | None = None,
                         page_registry: FrozenApiRegistry | None = None,
                         page_state: Callable[[], ConnectionState] | None = None,
                         drain_seconds: float | None = None,
                         admission: Callable[[], bool] | None = None) -> OperatorBridge:
    bridge = OperatorBridge(port=port, run_id=run_id, store_id=store_id, clock_ms=clock_ms,
                            dispatch=dispatch, supervisor_post=supervisor_post)
    bridge._open_discovery_backend = open_backend
    bridge._launch_runtime_dir = runtime_dir
    bridge._page_registry = page_registry
    bridge._page_state = page_state
    bridge._drain_seconds = drain_seconds
    if admission is not None:
        bridge._admission = admission
    return bridge


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    allow_reuse_port = False
    request_queue_size = _CONNECTIONS

    def __init__(self, bridge, open_backend):
        self.bridge = bridge
        self.launch_page: _LaunchPage | None = None
        run_prefix = bridge._run_id.replace('-', '')[:8].lower()
        self.cookie_name = ('adt_session_' + run_prefix
                            if re.fullmatch(r'[0-9a-f]{8}', run_prefix) else None)
        self.sessions: dict[str, str] = {}
        self._sessions_lock = Lock()
        self._sessions_retired = False
        self._refresh_thread: Thread | None = None
        self.connections = Condition()
        self.active: dict[socket.socket, int] = {}
        self._failed_closes = set()
        self.handling: set[socket.socket] = set()
        self.closing = False
        super().__init__(('127.0.0.1', bridge._port), _RequestHandler)
        host, port = self.server_address
        advertised = f'{host}:{port}'
        self.context = api_bridge.BridgeContext(advertised, f'http://{advertised}', open_backend)

    def create_session(self, launch):
        page = self.launch_page
        if self.cookie_name is None or page is None or not page.consume(launch):
            return None
        session_id = _new_secret()
        token = _new_secret()
        with self._sessions_lock:
            if self._sessions_retired or session_id in self.sessions:
                return None
            self.sessions[session_id] = token
            return session_id, token

    def session_token(self, cookie_headers):
        if self.cookie_name is None:
            return None
        ids = []
        for header in cookie_headers:
            for pair in header.split(';'):
                name, _, value = pair.strip().partition('=')
                if name == self.cookie_name:
                    ids.append(value)
        if len(ids) != 1 or not _SECRET.fullmatch(ids[0]):
            return None
        with self._sessions_lock:
            return self.sessions.get(ids[0])

    def service_actions(self):
        page = self.launch_page
        if page is None or not page.refresh_due():
            return
        with self.connections:
            if self.closing or (self._refresh_thread is not None and self._refresh_thread.is_alive()):
                return
            self._refresh_thread = Thread(target=self._refresh_launch, args=(page,),
                                          name='adt-launch-refresh', daemon=True)
            self._refresh_thread.start()

    def _refresh_launch(self, page):
        try:
            page.refresh()
        except (OSError, ValueError):
            self.bridge._record('launch')

    def process_request(self, request, client_address):
        with self.connections:
            accepted = not self.closing and self.bridge._admission() and len(self.active) < _CONNECTIONS
            if accepted:
                self.active[request] = self.bridge._clock_ms() + _REQUEST_MS
                self.connections.notify_all()
        if not accepted:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._retire(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._retire(request)

    def close_request(self, request):
        try:
            super().close_request(request)
        except OSError:
            with self.connections:
                self._failed_closes.add(request)
            raise

    def _retire(self, request):
        with self.connections:
            self.active.pop(request, None)
            self.handling.discard(request)
            self.connections.notify_all()

    def begin_handler(self, request):
        with self.connections:
            if self.closing or not self.bridge._admission():
                return False
            self.handling.add(request)
            return True

    def handle_error(self, request, client_address):
        self.bridge._record(type(sys.exception()).__name__)

    @staticmethod
    def _interrupt(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def stop(self, deadline=None):
        with self.connections:
            self.closing = True
            for request in self.active.keys() - self.handling:
                self._interrupt(request)
        self.shutdown()
        self.server_close()
        with self.connections:
            drain = self.bridge._drain_seconds
            drain = BRIDGE_DRAIN_S if drain is None else drain
            if deadline is not None:
                drain = max(0, min(drain, deadline - time.monotonic()))
            self.connections.wait_for(lambda: not self.handling, drain)
            for request in self.active:
                self._interrupt(request)
            if deadline is not None:
                self.connections.wait_for(lambda: not self.active, max(0, deadline - time.monotonic()))
                if self.active or self._failed_closes:
                    return False
        if deadline is not None and self._refresh_thread is not None:
            self._refresh_thread.join(max(0, deadline - time.monotonic()))
            if self._refresh_thread.is_alive():
                return False
        with self._sessions_lock:
            self._sessions_retired = True
            self.sessions.clear()
        return True


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = ''
    sys_version = ''

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.server.bridge._record('malformed')
        self._reply(403, _REFUSED)

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def _reply(self, status, body, content_type=_JSON_CONTENT_TYPE, *, cookie=None):
        self.close_connection = True
        # The library suppresses headers for HTTP/0.9 and adds Server via send_response.
        self.request_version = self.protocol_version
        self.send_response_only(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in _RESPONSE_HEADERS:
            self.send_header(name, value)
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, kind):
        self.server.bridge._record(kind)
        self._reply(403, _REFUSED)

    def _remaining_ms(self):
        return self._deadline - self.server.bridge._clock_ms()

    def _recv(self, size):
        remaining = self._remaining_ms()
        if remaining <= 0:
            raise TimeoutError
        self.connection.settimeout(remaining / 1000)
        return self.connection.recv(size)

    def _head(self):
        incoming = bytearray()
        while True:
            chunk = self._recv(min(4096, _HEAD_BYTES + 1 - len(incoming)))
            if not chunk or self._remaining_ms() <= 0:
                return None
            incoming.extend(chunk)
            end = incoming.find(b'\r\n\r\n')
            if end >= 0 and end + 4 <= _HEAD_BYTES:
                return bytes(incoming[:end + 4]), bytes(incoming[end + 4:])
            if len(incoming) > _HEAD_BYTES:
                self.send_error(431)
                return None

    def _parse_head(self, head):
        with io.BytesIO(head) as header_stream:
            raw_reader = self.rfile
            self.rfile = header_stream
            try:
                self.raw_requestline = header_stream.readline()
                return self.parse_request()
            finally:
                self.rfile = raw_reader

    def _body_length(self, post):
        if self.headers.defects or self.headers.get_all('Transfer-Encoding'):
            return None
        lengths = self.headers.get_all('Content-Length', [])
        if post:
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,5}', lengths[0]):
                return None
            length = int(lengths[0])
            return length if length <= _BODY_BYTES else None
        return 0 if not lengths or lengths == ['0'] else None

    def _read_body(self, buffered, length):
        body = bytearray(buffered[:length])
        while len(body) < length:
            chunk = self._recv(length - len(body))
            if not chunk:
                return None
            body.extend(chunk)
        return bytes(body)

    def handle_one_request(self):
        self.close_connection = True
        with self.server.connections:
            self._deadline = self.server.active[self.request]
        try:
            incoming = self._head()
            if incoming is None or self._remaining_ms() <= 0:
                return
            head, buffered = incoming
            if not self._parse_head(head):
                return
            # The library may normalize the target. The mapper receives the wire spelling.
            parts = self.requestline.split()
            if len(parts) != 3:
                self.send_error(400)
                return
            path, _, query = parts[1].partition('?')
            post = self.command == 'POST' and (
                path == '/api/session' or
                (path.startswith('/api/op/') and bool(path[8:]) and '/' not in path[8:]))
            length = self._body_length(post)
            if length is None:
                self.send_error(400)
                return
            body = self._read_body(buffered, length)
            if body is None or self._remaining_ms() <= 0 or not self.server.begin_handler(self.request):
                return
            self._route(path, query, body)
        except (TimeoutError, ConnectionError):
            return
        finally:
            self.close_connection = True

    def _session(self, body):
        context = self.server.context
        post = self.command == 'POST'
        if self.headers.get_all('Host', []) != [context.advertised_host]:
            return self._refuse('host')
        origins = self.headers.get_all('Origin', [])
        if (post or origins) and origins != [context.advertised_origin]:
            return self._refuse('origin')
        fetch_sites = self.headers.get_all('Sec-Fetch-Site', [])
        if fetch_sites and fetch_sites != ['same-origin']:
            return self._refuse('fetch_site')
        if not post:
            token = self.server.session_token(self.headers.get_all('Cookie', []))
            if token is None:
                return self._refuse('session')
            return self._reply(200, jcs.jcs_encode({'csrf_token': token}))
        if self.headers.get_all('Content-Type', []) != ['application/json']:
            return self._refuse('media_type')
        try:
            args = strict_json_loads(body)
        except StrictJsonError:
            return self._refuse('args')
        if (not isinstance(args, dict) or args.keys() != {'launch'}
                or not isinstance(args['launch'], str) or jcs.jcs_encode(args) != body):
            return self._refuse('args')
        try:
            session = self.server.create_session(args['launch'])
        except (OSError, ValueError):
            session = None
        if session is None:
            return self._refuse('launch')
        session_id, token = session
        cookie = f'{self.server.cookie_name}={session_id}; HttpOnly; SameSite=Strict; Path=/'
        return self._reply(200, jcs.jcs_encode({'csrf_token': token}), cookie=cookie)

    def _page_post(self, operation_name, body):
        bridge = self.server.bridge
        registry, new_state = bridge._page_registry, bridge._page_state
        if operation_name not in _PAGE_OPERATIONS or registry is None or new_state is None:
            return self._refuse('route')

        context = self.server.context
        if self.headers.get_all('Host', []) != [context.advertised_host]:
            return self._refuse('host')
        if self.headers.get_all('Origin', []) != [context.advertised_origin]:
            return self._refuse('origin')
        fetch_sites = self.headers.get_all('Sec-Fetch-Site', [])
        if fetch_sites and fetch_sites != ['same-origin']:
            return self._refuse('fetch_site')
        if self.headers.get_all('Content-Type', []) != ['application/json']:
            return self._refuse('media_type')
        token = self.server.session_token(self.headers.get_all('Cookie', []))
        if token is None:
            return self._refuse('session')
        csrf = self.headers.get_all('X-ADT-CSRF', [])
        if len(csrf) != 1 or not csrf[0].isascii() or not compare_digest(csrf[0], token):
            return self._refuse('csrf')

        operation = registry.operation(operation_name)
        if operation is None:
            return self._refuse('args')
        try:
            args = strict_json_loads(body)
        except StrictJsonError:
            return self._refuse('args')
        if (jcs.jcs_encode(args) != body
                or isinstance(validate_type(registry, operation.request_ref, args), ValidationFailure)):
            return self._refuse('args')

        request = ApiRequest('1', operation_name, args)
        try:
            exchange = bridge._dispatch(new_state(), request, principal=OperatorPrincipal("page"))
            if exchange.response is None:
                raise ValueError('missing_page_response')
            status, response = page_answer(exchange.response)
        except Exception:
            self.server.handle_error(self.request, self.client_address)
            return self._reply(403, _REFUSED)
        return self._reply(status, response)

    def _page_get(self, operation_name, query):
        bridge = self.server.bridge
        registry, new_state = bridge._page_registry, bridge._page_state
        if operation_name not in _PAGE_READS or registry is None or new_state is None:
            return self._refuse('route')

        context = self.server.context
        if self.headers.get_all('Host', []) != [context.advertised_host]:
            return self._refuse('host')
        origins = self.headers.get_all('Origin', [])
        if origins and origins != [context.advertised_origin]:
            return self._refuse('origin')
        fetch_sites = self.headers.get_all('Sec-Fetch-Site', [])
        if fetch_sites and fetch_sites != ['same-origin']:
            return self._refuse('fetch_site')
        if self.server.session_token(self.headers.get_all('Cookie', [])) is None:
            return self._refuse('session')

        operation = registry.operation(operation_name)
        if operation is None:
            return self._refuse('args')
        try:
            args = _queue_read_args(operation.request_ref.name, page_query(query))
        except ValueError:
            return self._refuse('args')
        if isinstance(validate_type(registry, operation.request_ref, args), ValidationFailure):
            return self._refuse('args')

        request = ApiRequest('1', operation_name, args)
        try:
            exchange = bridge._dispatch(new_state(), request, principal=None)
            if exchange.response is None:
                raise ValueError('missing_page_response')
            status, response = page_answer(exchange.response)
        except Exception:
            self.server.handle_error(self.request, self.client_address)
            return self._reply(403, _REFUSED)
        return self._reply(status, response)

    def _serve_asset(self, path):
        if self.headers.get_all('Host', []) != [self.server.context.advertised_host]:
            return self._refuse('host')
        filename, content_type = _PAGE_ASSETS[path]
        try:
            content = (_PAGE_DIRECTORY / filename).read_bytes()
        except OSError:
            return self._reply(404, _REFUSED)
        return self._reply(200, content, content_type)

    def _route(self, path, query, body):
        if self.command == 'GET' and path in _PAGE_ASSETS and not query:
            return self._serve_asset(path)
        if self.command == 'POST' and path.startswith(_PAGE_OPERATION_PREFIX):
            self._page_post(path[len(_PAGE_OPERATION_PREFIX):], body)
            return
        if path == '/api/session' and self.command in ('GET', 'POST'):
            self._session(body)
            return
        if self.command == 'GET' and path.startswith(_PAGE_READ_PREFIX):
            self._page_get(path[len(_PAGE_READ_PREFIX):], query)
            return
        if self.command != 'GET' or path not in _DISCOVERY:
            self._refuse('route')
            return
        request = api_bridge.BridgeRequest(self.command, path, query,
                                           tuple(self.headers.raw_items()), body)
        try:
            result = api_bridge.map_discovery_get(request, self.server.context)
        except Exception:
            self.server.handle_error(self.request, self.client_address)
            self._reply(403, _REFUSED)
            return
        self._reply(result.status, result.body, dict(result.headers)['Content-Type'])
