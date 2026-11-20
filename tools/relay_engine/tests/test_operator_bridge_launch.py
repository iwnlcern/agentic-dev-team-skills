"""Private launch page and the actual adt Console parser."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import os
import re
import runpy
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

from relay_engine import (
    adt_cli, api_client, api_operator_bridge, lifecycle_paths, lifecycle_state, store_layout, submit,
)

from relay_engine.operator_queue.notifier import DeliveryCancelled, NotifierIssueUnavailable


_PREFIX = b'<!doctype html><meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="0;url='
_SUFFIX = b'">'
_HINT = 'If the page did not open, run adt console open --print and paste the address.\n'


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
        temporary = tempfile.TemporaryDirectory(prefix='console-store-')
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
        for root, kind in ((production, 'production'), (selected, 'isolated')):
            connection = store_layout.open_catalogue(root, kind=kind)
            connection.close()
            store_id = store_layout.read_store_id(root)
            identities.add(store_id)
            ready = Path(lifecycle_paths.ready_file_path(
                store_id, platform=os.sys.platform, environ=os.environ, home=str(home)))
            ready.parent.parent.mkdir(mode=0o700, exist_ok=True)
            ready.parent.mkdir(mode=0o700)
            bridge = api_operator_bridge._new_operator_bridge(
                port=0, run_id='fixture-run', store_id=store_id, clock_ms=lambda: 0,
                dispatch=Mock(side_effect=AssertionError('application dispatch')),
                supervisor_post=None,
                open_backend=Mock(side_effect=AssertionError('discovery backend')),
                runtime_dir=ready.parent)
            self.addCleanup(bridge.stop)
            origin = bridge.start()
            ready.write_text(json.dumps({
                'format': 'adt-ready-v1', 'socket': str(ready.with_name('api.sock')),
                'store_id': store_id, 'build_id': 'b' * 64, 'schema_digest': 'c' * 64,
                'pid': os.getpid(),
            }))
            ready.chmod(0o600)
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
                self.assertTrue(not output and 'status' in error and 'doctor' in error)
                self.assertIn(shlex.quote(root), error)
                read.assert_not_called()
                key.assert_not_called()
                client.assert_not_called()
                self.opener.assert_not_called()
                self.assertTrue(all(path.read_bytes() == before for path, before in pages.items()))

        refusal(selected, 75, held=lifecycle_state.HeldProbeOutcome('verified', diagnostic))
        for kind in ('invalid_unavailable', 'timed_out'):
            refusal(selected, 75, held=lifecycle_state.HeldProbeOutcome(kind))
        refusal(missing, 1, fault='missing')
        directory = self.ready_paths[selected].parent
        directory.chmod(0o755)
        try:
            refusal(selected, 1, fault='runtime_dir_unsafe')
        finally:
            directory.chmod(0o700)
