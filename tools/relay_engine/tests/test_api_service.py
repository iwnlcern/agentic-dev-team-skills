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
                      patch.object(store_layout, 'open_ledger', wraps=store_layout.open_ledger) as opens):
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

    def check_hard_stop_with_parked_owner(self, phase):
        root = self.base / ('guard-' + phase)
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
service.arm_foreground()
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
                    started = time.monotonic()
                    child.terminate()
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
                        second = pool.submit(writer.submit_declared, job)
                        with self.assertRaisesRegex(api_writer.WriterUnavailable, 'stopped'):
                            second.result(0.25)
                        release.set()
                        with self.assertRaises(TimeoutError):
                            first.result(0.15)
                        with writer._lock:
                            self.assertFalse(writer._active_request.committing)
                    finally:
                        release_publication.set()
                    stopping.result(1)
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
                        execute.set()
                        remaining = origin[0] + 1.55 - time.monotonic()
                        self.assertGreater(remaining, 0)
                        with self.assertRaises(TimeoutError):
                            result.result(remaining)
                        self.assertIsNone(epoch.attempt)
                    finally:
                        publish.set()
                    stopping.result(1)
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
