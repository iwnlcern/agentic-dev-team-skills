"""Unsigned inbox reads through dispatch on disposable, served stores."""
from contextlib import closing
from dataclasses import replace
import base64
import itertools
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from relay_engine import api_platform, api_protocol as p, api_service, api_wire as w
from relay_engine import api_writer, imports, jcs, queries, store_layout
from relay_engine.tests import relay_reads_fixtures as fixtures
from relay_engine.tests.test_api_service import admitted_store_fixture, seed_service_store
from relay_engine.tests.test_submit import draft

PROJECT = fixtures.PROJECT
ADDRESS = 'platform.implementer@project'
MAX_ID = '18446744073709551615'
COUNTS = ('total', 'answered', 'unanswered', 'imported')


class InboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='inbox-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'store'
        self.tick = itertools.count(1_700_000_000_000)
        self.keys = {}

    def start(self, relays=0, *, extra=None, catalogue=None, receipt=None):
        def seed(root):
            seed_service_store(root, self.keys, self.tick)
            with closing(store_layout.open_ledger(root, PROJECT)) as conn, conn:
                conn.execute('BEGIN IMMEDIATE').close()
                for index in range(relays):
                    fixtures._import(conn, next(self.tick), f'subject-{index}', ('implementer',))
                if extra:
                    extra(conn)
            if catalogue:
                with closing(store_layout.open_catalogue(root)) as conn, conn:
                    catalogue(conn)

        if receipt is None:
            receipt = admitted_store_fixture(
                self.root, PROJECT, project_name='project', seed=seed).receipt
        self.service = api_service.ApiService(str(self.root), PROJECT,
                                              clock=lambda: next(self.tick), admission=receipt)
        self.addCleanup(self.service.stop)
        self.service.start()

    def dispatch(self, operation, args, *, binding=None, request_id=MAX_ID):
        registry, runtime = self.service.registry, self.service.runtime
        connect = p.dispatch_api(p.initial_connection_state(), w.ApiRequest('1', 'protocol.connect',
            {'supported': {'min': '3.0', 'max': '3.0'}}), runtime, registry, self.service.operation_ports)
        self.assertIn('result', connect.response, connect.response)
        state = connect.state
        if binding is not None:
            state = replace(state, binding=binding)
        outcome = p.dispatch_api(state, w.ApiRequest(request_id, operation, args), runtime,
                                 registry, self.service.operation_ports)
        self.assertFalse(outcome.close, outcome.response)
        return outcome

    def reply(self, **args):
        return self.dispatch('relay.inbox',
            {'seat_address': ADDRESS, 'limit': '10'} | args).response

    def result(self, **args):
        response = self.reply(**args)
        self.assertIn('result', response, response)
        return response['result']

    def cause(self, expected, **args):
        response = self.reply(**args)
        self.assertEqual(response.get('error', {}).get('cause', {}).get('kind'), expected, response)

    def engine(self, *, seat='implementer', limit=10, **bounds):
        return self.service.writer.read(PROJECT, lambda conn: queries.addressed_to(
            conn, PROJECT, seat, limit=limit, **bounds))

    def send(self, role, content, key):
        sign_in = self.dispatch('session.sign-in',
            self.keys[role] | {'host': 'codex', 'host_session_id': 'inbox-' + role,
                               'operation_key': 'inbox-' + role}, request_id='2')
        self.assertIn('result', sign_in.response, sign_in.response)
        response = self.dispatch('relay.submit',
            {'draft_base64': base64.b64encode(content).decode('ascii'), 'submission_key': key},
            binding=sign_in.state.binding).response
        self.assertIn('result', response, response)
        return response['result']

    def assert_counts(self, result, page):
        self.assertEqual(tuple(result[key] for key in COUNTS),
                         tuple(str(getattr(page, key)) for key in COUNTS))

    def test_inbox_lists_with_addresses(self):
        self.start(3)
        self.send('planner', draft(to='platform.implementer', subject='live'), 'live')
        initial = self.engine()
        low, high = initial.rows[-2].created_at, initial.rows[0].handle + 1
        result = self.result(limit='2', since_ms=str(low), before_handle=str(high))
        expected = self.engine(limit=2, since_ms=low, before_handle=high)
        self.assert_counts(result, expected)
        self.assertEqual(result['truncated'], 'true')
        self.assertEqual(result['rows'], [
            {'relay_id': row.relay_id, 'handle': str(row.handle), 'created_at': str(row.created_at),
             'sender': {'operator': 'operator', 'planner': 'platform.planner@project'}[row.sender],
             'subject': row.subject, 'subject_over_bound': 'false', 'needs': row.needs,
             'state': row.state} for row in expected.rows])

    def test_inbox_address_refusals(self):
        def routes(conn):
            unserved, creating = str(uuid4()), str(uuid4())
            store_layout.reserve_project(conn, unserved, 'unserved', 1)
            store_layout.activate_project(conn, unserved)
            store_layout.reserve_project(conn, creating, 'creating', 1)

        self.start(catalogue=routes)
        for address, cause in (
            ('platform.implementer', 'invalid_args'), ('operator', 'invalid_args'),
            ('platform.implementer@unknown', 'project_unknown'),
            ('platform.implementer@unserved', 'runtime_not_ready'),
            ('platform.implementer@creating', 'runtime_not_ready'),
            ('missing.implementer@project', 'seat_unknown'),
            ('platform.reviewer@project', 'seat_unknown'),
        ):
            with self.subTest(address=address):
                self.cause(cause, seat_address=address)

    def test_inbox_stood_down_seat_listed(self):
        def stand_down(conn):
            conn.execute("UPDATE seats SET state='stood-down' WHERE id='implementer'").close()

        self.start(1, extra=stand_down)
        result = self.result()
        self.assertEqual(result['total'], '1')
        self.assertEqual(result['rows'][0]['relay_id'], self.engine().rows[0].relay_id)

    def test_inbox_bounds_refused_before_read(self):
        self.start()
        with patch.object(self.service.writer, 'read', wraps=self.service.writer.read) as read:
            for field, value in (('limit', 0), ('limit', 501), ('since_ms', 2**48),
                                 ('before_handle', 0), ('before_handle', 2**63)):
                with self.subTest(field=field, value=value):
                    self.cause('invalid_args', **{field: str(value)})
            read.assert_not_called()
            for field, value in (('limit', 1), ('limit', 500), ('since_ms', 2**48 - 1),
                                 ('before_handle', 1), ('before_handle', 2**63 - 1)):
                with self.subTest(field=field, value=value):
                    self.result(**{field: str(value)})
            self.assertEqual(read.call_count, 5)

    def test_inbox_writes_nothing(self):
        self.start(1)
        allowed = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION}
        denials, changes = [], []
        original = self.service.writer.read

        def read(project_id, callback):
            def checked(conn):
                def authorize(action, *detail):
                    if action not in allowed:
                        denials.append((action, detail))
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                conn.set_authorizer(authorize)
                result = callback(conn)
                changes.append(conn.total_changes)
                return result
            return original(project_id, checked)

        with patch.object(self.service.writer, 'read', side_effect=read):
            self.assertEqual(self.result()['total'], '1')
        self.assertEqual(denials, [])
        self.assertEqual(changes, [0])

    def test_inbox_two_stages(self):
        self.start(1)
        with patch.object(store_layout, 'open_ledger_readonly',
                          wraps=store_layout.open_ledger_readonly) as opened:
            self.cause('project_unknown', seat_address='platform.implementer@missing')
            opened.assert_not_called()
        from relay_engine import api_relay_reads_service as reads
        events = []
        original_read = self.service.writer.read
        resolve, addressed = reads.resolve_seat, queries.addressed_to

        def read(project_id, callback):
            def checked(conn):
                events.append(('port', conn, conn.in_transaction))
                return callback(conn)
            return original_read(project_id, checked)

        def lookup(conn, *args):
            events.append(('resolve', conn, conn.in_transaction))
            return resolve(conn, *args)

        def list_to(conn, *args, **kwargs):
            events.append(('addressed', conn, conn.in_transaction))
            return addressed(conn, *args, **kwargs)

        with (patch.object(self.service.writer, 'read', side_effect=read),
              patch.object(reads, 'resolve_seat', side_effect=lookup),
              patch.object(queries, 'addressed_to', side_effect=list_to)):
            self.assertEqual(self.result()['total'], '1')
        self.assertEqual([item[0] for item in events], ['port', 'resolve', 'addressed'])
        self.assertEqual([item[1] for item in events], [events[0][1]] * 3)
        self.assertEqual([item[2] for item in events], [False] * 3)

    def test_inbox_walk_by_before_handle(self):
        self.start(7)
        all_rows = self.engine().rows
        seen, bounds = [], {}
        for total in (7, 4, 1):
            page = self.result(limit='3', **bounds)
            self.assertEqual(tuple(page[key] for key in COUNTS), (str(total), '0', '0', str(total)))
            self.assertEqual(page['truncated'], 'true' if total > 3 else 'false')
            self.assertEqual(len(page['rows']), min(total, 3))
            seen.extend(row['relay_id'] for row in page['rows'])
            bounds = {'before_handle': page['rows'][-1]['handle']}
        self.assertEqual(seen, [row.relay_id for row in all_rows])
        self.assertEqual(self.result(**bounds)['total'], '0')

    def test_inbox_answers_rule_passthrough(self):
        self.start()
        target = self.send('planner', draft(to='platform.implementer', needs='act'), 'target')
        def open_target(conn):
            queries.read(conn, target['relay_id'], 'implementer', next(self.tick))
            return api_writer.JobOutcome(True, None)

        writer = self.service.writer
        writer.submit_callee(PROJECT, SimpleNamespace(
            files=frozenset({writer.ledger_file(PROJECT)}), run=open_target))
        reply = self.send('implementer', draft(to='platform.planner', bundle='another',
                          answers=('#' + target['handle'],)), 'answer')
        witness = self.service.writer.read(PROJECT, lambda conn: conn.execute(
            'SELECT r.parent_id, target.answered_by FROM relays r '
            'JOIN recipients target ON target.relay_id=? AND target.seat_id=? WHERE r.id=?',
            (target['relay_id'], 'implementer', reply['relay_id'])).fetchone())
        self.assertEqual(witness, (None, reply['relay_id']))
        page = self.result()
        self.assertEqual((page['answered'], page['unanswered']), ('1', '0'))
        self.assertEqual([(row['relay_id'], row['state']) for row in page['rows']],
                         [(target['relay_id'], 'answered')])

    def test_inbox_subject_over_bound_rows(self):
        subjects = ('ok', 's' * 16_385, 'four' + chr(0) + '12345', 'b' * 16_384)

        def seed(conn):
            for subject in subjects:
                fixtures._import(conn, next(self.tick), subject, ('implementer',))

        self.start(extra=seed)
        result = self.result()
        expected = self.engine()
        self.assert_counts(result, expected)
        self.assertEqual([row['relay_id'] for row in result['rows']],
                         [row.relay_id for row in expected.rows])
        self.assertEqual([(row['subject'], row['subject_over_bound']) for row in result['rows']],
                         [(subjects[3], 'false'), (None, 'true'), (None, 'true'), ('ok', 'false')])

    def test_inbox_page_ends_before_frame(self):
        fixture = fixtures.frame_page_store(str(self.root))
        receipt = store_layout._decode_idle_receipt(
            self.root.with_name(self.root.name + '.idle-receipt').read_bytes())
        self.start(receipt=receipt)
        expected = self.engine(seat=fixtures._seat(0, 'planner'), limit=500)
        self.assertEqual(expected.total, 500)
        self.assertFalse(expected.truncated)
        args = {'seat_address': fixtures._team(0) + '.planner@' + fixtures.PROJECT_NAME, 'limit': '500'}
        first = self.reply(**args)
        self.assertIn('result', first, {key: value for key, value in first.items() if key != 'result'})
        page = first['result']
        self.assert_counts(page, expected)
        self.assertEqual(page['truncated'], 'true')
        size = len(w.encode_api_response(first, self.service.registry, 'relay.inbox', None)) - 4
        self.assertLessEqual(size, w.MAX_API_FRAME)
        seen = [row['relay_id'] for row in page['rows']]
        order = [row.relay_id for row in expected.rows]
        self.assertTrue(0 < len(seen) < 500)
        self.assertEqual(seen, order[:len(seen)])
        self.assertEqual(set(order), set(fixture.relay_ids))
        while page['truncated'] == 'true':
            before = len(seen)
            page = self.result(**args, before_handle=page['rows'][-1]['handle'])
            self.assertEqual(page['total'], str(500 - before))
            self.assertTrue(0 < len(page['rows']) <= 500 - before)
            seen.extend(row['relay_id'] for row in page['rows'])
        self.assertEqual(seen, order)

    def test_inbox_address_forms(self):
        self.start()
        for address in ('platform.pair-planner@project', 'Platform.implementer@project',
                        'platform.implementer@Project', 'platform.implementer@@project'):
            with self.subTest(address=address):
                self.cause('invalid_args', seat_address=address)

    def test_closed_team_sender_renders_its_name(self):
        sent = {}

        def seed(conn):
            for team, closed in (('old', 2), ('new', None)):
                conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,"
                             "opened_by,closed_at) VALUES (?,?,'same','pair',1,'operator',?)",
                             (team, PROJECT, closed)).close()
                conn.execute("INSERT INTO seats(id,team_instance_id,role,state)"
                             " VALUES (?,?,'planner','vacant')", (team, team)).close()
                sent[team] = imports.import_relay_step(conn, PROJECT, imports.ImportedRelay(
                    created_at=next(self.tick), kind='SITREP', verdict=None, subject=team,
                    sender=team, sender_role='planner', team_instance_id=team,
                    to=('implementer',), cc=(), parent_id=None, parent_spelling=None,
                    body=b'', header_bytes=1, header_sha256='0' * 64), marked_at_ms=next(self.tick))

        self.start(extra=seed)
        result = self.result()
        self.assertEqual({row['relay_id']: row['sender'] for row in result['rows']},
                         {row: 'same.planner@project' for row in sent.values()})


