"""Foreground serving, frame budgets and lifecycle on per-test scratch stores."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import itertools
import json
import os
from pathlib import Path
import signal
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from relay_engine import api_compose, api_definitions as d, api_keys, api_platform
from relay_engine import api_protocol as p, api_writer, jcs, store_layout, version
from relay_engine.tests.test_api_sessions import replace_occupant_for_test
from relay_engine.tests.test_submit import draft

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


def frame(op, args, number, size=None):
    raw = jcs.jcs_encode({'request_id': str(number), 'op': op, 'args': args})
    if size is not None:
        assert len(raw) <= size
        raw += b' ' * (size - len(raw))
    return len(raw).to_bytes(4, 'big') + raw


def exact(sock, size):
    parts = bytearray()
    while len(parts) < size:
        part = sock.recv(size - len(parts))
        if not part:
            raise EOFError('socket closed')
        parts.extend(part)
    return bytes(parts)


def response(sock):
    return json.loads(exact(sock, int.from_bytes(exact(sock, 4), 'big')))


class Connection:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(path)
        self.number = itertools.count(1)
        self.connected = self.call('protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}})

    def call(self, op, args, size=None):
        self.sock.sendall(frame(op, args, next(self.number), size))
        return response(self.sock)

    def close(self):
        self.sock.close()


class PrefixOnlySocket:
    def __init__(self, size):
        self.prefix = size.to_bytes(4, 'big')
        self.reads = []
        self.written = bytearray()
        self.closed = False

    def recv(self, size):
        self.reads.append(size)
        if len(self.reads) != 1:
            raise AssertionError('body read before refusal')
        assert size == 4
        return self.prefix

    def sendall(self, data):
        self.written.extend(data)

    def close(self):
        self.closed = True


class ApiServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='as-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home, self.temp = self.base / 'home', self.base / 'tmp'
        self.home.mkdir(mode=0o700); self.temp.mkdir(mode=0o700)
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'TMPDIR': str(self.temp)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.temp_patch = patch.object(tempfile, 'tempdir', str(self.temp))
        self.temp_patch.start(); self.addCleanup(self.temp_patch.stop)
        # Import is deliberately per-test: RED names each missing service witness.
        from relay_engine import api_service
        self.m = api_service
        self.root = self.base / 'store'
        catalogue = store_layout.open_catalogue(self.root, kind='isolated')
        catalogue.close()
        conn = store_layout.open_ledger(self.root, P, create=True)
        self.keys = {}
        self.tick = itertools.count(10000)
        try:
            conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (P, 'service')).close()
            conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
                         " VALUES ('team',?,'platform','pair',1,'operator')", (P,)).close()
            for role in ('planner', 'implementer'):
                conn.execute("INSERT INTO seats(id,team_instance_id,role,state) VALUES (?,'team',?,'vacant')",
                             (role, role)).close()
                key_id, secret = api_keys.issue_occupant_key(conn, role, next(self.tick))
                self.keys[role] = {'key_id': key_id, 'key_secret': secret}
            conn.commit()
        finally:
            conn.close()
        self.service = self.m.ApiService(str(self.root), P, clock=lambda: next(self.tick))
        self.addCleanup(self.service.stop)

    def start(self, service=None):
        service = service or self.service
        ready = Path(service.start())
        data = json.loads(ready.read_bytes())
        thread = threading.Thread(target=service.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(service.stop)
        return ready, data

    def connect(self, ready=None):
        if ready is None:
            ready = json.loads(Path(self.service.ready_path).read_bytes())
        conn = Connection(ready['socket'])
        self.addCleanup(conn.close)
        self.assertIn('result', conn.connected)
        return conn

    def sign_in(self, conn, role='planner'):
        result = conn.call('session.sign-in', dict(self.keys[role], host='codex',
            host_session_id=role, operation_key=role))
        self.assertIn('result', result, result)
        return result['result']

    def submit_args(self):
        return {'draft_base64': base64.b64encode(draft(to='platform.implementer')).decode(),
                'submission_key': 'submission'}

    def rows(self, sql, args=()):
        conn = store_layout.open_ledger(self.root, P)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def test_start_writes_ready_file_after_listening(self):
        original = os.replace
        observed = []
        def replace_ready(source, target):
            if Path(target).name == 'ready.json':
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(json.loads(Path(source).read_bytes())['socket'])
                    observed.append(True)
                finally:
                    sock.close()
            return original(source, target)
        with patch.object(os, 'replace', side_effect=replace_ready):
            ready = Path(self.service.start())
        data = json.loads(ready.read_bytes())
        self.assertEqual(observed, [True])
        self.assertEqual(set(data), {'format', 'socket', 'store_id', 'build_id', 'schema_digest', 'pid'})
        self.assertEqual(data['format'], 'adt-ready-v1')
        self.assertEqual(stat.S_IMODE(ready.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(ready.parent.stat().st_mode), 0o700)
        self.assertTrue(ready.is_absolute())
        self.assertEqual(Path(data['socket']).parent, ready.parent)

    def test_existing_callers_unchanged(self):
        original = p.dispatch_api
        recorded = []
        def recording(state, request, runtime, registry, ports, **kwargs):
            recorded.append((getattr(request, 'op', None), state.binding, kwargs))
            return original(state, request, runtime, registry, ports)
        with patch.object(p, 'dispatch_api', side_effect=recording):
            self.start()
            conn = self.connect()
            self.sign_in(conn)
            self.assertIn('result', conn.call('seat.identify', self.keys['planner']))
            self.assertIn('result', conn.call('relay.submit', self.submit_args()))
        self.assertEqual([row[0] for row in recorded],
                         ['protocol.connect', 'session.sign-in', 'seat.identify', 'relay.submit'])
        self.assertIsNotNone(recorded[-1][1])
        self.assertTrue(all(row[2] == {'principal': None} for row in recorded), recorded)

    def test_second_service_refuses_store_locked(self):
        ready, _ = self.start()
        other = self.m.ApiService(str(self.root), P, clock=lambda: next(self.tick))
        self.addCleanup(other.stop)
        with self.assertRaises(self.m.ServiceStartRefused) as caught:
            other.start()
        self.assertEqual(caught.exception.reason, 'store_locked')
        self.assertEqual(list(self.temp.rglob('ready.json')), [ready])

    def test_unrouted_operation_refuses_start(self):
        platform = api_platform.platform_component()
        for mode in ('missing', 'unrouted'):
            with self.subTest(mode=mode):
                service = self.m.ApiService(str(self.root), P, clock=lambda: 1,
                    components=(replace(platform, port_factory=lambda context: None),))
                self.addCleanup(service.stop)
                if mode == 'missing':
                    action = service.start
                else:
                    # A complete component factory with a damaged routing result.
                    service.components = (platform,)
                    original = api_compose.compose_components
                    def compose(components):
                        value = original(components)
                        return replace(value, owners={k:v for k,v in value.owners.items() if k != 'relay.submit'})
                    action = lambda: self.start_with_patch(service, compose)
                with self.assertRaises(self.m.ServiceStartRefused) as caught:
                    action()
                self.assertEqual(caught.exception.reason, 'port_missing' if mode == 'missing' else 'port_unrouted')
                self.assertEqual(list(self.temp.rglob('*.sock')), [])

    @staticmethod
    def start_with_patch(service, compose):
        with patch.object(api_compose, 'compose_components', side_effect=compose):
            return service.start()

    def test_no_2_9_7_paths_touched(self):
        legacy = Path('/tmp/relay-engine')
        before = set(legacy.iterdir()) if legacy.is_dir() else set()
        class Environment(dict):
            def get(self, key, default=None):
                if key == 'RELAY_SOCKET':
                    raise AssertionError('legacy endpoint read')
                return super().get(key, default)
            def __getitem__(self, key):
                if key == 'RELAY_SOCKET':
                    raise AssertionError('legacy endpoint read')
                return super().__getitem__(key)
        with patch.object(os, 'environ', Environment(os.environ)):
            self.start(); self.service.stop()
        self.assertFalse((self.root / '.engine').exists())
        self.assertEqual(set(legacy.iterdir()) if legacy.is_dir() else set(), before)
        self.assertFalse((self.home / '.adt').exists())

    def test_sigterm_drains_and_removes_files(self):
        ready, data = self.start()
        entered, release = threading.Event(), threading.Event()
        class Job:
            files = frozenset({self.service.writer.ledger_file(P)})
            def run(job, conn):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('release absent')
                conn.execute("UPDATE projects SET name='drained'").close()
                return api_writer.JobOutcome(True, 'committed')
        order = []
        real_unlink = os.unlink
        def unlink(path, *args, **kwargs):
            order.append(str(path))
            return real_unlink(path, *args, **kwargs)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.service.writer.submit, P, Job())
            try:
                self.assertTrue(entered.wait(5))
                timer = threading.Timer(0.05, release.set); timer.start()
                try:
                    with patch.object(os, 'unlink', side_effect=unlink):
                        os.kill(os.getpid(), signal.SIGTERM)
                finally:
                    timer.join()
                self.assertEqual(future.result(5).value, 'committed')
            finally:
                release.set()
        self.assertEqual(self.rows('SELECT name FROM projects'), [('drained',)])
        self.assertFalse(ready.exists()); self.assertFalse(Path(data['socket']).exists())
        self.assertLess(order.index(str(ready)), order.index(data['socket']))

    def test_second_signal_during_drain_finishes_stop(self):
        import fcntl
        import subprocess
        import sys
        import time

        tools = Path(__file__).resolve().parents[2]
        for first in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(first=first.name):
                root = self.base / first.name
                root.mkdir()
                script = f'''
import os, signal, sys
sys.path.insert(0, {str(tools)!r})
from relay_engine import api_service
service = api_service.ApiService({str(root)!r}, {P!r}, clock=lambda: 1)
service._take_lock()
for name in (signal.SIGTERM, signal.SIGINT):
    service._signals[name] = signal.getsignal(name)
    signal.signal(name, service._signal)
class Writer:
    calls = 0
    def close(self):
        self.calls += 1
        print('drain-entered', flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        if service._lock_fd is not None:
            print('lock-held', flush=True)
writer = Writer()
service.writer = writer
os.kill(os.getpid(), {int(first)})
assert writer.calls == 1, writer.calls
print('stopped', flush=True)
'''
                started = time.monotonic()
                timed_out = False
                try:
                    result = subprocess.run([sys.executable, '-I', '-c', script],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
                except subprocess.TimeoutExpired as error:
                    timed_out = True
                    self.fail(f'child timed out: {error.stdout!r}')
                finally:
                    if target := os.environ.get('RELAY_ENGINE_RESULTS_ROOT'):
                        with (Path(target) / 'second-signal-timings.jsonl').open('a') as stream:
                            stream.write(json.dumps({'first': first.name,
                                'seconds': time.monotonic() - started,
                                'timed_out': timed_out}) + '\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ['drain-entered', 'lock-held', 'stopped'])
                with (root / 'api-service.lock').open('r+b') as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def test_connection_bindings_are_independent(self):
        self.start()
        for role in ('planner', 'implementer'):
            conn = self.connect(); self.sign_in(conn, role)
            result = conn.call('relay.submit', self.submit_args())
            self.assertIn('result', result, result)
        self.assertEqual(set(self.rows('SELECT sender_seat_id FROM relays')), {('planner',), ('implementer',)})

    def test_closed_connection_keeps_session_open(self):
        self.start(); conn = self.connect(); signed = self.sign_in(conn); conn.close()
        self.assertEqual(self.rows('SELECT ended_at FROM seat_sessions WHERE id=?', (signed['session_id'],)), [(None,)])
        other = self.connect()
        self.assertIn('result', other.call('session.resume', dict(self.keys['planner'], session_id=signed['session_id'])))

    def test_oversized_prefix_refused_before_body(self):
        self.start()
        binding = p.SeatBinding(P, 'team', 'planner', 'occupant', 'key', 'session', 1)
        for bound in (False, True):
            state = replace(p.initial_connection_state(), binding=binding if bound else None)
            budget = self.m.REQUEST_BUDGET_BOUND if bound else self.m.REQUEST_BUDGET_UNBOUND
            fake = PrefixOnlySocket(budget + 1)
            self.service._connection(fake, state)
            self.assertEqual(fake.reads, [4]); self.assertTrue(fake.closed)
            size = int.from_bytes(fake.written[:4], 'big')
            self.assertEqual(len(fake.written), size + 4)
            value = json.loads(fake.written[4:])
            self.assertIsNone(value['request_id'])
            self.assertEqual(value['error']['cause']['kind'], 'malformed_frame')
        # In-memory read-first control must hit the socket's explicit body assertion.
        fake = PrefixOnlySocket(self.m.REQUEST_BUDGET_UNBOUND + 1)
        def read_first(sock, state):
            prefix = self.service._exact(sock, 4)
            self.service._exact(sock, int.from_bytes(prefix, 'big'))
        with patch.object(self.service, '_read_request', side_effect=read_first):
            with self.assertRaisesRegex(AssertionError, 'body read before refusal'):
                self.service._connection(fake)

    def test_budget_boundaries(self):
        self.start()
        for bound in (False, True):
            conn = self.connect()
            if bound:
                self.sign_in(conn, 'implementer')
            budget = self.m.REQUEST_BUDGET_BOUND if bound else self.m.REQUEST_BUDGET_UNBOUND
            self.assertIn('result', conn.call('protocol.schema', {}, budget))
            conn.sock.sendall((budget + 1).to_bytes(4, 'big'))
            self.assertEqual(response(conn.sock)['error']['cause']['kind'], 'malformed_frame')
            self.assertEqual(conn.sock.recv(1), b'')

    def test_largest_relay_submit_admitted(self):
        self.start(); conn = self.connect(); self.sign_in(conn)
        prefix = draft(to='platform.implementer', body='')
        data = prefix + b'x' * (131072 - len(prefix))
        args = {'draft_base64': base64.b64encode(data).decode(), 'submission_key': '\x01' * 256}
        encoded = frame('relay.submit', args, 18446744073709551615)
        self.assertEqual(len(args['draft_base64']), 174764)
        self.assertEqual(len(encoded) - 4, 176404)
        port = self.service.operation_ports['relay.submit'].submit
        with patch.object(port, 'run', wraps=port.run) as called:
            conn.sock.sendall(encoded)
            answer = response(conn.sock)
        called.assert_called_once()
        self.assertEqual(answer['error']['code'], 'E_API_ADMISSION')
        self.assertEqual(answer['error']['cause']['engine_code'], 'E-BODY-SIZE')

    def test_budget_follows_binding(self):
        self.start()
        for role, clear in (('planner', 'end'), ('implementer', 'replace')):
            conn = self.connect(); signed = self.sign_in(conn, role)
            self.assertIn('result', conn.call('protocol.schema', {}, 200000))
            if clear == 'end':
                answer = conn.call('session.end', dict(self.keys[role], session_id=signed['session_id'], operation_key='end'))
                self.assertIn('result', answer)
            else:
                replace_occupant_for_test(self.root, P, role, next(self.tick))
                self.assertEqual(conn.call('relay.submit', self.submit_args())['error']['cause']['kind'], 'key_replaced')
            conn.sock.sendall((200000).to_bytes(4, 'big'))
            self.assertEqual(response(conn.sock)['error']['cause']['kind'], 'malformed_frame')
            self.assertEqual(conn.sock.recv(1), b'')

    def test_refused_end_keeps_budget(self):
        self.start(); conn = self.connect(); signed = self.sign_in(conn)
        bad = dict(self.keys['planner'], key_secret='A' * 43, session_id=signed['session_id'], operation_key='end')
        self.assertEqual(conn.call('session.end', bad)['error']['cause']['kind'], 'key_rejected')
        self.assertIn('result', conn.call('protocol.schema', {}, 200000))
        self.assertEqual(conn.call('session.end', {})['error']['cause']['kind'], 'invalid_args')
        self.assertIn('result', conn.call('protocol.schema', {}, 200000))

    def test_pipelined_sign_in_and_submit(self):
        self.start(); conn = self.connect()
        args = dict(self.keys['planner'], host='codex', host_session_id='pipeline', operation_key='pipeline')
        conn.sock.sendall(frame('session.sign-in', args, 2) + frame('relay.submit', self.submit_args(), 3, 200000))
        self.assertEqual(response(conn.sock)['request_id'], '2')
        answer = response(conn.sock)
        self.assertEqual(answer['request_id'], '3'); self.assertIn('result', answer)

    def test_connection_bound(self):
        self.start()
        clients = [self.connect() for _ in range(self.m.MAX_CONNECTIONS)]
        extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(extra.close); extra.settimeout(5)
        extra.connect(json.loads(Path(self.service.ready_path).read_bytes())['socket'])
        self.assertEqual(extra.recv(1), b'')
        clients[0].close()
        with self.service._condition:
            self.assertTrue(self.service._condition.wait_for(
                lambda: len(self.service._clients) < self.m.MAX_CONNECTIONS, timeout=5))
        self.connect()

    def test_only_the_service_calls_writer_lifecycle(self):
        calls = []
        real = api_writer.StoreWriter
        class RecordingWriter:
            def __init__(owner, *args, **kwargs):
                owner.actual = real(*args, **kwargs)
            def __getattr__(owner, name):
                return getattr(owner.actual, name)
            def start(owner):
                calls.append('start'); return owner.actual.start()
            def open_project(owner, project):
                calls.append('open_project'); return owner.actual.open_project(project)
            def close(owner):
                calls.append('close'); return owner.actual.close()
        with patch.object(self.m, 'StoreWriter', RecordingWriter):
            self.start()
            baseline = list(calls)
            conn = self.connect(); signed = self.sign_in(conn)
            self.assertIn('result', conn.call('relay.submit', self.submit_args()))
            resumed = self.connect()
            self.assertIn('result', resumed.call('session.resume', dict(self.keys['planner'], session_id=signed['session_id'])))
            self.assertIn('result', resumed.call('session.end', dict(self.keys['planner'], session_id=signed['session_id'], operation_key='end')))
            self.assertEqual(calls, baseline)
            self.assertEqual(baseline, ['start', 'open_project'])
            self.service.stop(); self.assertEqual(calls, baseline + ['close'])
            platform = api_platform.platform_component()
            factory = platform.port_factory
            def wrong_factory(context):
                context.port.open_project(P)
                return factory(context)
            calls.clear()
            control = self.m.ApiService(str(self.root), P, clock=lambda: next(self.tick),
                                        components=(replace(platform, port_factory=wrong_factory),))
            self.addCleanup(control.stop)
            control.start()
            with self.assertRaises(AssertionError):
                self.assertEqual(calls, ['start', 'open_project'])

    def test_ready_file_build_id_is_the_install_fingerprint(self):
        ready, data = self.start()
        tools = Path(__file__).resolve().parents[2]
        self.assertEqual(data['build_id'], version.fingerprint(tools))
        self.assertRegex(data['build_id'], r'^[0-9a-f]{64}$')
        self.assertEqual(self.connect(data).connected['result']['build_id'], data['build_id'])

    def test_fingerprint_fault_refuses_start_before_the_lock(self):
        taken = []
        def take():
            taken.append(True)
        with patch.object(version, 'fingerprint', side_effect=version.FingerprintError('missing-member')):
            with patch.object(self.service, '_take_lock', side_effect=take):
                with self.assertRaises(version.FingerprintError):
                    self.service.start()
                self.assertEqual(taken, [])
                self.assertEqual(list(self.temp.iterdir()), [])
                # Control: taking the lock first violates the same observation.
                take()
                with self.assertRaises(AssertionError):
                    self.assertEqual(taken, [])


    def test_writer_start_fault_propagates_before_any_file(self):
        root = self.base / 'missing-catalogue'
        conn = store_layout.open_ledger(root, P, create=True)
        conn.close()
        for attempt in range(2):
            service = self.m.ApiService(str(root), P, clock=lambda: next(self.tick))
            self.addCleanup(service.stop)
            caught = None
            try:
                service.start()
            except Exception as error:
                caught = error
            self.assertIsInstance(caught, store_layout.LayoutFault)
            self.assertEqual(caught.cause, 'missing')
            self.assertIsNone(service.runtime_dir)
            self.assertIsNone(service.socket_path)
            self.assertIsNone(service.ready_path)
            self.assertFalse(any(t.name == 'store-writer' and t.is_alive()
                                 for t in threading.enumerate()))

    def test_missing_store_root_refuses_before_any_file(self):
        for kind in ('absent', 'regular-file'):
            with self.subTest(kind=kind):
                root = self.base / kind
                if kind == 'regular-file':
                    root.write_text('not a folder')
                before = {p.relative_to(self.base) for p in self.base.rglob('*')}
                for attempt in range(2):
                    service = self.m.ApiService(str(root), P, clock=lambda: next(self.tick))
                    self.addCleanup(service.stop)
                    with self.assertRaises(store_layout.LayoutFault) as caught:
                        service.start()
                    self.assertEqual(caught.exception.cause, 'missing')
                    self.assertEqual({p.relative_to(self.base) for p in self.base.rglob('*')}, before)
                    self.assertIsNone(service.runtime_dir)
                    self.assertIsNone(service.socket_path)
                    self.assertIsNone(service.ready_path)
