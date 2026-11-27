"""Foreground serving, frame budgets and lifecycle on per-test scratch stores."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
import fcntl
import itertools
import json
import os
import select
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from relay_engine import api_compose, api_definitions as d, api_keys, api_platform
from relay_engine import daemon
from relay_engine import api_protocol as p, api_writer, jcs, lifecycle_paths, store_layout, store_steps, version
from relay_engine.tests.test_api_sessions import replace_occupant_for_test
from relay_engine.tests.test_submit import draft

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


@dataclass(frozen=True)
class AdmissionFixture:
    idle: store_layout.IncumbentRecord
    proof: store_layout._RetainedIncumbentDisposition

    @property
    def receipt(self):
        return self.idle, self.proof


def admitted_store_fixture(root, project_id, *, project_name, seed=None):
    store_id = str(uuid4())
    guard, birth = store_layout.IncumbentGuard._create_new(root, store_id, project_id)
    with guard:
        idle, _ = guard.enroll_new(birth)
        birth_id, boot = lifecycle_paths._native_identity(os.getpid())
        owner = store_layout._IncumbentOwner(str(uuid4()), os.getpid(), birth_id, boot, 'fixture', 'off')
        armed = guard.arm(idle, owner)
        guard._materialize_birth(armed, project_name=project_name, now_ms=1)
        if seed is not None:
            seed(root)
        proof = guard._retirement_proof(armed, b'disposable fixture Store handles closed')
        idle = guard.retire(armed, proof)
    return AdmissionFixture(idle, proof)


@contextmanager
def private_idle(admission, environment=None):
    reader, writer = os.pipe()
    try:
        data = store_layout._encode_idle_receipt(*admission.receipt)
        try:
            assert os.write(writer, data) == len(data)
        finally:
            os.close(writer)
        env = dict(os.environ if environment is None else environment, ADT_PRIVATE_IDLE_FD=str(reader))
        yield reader, env
    finally:
        os.close(reader)


def seed_service_store(root, keys, tick):
    conn = store_layout.open_ledger(root, P)
    try:
        conn.execute(
            "INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
            " VALUES ('team',?,'platform','pair',1,'operator')", (P,)).close()
        for role in ('planner', 'implementer'):
            conn.execute(
                "INSERT INTO seats(id,team_instance_id,role,state) VALUES (?,'team',?,'vacant')",
                (role, role)).close()
            key_id, secret = api_keys.issue_occupant_key(conn, role, next(tick))
            keys[role] = {'key_id': key_id, 'key_secret': secret}
        conn.commit()
    finally:
        conn.close()


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
        self.keys = {}
        self.tick = itertools.count(10000)
        self.admitted = admitted_store_fixture(
            self.root, P, project_name='service',
            seed=lambda root: seed_service_store(root, self.keys, self.tick))
        self.service = self.m.ApiService(str(self.root), P, clock=lambda: next(self.tick),
                                         admission=self.admitted.receipt)
        self.addCleanup(self.service.stop)

    def test_start_without_verified_idle_opens_no_store(self):
        service = self.m.ApiService(str(self.root), P, clock=lambda: 1)
        with patch.object(store_layout.sqlite3, 'connect') as opens:
            with self.assertRaises(store_layout.LayoutFault):
                service.start()
            opens.assert_not_called()
            self.assertIsNone(service.ready_path)

    @contextmanager
    def _sqlite_trace(self, phase):
        original = store_layout.sqlite3.connect
        observed = []

        def connect(database, *args, **kwargs):
            observed.append((phase(), database, kwargs.get('uri', False)))
            return original(database, *args, **kwargs)

        with patch.object(store_layout.sqlite3, 'connect', connect):
            yield observed

    def _assert_protected_trace(self, observed, expected):
        actual = [entry for entry in observed if entry[1] != ':memory:']
        self.assertEqual(actual, [(phase, path.resolve().as_uri() + '?mode=ro&immutable=1', True)
                                  for phase, path in expected])

    def _assert_no_publication(self, service, root):
        self.assertIsNone(service.ready_path)
        self.assertIsNone(service._retired_admission)
        self.assertFalse((root / lifecycle_paths._LIVE_LOCATOR).exists())

    def _assert_armed(self, service, root, admission):
        self._assert_no_publication(service, root)
        with store_layout.IncumbentGuard.acquire(root, expected_lock=admission.idle.lock) as guard:
            armed = guard.read()
            self.assertEqual(armed, service._owned_arm)
            self.assertEqual((armed.phase, armed.root, armed.lock),
                             ('armed', admission.idle.root, admission.idle.lock))

    @staticmethod
    def _replace_member(path):
        replacement = path.with_name('replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)

    def test_startup_path_and_proof_refusals_precede_sqlite(self):
        root = self.root.resolve()
        record = root / store_layout._INCUMBENT
        idle = record.read_bytes()
        cases = (
            (root, None, 'startup-unverified'),
            (root, str(self.base / 'missing-startup-socket'), 'startup_peer'),
            (root.with_name(root.name.upper()), None, 'startup-unverified'),
            (root.with_name('canonical-leaf'), None, 'startup-unverified'),
        )
        for canonical, startup_socket, reason in cases:
            with self.subTest(canonical=canonical, startup_socket=startup_socket):
                visible = Path.lstat

                def stat_canonical(path):
                    return visible(root if path == canonical else path)

                service = self.m.ApiService(str(root), P, clock=lambda: 1,
                                            admission=self.admitted.receipt, startup_socket=startup_socket)
                self.addCleanup(service.stop)
                with (patch.object(Path, 'lstat', stat_canonical),
                      patch.object(store_layout, 'production_root', return_value=canonical),
                      patch.object(store_layout.sqlite3, 'connect') as opens):
                    with self.assertRaisesRegex(store_layout.LayoutFault, reason):
                        service.start()
                    opens.assert_not_called()
                self.assertEqual(record.read_bytes(), idle)
                self._assert_no_publication(service, root)

    def test_production_symlink_refuses_before_startup_and_sqlite(self):
        root = self.root.resolve()
        alias = root.with_name('production-link')
        alias.symlink_to(root, target_is_directory=True)
        record = root / store_layout._INCUMBENT
        idle = record.read_bytes()
        from relay_engine import owner_start

        service = self.m.ApiService(str(alias), P, clock=lambda: 1, admission=self.admitted.receipt,
                                    startup_socket=str(self.base / 'synthetic-startup-socket'))
        self.addCleanup(service.stop)
        with (patch.object(store_layout, 'production_root', return_value=root),
              patch.object(owner_start, 'verify_service_start') as startup,
              patch.object(store_layout.sqlite3, 'connect') as opens):
            with self.assertRaisesRegex(store_layout.LayoutFault, 'startup-unverified'):
                service.start()
            startup.assert_not_called()
            opens.assert_not_called()
        self.assertEqual(record.read_bytes(), idle)
        self._assert_no_publication(service, root)

    def test_moved_production_kind_refuses_at_immutable_preflight(self):
        record = self.root / store_layout._INCUMBENT
        idle = record.read_bytes()
        path = store_layout.catalogue_path(self.root)
        with closing(store_layout.sqlite3.connect(path)) as db, db:
            db.execute("UPDATE catalogue_identity SET kind='production' WHERE id=1").close()
        with self._sqlite_trace(lambda: 'armed' if self.service._owned_arm else 'preflight') as trace:
            with self.assertRaisesRegex(store_layout.LayoutFault, 'wrong-kind'):
                self.service.start()
        self._assert_protected_trace(trace, [('preflight', path)])
        self.assertEqual(record.read_bytes(), idle)
        self._assert_no_publication(self.service, self.root)

    def test_wrong_store_or_selected_member_fences_before_writable_setup(self):
        for case in ('store', 'route', 'ledger'):
            with self.subTest(substitution=case):
                root = self.base / f'identity-{case}'
                admission = admitted_store_fixture(root, P, project_name='admitted')
                other_root = self.base / f'other-{case}'
                other_project = str(uuid4()) if case == 'ledger' else P
                admitted_store_fixture(other_root, other_project, project_name='other')
                catalogue_path = store_layout.catalogue_path(root)
                ledger_path = store_layout.ledger_path(root, P)
                if case == 'store':
                    os.replace(store_layout.catalogue_path(other_root), catalogue_path)
                elif case == 'route':
                    with closing(store_layout.open_catalogue(root)) as catalog, catalog:
                        catalog.execute('DELETE FROM project_routes WHERE project_id=?', (P,)).close()
                else:
                    os.replace(store_layout.ledger_path(other_root, other_project), ledger_path)
                service = self.m.ApiService(str(root), P, clock=lambda: 1, admission=admission.receipt)
                self.addCleanup(service.stop)
                with self._sqlite_trace(lambda: 'armed' if service._owned_arm else 'preflight') as trace:
                    with self.assertRaises(store_layout.LayoutFault):
                        service.start()
                self._assert_protected_trace(trace, [
                    ('preflight', catalogue_path), ('armed', catalogue_path), ('armed', ledger_path)])
                self._assert_armed(service, root, admission)

    def test_member_inode_substitution_after_protected_read_fences_writer(self):
        ledger = store_layout.ledger_path(self.root, P)
        obtain = store_layout.IncumbentGuard._admit_members

        def substitute(guard, arm):
            members = obtain(guard, arm)
            self._replace_member(ledger)
            return members

        with (patch.object(store_layout.IncumbentGuard, '_admit_members', substitute),
              self._sqlite_trace(lambda: 'armed' if self.service._owned_arm else 'preflight') as trace):
            with self.assertRaisesRegex(store_layout.LayoutFault, 'member-changed'):
                self.service.start()
        path = store_layout.catalogue_path(self.root)
        self._assert_protected_trace(trace, [('preflight', path), ('armed', path), ('armed', ledger)])
        self._assert_armed(self.service, self.root, self.admitted)

    def test_member_substitution_between_preflight_and_arm_preserves_idle(self):
        catalogue_path = store_layout.catalogue_path(self.root)
        record = self.root / store_layout._INCUMBENT
        idle = record.read_bytes()
        create_runtime = self.service._create_runtime

        def substitute(store):
            create_runtime(store)
            self._replace_member(catalogue_path)

        with (patch.object(self.service, '_create_runtime', substitute),
              self._sqlite_trace(lambda: 'armed' if self.service._owned_arm else 'preflight') as trace):
            with self.assertRaisesRegex(store_layout.LayoutFault, 'member-changed'):
                self.service.start()
        self._assert_protected_trace(trace, [('preflight', catalogue_path)])
        self.assertEqual(record.read_bytes(), idle)
        self._assert_no_publication(self.service, self.root)

    def test_member_substitution_after_arm_preserves_fence(self):
        catalogue_path = store_layout.catalogue_path(self.root)
        ledger = store_layout.ledger_path(self.root, P)
        arm = store_layout.IncumbentGuard.arm

        def substitute(guard, idle, owner):
            armed = arm(guard, idle, owner)
            self._replace_member(catalogue_path)
            return armed

        with (patch.object(store_layout.IncumbentGuard, 'arm', substitute),
              self._sqlite_trace(lambda: 'armed' if self.service._owned_arm else 'preflight') as trace):
            with self.assertRaisesRegex(store_layout.LayoutFault, 'member-changed'):
                self.service.start()
        self._assert_protected_trace(trace, [
            ('preflight', catalogue_path), ('armed', catalogue_path), ('armed', ledger)])
        self._assert_armed(self.service, self.root, self.admitted)

    def test_stop_before_first_open_keeps_sqlite_unopened(self):
        for port in (None, 0):
            with self.subTest(bridge=port):
                root = self.base / f'early-{port}'
                admission = admitted_store_fixture(root, P, project_name='service')
                service = self.m.ApiService(str(root), P, clock=lambda: 1, bridge_port=port,
                                             admission=admission.receipt, notifier_mode='off')
                service.arm_foreground()
                self.addCleanup(service.stop)
                arm = store_layout.IncumbentGuard.arm

                def arm_then_stop(guard, idle, owner):
                    record = arm(guard, idle, owner)
                    self.assertEqual(guard.read(), record)
                    service._foreground.enter_stop()
                    return record

                with (patch.object(store_layout.IncumbentGuard, 'arm', arm_then_stop),
                      self._sqlite_trace(lambda: 'after-stop' if service.is_stopping() else 'preflight') as trace):
                    try:
                        self.assertIsNone(service.start())
                        self.assertFalse(any(phase == 'after-stop' for phase, *_ in trace))
                        path = store_layout.catalogue_path(root)
                        self._assert_protected_trace(trace, [('preflight', path)])
                        self._assert_no_publication(service, root)
                    finally:
                        with self._sqlite_trace(lambda: 'retirement') as retirement:
                            self.assertEqual(service.stop(), 0)
                self._assert_protected_trace(retirement, [
                    ('retirement', path), ('retirement', store_layout.ledger_path(root, P))])
                with store_layout.IncumbentGuard.acquire(root, expected_lock=admission.idle.lock) as guard:
                    guard.verify_idle(*service._retired_admission)

    def test_stop_before_kind_preflight_opens_no_sqlite(self):
        guard_verify = store_layout.IncumbentGuard.verify_idle

        def stop_after_receipt(guard, idle, proof):
            result = guard_verify(guard, idle, proof)
            self.service._foreground.enter_stop()
            return result

        self._check_preflight_stop(store_layout.IncumbentGuard, 'verify_idle', stop_after_receipt, ())

    def test_stop_during_kind_preflight_refuses_arm(self):
        open_protected = store_layout._immutable_incumbent_read

        def stop_during_connection(path):
            result = open_protected(path)
            self.service._foreground.enter_stop()
            return result

        path = store_layout.catalogue_path(self.root)
        self._check_preflight_stop(store_layout, '_immutable_incumbent_read', stop_during_connection,
                                   [('preflight', path)])

    def _check_preflight_stop(self, target, name, replacement, expected):
        record = self.root / store_layout._INCUMBENT
        idle = record.read_bytes()
        self.service.arm_foreground()
        with (patch.object(target, name, replacement),
              self._sqlite_trace(lambda: 'after-stop' if self.service.is_stopping() else 'preflight') as trace):
            try:
                self.assertIsNone(self.service.start())
                self.assertFalse(any(phase == 'after-stop' for phase, *_ in trace))
                self._assert_protected_trace(trace, expected)
                self._assert_no_publication(self.service, self.root)
            finally:
                self.assertEqual(self.service.stop(), 0)
        self.assertEqual(record.read_bytes(), idle)

    def test_parked_kind_preflight_honors_hard_stop(self):
        self._check_incomplete_startup_stop('preflight', None)

    def test_early_stop_member_substitution_preserves_fence(self):
        for port in (None, 0):
            with self.subTest(bridge=port):
                self._check_incomplete_startup_stop('early-substitution', port)

    def test_unresolved_armed_admission_stop_preserves_fence(self):
        for port in (None, 0):
            with self.subTest(bridge=port):
                self._check_incomplete_startup_stop('unresolved', port)

    def _check_incomplete_startup_stop(self, phase, port):
        root = self.base / f'{phase}-{port}'
        admission = admitted_store_fixture(root, P, project_name='service')
        tools = Path(__file__).resolve().parents[2]
        reader, writer = os.pipe()
        script = f"""
