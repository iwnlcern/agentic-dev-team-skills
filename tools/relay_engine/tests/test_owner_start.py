"""Exercise signed cold startup with a test signer and native process peers."""
import ctypes
from dataclasses import replace
import importlib
import inspect
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from relay_engine import operator_terminal


STORE = str(UUID(int=1))
PROJECT = str(UUID(int=2))
WINDOW = 120_000_000_000
ISSUED = 2**53 - 1
EXPIRES = ISSUED + WINDOW


def installed_key(path, _stat):
    return operator_terminal._installed_key(
        str(path), lambda item: type('Metadata', (), {
            'st_uid': 0, 'st_mode': 0o100644 if item == str(path) else 0o40755})()
    )


def synthetic_birth(selection, consume, *, incomplete=False):
    """Use the checked production-kind reservation with a synthetic selection."""
    from relay_engine import store_layout
    guard, enrollment = store_layout.IncumbentGuard._create_new(
        selection.root, selection.store, selection.project, kind='production',
        consume_production_birth=consume)
    with guard:
        idle, _ = guard.enroll_new(enrollment)
        from relay_engine import owner_start
        process = owner_start.NativeIdentity.current()
        owner = store_layout._IncumbentOwner(str(uuid4()), *process, str(selection.root.parent), 'off')
        armed = guard.arm(idle, owner)
        if incomplete:
            raise store_layout.LayoutFault('synthetic-birth-unsettled')
        guard._materialize_birth(armed, project_name=selection.project_name, now_ms=1)
        proof = guard._retirement_proof(armed, b'synthetic birth settled')
        idle = guard.retire(armed, proof)
        return store_layout._encode_idle_receipt(idle, proof)


def child_main(path, pub, root, clock, wrong_parent):
    from relay_engine import owner_start as startup

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(path)
            with (patch('relay_engine.store_layout.production_root', return_value=Path(root)),
                  patch.object(operator_terminal, '_installed_key', return_value=installed_key(pub, None)),
                  patch.object(startup, '_clock_ns', return_value=int(clock))):
                selection = startup.StartSelection.checked(
                    root, STORE, PROJECT, project_name='Example', initialize=True, bridge_port=0)
                if wrong_parent == '1':
                    with patch.object(startup.os, 'getppid', return_value=1):
                        startup.verify_child_start(channel, selection, deadline=time.monotonic() + 5)
                verified = startup.verify_child_start(channel, selection, deadline=time.monotonic() + 5)
            return 'accepted' if verified.selection == selection else 'wrong selection'
    except startup.StartupRefused as error:
        return error.reason