class InboxShapeTests(unittest.TestCase):
    def test_fit_rows_boundary(self):
        from relay_engine.api_relay_reads_service import fit_rows
        head = {'total': '500', 'answered': '0', 'unanswered': '0', 'imported': '500',
                'truncated': 'false'}
        def row(subject):
            return {'relay_id': 'f' * 36, 'handle': '9223372036854775807',
                    'created_at': '281474976710655', 'sender': 't' * 64 +
                    '.orchestrator-reviewer@' + 'p' * 32, 'subject': subject,
                    'subject_over_bound': 'false', 'needs': 'none', 'state': 'imported'}

        def size(rows):
            return len(jcs.jcs_encode({'request_id': MAX_ID,
                'result': head | {'rows': rows, 'truncated': 'true' if len(rows) < 500 else 'false'}}))

        largest = row(chr(1) * 16_384)
        # All 500 rows fit exactly, accounting for the longer false flag.
        rows = [largest] * 160 + [row('')] * 340
        remaining = w.MAX_API_FRAME - size(rows)
        index = 160
        while remaining > 16_384:
            chars = min(16_384, remaining // 6)
            rows[index] = row(chr(1) * chars)
            remaining -= chars * 6
            index += 1
        rows[-1] = row('s' * remaining)
        self.assertEqual(size(rows), w.MAX_API_FRAME)
        self.assertEqual(fit_rows(head, rows), 500)
        rows[-1] = row('s' * (remaining + 1))
        self.assertEqual(size(rows), w.MAX_API_FRAME + 1)
        self.assertEqual(fit_rows(head, rows), 499)
        self.assertEqual(fit_rows(head, [largest]), 1)

    def test_inbox_metadata(self):
        op = api_platform.build_platform().registry.operation('relay.inbox')
        self.assertIsNotNone(op, 'missing relay.inbox')
        self.assertEqual((op.principal, op.effect, op.retry, op.browser_get),
                         ('unsigned', 'pure_observation', 'read_repeat', 'none'))
        self.assertEqual((op.request_ref.name, op.result_ref.name, op.error_ref.name),
                         ('RelayInboxArgs', 'RelayInboxResult', 'ApiError'))

    def test_reads_import_boundary_fresh_process(self):
        tools = str(Path(__file__).resolve().parents[2])
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from relay_engine import api_platform, api_relay_reads, api_schema
registry = api_platform.build_platform().registry
assert registry.operation('relay.inbox') is not None
api_schema.export_schema(registry, '3.0')
forbidden = {'sqlite3', 'relay_engine.queries', 'relay_engine.store_layout',
             'relay_engine.api_relay_reads_service'}
assert not forbidden & sys.modules.keys(), forbidden & sys.modules.keys()
'''
        outcome = subprocess.run([sys.executable, '-I', '-B', '-c', script, tools],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
