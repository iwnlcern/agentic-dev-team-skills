"""Client identity, credential and response guards on isolated endpoints."""
import ast
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay_engine import api_protocol, api_schema, jcs
from relay_engine.tests import test_api_service as fixture


def sign_in_calls(source):
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Attribute) and node.func.attr == 'sign_in'
                or isinstance(node.func, ast.Name) and node.func.id == 'sign_in')]


class ClientTests(unittest.TestCase):
    def setUp(self):
        fixture.ApiServiceTests.setUp(self)
        from relay_engine import api_client
        self.c = api_client

    start = fixture.ApiServiceTests.start

    def key(self):
        return self.c.SeatKey(**self.keys['planner'])

    def key_path(self):
        path = self.base / 'key'
        key = self.keys['planner']
        path.write_text('adt-seat-key-v1 ' + key['key_id'] + ' ' + key['key_secret'] + '\n')
        path.chmod(0o600)
        return path

    def client(self):
        path, _ = self.start()
        client = self.c.ApiClient.open(self.c.read_ready_file(str(path)))
        self.addCleanup(client.close)
        return client

    def scripted(self, change=None, answer=None):
        # The fake peer uses a real Unix socket and retains only request frames.
        directory = self.temp / ('fake' + str(len(list(self.temp.iterdir()))))
        directory.mkdir(mode=0o700)
        path = directory / 'api.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path)); listener.listen(1); listener.settimeout(5)
        self.addCleanup(listener.close)
        build = self.service
        self.start()
        ready_data = json.loads(Path(build.ready_path).read_bytes())
        ready_data['socket'] = str(path)
        ready_path = directory / 'ready.json'
        ready_path.write_text(json.dumps(ready_data)); ready_path.chmod(0o600)
        requests, raw_frames, errors = [], [], []
        def run():
            try:
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(5)
                    while True:
                        try:
                            raw = fixture.exact(peer, int.from_bytes(fixture.exact(peer, 4), 'big'))
                        except EOFError:
                            break
                        request = json.loads(raw)
                        requests.append(request); raw_frames.append(raw)
                        op = request['op']
                        if op == 'protocol.connect':
                            state = api_protocol.ConnectionState('connected', 1, '3.0',
                                build.runtime.schemas['3.0'].digest)
                            result = api_protocol._connect({}, build.runtime, state, build.registry)
                            if change is not None:
                                result = change(dict(result))
                            response = {'request_id': request['request_id'], 'result': result}
                        elif answer is not None:
                            response = answer(request)
                        else:
                            response = {'request_id': request['request_id'],
                                'error': api_protocol.api_error(build.registry, 'key_rejected')}
                        raw = jcs.jcs_encode(response)
                        peer.sendall(len(raw).to_bytes(4, 'big') + raw)
            except (OSError, EOFError):
                pass
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        def finish():
            listener.close(); thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.addCleanup(finish)
        return self.c.read_ready_file(str(ready_path)), requests, raw_frames

    def test_key_file_guards_refuse_before_connect(self):
        path = self.key_path()
        with patch.object(socket.socket, 'connect') as connect:
            symlink = self.base / 'link'; symlink.symlink_to(path)
            for target in (symlink,):
                with self.assertRaises(self.c.LocalRefusal):
                    self.c.read_key_file(str(target))
            path.chmod(0o640)
            with self.assertRaises(self.c.LocalRefusal) as error:
                self.c.read_key_file(str(path))
            self.assertEqual(error.exception.kind, 'key_file_unprotected')
            path.chmod(0o600)
            original = os.stat
            def foreign(name, *args, **kwargs):
                result = original(name, *args, **kwargs)
                if Path(name) == path:
                    fields = list(result); fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return result
            with patch.object(os, 'stat', side_effect=foreign):
                with self.assertRaises(self.c.LocalRefusal):
                    self.c.read_key_file(str(path))
            path.write_text('malformed\n')
            with self.assertRaises(self.c.LocalRefusal):
                self.c.read_key_file(str(path))
            connect.assert_not_called()

    def test_secret_never_in_repr_or_errors(self):
        key = self.key(); secret = key.key_secret
        text = repr(key) + str(key)
        for kind in ('endpoint_not_selected', 'ready_file_invalid', 'ready_file_mismatch',
                     'key_file_invalid', 'key_file_unprotected', 'mixed_credentials',
                     'no_session', 'draft_unreadable', 'draft_too_large'):
            text += str(self.c.LocalRefusal(kind)) + repr(self.c.LocalRefusal(kind))
        for outcome in ('not_performed', 'unknown', 'endpoint_mismatch'):
            text += str(self.c.EndpointUnavailable(outcome))
        self.assertNotIn(secret, text)
        ready, requests, _ = self.scripted()
        out, err = io.StringIO(), io.StringIO()
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        with redirect_stdout(out), redirect_stderr(err):
            client = self.c.ApiClient.open(ready)
            try:
                client.identify(key)
                client.sign_in(key, 'codex', 'window', 'sign')
                client.call('session.resume', dict(self.keys['planner'], session_id='session'))
                client.call('session.end', dict(self.keys['planner'], session_id='session', operation_key='end'))
                client.call('protocol.schema', {})
            finally:
                client.close()
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        carrying = {r['op'] for r in requests if secret in json.dumps(r)}
        self.assertEqual(carrying, {'seat.identify', 'session.sign-in', 'session.resume', 'session.end'})

    def test_ready_file_checks(self):
        path, data = self.start()
        original = path.read_bytes()
        path.chmod(0o620)
        with self.assertRaises(self.c.LocalRefusal): self.c.read_ready_file(str(path))
        path.chmod(0o600); path.parent.chmod(0o750)
        with self.assertRaises(self.c.LocalRefusal): self.c.read_ready_file(str(path))
        path.parent.chmod(0o700)
        for replacement in ({**data, 'format': 'other'}, {**data, 'socket': str(self.base / 'elsewhere')}):
            path.write_text(json.dumps(replacement))
            with self.assertRaises(self.c.LocalRefusal): self.c.read_ready_file(str(path))
        path.write_bytes(original)
        ready = replace(self.c.read_ready_file(str(path)), socket=str(path.parent / 'absent'))
        with self.assertRaises(self.c.EndpointUnavailable) as error: self.c.ApiClient.open(ready)
        self.assertEqual(error.exception.outcome, 'not_performed')

    def test_ready_file_reader_ignores_unknown_keys(self):
        path, data = self.start()
        expected = self.c.read_ready_file(str(path))
        path.write_text(json.dumps({**data, 'store_state': 'serving', 'held_causes': [],
                                    'future_metadata': {'value': None}}))
        self.assertEqual(self.c.read_ready_file(str(path)), expected)
        for required in data:
            path.write_text(json.dumps({k: v for k, v in data.items() if k != required}))
            with self.assertRaises(self.c.LocalRefusal): self.c.read_ready_file(str(path))

    def test_schema_pin_mismatch(self):
        from dataclasses import replace as dc_replace
        original = self.service.start
        def different():
            path = original()
            artifact = self.service.runtime.schemas['3.0']
            self.service.runtime = dc_replace(self.service.runtime,
                schemas={'3.0': dc_replace(artifact, digest='f' * 64)})
            return path
        with patch.object(self.service, 'start', side_effect=different):
            path, _ = self.start()
        with patch.object(api_protocol, 'dispatch_api', wraps=api_protocol.dispatch_api) as dispatch:
            with self.assertRaises(self.c.EndpointUnavailable):
                self.c.ApiClient.open(self.c.read_ready_file(str(path)))
        self.assertEqual([c.args[1].op for c in dispatch.call_args_list], ['protocol.connect'])
        result = api_protocol.dispatch_api(api_protocol.initial_connection_state(),
            __import__('relay_engine.api_wire', fromlist=['ApiRequest']).ApiRequest('1', 'protocol.connect',
            {'supported': {'min': '3.0', 'max': '3.0'}, 'required_schema_digest': json.loads(path.read_bytes())['schema_digest']}),
            self.service.runtime, self.service.registry, self.service.operation_ports)
        self.assertEqual(result.response['error']['cause']['kind'], 'pin_mismatch')

    def test_endpoint_identity_checked_before_credentials(self):
        for field, value in [('store_id', 'another-store'), ('build_id', 'a' * 64)]:
            ready, requests, _ = self.scripted(lambda result, f=field, v=value: {**result, f: v})
            with self.assertRaises(self.c.EndpointUnavailable) as error: self.c.ApiClient.open(ready)
            self.assertEqual(error.exception.outcome, 'endpoint_mismatch')
            self.assertEqual([r['op'] for r in requests], ['protocol.connect'])
        ready = self.c.read_ready_file(self.service.ready_path)
        with patch.object(socket.socket, 'connect') as connect:
            with self.assertRaises(self.c.LocalRefusal) as error:
                self.c.ApiClient.open(replace(ready, schema_digest='e' * 64))
            self.assertEqual(error.exception.kind, 'ready_file_mismatch'); connect.assert_not_called()
        client = self.c.ApiClient.open(ready)
        try: self.assertIn('session_id', client.sign_in(self.key(), 'codex', 'matching', 'control'))
        finally: client.close()

    def test_response_guard(self):
        for answer in (lambda request: {'request_id': '999', 'result': {}},
                       lambda request: {'request_id': request['request_id'], 'result': {'unexpected': 'x'}}):
            ready, _, _ = self.scripted(answer=answer)
            client = self.c.ApiClient.open(ready)
            try:
                with self.assertRaises(self.c.EndpointUnavailable) as error: client.identify(self.key())
                self.assertEqual(error.exception.outcome, 'unknown')
            finally: client.close()

    def test_typed_error_returned_as_value(self):
        client = self.client()
        key = replace(self.key(), key_secret='A' * 43)
        result = client.identify(key)
        self.assertIsInstance(result, self.c.ApiErrorValue)
        self.assertEqual((result.code, result.cls, result.cause, result.remedy),
                         ('E_API_AUTH', 'auth', {'kind': 'key_rejected'}, 'check_key_file'))

    def test_requests_are_jcs_encoded(self):
        ready, requests, frames = self.scripted()
        client = self.c.ApiClient.open(ready)
        try: client.identify(self.key())
        finally: client.close()
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames, [jcs.jcs_encode(r) for r in requests])
        self.assertEqual([r['request_id'] for r in requests], ['1', '2'])

    def test_identify_and_sign_in_wrap_call(self):
        client = self.client(); key = self.key()
        seat = client.identify(key)
        self.assertEqual(set(seat), {'seat_id', 'seat_address', 'project_id'})
        session = client.sign_in(key, 'codex', 'window', 'once')
        self.assertEqual((session['seat_id'], session['project_id']), (seat['seat_id'], seat['project_id']))
        ready, requests, _ = self.scripted()
        other = self.c.ApiClient.open(ready)
        try:
            other.identify(key); other.sign_in(key, 'codex', 'window', 'once')
        finally: other.close()
        self.assertEqual([r['op'] for r in requests], ['protocol.connect', 'seat.identify', 'session.sign-in'])
        self.assertEqual(set(requests[1]['args']), {'key_id', 'key_secret'})
        self.assertEqual(set(requests[2]['args']), {'key_id', 'key_secret', 'host', 'host_session_id', 'operation_key'})
        self.assertNotIn(key.key_secret, json.dumps(requests[0]))

    def test_no_shipped_caller_of_sign_in(self):
        """Ends at G-B1: Adapter hooks Task 3 counts sign_in calls under owner review."""
        tools = Path(__file__).resolve().parents[2]
        hits = [(str(p.relative_to(tools)), sign_in_calls(p.read_text()))
                for p in tools.rglob('*.py') if 'tests' not in p.relative_to(tools).parts]
        self.assertEqual([(p, lines) for p, lines in hits if lines], [])
        self.assertEqual(sign_in_calls('client.sign_in(key, host, window, operation_key)'), [1])


