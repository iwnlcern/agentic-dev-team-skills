"""Exercise the checked launcher, guarded bridge and real Store on one scratch project."""
import base64
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket as network
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from relay_engine import api_client, api_operator_bridge, jcs, lifecycle_paths, queries, store_layout
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

        program = self.base / 'terminal-notifier'
        held_message = '\\Held local decision'
        program.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, sys, time
held = sys.argv[-1] == {held_message!r}
if held:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = pathlib.Path({str(self.base)!r}) / ('held' if held else 'sent')
with path.with_suffix('.pending').open('w') as stream:
    json.dump({{'pid': os.getpid(), 'argv': sys.argv[1:]}}, stream)
path.with_suffix('.pending').replace(path)
if held:
    time.sleep(15)
""")
        program.chmod(0o700)

        def submit_question(peer, subject):
            raw = draft(kind='QUESTION', to='operator', needs='answer',
                        subject=subject, body='Can we proceed?\n')
            reply = peer.call('relay.submit', {'draft_base64': base64.b64encode(raw).decode(),
                                               'submission_key': subject})
            self.assertIn('result', reply)
            return reply['result']['relay_id']

        def outbox(relay_id, *, stopped=False):
            catalogue = (store_layout.open_catalogue(self.root, kind='isolated') if stopped else
                         store_layout.open_catalogue_readonly(self.root))
            try:
                return catalogue.execute(
                    'SELECT n.state,n.attempts,n.last_error_class,n.revision FROM queue_notifications n '
                    'JOIN queue_items i ON i.item_id=n.item_id WHERE i.source_relay_id=?',
                    (relay_id,)).fetchall()
            finally:
                catalogue.close()

        environment = {'HOME': str(self.home), 'TMPDIR': str(self.temp),
                       'PYTHONDONTWRITEBYTECODE': '1'}
        command = [sys.executable, str(installed / 'tools' / 'adt'), 'engine', 'serve',
                   '--store-root', str(self.root), '--project-id', P, '--bridge-port', '0',
                   '--terminal-notifier', str(program)]
        with subprocess.Popen(command, cwd=self.base, env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as daemon:
            try:
                ready = Path(lifecycle_paths.console_target(str(self.root)).ready_path)
                self.wait_until(ready.exists)
                data = json.loads(ready.read_bytes())
                origin = data['bridge_origin']
                peer = self.connect(data)
                self.sign_in(peer)
                sent_id = submit_question(peer, 'Local decision')
                sent = self.base / 'sent'
                self.wait_until(sent.is_file)
                sent_record = json.loads(sent.read_bytes())
                self.assertEqual(sent_record['argv'],
                                 ['-title', '\\service: platform.planner', '-message', '\\Local decision'])
                delivered_row = ('delivered', 0, None, 1)
                self.wait_until(lambda: outbox(sent_id) == [delivered_row])

                held_id = submit_question(peer, 'Held local decision')
                held = self.base / 'held'
                self.wait_until(held.is_file)
                held_record = json.loads(held.read_bytes())
                self.assertEqual(held_record['argv'],
                                 ['-title', '\\service: platform.planner', '-message', held_message])
                os.kill(held_record['pid'], 0)
                self.assertEqual(self.request(origin, '/')[0], 200)
                self.connect(data)
                pending_row = ('pending', 0, None, 1)
                self.assertEqual(outbox(held_id), [pending_row])

                started = time.monotonic()
                daemon.send_signal(signal.SIGTERM)
                _, stderr = daemon.communicate(timeout=3)
                elapsed = time.monotonic() - started
                self.assertEqual(daemon.returncode, 0, stderr)
                self.assertLess(elapsed, 3)
                with self.assertRaises(ProcessLookupError):
                    os.kill(held_record['pid'], 0)
                self.assertFalse(ready.parent.exists())
                with self.assertRaises(OSError):
                    self.request(origin, '/')
                self.assertEqual(outbox(sent_id, stopped=True), [delivered_row])
                self.assertEqual(outbox(held_id, stopped=True), [pending_row])
                print(json.dumps({'sent': sent_record, 'held': held_record,
                                  'delivered': delivered_row, 'cancelled': pending_row,
                                  'term_seconds': round(elapsed, 3), 'owned_cleanup': True}))
            finally:
                if daemon.poll() is None:
                    daemon.terminate()
                    try:
                        daemon.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.communicate(timeout=3)