class OwnerStartupBoundaryTests(unittest.TestCase):
    BOOT = str(UUID(int=7))

    def test_darwin_boot_read_checks_length_and_call_failures(self):
        from relay_engine import lifecycle_paths, owner_start

        self.assertTrue(hasattr(lifecycle_paths, '_darwin_boot_session'))
        read_boot = lifecycle_paths._darwin_boot_session
        frame = self.BOOT.encode('ascii') + b'\0'
        cases = (
            ('ok', len(frame), len(frame), frame, None),
            ('size-error', len(frame), len(frame), frame, OSError),
            ('fill-error', len(frame), len(frame), frame, OSError),
            ('zero-size', 0, 0, b'', ValueError),
            ('large-size', 65, 0, b'', ValueError),
            ('zero-length', len(frame), 0, frame, ValueError),
            ('large-length', len(frame), len(frame) + 1, frame, ValueError),
            ('missing-nul', len(frame), len(frame), frame[:-1] + b'x', ValueError),
        )
        for cut, size, length, data, error_type in cases:
            with self.subTest(cut=cut):
                calls = []

                def sysctlbyname(name, buffer, size_pointer, new_value, new_size):
                    self.assertEqual(name, b'kern.bootsessionuuid')
                    self.assertIsNone(new_value)
                    self.assertEqual(new_size, 0)
                    length_pointer = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
                    calls.append(buffer is not None)
                    if buffer is None:
                        length_pointer[0] = size
                        return -1 if cut == 'size-error' else 0
                    self.assertEqual(length_pointer[0], size)
                    ctypes.memmove(buffer, data, len(data))
                    length_pointer[0] = length
                    return -1 if cut == 'fill-error' else 0

                if error_type is None:
                    self.assertEqual(read_boot(sysctlbyname), self.BOOT)
                    self.assertEqual(calls, [False, True])
                    continue

                def native_identity(_pid):
                    return '1', read_boot(sysctlbyname)

                with (patch.object(ctypes, 'get_errno', return_value=5),
                      patch.object(lifecycle_paths, '_native_identity', side_effect=native_identity)):
                    with self.assertRaisesRegex(owner_start.StartupRefused, 'startup_owner') as refused:
                        owner_start.NativeIdentity.current()
                self.assertIsInstance(refused.exception.__cause__, error_type)
                if error_type is OSError:
                    self.assertEqual(refused.exception.__cause__.errno, 5)
                self.assertEqual(calls, [False] if cut in ('size-error', 'zero-size', 'large-size')
                                 else [False, True])

    def test_native_boot_identity_matches_the_host_oracle(self):
        from relay_engine import owner_start

        if sys.platform == 'darwin':
            boot = subprocess.check_output(
                ['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], encoding='ascii', timeout=1)
        else:
            self.assertEqual(sys.platform, 'linux')
            boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii')
        self.assertEqual(owner_start.NativeIdentity.current().boot, str(UUID(boot.strip())))

    def test_reap_failed_start_kills_a_child_that_exceeds_the_deadline(self):
        from relay_engine import owner_start

        child = Mock()
        child.poll.return_value = None

        def wait(timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired('child', timeout)
            return -signal.SIGKILL

        child.wait.side_effect = wait
        with patch.object(owner_start.time, 'monotonic', side_effect=[0.0, 0.0, 5.0]):
            owner_start._reap_failed_start(child, owner_start._OwnerStop())
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args.args, ())
        self.assertEqual(child.wait.call_args.kwargs, {})

    def test_reap_failed_start_preserves_a_child_exit_before_the_deadline(self):
        from relay_engine import owner_start

        child = Mock()
        child.poll.side_effect = [None, 0]
        child.wait.return_value = 0
        owner_start._reap_failed_start(child, owner_start._OwnerStop())
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        child.kill.assert_not_called()

    def test_spawn_oserror_propagates_without_reaping(self):
        from relay_engine import owner_start

        attempt = SimpleNamespace(selection=SimpleNamespace(root=Path('/test-store'), project=PROJECT,
                                                            bridge_port=None),
                                  spawn=lambda launch: launch())
        failure = OSError('spawn failed')
        with patch.object(owner_start, '_reap_failed_start') as reap:
            with self.assertRaises(OSError) as raised:
                owner_start._run_child(attempt, b'frame', None, Mock(side_effect=failure),
                                       owner_start._OwnerStop())
        self.assertIs(raised.exception, failure)
        reap.assert_not_called()

    def test_post_spawn_oserror_reaps_and_refuses_retirement(self):
        from relay_engine import owner_start

        attempt = SimpleNamespace(selection=SimpleNamespace(root=Path('/test-store'), project=PROJECT,
                                                            bridge_port=None),
                                  spawn=lambda launch: launch())
        child = Mock()
        child.poll.return_value = None
        failure = OSError('post-spawn failure')
        stop = owner_start._OwnerStop()

        def launch(_command, *, pass_fds, **_options):
            # Keep the child's read end alive until the injected post-spawn cut.
            self.addCleanup(os.close, os.dup(pass_fds[0]))
            return child

        with (patch.object(owner_start, 'offer_child_start', side_effect=failure),
              patch.object(owner_start, '_reap_failed_start') as reap):
            with self.assertRaisesRegex(owner_start.StartupRefused, 'retirement-incomplete') as refused:
                owner_start._run_child(attempt, b'frame', None, launch, stop)
        self.assertIs(refused.exception.__cause__, failure)
        reap.assert_called_once_with(child, stop)

    def test_production_birth_capability_matches_provider_signature(self):
        from relay_engine.store_layout import IncumbentGuard

        supported = getattr(IncumbentGuard, 'ACCEPTS_PRODUCTION_BIRTH', False) is True
        declared = 'consume_production_birth' in inspect.signature(IncumbentGuard._create_new).parameters
        self.assertEqual(supported, declared)

    def test_absent_or_non_boolean_birth_capability_refuses_before_calling_provider(self):
        from relay_engine import owner_start, store_layout

        for attributes in ({}, {'ACCEPTS_PRODUCTION_BIRTH': 1}):
            with self.subTest(attributes=attributes):
                # A matching signature makes this test distinguish the new capability check.
                def create_new(_root, _store, _project, *, kind, consume_production_birth):
                    self.fail('incapable provider was called')
                provider = type('Provider', (), {'_create_new': staticmethod(create_new), **attributes})
                with patch.object(store_layout, 'IncumbentGuard', provider):
                    with self.assertRaisesRegex(owner_start.StartupRefused, 'birth-provider-unavailable'):
                        owner_start.production_birth(None, None)

class OwnerStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory(prefix='adt-start-', dir='/tmp')
        cls.addClassCleanup(scratch.cleanup)
        cls.base = Path(scratch.name).resolve()
        cls.key = cls.base / 'signer'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(cls.key)],
                       check=True, capture_output=True, timeout=10)
        cls.pub = cls.key.with_suffix('.pub')
        cls.raw_key = installed_key(cls.pub, None)

    def setUp(self):
        spec = importlib.util.find_spec('relay_engine.owner_start')
        self.assertIsNotNone(spec, 'signed startup admission is missing')
        self.startup = importlib.import_module(spec.name)
        self.enterContext(patch.object(operator_terminal, '_installed_key', return_value=self.raw_key))
        self.enterContext(patch('relay_engine.store_layout.production_root', return_value=self.base / 'store'))
        self.selection = self.startup.StartSelection.checked(
            self.base / 'store', STORE, PROJECT, project_name='Example', initialize=True, bridge_port=0)
        self.owner = self.startup.NativeIdentity.current()

    def sign(self, message):
        return subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'sign', '-f', str(self.key),
                               '-n', 'adt-terminal-check-v1'], input=message, check=True,
                              capture_output=True, timeout=10).stdout

    def authorize(self, prover=None, now=ISSUED, selection=None, owner=None):
        with patch.object(self.startup, '_clock_ns', side_effect=[ISSUED, now]):
            return self.startup.authorize_owner_start(
                selection or self.selection, owner or self.owner, prove=prover or self.sign,
                show=lambda line: self.assertTrue(
                    'initialize' in line.lower() and str(self.selection.root) in line and
                    PROJECT in line and 'Example' in line))

    def exchange(self, attempt, *, child_clock=ISSUED, wrong_parent=False):
        with self.startup.startup_listener() as (listener, path):
            with patch.object(self.startup, '_clock_ns', return_value=ISSUED):
                child = attempt.spawn(lambda: subprocess.Popen(
                    [sys.executable, __file__, str(path), str(self.pub), str(self.selection.root),
                     str(child_clock), str(int(wrong_parent))],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            try:
                try:
                    with self.startup.offer_child_start(
                        listener, child, attempt, deadline=time.monotonic() + 5
                    ):
                        output, stderr = child.communicate(timeout=6)
                except self.startup.StartupRefused as error:
                    if not wrong_parent:
                        raise
                    self.assertIn(error.reason, ('startup_peer', 'startup_frame'))
                    output, stderr = child.communicate(timeout=6)
                self.assertEqual((child.returncode, stderr), (0, ''), output)
                return output.strip()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_owner_bad_proof_has_no_mutation_or_spawn(self):
        consume, spawn = Mock(), Mock()
        with self.assertRaisesRegex(self.startup.StartupRefused, 'bad_signature'):
            attempt = self.authorize(lambda message: self.sign(message + b'changed'))
            attempt.consume_birth(consume)
            attempt.spawn(spawn)
        self.assertFalse(self.selection.root.exists())
        consume.assert_not_called()
        spawn.assert_not_called()

    def test_child_refuses_replayed_or_wrong_owner_startup(self):
        attempt = self.authorize()
        self.assertEqual(self.exchange(attempt), 'accepted')
        with self.assertRaisesRegex(self.startup.StartupRefused, 'startup_used'):
            self.exchange(attempt)
        wrong_boot = self.owner._replace(boot=str(UUID(int=3)))
        with self.assertRaisesRegex(self.startup.StartupRefused, 'startup_owner'):
            self.authorize(owner=wrong_boot)

        attempt = self.authorize()
        self.assertEqual(self.exchange(attempt, wrong_parent=True), 'startup_peer')

        changed_selection = replace(self.selection, bridge_port=15)
        attempt = self.authorize(selection=changed_selection)
        self.assertEqual(self.exchange(attempt), 'startup_selection')
        self.assertFalse(self.selection.root.exists())

    def test_signed_startup_expiry_at_each_consumption_cut(self):
        attempt = self.authorize(now=EXPIRES - 1)
        with self.assertRaisesRegex(self.startup.StartupRefused, 'startup_expired'):
            self.authorize(now=EXPIRES)
        self.assertEqual(self.exchange(attempt, child_clock=EXPIRES - 1), 'accepted')
        self.assertEqual(self.exchange(self.authorize(), child_clock=EXPIRES), 'startup_expired')
        for cut in ('consume_birth', 'spawn'):
            with self.subTest(cut=cut):
                for now, allowed in ((EXPIRES - 1, True), (EXPIRES, False)):
                    attempt = self.authorize()
                    operation = Mock(return_value='reached')
                    with patch.object(self.startup, '_clock_ns', return_value=now):
                        if allowed:
                            self.assertEqual(getattr(attempt, cut)(operation), 'reached')
                            operation.assert_called_once()
                        else:
                            with self.assertRaisesRegex(self.startup.StartupRefused, 'startup_expired'):
                                getattr(attempt, cut)(operation)
                            operation.assert_not_called()
        self.assertFalse(self.selection.root.exists())

    def test_monotonic_encoding_is_exact_and_bounded(self):
        attempt = self.authorize(now=EXPIRES - 1)
        self.assertEqual(attempt.arguments['issued_monotonic_ns'], str(ISSUED))
        self.assertEqual(attempt.arguments['expires_monotonic_ns'], str(EXPIRES))
        changed = {**attempt.arguments, 'issued_monotonic_ns': str(ISSUED + 1),
                   'expires_monotonic_ns': str(EXPIRES + 1)}
        variant = operator_terminal.build_message(
            'engine_start', changed, store_id=STORE, value=attempt.value)
        self.assertNotEqual(variant, attempt.message)
        with self.assertRaisesRegex(operator_terminal.OperatorCheckRefused, 'bad_signature'):
            operator_terminal._principal_after_verify(variant, attempt.signature)

        for bad in (ISSUED, True, '', '+1', '-1', '01', '１', '1\n', '9' * 20, str(2**63)):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                self.startup.StartupRefused, 'startup_clock'
            ):
                self.startup._clock_bounds({**attempt.arguments, 'issued_monotonic_ns': bad})
        with self.assertRaisesRegex(self.startup.StartupRefused, 'startup_clock'):
            self.startup._clock_bounds({'issued_monotonic_ns': str(2**63 - 120),
                                        'expires_monotonic_ns': str(2**63 - 1)})

    def test_native_identity_lookup_failure_is_a_startup_refusal(self):
        failure = OSError('native lookup failed')
        with patch.object(self.startup.lifecycle_paths, '_native_identity', side_effect=failure):
            with self.assertRaisesRegex(self.startup.StartupRefused, 'startup_owner') as refused:
                self.startup.NativeIdentity.current()
        self.assertIs(refused.exception.__cause__, failure)

    def test_native_filesystem_identity_is_encoded_losslessly(self):
        wide_identity = (2**53 + 1, 2**64 - 1)
        with patch.object(self.startup, '_selection_identity', return_value=wide_identity):
            attempt = self.authorize(selection=replace(self.selection, identity=wide_identity))
        self.assertEqual(attempt.arguments['identity'], [str(value) for value in wide_identity])


    def test_production_birth_invokes_the_real_provider_with_a_single_checked_claim(self):
        from relay_engine import store_layout

        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory).resolve() / 'store'
            with (patch.object(store_layout, 'production_root', return_value=root),
                  patch.object(self.startup, '_clock_ns', return_value=ISSUED)):
                selection = self.startup.StartSelection.checked(
                    root, STORE, PROJECT, initialize=True, project_name='Example', bridge_port=None)
                attempt = self.startup.authorize_owner_start(
                    selection, self.owner, prove=self.sign, show=lambda _: None)
                consume = Mock(wraps=attempt.consume_production_birth)
                frame = self.startup.production_birth(selection, consume)
                consume.assert_called_once_with(root, STORE, PROJECT, selection.identity)
                idle, proof = store_layout._decode_idle_receipt(frame)
                self.assertEqual((idle.store, idle.project, proof.kind), (STORE, PROJECT, 'retirement'))
                with self.assertRaisesRegex(store_layout.LayoutFault, 'birth-unproved'):
                    attempt.consume_production_birth(root, STORE, PROJECT, selection.identity)

    def test_owner_birth_consume_matches_signed_parent_and_leaf(self):
        from relay_engine import store_layout

        root = self.selection.root
        expected = self.selection.identity
        directory = root.parent / '.adt-owner' / 'receipts'
        directory.parent.mkdir(mode=0o700)
        directory.mkdir(mode=0o700)
        spawn = Mock()

        def authorize(selection):
            return self.authorize(selection=selection)

        def birth(selection, consume):
            for selected, parent in ((root, (expected[0], expected[1] + 1)),
                                     (root.with_name('different'), expected)):
                with self.subTest(root=selected, parent=parent), self.assertRaisesRegex(
                    store_layout.LayoutFault, 'birth-unproved'
                ):
                    consume(selected, selection.store, PROJECT, parent)
            self.assertFalse(root.exists())
            consume(root, selection.store, PROJECT, expected)
            consume(root, selection.store, PROJECT, expected)

        with (patch.object(store_layout, 'production_root', return_value=root),
              patch.object(self.startup, '_clock_ns', return_value=ISSUED)):
            with self.assertRaisesRegex(store_layout.LayoutFault, 'birth-unproved'):
                self.startup.run_owner(root, PROJECT, initialize=True, project_name='Example',
                                       bridge_port=None, authorize=authorize, launch=spawn,
                                       birth_provider=birth, show=lambda _: None)
        self.assertFalse(root.exists())
        spawn.assert_not_called()

    def test_expiry_after_guarded_birth_retains_exact_receipt(self):
        from relay_engine import store_layout
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete), tempfile.TemporaryDirectory(dir=self.base) as home:
                home = Path(home)
                root = home / 'store'
                directory = home / '.adt-owner' / 'receipts'
                directory.parent.mkdir(mode=0o700)
                directory.mkdir(mode=0o700)
                now = [ISSUED]
                captured = []
                spawn = Mock()

                def authorize(selection):
                    captured.append(selection)
                    return self.startup.authorize_owner_start(
                        selection, self.owner, prove=self.sign, show=lambda _: None)

                def birth(selection, consume):
                    frame = synthetic_birth(selection, consume, incomplete=incomplete)
                    captured.append(frame)
                    catalogue.reset_mock()
                    ledger.reset_mock()
                    now[0] = EXPIRES
                    return frame

                with (patch.object(store_layout, 'production_root', return_value=root),
                      patch.object(self.startup, '_clock_ns', side_effect=lambda: now[0]),
                      patch.object(store_layout, 'open_catalogue', wraps=store_layout.open_catalogue) as catalogue,
                      patch.object(store_layout, 'open_ledger_runtime', wraps=store_layout.open_ledger_runtime) as ledger):
                    refusal = 'synthetic-birth-unsettled' if incomplete else 'startup_expired'
                    with self.assertRaisesRegex((self.startup.StartupRefused, store_layout.LayoutFault), refusal):
                        self.startup.run_owner(root, PROJECT, initialize=True, project_name='Example',
                                               bridge_port=None, authorize=authorize, launch=spawn,
                                               birth_provider=birth, show=lambda _: None)
                    spawn.assert_not_called()
                    catalogue.assert_not_called()
                    ledger.assert_not_called()
                    with store_layout.IncumbentGuard.acquire(root) as guard:
                        self.assertEqual(guard.read().phase, 'armed' if incomplete else 'idle')
                    if incomplete:
                        self.assertEqual(list(directory.iterdir()), [])
                    else:
                        selection = self.startup.StartSelection.checked(root, captured[0].store, PROJECT)
                        self.assertEqual(self.startup.read_owner_receipt(selection), captured[1])


if __name__ == '__main__':
    print(child_main(*sys.argv[1:]))
