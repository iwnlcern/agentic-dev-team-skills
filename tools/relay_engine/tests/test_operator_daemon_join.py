"""Exercise the checked launcher, guarded bridge and real Store on one scratch project."""
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
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

from relay_engine import api_client, api_operator_bridge, api_writer, jcs, lifecycle_paths, queries, store_layout
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
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''})
        env.start()
        self.addCleanup(env.stop)
        catalogue = store_layout.open_catalogue(self.root)
        try:
            catalogue.execute(
                "INSERT INTO project_routes (project_id,name,state,created_at) VALUES (?,?,'active',0)",
                (P, 'service')).close()
            catalogue.commit()
        finally:
            catalogue.close()
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
        with subprocess.Popen(
            [str(cli), 'engine', 'serve', '--store-root', str(self.root), '--project-id', P,
             '--bridge-port', '0'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as daemon:
            try:
                target = lifecycle_paths.console_target(str(self.root))
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

    def test_installed_cli_delivers_and_reaps_held_notifier_on_term(self):
        checker_path = Path(__file__).resolve().parents[2] / 'check-generate-plugins.py'
        spec = importlib.util.spec_from_file_location('check_generate_plugins', checker_path)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        installed = checker.install_tier('adt-orchestrator', self.base / 'installed')

        tools = installed / 'tools'
        launcher = tools / 'adt'
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
        shutil.copytree(self.root, root)
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

        catalogue = store_layout.open_catalogue(root)
        try:
            with catalogue:
                catalogue.execute('INSERT INTO queue_notifier_health VALUES (1,2,7)').close()
        finally:
            catalogue.close()

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

        with subprocess.Popen(command, cwd=arm, env=environment, start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as daemon:
            try:
                store_id = store_layout.read_store_id(root)
                ready = Path(lifecycle_paths.ready_file_path(
                    store_id, platform=sys.platform, environ=environment, home=str(home)))
                self.wait_until(ready.exists)
                data = json.loads(ready.read_bytes())
                origin = data['bridge_origin']
                peer = self.connect(data)
                self.sign_in(peer)
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

                discovery = self.request(origin, '/api/connect?min_protocol=3.0&max_protocol=3.0')
                self.assertEqual(discovery[0], 200)
                self.assertEqual(json.loads(discovery[2])['result']['schema_digest'], data['schema_digest'])
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
        shutil.copytree(self.root, root)
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
    elif phase in ('before_final', 'overlap'):
        gate = int(sys.argv[3])
        reads = 0
        read = os.read
        def final_read(fd, size):
            global reads
            if fd == gate:
                reads += 1
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
        with subprocess.Popen(command, cwd=arm, env=env, start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as daemon:
            try:
                ready = Path(lifecycle_paths.ready_file_path(store_layout.read_store_id(root),
                             platform=sys.platform, environ=env, home=str(home)))
                if phase != 'pre_ready':
                    self.wait_until(ready.exists)
                    data = json.loads(ready.read_bytes())
                    peer = self.connect(data)
                    self.sign_in(peer)
                    raw = draft(kind='QUESTION', to='operator', needs='answer', subject='Local decision',
                                body='Can we proceed?\n')
                    reply = peer.call('relay.submit', {'draft_base64': base64.b64encode(raw).decode(),
                                                       'submission_key': 'question'})
                    self.assertIn('result', reply)
                self.wait_until(lambda: parked.exists() and parked.stat().st_size)
                if phase == 'pre_ready':
                    custodian = int(parked.read_text())
                    self.assertFalse(ready.exists())
                    self.assertFalse((root / 'api-service.lock').exists())
                    self.verify_custodian_inventory(custodian, daemon.pid, root, ready.parent)
                self.assertEqual(issued.exists(), phase == 'late_return')
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
