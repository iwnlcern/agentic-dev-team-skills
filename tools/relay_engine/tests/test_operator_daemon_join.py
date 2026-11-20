"""Exercise the checked launcher, guarded bridge and real Store on one scratch project."""
import base64
from concurrent.futures import ThreadPoolExecutor
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
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from relay_engine import (
    api_client, api_operator_bridge, api_service, api_writer, jcs, lifecycle_paths, queries, store_layout, submit,
)
from relay_engine.operator_queue import notifier, records
from relay_engine.tests import test_api_service as service_fixture
from relay_engine.tests.test_api_service import P
from relay_engine.tests.test_submit import draft


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

    def wait_until(self, observe):
        deadline = time.monotonic() + 6
        while True:
            result = observe()
            if result:
                return result
            self.assertLess(time.monotonic(), deadline, 'timed out waiting for durable state')
            time.sleep(0.05)

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
                target = self.wait_until(lambda: self.live_target(self.root))
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
                discovery = self.request(origin, '/api/connect?min_protocol=3.0&max_protocol=3.0')
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
                    self.wait_until(started.exists)
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
        retiring, claimed, eof = arm / 'retiring', arm / 'claimed', arm / 'eof'
        after_exit = arm / 'after-exit'
        # The test hook loads in the actual installed launcher processes. It
        # parks the specified boundary without changing any shipped bytes.
        (arm / 'sitecustomize.py').write_text(f"""
import json, os, pathlib, socket, subprocess, sys, threading, time
phase = {phase!r}
parked = pathlib.Path({str(parked)!r})
release = pathlib.Path({str(release)!r})
issued = pathlib.Path({str(issued)!r})
reaped = pathlib.Path({str(reaped)!r})
retiring = pathlib.Path({str(retiring)!r})
claimed = pathlib.Path({str(claimed)!r})
eof = pathlib.Path({str(eof)!r})
after_exit = pathlib.Path({str(after_exit)!r})
def wait_for(path):
    deadline = time.monotonic() + 9
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
def park(value):
    parked.write_text(str(value))
    wait_for(release)
def native(argv, **kwargs):
    if phase == 'late_invocation':
        park('before invocation')
    child = original_popen(argv, **kwargs)
    issued.write_text(str(child.pid))
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
        def start_custodian(argv, **kwargs):
            child = original_popen(argv, **kwargs)
            if '--private-notifier-custodian' in argv:
                park(child.pid)
            return child
        subprocess.Popen = start_custodian
    elif phase == 'before_final':
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
                    self.assertFalse(ready.exists())
                    with (root / 'api-service.lock').open('rb') as original:
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
                early = phase in {'pre_ready', 'before_final'}
                if early:
                    time.sleep(0.25)
                    release.touch()
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
                                  'eof': eof.exists(), 'stop_unavailable_to_custodian': phase != 'pre_ready'}))
            finally:
                after_exit.touch()
                release.touch()
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.communicate(timeout=3)

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
