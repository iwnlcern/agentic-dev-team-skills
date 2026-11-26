"""Exercise the checked launcher, guarded bridge and real Store on one scratch project."""
import base64
from contextlib import closing, contextmanager
from dataclasses import asdict
import itertools
import fcntl
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket as network
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from relay_engine import (
    api_client, api_operator_bridge, api_service, jcs, lifecycle_paths, queries, store_layout, submit,
)
from relay_engine.operator_queue import records
from relay_engine.tests import test_api_service as service_fixture
from relay_engine.tests.test_api_service import P
from relay_engine.tests.test_submit import draft


ADMISSION_CEILING_SECONDS = 20
HOST_PROGRESS_CEILING_SECONDS = 20


class OperatorDaemonJoinTests(unittest.TestCase):
    start = service_fixture.ApiServiceTests.start
    connect = service_fixture.ApiServiceTests.connect
    sign_in = service_fixture.ApiServiceTests.sign_in
    rows = service_fixture.ApiServiceTests.rows

    def setUp(self):
        service_fixture.ApiServiceTests.setUp(self)
        self.service.bridge_port = 0
        self.service._notifier_mode = 'configured'
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''})
        env.start()
        self.addCleanup(env.stop)
        self.assets = self.base / 'page'
        self.assets.mkdir()
        fixture = Path(__file__).with_name('console_page_walk') / 'smoke_page' / 'index.html'
        self.assets.joinpath('index.html').write_bytes(fixture.read_bytes())
        self.assets.joinpath('page.css').write_text('#smoke {display: block;}')
        self.assets.joinpath('page.js').write_text('document.querySelector("#smoke");')
        self.asset_patch = patch.object(api_operator_bridge, '_PAGE_DIRECTORY', self.assets)

    def request(self, origin, path, *, payload=None, headers=None):
        connection = HTTPConnection(urlsplit(origin).netloc, timeout=4)
        try:
            connection.request('GET' if payload is None else 'POST', path,
                               None if payload is None else jcs.jcs_encode(payload), headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def admitted_request(self, origin, path, daemon, root) -> tuple:
        # ready.json can be published before the bridge admits requests.
        # Until then the bridge closes connections without answering.
        started = time.monotonic()
        deadline = started + ADMISSION_CEILING_SECONDS
        closed_unanswered = 0

        def observation(outcome):
            return json.dumps({
                'path': path,
                'outcome': outcome,
                'closed_unanswered': closed_unanswered,
                'elapsed_seconds': time.monotonic() - started,
                'service_exit': daemon.poll(),
                'locator_exists': (root / lifecycle_paths._LIVE_LOCATOR).exists(),
            })

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(observation('no response before the ceiling'))
            connection = HTTPConnection(urlsplit(origin).netloc, timeout=min(4, remaining))
            try:
                try:
                    connection.request('GET', path)
                    response = connection.getresponse()
                except (ConnectionResetError, BrokenPipeError):
                    closed_unanswered += 1
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        self.fail(observation('attempt timed out at the ceiling'))
                    raise
                else:
                    with response:
                        if time.monotonic() >= deadline:
                            self.fail(observation('response after the ceiling'))
                        print(observation('response'))
                        return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
            time.sleep(max(0, min(0.05, deadline - time.monotonic())))

    def wait_until(self, observe):
        deadline = time.monotonic() + 6
        while True:
            result = observe()
            if result:
                return result
            self.assertLess(time.monotonic(), deadline, 'timed out waiting for durable state')
            time.sleep(0.05)

    def wait_for_host_progress(self, observe, what) -> object:
        started = time.monotonic()
        while True:
            outcome = observe()
            elapsed = time.monotonic() - started
            if outcome:
                print(json.dumps({'host_wait': {'what': what, 'elapsed_seconds': round(elapsed, 3)}}))
                return outcome
            if elapsed >= HOST_PROGRESS_CEILING_SECONDS:
                self.fail(f'host delay: {what} not reached after {elapsed:.1f} s '
                          f'(ceiling {HOST_PROGRESS_CEILING_SECONDS} s)')
            time.sleep(0.05)

    def test_host_progress_wait_reports_host_delay_at_ceiling(self):
        what = 'never observed'
        ticks = 0
        observations = []

        def monotonic():
            return ticks * 0.05

        def advance(_seconds):
            nonlocal ticks
            ticks += 1

        def observe():
            observations.append(monotonic())
            return False

        with patch.object(time, 'monotonic', side_effect=monotonic), patch.object(
                time, 'sleep', side_effect=advance) as sleep:
            with self.assertRaisesRegex(AssertionError, (
                    r'^host delay: never observed not reached after 20\.0 s \(ceiling 20 s\)$')):
                self.wait_for_host_progress(observe, what)
        self.assertEqual(observations[-1], 20.0)
        self.assertEqual(sleep.call_count, 400)
        self.assertTrue(all(call.args == (0.05,) for call in sleep.call_args_list))

    def check_assets(self, origin, directory):
        for route, filename, media in (
                ('/', 'index.html', 'text/html'), ('/page.css', 'page.css', 'text/css'),
                ('/page.js', 'page.js', 'text/javascript')):
            status, headers, body = self.request(origin, route)
            self.assertEqual((status, body), (200, directory.joinpath(filename).read_bytes()))
            self.assertTrue(headers['Content-Type'].startswith(media))
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def browser_reply(self, module, launch, payload):
        script = """
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { chromium } = require(process.argv[1]);
(async () => {
  const { launch, payload } = JSON.parse(readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(8000);
    await page.goto(launch);
    await page.getByRole('button', { name: 'Answer: Local decision' }).click();
    await page.getByRole('heading', { name: 'Local decision', level: 1 }).waitFor();
    await page.getByText('Can we proceed?', { exact: true }).waitFor();
    await page.getByText('from platform.planner', { exact: true }).waitFor();
    assert.equal(new URL(page.url()).hash, '');
    const reply = page.waitForResponse(response =>
      response.url().endsWith('/api/op/queue.answer') && response.request().method() === 'POST');
    await page.getByLabel('Your answer').fill(payload.answer.text);
    await page.getByRole('button', { name: 'Send answer' }).click();
    const response = await reply;
    const actual = response.request().postDataJSON();
    assert.deepEqual(actual, { ...payload, operation_key: actual.operation_key });
    assert.match(actual.operation_key, /^[0-9a-f]{32}$/);
    assert.equal(response.status(), 200);
    const answered = await response.json();
    assert.equal(answered.result.state, 'applying');
    await page.getByRole('heading', { name: 'Recorded answer' }).waitFor();
    await page.getByText('Answer delivered.', { exact: true }).waitFor();
    process.stdout.write(JSON.stringify(actual));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
"""
        output = subprocess.check_output(
            ['node', '-e', script, module], input=json.dumps({'launch': launch, 'payload': payload}),
            text=True, timeout=30)
        return json.loads(output)

    def test_real_guarded_reply(self):
        notifications = []
        self.service.queue_notifier = ('notify-send', notifications.append)
        ready, data = self.start()
        origin = data['bridge_origin']
        cli = Path(__file__).resolve().parents[2] / 'adt'
        selected = subprocess.check_output(
            [str(cli), 'console', 'open', '--store-root', str(self.root), '--print'], timeout=6)
        explicit = subprocess.check_output(
            [str(cli), 'console', 'open', '--ready', str(ready), '--print'], timeout=6)
        self.assertTrue(selected.startswith(origin.encode() + b'/#launch='))
        self.assertEqual(selected, explicit)
        self.assertEqual(str(ready), lifecycle_paths.console_target(str(self.root)).ready_path)
        checked = api_client.ApiClient.open(api_client.read_ready_file(str(ready)))
        checked.close()

        with self.asset_patch:
            self.check_assets(origin, self.assets)
        self.check_assets(origin, Path(api_operator_bridge.__file__).with_name('console_page'))
        for target in ('/open.html', '/index.html', '/%70age.js'):
            self.assertNotEqual(self.request(origin, target)[0], 200)
        self.assertNotEqual(self.request(origin, '/', headers={'Host': 'invalid.test'})[0], 200)
        status, _, body = self.request(origin, '/api/connect?min_protocol=3.0&max_protocol=3.0')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['result']['schema_digest'], data['schema_digest'])

        socket = self.connect(data)
        self.sign_in(socket)
        raw = draft(kind='QUESTION', to='operator', needs='answer', subject='Local decision', body='Can we proceed?\n')
        submitted = socket.call('relay.submit', {'draft_base64': base64.b64encode(raw).decode(),
                                                 'submission_key': 'question'})
        self.assertIn('result', submitted)
        question = submitted['result']['relay_id']

        headers = {'Origin': origin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'}
        launch = selected.decode().strip().split('#launch=', 1)[1]
        status, session_headers, body = self.request(origin, '/api/session', payload={'launch': launch},
                                                     headers=headers)
        self.assertEqual(status, 200)
        signed = {**headers, 'Cookie': session_headers['Set-Cookie'].split(';', 1)[0],
                  'X-ADT-CSRF': json.loads(body)['csrf_token']}
        catalogue = self.service.writer
        item = self.wait_until(lambda: catalogue.read_catalogue(lambda conn: conn.execute(
            'SELECT item_id,revision FROM queue_items WHERE source_relay_id=?', (question,)).fetchone()))
        self.wait_until(lambda: notifications)
        self.assertEqual(len(notifications), 1)

        status, _, body = self.request(origin,
            '/api/read/queue.list?scope=machine&state=pending&kind=question&page_size=100', headers=signed)
        self.assertEqual(status, 200, body)
        self.assertEqual([row['item_id'] for row in json.loads(body)['result']['items']], [item[0]])
        status, _, body = self.request(origin,
            '/api/read/queue.get?item_id=' + item[0] + '&history_page_size=100', headers=signed)
        self.assertEqual(status, 200, body)
        view = json.loads(body)['result']['item']
        self.assertEqual((view['answer_to'], view['asking_seat_id']), ('platform.planner', 'planner'))
        self.assertIn('Can we proceed?', view['texts'])
        for suffix in ('&unexpected=yes', '&state_filter=pending', '&page_size=1'):
            status, _, _ = self.request(origin,
                '/api/read/queue.list?scope=machine&state=pending&kind=question&page_size=100' + suffix,
                headers=signed)
            self.assertNotEqual(status, 200)
        for size in ('0', '01', '1001', '1.5'):
            status, _, _ = self.request(origin, '/api/read/queue.list?scope=machine&page_size=' + size,
                                        headers=signed)
            self.assertNotEqual(status, 200)
        page = '/api/read/queue.list?scope=machine&state=pending&kind=question&page_size=1'
        another = socket.call('relay.submit', {'draft_base64': base64.b64encode(
            draft(kind='QUESTION', to='operator', needs='answer', body='Second question.\n',
                  bundle='second-question')).decode(), 'submission_key': 'another-question'})
        self.assertIn('result', another)
        other_question = another['result']['relay_id']
        self.wait_until(lambda: catalogue.read_catalogue(lambda conn: conn.execute(
            'SELECT item_id FROM queue_items WHERE source_relay_id=?', (other_question,)).fetchone()))
        status, _, body = self.request(origin, page, headers=signed)
        self.assertEqual(status, 200, body)
        first = json.loads(body)['result']
        self.assertEqual(first['items'][0]['source_relay_id'], question)
        self.assertTrue(first['continuation'])
        status, _, body = self.request(origin, page + '&continuation=' + first['continuation'], headers=signed)
        self.assertEqual(status, 200, body)
        second = json.loads(body)['result']
        self.assertEqual(second['items'][0]['source_relay_id'], other_question)
        self.assertIsNone(second['continuation'])

        payload = {'item_id': item[0], 'expected_revision': str(item[1]), 'operation_key': 'answer',
                   'answer': {'text': 'Proceed with the synthetic check.'}}

        def queue_snapshot():
            return catalogue.read_catalogue(lambda conn: tuple(conn.execute(
                'SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
                for name in ('queue_items', 'queue_answers', 'queue_events')))
        before = queue_snapshot()
        before_relays = self.rows('SELECT id FROM relays ORDER BY handle')
        status, _, _ = self.request(origin, '/api/op/queue.answer', payload=payload, headers=headers)
        self.assertEqual(status, 403)
        self.assertEqual(queue_snapshot(), before)
        self.assertEqual(self.rows('SELECT id FROM relays ORDER BY handle'), before_relays)
        self.assertEqual(socket.call('queue.answer', payload)['error']['cause']['kind'], 'not_signed_in')

        browser_module = os.environ.get('ADT_JOIN_PLAYWRIGHT')
        if browser_module:
            fresh_launch = subprocess.check_output(
                [str(cli), 'console', 'open', '--store-root', str(self.root), '--print'],
                text=True, timeout=6).strip()
            payload = self.browser_reply(browser_module, fresh_launch, payload)
        else:
            status, _, body = self.request(origin, '/api/op/queue.answer', payload=payload, headers=signed)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)['result']['state'], 'applying')
        self.wait_until(lambda: catalogue.read_catalogue(lambda conn: conn.execute(
            "SELECT item_id FROM queue_items WHERE item_id=? AND state='resolved'", (item[0],)).fetchone()))
        history = '/api/read/queue.get?item_id=' + item[0] + '&history_page_size=1'
        status, _, body = self.request(origin, history, headers=signed)
        self.assertEqual(status, 200, body)
        first = json.loads(body)['result']
        self.assertEqual(first['events'][0]['event_kind'], 'raised')
        self.assertTrue(first['continuation'])
        status, _, body = self.request(origin, history + '&history_continuation=' + first['continuation'],
                                       headers=signed)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['result']['events'][0]['event_kind'], 'answered')
        self.assertNotEqual(self.request(origin, history + '&continuation=value', headers=signed)[0], 200)
        replies = self.rows("SELECT id,parent_id FROM relays WHERE sender_special='operator'")
        self.assertEqual(len(replies), 1)
        reply, parent = replies[0]
        self.assertEqual(parent, question)
        self.assertEqual(self.rows('SELECT role,seat_id FROM recipients WHERE relay_id=?', (reply,)),
                         [('to', 'planner')])
        self.assertEqual(self.rows("SELECT answered_by FROM recipients WHERE relay_id=? AND special='operator'",
                                   (question,)), [(reply,)])
        ledger = store_layout.open_ledger(self.root, P)
        try:
            delivered = queries.read(ledger, reply, 'planner', self.service.clock())
            self.assertIn('Proceed with the synthetic check.', repr(delivered))
        finally:
            ledger.close()
        status, _, body = self.request(origin, '/api/op/queue.answer', payload=payload, headers=signed)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['result']['replayed'], 'true')
        self.assertEqual(self.rows("SELECT id FROM relays WHERE sender_special='operator'"), [(reply,)])

        self.service.stop()
        self.assertFalse(ready.parent.exists())
        with self.assertRaises(OSError):
            self.request(origin, '/api/session', headers=signed)

    def test_stable_collision_preserves_existing_directory(self):
        store_id = store_layout.read_store_id(self.root)
        runtime = lifecycle_paths.create_runtime_dir(store_id, platform=os.sys.platform,
                                                     environ=os.environ, home=str(self.home))
        sentinel = runtime / 'ready.json'
        sentinel.write_text('unowned')
        with self.assertRaisesRegex(self.m.ServiceStartRefused, 'runtime_dir_unsafe'):
            self.service.start()
        self.assertEqual(sentinel.read_text(), 'unowned')
        self.assertIsNone(self.service.writer)

    def test_cli_bridge_publishes_and_retires_on_term(self):
        cli = Path(__file__).resolve().parents[2] / 'adt'
        program = self.base / 'terminal-notifier'
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o700)
        with service_fixture.private_idle(self.admitted) as (idle_fd, environment), subprocess.Popen(
            [str(cli), 'engine', 'serve', '--store-root', str(self.root), '--project-id', P,
             '--bridge-port', '0', '--terminal-notifier', str(program)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, pass_fds=(idle_fd,),
        ) as daemon:
            try:
                target = self.wait_for_host_progress(lambda: self.live_target(self.root),
                                                     'service discovery after cold start')
                ready = Path(target.ready_path)
                self.wait_until(ready.exists)
                origin = json.loads(ready.read_text())['bridge_origin']
                self.assertTrue(ready.with_name('open.html').is_file())
                self.assertEqual(self.request(origin, '/')[0], 200)
                with network.socket(network.AF_UNIX, network.SOCK_STREAM) as peer:
                    peer.settimeout(3)
                    peer.connect(str(ready.with_name('api.sock')))
                    started = time.monotonic()
                    daemon.send_signal(signal.SIGTERM)
                    _, stderr = daemon.communicate(timeout=3)
                    self.assertEqual(daemon.returncode, 0, stderr)
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(peer.recv(1), b'')
                self.assertFalse(ready.parent.exists())
                with self.assertRaises(OSError):
                    self.request(origin, '/')
            finally:
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.communicate(timeout=3)

    def live_target(self, root):
        target = lifecycle_paths.console_target(str(root))
        return target if isinstance(target, lifecycle_paths.ConsoleTarget) else None

    def install_candidate(self):
        checker_path = Path(__file__).resolve().parents[2] / 'check-generate-plugins.py'
        spec = importlib.util.spec_from_file_location('check_generate_plugins', checker_path)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        return checker.install_tier('adt-orchestrator', self.base / 'installed') / 'tools' / 'adt'

    def test_installed_cli_delivers_and_reaps_held_notifier_on_term(self):
        launcher = self.install_candidate()
        cases = ('ordinary', 'finishing', 'commit_permitted', 'held')
        qualified = (sys.platform, os.uname().machine) in {('darwin', 'arm64'), ('linux', 'aarch64')}
        if not qualified:
            cases = ('unsupported',)

        for phase in cases:
            with self.subTest(phase=phase):
                self.check_installed_notifier_arm(launcher, phase)
        if qualified:
            for phase in ('pre_ready', 'before_final', 'overlap', 'late_invocation', 'late_return'):
                with self.subTest(phase=phase):
                    self.check_installed_custody_discriminator(launcher, phase)

    def verify_custodian_inventory(self, custodian, foreground, root, runtime):
        if sys.platform == 'darwin':
            files = subprocess.check_output(['/usr/sbin/lsof', '-Ffatn', '-p', str(custodian)],
                                            text=True, timeout=3)
            self.assertEqual(files.splitlines().count('tPIPE'), 1, files)
            self.assertEqual(files.splitlines().count('tunix'), 1, files)
            self.assertNotIn(str(root), files)
            self.assertNotIn(str(runtime), files)
            # Darwin lsof omits PIPE access. READY requires F_GETFL read-only
            # verification in the installed custodian.
            return

        def inventory(pid):
            proc = Path('/proc') / str(pid)
            records = []
            for fd in (proc / 'fd').iterdir():
                try:
                    name = os.readlink(fd)
                    lines = (proc / 'fdinfo' / fd.name).read_text().splitlines()
                except FileNotFoundError:
                    continue
                flags = next(int(line.split()[1], 8) for line in lines if line.startswith('flags:'))
                records.append((fd.name, name, flags))
            return records

        def settled_inventory():
            descriptors = [(fd, name, flags) for fd, name, flags in inventory(custodian) if int(fd) > 2]
            if len(descriptors) == 2 and all(flags & os.O_CLOEXEC for _, _, flags in descriptors):
                return descriptors
            return None

        # pass_fds makes the two descriptors inheritable for exec. Observe
        # them after the custodian restores CLOEXEC and while READY is parked.
        descriptors = self.wait_until(settled_inventory)
        self.assertEqual(len(descriptors), 2, descriptors)
        read_pipe, = [(name, flags) for _, name, flags in descriptors if name.startswith('pipe:')]
        self.assertEqual(read_pipe[1] & os.O_ACCMODE, os.O_RDONLY)
        self.assertTrue(read_pipe[1] & os.O_NONBLOCK)
        self.assertTrue(read_pipe[1] & os.O_CLOEXEC)
        self.assertEqual(sum(name.startswith('socket:') for _, name, _ in descriptors), 1)
        writes = [name for _, name, flags in inventory(foreground)
                  if name == read_pipe[0] and flags & os.O_ACCMODE == os.O_WRONLY]
        self.assertEqual(writes, [read_pipe[0]])

    def check_installed_notifier_arm(self, launcher, phase):
        arm = self.base / phase
        arm.mkdir()
        root = arm / 'store'
        keys = {}

        def seed(path):
            service_fixture.seed_service_store(path, keys, itertools.count(10000))
            with closing(store_layout.open_catalogue(path)) as catalogue, catalogue:
                catalogue.execute('INSERT INTO queue_notifier_health VALUES (1,2,7)').close()

        admission = service_fixture.admitted_store_fixture(root, P, project_name='service', seed=seed)
        home = arm / 'home'
        home.mkdir(mode=0o700)
        program = arm / 'terminal-notifier'
        started = arm / 'child.json'
        parked = arm / 'parked'
        release = arm / 'release'
        waiter = arm / 'waiter'
        program.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, sys, time
if {phase!r} == 'held':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
fds = []
for fd in range(3, 256):
    try:
        os.fstat(fd)
    except OSError:
        continue
    fds.append(fd)
path = pathlib.Path({str(started)!r})
path.with_suffix('.tmp').write_text(json.dumps([os.getpid(), os.getppid(), sys.argv[1:], fds]))
path.with_suffix('.tmp').replace(path)
if {phase!r} == 'held':
    time.sleep(15)
""")
        program.chmod(0o700)

        def snapshot(relay_id, *, stopped=False):
            catalogue = (store_layout.open_catalogue(root, kind='isolated') if stopped else
                         store_layout.open_catalogue_readonly(root))
            try:
                row = records._one(catalogue,
                    'SELECT n.item_id,n.revision,n.state,n.attempts,n.next_attempt_at_ms,n.last_error_class '
                    'FROM queue_notifications n JOIN queue_items i ON i.item_id=n.item_id '
                    'WHERE i.source_relay_id=?', (relay_id,))
                health = records._one(catalogue, 'SELECT * FROM queue_notifier_health')
                return tuple(row) if row is not None else None, tuple(health)
            finally:
                catalogue.close()

        args = ['engine', 'serve', '--store-root', str(root), '--project-id', P, '--bridge-port', '0',
                '--terminal-notifier', str(program)]
        environment = {'HOME': str(home), 'TMPDIR': str(self.temp), 'PYTHONDONTWRITEBYTECODE': '1'}
        command = [sys.executable, str(launcher), *args]
        if phase in {'finishing', 'commit_permitted'}:
            fixture = f"""
import pathlib, runpy, sys, threading, time
sys.path.insert(0, {str(launcher.parent)!r})
from relay_engine.api_writer import StoreWriter
from relay_engine.operator_queue import notifier
target = {phase!r}
parked = pathlib.Path({str(parked)!r})
release = pathlib.Path({str(release)!r})
waiter = pathlib.Path({str(waiter)!r})
receipt = threading.local()
old_delivered = notifier.notify_delivered
old_phase = StoreWriter._phase
old_submit = StoreWriter.submit_declared
def delivered(conn, note, now):
    receipt.active = True
    return old_delivered(conn, note, now)
def checkpoint(self, phase):
    if phase == target and getattr(receipt, 'active', False):
        receipt.active = False
        parked.touch()
        until = time.monotonic() + 5
        while not release.exists() and time.monotonic() < until:
            time.sleep(0.01)
    return old_phase(self, phase)
def submit(self, job):
    value = old_submit(self, job)
    if threading.current_thread().name == 'adt-queue-notify' and value.fault:
        waiter.write_text(value.fault)
    return value
notifier.notify_delivered = delivered
StoreWriter._phase = checkpoint
StoreWriter.submit_declared = submit
sys.argv = { [str(launcher), *args]!r}
runpy.run_path(sys.argv[0], run_name='__main__')
"""
            command = [sys.executable, '-c', fixture]

        with (service_fixture.private_idle(admission, environment) as (idle_fd, child_env),
              subprocess.Popen(command, cwd=arm, env=child_env, pass_fds=(idle_fd,), start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE) as daemon):
            try:
                store_id = admission.idle.store
                ready = Path(lifecycle_paths.ready_file_path(
                    store_id, platform=sys.platform, environ=environment, home=str(home)))
                self.wait_until(ready.exists)
                data = json.loads(ready.read_bytes())
                origin = data['bridge_origin']
                discovery = self.admitted_request(
                    origin, '/api/connect?min_protocol=3.0&max_protocol=3.0', daemon, root)
                self.assertEqual(discovery[0], 200)
                self.assertEqual(json.loads(discovery[2])['result']['schema_digest'], data['schema_digest'])
                peer = self.connect(data)
                self.sign_in(peer, keys=keys)
                raw = draft(kind='QUESTION', to='operator', needs='answer', subject='Local decision',
                            body='Can we proceed?\n')
                reply = peer.call('relay.submit', {'draft_base64': base64.b64encode(raw).decode(),
                                                   'submission_key': 'question'})
                self.assertIn('result', reply)
                question = reply['result']['relay_id']
                self.wait_until(lambda: snapshot(question)[0])
                expected_row = 'pending'
                expected_status = 75 if phase in {'finishing', 'commit_permitted', 'held'} else 0
                if phase == 'unsupported':
                    before = snapshot(question)
                    time.sleep(1.2)
                    self.assertEqual(snapshot(question), before)
                    self.assertFalse(started.exists())
                else:
                    self.wait_for_host_progress(started.exists, 'notifier program start after submit')
                    pid, custodian, argv, inherited = json.loads(started.read_bytes())
                    self.assertNotEqual(custodian, daemon.pid)
                    self.assertEqual(inherited, [])
                    self.assertEqual(argv, ['-title', '\\service: platform.planner', '-message', '\\Local decision'])
                    if phase == 'ordinary':
                        expected_row = 'delivered'
                        self.wait_until(lambda: snapshot(question)[0][2] == expected_row)
                    elif phase == 'held':
                        os.kill(pid, 0)
                        self.verify_custodian_inventory(custodian, daemon.pid, root, ready.parent)
                        print(json.dumps({'custodian': custodian, 'pipe_read_only': True,
                                          'private_control_only': True, 'helper_inherited_fds': inherited}))
                    else:
                        self.wait_until(parked.exists)
                        expected_row = 'delivered' if phase == 'commit_permitted' else 'pending'

                with (root / 'api-service.lock').open('r+b') as witness:
                    held = os.fstat(witness.fileno())
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(witness, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    sent = time.monotonic()
                    if phase == 'held':
                        self.assertNotEqual(os.getpgid(custodian), os.getpgid(daemon.pid))
                        os.killpg(daemon.pid, signal.SIGTERM)
                    else:
                        daemon.send_signal(signal.SIGTERM)
                    if phase in {'finishing', 'commit_permitted'}:
                        self.wait_until(waiter.exists)
                        fault = 'stop_commit_unknown' if phase == 'commit_permitted' else 'stop_unwritten'
                        self.assertEqual(waiter.read_text(), fault)
                        release.touch()
                    _, stderr = daemon.communicate(timeout=5)
                    elapsed = time.monotonic() - sent
                    self.assertEqual(daemon.returncode, expected_status, stderr)
                    self.assertLess(elapsed, 2 if expected_status == 0 else 5)
                    fcntl.flock(witness, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    after = (root / 'api-service.lock').stat()
                    self.assertEqual((after.st_dev, after.st_ino), (held.st_dev, held.st_ino))
                if expected_status == 0:
                    self.assertFalse(ready.parent.exists())
                if phase != 'unsupported':
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                row, _ = snapshot(question, stopped=True)
                self.assertEqual(row[1:4], (1, expected_row, 0))
                print(json.dumps({'installed': phase, 'build': data['build_id'], 'durable_row': expected_row,
                                  'status': expected_status, 'term_seconds': round(elapsed, 3),
                                  'same_inode_return': True}))
            finally:
                release.touch()
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.communicate(timeout=3)


    def check_installed_custody_discriminator(self, launcher, phase):
        arm = self.base / phase
        arm.mkdir()
        root = arm / 'store'
        keys = {}

        def seed(path):
            service_fixture.seed_service_store(path, keys, itertools.count(10000))
            with closing(store_layout.open_catalogue(path)) as catalogue, catalogue:
                catalogue.execute('INSERT INTO queue_notifier_health VALUES (1,2,7)').close()

        admission = service_fixture.admitted_store_fixture(root, P, project_name='service', seed=seed)
        home = arm / 'home'
        home.mkdir(mode=0o700)
        program = arm / 'terminal-notifier'
        program.write_text(f"""#!{sys.executable}
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(15)
""")
        program.chmod(0o700)
        parked, release = arm / 'parked', arm / 'release'
        issued, reaped = arm / 'issued', arm / 'reaped'
        custodian_reaped = arm / 'custodian-reaped'
        retiring, claimed, eof = arm / 'retiring', arm / 'claimed', arm / 'eof'
        after_exit = arm / 'after-exit'
        early = phase in {'pre_ready', 'before_final'}
        marker = arm / ('stop_entered' if phase == 'pre_ready' else 'retired')
        parked_exit = arm / 'parked_exit'
        custodian_identity = arm / 'custodian_identity'
        issued_identity = arm / 'issued_identity'

        def atomic_write(path, data):
            temporary = path.with_suffix('.tmp')
            temporary.write_text(data)
            os.replace(temporary, path)

        def read_optional(path):
            try:
                return path.read_text()
            except FileNotFoundError:
                return None

        def stop_facts(sent, end):
            marker_seconds = time.monotonic() - sent
            exit_record = read_optional(parked_exit)
            return {
                'marker': marker.exists(),
                'wait_end': end,
                'parked_entry_pid': parked_entry_pid,
                'release_text': read_optional(release),
                'parked_exit': json.loads(exit_record) if exit_record is not None else None,
                'daemon_status': daemon.poll(),
                'elapsed_seconds': round(marker_seconds, 3),
            }

        def departed(identity, label, *, pending=False):
            self.assertIsNotNone(identity, f'missing {label} identity')
            self.assertIsInstance(identity.get('birth'), str, f'unverified {label} birth')
            self.assertTrue(identity['birth'], f'empty {label} birth')
            pid = identity.get('pid')
            self.assertTrue(type(pid) is int and pid > 0, f'invalid {label} PID')
            try:
                observed, _ = lifecycle_paths._native_identity(pid)
            except (OSError, ValueError) as error:
                # Distinguish a departed PID from a live PID whose birth cannot be read.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return 'gone'
                self.fail(f'unverified {label} PID {pid}: {error}')
            if observed == identity['birth']:
                if pending:
                    return None
                self.fail(f'{label} PID {pid} survived with matching birth')
            return 'different-birth'

        def cleanup_facts(daemon, observed_stop):
            self.assertIsNotNone(daemon.returncode, 'daemon was not reaped')
            saved = read_optional(custodian_identity)
            custody = json.loads(saved) if saved is not None else None
            self.assertIsNotNone(custody, 'missing custodian identity')
            self.assertTrue(release.exists(), 'cleanup attempted before release')
            departure_started = time.monotonic()
            try:
                outcome = self.wait_for_host_progress(lambda: departed(custody, 'custodian', pending=True),
                                                      'custodian departure after release')
            except AssertionError as error:
                self.fail(f'{error}; custodian PID {custody.get("pid")} birth {custody.get("birth")}')
            departure_seconds = time.monotonic() - departure_started
            self.assertLess(departure_seconds, HOST_PROGRESS_CEILING_SECONDS,
                            f'custodian departure was not observed within the ceiling: {custody!r}')
            self.assertIn(outcome, ('gone', 'different-birth'))
            record = {'installed_custody_cleanup': phase, 'daemon_pid': daemon.pid,
                      'daemon_status': daemon.returncode, 'custodian': custody,
                      'custodian_outcome': outcome, 'departure_seconds': round(departure_seconds, 3)}
            issued_pid = read_optional(issued)
            record['notifier'] = None
            if issued_pid is not None:
                exit_code = read_optional(reaped)
                if exit_code is not None:
                    record['notifier'] = {'pid': int(issued_pid), 'reaped': int(exit_code)}
                else:
                    saved = read_optional(issued_identity)
                    identity = json.loads(saved) if saved is not None else None
                    self.assertIsNotNone(identity, 'missing issued notifier identity')
                    self.assertEqual(identity['pid'], int(issued_pid))
                    record['notifier'] = {**identity, 'outcome': departed(identity, 'notifier')}

            saved_exit = read_optional(parked_exit)
            parked_outcome = json.loads(saved_exit) if saved_exit is not None else None
            cleaned_release = {'exit': 'release', 'release_text': ''}
            fallback = {'exit': 'deadline', 'release_text': None}
            record['parked_exit'] = parked_outcome
            if observed_stop is not None and observed_stop['marker']:
                self.assertEqual(parked_outcome, {'exit': 'release', 'release_text': 'after-marker'})
            elif observed_stop is not None:
                self.assertIsNone(observed_stop['release_text'], 'release existed before cleanup')
                self.assertIn(observed_stop['parked_exit'], (None, cleaned_release, fallback))
                end = observed_stop['wait_end']
                self.assertIn(end, ('daemon-exit', 'ceiling'))
                before_status = observed_stop['daemon_status']
                if end == 'daemon-exit':
                    self.assertIsNotNone(before_status, 'daemon exit was not observed before cleanup')
                    self.assertEqual(before_status, daemon.returncode)
                if phase == 'before_final':
                    self.assertIn(parked_outcome, (cleaned_release, fallback))
                elif saved_exit is None:
                    self.assertEqual(end, 'daemon-exit', 'parked exit missing without foreground exit')
                    self.assertEqual(before_status, 75, 'parked exit missing without foreground cutoff status')
                    self.assertEqual(observed_stop['parked_entry_pid'], custody['pid'],
                                     'foreground was not observed parked on the recorded custodian')
                    self.assertFalse(custodian_reaped.exists(), 'foreground recorded a custodian reap')
                    record['parked_end'] = 'foreground_cutoff'
                    record['custodian_reaped_present'] = False
                else:
                    allowed = (cleaned_release, fallback) if end == 'ceiling' else (fallback,)
                    self.assertIn(parked_outcome, allowed)
                    if end == 'ceiling' and parked_outcome == cleaned_release:
                        self.assertIsNone(before_status, 'daemon already exited at ceiling capture')
            return record

        # The test hook loads in the actual installed launcher processes. It
        # parks the specified boundary without changing any shipped bytes.
        (arm / 'sitecustomize.py').write_text(f"""
import json, os, pathlib, socket, subprocess, sys, threading, time
phase = {phase!r}
parked = pathlib.Path({str(parked)!r})
release = pathlib.Path({str(release)!r})
issued = pathlib.Path({str(issued)!r})
reaped = pathlib.Path({str(reaped)!r})
custodian_reaped = pathlib.Path({str(custodian_reaped)!r})
retiring = pathlib.Path({str(retiring)!r})
claimed = pathlib.Path({str(claimed)!r})
eof = pathlib.Path({str(eof)!r})
after_exit = pathlib.Path({str(after_exit)!r})
early = {early!r}
marker = pathlib.Path({str(marker)!r})
parked_exit = pathlib.Path({str(parked_exit)!r})
custodian_identity = pathlib.Path({str(custodian_identity)!r})
issued_identity = pathlib.Path({str(issued_identity)!r})
publish_marker = True
if early:
    sys.path.insert(0, {str(launcher.parent)!r})
    from relay_engine.lifecycle_paths import _native_identity
def atomic_write(path, data):
    temporary = path.with_suffix(f'.{{os.getpid()}}.{{threading.get_ident()}}.tmp')
    temporary.write_text(data)
    os.replace(temporary, path)
def record_identity(path, pid):
    birth, _ = _native_identity(pid)
    atomic_write(path, json.dumps(dict(pid=pid, birth=birth)))
def wait_for(path):
    deadline = time.monotonic() + 9
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return 'release' if path.exists() else 'deadline'
def park(value):
    parked.write_text(str(value))
    outcome = wait_for(release)
    if early:
        release_text = release.read_text() if outcome == 'release' else None
        atomic_write(parked_exit, json.dumps(dict(exit=outcome, release_text=release_text)))
def native(argv, **kwargs):
    if phase == 'late_invocation':
        park('before invocation')
    child = original_popen(argv, **kwargs)
    issued.write_text(str(child.pid))
    if early:
        record_identity(issued_identity, child.pid)
    for name in ('poll', 'wait'):
        method = getattr(child, name)
        def capture(*args, base=method, **kwargs):
            result = base(*args, **kwargs)
            if child.returncode is not None:
                reaped.write_text(str(child.returncode))
            return result
        setattr(child, name, capture)
    if phase == 'late_return':
        park('after invocation')
    return child
original_popen = subprocess.Popen
if len(sys.argv) == 6 and sys.argv[1] == '--private-notifier-custodian':
    if early:
        record_identity(custodian_identity, os.getpid())
    if phase in ('late_invocation', 'late_return'):
        subprocess.Popen = native
    elif phase in ('before_final', 'overlap', 'first_read'):
        gate = int(sys.argv[3])
        reads = 0
        read = os.read
        def final_read(fd, size):
            global reads
            if fd == gate:
                reads += 1
                if phase == 'first_read' and reads == 1:
                    park('before first claim')
                if phase == 'overlap':
                    if reads == 1:
                        park('before claim')
                        try:
                            return read(fd, size)
                        except BlockingIOError:
                            claimed.touch()
                            raise
                    if reads == 2:
                        wait_for(after_exit)
                elif reads == 2:
                    park('final check')
                result = read(fd, size)
                if not result:
                    eof.touch()
                return result
            return read(fd, size)
        os.read = final_read
        subprocess.Popen = native
elif len(sys.argv) > 2 and sys.argv[1:3] == ['engine', 'serve']:
    if phase == 'pre_ready':
        from relay_engine.api_service import _ForegroundEpoch
        enter_stop = _ForegroundEpoch.enter_stop
        def observe_stop(self):
            result = enter_stop(self)
            if publish_marker:
                atomic_write(marker, json.dumps(dict(admission_closed=self.admission_closed,
                                                     attempt=self.attempt is not None)))
            return result
        _ForegroundEpoch.enter_stop = observe_stop
        def start_custodian(argv, **kwargs):
            child = original_popen(argv, **kwargs)
            if '--private-notifier-custodian' in argv:
                wait = child.wait
                def capture_wait(*args, **kwargs):
                    status = wait(*args, **kwargs)
                    custodian_reaped.write_text(json.dumps([child.pid, status]))
                    return status
                child.wait = capture_wait
                park(child.pid)
            return child
        subprocess.Popen = start_custodian
    elif phase == 'before_final':
        from relay_engine.api_service import _NotifierGate
        retire = _NotifierGate.retire
        def observe_retire(self):
            result = retire(self)
            if publish_marker and self.released:
                atomic_write(marker, 'retired')
            return result
        _NotifierGate.retire = observe_retire
        sendall = socket.socket.sendall
        def leave_stop_unread(self, data, *args):
            if b'"kind":"stop"' not in data:
                return sendall(self, data, *args)
        socket.socket.sendall = leave_stop_unread
    elif phase == 'overlap':
        sys.path.insert(0, {str(launcher.parent)!r})
        from relay_engine.api_service import _NotifierGate
        def park_retirement(self):
            retiring.touch()
            threading.Event().wait()
        _NotifierGate.retire = park_retirement
    elif phase in ('late_invocation', 'late_return'):
        sys.path.insert(0, {str(launcher.parent)!r})
        from relay_engine.api_service import ApiService
        def park_before_control_stop(self, attempt):
            threading.Event().wait()
        ApiService._stop_owned = park_before_control_stop
""")
        env = {'HOME': str(home), 'TMPDIR': str(self.temp), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(arm)}
        command = [sys.executable, str(launcher), 'engine', 'serve', '--store-root', str(root),
                   '--project-id', P, '--bridge-port', '0', '--terminal-notifier', str(program)]
        with (service_fixture.private_idle(admission, env) as (idle_fd, child_env),
              subprocess.Popen(command, cwd=arm, env=child_env, pass_fds=(idle_fd,), start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE) as daemon):
            parked_entry_pid = None
            observed_stop = None
            try:
                ready = Path(lifecycle_paths.ready_file_path(admission.idle.store,
                             platform=sys.platform, environ=env, home=str(home)))
                if phase != 'pre_ready':
                    self.wait_until(ready.exists)
                    data = json.loads(ready.read_bytes())
                    peer = self.connect(data)
                    self.sign_in(peer, keys=keys)
                    raw = draft(kind='QUESTION', to='operator', needs='answer', subject='Local decision',
                                body='Can we proceed?\n')
                    reply = peer.call('relay.submit', {'draft_base64': base64.b64encode(raw).decode(),
                                                       'submission_key': 'question'})
                    self.assertIn('result', reply)
                self.wait_until(lambda: parked.exists() and parked.stat().st_size)
                if phase == 'pre_ready':
                    custodian = int(parked.read_text())
                    parked_entry_pid = custodian
                    self.assertFalse(ready.exists())
                    with (root / 'api-service.lock').open('rb') as original:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(original, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self.assertEqual(store_layout._inode(os.fstat(original.fileno())), admission.idle.lock)
                    self.verify_custodian_inventory(custodian, daemon.pid, root, ready.parent)
                self.assertEqual(issued.exists(), phase == 'late_return')
                if phase == 'first_read':
                    self.assertEqual(parked.read_text(), 'before first claim')
                    self.assertFalse(eof.exists())
                    with closing(store_layout.open_catalogue_readonly(root)) as catalogue:
                        pending = catalogue.execute('SELECT * FROM queue_notifications').fetchall()
                        health = catalogue.execute('SELECT * FROM queue_notifier_health').fetchall()
                    self.assertTrue(pending)
                    daemon.kill()
                    daemon.communicate(timeout=5)
                    self.assertEqual(daemon.returncode, -signal.SIGKILL)
                    release.touch()
                    self.wait_until(eof.exists)
                    self.assertFalse(issued.exists())
                    observed = arm / 'readback'
                    shutil.copytree(root, observed)
                    with closing(store_layout.open_catalogue(observed)) as catalogue:
                        self.assertEqual(catalogue.execute('SELECT * FROM queue_notifications').fetchall(), pending)
                        self.assertEqual(catalogue.execute('SELECT * FROM queue_notifier_health').fetchall(), health)
                    print(json.dumps({'installed_custody': phase, 'build': data['build_id'],
                                      'foreground_status': daemon.returncode, 'first_read_eof': True,
                                      'stop_unavailable_to_custodian': True, 'issued': False,
                                      'receipt_and_health_unchanged': True}))
                    return
                sent = time.monotonic()
                daemon.terminate()
                if early:
                    try:
                        self.wait_for_host_progress(lambda: marker.exists() or daemon.poll() is not None,
                                                    'stop observed after SIGTERM')
                    except AssertionError as error:
                        observed_stop = stop_facts(sent, 'ceiling')
                        self.fail(f'{error}; {json.dumps(observed_stop)}')
                    observed_stop = stop_facts(sent, 'daemon-exit' if not marker.exists() else 'marker')
                    self.assertTrue(observed_stop['marker'], f'missing stop marker; {json.dumps(observed_stop)}')
                    if phase == 'pre_ready':
                        self.assertEqual(json.loads(marker.read_text()), {'admission_closed': True, 'attempt': True})
                    atomic_write(release, 'after-marker')
                if phase == 'overlap':
                    self.wait_until(retiring.exists)
                    release.touch()
                    self.wait_until(claimed.exists)
                _, stderr = daemon.communicate(timeout=5)
                elapsed = time.monotonic() - sent
                self.assertEqual(daemon.returncode, 0 if early else 75, stderr)
                self.assertLess(elapsed, 2 if early else 5)
                if early:
                    self.assertFalse(issued.exists())
                    saved_exit = read_optional(parked_exit)
                    self.assertIsNotNone(saved_exit, 'missing parked exit record')
                    self.assertEqual(json.loads(saved_exit), {'exit': 'release', 'release_text': 'after-marker'})
                    if phase == 'pre_ready':
                        self.assertTrue(custodian_reaped.exists(), 'foreground did not reap the pre-READY custodian')
                        self.assertEqual(json.loads(custodian_reaped.read_text()), [custodian, 0])
                        with self.assertRaises(ProcessLookupError):
                            os.kill(custodian, 0)
                    if phase == 'before_final':
                        self.assertTrue(eof.exists())
                elif phase == 'overlap':
                    after_exit.touch()
                    self.wait_until(eof.exists)
                    self.assertFalse(issued.exists())
                else:
                    self.assertEqual(issued.exists(), phase == 'late_return')
                    release.touch()
                    self.wait_until(lambda: reaped.exists() and reaped.stat().st_size)
                    pid = int(issued.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                print(json.dumps({'installed_custody': phase, 'status': daemon.returncode,
                                  'term_seconds': round(elapsed, 3), 'issued': issued.exists(),
                                  'reaped': reaped.exists(), 'overlap_claim': claimed.exists(),
                                  'eof': eof.exists(), 'stop_unavailable_to_custodian': phase != 'pre_ready',
                                  **({'marker_seconds': observed_stop['elapsed_seconds'],
                                      'parked_exit': json.loads(saved_exit)} if early else {})}))
            finally:
                prior_failure = sys.exception()
                after_exit.touch()
                release.touch()
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.communicate(timeout=3)
                if early:
                    try:
                        cleanup = json.dumps(cleanup_facts(daemon, observed_stop))
                        print(cleanup)
                        if prior_failure is not None:
                            prior_failure.add_note(f'cleanup evidence: {cleanup}')
                    except (AssertionError, OSError, ValueError) as error:
                        if prior_failure is None:
                            raise
                        prior_failure.add_note(f'unverified cleanup for {phase}: {error}')

    def test_installed_first_read_eof_after_foreground_loss(self):
        self.check_installed_custody_discriminator(self.install_candidate(), 'first_read')

    @contextmanager
    def start_installed_store(self, launcher, root, directory, admission=None, *,
                              bridge=False, environment=None, pass_fds=(), retirement_fd=None,
                              notifier_source='#!/bin/sh\nexit 0\n'):
        from contextlib import nullcontext

        command = [sys.executable, str(launcher), 'engine', 'serve',
                   '--store-root', str(root), '--project-id', P]
        if bridge:
            program = directory / 'terminal-notifier'
            program.write_text(notifier_source)
            program.chmod(0o700)
            command.extend(('--bridge-port', '0', '--terminal-notifier', str(program)))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(self.temp))
        env.update(environment or {})
        if retirement_fd is not None:
            env['ADT_PRIVATE_RETIREMENT_FD'] = str(retirement_fd)
            pass_fds = (*pass_fds, retirement_fd)
        channels = (nullcontext((None, env)) if admission is None else
                    service_fixture.private_idle(admission, env))
        with channels as (fd, env):
            if fd is not None:
                pass_fds = (*pass_fds, fd)
            proc = subprocess.Popen(command, cwd=directory, env=env, pass_fds=pass_fds,
                                    start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with proc:
            try:
                yield proc
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    def ready_from(self, child):
        self.assertTrue(select.select([child.stdout], [], [], 6)[0], 'installed ready timed out')
        ready = child.stdout.readline().decode().strip()
        self.assertTrue(ready, child.stderr.read() if not ready else None)
        return Path(ready)

    def assert_preopen_refusal(self, launcher, root, directory, admission=None, *, raw=None):
        marker = directory / 'ordinary-open'
        locator = root / lifecycle_paths._LIVE_LOCATOR
        before = (locator.stat().st_ino, locator.read_bytes()) if locator.exists() else None
        (directory / 'sitecustomize.py').write_text(f"""
import pathlib, sys
sys.path.insert(0, {str(launcher.parent)!r})
from relay_engine import store_layout
def refused(*args, **kwargs):
    pathlib.Path({str(marker)!r}).touch()
    raise AssertionError('ordinary SQLite opened during rejected launch')
store_layout.sqlite3.connect = refused
""")
        reader = writer = None
        try:
            fds, env = (), {'PYTHONPATH': str(directory)}
            if raw is not None:
                reader, writer = os.pipe()
                os.write(writer, raw)
                os.close(writer)
                writer = None
                fds, env = (reader,), dict(env, ADT_PRIVATE_IDLE_FD=str(reader))
            with self.start_installed_store(launcher, root, directory, admission,
                                            environment=env, pass_fds=fds) as child:
                output, diagnostic = child.communicate(timeout=6)
                self.assertEqual((child.returncode, output), (1, b''), diagnostic)
            self.assertFalse(marker.exists())
            after = (locator.stat().st_ino, locator.read_bytes()) if locator.exists() else None
            self.assertEqual(after, before)
        finally:
            for fd in (writer, reader):
                if fd is not None:
                    os.close(fd)

    def clean_admitted_round(self, launcher, root, directory, admission, *, bridge=False):
        reader, writer = os.pipe()
        try:
            with self.start_installed_store(launcher, root, directory, admission,
                                            bridge=bridge, retirement_fd=writer) as child:
                os.close(writer)
                writer = None
                ready = self.ready_from(child)
                target = self.live_target(root)
                self.assertIsNotNone(target)
                self.assertEqual(target.ready_path, str(ready))
                observation = json.loads(ready.read_bytes())
                self.assertEqual(observation['store_id'], admission.idle.store)
                live = json.loads((root / lifecycle_paths._LIVE_LOCATOR).read_bytes())
                arm = store_layout.IncumbentGuard._parse_record(store_layout._canonical_frame(live['arm']))
                self.assertNotEqual(arm.owner.epoch, admission.idle.disposition.prior_epoch)
                if bridge:
                    printed = subprocess.check_output([sys.executable, str(launcher), 'console', 'open',
                        '--store-root', str(root), '--print'], timeout=5)
                    self.assertTrue(printed.startswith(observation['bridge_origin'].encode()))
                child.terminate()
                output, diagnostic = child.communicate(timeout=5)
                self.assertEqual((child.returncode, output), (0, b''), diagnostic)
                self.assertFalse(ready.exists())
            received, reader = reader, None
            idle, proof = api_service._read_idle_receipt(received, deadline=time.monotonic() + 2)
            self.assertEqual(proof.prior_epoch, arm.owner.epoch)
            self.assertEqual(idle.lock, admission.idle.lock)
            print(json.dumps({'installed_restart': 'clean', 'build': observation['build_id'],
                              'bridge': bridge, 'epoch': arm.owner.epoch, 'same_lock': True}))
            return service_fixture.AdmissionFixture(idle, proof)
        finally:
            for fd in (writer, reader):
                if fd is not None:
                    os.close(fd)

    def test_composed_owner_birth_refuses_replaced_parent_before_creation(self):
        from unittest.mock import Mock

        from relay_engine import operator_terminal, owner_start
        from relay_engine.tests.test_owner_start import installed_key

        home = self.home.resolve()
        root = home / '.adt'
        moved = home.with_name('signed-home')
        owner_directory = home / '.adt-owner' / 'receipts'
        owner_directory.parent.mkdir(mode=0o700)
        owner_directory.mkdir(mode=0o700)
        original_parent = store_layout._inode(home.stat())
        key = self.base / 'ephemeral-signer'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                       check=True, capture_output=True, timeout=10)
        pub = installed_key(key.with_suffix('.pub'), None)
        spawn = Mock()
        native_mkdir = os.mkdir
        observations = []
        attempt = None

        def prove(message):
            return subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'sign', '-f', str(key),
                                   '-n', 'adt-terminal-check-v1'], input=message, check=True,
                                  capture_output=True, timeout=10).stdout

        def authorize(selection):
            nonlocal attempt
            attempt = owner_start.authorize_owner_start(
                selection, owner_start.NativeIdentity.current(), prove=prove, show=lambda _: None)
            return attempt

        def mkdir(path, mode=0o777, *, dir_fd=None):
            if path == root.name:
                parent = store_layout._inode(os.fstat(dir_fd))
                observations.append(('mkdir', parent))
            native_mkdir(path, mode, dir_fd=dir_fd)

        class GuardedBirthObserved(Exception):
            pass

        def birth(selection, consume):
            def observe(selected_root, store, project, parent):
                self.assertEqual((selected_root, store, project), (root, selection.store, P))
                observations.append(('consume', parent))
                consume(selected_root, store, project, parent)

            home.rename(moved)
            home.mkdir(mode=0o700)
            replacement = store_layout._inode(home.stat())
            with patch.object(store_layout.sqlite3, 'connect', wraps=store_layout.sqlite3.connect) as sqlite:
                with self.assertRaisesRegex(store_layout.LayoutFault, 'birth-unproved'):
                    owner_start.production_birth(selection, observe)
                sqlite.assert_not_called()
            self.assertEqual(observations, [('consume', replacement)])
            self.assertFalse(attempt._uses.birth)
            self.assertFalse(root.exists())
            self.assertFalse((moved / root.name).exists())
            spawn.assert_not_called()
            home.rmdir()
            moved.rename(home)

            frame = owner_start.production_birth(selection, observe)
            self.assertEqual(observations, [('consume', replacement), ('consume', original_parent),
                                           ('mkdir', original_parent)])
            self.assertTrue(attempt._uses.birth)
            with self.assertRaisesRegex(store_layout.LayoutFault, 'birth-unproved'):
                consume(root, selection.store, P, original_parent)
            idle, proof = store_layout._decode_idle_receipt(frame)
            with store_layout.IncumbentGuard.acquire(root, expected_lock=idle.lock) as guard:
                guard.verify_idle(idle, proof)
                with closing(store_layout._immutable_incumbent_read(store_layout.catalogue_path(root))) as catalogue:
                    self.assertEqual(catalogue.execute(
                        'SELECT kind FROM catalogue_identity WHERE id=1').fetchone(), ('production',))
            print(json.dumps({'replaced_parent': replacement, 'signed_parent': original_parent,
                              'held_create_parent': observations[-1][1], 'store': idle.store,
                              'original_lock': idle.lock, 'replay_refused': True, 'spawn': False}))
            raise GuardedBirthObserved

        with (patch.object(store_layout, 'production_root', return_value=root),
              patch.object(operator_terminal, '_installed_key', return_value=pub),
              patch.object(os, 'mkdir', side_effect=mkdir),
              self.assertRaises(GuardedBirthObserved)):
            owner_start.run_owner(root, P, initialize=True, project_name='Selected project', bridge_port=None,
                                  authorize=authorize, launch=spawn, birth_provider=birth, show=lambda _: None)
        spawn.assert_not_called()

    def test_installed_foreground_owner_clean_restart(self):
        import hashlib
        import pty
        import sqlite3

        from relay_engine import owner_start, version

        package = self.install_candidate().resolve().parents[1]
        scratch = self.base.resolve()
        home = self.home.resolve()
        drop_in = home / '.agents' / 'skills'
        drop_in.parent.mkdir(mode=0o700)
        package.rename(drop_in)
        launcher = drop_in / 'tools' / 'adt'
        installed = launcher.parent
        root = home / '.adt'
        overlay = scratch / 'synthetic'
        overlay.mkdir(mode=0o700)
        trace = scratch / 'installed.jsonl'
        marker = scratch / 'ordinary-open'
        (overlay / 'sitecustomize.py').write_text(f"""
import hashlib, hmac, json, os, pathlib, sqlite3, sys
sys.path.insert(0, {str(installed)!r})
from relay_engine import adt_cli, api_service, operator_terminal, owner_start, store_layout, submit
base = pathlib.Path(os.environ['ADT_SYNTHETIC_HOME'])
assert base.is_relative_to(pathlib.Path({str(scratch)!r}))
submit._home = lambda: str(base)
trace = pathlib.Path({str(trace)!r})

def emit(data):
    with trace.open('a') as out:
        print(json.dumps(data), file=out)

emit({{'pid': os.getpid(), 'origins': [module.__file__ for module in
       (adt_cli, api_service, owner_start, store_layout)], 'sqlite': sqlite3.sqlite_version,
       'interpreter': sys.executable}})

def sign(message):
    return hmac.digest(b'ephemeral-installed-fixture', message, 'sha256')

def verify(message, proof):
    if not hmac.compare_digest(sign(message), proof):
        raise operator_terminal.OperatorCheckRefused('bad_signature')

operator_terminal._verify_signature = verify
authorize = owner_start.authorize_owner_start

def confirm(selection, owner, *, prove, show):
    return authorize(selection, owner, prove=sign, show=show)

owner_start.authorize_owner_start = confirm
collect = owner_start._collect_retirement

def returned(child, retirement, stop):
    frame = collect(child, retirement, stop)
    emit({{'reaped_pid': child.pid, 'native_exit': child.returncode,
           'receipt_sha256': hashlib.sha256(frame).hexdigest()}})
    return frame

owner_start._collect_retirement = returned
if os.environ.get('ADT_SYNTHETIC_REFUSAL'):
    def forbidden(*args, **kwargs):
        pathlib.Path({str(marker)!r}).touch()
        raise AssertionError('SQLite opened during rejected admission')
    sqlite3.connect = forbidden
""")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(overlay),
                   HOME=str(home), ADT_SYNTHETIC_HOME=str(home))

        def receipt_directory(directory):
            receipts = directory / '.adt-owner' / 'receipts'
            receipts.mkdir(mode=0o700, parents=True)
            receipts.parent.chmod(0o700)
            return receipts

        receipts = receipt_directory(home)

        def command(selected_root, *, initialize=False, project=P):
            argv = [sys.executable, str(launcher), 'engine', 'run', '--store-root', str(selected_root),
                    '--project-id', project, '--bridge-port', '0']
            if initialize:
                argv += ['--initialize', '--project-name', 'Selected project']
            return argv

        def arm():
            return store_layout.IncumbentGuard._parse_record((root / store_layout._INCUMBENT).read_bytes())

        def retained(expected):
            receipt, = receipts.iterdir()
            frame = receipt.read_bytes()
            idle, proof = store_layout._decode_idle_receipt(frame)
            self.assertEqual((idle.root, idle.lock, idle.store, idle.project, proof.prior_epoch),
                             (expected.root, expected.lock, expected.store, expected.project, expected.owner.epoch))
            with store_layout.IncumbentGuard.acquire(root, expected_lock=expected.lock) as guard:
                guard.verify_idle(idle, proof)
            expected_return = {'reaped_pid': expected.owner.pid, 'native_exit': 0,
                               'receipt_sha256': hashlib.sha256(frame).hexdigest()}
            self.assertIn(expected_return, [json.loads(line) for line in trace.read_text().splitlines()])
            return {**expected_return, 'raw_receipt_base64': base64.b64encode(frame).decode('ascii')}

        def refuse(argv, expected, *, selected_home=home, environment=env, pass_fds=()):
            before = len(trace.read_text().splitlines())
            result = subprocess.run(argv, env=dict(environment, ADT_SYNTHETIC_HOME=str(selected_home),
                                                   ADT_SYNTHETIC_REFUSAL='1'),
                                    pass_fds=pass_fds, capture_output=True, timeout=8)
            self.assertEqual((result.returncode, result.stdout), (1, b''), result.stderr)
            self.assertIn(expected, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(len(trace.read_text().splitlines()), before + 1)

        self.assertFalse(root.exists())
        terminal_pid, terminal = pty.fork()
        if terminal_pid == 0:
            try:
                os.execve(sys.executable, command(root, initialize=True), env)
            finally:
                os._exit(127)
        status = None
        transcript = bytearray()

        def reap_terminal():
            nonlocal status
            seen, exit_status = os.waitpid(terminal_pid, os.WNOHANG)
            if seen:
                status = exit_status
                return True
            return False

        try:
            deadline = time.monotonic() + 8
            while b'/ready.json' not in transcript and time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        data = os.read(terminal, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    transcript.extend(data)
            ready_lines = [line for line in transcript.decode(errors='replace').splitlines()
                           if line.endswith('/ready.json') and line.startswith('/')]
            self.assertEqual(len(ready_lines), 1, transcript.decode(errors='replace'))
            ready = Path(ready_lines[0])
            metadata = json.loads(ready.read_bytes())
            expected_build = version.fingerprint(Path(__file__).resolve().parents[2])
            self.assertEqual(metadata['build_id'], expected_build)
            self.assertEqual(version.fingerprint(installed), expected_build)
            live = arm()
            self.assertEqual((live.store, live.project), (metadata['store_id'], P))
            self.assertEqual(os.getpgid(terminal_pid), terminal_pid)
            self.assertEqual(os.getpgid(live.owner.pid), live.owner.pid)
            with closing(store_layout.open_catalogue_readonly(root)) as catalogue:
                self.assertEqual(catalogue.execute(
                    'SELECT store_id, kind FROM catalogue_identity WHERE id=1').fetchone(),
                                 (live.store, 'production'))
            installed_launch = subprocess.check_output(
                [sys.executable, str(launcher), 'console', 'open', '--store-root', str(root), '--print'],
                env=env, timeout=5).decode().strip()
            self.assertEqual(urlsplit(installed_launch).netloc, urlsplit(metadata['bridge_origin']).netloc)
            os.write(terminal, b'\x03')
            self.wait_until(reap_terminal)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertFalse(ready.exists())
            first_return = retained(live)
        finally:
            os.close(terminal)
            if status is None:
                os.kill(terminal_pid, signal.SIGTERM)
                try:
                    self.wait_until(reap_terminal)
                except AssertionError:
                    os.kill(terminal_pid, signal.SIGKILL)
                    os.waitpid(terminal_pid, 0)

        infrastructure = scratch / 'infrastructure'
        infrastructure.mkdir(mode=0o700)
        receipt_directory(infrastructure)
        partial_root = infrastructure / '.adt'
        partial_root.mkdir(mode=0o700)
        infrastructure_file = partial_root / store_layout._INCUMBENT_LOCK
        infrastructure_file.touch(mode=0o600)
        original_infrastructure = (infrastructure_file.stat().st_ino, infrastructure_file.read_bytes())
        refuse(command(partial_root, initialize=True), b'startup_selection', selected_home=infrastructure)
        self.assertEqual(list(partial_root.iterdir()), [infrastructure_file])
        self.assertEqual((infrastructure_file.stat().st_ino, infrastructure_file.read_bytes()),
                         original_infrastructure)
        first_frame = base64.b64decode(first_return['raw_receipt_base64'])
        idle, proof = store_layout._decode_idle_receipt(first_frame)
        with service_fixture.private_idle(service_fixture.AdmissionFixture(idle, proof), env) as (fd, child_env):
            refuse([sys.executable, str(launcher), 'engine', 'serve', '--store-root', str(root),
                    '--project-id', P, '--bridge-port', '0'], b'startup-unverified',
                   environment=child_env, pass_fds=(fd,))
        self.assertEqual((root / store_layout._INCUMBENT).read_bytes(), store_layout._canonical_frame(asdict(idle)))
        changed_project = str(uuid4())
        wrong_receipt = receipts / owner_start._receipt_name(root, changed_project)
        wrong_receipt.write_bytes(first_frame)
        wrong_receipt.chmod(0o600)
        try:
            refuse(command(root, project=changed_project), b'receipt-unproved')
            self.assertEqual(wrong_receipt.read_bytes(), first_frame)
        finally:
            wrong_receipt.unlink()

        def stop_round(*, kill_child):
            with subprocess.Popen(command(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as owner:
                try:
                    next_ready = self.ready_from(owner)
                    current = arm()
                    self.assertNotEqual(current.owner.epoch, live.owner.epoch)
                    self.assertEqual(current.lock, live.lock)
                    if kill_child:
                        os.kill(current.owner.pid, signal.SIGKILL)
                    else:
                        owner.terminate()
                    output, error = owner.communicate(timeout=8)
                    self.assertEqual((owner.returncode == 0, output), (not kill_child, b''), error)
                    if kill_child:
                        self.assertEqual(arm(), current)
                    else:
                        self.assertFalse(next_ready.exists())
                    return current
                finally:
                    if owner.poll() is None:
                        owner.terminate()
                        try:
                            owner.communicate(timeout=6)
                        except subprocess.TimeoutExpired:
                            owner.kill()
                            owner.communicate(timeout=3)

        successor = stop_round(kill_child=False)
        second_return = retained(successor)
        abandoned = stop_round(kill_child=True)
        refuse(command(root), b'incumbent-changed')
        self.assertEqual(arm(), abandoned)
        receipt, = receipts.iterdir()
        self.assertEqual(receipt.read_bytes(), base64.b64decode(second_return['raw_receipt_base64']))
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        origins = sorted({origin for entry in events for origin in entry.get('origins', ())})
        self.assertEqual(origins, [str(installed / 'relay_engine' / name) for name in
                                   ('adt_cli.py', 'api_service.py', 'owner_start.py', 'store_layout.py')])
        interpreters = {os.path.realpath(entry['interpreter']) for entry in events if 'interpreter' in entry}
        self.assertEqual(interpreters, {os.path.realpath(sys.executable)})
        self.assertEqual({entry['sqlite'] for entry in events if 'sqlite' in entry}, {sqlite3.sqlite_version})
        print(json.dumps({'platform': sys.platform, 'machine': os.uname().machine,
                          'uid': os.geteuid(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                          'installed_origins': origins, 'build': metadata['build_id'],
                          'terminal_ctrl_c': True, 'terminal_exit': os.waitstatus_to_exitcode(status),
                          'first': {**first_return, 'epoch': live.owner.epoch},
                          'restart': {**second_return, 'epoch': successor.owner.epoch},
                          'store': live.store, 'project': live.project, 'same_lock': successor.lock,
                          'refusals_before_sqlite': ['infrastructure', 'missing-proof', 'changed-selection',
                                                     'abnormal-loss']}))

    def test_installed_disposable_guarded_restart(self):
        launcher = self.install_candidate()
        directory = self.base / 'clean'
        directory.mkdir(mode=0o700)
        root = directory / 'store'
        admission = service_fixture.admitted_store_fixture(root, P, project_name='service')
        retired = self.clean_admitted_round(launcher, root, directory, admission, bridge=True)
        self.assert_preopen_refusal(launcher, root, directory, admission)
        self.assert_preopen_refusal(launcher, root, directory, raw=b'{}')
        self.clean_admitted_round(launcher, root, directory, retired)
        legacy = self.base / 'legacy'
        legacy.mkdir(mode=0o700)
        legacy_root = legacy / 'store'
        with closing(store_layout.open_catalogue(legacy_root, kind='isolated')) as catalogue, catalogue:
            store_id = store_layout._row(catalogue, 'SELECT store_id FROM catalogue_identity')[0]
            store_layout.reserve_project(catalogue, P, 'legacy', 1)
            store_layout.activate_project(catalogue, P)
        with closing(store_layout.open_ledger(legacy_root, P, create=True)) as ledger, ledger:
            ledger.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, ?)',
                           (P, 'legacy', 1)).close()
        for path in (store_layout.catalogue_path(legacy_root), store_layout.ledger_path(legacy_root, P)):
            path.chmod(0o600)
        (legacy_root / 'api-service.lock').touch(mode=0o600)
        self.assert_preopen_refusal(launcher, legacy_root, legacy)
        with store_layout.IncumbentGuard.acquire(legacy_root) as guard:
            proof = guard._offline_checkpoint(store_id, P, b'disposable runner owns pre-service offline fixture')
            idle, retained = guard.enroll_existing(proof)
        self.clean_admitted_round(launcher, legacy_root, legacy,
                                  service_fixture.AdmissionFixture(idle, retained))
        self.check_installed_recovery(launcher, 'recover')
        for mode in ('reader', 'sidecar', 'reentry', 'notifier'):
            with self.subTest(mode=mode):
                self.check_installed_recovery(launcher, mode)

    def test_installed_stop_with_unproved_admission_keeps_armed(self):
        launcher = self.install_candidate()
        for cut in ('during_identity', 'immediately_after_arm'):
            with self.subTest(cut=cut):
                self.check_unproved_admission_stop(launcher, cut)
        directory = self.base / 'verified-stop'
        directory.mkdir(mode=0o700)
        root = directory / 'store'
        admission = service_fixture.admitted_store_fixture(root, P, project_name='verified')
        self.clean_admitted_round(launcher, root, directory, admission)

    def check_unproved_admission_stop(self, launcher, cut):
        directory = self.base / cut
        directory.mkdir(mode=0o700)
        root = directory / 'store'
        admission = service_fixture.admitted_store_fixture(root, P, project_name='admitted')
        other = directory / 'other'
        wrong = service_fixture.admitted_store_fixture(other, P, project_name='different Store')
        self.assertNotEqual(wrong.idle.store, admission.idle.store)
        store_layout.catalogue_path(other).replace(store_layout.catalogue_path(root))
        record = root / store_layout._INCUMBENT
        parked = directory / 'parked'
        started = directory / 'startup-result'
        protected = directory / 'protected'
        writable = directory / 'ordinary-writable'
        (directory / 'sitecustomize.py').write_text(f"""
import json, pathlib, sys, time
sys.path.insert(0, {str(launcher.parent)!r})
from relay_engine import api_service, store_layout

original_start = api_service.ApiService.start
original_arm = store_layout.IncumbentGuard.arm
original_protected_read = store_layout.IncumbentGuard._protected_read
original_connect = store_layout.sqlite3.connect
service = None
immutable_reads = 0
park_on_catalogue_open = False

def wait_for_stop():
    marker = pathlib.Path({str(parked)!r})
    staging = marker.with_suffix('.tmp')
    staging.write_bytes(pathlib.Path({str(record)!r}).read_bytes())
    staging.replace(marker)
    deadline = time.monotonic() + 3
    while not service.is_stopping():
        if time.monotonic() >= deadline:
            raise AssertionError('foreground STOP did not arrive')
        time.sleep(0.01)

def start(self):
    global service
    service = self
    result = original_start(self)
    pathlib.Path({str(started)!r}).write_text(json.dumps([result, self._admission_unresolved]))
    return result

def arm(guard, idle, owner):
    result = original_arm(guard, idle, owner)
    if {cut!r} == 'immediately_after_arm':
        wait_for_stop()
    return result

def connect(path, *args, **kwargs):
    global immutable_reads, park_on_catalogue_open
    if isinstance(path, str) and path.endswith('?mode=ro&immutable=1'):
        conn = original_connect(path, *args, **kwargs)
        immutable_reads += 1
        if park_on_catalogue_open:
            park_on_catalogue_open = False
            wait_for_stop()
        return conn
    if path != ':memory:':
        pathlib.Path({str(writable)!r}).touch()
        raise AssertionError('unexpected ordinary SQLite setup')
    return original_connect(path, *args, **kwargs)

def protected_read(guard, *args):
    global park_on_catalogue_open
    park_on_catalogue_open = {cut!r} == 'during_identity'
    try:
        return original_protected_read(guard, *args)
    except store_layout.LayoutFault as error:
        pathlib.Path({str(protected)!r}).write_text(json.dumps([error.cause, immutable_reads]))
        raise
    finally:
        park_on_catalogue_open = False

api_service.ApiService.start = start
store_layout.IncumbentGuard.arm = arm
store_layout.IncumbentGuard._protected_read = protected_read
store_layout.sqlite3.connect = connect
""")
        reader, writer = os.pipe()
        try:
            with self.start_installed_store(launcher, root, directory, admission, retirement_fd=writer,
                                            environment={'PYTHONPATH': str(directory)}) as child:
                os.close(writer)
                writer = None
                self.wait_until(parked.exists)
                frame = parked.read_bytes()
                arm = store_layout.IncumbentGuard._parse_record(frame)
                self.assertIsNotNone(arm.owner)
                self.assertEqual((arm.phase, arm.store, arm.owner.pid), ('armed', admission.idle.store, child.pid))
                self.assertEqual(arm.lock, admission.idle.lock)
                with self.assertRaisesRegex(store_layout.LayoutFault, 'store-locked'):
                    store_layout.IncumbentGuard.acquire(root, expected_lock=arm.lock)
                since = time.monotonic()
                child.terminate()
                output, diagnostic = child.communicate(timeout=5)
                elapsed = time.monotonic() - since
            self.assertEqual(output, b'')
            self.assertEqual(json.loads(started.read_text()), [None, cut == 'during_identity'])
            cause, immutable_reads = json.loads(protected.read_text())
            self.assertEqual(cause, 'member-unproved')
            self.assertEqual(immutable_reads, 3, 'expected the kind preflight and two protected opens')
            self.assertFalse(writable.exists())
            self.assertFalse((Path(arm.owner.runtime) / 'ready.json').exists())
            self.assertFalse((root / lifecycle_paths._LIVE_LOCATOR).exists())
            self.assertTrue(select.select([reader], [], [], 1)[0])
            self.assertEqual(os.read(reader, 1), b'')
            with store_layout.IncumbentGuard.acquire(root, expected_lock=arm.lock) as guard:
                self.assertEqual(guard.read(), arm)
                self.assertEqual(record.read_bytes(), frame)
            self.assertEqual(child.returncode, 75, diagnostic)
            self.assertGreaterEqual(elapsed, 3.8)
            self.assertLess(elapsed, 5)
            print(json.dumps({'installed_stop': cut, 'status': child.returncode, 'seconds': round(elapsed, 3),
                              'protected_reads': immutable_reads, 'writable_opens': 0, 'same_inode_return': True,
                              'unchanged_arm': True, 'idle_receipt': False, 'ready_or_locator': False}))
        finally:
            for fd in (writer, reader):
                if fd is not None:
                    os.close(fd)

    def test_installed_recovery_refuses_missing_checkpoint_or_unresolved_publication(self):
        launcher = self.install_candidate()
        for mode in ('missing', 'mismatched', 'publication'):
            with self.subTest(mode=mode):
                self.check_installed_recovery(launcher, mode)

    def check_installed_recovery(self, launcher, mode):
        directory = self.base / mode
        directory.mkdir(mode=0o700)
        root = directory / 'store'
        trigger = directory / 'checkpoint'
        failed = directory / 'checkpoint-refused'
        issued = directory / 'notifier-issued'
        notifier_source = f"""#!{sys.executable}
import os, pathlib, time
path = pathlib.Path({str(issued)!r})
path.with_suffix('.tmp').write_text(str(os.getpid()))
path.with_suffix('.tmp').replace(path)
time.sleep(15)
"""
        operation_key = 'disposable-original-operation'
        operation_draft = draft(to='operator')

        def seed(path):
            if mode != 'notifier':
                return
            with closing(store_layout.open_catalogue(path)) as catalogue, catalogue:
                catalogue.execute(
                    "INSERT INTO queue_items (item_id,kind,subkind,project_id,raiser,revision,state,"
                    "texts,pins,condition_key,raised_position,raised_at_ms,popup_text) VALUES "
                    "('pending','notice','project_stopped',?,'fixture',1,'pending','{}','{}',"
                    "'fixture',1,1000,'disposable notification')", (P,)).close()
                catalogue.execute("INSERT INTO queue_notifications (item_id,revision,state,next_attempt_at_ms) "
                                  "VALUES ('pending',1,'pending',0)").close()
                catalogue.execute('INSERT INTO queue_notifier_health VALUES (1,2,7)').close()

        admission = service_fixture.admitted_store_fixture(root, P, project_name='service', seed=seed)
        reader, writer = os.pipe()
        (directory / 'sitecustomize.py').write_text(f"""
import json, os, pathlib, sys, threading, time
from dataclasses import asdict
sys.path.insert(0, {str(launcher.parent)!r})
from relay_engine import api_service, api_writer, store_layout, submit
from relay_engine.api_submit import ServiceContext

mode = {mode!r}
key, data = {operation_key!r}, {operation_draft!r}
trigger = pathlib.Path({str(trigger)!r})
failed = pathlib.Path({str(failed)!r})
out = int(os.environ.pop('ADT_FIXTURE_CHECKPOINT_FD'))
os.set_inheritable(out, False)

def witnessed(self):
    project = self.project_id
    port = self.writer
    ctx = ServiceContext(port, self.clock, self.store_root, project)
    class Operation:
        files = frozenset({{port.ledger_file(project)}})

        def run(self, connection):
            operator = submit.SeatRef('operator', 'operator', 'operator', None, project)
            return submit.submit(connection, ctx.submit_context(connection, project, operator, key), data)

    until = time.monotonic() + 7
    while not trigger.exists() and time.monotonic() < until:
        time.sleep(0.01)
    try:
        if not trigger.exists():
            raise AssertionError('no runner checkpoint authorization')
        result = port.submit_callee(project, Operation())
        if mode == 'reader':
            parked = threading.Event()
            def blocked(*args):
                parked.set()
                threading.Event().wait()
            store_layout.open_ledger_readonly = blocked
            threading.Thread(target=port.read, args=(project, lambda conn: None), daemon=True).start()
            if not parked.wait(1):
                raise AssertionError('reader did not reserve before settlement')
        if mode in ('sidecar', 'reentry'):
            settle = port.settle_private_io
            def inject(deadline, *, checkpoint=False):
                settled = settle(deadline, checkpoint=checkpoint)
                if mode == 'sidecar':
                    pathlib.Path(str(store_layout.ledger_path(self.store_root, project)) + '-wal').touch()
                else:
                    try:
                        port.read(project, lambda conn: None)
                    except api_writer.WriterUnavailable:
                        pass
                return settled
            port.settle_private_io = inject
        operation = store_layout._IncumbentOperation(key, data, result)
        checkpoint, snapshot = self._capture_recovery_checkpoint(self._owned_arm, operation)
        payload = {{'arm': asdict(checkpoint.armed), 'members': checkpoint.members,
                    'key': checkpoint.operation.key, 'draft': checkpoint.operation.draft.hex(),
                    'admitted': asdict(checkpoint.operation.admitted), 'snapshot': snapshot}}
        api_service._write_private_frame(out, store_layout._canonical_frame(payload), time.monotonic() + 2)
    except store_layout.LayoutFault as error:
        failed.write_text(error.cause)
        os.close(out)
    threading.Event().wait()

api_service.ApiService.serve_forever = witnessed
""")
        try:
            environment = {'PYTHONPATH': str(directory), 'ADT_FIXTURE_CHECKPOINT_FD': str(writer)}
            with self.start_installed_store(launcher, root, directory, admission,
                                            environment=environment, pass_fds=(writer,),
                                            bridge=mode == 'notifier', notifier_source=notifier_source) as proc:
                os.close(writer)
                writer = None
                ready = self.ready_from(proc)
                metadata = json.loads(ready.read_bytes())
                raw_live = json.loads((root / lifecycle_paths._LIVE_LOCATOR).read_bytes())
                arm = store_layout.IncumbentGuard._parse_record(store_layout._canonical_frame(raw_live['arm']))

                class DirectChild:
                    pid = proc.pid

                    @property
                    def exitcode(self):
                        return proc.returncode

                    def is_alive(self):
                        return proc.poll() is None

                birth, boot = lifecycle_paths._native_identity(proc.pid)
                child = store_layout._IncumbentChild.observe(DirectChild(), arm, birth, boot)
                self.assertEqual(arm.owner.notifier, 'configured' if mode == 'notifier' else 'off')
                notifier_pid = None
                if mode == 'notifier':
                    self.wait_until(issued.exists)
                    notifier_pid = int(issued.read_text())
                    os.kill(notifier_pid, 0)
                trigger.touch()
                self.assertTrue(select.select([reader], [], [], 6)[0], 'no checkpoint disposition')
                received, reader = reader, None
                with os.fdopen(received, 'rb') as stream:
                    payload = stream.read(65537)
                self.assertLessEqual(len(payload), 65536)
                self.assertEqual(proc.poll(), None)
                with self.assertRaisesRegex(store_layout.LayoutFault, 'store-locked'):
                    store_layout.IncumbentGuard.acquire(root, expected_lock=admission.idle.lock)
                if mode in ('reader', 'sidecar', 'reentry', 'notifier'):
                    self.assertFalse(payload)
                    self.assertTrue(failed.read_text() in ('operation-unproved', 'journal-unresolved'))
                    checkpoint = snapshot = None
                else:
                    self.assertTrue(payload)
                    value = json.loads(payload)
                    self.assertEqual(store_layout._canonical_frame(value), payload)
                    self.assertEqual(value['arm'], raw_live['arm'])
                    self.assertEqual(value['key'], operation_key)
                    self.assertEqual(bytes.fromhex(value['draft']), operation_draft)
                    checkpoint = store_layout._IncumbentCheckpoint(
                        arm, tuple(tuple(item) for item in value['members']),
                        store_layout._IncumbentOperation(value['key'], bytes.fromhex(value['draft']),
                                                         submit.Admitted(**value['admitted'])))
                    snapshot = value['snapshot']
                proc.kill()
                proc.communicate(timeout=5)
                self.assertEqual(child.process.exitcode, -signal.SIGKILL)
            if notifier_pid is not None:
                def helper_reaped():
                    try:
                        os.kill(notifier_pid, 0)
                    except ProcessLookupError:
                        return True
                    return False

                self.wait_until(helper_reaped)
            with store_layout.IncumbentGuard.acquire(root, expected_lock=arm.lock) as guard:
                self.assertEqual(guard.read(), arm)
                if mode == 'recover':
                    publication = api_service._verify_recovery_publications(guard, arm, snapshot)
                    idle, proof = guard.recover(arm, child, checkpoint, publication=publication)
                    self.assertEqual(idle.phase, 'idle')
                    self.assertEqual(proof.prior_epoch, arm.owner.epoch)
                elif mode == 'publication':
                    moved = ready.with_name('substitution')
                    moved.write_bytes(ready.read_bytes())
                    moved.chmod(0o600)
                    moved.replace(ready)
                    with self.assertRaisesRegex(store_layout.LayoutFault, 'publication-unresolved'):
                        api_service._verify_recovery_publications(guard, arm, snapshot)
                else:
                    selected = checkpoint if mode == 'mismatched' else None
                    if selected is not None:
                        from dataclasses import replace
                        selected = replace(selected, operation=replace(selected.operation, key='later-key'))
                    publication = (api_service._verify_recovery_publications(guard, arm, snapshot)
                                   if snapshot is not None else None)
                    reason = 'notifier-unresolved' if mode == 'notifier' else 'operation-unproved'
                    with self.assertRaisesRegex(store_layout.LayoutFault, reason):
                        guard.recover(arm, child, selected, publication=publication)
                    if mode == 'notifier':
                        copied = directory / 'pending-readback'
                        shutil.copytree(root, copied)
                        with closing(store_layout.open_catalogue(copied)) as catalogue:
                            self.assertEqual(catalogue.execute(
                                'SELECT item_id,revision,state,attempts,next_attempt_at_ms FROM queue_notifications'
                            ).fetchall(), [('pending', 1, 'pending', 0, 0)])
                            self.assertEqual(catalogue.execute('SELECT * FROM queue_notifier_health').fetchall(),
                                             [(1, 2, 7)])
                if mode != 'recover':
                    self.assertEqual(guard.read(), arm)
            if mode == 'recover':
                self.clean_admitted_round(launcher, root, directory,
                                          service_fixture.AdmissionFixture(idle, proof))
            else:
                self.assert_preopen_refusal(launcher, root, directory)
            print(json.dumps({'installed_restart': mode, 'build': metadata['build_id'],
                              'original_epoch': arm.owner.epoch, 'pid': proc.pid,
                              'native_birth': birth, 'native_boot': boot, 'same_lock': True,
                              'exit': child.process.exitcode, 'checkpoint_sent': bool(payload),
                              'protected_operation': asdict(checkpoint.operation.admitted)
                              if mode == 'recover' else None,
                              'original_notifier_mode': arm.owner.notifier,
                              'notifier_issued_reaped_pending': notifier_pid is not None,
                              'successor_admitted': mode == 'recover'}))
        finally:
            for fd in (writer, reader):
                if fd is not None:
                    os.close(fd)


def foreground_fixture_main():
    """Execute the source owner or its private child with synthetic host inputs."""
    import runpy

    from relay_engine import adt_cli, operator_terminal, owner_start
    from relay_engine.tests.test_owner_start import installed_key, synthetic_birth

    mode, root, key, *argv = sys.argv[1:]
    root, key = Path(root), Path(key)
    pub = key.with_suffix('.pub')

    def prove(message):
        return subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'sign', '-f', str(key),
                               '-n', 'adt-terminal-check-v1'], input=message, check=True,
                              capture_output=True, timeout=10).stdout

    def authorize(selection):
        if mode == 'missing-signer':
            raise operator_terminal.OperatorCheckRefused('not_installed')
        return owner_start.authorize_owner_start(
            selection, owner_start.NativeIdentity.current(), prove=prove, show=lambda _: None)

    def unexpected_open(*_args, **_kwargs):
        (root.parent / 'ordinary-open').touch()
        raise AssertionError('refusal performed an ordinary Store open')

    if argv[:1] == ['child']:
        argv = argv[1:]
        arm = store_layout.IncumbentGuard.arm

        def stop_after_arm(guard, idle, owner):
            result = arm(guard, idle, owner)
            os.kill(os.getpid(), signal.SIGTERM)
            return result

        with (patch.object(store_layout, 'production_root', return_value=root),
              patch.object(operator_terminal, '_installed_key', return_value=installed_key(pub, None))):
            if mode == 'pre-ready':
                store_layout.IncumbentGuard.arm = stop_after_arm
                store_layout.open_catalogue = unexpected_open
                store_layout.open_ledger = unexpected_open
                store_layout.open_ledger_runtime = unexpected_open
                api_service.ApiService._publish_locator = unexpected_open
            if mode == 'witness-barrier':
                acknowledge = owner_start.witness_child_epoch

                def record_ack(*args, **kwargs):
                    acknowledge(*args, **kwargs)
                    (root.parent / 'witness-acknowledged').touch()

                owner_start.witness_child_epoch = record_ack
            if mode == 'lost-return':
                api_service._write_private_frame = lambda fd, *_: os.close(fd)
            raise SystemExit(adt_cli.main(argv))

    class TracedChild(subprocess.Popen):
        def send_signal(self, signum):
            if signum in (signal.SIGTERM, signal.SIGINT):
                with (root.parent / 'forwarded').open('a') as log:
                    print(int(signum), file=log)
            super().send_signal(signum)

    def launch(command, **options):
        child = TracedChild([sys.executable, '-c',
                            'from relay_engine.tests.test_operator_daemon_join import foreground_fixture_main;'
                            'foreground_fixture_main()', mode, str(root), str(key), 'child', *command], **options)
        (root.parent / 'child').write_text(str(child.pid))
        if mode == 'spawn-barrier':
            limit = time.monotonic() + 5
            while not (root.parent / 'release-spawn').exists():
                if time.monotonic() >= limit:
                    child.kill()
                    child.wait()
                    raise RuntimeError('test did not release spawn')
                time.sleep(0.01)
        return child

    read_child_epoch = owner_start.read_child_epoch

    def at_witness(*args, **kwargs):
        (root.parent / 'witness-pending').touch()
        deadline = time.monotonic() + 2
        while not (root.parent / 'release-witness').exists():
            if time.monotonic() >= deadline:
                raise AssertionError('test did not release witness')
            time.sleep(0.01)
        return read_child_epoch(*args, **kwargs)

    actual_run_owner = owner_start.run_owner

    def run_owner(root_arg, project, **options):
        options.update(authorize=authorize, launch=launch, birth_provider=synthetic_birth)
        return actual_run_owner(root_arg, project, **options)

    adt = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'adt'))
    with (patch.object(store_layout, 'production_root', return_value=root),
          patch.object(operator_terminal, '_installed_key', return_value=installed_key(pub, None)),
          patch.object(owner_start, 'run_owner', side_effect=run_owner)):
        if mode == 'blocked-restart':
            store_layout.open_catalogue = unexpected_open
            store_layout.open_ledger = unexpected_open
        if mode == 'witness-barrier':
            owner_start.read_child_epoch = at_witness
        raise SystemExit(adt_cli.main(argv, groups=(adt['_engine_owner_group'](),)))


class ForegroundOwnerTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(dir='/tmp'))).resolve()
        self.temp = self.base / 'tmp'
        self.temp.mkdir(mode=0o700)
        self.enterContext(patch.dict(os.environ, TMPDIR=str(self.temp)))
        self.root = self.base / '.adt'
        self.receipts = self.base / '.adt-owner' / 'receipts'
        self.receipts.parent.mkdir(mode=0o700)
        self.receipts.mkdir(mode=0o700)
        self.key = self.base / 'signer'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)],
                       check=True, capture_output=True, timeout=10)

    def _command(self, mode, initialize=True, project=P):
        command = [sys.executable, '-c',
                   'from relay_engine.tests.test_operator_daemon_join import foreground_fixture_main;'
                   'foreground_fixture_main()', mode, str(self.root), str(self.key),
                   'engine', 'run', '--store-root', str(self.root), '--project-id', project]
        if initialize:
            command += ['--initialize', '--project-name', 'Selected project']
        return command

    def _wait(self, observation):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = observation()
            if result:
                return result
            time.sleep(0.015)
        self.fail('foreground fixture did not reach the requested cut')

    def _receipt(self):
        files = list(self.receipts.iterdir())
        self.assertEqual(len(files), 1)
        return files[0].read_bytes()

    def _arm(self):
        return store_layout.IncumbentGuard._parse_record(
            (self.root / store_layout._INCUMBENT).read_bytes())

    def _child_pid(self):
        path = self.base / 'child'
        return self._wait(lambda: int(path.read_text() or '0') if path.exists() else None)

    @contextmanager
    def _owner(self, mode, initialize=True):
        child = subprocess.Popen(self._command(mode, initialize), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.communicate(timeout=6)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=3)
            child.stdout.close()
            child.stderr.close()

    def _ready(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 6)[0])
        line = process.stdout.readline().strip()
        self.assertTrue(line, process.stderr.read() if process.poll() is not None else b'')
        return Path(os.fsdecode(line))

    def _assert_retired(self, arm):
        idle, proof = store_layout._decode_idle_receipt(self._receipt())
        self.assertEqual((idle.lock, proof.prior_epoch), (arm.lock, arm.owner.epoch))
        with store_layout.IncumbentGuard.acquire(self.root, expected_lock=arm.lock) as guard:
            guard.verify_idle(idle, proof)

    def test_owner_terminal_ctrl_c_reaps_and_retains(self):
        import pty

        owner, terminal = pty.fork()
        if owner == 0:
            command = self._command('normal')
            os.execv(command[0], command)
        status = None
        try:
            limit = time.monotonic() + 8
            output = bytearray()
            while b'/ready.json' not in output and time.monotonic() < limit:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        output.extend(os.read(terminal, 4096))
                    except OSError:
                        break
            self.assertIn(b'/ready.json', output)
            child = self._child_pid()
            arm = self._arm()
            self.assertEqual(os.getpgid(owner), owner)
            self.assertEqual(os.getpgid(child), child)
            os.write(terminal, b'\x03')

            def reap():
                nonlocal status
                seen, value = os.waitpid(owner, os.WNOHANG)
                if seen:
                    status = value
                    return True
                return False

            self._wait(reap)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self._assert_retired(arm)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            os.close(terminal)
            if status is None:
                os.kill(owner, signal.SIGTERM)
                try:
                    self._wait(reap)
                except (AssertionError, UnboundLocalError):
                    os.kill(owner, signal.SIGKILL)
                    os.waitpid(owner, 0)

    def test_owner_latches_stop_during_spawn(self):
        with self._owner('spawn-barrier') as owner:
            child = self._child_pid()
            owner.send_signal(signal.SIGTERM)
            owner.send_signal(signal.SIGINT)
            (self.base / 'release-spawn').touch()
            owner.communicate(timeout=8)
            self.assertEqual(len((self.base / 'forwarded').read_text().splitlines()), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_owner_stop_during_witness_refuses_before_arm(self):
        with self._owner('witness-barrier') as owner:
            self._wait((self.base / 'witness-pending').exists)
            child = self._child_pid()
            receipt = self._receipt()
            idle = self._arm()
            self.assertEqual(idle.phase, 'idle')
            owner.terminate()
            (self.base / 'release-witness').touch()
            output, error = owner.communicate(timeout=8)
            self.assertNotEqual(owner.returncode, 0, error)
            self.assertEqual(output, b'')
            self.assertFalse((self.base / 'witness-acknowledged').exists())
            self.assertEqual(self._receipt(), receipt)
            self.assertEqual(self._arm(), idle)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def _assert_abnormal_loss_preserves_fence(self):
        with self._owner('normal') as owner:
            ready = self._ready(owner)
            self.assertTrue(ready.exists())
            self.assertEqual(ready.parent.parent, self.temp)
            arm = self._arm()
            prior = self._receipt()
            os.kill(self._child_pid(), signal.SIGKILL)
            _, error = owner.communicate(timeout=8)
            self.assertNotEqual(owner.returncode, 0, error)
            self.assertEqual(self._arm(), arm)
            self.assertEqual(self._receipt(), prior)

    def test_owner_incomplete_exit_preserves_fence(self):
        self._assert_abnormal_loss_preserves_fence()

    def test_owner_abnormal_loss_refuses_without_checkpoint(self):
        self._assert_abnormal_loss_preserves_fence()
        prior = self._receipt()
        marker = self.base / 'child'
        pid = marker.read_bytes()
        attempt = subprocess.run(self._command('blocked-restart', False), capture_output=True, timeout=8)
        self.assertNotEqual(attempt.returncode, 0)
        self.assertEqual(attempt.stdout, b'')
        self.assertFalse((self.base / 'ordinary-open').exists())
        self.assertEqual(marker.read_bytes(), pid)
        self.assertEqual(self._receipt(), prior)

    def test_owner_clean_restart_requires_retained_exact_receipt(self):
        with self._owner('normal') as first:
            self.assertTrue(self._ready(first).exists())
            arm = self._arm()
            first.terminate()
            _, error = first.communicate(timeout=8)
            self.assertEqual(first.returncode, 0, error)
        self._assert_retired(arm)
        accepted = self._receipt()
        wrong = subprocess.run(self._command('normal', False, str(uuid4())),
                               capture_output=True, timeout=8)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual(self._receipt(), accepted)
        with self._owner('lost-return', False) as second:
            self.assertTrue(self._ready(second).exists())
            next_arm = self._arm()
            self.assertNotEqual(next_arm.owner.epoch, arm.owner.epoch)
            self.assertEqual(next_arm.lock, arm.lock)
            second.terminate()
            second.communicate(timeout=8)
            self.assertNotEqual(second.returncode, 0)
        self.assertEqual(self._receipt(), accepted)
        retry = subprocess.run(self._command('normal', False), capture_output=True, timeout=8)
        self.assertNotEqual(retry.returncode, 0)
        self.assertEqual(retry.stdout, b'')

    def test_owner_retains_verified_retirement_without_ready(self):
        result = subprocess.run(self._command('pre-ready'), capture_output=True, timeout=8)
        self.assertEqual((result.returncode, result.stdout), (0, b''), result.stderr)
        idle, proof = store_layout._decode_idle_receipt(self._receipt())
        origin = json.loads(proof.evidence)['arm']
        self.assertEqual(origin['owner']['pid'], self._child_pid())
        self.assertEqual(proof.prior_epoch, origin['owner']['epoch'])
        with store_layout.IncumbentGuard.acquire(self.root, expected_lock=idle.lock) as guard:
            guard.verify_idle(idle, proof)
        self.assertFalse((self.root / lifecycle_paths._LIVE_LOCATOR).exists())
        self.assertFalse((self.base / 'ordinary-open').exists())

    def test_owner_missing_signer_reports_refusal_before_creation(self):
        result = subprocess.run(self._command('missing-signer'), capture_output=True, timeout=8)
        self.assertEqual((result.returncode, result.stdout), (1, b''))
        self.assertIn(b'Run adt operator install-check.', result.stderr)
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.receipts.iterdir()), [])

    def test_owner_rejects_valid_same_store_receipt_from_another_epoch_or_child(self):
        from relay_engine import owner_start

        with self._owner('normal') as first:
            self.assertTrue(self._ready(first).exists())
            first_arm = self._arm()
            first.terminate()
            _, error = first.communicate(timeout=8)
            self.assertEqual(first.returncode, 0, error)
        first_receipt = self._receipt()
        with self._owner('normal', False) as second:
            self.assertTrue(self._ready(second).exists())
            child_arm = self._arm()
            second.terminate()
            _, error = second.communicate(timeout=8)
            self.assertEqual(second.returncode, 0, error)
        current = self._receipt()
        with patch.object(store_layout, 'production_root', return_value=self.root):
            selection = owner_start.StartSelection.checked(self.root, child_arm.store, child_arm.project)
            for wrong_frame, arm in ((first_receipt, child_arm), (current, first_arm)):
                witness = owner_start.ChildEpochWitness(arm.root, arm.lock, arm.store, arm.project, arm.owner)
                with self.assertRaisesRegex(owner_start.StartupRefused, 'retirement-unproved'):
                    owner_start.retain_child_retirement(selection, wrong_frame, witness)
                self.assertEqual(self._receipt(), current)

    def test_owner_retention_failure_preserves_prior_receipt(self):
        from relay_engine import owner_start

        frames = []
        for initialize in (True, False):
            with self._owner('normal', initialize) as owner:
                self.assertTrue(self._ready(owner).exists())
                arm = self._arm()
                owner.terminate()
                _, error = owner.communicate(timeout=8)
                self.assertEqual(owner.returncode, 0, error)
            frames.append(self._receipt())
        prior, candidate = frames
        receipt, = self.receipts.iterdir()
        receipt.write_bytes(prior)
        original_fsync = os.fsync
        original_replace = os.replace
        fail_restore = False
        lock = self.root / store_layout._INCUMBENT_LOCK
        original_lock = lock.with_suffix('.saved')

        def replace_lock_after_publish(fd):
            if not original_lock.exists() and receipt.read_bytes() == candidate:
                lock.rename(original_lock)
                lock.touch(mode=0o600)
            original_fsync(fd)

        def reject_readback(fd):
            nonlocal fail_restore
            if not fail_restore and receipt.read_bytes() == candidate:
                fail_restore = True
                raise OSError('post-replace durability failure')
            original_fsync(fd)

        def reject_restore(src, dst, **kwargs):
            if fail_restore and dst == receipt.name:
                raise OSError('restore unavailable')
            original_replace(src, dst, **kwargs)

        witness = owner_start.ChildEpochWitness(arm.root, arm.lock, arm.store, arm.project, arm.owner)
        with patch.object(store_layout, 'production_root', return_value=self.root):
            selection = owner_start.StartSelection.checked(self.root, arm.store, arm.project)
            with (patch.object(owner_start.os, 'fsync', side_effect=replace_lock_after_publish),
                  self.assertRaisesRegex(owner_start.StartupRefused, 'retirement-unproved')):
                owner_start.retain_child_retirement(selection, candidate, witness)
            self.assertTrue(original_lock.exists())
            self.assertEqual(self._receipt(), prior)
            original_lock.replace(lock)
            with (patch.object(owner_start.os, 'fsync', side_effect=reject_readback),
                  patch.object(owner_start.os, 'replace', side_effect=reject_restore),
                  self.assertRaisesRegex(owner_start.StartupRefused, 'retirement-unproved')):
                owner_start.retain_child_retirement(selection, candidate, witness)
            self.assertTrue(fail_restore)
            self.assertIn(prior, [path.read_bytes() for path in self.receipts.iterdir()])
            with self.assertRaises(owner_start.StartupRefused):
                owner_start.read_owner_receipt(selection)
