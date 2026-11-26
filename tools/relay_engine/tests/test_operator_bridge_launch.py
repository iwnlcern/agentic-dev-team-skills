"""Private launch page and the actual adt Console parser."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import os
import re
import runpy
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest
from unittest.mock import ANY, Mock, patch

from relay_engine import (
    adt_cli, api_client, api_operator_bridge, lifecycle_paths, lifecycle_state, store_layout, submit,
)

from relay_engine.tests import test_api_service as service_fixture
from relay_engine.operator_queue.notifier import DeliveryCancelled, NotifierIssueUnavailable


_PREFIX = b'<!doctype html><meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="0;url='
_SUFFIX = b'">'
_HINT = 'If the page did not open, run adt console open --print and paste the address.\n'

_READY_UNAVAILABLE = (
    'The Store for this ready file is unavailable for Console. If it is still starting, '
    'wait a moment and run this command again. Otherwise check the operator launch terminal for the refusal.\n'
)
_READY_PAGE_FAULT = 'The Console launch page could not be opened.\n'


def load_launcher():
    path = Path(__file__).resolve().parents[2] / 'adt'
    with patch.object(adt_cli, 'main', create=True):
        # A neutral run name exposes definitions without running a guarded main.
        try:
            return runpy.run_path(str(path), run_name='adt_launch_fixture')
        except SystemExit:
            # The pre-Task 2 launcher calls main unconditionally.
            return {}


class TestOperatorBridgeLaunch(unittest.TestCase):
    def test_notifier_launch_uses_closed_paths_and_rechecks_configured_file(self):
        launcher = load_launcher()
        child = Mock()
        child.wait.return_value = child.poll.return_value = 0
        popen = Mock(return_value=child)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'terminal-notifier'
            path.touch()
            deliver = launcher['make_deliver'](str(path), stopping=lambda: False, issue=popen)
            for name, executable in (
                ('osascript', '/usr/bin/osascript'), ('notify-send', '/usr/bin/notify-send'),
                ('terminal-notifier', str(path)),
            ):
                suffix = ['--', r'\literal argument']
                deliver([name, *suffix])
                popen.assert_called_with([executable, *suffix], expires_at=ANY, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            popen.reset_mock()
            other = path.with_name('other')
            path.rename(other)
            path.symlink_to(other)
            for name in ('none', str(path), 'terminal-notifier'):
                with self.assertRaises(launcher['DeliverRefused']):
                    deliver([name, '--'])
            popen.assert_not_called()

    def fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix='launch ')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o700)
        self.page = root / 'open.html'
        self.ready = root / 'ready.json'
        self.ready.write_text(json.dumps({
            'format': 'adt-ready-v1', 'socket': str(root / 'api.sock'),
            'store_id': 'fixture-store', 'build_id': 'b' * 64,
            'schema_digest': 'c' * 64, 'pid': os.getpid(),
        }))
        self.ready.chmod(0o600)
        self.now = 0
        owner = get_ident()
        self.dispatch = Mock(side_effect=AssertionError('application dispatch'))
        self.backend = Mock(side_effect=AssertionError('discovery backend'))
        bridge = api_operator_bridge._new_operator_bridge(
            port=0, run_id='fixture-run', store_id='fixture-store',
            clock_ms=lambda: self.now if get_ident() == owner else 0,
            dispatch=self.dispatch, supervisor_post=None, open_backend=self.backend,
            runtime_dir=root)
        self.addCleanup(bridge.stop)
        self.origin = bridge.start()
        return bridge

    def page_bytes(self):
        contents = self.page.read_bytes()
        self.assertTrue(contents.startswith(_PREFIX) and contents.endswith(_SUFFIX))
        address = contents[len(_PREFIX):-len(_SUFFIX)]
        prefix = self.origin.encode('ascii') + b'/#launch='
        self.assertTrue(address.startswith(prefix))
        secret = address[len(prefix):]
        self.assertTrue(re.fullmatch(rb'[A-Za-z0-9_-]{43}', secret) is not None)
        return contents, address.decode('ascii'), secret.decode('ascii')

    def test_staged_offer_loses_to_ingress_while_existing_claim_can_win(self):
        launcher = load_launcher()
        gate, writer = os.pipe()
        os.set_blocking(gate, False)
        try:
            owner = launcher['_NotifierCustodian'].__new__(launcher['_NotifierCustodian'])
            owner._disabled = False
            owner._ticket = None
            owner._number = 0
            owner._channel = Mock()
            observed = []

            def ingress():
                observed.append(owner._ticket)
                return True

            owner._stopping = ingress
            note = SimpleNamespace(item_id='item', revision=1)
            with self.assertRaises(DeliveryCancelled):
                owner.offer(note, ['synthetic'])
            self.assertEqual(observed, [[1, 'item', 1]])
            self.assertIsNone(owner._ticket)
            owner._channel.send.assert_not_called()

            # The pipe is still open. A previously admitted ticket can claim.
            issue = Mock(return_value='issued')
            self.assertEqual(launcher['_claimed_call'](gate, Event(), ['synthetic'], issue), 'issued')
            issue.assert_called_once_with(['synthetic'])
        finally:
            os.close(gate)
            os.close(writer)

    def test_known_cancellation_during_final_read_prevents_issue(self):
        launcher = load_launcher()
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        cancelled = Event()
        original = os.read
        count = 0

        def check(fd, size):
            nonlocal count
            count += 1
            if count == 2:
                cancelled.set()
            return original(fd, size)

        issue = Mock()
        try:
            with patch.object(os, 'read', side_effect=check):
                self.assertEqual(launcher['_claimed_call'](reader, cancelled, ['synthetic'], issue), 'refused')
            issue.assert_not_called()
        finally:
            os.close(reader)
            os.close(writer)

    def test_malformed_custodian_result_retains_ticket(self):
        launcher = load_launcher()
        owner = launcher['_NotifierCustodian'].__new__(launcher['_NotifierCustodian'])
        owner._disabled = False
        owner._ticket = None
        owner._number = 0
        owner._epoch = 'epoch'
        owner._stopping = lambda: False
        owner._channel = Mock()
        owner._channel.receive.return_value = {'epoch': 'epoch', 'ticket': [1, 'item', 1], 'outcome': []}
        with self.assertRaises(NotifierIssueUnavailable):
            owner.offer(SimpleNamespace(item_id='item', revision=1), ['synthetic'])
        self.assertTrue(owner._disabled)
        self.assertEqual(owner._ticket, [1, 'item', 1])
        self.assertEqual(owner._channel.send.call_count, 1)

    def test_claimed_worker_keeps_one_late_invocation_and_does_not_renew_grace(self):
        launcher = load_launcher()
        gate, writer = os.pipe()
        os.set_blocking(gate, False)
        paused, resume, cancelled = Event(), Event(), Event()
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 0), -9]
        issues = []

        def delayed_popen(argv, **kwargs):
            paused.set()
            self.assertTrue(resume.wait(7))
            issues.append(argv)
            return child

        try:
            with (patch.object(subprocess, 'Popen', side_effect=delayed_popen),
                  ThreadPoolExecutor(1) as pool):
                result = pool.submit(launcher['_launch_notification'], gate, cancelled, [None], ['synthetic'])
                try:
                    self.assertTrue(paused.wait(1))
                    os.close(writer)
                    writer = None
                    cancelled.set()
                    self.assertEqual(issues, [])
                    time.sleep(4.05)
                finally:
                    resume.set()
                self.assertEqual(result.result(1), 'unknown')
            self.assertEqual(issues, [['synthetic']])
            child.terminate.assert_called_once()
            child.kill.assert_called_once()
            self.assertEqual(child.wait.call_args_list, [unittest.mock.call(timeout=0), unittest.mock.call()])
        finally:
            os.close(gate)
            if writer is not None:
                os.close(writer)

    def test_claimed_worker_checks_pipe_again_immediately_before_issue(self):
        launcher = load_launcher()
        gate, writer = os.pipe()
        os.set_blocking(gate, False)
        observed = Event()
        issue = Mock()
        calls = []
        original = os.read

        def retire_after_claim(fd, count):
            calls.append(fd)
            if len(calls) == 1:
                try:
                    return original(fd, count)
                finally:
                    os.close(writer)
            return original(fd, count)

        try:
            with patch.object(os, 'read', side_effect=retire_after_claim):
                launch = launcher.get('_claimed_call')
                self.assertIsNotNone(launch)
                self.assertEqual(launch(gate, observed, ['inert'], issue), 'refused')
            self.assertEqual(calls, [gate, gate])
            issue.assert_not_called()
        finally:
            os.close(gate)
            if not calls:
                os.close(writer)

    def test_notifier_unavailable_or_expired_before_fixture_issue(self):
        launcher = load_launcher()
        stopping = Mock(return_value=False)
        with self.assertRaises(NotifierIssueUnavailable):
            launcher['make_deliver'](None, stopping=stopping)(['notify-send'])
        stopping.assert_not_called()
        issue = Mock()
        offered, release = Event(), Event()

        def fixture_issuer(argv, *, expires_at, **kwargs):
            offered.set()
            release.wait(5)
            if time.monotonic() >= expires_at:
                raise NotifierIssueUnavailable()
            return issue(argv, **kwargs)

        deliver = launcher['make_deliver'](None, stopping=stopping, issue=fixture_issuer)
        with ThreadPoolExecutor(1) as pool:
            receipt = pool.submit(deliver, ['notify-send'])
            try:
                self.assertTrue(offered.wait(1))
                # The fixture backend reads no STOP flag across the issue deadline.
                time.sleep(3.05)
                self.assertFalse(receipt.done())
            finally:
                release.set()
            with self.assertRaises(NotifierIssueUnavailable):
                receipt.result(1)
        issue.assert_not_called()

    def test_notifier_fixture_parent_owns_child_with_delayed_issue_return(self):
        launcher = load_launcher()
        issued, release, stopped = Event(), Event(), Event()
        children = []

        def fixture_issuer(_argv, *, expires_at, **kwargs):
            self.assertLess(time.monotonic(), expires_at)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            issued.set()
            release.wait(8)
            return child

        deliver = launcher['make_deliver'](None, stopping=stopped.is_set, issue=fixture_issuer)
        foreground = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()'])
        try:
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(deliver, ['notify-send'])
                try:
                    self.assertTrue(issued.wait(1))
                    self.assertIsNone(children[0].poll())
                    started = time.monotonic()
                    stopped.set()
                    foreground.terminate()
                    foreground.wait(timeout=2)
                    # Park the native return beyond the original foreground budget.
                    time.sleep(max(0, started + 4.05 - time.monotonic()))
                    self.assertIsNone(children[0].poll())
                    self.assertFalse(result.done())
                finally:
                    release.set()
                with self.assertRaises(DeliveryCancelled):
                    result.result(2)
            child, = children
            self.assertIsNotNone(child.returncode)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
            self.assertGreater(time.monotonic() - started, 4)
            print(json.dumps({'inert_child_pid': child.pid, 'parent_reaped': True,
                              'fixture_foreground_exited': True, 'return_after_original_H': True}))
        finally:
            if foreground.poll() is None:
                foreground.kill()
                foreground.wait(timeout=2)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)


    def test_launch_once_and_bounded(self):
        self.assertTrue(hasattr(api_operator_bridge.OperatorBridge, 'consume_launch'),
                        'missing Task 2 launch page state')
        bridge = self.fixture()
        _, _, first = self.page_bytes()
        self.now = 59999
        bridge._server.launch_page.refresh()
        self.assertTrue(self.page_bytes()[2] == first)
        self.now = 60000
        bridge._server.launch_page.refresh()
        _, _, second = self.page_bytes()
        self.assertTrue(first != second)
        self.now = 89999
        self.assertTrue(bridge.consume_launch(first))
        self.assertFalse(bridge.consume_launch(first))
        self.assertTrue(self.page_bytes()[2] not in (first, second))
        self.assertTrue(bridge.consume_launch(second))
        self.assertFalse(bridge.consume_launch(second))
        _, _, current = self.page_bytes()
        self.now += 91000
        self.assertFalse(bridge.consume_launch(current))
        self.assertTrue(self.page_bytes()[2] != current)
        self.dispatch.assert_not_called()
        self.backend.assert_not_called()
        bridge.stop()
        self.assertFalse(self.page.exists())

    def test_console_open_argv_holds_no_secret(self):
        launcher = load_launcher()
        self.assertIn('console_open', launcher, 'missing Task 2 console_open')
        self.fixture()
        console_open = launcher['console_open']
        content, url, secret = self.page_bytes()
        self.assertEqual(stat.S_IMODE(self.page.stat().st_mode), 0o600)
        for platform, expected in (('darwin', ['/usr/bin/open', '--', str(self.page.absolute())]),
                                   ('linux', ['xdg-open', str(self.page.absolute())])):
            with self.subTest(platform=platform), patch.object(os.sys, 'platform', platform):
                opened = []
                def opener(argv):
                    opened.append(argv)
                    return subprocess.CompletedProcess(argv, 0)
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(console_open(str(self.ready), print_only=False, run=opener), 0)
                self.assertEqual(len(opened), 1)
                self.assertTrue(opened[0] == expected, 'opener requires only the absolute page path')
                self.assertFalse(any(secret in arg for arg in opened[0]))
                self.assertTrue(out.getvalue() == _HINT)
        forbidden = Mock(side_effect=AssertionError('opener on print or refusal'))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(console_open(str(self.ready), print_only=True, run=forbidden), 0)
        self.assertTrue(out.getvalue() == url + '\n', 'print must emit exactly the fragment URL')

        info = self.page.stat()
        actual_fstat = os.fstat
        def wrong_owner(fd):
            status = actual_fstat(fd)
            if status.st_ino == info.st_ino and status.st_dev == info.st_dev:
                return SimpleNamespace(st_mode=status.st_mode, st_uid=os.getuid()+1,
                                       st_dev=status.st_dev, st_ino=status.st_ino)
            return status
        with patch.object(os, 'fstat', side_effect=wrong_owner), redirect_stderr(io.StringIO()):
            self.assertEqual(console_open(str(self.ready), print_only=False, run=forbidden), 1)

        self.page.chmod(0o644)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(console_open(str(self.ready), print_only=False, run=forbidden), 1)
            self.page.unlink()
            self.page.mkdir(mode=0o700)
            self.assertEqual(console_open(str(self.ready), print_only=False, run=forbidden), 1)
            self.page.rmdir()
            sibling = self.page.with_name('target')
            sibling.write_bytes(content)
            sibling.chmod(0o600)
            self.page.symlink_to(sibling)
            self.assertEqual(console_open(str(self.ready), print_only=True, run=forbidden), 1)
            self.page.unlink()
            self.page.write_bytes(b'invalid page')
            self.page.chmod(0o600)
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(console_open(str(self.ready), print_only=True, run=forbidden), 1)
            self.assertEqual(out.getvalue(), '')
            self.ready.unlink()
            self.assertEqual(console_open(str(self.ready), print_only=False, run=forbidden), 1)
        forbidden.assert_not_called()

    def test_open_html_bytes_and_rename(self):
        self.assertTrue(hasattr(api_operator_bridge.OperatorBridge, 'consume_launch'),
                        'missing Task 2 launch page state')
        bridge = self.fixture()
        old, url, _ = self.page_bytes()
        self.assertTrue(old == _PREFIX + url.encode('ascii') + _SUFFIX)
        self.assertEqual(stat.S_IMODE(self.page.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.page.parent.stat().st_mode), 0o700)

        replace = os.replace
        publications = []
        with self.page.open('rb') as old_reader:
            def trace_rename(source, destination, **kwargs):
                destination_name = Path(destination)
                self.assertEqual(destination_name.name, 'open.html')
                self.assertTrue(self.page.read_bytes() == old, 'old page changed before rename')
                fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get('src_dir_fd'))
                with os.fdopen(fd, 'rb') as new_reader:
                    self.assertEqual(stat.S_IMODE(os.fstat(new_reader.fileno()).st_mode), 0o600)
                    candidate = new_reader.read()
                replace(source, destination, **kwargs)
                self.assertTrue(self.page.read_bytes() == candidate)
                publications.append(candidate)
            with patch.object(os, 'replace', side_effect=trace_rename):
                self.now = 60000
                bridge._server.launch_page.refresh()
            self.assertTrue(old_reader.read() == old, 'open reader must see the complete old page')
        self.assertEqual(len(publications), 1)
        new, url, _ = self.page_bytes()
        self.assertTrue(new == publications[0] == _PREFIX + url.encode('ascii') + _SUFFIX)
        self.assertTrue(new != old)

    def test_console_group_extends(self):
        launcher = load_launcher()
        self.assertIn('console_group', launcher, 'missing Task 2 console_group')
        group = launcher['console_group']
        order = []
        def extra(sp):
            order.append(sp)
            sp.add_parser('list').set_defaults(handler=lambda _args: 7)
        def observe(sp):
            order.append(sp)
            self.assertIn('open', sp.choices)
            self.assertIn('list', sp.choices)
        extension = group((extra, observe))
        self.assertEqual(extension.name, 'console')
        parser = adt_cli._parser((extension,))
        self.assertEqual(len(order), 2)
        self.assertIs(order[0], order[1])
        opened = parser.parse_args(['console', 'open', '--ready', 'fixture', '--print'])
        self.assertTrue(callable(opened.handler))
        self.assertEqual((opened.ready, opened.print_only), ('fixture', True))
        self.assertEqual(parser.parse_args(['console', 'list']).handler(None), 7)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                parser.parse_args(['list'])
            self.assertEqual(error.exception.code, 2)
            with self.assertRaises(SystemExit) as error:
                adt_cli._parser((group(),)).parse_args(['console', 'list'])
            self.assertEqual(error.exception.code, 2)
        with self.assertRaises((argparse.ArgumentError, ValueError)):
            adt_cli._parser((group((lambda sp: sp.add_parser('open'),)),))

    def selected_store_fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix='co-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve()
        home = workspace / 'home'
        home.mkdir(mode=0o700)
        env = patch.dict(os.environ, {'HOME': str(home), 'XDG_RUNTIME_DIR': ''})
        env.start()
        self.addCleanup(env.stop)
        password_home = patch.object(submit, '_home', return_value=str(home))
        password_home.start()
        self.addCleanup(password_home.stop)
        production = store_layout.production_root()
        selected = workspace / 'selected store'

        self.ready_paths = {}
        self.urls = {}
        identities = set()
        from relay_engine.api_service import ApiService

        for root in (production, selected):
            root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with patch.object(submit, '_home', return_value=str(workspace)):
                admission = service_fixture.admitted_store_fixture(root, service_fixture.P, project_name='console')
                identities.add(admission.idle.store)
                service = ApiService(str(root), service_fixture.P, clock=lambda: 0, bridge_port=0,
                                     notifier_mode='off', admission=admission.receipt)
                ready = Path(service.start())
            loop = Thread(target=service.serve_forever)
            loop.start()
            self.addCleanup(loop.join, 5)
            self.addCleanup(service.stop)
            origin = json.loads(ready.read_bytes())['bridge_origin']
            page = ready.with_name('open.html').read_bytes()
            self.assertTrue(page.startswith(_PREFIX + origin.encode() + b'/#launch='))
            self.assertTrue(page.endswith(_SUFFIX))
            self.ready_paths[root] = ready
            self.urls[root] = page[len(_PREFIX):-len(_SUFFIX)].decode('ascii')
        self.assertEqual(len(identities), 2)
        self.opener = Mock(return_value=SimpleNamespace(returncode=0))
        with patch.object(subprocess, 'run', self.opener):
            self.group = load_launcher()['console_group']()
        return production, selected

    def invoke_console(self, *argv):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            try:
                status = adt_cli.main(['console', 'open', *argv], groups=(self.group,))
            except SystemExit as exit_status:
                status = exit_status.code
        return status, stdout.getvalue(), stderr.getvalue()

    def held_ready_fixture(self):
        from relay_engine.api_service import ApiService

        temporary = tempfile.TemporaryDirectory(prefix='cr-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve()
        home = workspace / 'home'
        home.mkdir(mode=0o700)
        env = patch.dict(os.environ, {'HOME': str(home), 'XDG_RUNTIME_DIR': ''})
        env.start()
        self.addCleanup(env.stop)
        root = workspace / 'store'
        with patch.object(submit, '_home', return_value=str(workspace)):
            admission = service_fixture.admitted_store_fixture(root, service_fixture.P, project_name='console')
        hold = Event()
        hold.set()
        admits = ApiService._admits
        admission_patch = patch.object(ApiService, '_admits',
                                       lambda service: not hold.is_set() and admits(service))
        admission_patch.start()
        self.addCleanup(admission_patch.stop)
        service = ApiService(str(root), service_fixture.P, clock=lambda: 0, bridge_port=0,
                             notifier_mode='off', admission=admission.receipt)
        self.addCleanup(service.stop)
        ready = Path(service.start())
        origin = json.loads(ready.read_bytes())['bridge_origin']

        def release():
            hold.clear()
            loop = Thread(target=service.serve_forever)
            self.addCleanup(loop.join, 5)
            self.addCleanup(service.stop)
            loop.start()

        self.opener = Mock(return_value=SimpleNamespace(returncode=0))
        with patch.object(subprocess, 'run', self.opener):
            self.group = load_launcher()['console_group']()
        return ready, origin, release

    @contextmanager
    def ready_probe_witness(self, path):
        records = []
        results = []
        base = socket.socket

        class RecordingSocket(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.events = []

            def connect(self, address):
                if address == path:
                    records.append(self.events)
                try:
                    outcome = super().connect(address)
                except OSError:
                    self.events.append(('connect', 'error'))
                    raise
                self.events.append(('connect', 'ok'))
                return outcome

            def settimeout(self, value):
                self.events.append(('timeout', value))
                return super().settimeout(value)

            def sendall(self, data, *args):
                self.events.append(('sendall', data))
                return super().sendall(data, *args)

            def recv(self, *args):
                try:
                    data = super().recv(*args)
                except TimeoutError:
                    self.events.append(('recv', 'timeout'))
                    raise
                self.events.append(('recv', 'data' if data else 'empty'))
                return data

        with ExitStack() as stack:
            stack.enter_context(patch.object(socket, 'socket', RecordingSocket))
            resolve = getattr(lifecycle_paths, 'ready_target', None)
            if resolve is not None:
                def record(ready_path):
                    result = resolve(ready_path)
                    results.append(result)
                    return result

                stack.enter_context(patch.object(lifecycle_paths, 'ready_target', record))
            yield records, results

    def assert_ready_probe_request(self, records):
        self.assertEqual(len(records), 1)
        events, = records
        self.assertEqual([value for kind, value in events if kind == 'connect'], ['ok'])
        sent, = [value for kind, value in events if kind == 'sendall']
        self.assertEqual(int.from_bytes(sent[:4], 'big'), len(sent) - 4)
        self.assertEqual(json.loads(sent[4:])['op'], 'protocol.connect')
        timeouts = [value for kind, value in events if kind == 'timeout']
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < value <= 0.5 for value in timeouts))
        return [value for kind, value in events if kind == 'recv']

    def assert_ready_identity_fault(self, results):
        self.assertEqual(len(results), 1)
        result, = results
        self.assertIsInstance(result, lifecycle_paths.ConsoleTargetUnavailable)
        self.assertEqual(result.reason, 'identity_unavailable')
        return result.identity_fault

    def assert_ready_refusal(self, status, output, error, words=_READY_UNAVAILABLE):
        self.assertEqual(status, 1)
        self.assertFalse(output)
        self.assertEqual(error, words)
        self.opener.assert_not_called()

    def assert_held_ready_refusal(self, ready, *options):
        path = json.loads(ready.read_bytes())['socket']
        with self.ready_probe_witness(path) as (records, results):
            status, output, error = self.invoke_console('--ready', str(ready), *options)
        if not options and status == 0:
            self.assertEqual(self.opener.call_count, 1)
        self.assert_ready_refusal(status, output, error)
        self.assertEqual(self.assert_ready_probe_request(records), ['timeout'])
        self.assertIsInstance(self.assert_ready_identity_fault(results), api_client.EndpointUnavailable)

    def test_console_open_ready_print_refuses_inside_start_window(self):
        ready, origin, _ = self.held_ready_fixture()
        connection = HTTPConnection(urlsplit(origin).netloc, timeout=2)
        self.addCleanup(connection.close)
        connection.request('GET', '/')
        # Inspect the first response byte so a reset after a status line cannot
        # be confused with an unadmitted connection that never sent a response.
        try:
            first_byte = connection.sock.recv(1)
        except ConnectionResetError:
            print('held bridge GET: connection reset before status')
        else:
            self.assertEqual(first_byte, b'', 'held bridge must not send an HTTP response')
            print('held bridge GET: end of stream before status')
        self.assert_held_ready_refusal(ready, '--print')

    def test_console_open_ready_opening_refuses_inside_start_window(self):
        ready, _, _ = self.held_ready_fixture()
        self.assert_held_ready_refusal(ready)

    def test_console_open_ready_after_release(self):
        ready, origin, release = self.held_ready_fixture()
        release()
        status, output, error = self.invoke_console('--ready', str(ready), '--print')
        self.assertEqual(status, 0)
        self.assertFalse(error)
        self.opener.assert_not_called()
        launch = urlsplit(output.rstrip('\n'))
        self.assertEqual(f'{launch.scheme}://{launch.netloc}', origin)
        self.assertEqual(launch.path, '/')
        self.assertTrue(launch.fragment.startswith('launch='))
        connection = HTTPConnection(launch.netloc, timeout=2)
        self.addCleanup(connection.close)
        connection.request('GET', launch.path)
        with connection.getresponse() as response:
            self.assertEqual(response.status, 200)
            response.read()
        status, output, error = self.invoke_console('--ready', str(ready))
        self.assertEqual((status, output, error), (0, _HINT, ''))
        page = str(ready.with_name('open.html'))
        argv = ['/usr/bin/open', '--', page] if sys.platform == 'darwin' else ['xdg-open', page]
        self.opener.assert_called_once_with(argv)

    @contextmanager
    def ready_variant(self, path, data):
        with path.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
        try:
            yield path
        finally:
            path.unlink()

    def test_console_open_ready_endpoint_refusals(self):
        production, selected = self.selected_store_fixture()
        ready = self.ready_paths[selected]
        live = json.loads(ready.read_bytes())
        other_store = json.loads(self.ready_paths[production].read_bytes())['store_id']
        with tempfile.TemporaryDirectory(prefix='rs-', dir='/tmp') as temporary, ExitStack() as stack:
            private = Path(temporary).resolve()
            stale_socket = str(private / 'api.sock')
            child = stack.enter_context(subprocess.Popen(['/bin/sleep', '30']))
            stack.callback(child.terminate)
            cases = (
                ('refused', private / 'ready.json', dict(live, socket=stale_socket)),
                ('absent', private / 'ready.json', dict(live, socket=stale_socket)),
                ('wrong_process', ready.with_name('wrong-process.json'), dict(live, pid=child.pid)),
                ('foreign', ready.with_name('foreign.json'), dict(live, store_id=other_store)),
            )
            for case, path, data in cases:
                if case == 'refused':
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound:
                        bound.bind(stale_socket)
                elif case == 'absent':
                    Path(stale_socket).unlink()
                with self.ready_variant(path, data):
                    api_client.read_ready_file(str(path))
                    for options in (('--print',), ()):
                        with self.subTest(case=case, options=options):
                            self.opener.reset_mock()
                            with self.ready_probe_witness(data['socket']) as (records, results):
                                status, output, error = self.invoke_console('--ready', str(path), *options)
                            self.assert_ready_refusal(status, output, error)
                            fault = self.assert_ready_identity_fault(results)
                            if case == 'foreign':
                                self.assertIn('data', self.assert_ready_probe_request(records))
                                self.assertIsInstance(fault, api_client.EndpointUnavailable)
                                self.assertEqual(fault.outcome, 'endpoint_mismatch')
                            else:
                                self.assertEqual(len(records), 1)
                                events, = records
                                stage = 'ok' if case == 'wrong_process' else 'error'
                                self.assertEqual([value for kind, value in events if kind == 'connect'], [stage])
                                self.assertFalse(any(kind == 'sendall' for kind, _ in events))
                                self.assertIsInstance(fault, ValueError if case == 'wrong_process' else OSError)

    def test_console_open_ready_file_faults_keep_page_words(self):
        _, selected = self.selected_store_fixture()
        ready = self.ready_paths[selected]
        other_schema = json.loads(ready.read_bytes())
        other_schema['schema_digest'] = ('a' if other_schema['schema_digest'][0] != 'a' else 'b') + (
            other_schema['schema_digest'][1:])
        with self.ready_variant(ready.with_name('other-schema.json'), other_schema) as mismatch:
            api_client.read_ready_file(str(mismatch))
            paths = (ready.with_name('missing.json'), mismatch)
            observed = []
            for path in paths:
                for options in (('--print',), ()):
                    with self.subTest(path=path.name, options=options):
                        self.opener.reset_mock()
                        with self.ready_probe_witness(other_schema['socket']) as (records, _):
                            status, output, error = self.invoke_console('--ready', str(path), *options)
                        self.assert_ready_refusal(status, output, error, _READY_PAGE_FAULT)
                        self.assertEqual(records, [])
                        observed.append(path)
            # A command red must not be obscured by any direct call of the new helper.
            if observed == [path for path in paths for _ in range(2)]:
                for path in paths:
                    with self.subTest(helper=path.name):
                        with self.ready_probe_witness(other_schema['socket']) as (records, results):
                            lifecycle_paths.ready_target(str(path))
                        self.assertEqual(records, [])
                        result, = results
                        self.assertIsInstance(result, lifecycle_paths.ReadyFileInvalid)
                        self.assertIsInstance(result.fault, api_client.LocalRefusal)

    def test_console_open_selects_store(self):
        production, selected = self.selected_store_fixture()
        with patch.object(lifecycle_paths, 'console_target', wraps=lifecycle_paths.console_target) as resolve:
            status, output, error = self.invoke_console('--store-root', str(selected))
            self.assertEqual(status, 0, 'selected Store requires the --store-root selector')
            self.assertTrue(output == _HINT and not error)
            resolve.assert_called_once_with(str(selected))
            page = str(self.ready_paths[selected].with_name('open.html'))
            expected = ['/usr/bin/open', '--', page] if os.sys.platform == 'darwin' else ['xdg-open', page]
            self.assertEqual(self.opener.call_count, 1)
            self.assertTrue(self.opener.call_args.args == (expected,), 'opener must receive only the page path')
            resolve.reset_mock()
            self.opener.reset_mock()
            status, output, error = self.invoke_console('--print')
            self.assertEqual(status, 0)
            self.assertTrue(output == self.urls[production] + '\n' and not error)
            resolve.assert_called_once_with(str(production))
            resolve.reset_mock()
            status, output, error = self.invoke_console('--ready', str(self.ready_paths[selected]), '--print')
            self.assertEqual(status, 0)
            self.assertTrue(output == self.urls[selected] + '\n' and not error)
            resolve.assert_not_called()
            self.opener.assert_not_called()
            status, output, error = self.invoke_console(
                '--store-root', str(selected), '--ready', str(self.ready_paths[production]))
            self.assertEqual(status, 2)
            self.assertTrue(not output and 'not allowed with argument' in error)
            resolve.assert_not_called()
            self.opener.assert_not_called()

    def test_console_open_selected_store_refusals(self):
        _, selected = self.selected_store_fixture()
        missing = selected.with_name('missing catalogue')
        missing.mkdir(mode=0o700)
        pages = {p.with_name('open.html'): p.with_name('open.html').read_bytes()
                 for p in self.ready_paths.values()}
        diagnostic = lifecycle_state.HeldDiagnosticObservation(
            'a' * 32, 'b' * 64, 'c' * 64, ('catalogue_unopenable',))

        def refusal(root, status_expected, *, held=None, fault=None):
            root = str(root)
            observed = []
            real_resolve = lifecycle_paths.console_target
            def trace(candidate):
                result = real_resolve(candidate)
                observed.append(result)
                return result
            with ExitStack() as guards:
                resolve = guards.enter_context(patch.object(
                    lifecycle_paths, 'console_target', side_effect=trace))
                key = guards.enter_context(patch.object(api_client, 'read_key_file'))
                client = guards.enter_context(patch.object(api_client.ApiClient, 'open'))
                read = guards.enter_context(patch.object(api_client, 'read_ready_file'))
                # The loader captures imports and the opener at definition time.
                with patch.object(subprocess, 'run', self.opener):
                    self.group = load_launcher()['console_group']()
                if held is not None:
                    probe = guards.enter_context(patch.object(
                        lifecycle_state, 'probe_held_diagnostic', return_value=held))
                    identity = guards.enter_context(patch.object(store_layout, 'read_store_id'))
                status, output, error = self.invoke_console('--store-root', root, '--print')
                self.assertEqual(status, status_expected, 'ordinary caller must map the selected Store refusal')
                resolve.assert_called_once_with(root)
                if fault is not None:
                    self.assertEqual(observed[0].reason, 'identity_unavailable')
                    local = observed[0].identity_fault
                    self.assertEqual(getattr(local, 'cause', str(local)), fault)
                    self.assertNotIn(repr(local), error)
                if held is not None:
                    probe.assert_called_once_with(root)
                    identity.assert_not_called()
                self.assertEqual(output, '')
                self.assertIn('Check the operator launch terminal for the refusal.', error)
                command = f'adt engine run --store-root {shlex.quote(root)} --project-id PROJECT'
                self.assertIn(command, error)
                read.assert_not_called()
                key.assert_not_called()
                client.assert_not_called()
                self.opener.assert_not_called()
                self.assertTrue(all(path.read_bytes() == before for path, before in pages.items()))

        refusal(selected, 75, held=lifecycle_state.HeldProbeOutcome('verified', diagnostic))
        for kind in ('invalid_unavailable', 'timed_out'):
            refusal(selected, 75, held=lifecycle_state.HeldProbeOutcome(kind))
        refusal(missing, 1, fault="[Errno 2] No such file or directory: '.adt-store-live.json'")
        directory = self.ready_paths[selected].parent
        directory.chmod(0o755)
        try:
            refusal(selected, 1, fault='diagnostic_metadata_unsafe')
        finally:
            directory.chmod(0o700)
