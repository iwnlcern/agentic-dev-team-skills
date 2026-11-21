"""Explicit adt commands on scratch services, including lost-reply recovery."""
import ast
from contextlib import redirect_stdout, redirect_stderr, nullcontext
import io
import hashlib
import tempfile
import json
import os
from pathlib import Path
import runpy
import select
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from relay_engine import api_protocol, api_sessions
from relay_engine.tests import test_api_service as fixture
from relay_engine.tests import test_api_client as client_fixture
from relay_engine.tests.test_submit import draft


def forbidden_sign_in(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if (isinstance(node.func, ast.Attribute) and node.func.attr == 'sign_in'
                or isinstance(node.func, ast.Name) and node.func.id == 'sign_in'
                or any(isinstance(arg, ast.Constant) and arg.value == 'session.sign-in'
                       for arg in list(node.args) + [k.value for k in node.keywords])):
            found.append(node.lineno)
    return found


class OwnerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.launcher = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'adt'))
        self.args = SimpleNamespace(store_root='/test-store', project_id='project', initialize=False,
                                    project_name=None, bridge_port=None)

    def test_owner_launcher_uses_devnull_and_preserves_spawn_errors(self):
        from relay_engine import owner_start

        def run_owner(*_args, **options):
            return options['launch'](['engine', 'serve'], env={}, pass_fds=(4,), start_new_session=True)

        failure = subprocess.SubprocessError('spawn failed')
        with (patch.object(owner_start, 'run_owner', side_effect=run_owner),
              patch.object(subprocess, 'Popen', side_effect=failure) as popen):
            with self.assertRaises(subprocess.SubprocessError) as raised:
                self.launcher['_run_selected_owner'](self.args)
        self.assertIs(raised.exception, failure)
        popen.assert_called_once_with(
            [sys.executable, self.launcher['__file__'], 'engine', 'serve'],
            env={}, pass_fds=(4,), start_new_session=True, stdin=subprocess.DEVNULL,
        )

    def test_owner_launcher_preserves_cli_spawn_failure_messages(self):
        from relay_engine import adt_cli, owner_start

        def run_owner(*_args, **options):
            return options['launch'](['engine', 'serve'])

        argv = ['engine', 'run', '--store-root', self.args.store_root,
                '--project-id', self.args.project_id]
        for failure, message in (
            (OSError('spawn failed'), 'adt engine run: The selected Store could not be served (owner-unavailable).'),
            (subprocess.SubprocessError('spawn failed'), 'unexpected error'),
        ):
            with self.subTest(failure=type(failure).__name__):
                output = io.StringIO()
                with (patch.object(owner_start, 'run_owner', side_effect=run_owner),
                      patch.object(subprocess, 'Popen', side_effect=failure),
                      patch.object(adt_cli.strings, 'report_diagnostic'),
                      redirect_stderr(output)):
                    status = adt_cli.main(argv, groups=(self.launcher['_engine_owner_group'](),))
                self.assertEqual((status, output.getvalue().strip()), (1, message))

    def test_owner_launcher_preserves_birth_provider_unavailable_guidance(self):
        from relay_engine import owner_start, store_layout

        def run_owner(*_args, **_options):
            return owner_start.production_birth(None, None)

        class Provider:
            @staticmethod
            def _create_new(root, store, project):
                raise AssertionError('incapable provider was called')

        for capability in (None, 1):
            with self.subTest(capability=capability):
                if capability is not None:
                    Provider.ACCEPTS_PRODUCTION_BIRTH = capability
                output = io.StringIO()
                with (patch.object(store_layout, 'IncumbentGuard', Provider),
                      patch.object(owner_start, 'run_owner', side_effect=run_owner),
                      redirect_stderr(output)):
                    status = self.launcher['_run_selected_owner'](self.args)
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue().strip(),
                                 'adt engine run: This package does not contain the checked production Store provider.')