class _DeadlineSocket:
    def __init__(self, registry):
        self.registry = registry
        self.timeouts = []
        self.requests = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def gettimeout(self):
        return self.timeouts[-1]

    def sendall(self, data):
        self.requests.append(json.loads(data[4:]))

    def recv(self, _size):
        raw = jcs.jcs_encode({
            'request_id': self.requests[-1]['request_id'],
            'error': api_protocol.api_error(self.registry, 'key_rejected'),
        })
        return len(raw).to_bytes(4, 'big') + raw

    def connect(self, _path):
        raise socket.timeout

    def close(self):
        self.closed = True


class ClientDeadlineTests(unittest.TestCase):
    def setUp(self):
        from relay_engine import api_client

        self.c = api_client
        self.registry, self.digest = self.c._registry_and_digest()

    def ready(self, endpoint='unused'):
        return self.c.ReadyFile('unused', endpoint, 'fixture', 'a' * 64, self.digest, 1)

    def recording_client(self):
        peer = _DeadlineSocket(self.registry)
        peer.settimeout(30)
        client = self.c.ApiClient(peer, self.registry, self.digest)
        self.addCleanup(client.close)
        return client, peer

    def bounded(self, call, unblock):
        outcomes = []

        def invoke():
            try:
                outcomes.append(call())
            except BaseException as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(1.2)
        if worker.is_alive():
            unblock()
            worker.join(0.4)
            self.assertFalse(worker.is_alive(), 'bounded test worker did not stop')
            self.fail('client operation exceeded the test time bound')
        result, = outcomes
        if isinstance(result, BaseException):
            raise result
        return result

    def test_open_past_deadline_not_performed(self):
        with patch.object(socket, 'socket') as construct:
            with self.assertRaises(self.c.EndpointUnavailable) as error:
                self.c.ApiClient.open(self.ready(), deadline=time.monotonic() - 1)
            construct.assert_not_called()
        self.assertEqual(error.exception.outcome, 'not_performed')

    def test_open_deadline_bounds_silent_discovery(self):
        with tempfile.TemporaryDirectory(prefix='ad-c-') as directory:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                address = str(Path(directory) / 'api.sock')
                listener.bind(address)
                listener.listen(1)
                listener.settimeout(0.8)
                accepted = threading.Event()
                release = threading.Event()

                def silence():
                    try:
                        peer, _ = listener.accept()
                    except (OSError, TimeoutError):
                        return
                    with peer:
                        accepted.set()
                        release.wait(1.5)

                server = threading.Thread(target=silence, daemon=True)
                server.start()
                try:
                    start = time.monotonic()
                    with self.assertRaises(self.c.EndpointUnavailable) as error:
                        self.bounded(
                            lambda: self.c.ApiClient.open(self.ready(address), deadline=start + 0.3),
                            release.set,
                        )
                    self.assertTrue(accepted.is_set())
                    self.assertEqual(error.exception.outcome, 'unknown')
                    self.assertLess(time.monotonic() - start, 2)
                finally:
                    release.set()
                    server.join(0.9)
                    self.assertFalse(server.is_alive())

    def test_call_deadline_bounds_silent_reply(self):
        sock, peer = socket.socketpair()
        with sock, peer:
            sock.settimeout(30)
            client = self.c.ApiClient(sock, self.registry, self.digest)
            start = time.monotonic()
            with self.assertRaises(self.c.EndpointUnavailable) as error:
                self.bounded(
                    lambda: client.call('queue.list', {}, deadline=start + 0.3),
                    lambda: peer.shutdown(socket.SHUT_RDWR),
                )
            self.assertEqual(error.exception.outcome, 'unknown')
            self.assertLess(time.monotonic() - start, 2)
            peer.settimeout(0.15)
            size = int.from_bytes(fixture.exact(peer, 4), 'big')
            request = json.loads(fixture.exact(peer, size))
            self.assertEqual(request['op'], 'queue.list')
            self.assertEqual(sock.fileno(), -1)

    def test_call_without_deadline_restores_default_timeout(self):
        client, peer = self.recording_client()
        bounded = client._call('queue.list', {}, deadline=time.monotonic() + 1)
        self.assertLess(peer.gettimeout(), 30)
        self.assertEqual(client.call('queue.list', {}), bounded)
        self.assertEqual(peer.gettimeout(), 30)

    def test_deadline_domain(self):
        client, peer = self.recording_client()
        expected = client.call('queue.list', {}, deadline=time.monotonic() + 60)
        for value, exception in (
            ('1', TypeError), (True, TypeError), (float('nan'), ValueError),
            (float('inf'), ValueError), (-float('inf'), ValueError),
            (10**1000, ValueError), (-10**1000, ValueError), (2e15, ValueError),
        ):
            with self.subTest(value=value):
                before = (len(peer.requests), len(peer.timeouts), client._request_id)
                with self.assertRaises(exception):
                    client.call('queue.list', {}, deadline=value)
                self.assertEqual(
                    (len(peer.requests), len(peer.timeouts), client._request_id), before
                )
                self.assertFalse(peer.closed)
                self.assertEqual(client.call('queue.list', {}), expected)

    def test_expired_deadline_refuses_before_lock(self):
        client, peer = self.recording_client()
        with patch.object(client, '_lock', wraps=client._lock) as lock:
            lock.acquire(blocking=False)
            try:
                start = time.monotonic()
                with self.assertRaises(self.c.EndpointUnavailable) as error:
                    client.call('queue.list', {}, deadline=start - 1.5)
                self.assertLess(time.monotonic() - start, 0.1)
                self.assertEqual(error.exception.outcome, 'not_performed')
                lock.acquire.assert_called_once_with(blocking=False)
                self.assertFalse(peer.closed)
                self.assertEqual(peer.requests, [])
                self.assertEqual(client._request_id, 0)
            finally:
                lock.release()

    def test_far_deadline_caps_socket_wait_at_30(self):
        client, peer = self.recording_client()
        client.call('queue.list', {}, deadline=time.monotonic() + 1e12)
        self.assertGreater(len(peer.timeouts), 1)
        self.assertTrue(all(0 < timeout <= 30 for timeout in peer.timeouts))

    def test_lock_wait_bounded_without_closing(self):
        client, peer = self.recording_client()
        lock = client._lock
        self.assertTrue(lock.acquire(blocking=False))
        released = False

        def unblock():
            nonlocal released
            lock.release()
            released = True

        try:
            start = time.monotonic()
            with self.assertRaises(self.c.EndpointUnavailable) as error:
                self.bounded(
                    lambda: client.call('queue.list', {}, deadline=start + 0.2),
                    unblock,
                )
            self.assertEqual(error.exception.outcome, 'not_performed')
            self.assertLess(time.monotonic() - start, 2)
            self.assertFalse(peer.closed)
            self.assertEqual(peer.requests, [])
            self.assertEqual(client._request_id, 0)
        finally:
            if not released:
                unblock()
        self.assertIsInstance(client.call('queue.list', {}), self.c.ApiErrorValue)

    def test_open_connect_deadline_installed(self):
        for budget in (5, 100):
            with self.subTest(budget=budget):
                peer = _DeadlineSocket(self.registry)
                with patch.object(socket, 'socket', return_value=peer):
                    with patch.object(self.c.ApiClient, '_discover') as discover:
                        with self.assertRaises(self.c.EndpointUnavailable) as error:
                            self.c.ApiClient.open(
                                self.ready(), deadline=time.monotonic() + budget
                            )
                discover.assert_not_called()
                self.assertEqual(error.exception.outcome, 'not_performed')
                self.assertTrue(peer.closed)
                self.assertEqual(len(peer.timeouts), 1)
                if budget == 5:
                    self.assertGreater(peer.timeouts[0], 0)
                    self.assertLessEqual(peer.timeouts[0], 5)
                else:
                    self.assertEqual(peer.timeouts[0], 30)

    def test_identify_and_sign_in_forward_deadline(self):
        client, _ = self.recording_client()
        key = self.c.SeatKey('fixture', 'a' * 43)
        deadline = time.monotonic() + 3
        with patch.object(client, 'call') as call:
            client.identify(key, deadline=deadline)
            client.sign_in(key, 'codex', 'window', 'once', deadline=deadline)
            self.assertEqual(
                [(row.args[0], row.kwargs) for row in call.call_args_list],
                [('seat.identify', {'deadline': deadline}),
                 ('session.sign-in', {'deadline': deadline})],
            )
