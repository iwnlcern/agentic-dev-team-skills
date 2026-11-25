"""Exercise the read and inbox CLI over disposable in-process service sockets."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from relay_engine import adt_cli, api_client, header
from relay_engine.tests import test_api_relay_read as fixtures
from relay_engine.tests.test_submit import draft


INBOX = 'platform.implementer@project'


@contextmanager
def timezone(name):
    try:
        with patch.dict(os.environ, {'TZ': name}):
            time.tzset()
            yield
    finally:
        time.tzset()


class AdtReadsTests(unittest.TestCase):
    def setUp(self):
        self.store = fixtures.RelayReadTests()
        self.store.setUp()
        self.addCleanup(self.store.doCleanups)
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for key in ('ADT_READY_FILE', 'ADT_KEY_FILE', 'ADT_SESSION_ID'):
            os.environ.pop(key, None)

    def command(self, *args, **kwargs):
        raw, err = io.BytesIO(), io.StringIO()
        out = io.TextIOWrapper(raw, encoding='utf-8', newline='\n', write_through=True)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                status = adt_cli.main(args, **kwargs)
            out.flush()
            return status, raw.getvalue(), err.getvalue()
        finally:
            out.close()
            err.close()

    def window(self, role='implementer'):
        key = self.store.keys[role]
        path = self.store.base / f'{self.store.root.name}-{role}.key'
        if not path.exists():
            fixtures.f._private_file(path,
                f"adt-seat-key-v1 {key['key_id']} {key['key_secret']}\n".encode('ascii'))
        session = self.store.observe(lambda conn: conn.execute(
            'SELECT id FROM seat_sessions WHERE seat_id=? AND ended_at IS NULL',
            (role,),
        ).fetchone()[0])
        return adt_cli.WindowSession(str(path), session, self.store.ready.path)

    def read(self, relay_id, *, role='implementer', session_id=None, ready=None):
        record = self.window(role)
        return self.command('read', relay_id, '--key', record.key_path,
                            '--session-id', session_id or record.session_id,
                            '--ready', ready or record.ready_path)

    def inbox(self, *options, ready=None):
        return self.command('inbox', INBOX, '--ready', ready or self.store.ready.path, *options)

    def success(self, reply, *, status=0):
        self.assertEqual((reply[0], reply[2]), (status, ''), reply)
        return reply[1]

    def seed_live(self, count=3):
        self.store.start()
        writer = self.store.sign_in('planner')
        return [self.store.submit(writer, draft(to='platform.implementer',
                    subject=f'message {n}', bundle=f'b{n}'), f'notice-{n}')
                for n in range(count)]

    def test_adt_read_prints_lines_and_body(self):
        self.store.start()
        payload = 'relay café\n'
        _, row = self.store.simple(payload)
        actual = self.success(self.read(row['relay_id']))
        expected = (
            f"handle: {row['handle']}\nkind: SITREP\nsender: platform.planner@project\n"
            "to: platform.implementer@project\ncc: operator\nsubject: ordinary\n"
            "parent: -\nparent_id: -\nverdict: -\nneeds: none\n"
            f"created_at: {row['created_at']}\nimported: false\n\n{payload}"
        )
        self.assertEqual(actual, expected.encode('utf-8'))

    def test_adt_read_pointer_ids_read(self):
        pair = self.store.review_pair()
        out = self.success(self.read(pair['request']['relay_id'])).decode()
        pointers = [line.split()[1] for line in out.splitlines() if line.startswith('unopened: ')]
        self.assertEqual(pointers, [pair[name]['relay_id'] for name in ('oldest', 'ancestor')])
        for pointer in pointers:
            self.assertTrue(self.success(self.read(pointer)).startswith(b'handle: '))

    def test_adt_read_refs_and_findings_lines(self):
        pair = self.store.review_pair()
        reply = self.store.reply(pair)
        self.store.submit(pair['planner'], draft(kind='REVIEW-FOLD', to='platform.implementer',
            parent='#' + reply['handle'], fixes=('F1 ' + 'c' * 40,)), 'fold')
        out = self.success(self.read(reply['relay_id'], role='planner')).decode()
        lines = out.splitlines()[12:]
        first, second = pair['refs']
        self.assertEqual(lines[:5], [f'ref: 1 {first}', f'ref: 2 {second}',
                         'finding: F1 blocking fixed check 2 a',
                         'finding: F2 advisory open - b', ''])

    def test_adt_inbox_since_local_midnight(self):
        self.seed_live()
        operations = []
        real = api_client.ApiClient.call

        def record(client, name, args):
            if name == 'relay.inbox':
                operations.append(args)
            return real(client, name, args)

        with timezone('UTC+05'), patch.object(api_client.ApiClient, 'call', record):
            self.success(self.inbox('--since', '20261115'), status=0)
            self.assertEqual(self.inbox('--since', '20261131')[0], 2)
        offset = datetime.timezone(datetime.timedelta(hours=-5))
        midnight = datetime.datetime(2026, 11, 15, tzinfo=offset)
        self.assertEqual(operations, [{'seat_address': INBOX, 'limit': '100',
                                      'since_ms': str(int(midnight.timestamp()) * 1000)}])

    def test_adt_inbox_json_and_summary(self):
        rows = self.seed_live()
        expected = self.store.success(self.store.client().call('relay.inbox',
            {'seat_address': INBOX, 'before_handle': '4', 'limit': '1'}))
        opts = ('--before-handle', '4', '--limit', '1')
        data = json.loads(self.success(self.inbox('--json', *opts), status=3))
        self.assertEqual(data, expected | {'seat_address': INBOX})
        out = self.success(self.inbox(*opts), status=3).decode()
        first, only = out.splitlines()
        self.assertEqual(first, f'{INBOX}: 3 relays TO it in this range; 0 answered, '
                         f"3 unanswered, 0 imported; 1 shown; next: --before-handle {rows[-1]['handle']}")
        self.assertRegex(only, rf"^#{rows[-1]['handle']} {rows[-1]['relay_id']} unanswered none "
                               r"platform\.planner@project [0-9]{8}-[0-9]{6} message 2$")

    def test_adt_read_resolver_no_sign_in(self):
        self.store.start()
        _, row = self.store.simple()
        record = self.window()
        sent = []
        real = api_client.ApiClient.call

        def capture(client, name, args):
            sent.append(name)
            return real(client, name, args)

        with patch.object(api_client.ApiClient, 'call', capture):
            out = self.success(self.command('read', row['relay_id'], resolver=lambda: record))
        self.assertIn(b'subject: ordinary\n', out)
        self.assertEqual(sent, ['protocol.connect', 'session.resume', 'relay.read'])

    def test_no_service_exit_75(self):
        self.store.start()
        _, row = self.store.simple()
        ready = Path(self.store.ready.path)
        original = json.loads(ready.read_bytes())
        stale, refused, absent = (ready.with_name(name) for name in ('stale.json', 'refused.json', 'absent.json'))
        fixtures.f._private_file(stale, json.dumps(original | {'schema_digest': '0' * 64}).encode())
        fixtures.f._private_file(refused, json.dumps(original | {'socket': str(ready.parent / 'no.sock')}).encode())
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('read commands must not launch')) as launch:
            for path in (absent, stale, refused):
                with self.subTest(path=path.name):
                    for reply in (self.read(row['relay_id'], ready=str(path)), self.inbox(ready=str(path))):
                        self.assertEqual((reply[0], reply[1]), (75, b''), reply)
            launch.assert_not_called()
        self.assertFalse(absent.exists())

    def test_present_invalid_ready_file_exit_1(self):
        self.store.start()
        _, row = self.store.simple()
        for command in ('read', 'inbox'):
            with self.subTest(command=command):
                ready = Path(self.store.ready.path).with_name(f'invalid-{command}.json')
                fixtures.f._private_file(ready, b'{')
                with patch.object(subprocess, 'Popen',
                                  side_effect=AssertionError('read commands must not launch')) as launch:
                    if command == 'read':
                        status, out, err = self.read(row['relay_id'], ready=str(ready))
                    else:
                        status, out, err = self.inbox(ready=str(ready))
                    launch.assert_not_called()
                self.assertEqual(ready.read_bytes(), b'{')
                self.assertEqual((status, out), (1, b''), err)
                self.assertEqual(err, '{"local":"ready_file_invalid"}\n')

    def test_adt_inbox_walk_printed_next(self):
        expected = self.seed_live(5)
        opts = ('--limit', '2')
        found = []
        for _ in range(3):
            summary, *rows = self.success(self.inbox(*opts), status=3).decode().splitlines()
            found.extend(line.split()[1] for line in rows)
            if 'next: --before-handle ' not in summary:
                break
            opts = ('--limit', '2', '--before-handle', summary.rsplit(' ', 1)[-1])
        self.assertEqual(found, [item['relay_id'] for item in reversed(expected)])
        self.assertNotIn('next:', summary)

    def test_adt_inbox_exit_three(self):
        self.store.start()
        writer, reader = (self.store.sign_in(role) for role in ('planner', 'implementer'))
        for mode in ((), ('--json',)):
            self.success(self.inbox(*mode), status=0)
        answered = self.store.submit(writer, draft(to='platform.implementer'), 'answered')
        self.store.submit(reader, draft(to='platform.planner', parent='#' + answered['handle']), 'answer')
        for mode in ((), ('--json',)):
            self.success(self.inbox(*mode), status=0)
        pending = self.store.submit(writer, draft(to='platform.implementer'), 'pending')
        shown = self.store.submit(writer, draft(to='platform.implementer'), 'shown')
        self.store.submit(reader, draft(to='platform.planner', parent='#' + shown['handle']), 'answer-shown')
        self.assertLess(int(pending['handle']), int(shown['handle']))
        for mode in ((), ('--json',)):
            out = self.success(self.inbox('--limit', '1', *mode), status=3)
            self.assertIn(shown['relay_id'].encode(), out)
            self.assertNotIn(pending['relay_id'].encode(), out)
            self.success(self.inbox('--before-handle', '1', *mode), status=0)
        self.store.root = self.store.base / 'only-imported'
        self.store.start(seed=lambda conn: fixtures.f._import(
            conn, next(self.store.clock), 'history', ('implementer',)))
        for mode in ((), ('--json',)):
            self.success(self.inbox(*mode), status=0)

    def test_adt_inbox_fallback_line(self):
        def seed(conn):
            for subject in ('s' * 16385, 'invisible\x00byte'):
                fixtures.f._import(conn, next(self.store.clock), subject, ('implementer',))
        self.store.start(seed=seed)
        out = self.success(self.inbox()).decode()
        lines = out.splitlines()[1:]
        fallback = '(subject not shown: over 16,384 characters or holding a character v3 cannot send)'
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.endswith(fallback) for line in lines), lines)

    def test_adt_read_secret_absent(self):
        self.store.start()
        _, row = self.store.simple()
        secret = self.store.keys['implementer']['key_secret']
        success = self.read(row['relay_id'])
        refusal = self.read(row['relay_id'], session_id='not-the-session')
        self.assertEqual((success[0], refusal[0]), (0, 1))
        for _, out, err in (success, refusal):
            self.assertNotIn(secret.encode(), out)
            self.assertNotIn(secret, err)

    def test_subject_line_break_stays_one_line(self):
        self.store.start(seed=lambda conn: fixtures.f._import(
            conn, next(self.store.clock), 'first\nsecond\x85third', ('implementer',)))
        _, *lines = self.success(self.inbox()).decode().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(r'first\u000asecond\u0085third'))

    def test_since_first_instant_of_local_date(self):
        with timezone('America/Los_Angeles'):
            midnight = adt_cli.local_midnight_ms('20261101')
            self.assertEqual(midnight % 1000, 0)
            self.assertEqual(time.strftime('%Y%m%d %H%M%S', time.localtime(midnight // 1000)),
                             '20261101 000000')
            self.assertEqual(time.strftime('%Y%m%d', time.localtime((midnight - 1) // 1000)),
                             '20261031')
            with self.assertRaises(ValueError):
                adt_cli.local_midnight_ms('20260229')

    def test_adt_read_escapes_live_values(self):
        self.store.start()
        writer = self.store.sign_in('planner')
        self.store.sign_in('implementer')
        ref = self.store.base / 'check\u200b.txt'
        ref.write_bytes(b'check\n')
        subject = 'non\u00a0breaking'
        body = 'body\u00a0\u200b\n'
        row = self.store.submit(writer, draft(to='platform.implementer', subject=subject,
                    refs=('file:' + str(ref),), body=body), 'unicode')
        read = self.success(self.read(row['relay_id']))
        lines, raw = read.split(b'\n\n', 1)
        self.assertIn(b'subject: non\\u00a0breaking', lines.splitlines())
        self.assertIn(b'ref: 1 file:' + str(ref).replace('\u200b', r'\u200b').encode(), lines.splitlines())
        self.assertEqual(raw, body.encode())
        answer = json.loads(self.success(self.inbox('--json'), status=3))
        self.assertEqual(answer['rows'][0]['subject'], subject)

    def test_printable_matches_display_escapes(self):
        for codepoint in range(0x30000):
            if 0xD800 <= codepoint <= 0xDFFF:
                continue
            char = chr(codepoint)
            self.assertEqual(adt_cli.printable(char), header._display(char, limit=1),
                             f'U+{codepoint:04X}')
        self.assertEqual(adt_cli.printable('x' * 200), 'x' * 200)