import os, threading
from pathlib import Path
from relay_engine import api_service, store_layout

root = Path({str(root)!r})
phase = {phase!r}
record = root / store_layout._INCUMBENT
arm = store_layout.IncumbentGuard.arm
connect = store_layout.sqlite3.connect
open_protected = store_layout._immutable_incumbent_read

def trace(database, *args, **kwargs):
    if database != ':memory:' and not database.endswith('?mode=ro&immutable=1'):
        print('unexpected ordinary SQLite', flush=True)
    return connect(database, *args, **kwargs)

def arm_cut(guard, idle, owner):
    armed = arm(guard, idle, owner)
    if phase == 'early-substitution':
        catalogue = store_layout.catalogue_path(root)
        replacement = catalogue.with_name('replacement')
        replacement.write_bytes(catalogue.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(catalogue)
        print(record.read_bytes().hex(), flush=True)
        service._foreground.enter_stop()
    return armed

def park(guard, armed):
    assert service._admission_unresolved
    print(record.read_bytes().hex(), flush=True)
    threading.Event().wait()

def park_preflight(path):
    with open_protected(path):
        print(record.read_bytes().hex(), flush=True)
        threading.Event().wait()

store_layout.sqlite3.connect = trace
store_layout.IncumbentGuard.arm = arm_cut
if phase == 'unresolved':
    store_layout.IncumbentGuard._admit_members = park
if phase == 'preflight':
    store_layout._immutable_incumbent_read = park_preflight
service = api_service.ApiService(str(root), {P!r}, clock=lambda: 1, bridge_port={port!r},
                                 idle_fd=int(os.environ.pop('ADT_PRIVATE_IDLE_FD')),
                                 retirement_fd={writer}, notifier_mode='off')
service.arm_foreground()
assert service.start() is None
raise SystemExit(service.stop())
"""
        try:
            with private_idle(admission) as (fd, env), subprocess.Popen(
                [sys.executable, '-c', script], pass_fds=(fd, writer),
                env=dict(env, PYTHONPATH=str(tools)), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as proc:
                os.close(writer)
                writer = None
                try:
                    self.assertTrue(select.select([proc.stdout], [], [], 3)[0])
                    held_record = bytes.fromhex(proc.stdout.readline().decode('ascii'))
                    self.assertTrue(held_record)
                    started = time.monotonic()
                    if phase != 'early-substitution':
                        proc.terminate()
                    output, error = proc.communicate(timeout=5)
                    self.assertEqual((proc.returncode, output), (75, b''), error)
                    self.assertLess(time.monotonic() - started, 5)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=5)
            self.assertEqual(os.read(reader, 1), b'')
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)
        self.assertEqual((root / store_layout._INCUMBENT).read_bytes(), held_record)
        self.assertFalse((root / lifecycle_paths._LIVE_LOCATOR).exists())
        with store_layout.IncumbentGuard.acquire(root, expected_lock=admission.idle.lock) as guard:
            self.assertEqual(guard.read().phase, 'idle' if phase == 'preflight' else 'armed')

    def test_receipt_transport_refuses_short_oversized_and_expired_before_open(self):
        for case in ('short', 'oversized', 'expired'):
            with self.subTest(receipt=case):
                reader, writer = os.pipe()
                def supply():
                    with os.fdopen(writer, 'wb') as pipe:
                        if case == 'expired':
                            time.sleep(2.2)
                        else:
                            size = 1 if case == 'short' else store_layout._IDLE_RECEIPT_LIMIT + 1
                            pipe.write(b'{' * size)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    producer = pool.submit(supply)
                    service = self.m.ApiService(str(self.root), P, clock=lambda: 1, idle_fd=reader)
                    with patch.object(store_layout.sqlite3, 'connect') as opens:
                        reason = 'idle-receipt-expired' if case == 'expired' else 'idle-receipt-invalid'
                        with self.assertRaisesRegex(store_layout.LayoutFault, reason):
                            service.start()
                        opens.assert_not_called()
                    producer.result(timeout=3)
                self.assertIsNone(service.ready_path)
                self.assertFalse((self.root / lifecycle_paths._LIVE_LOCATOR).exists())
                print(json.dumps({'receipt_transport': case, 'refusal': reason, 'ordinary_opens': 0}))

    def test_stop_while_receipt_intake_parked(self):
        tools = Path(__file__).resolve().parents[2]
        reader, writer = os.pipe()
        script = f"""
import sys, threading
from relay_engine import api_service, store_layout
def park(fd, **kwargs):
    print('intake', flush=True)
    threading.Event().wait()
def opening(*args, **kwargs):
    print('unexpected SQLite', flush=True)
api_service._read_idle_receipt = park
store_layout.sqlite3.connect = opening
service = api_service.ApiService({str(self.root)!r}, {P!r}, clock=lambda: 1, idle_fd={reader})
service.arm_foreground()
service.start()
"""
        try:
            with subprocess.Popen([sys.executable, '-c', script], pass_fds=(reader,),
                                  env=dict(os.environ, PYTHONPATH=str(tools)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                self.assertTrue(select.select([proc.stdout], [], [], 3)[0])
                self.assertEqual(proc.stdout.readline().strip(), b'intake')
                started = time.monotonic()
                proc.terminate()
                output, diagnostic = proc.communicate(timeout=5)
                self.assertEqual((proc.returncode, output), (75, b''), diagnostic)
                self.assertLess(time.monotonic() - started, 5)
                self.assertFalse((self.root / lifecycle_paths._LIVE_LOCATOR).exists())
        finally:
            os.close(reader)
            os.close(writer)

    def test_stop_after_admitted_open_before_ready_withdraws_publication(self):
        for port in (None, 0):
            with self.subTest(bridge=port):
                root = self.base / f'late-{port}'
                admission = admitted_store_fixture(root, P, project_name='service')
                service = self.m.ApiService(str(root), P, clock=lambda: 1, bridge_port=port,
                                             admission=admission.receipt, notifier_mode='off')
                service.arm_foreground()
                self.addCleanup(service.stop)
                stopping = service.is_stopping
                triggered = False

                def stop_after_last_sample():
                    nonlocal triggered
                    observed = stopping()
                    if not triggered and 'ready.pending' in service._runtime_files:
                        triggered = True
                        self.assertTrue((service.runtime_dir / 'ready.pending').exists())
                        self.assertFalse(Path(service.ready_path).exists())
                        service._foreground.enter_stop()
                    return observed

                publish = os.replace if port is None else os.link
                with (patch.object(service, 'is_stopping', side_effect=stop_after_last_sample),
                      patch.object(os, publish.__name__, wraps=publish) as published,
                      patch.object(service, '_publish_locator', wraps=service._publish_locator) as locator,
                      patch.object(store_layout, 'open_ledger_runtime', wraps=store_layout.open_ledger_runtime) as opens):
                    self.assertIsNone(service.start())
                    opens.assert_called_once()
                    self.assertTrue(triggered)
                    self.assertFalse(any(Path(call.args[1]).name == 'ready.json'
                                         for call in published.call_args_list))
                    locator.assert_not_called()
                self.assertFalse((root / lifecycle_paths._LIVE_LOCATOR).exists())
                self.assertEqual(service.stop(), 0)
                self.assertFalse(Path(service.ready_path).exists())
                self.assertEqual(service._retired_admission[0].phase, 'idle')

    def test_direct_stop_fences_a_substituted_runtime(self):
        ready = Path(self.service.start())
        runtime = ready.parent
        moved = runtime.with_name('moved')
        runtime.rename(moved)
        runtime.mkdir(mode=0o700)
        replacement = runtime / 'ready.json'
        replacement.write_bytes(b'replacement')
        try:
            with self.assertRaises(OSError):
                self.service.stop()
            self.assertEqual(replacement.read_bytes(), b'replacement')
            self.assertTrue((moved / 'ready.json').exists())
            self.assertEqual(self.service._guard.read().phase, 'armed')
            self.assertIsNone(self.service._retired_admission)
        finally:
            replacement.unlink()
            runtime.rmdir()
            moved.rename(runtime)

    def test_console_rejects_wrong_live_protocol_instance_without_sqlite(self):
        from dataclasses import replace

        self.start()
        with patch.object(store_layout.sqlite3, 'connect') as opens:
            self.assertIsInstance(lifecycle_paths.console_target(str(self.root)), lifecycle_paths.ConsoleTarget)
            with patch.object(self.service, 'runtime', replace(self.service.runtime, runtime_instance_id=uuid4().hex)):
                self.assertIsInstance(lifecycle_paths.console_target(str(self.root)),
                                      lifecycle_paths.ConsoleTargetUnavailable)
            opens.assert_not_called()

    def test_console_ordinary_locator_rejects_wrong_epoch_without_sqlite(self):
        from relay_engine import lifecycle_paths
        self.start()
        target = lifecycle_paths.console_target(str(self.root))
        self.assertIsInstance(target, lifecycle_paths.ConsoleTarget)
        locator = self.root / lifecycle_paths._LIVE_LOCATOR
        original = locator.read_bytes()
        stale = json.loads(original)
        stale['instance'] = uuid4().hex
        locator.write_bytes(store_layout._canonical_frame(stale))
        self.addCleanup(locator.write_bytes, original)
        with patch.object(store_layout.sqlite3, 'connect') as opens:
            observed = lifecycle_paths.console_target(str(self.root))
        opens.assert_not_called()
        self.assertIsInstance(observed, lifecycle_paths.ConsoleTargetUnavailable)

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

    def sign_in(self, conn, role='planner', *, keys=None):
        result = conn.call('session.sign-in', dict((self.keys if keys is None else keys)[role], host='codex',
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

    def test_foreground_stop_wins_parked_first_ready(self):
        self.service.bridge_port = 0
        self.service.arm_foreground()
        link = os.link

        def stop_before_ack(source, target, **kwargs):
            if Path(target).name == 'ready.json':
                os.kill(os.getpid(), signal.SIGTERM)
            link(source, target, **kwargs)

        with patch.object(os, 'link', side_effect=stop_before_ack):
            self.assertIsNone(self.service.start())
        ready = Path(self.service.ready_path)
        self.assertTrue(self.service.is_stopping())
        self.assertEqual(self.service.stop(), 0)
        self.assertFalse(ready.parent.exists())

    def test_foreground_watchdog_exits_with_original_work_parked(self):
        for phase in ('cleanup', 'report', 'accept', 'record-sync'):
            with self.subTest(phase=phase):
                self.check_hard_stop_with_parked_owner(phase)

    def test_foreground_watchdog_observes_signal_sent_to_another_thread(self):
        for name in ('adt-stop-observer', 'adt-store-fence', 'main'):
            with self.subTest(thread=name):
                try:
                    self.check_hard_stop_with_parked_owner('accept', deliver='thread:' + name)
                except subprocess.TimeoutExpired:
                    self.fail(f'child alive 5 s after SIGTERM to {name}')
                finally:
                    (self.base / 'accept').unlink(missing_ok=True)

    def test_losing_enter_stop_never_sees_claim_without_attempt(self):
        script = """
import threading
from relay_engine import api_service as m
entered = threading.Event()
release = threading.Event()
original = m._StopAttempt.__init__
results = []
def parked(self, now):
    if threading.current_thread().name == 'parked-constructor':
        entered.set()
        assert release.wait(2)
    original(self, now)
m._StopAttempt.__init__ = parked
epoch = m._ForegroundEpoch(lambda attempt: True)
first = threading.Thread(target=lambda: results.append(epoch.enter_stop()), name='parked-constructor')
first.start()
try:
    assert entered.wait(2)
    claim_without_attempt = epoch.admission_closed and epoch.attempt is None
    loser = epoch.enter_stop()
    assert loser is not None, 'loser returned None while admission was closed'
    assert not claim_without_attempt, 'admission closed before the attempt was published'
finally:
    release.set()
    first.join(2)
    epoch.finish()
assert results == [loser] and epoch.attempt is loser
"""
        tools = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, '-c', script],
                                env=dict(os.environ, PYTHONPATH=str(tools), PYTHONDONTWRITEBYTECODE='1'),
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def check_watchdog_child(self, script, *, case=None, expected=0, hard_stop=False):
        tools = Path(__file__).resolve().parents[2]
        env = dict(os.environ, PYTHONPATH=str(tools), PYTHONDONTWRITEBYTECODE='1')
        prelude = """
import os, signal, threading, time
from relay_engine import api_service as m
def service():
    return m.ApiService('/unused', 'test', clock=lambda: 1)
def wakeup_fd():
    current = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(current, warn_on_full_buffer=False)
    return current
def fd_open(fd):
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True
"""
        args = [] if case is None else [case]
        result = subprocess.run([sys.executable, '-c', prelude + script, *args],
                                capture_output=True, env=env, timeout=12)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        if hard_stop:
            sent = float(result.stdout.decode().strip())
            self.assertGreaterEqual(time.monotonic() - sent, 3.8)
            self.assertLess(time.monotonic() - sent, 5)

    def test_serve_arms_the_process_owner(self):
        import ast

        source = Path(__file__).resolve().parents[1] / 'adt_cli.py'
        tree = ast.parse(source.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'arm_foreground']
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(keyword.arg == 'process_owner' and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True for keyword in calls[0].keywords))

    def test_foreground_arm_registry(self):
        script = """
import sys
case = sys.argv[1]
reader_parked, reader_release, reader_drained = (threading.Event() for _ in range(3))
original_read = os.read
reader_calls = 0
def park_reader(fd, size):
    global reader_calls
    if threading.current_thread().name == 'adt-signal-reader':
        reader_calls += 1
        if reader_calls == 1:
            reader_parked.set()
            assert reader_release.wait(3)
        else:
            reader_drained.set()
    return original_read(fd, size)
if case == 'owner_after_finish':
    os.read = park_reader
a, b = service(), service()
old = signal.getsignal(signal.SIGTERM)
a.arm_foreground(process_owner=case != 'other_then_owner' and case != 'nested_non_owners')
if case == 'nested_non_owners':
    b.arm_foreground()
    assert signal.getsignal(signal.SIGTERM) == b._signal
    b.stop()
    assert signal.getsignal(signal.SIGTERM) == a._signal
    a.stop()
    assert signal.getsignal(signal.SIGTERM) == old
elif case == 'owner_after_finish':
    assert reader_parked.wait(1)
    inherited_fd = wakeup_fd()
    a.stop()
    b.arm_foreground(process_owner=True)
    assert b._foreground.signal_ingress == 'handler'
    assert b._foreground.signal_ingress_reason == 'reader_bound'
    assert wakeup_fd() == inherited_fd
    os.write(inherited_fd, bytes([signal.SIGTERM]))
    reader_release.set()
    assert reader_drained.wait(1) and not b._foreground._stopped.is_set()
    assert b._foreground.attempt is None
    b.stop()
else:
    handler, prior = signal.getsignal(signal.SIGTERM), wakeup_fd()
    try:
        b.arm_foreground(process_owner=case == 'other_then_owner')
    except RuntimeError as error:
        assert str(error) == 'foreground arm conflict: live_epoch'
    else:
        raise AssertionError('live arm was not refused')
    assert wakeup_fd() == prior and signal.getsignal(signal.SIGTERM) == handler
    assert b._foreground is None
    if case == 'mixed_signal':
        def deliver():
            sent = time.monotonic()
            signal.pthread_kill(a._foreground._observer.ident, signal.SIGTERM)
            print(sent, flush=True)
        threading.Thread(target=deliver, daemon=True).start()
        threading.Event().wait()
    a.stop()
    assert signal.getsignal(signal.SIGTERM) == old
"""
        for case in ('owner_then_other', 'other_then_owner', 'nested_non_owners', 'owner_after_finish',
                     'mixed_signal'):
            with self.subTest(case=case):
                mixed = case == 'mixed_signal'
                self.check_watchdog_child(script, case=case, expected=75 if mixed else 0, hard_stop=mixed)

    def test_process_owner_reader(self):
        script = """
import sys
case = sys.argv[1]
svc = service()
parked, release, read_again, reader_entered = (threading.Event() for _ in range(4))
read = os.read
reading = []
child_closes = []
if case == 'fork_hook_failure':
    import tempfile
    child_stderr = tempfile.TemporaryFile()
    close = os.close
    def fail_detach(*args, **kwargs):
        raise OSError('fixture failed signal wakeup detach')
    def child_close(fd):
        child_closes.append(fd)
        close(fd)
    def prepare_failed_child():
        os.dup2(child_stderr.fileno(), 2)
        signal.set_wakeup_fd = fail_detach
        os.close = child_close
    # after_in_child hooks run in registration order; inject the failure
    # in the child only, just before the real service hook.
    os.register_at_fork(after_in_child=prepare_failed_child)
def park_first_reader(fd, size):
    if threading.current_thread().name == 'adt-signal-reader':
        reading.append(fd)
        reader_entered.set()
        if len(reading) == 1 and case in ('finished_no_op', 'fds_open_after_finish'):
            parked.set()
            assert release.wait(3)
        if len(reading) > 1:
            read_again.set()
    return read(fd, size)
os.read = park_first_reader
if case == 'foreign':
    foreign_read, foreign_write = os.pipe()
    os.set_blocking(foreign_write, False)
    signal.set_wakeup_fd(foreign_write)
svc.arm_foreground(process_owner=True)
epoch = svc._foreground
sigfd = wakeup_fd()
if case == 'foreign':
    assert epoch.signal_ingress == 'handler' and epoch.signal_ingress_reason == 'foreign_wakeup_fd'
    assert sigfd == foreign_write
    sent = time.monotonic()
    signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
    print(sent, flush=True)
    threading.Event().wait()
assert epoch.signal_ingress == 'wakeup' and reader_entered.wait(1)
if case == 'classify':
    os.write(sigfd, bytes([signal.SIGUSR1]))
    assert read_again.wait(1) and epoch.attempt is None
    gate_read, gate_write = os.pipe()
    gate = m._NotifierGate(gate_write)
    assert epoch.install_notifier_gate(gate)
    signal.pthread_kill(epoch._observer.ident, signal.SIGTERM)
    assert epoch._stopped.wait(1) and epoch.attempt is not None
    ready, _, _ = m.select.select([gate_read], [], [], 1)
    assert ready and os.read(gate_read, 1) == b''
    os.close(gate_read)
    svc.stop()
elif case in ('fds_open_after_finish', 'finished_no_op'):
    assert parked.wait(1)
    assert epoch._wake_pipe.fileno() == epoch._write_fd
    fds = [epoch.read_fd, epoch._write_fd, reading[0], sigfd]
    inodes = [os.fstat(fd).st_ino for fd in fds]
    calls = []
    publish = epoch.publish_stop
    def record(now):
        if threading.current_thread().name == 'adt-signal-reader':
            calls.append(now)
        return publish(now)
    epoch.publish_stop = record
    os.write(sigfd, bytes([signal.SIGTERM]))
    svc.stop()
    assert [os.fstat(fd).st_ino for fd in fds] == inodes
    release.set()
    assert read_again.wait(1) and not calls
elif case == 'in_flight_after_check':
    publish = epoch.publish_stop
    errors = []
    threading.excepthook = errors.append
    def blocked_publish(now):
        if threading.current_thread().name == 'adt-signal-reader':
            parked.set()
            assert release.wait(3)
        return publish(now)
    epoch.publish_stop = blocked_publish
    os.write(sigfd, bytes([signal.SIGTERM]))
    assert parked.wait(1)
    svc.stop()
    release.set()
    assert read_again.wait(1) and not errors
    assert any(t.name == 'adt-signal-reader' and t.is_alive() for t in threading.enumerate())
else:
    if case == 'fork_foreign':
        foreign_read, foreign_write = os.pipe()
        os.set_blocking(foreign_write, False)
        signal.set_wakeup_fd(foreign_write)
    if case == 'fork_after_clean_finish':
        svc.stop()
        def sentinel(*args):
            pass
        signal.signal(signal.SIGTERM, sentinel)
    if case == 'fork_lock':
        locked = threading.Event()
        released_at = []
        def hold():
            with m._foreground_registry.lock:
                locked.set()
                time.sleep(0.5)
                released_at.append(time.monotonic())
        threading.Thread(target=hold, daemon=True).start()
        assert locked.wait(1)
    child = os.fork()
    if child == 0:
        signal.alarm(2)
        try:
            if case == 'fork_signal':
                os.kill(os.getpid(), signal.SIGTERM)
            elif case == 'fork_foreign':
                assert wakeup_fd() == foreign_write
                assert os.fstat(sigfd) and os.fstat(reading[0])
            elif case == 'fork_after_clean_finish':
                assert signal.getsignal(signal.SIGTERM) is sentinel
                assert wakeup_fd() == -1 and m._foreground_registry.owner is None
            elif case == 'fork_lock':
                assert m._foreground_registry.owner is None
                service().arm_foreground()
            elif case == 'fork_hook_failure':
                assert not child_closes and fd_open(sigfd) and fd_open(reading[0])
                assert getattr(m, '_fork_isolation_failed', False), 'failed detach not recorded'
            else:
                assert wakeup_fd() == -1
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    if case == 'fork_lock':
        assert released_at and time.monotonic() >= released_at[0], 'fork did not wait for the registry lock'
    status = os.waitpid(child, 0)[1]
    if case == 'fork_hook_failure':
        child_stderr.seek(0)
        output = child_stderr.read()
        child_stderr.close()
        assert not output, output
    assert status == 0
    if case == 'fork_signal':
        assert not epoch._stopped.wait(1) and epoch.attempt is None
    if not epoch.finished:
        svc.stop()
"""
        for case in ('classify', 'foreign', 'fds_open_after_finish', 'finished_no_op', 'in_flight_after_check',
                     'fork_signal', 'fork_probe', 'fork_foreign', 'fork_after_clean_finish', 'fork_lock',
                     'fork_hook_failure'):
            with self.subTest(case=case):
                foreign = case == 'foreign'
                self.check_watchdog_child(script, case=case, expected=75 if foreign else 0, hard_stop=foreign)

    def test_foreground_arm_unwind(self):
        script = """
import sys
from unittest.mock import patch
case, _, mode = sys.argv[1].partition(':')
svc = service()
signal_pair = (signal.SIGTERM, signal.SIGINT)
handlers = [signal.getsignal(sig) for sig in signal_pair]
trace = []
trace_lock = threading.Lock()
started = {}
pipes = []
fences = []
def record_fence(attempt):
    fences.append(attempt)
    return True
svc._fence_writer = record_fence
pipe = os.pipe
set_blocking = os.set_blocking
close = os.close
join = threading.Thread.join
start = threading.Thread.start
start_worker = m._ForegroundEpoch._start_worker
sig = signal.signal
install = m.ApiService._install_signals
wakeup = signal.set_wakeup_fd
at_fork = os.register_at_fork
def capture_pipe():
    if case == 'reader_pipe' and pipes:
        raise OSError('reader pipe')
    fds = pipe()
    pipes.append(fds)
    return fds
def blocking(fd, block):
    if case == 'after_pipe' or (case == 'reader_blocking' and len(pipes) == 2):
        raise OSError('blocking setup')
    set_blocking(fd, block)
    if case == 'after_blocking':
        raise OSError('post-blocking setup')
def track_close(fd):
    with trace_lock:
        trace.append(('close', fd))
    return close(fd)
class RecordingFileIO(m.io.FileIO):
    def close(self):
        with trace_lock:
            trace.append(('close', self.fileno()))
        super().close()
def track_join(thread, *args, **kwargs):
    join(thread, *args, **kwargs)
    with trace_lock:
        trace.append(('join', thread, thread.is_alive()))
def worker(epoch, role, thread):
    if case == role + '_pre':
        raise RuntimeError('pre-start')
    start_worker(epoch, role, thread)
    started[role] = thread
    if case == role + '_post':
        raise RuntimeError('post-start')
def install_fault(self):
    if case == 'before_handlers':
        raise RuntimeError('before handlers')
    return install(self)
def second_handler_fault(signum, handler):
    if case == 'mid_handlers' and signum == signal.SIGINT and handler == svc._signal:
        raise RuntimeError('mid handlers')
    return sig(signum, handler)
def reader_start(thread):
    if thread.name == 'adt-signal-reader' and case in ('reader_start', 'unwind_incomplete'):
        raise RuntimeError('reader start')
    return start(thread)
def installed_wakeup(fd, **kwargs):
    if case == 'reader_wakeup' and fd >= 0 and len(pipes) == 2:
        raise OSError('fd install')
    if case == 'unwind_incomplete' and fd == -1:
        raise OSError('ownership probe')
    return wakeup(fd, **kwargs)
def fork_hook(**kwargs):
    if case == 'reader_fork':
        raise OSError('fork hook')
    return at_fork(**kwargs)
def assert_registry_clear():
    r = m._foreground_registry
    assert r.owner is None and not r.others

if case == 'reservation':
    other = service()
    other.arm_foreground()
try:
    with (patch.object(os, 'pipe', capture_pipe),
          patch.object(os, 'set_blocking', blocking),
          patch.object(os, 'close', track_close),
          patch.object(m.io, 'FileIO', RecordingFileIO),
          patch.object(os, 'register_at_fork', fork_hook),
          patch.object(threading.Thread, 'join', track_join),
          patch.object(threading.Thread, 'start', reader_start),
          patch.object(signal, 'signal', second_handler_fault),
          patch.object(signal, 'set_wakeup_fd', installed_wakeup),
          patch.object(m._ForegroundEpoch, '_start_worker', worker),
          patch.object(m.ApiService, '_install_signals', install_fault)):
        svc.arm_foreground(process_owner=True)
except (RuntimeError, OSError):
    assert case not in ('reader_pipe', 'reader_blocking', 'reader_wakeup', 'reader_fork',
                        'reader_start', 'unwind_incomplete')
    if case == 'reservation':
        other.stop()
    assert_registry_clear()
    assert svc._foreground is None and not fences
    assert [signal.getsignal(sig) for sig in signal_pair] == handlers
    for pair in pipes:
        assert not any(fd_open(fd) for fd in pair)
    assert not any(t.name in ('adt-store-fence', 'adt-stop-observer') for t in threading.enumerate())
    joiners = [entry[1] for entry in trace if entry[0] == 'join']
    assert set(joiners) == set(started.values())
    if pipes:
        indices = [i for i, event in enumerate(trace) if event[0] == 'close']
        write_fd = pipes[0][1]
        assert sum(trace[i][1] == write_fd for i in indices) == 1, trace
        assert len(indices) == 2 and set(trace[i][1] for i in indices) == set(pipes[0])
        assert all(i < indices[0] and not entry[2] for i, entry in enumerate(trace) if entry[0] == 'join')
else:
    reasons = dict(reader_pipe='pipe_failed', reader_blocking='pipe_failed', reader_wakeup='wakeup_failed',
                   reader_fork='fork_hook_failed', reader_start='reader_failed',
                   unwind_incomplete='unwind_incomplete')
    epoch = svc._foreground
    assert m._foreground_registry.owner is epoch
    assert all(signal.getsignal(sig) == svc._signal for sig in signal_pair)
    assert epoch.signal_ingress == 'handler' and epoch.signal_ingress_reason == reasons[case]
    assert not any(t.name == 'adt-signal-reader' for t in threading.enumerate())
    if case == 'unwind_incomplete':
        assert all(fd_open(fd) for fd in pipes[1])
        assert wakeup_fd() == pipes[1][1]
    else:
        assert wakeup_fd() == -1
        if len(pipes) == 2:
            assert not any(fd_open(fd) for fd in pipes[1])
    # Independently exercise bounded hard stop and ordinary stop cleanup.
    sent = time.monotonic()
    signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
    assert epoch.attempt is not None
    if mode == 'hard':
        print(sent, flush=True)
        threading.Event().wait()
    svc.stop()
    assert_registry_clear()
    assert [signal.getsignal(sig) for sig in signal_pair] == handlers
    assert wakeup_fd() == (pipes[1][1] if case == 'unwind_incomplete' else -1)
"""
        cases = ('reservation', 'after_pipe', 'after_blocking', 'observer_pre', 'observer_post', 'fencer_pre',
                 'fencer_post', 'before_handlers', 'mid_handlers', 'reader_pipe', 'reader_blocking',
                 'reader_wakeup', 'reader_fork', 'reader_start', 'unwind_incomplete')
        for case in cases:
            with self.subTest(case=case):
                self.check_watchdog_child(script, case=case)
                if case.startswith('reader_') or case == 'unwind_incomplete':
                    self.check_watchdog_child(script, case=case + ':hard', expected=75, hard_stop=True)

    def test_foreground_stale_handler_and_cli_cleanup(self):
        self.check_watchdog_child("""
from relay_engine import adt_cli
from unittest.mock import patch
old = signal.getsignal(signal.SIGTERM)
svc = service()
svc.arm_foreground()
stop = threading.Thread(target=svc.stop)
stop.start()
stop.join(2)
assert not stop.is_alive() and svc._foreground is None and svc._signals_orphaned
assert signal.getsignal(signal.SIGTERM) == svc._signal
def refuse_stale():
    target = service()
    try:
        target.arm_foreground(process_owner=True)
    except RuntimeError as error:
        assert str(error) == 'foreground arm conflict: stale_handler'
    else:
        raise AssertionError('stale handler was accepted')
    assert signal.getsignal(signal.SIGTERM) == svc._signal and wakeup_fd() == -1
refuse_stale()
svc._restore_signals()
assert not svc._signals_orphaned and signal.getsignal(signal.SIGTERM) == old

blocker = service()
blocker.arm_foreground()
idle, retired = os.pipe()
with patch.object(adt_cli, '_private_store_channels', return_value=(idle, retired)):
    errors = []
    with patch.object(adt_cli.strings, 'report_diagnostic', side_effect=errors.append):
        rc = adt_cli.main(['engine', 'serve', '--store-root', '/unused', '--project-id', 'test'])
    assert rc == 1 and len(errors) == 1
    assert str(errors[0]) == 'foreground arm conflict: live_epoch'
assert not fd_open(idle) and not fd_open(retired)
blocker.stop()
target = service()
target.arm_foreground(process_owner=True)
target.stop()
""")

    def test_saturated_signal_pipe_still_stops(self):
        self.check_hard_stop_with_parked_owner('accept', deliver='thread:saturated')

    def check_hard_stop_with_parked_owner(self, phase, *, deliver='terminate'):
        root = self.base / ('guard-' + phase + ('' if deliver == 'terminate' else '-' + deliver[7:]))
        admission = admitted_store_fixture(root, P, project_name='service')
        tools = Path(__file__).resolve().parents[2]
        # Keep the original process alive with the same actual flock, then park
        # ordinary cleanup. The observer must own the native exit independently.
        script = f"""
import os, threading
from pathlib import Path
from relay_engine.api_service import ApiService
from relay_engine import store_layout

marker = Path({str(self.base)!r}) / {phase!r}

def park(*_args):
    marker.write_text("parked")
    threading.Event().wait()

service = ApiService({str(root)!r}, {P!r}, clock=lambda: 1, stop_report=park,
                     idle_fd=int(os.environ.pop('ADT_PRIVATE_IDLE_FD')))
service.arm_foreground(process_owner=True)
service.start()
if {phase!r} == 'record-sync':
    original_replace = store_layout.IncumbentGuard._replace
    original_fsync = os.fsync

    def park_first_idle_record_sync(guard, expected, record):
        if record.phase == 'idle':
            def parked_sync(fd):
                assert os.fstat(fd).st_size > 0
                park()
            os.fsync = parked_sync
        try:
            return original_replace(guard, expected, record)
        finally:
            os.fsync = original_fsync

    store_layout.IncumbentGuard._replace = park_first_idle_record_sync
if {phase!r} == 'accept':
    class ParkedAccept:
        listener = service._listener

        def accept(self):
            endpoint = self.listener.accept()
            park()
            return endpoint

    service._listener = ParkedAccept()
print(service.socket_path, flush=True)
service.serve_forever()
if {phase!r} == 'cleanup':
    service._stop_owned = park
elif {phase!r} != 'record-sync':
    def refuse(_attempt):
        raise OSError('fixture outstanding')
    service._stop_owned = refuse
service.stop()
"""
        if deliver != 'terminate':
            assert phase == 'accept' and deliver.startswith('thread:')
            target = deliver[7:]
            if target == 'saturated':
                setup = r"""
import signal
reader_parked = threading.Event()
reader_release = threading.Event()
signal_write_fd = None
real_set_fd = signal.set_wakeup_fd
real_read = os.read
def capture_fd(fd, **kwargs):
    global signal_write_fd
    if fd >= 0:
        signal_write_fd = fd
    return real_set_fd(fd, **kwargs)
def parked_read(fd, size):
    if threading.current_thread().name == 'adt-signal-reader':
        reader_parked.set()
        assert reader_release.wait(3)
    return real_read(fd, size)
signal.set_wakeup_fd = capture_fd
os.read = parked_read
"""
                script = script.replace('service.arm_foreground', setup + 'service.arm_foreground', 1)
            helper = f"""
import signal
import time
def deliver_signal():
    while not marker.exists():
        time.sleep(0.005)
    threads = {{item.name: item for item in threading.enumerate()}}
    if {target!r} == 'saturated':
        assert reader_parked.wait(2)
        chunk = bytes([signal.SIGTERM]) * 4096
        while True:
            try:
                os.write(signal_write_fd, chunk)
            except BlockingIOError:
                break
        while True:
            try:
                os.write(signal_write_fd, chunk[:1])
            except BlockingIOError:
                break
        signal.pthread_kill(threads['adt-stop-observer'].ident, signal.SIGTERM)
        try:
            os.write(signal_write_fd, chunk[:1])
        except BlockingIOError:
            pass
        else:
            raise AssertionError('signal pipe did not remain full')
        sent = time.monotonic()
        reader_release.set()
    else:
        thread = threading.main_thread() if {target!r} == 'main' else threads[{target!r}]
        sent = time.monotonic()
        signal.pthread_kill(thread.ident, signal.SIGTERM)
    print('sent', sent, flush=True)
threading.Thread(target=deliver_signal, daemon=True).start()
"""
            accept_setup = '    service._listener = ParkedAccept()\n'
            script = script.replace(accept_setup, accept_setup + helper, 1)
        environment = dict(os.environ, PYTHONPATH=str(tools), PYTHONDONTWRITEBYTECODE='1')
        lock = root / 'api-service.lock'
        with (private_idle(admission, environment) as (idle_fd, child_env),
              subprocess.Popen([sys.executable, '-c', script], env=child_env,
                               pass_fds=(idle_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child,
              socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer):
            try:
                self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                endpoint = child.stdout.readline().decode().strip()
                self.assertTrue(endpoint, child.stderr.read() if not endpoint else None)
                if phase == 'accept':
                    peer.connect(endpoint)
                    deadline = time.monotonic() + 2
                    while not (self.base / phase).exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                with lock.open('rb') as observer:
                    original_inode = os.fstat(observer.fileno()).st_ino
                    if deliver == 'terminate':
                        started = time.monotonic()
                        child.terminate()
                    else:
                        self.assertTrue(select.select([child.stdout], [], [], 3)[0], 'missing sent marker')
                        sent = child.stdout.readline().decode().split()
                        self.assertEqual(sent[:1], ['sent'])
                        started = float(sent[1])
                    deadline = started + 2
                    while not (self.base / phase).exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _, error = child.communicate(timeout=5)
                    elapsed = time.monotonic() - started
                    self.assertEqual(child.returncode, 75, error)
                    self.assertGreaterEqual(elapsed, 3.8)
                    self.assertLess(elapsed, 5)
                    self.assertEqual(original_inode, lock.stat().st_ino)
                    fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if phase == 'accept':
                        peer.settimeout(1)
                        self.assertEqual(peer.recv(1), b'')
                    if phase == 'record-sync':
                        record = json.loads((root / store_layout._INCUMBENT).read_bytes())
                        self.assertEqual(record['phase'], 'armed')
                    print(json.dumps({'parked': phase, 'hard_stop_seconds': round(elapsed, 3),
                                      'status': child.returncode, 'same_inode_kernel_return': True}))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)

    def test_repeated_gate_retirement_does_not_close_reused_fd(self):
        reader, writer = os.pipe()
        gate = self.m._NotifierGate(writer)
        replacement_read = replacement_write = None
        try:
            gate.retire()
            self.assertTrue(gate.released)
            self.assertEqual(os.read(reader, 1), b'')
            replacement_read, replacement_write = os.pipe()
            if writer not in {replacement_read, replacement_write}:
                os.dup2(replacement_write, writer)
            gate.retire()
            os.fstat(writer)
        finally:
            for fd in {reader, writer, replacement_read, replacement_write} - {None}:
                os.close(fd)

    def test_published_clock_reaps_foreground_with_gate_retirement_parked(self):
        tools = Path(__file__).resolve().parents[2]
        marker = self.base / 'retiring'
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        script = f"""
import threading
from pathlib import Path
from relay_engine.api_service import ApiService, _NotifierGate
class ParkedGate(_NotifierGate):
    def retire(self):
        Path({str(marker)!r}).touch()
        threading.Event().wait()
service = ApiService({str(self.root)!r}, {P!r}, clock=lambda: 1,
                     idle_fd=int(__import__('os').environ.pop('ADT_PRIVATE_IDLE_FD')))
service.arm_foreground()
service._foreground.install_notifier_gate(ParkedGate({writer}))
service.start()
print('ready', flush=True)
service.serve_forever()
"""
        env = dict(os.environ, PYTHONPATH=str(tools), PYTHONDONTWRITEBYTECODE='1')
        try:
            with private_idle(self.admitted, env) as (idle_fd, child_env):
                child = subprocess.Popen([sys.executable, '-c', script], env=child_env,
                                         pass_fds=(writer, idle_fd), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        finally:
            os.close(writer)
        with child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                self.assertEqual(child.stdout.readline(), b'ready\n')
                began = time.monotonic()
                child.terminate()
                while not marker.exists():
                    self.assertLess(time.monotonic() - began, 2)
                    time.sleep(0.01)
                with self.assertRaises(BlockingIOError):
                    os.read(reader, 1)
                with (self.root / 'api-service.lock').open('rb') as lock:
                    inode = os.fstat(lock.fileno()).st_ino
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _, error = child.communicate(timeout=5)
                    elapsed = time.monotonic() - began
                    self.assertEqual(child.returncode, 75, error)
                    self.assertLess(elapsed, 5)
                    self.assertEqual(os.read(reader, 1), b'')
                    self.assertEqual((self.root / 'api-service.lock').stat().st_ino, inode)
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print(json.dumps({'parked_gate_retirement': True, 'status': 75,
                                  'seconds': round(elapsed, 3), 'post_teardown_eof': True}))
            finally:
                os.close(reader)
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)

    def test_writer_admission_and_permit_while_original_timestamp_is_unpublished(self):
        service = self.service
        service.arm_foreground()
        self.assertIsNotNone(service.start())
        epoch = service._foreground
        parked, release = threading.Event(), threading.Event()
        waiting = threading.Event()
        original_publish = epoch.publish_stop

        def delay_publication(observed_at):
            waiting.set()
            self.assertTrue(release_publication.wait(3))
            return original_publish(observed_at)

        def pause(phase):
            if phase == 'finishing':
                parked.set()
                self.assertTrue(release.wait(3))

        release_publication = threading.Event()
        writer = service.writer
        writer._phase_hook = pause
        job = api_writer.DeclaredJob(store_steps.Declared('written', {}),
                                     lambda _: api_writer.JobOutcome(True, 'receipt'))
        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(writer.submit_declared, job)
            try:
                self.assertTrue(parked.wait(1))
                with patch.object(epoch, 'publish_stop', side_effect=delay_publication):
                    stopping = pool.submit(service._signal, signal.SIGTERM, None)
                    try:
                        self.assertTrue(waiting.wait(1))
                        self.assertIsNone(epoch.attempt)
                        self.assertFalse(writer._admission_closed())
                        second = pool.submit(writer.submit_declared, job)
                        deadline = time.monotonic() + 1
                        while True:
                            with writer._lock:
                                active = writer._active_request
                                queued = [request for request in writer._requests.values()
                                          if request is not active]
                                if queued:
                                    self.assertEqual(len(queued), 1)
                                    self.assertEqual(queued[0].kind, 'declared')
                                    self.assertFalse(queued[0].withdrawn)
                                    self.assertFalse(active.committing)
                                    break
                            self.assertLess(time.monotonic(), deadline, 'second request was not queued')
                            time.sleep(0.005)
                        with self.assertRaises(TimeoutError):
                            second.result(0.25)
                        self.assertIsNone(epoch.attempt)
                    finally:
                        release_publication.set()
                    stopping.result(1)
                self.assertTrue(writer._admission_closed())
                self.assertIsNotNone(epoch.attempt)
                with self.assertRaisesRegex(api_writer.WriterUnavailable, 'stopped'):
                    second.result(1)
                with self.assertRaisesRegex(api_writer.WriterUnavailable, 'stopped'):
                    writer.submit_declared(job)
                self.assertLess(time.monotonic(), epoch.attempt.active)
            finally:
                release.set()
            self.assertTrue(first.result(1).commit)
        self.assertEqual(service.stop(), 0)
        self.check_late_receipt_permit_uses_original_a()

    def check_late_receipt_permit_uses_original_a(self):
        from relay_engine.operator_queue import notifier

        root = self.base / 'late-permit'
        note = notifier.Notification('pending', 1, 'fixture', 'pending')

        def seed(path):
            with closing(store_layout.open_catalogue(path)) as catalogue, catalogue:
                catalogue.execute(
                    "INSERT INTO queue_items (item_id,kind,subkind,project_id,raiser,revision,state,"
                    "texts,pins,condition_key,raised_position,raised_at_ms) VALUES "
                    "('pending','notice','project_stopped',?,'fixture',1,'pending','{}','{}',"
                    "'fixture',1,1000)", (P,)).close()
                catalogue.execute("INSERT INTO queue_notifications (item_id,revision,state,next_attempt_at_ms) "
                                  "VALUES ('pending',1,'pending',1000)").close()
                catalogue.execute('INSERT INTO queue_notifier_health VALUES (1,2,7)').close()

        def snapshot(conn):
            with closing(conn.execute('SELECT * FROM queue_notifications')) as rows:
                queue = rows.fetchall()
            with closing(conn.execute('SELECT * FROM queue_notifier_health')) as health:
                return queue, health.fetchall()

        admission = admitted_store_fixture(root, P, project_name='late-permit', seed=seed)
        service = self.m.ApiService(str(root), P, clock=lambda: 20, admission=admission.receipt)
        service.arm_foreground()
        self.addCleanup(service.stop)
        service.start()
        writer, epoch = service.writer, service._foreground
        before = writer.read_catalogue(snapshot)
        parked, execute = threading.Event(), threading.Event()
        publishing, publish = threading.Event(), threading.Event()
        origin = []
        original = epoch.publish_stop

        def delayed(observed):
            origin.append(observed)
            publishing.set()
            if not publish.wait(3):
                raise AssertionError('fixture never resumed original publication')
            return original(observed)

        def stop_phase(phase):
            if phase == 'finishing':
                parked.set()
                if not execute.wait(3):
                    raise AssertionError('declared receipt not resumed')

        def delivered(bindings):
            notifier.notify_delivered(bindings['catalogue'], note, 20)
            return api_writer.JobOutcome(True, 'delivered')

        writer._phase_hook = stop_phase
        job = api_writer.DeclaredJob(store_steps.Declared('written', {}), delivered)
        with ThreadPoolExecutor(2) as pool:
            result = pool.submit(writer.submit_declared, job)
            try:
                self.assertTrue(parked.wait(1))
                with patch.object(epoch, 'publish_stop', side_effect=delayed):
                    stopping = pool.submit(service._signal, signal.SIGTERM, None)
                    try:
                        self.assertTrue(publishing.wait(1))
                        deadline = origin[0] + 1.55
                        while (remaining := deadline - time.monotonic()) > 0:
                            time.sleep(remaining)
                        self.assertIsNone(epoch.attempt)
                    finally:
                        publish.set()
                    stopping.result(1)
                self.assertIsNotNone(epoch.attempt)
                self.assertGreater(time.monotonic(), origin[0] + 1.5)
                execute.set()
                outcome = result.result(1)
                self.assertEqual((outcome.commit, outcome.fault, outcome.indeterminate),
                                 (False, 'stop_unwritten', False))
            finally:
                execute.set()
                publish.set()
        self.assertEqual(service.stop(), 0)
        with closing(store_layout._immutable_incumbent_read(store_layout.catalogue_path(root))) as conn:
            self.assertEqual(snapshot(conn), before)
        print(json.dumps({'late_original_a': True, 'fault': outcome.fault, 'queue_and_health_unchanged': True}))

    def test_declared_turn_stop_distinguishes_body_and_commit_permit(self):
        admission = self.admitted.receipt
        for parked_phase, expected_fault in (('finishing', 'stop_unwritten'),
                                              ('commit_permitted', 'stop_commit_unknown')):
            with self.subTest(phase=parked_phase):
                service = self.m.ApiService(str(self.root), P, clock=lambda: 1, admission=admission)
                service.arm_foreground()
                service.start()
                parked, release = threading.Event(), threading.Event()

                def pause(phase):
                    if phase == parked_phase:
                        parked.set()
                        release.wait(3)

                service.writer._phase_hook = pause
                original = 22 if parked_phase == 'finishing' else 33

                def body(connections):
                    connections['catalogue'].execute(
                        'UPDATE catalogue_identity SET created_at=?', (original,)).close()
                    return api_writer.JobOutcome(True, 'committed')

                job = api_writer.DeclaredJob(store_steps.Declared('written', {}), body)
                try:
                    with ThreadPoolExecutor(1) as pool:
                        turn = pool.submit(service.writer.submit_declared, job)
                        try:
                            self.assertTrue(parked.wait(1))
                            service._foreground.publish_stop(time.monotonic() - 1.5)
                            outcome = turn.result(1)
                            self.assertEqual(outcome.fault, expected_fault)
                            self.assertEqual(outcome.indeterminate, parked_phase == 'commit_permitted')
                        finally:
                            release.set()
                finally:
                    self.assertEqual(service.stop(), 0)
                    admission = service._retired_admission
                catalogue = store_layout.open_catalogue(self.root)
                try:
                    value, = catalogue.execute('SELECT created_at FROM catalogue_identity').fetchone()
                    self.assertEqual(value == original, parked_phase == 'commit_permitted')
                finally:
                    catalogue.close()

    def test_stop_retains_a_reader_parked_before_open(self):
        self.service.arm_foreground()
        self.assertIsNotNone(self.service.start())
        writer = self.service.writer
        parked, release = threading.Event(), threading.Event()
        original = store_layout.open_ledger_readonly
        used = []

        def park(*args):
            parked.set()
            release.wait(3)
            return original(*args)

        with patch.object(store_layout, 'open_ledger_readonly', side_effect=park):
            with ThreadPoolExecutor(1) as pool:
                reading = pool.submit(writer.read, P, used.append)
                try:
                    self.assertTrue(parked.wait(1))
                    attempt = self.service._foreground.publish_stop(time.monotonic())
                    self.assertIn(None, writer._readers.values())
                    self.assertTrue(self.service._foreground.join_fencer(attempt))
                    self.assertFalse(reading.done())
                finally:
                    release.set()
                with self.assertRaisesRegex(api_writer.WriterUnavailable, 'stopped'):
                    reading.result(1)
        self.assertEqual(used, [])
        self.assertEqual(self.service.stop(), 0)

    def test_admitted_callee_remains_original_owned_after_waiter_cut(self):
        self.service.arm_foreground()
        self.assertIsNotNone(self.service.start())
        writer = self.service.writer
        parked, release = threading.Event(), threading.Event()

        class Callee:
            files = frozenset({writer.ledger_file(P)})

            def run(self, conn):
                parked.set()
                release.wait(3)
                return api_writer.JobOutcome(True, 'late')

        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(writer.submit_callee, P, Callee())
            try:
                self.assertTrue(parked.wait(1))
                attempt = self.service._foreground.publish_stop(time.monotonic() - 1.5)
                outcome = running.result(1)
                self.assertTrue(outcome.indeterminate)
                self.assertEqual(outcome.fault, 'stop_commit_unknown')
                self.assertIsNotNone(writer._active_request)
                with self.assertRaisesRegex(api_writer.WriterUnavailable, 'stopped'):
                    writer.submit_callee(P, Callee())
                self.assertTrue(self.service._foreground.join_fencer(attempt))
            finally:
                release.set()
        self.assertEqual(self.service.stop(), 0)
        self.assertIsNone(writer._active_request)

    def test_start_writes_ready_file_after_listening(self):
        original = os.replace
        observed = []
        def replace_ready(source, target, **kwargs):
            if Path(target).name == 'ready.json':
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(json.loads(Path(source).read_bytes())['socket'])
                    observed.append(True)
                finally:
                    sock.close()
            return original(source, target, **kwargs)
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
        admission = self.admitted.receipt
        for mode in ('missing', 'unrouted'):
            with self.subTest(mode=mode):
                service = self.m.ApiService(str(self.root), P, clock=lambda: 1,
                    components=(replace(platform, port_factory=lambda context: None),), admission=admission)
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
                admission = service._retired_admission
                self.assertEqual(list(self.temp.rglob('*.sock')), [])

    @staticmethod
    def start_with_patch(service, compose):
        with patch.object(api_compose, 'compose_components', side_effect=compose):
            return service.start()

    @staticmethod
    def _remove_owned_legacy_socket(directory, identity):
        try:
            current = directory.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) != identity:
            return
        endpoint = directory / 's'
        try:
            if stat.S_ISSOCK(endpoint.lstat().st_mode):
                endpoint.unlink()
        except FileNotFoundError:
            pass
        if not any(directory.iterdir()):
            directory.rmdir()

    def _make_legacy_neighbour(self, other_root):
        endpoint = Path(daemon.socket_path(str(other_root), override=''))
        child = """import os
import sys
from pathlib import Path
from relay_engine import daemon
endpoint = Path(daemon.socket_path(sys.argv[1], override=''))
endpoint.parent.parent.mkdir(mode=0o700, exist_ok=True)
try:
    endpoint.parent.mkdir(mode=0o700)
except FileExistsError:
    print(f'conflict: {endpoint.parent}', flush=True)
    sys.exit(3)
owned = endpoint.parent.lstat()
print(f'owned {owned.st_dev} {owned.st_ino}', flush=True)
with daemon.prepare_socket(str(endpoint)):
    print(endpoint, flush=True)
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]),
                   PYTHONDONTWRITEBYTECODE='1')
        env.pop('RELAY_SOCKET', None)
        cmd = [sys.executable, '-c', child, str(other_root)]
        try:
            completed = subprocess.run(cmd, env=env, capture_output=True, timeout=10)
            status = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            status = 'timeout'
            stdout, stderr = error.stdout or b'', error.stderr or b''
        lines = stdout.decode(errors='replace').splitlines()
        identity = None
        if lines and lines[0].startswith('owned '):
            _, device, inode = lines[0].split()
            identity = (int(device), int(inode))
            self.addCleanup(self._remove_owned_legacy_socket, endpoint.parent, identity)
        details = f'neighbour {endpoint}: {status}; stdout={stdout!r}; stderr={stderr!r}'
        self.assertEqual(status, 0, details)
        self.assertIsNotNone(identity, details)
        self.assertEqual(lines[1:], [str(endpoint)], details)
        info = endpoint.parent.lstat()
        self.assertEqual((info.st_dev, info.st_ino), identity)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual((info.st_uid, stat.S_IMODE(info.st_mode)), (os.geteuid(), 0o700))
        self.assertTrue(stat.S_ISSOCK(endpoint.lstat().st_mode))

    def test_no_2_9_7_paths_touched(self):
        self._assert_no_2_9_7_paths(neighbour=False)

    def test_no_2_9_7_paths_touched_beside_a_neighbour_daemon(self):
        self._assert_no_2_9_7_paths(neighbour=True)

    def _assert_no_2_9_7_paths(self, *, neighbour):
        legacy = Path('/tmp/relay-engine')
        bases = (legacy, Path(os.path.realpath(legacy)))
        real_mkdir = os.mkdir
        real_bind = socket.socket.bind

        def deny_legacy(path):
            if not isinstance(path, (str, bytes, os.PathLike)):
                return
            name = os.fsdecode(path)
            if name.startswith('\0'):
                return
            absolute = Path(os.path.abspath(name))
            resolved = Path(os.path.realpath(absolute))
            if any(candidate.is_relative_to(base)
                   for candidate in (absolute, resolved) for base in bases):
                raise AssertionError(f'legacy socket path attempted: {absolute}')

        def mkdir(path, mode=0o777, *, dir_fd=None):
            deny_legacy(path)
            return real_mkdir(path, mode, dir_fd=dir_fd)

        def bind(sock, address):
            deny_legacy(address)
            return real_bind(sock, address)

        real_start = self.service.start

        def start():
            if neighbour:
                with tempfile.TemporaryDirectory() as root:
                    self._make_legacy_neighbour(Path(root).resolve())
            return real_start()

        class Environment(dict):
            def get(self, key, default=None):
                if key == 'RELAY_SOCKET':
                    raise AssertionError('legacy endpoint read')
                return super().get(key, default)
            def __getitem__(self, key):
                if key == 'RELAY_SOCKET':
                    raise AssertionError('legacy endpoint read')
                return super().__getitem__(key)
        with (patch.object(os, 'environ', Environment(os.environ)),
              patch.object(self.service, 'start', start),
              patch.object(os, 'mkdir', mkdir),
              patch.object(socket.socket, 'bind', bind)):
            self.start()
            self.service.stop()
        self.assertFalse((self.root / '.engine').exists())
        own_directory = Path(daemon.socket_path(self.service.store_root, override='')).parent
        self.assertFalse(os.path.lexists(own_directory),
                         f'legacy socket directory remains: {own_directory}')
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
        self.assertLess(order.index(ready.name), order.index(Path(data['socket']).name))

    def test_second_signal_during_drain_finishes_stop(self):
        import fcntl
        import subprocess
        import sys
        import time

        tools = Path(__file__).resolve().parents[2]
        for first in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(first=first.name):
                root = self.base / first.name
                admitted_store_fixture(root, P, project_name='service')
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
    def settle_private_io(self, deadline):
        self.calls += 1
        print('drain-entered', flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        if service._lock_fd is not None:
            print('lock-held', flush=True)
        return True
writer = Writer()
service.writer = writer
os.kill(os.getpid(), {int(first)})
assert writer.calls == 1, writer.calls
print('stopped', flush=True)
'''
                started = time.monotonic()
                timed_out = False
                try:
                    result = subprocess.run([sys.executable, '-I', '-B', '-c', script],
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
            def settle_private_io(owner, deadline):
                calls.append('close'); return owner.actual.settle_private_io(deadline)
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
                                        components=(replace(platform, port_factory=wrong_factory),),
                                        admission=self.service._retired_admission)
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