class AdtTests(unittest.TestCase):
    def setUp(self):
        fixture.ApiServiceTests.setUp(self)
        from relay_engine import adt_cli, api_client
        self.cli, self.c = adt_cli, api_client
        self.adt_env = patch.dict(os.environ, {})
        self.adt_env.start(); self.addCleanup(self.adt_env.stop)
        for name in ('ADT_KEY_FILE', 'ADT_SESSION_ID', 'ADT_READY_FILE'):
            os.environ.pop(name, None)

    start = fixture.ApiServiceTests.start
    rows = fixture.ApiServiceTests.rows
    key_path = client_fixture.ClientTests.key_path

    def begin(self):
        if self.service.ready_path is None:
            self.ready, _ = self.start()
        path = self.key_path()
        client = self.c.ApiClient.open(self.c.read_ready_file(str(self.ready)))
        try:
            result = client.sign_in(self.c.read_key_file(str(path)), 'codex', 'window', 'sign-once')
        finally:
            client.close()
        self.assertIsInstance(result, dict)
        self.signed = result
        self.flags = ['--key', str(path), '--session-id', result['session_id'], '--ready', str(self.ready)]
        self.draft = self.base / 'draft.md'
        self.draft.write_bytes(draft(to='platform.implementer'))
        return result

    def run_command(self, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = self.cli.main(argv, **kwargs)
        return status, out.getvalue(), err.getvalue()

    def submit(self, *extra):
        return self.run_command(['submit', str(self.draft), *self.flags, *extra])

    def drop_reply(self, field):
        original = socket.socket.sendall
        captured = []
        def send(peer, data, *args, **kwargs):
            envelope = json.loads(data[4:])
            if field in envelope.get('result', {}):
                captured.append(envelope['result'])
                peer.close()
                raise OSError('test reply dropped after commit')
            return original(peer, data, *args, **kwargs)
        return patch.object(socket.socket, 'sendall', send), captured

    def test_end_to_end_submit(self):
        self.begin()
        status, out, err = self.submit()
        self.assertEqual((status, err), (0, ''))
        result = json.loads(out)
        self.assertEqual(set(result), {'relay_id', 'handle', 'created_at'})
        self.assertEqual(self.rows('SELECT id FROM relays'), [(result['relay_id'],)])

    def test_lost_submit_response_rerun_replays(self):
        self.begin(); dropped, captured = self.drop_reply('relay_id')
        with dropped:
            status, out, err = self.submit()
        self.assertEqual((status, out), (75, ''))
        refusal = json.loads(err)
        self.assertEqual(refusal['outcome'], 'unknown')
        self.assertTrue(refusal['submission_key'].startswith('sha256:'))
        self.assertEqual(len(captured), 1)
        status, out, err = self.submit()
        self.assertEqual((status, err), (0, ''))
        self.assertEqual(json.loads(out), captured[0])
        self.assertEqual(self.rows('SELECT count(*) FROM relays'), [(1,)])

    def test_session_kept_across_submits(self):
        original = self.c.ApiClient.call; operations = []
        def call(client, operation, args):
            operations.append(operation)
            return original(client, operation, args)
        with patch.object(self.c.ApiClient, 'call', call):
            signed = self.begin()
            before = self.rows('SELECT id,generation,ended_at FROM seat_sessions')
            for number in range(3):
                self.draft.write_bytes(draft(to='platform.implementer', subject=str(number)))
                self.assertEqual(self.submit()[0], 0)
                self.assertEqual(self.rows('SELECT id,generation,ended_at FROM seat_sessions'), before)
            self.assertEqual(self.run_command(['sign-out', *self.flags])[0], 0)
        self.assertEqual(sum(op == 'session.sign-in' for op in operations), 1)
        rows = self.rows('SELECT id,generation,ended_at FROM seat_sessions')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], (signed['session_id'], int(signed['generation'])))
        self.assertIsNotNone(rows[0][2])

    def test_secret_absent_from_all_output(self):
        self.begin(); secret = self.keys['planner']['key_secret']
        texts = []
        texts.extend(self.submit()[1:])
        for kind in ('endpoint_not_selected', 'ready_file_invalid', 'ready_file_mismatch',
                     'key_file_invalid', 'key_file_unprotected', 'mixed_credentials',
                     'no_session', 'draft_unreadable', 'draft_too_large'):
            for command in (['submit', str(self.draft), *self.flags], ['sign-out', *self.flags]):
                with patch.object(self.c, 'read_key_file', side_effect=self.c.LocalRefusal(kind)):
                    texts.extend(self.run_command(command)[1:])
        for kind, payload in [('key_rejected', {}), ('invalid_args', {}),
                              ('runtime_not_ready', {}), ('handler_fault', {}),
                              ('submission_key_reused', {}), ('policy', {'engine_code': 'E-ADDRESS'})]:
            error = api_protocol.api_error(self.service.registry, kind, **payload)
            value = self.c.ApiErrorValue(error['code'], error['class'], error['cause'], error['remedy'])
            for command in (['submit', str(self.draft), *self.flags], ['sign-out', *self.flags]):
                with patch.object(self.c.ApiClient, 'call', return_value=value):
                    # Leave endpoint negotiation real; only the operation's response is scripted.
                    with patch.object(self.c.ApiClient, 'open') as opened:
                        opened.return_value.call.return_value = value
                        texts.extend(self.run_command(command)[1:])
        texts.extend(self.run_command(['sign-out', *self.flags])[1:])
        self.assertNotIn(secret, ''.join(texts))
        for path in self.home.rglob('*'):
            if path.is_file(): self.assertNotIn(secret.encode(), path.read_bytes())

    def test_sign_out_retry_reaches_end_replay(self):
        self.begin(); original_end = api_sessions.end_session_step
        dropped, captured = self.drop_reply('ended_at')
        with patch.object(api_sessions, 'end_session_step', wraps=original_end) as ended:
            with dropped:
                status, out, err = self.run_command(['sign-out', *self.flags, '--operation-key', 'end-once'])
            self.assertEqual((status, out), (75, ''))
            self.assertEqual(json.loads(err)['operation_key'], 'end-once')
            self.assertEqual(json.loads(err)['outcome'], 'unknown')
            status, out, err = self.run_command(['sign-out', *self.flags, '--operation-key', 'end-once'])
            self.assertEqual((status, err), (0, ''))
            result = json.loads(out)
            self.assertEqual(result['ended_at'], captured[0]['ended_at'])
            self.assertEqual(result['receipt'], 'replayed')
            self.assertEqual(ended.call_count, 1)
        self.assertEqual(self.rows('SELECT count(*) FROM seat_sessions'), [(1,)])
        # A fresh session with no end receipt must first resume, then end.
        other = self.c.ApiClient.open(self.c.read_ready_file(str(self.ready)))
        try:
            signed = other.sign_in(self.c.read_key_file(str(self.base / 'key')), 'codex', 'second', 'second-sign')
        finally: other.close()
        flags = list(self.flags); flags[3] = signed['session_id']
        original_call = self.c.ApiClient.call; operations = []
        def call(client, op, args):
            operations.append(op); return original_call(client, op, args)
        with patch.object(self.c.ApiClient, 'call', call):
            self.assertEqual(self.run_command(['sign-out', *flags, '--operation-key', 'fresh-end'])[0], 0)
        self.assertEqual(operations, ['protocol.connect', 'session.end', 'session.resume', 'session.end'])

    def test_credential_tiers(self):
        self.begin()
        os.environ['ADT_SESSION_ID'] = self.signed['session_id']
        status, out, err = self.run_command(['submit', str(self.draft), '--key', str(self.base / 'key'), '--ready', str(self.ready)])
        self.assertEqual((status, out), (1, '')); self.assertEqual(json.loads(err)['local'], 'mixed_credentials')
        os.environ.pop('ADT_SESSION_ID')
        status, out, err = self.run_command(['submit', str(self.draft)])
        self.assertEqual((status, out), (1, '')); self.assertEqual(json.loads(err)['local'], 'no_session')
        self.assertIn('adt sign-in', err)
        record = self.cli.WindowSession(str(self.base / 'key'), self.signed['session_id'], str(self.ready))
        self.assertEqual(self.run_command(['submit', str(self.draft)], resolver=lambda: record)[0], 0)
        os.environ.update(ADT_KEY_FILE=record.key_path, ADT_SESSION_ID=record.session_id, ADT_READY_FILE=record.ready_path)
        self.assertEqual(self.run_command(['submit', str(self.draft)])[0], 0)
        status, out, err = self.run_command(['submit', str(self.draft), '--ready', ''])
        self.assertEqual((status, out), (1, ''))
        self.assertEqual(json.loads(err)['local'], 'endpoint_not_selected')

    def test_adt_sends_no_sign_in(self):
        self.assertEqual(self.run_command(['sign-in'])[0], 2)
        tools = Path(__file__).resolve().parents[2]
        self.assertEqual(forbidden_sign_in((tools / 'relay_engine/adt_cli.py').read_text()), [])
        self.assertEqual(forbidden_sign_in('client.sign_in(key, host, window, operation)'), [1])
        launcher = ast.parse((tools / 'adt').read_text())
        calls = [n for n in ast.walk(launcher) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'main']
        self.assertEqual(len(calls), 1)
        self.assertEqual({keyword.arg for keyword in calls[0].keywords},
                         {'groups', 'queue_notifier', 'stop_report'})

    def test_seam_registers_a_group(self):
        def build(commands):
            commands.add_parser('probe').set_defaults(handler=lambda args: 0)
        self.assertEqual(self.run_command(['probe'], groups=(self.cli.CommandGroup('probe', build),)), (0, '', ''))

    def test_relay_engine_never_imports_adapters(self):
        tools = Path(__file__).resolve().parents[2]
        prefix = 'import sys,json,importlib,pkgutil; sys.path[:0]=' + repr([str(tools), str(tools.parent)]) + '; '
        imports = 'import relay_engine; [importlib.import_module(m.name) for m in pkgutil.walk_packages(relay_engine.__path__, "relay_engine.") if ".tests" not in m.name]; '
        query = 'print(json.dumps(sorted(n for n in sys.modules if n=="adapters" or n.startswith("adapters.") or n.startswith("tools.adapters"))))'
        for extra, expected in [('', False), ('import tools.adapters.seat_record; ', True)]:
            proc = subprocess.run([sys.executable, '-I', '-c', prefix + imports + extra + query],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(bool(json.loads(proc.stdout)), expected)

    def test_exit_statuses(self):
        self.begin(); self.assertEqual(self.submit()[0], 0)
        for command in (['submit', str(self.draft), *self.flags, '--submission-key', ''],
                        ['sign-out', *self.flags, '--operation-key', '']):
            with self.subTest(empty_explicit=command[0]):
                self.assertEqual(self.run_command(command)[0], 1)
        self.assertEqual(self.run_command(['submit'])[0], 2)
        with patch.object(self.c, 'read_key_file', side_effect=self.c.LocalRefusal('key_file_invalid')):
            self.assertEqual(self.submit()[0], 1)
        for outcome in ('not_performed', 'endpoint_mismatch', 'unknown'):
            with patch.object(self.c.ApiClient, 'open', side_effect=self.c.EndpointUnavailable(outcome)):
                status, out, err = self.submit()
            self.assertEqual((status, out), (75, '')); self.assertEqual(json.loads(err)['outcome'], outcome)
        for cause in ('runtime_not_ready', 'admission_unknown'):
            error = api_protocol.api_error(self.service.registry, cause)
            value = self.c.ApiErrorValue(error['code'], error['class'], error['cause'], error['remedy'])
            with patch.object(self.c.ApiClient, 'open') as opened:
                opened.return_value.call.return_value = value
                status, out, err = self.submit()
            self.assertEqual((status, out), (75, '')); self.assertEqual(json.loads(err), error)
        data = json.loads(self.ready.read_bytes()); data['socket'] = str(self.ready.parent / 'refused')
        self.ready.write_text(json.dumps(data))
        status, out, err = self.submit()
        self.assertEqual((status, out), (75, '')); self.assertEqual(json.loads(err)['outcome'], 'not_performed')

    def test_serve_command_prints_ready_path(self):
        tools = Path(__file__).resolve().parents[2]
        with fixture.private_idle(self.admitted) as (idle_fd, environment):
            proc = subprocess.Popen([sys.executable, str(tools / 'adt'), 'engine', 'serve',
                '--store-root', str(self.root), '--project-id', fixture.P], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=environment, pass_fds=(idle_fd,))
        def finish():
            if proc.poll() is None: proc.terminate()
            proc.communicate(timeout=10)
        self.addCleanup(finish)
        self.assertTrue(select.select([proc.stdout], [], [], 10)[0], 'no ready line')
        line = proc.stdout.readline().strip()
        self.assertTrue(Path(line).is_absolute(), line)
        self.assertTrue(Path(line).is_file())
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, out, err), (0, '', ''))
        self.assertFalse(Path(line).exists())

    def _assert_production_refusal(self, spelling, canonical, reason):
        from relay_engine import api_service, store_layout

        record = self.root / store_layout._INCUMBENT
        original = record.read_bytes()
        argv = ['engine', 'serve', '--store-root', str(spelling), '--project-id', fixture.P]
        with fixture.private_idle(self.admitted) as (fd, environment):
            environment['ADT_PRIVATE_IDLE_FD'] = str(os.dup(fd))
            with patch.dict(os.environ, environment), patch.object(
                store_layout, 'production_root', return_value=canonical
            ), patch.object(api_service.ApiService, 'arm_foreground'), patch.object(
                store_layout.IncumbentGuard, 'arm', side_effect=store_layout.LayoutFault('test-arm-reached')
            ) as arm, patch.object(api_service.StoreWriter, 'start') as writer:
                status, output, error = self.run_command(argv)
        self.assertEqual((status, output), (1, ''))
        self.assertIn(reason, error)
        arm.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(record.read_bytes(), original)

    def test_child_refuses_receipt_only_production_before_open(self):
        self._assert_production_refusal(self.root, self.root, 'startup-unverified')

    def test_canonical_production_inode_cannot_use_isolated_intake(self):
        actual = self.root.resolve()
        canonical = actual.with_name(actual.name.upper())
        original_lstat = Path.lstat

        def case_insensitive_lstat(path):
            return original_lstat(actual if path == canonical else path)

        with patch.object(Path, 'lstat', case_insensitive_lstat):
            self._assert_production_refusal(actual, canonical, 'startup-unverified')

    def _mark_fixture_production(self):
        import sqlite3

        from relay_engine import store_layout

        with sqlite3.connect(store_layout.catalogue_path(self.root)) as catalogue:
            catalogue.execute("UPDATE catalogue_identity SET kind='production' WHERE id=1")
        catalogue.close()

    def test_production_alias_cannot_use_isolated_intake(self):
        self._mark_fixture_production()
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self._assert_production_refusal(alias, self.base / 'original-production-root', 'wrong-kind')

    def test_production_child_checks_expiry_under_original_lock(self):
        from relay_engine import operator_terminal, owner_start, store_layout
        from relay_engine.tests.test_owner_start import EXPIRES, ISSUED, installed_key

        self._mark_fixture_production()
        root = self.root.resolve()
        record = root / store_layout._INCUMBENT
        original = record.read_bytes()
        key = self.base / 'synthetic-signer'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                       check=True, capture_output=True, timeout=10)
        public_key = key.with_suffix('.pub')
        raw_key = installed_key(public_key, None)

        def prove(message):
            return subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'sign', '-f', str(key),
                                   '-n', 'adt-terminal-check-v1'], input=message, check=True,
                                  capture_output=True, timeout=10).stdout

        child_script = """
from pathlib import Path
import runpy
import sys
from unittest.mock import patch
from relay_engine import adt_cli, api_service, operator_terminal, owner_start, store_layout
from relay_engine.tests.test_owner_start import installed_key
root, project, pub, clock, expiry_cut = sys.argv[1:]
current = [int(clock)]

create_runtime = api_service.ApiService._create_runtime
witness_child_epoch = owner_start.witness_child_epoch

def at_runtime(service, store):
    create_runtime(service, store)
    if expiry_cut == 'runtime':
        current[0] += 1

def at_witness(*args, **kwargs):
    witness_child_epoch(*args, **kwargs)
    if expiry_cut == 'witness':
        current[0] += 1

with (patch.object(store_layout, 'production_root', return_value=Path(root)),
      patch.object(operator_terminal, '_installed_key', return_value=installed_key(pub, None)),
      patch.object(owner_start, '_clock_ns', side_effect=lambda: current[0]),
      patch.object(api_service.ApiService, '_create_runtime', at_runtime),
      patch.object(owner_start, 'witness_child_epoch', at_witness),
      patch.object(store_layout.IncumbentGuard, 'arm',
                   side_effect=store_layout.LayoutFault('checked-startup-gate'))):
    raise SystemExit(adt_cli.main(['engine', 'serve', '--store-root', root, '--project-id', project]))
"""
        with (patch.object(store_layout, 'production_root', return_value=root),
              patch.object(operator_terminal, '_installed_key', return_value=raw_key),
              patch.object(owner_start, '_clock_ns', return_value=ISSUED)):
            selection = owner_start.StartSelection.checked(root, self.admitted.idle.store, fixture.P)
            owner = owner_start.NativeIdentity.current()
            cases = ((EXPIRES - 1, 'none', True, 'checked-startup-gate'),
                     (EXPIRES, 'none', False, 'startup_expired'),
                     (EXPIRES - 1, 'runtime', False, 'startup_expired'),
                     (EXPIRES - 1, 'witness', True, 'startup_expired'),
                     (EXPIRES - 1, 'no-ack', False, 'startup_witness'))
            for now, expiry_cut, expect_witness, reason in cases:
                with self.subTest(now=now, expiry_cut=expiry_cut):
                    attempt = owner_start.authorize_owner_start(selection, owner, prove=prove, show=lambda _: None)
                    with owner_start.startup_listener() as (listener, path):
                        with fixture.private_idle(self.admitted) as (fd, environment):
                            environment['ADT_PRIVATE_START_SOCKET'] = str(path)
                            child = attempt.spawn(lambda: subprocess.Popen(
                                [sys.executable, '-c', child_script, str(root), fixture.P, str(public_key), str(now),
                                 expiry_cut],
                                env=environment, pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True))
                        try:
                            deadline = time.monotonic() + 5
                            with owner_start.offer_child_start(listener, child, attempt, deadline=deadline) as peer:
                                if expect_witness:
                                    owner_start.read_child_epoch(peer, child, deadline=deadline, idle=self.admitted.idle)
                                output, error = child.communicate(timeout=6)
                            self.assertEqual((child.returncode, output), (1, ''))
                            self.assertIn(reason, error)
                        except owner_start.StartupRefused as failure:
                            if child.poll() is None:
                                raise
                            output, error = child.communicate(timeout=5)
                            self.fail(f'{failure.reason}: {child.returncode} {output} {error}')
                        finally:
                            if child.poll() is None:
                                child.kill()
                                child.communicate(timeout=5)
                    self.assertEqual(record.read_bytes(), original)

    def test_serve_start_refusals_print_one_line(self):
        from relay_engine import api_service, store_layout, version
        # Literal copies from erratum 1 r8's table, not strings.INVENTORY.
        templates = {'serve-no-folder': 'adt engine serve: no store at {path}: there is no folder there', 'serve-no-catalogue': 'adt engine serve: no store at {path}: the folder has no store catalogue', 'serve-store-unreadable': 'adt engine serve: the store at {path} cannot be opened ({cause})', 'serve-store-locked': 'adt engine serve: the store at {path} is already served by another adt engine serve; stop that one first', 'serve-project-unavailable': 'adt engine serve: project {project} cannot be opened in the store at {path}', 'serve-runtime-unsafe': 'adt engine serve: the private folder it made in {tmp} for the store at {path} is not private to you; set TMPDIR to a folder of your own and run it again', 'serve-install-incomplete': 'adt engine serve: this install cannot serve the store at {path} ({reason}); reinstall the kit', 'serve-install-mismatch': "adt engine serve: this install's files do not match its roster ({cause}), so it will not serve the store at {path}; reinstall the kit"}
        empty = self.base / 'empty-folder'
        empty.mkdir()
        absent = self.base / 'absent-folder'
        cases = [
            ('serve-no-folder', absent, None, {}),
            ('serve-no-catalogue', empty, None, {}),
            ('serve-store-unreadable', self.root, store_layout.LayoutFault('bad-kind'), {'cause': 'bad-kind'}),
            ('serve-store-locked', self.root, api_service.ServiceStartRefused('store_locked'), {}),
            ('serve-project-unavailable', self.root, api_service.ServiceStartRefused('ledger_unavailable'), {'project': fixture.P}),
            ('serve-runtime-unsafe', self.root, api_service.ServiceStartRefused('runtime_dir_unsafe'), {'tmp': os.path.abspath(tempfile.gettempdir())}),
            ('serve-install-incomplete', self.root, api_service.ServiceStartRefused('port_missing'), {'reason': 'port_missing'}),
            ('serve-install-incomplete', self.root, api_service.ServiceStartRefused('port_unrouted'), {'reason': 'port_unrouted'}),
            ('serve-install-mismatch', self.root, version.FingerprintError('missing-member'), {'cause': 'missing-member'}),
            ('serve-no-folder', self.base / 'missing\nfolder', None, {}),
            ('serve-install-mismatch', self.base / 'mismatch\nfolder', version.FingerprintError('missing-member'), {'cause': 'missing-member'}),
        ]
        for key, root, error, fields in cases:
            with self.subTest(key=key, path=str(root), reason=fields.get('reason')):
                absolute = os.path.abspath(root)
                if '\n' in absolute:
                    raw = os.fsencode(absolute)
                    shown = 'unrecognized-input (sha256:' + hashlib.sha256(raw).hexdigest()[:12] + ', length ' + str(len(raw)) + ')'
                else:
                    shown = absolute
                expected = templates[key].format(path=shown, **fields) + '\n'
                control = (patch.object(api_service.ApiService, 'start', side_effect=error)
                           if error is not None else nullcontext())
                with control:
                    status, out, err = self.run_command(['engine', 'serve', '--store-root', str(root), '--project-id', fixture.P])
                self.assertEqual((status, out), (1, ''))
                self.assertEqual(err, expected)
