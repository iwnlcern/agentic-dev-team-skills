"""Signed relay reads over the API on disposable stores."""
import base64
from contextlib import closing
from dataclasses import fields
import itertools
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from relay_engine import api_client, api_platform, api_registry, api_service, api_wire
from relay_engine import jcs, queries, store_layout
from relay_engine.tests import relay_reads_fixtures as f
from relay_engine.tests.test_api_service import admitted_store_fixture, seed_service_store
from relay_engine.tests.test_api_sessions import replace_on_connection
from relay_engine.tests.test_submit import draft

PROJECT = f.PROJECT
MAX_REQUEST = '18446744073709551615'
ENVELOPE_KEYS = ('handle', 'kind', 'sender', 'to', 'cc', 'subject', 'parent', 'parent_id',
                 'verdict', 'needs', 'created_at', 'imported')


class RelayReadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='relay-read-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'store'
        self.clock = itertools.count(1_700_000_010_000)
        self.keys = {}

    def start(self, *, seed=None, receipt=None, project='project'):
        def prepare(root):
            seed_service_store(root, self.keys, self.clock)
            if seed:
                with closing(store_layout.open_ledger(root, PROJECT)) as conn, conn:
                    conn.execute('BEGIN IMMEDIATE').close()
                    seed(conn)
        if receipt is None:
            receipt = admitted_store_fixture(self.root, PROJECT, project_name=project,
                                              seed=prepare).receipt
        self.service = api_service.ApiService(str(self.root), PROJECT,
                                              clock=lambda: next(self.clock), admission=receipt)
        self.addCleanup(self.service.stop)
        self.ready = api_client.read_ready_file(self.service.start())
        acceptor = threading.Thread(target=self.service.serve_forever, name='relay-read-fixture')
        acceptor.start()

        service = self.service

        def stop_service():
            service.stop()
            acceptor.join(5)
            self.assertFalse(acceptor.is_alive())

        self.addCleanup(stop_service)

    def client(self):
        client = api_client.ApiClient.open(self.ready)
        self.addCleanup(client.close)
        return client

    def sign_in(self, role, *, suffix='original'):
        client = self.client()
        key = api_client.SeatKey(**self.keys[role])
        self.success(client.sign_in(key, 'codex', f'test-{role}-{suffix}', f'sign-{suffix}'))
        return client

    def pinned(self, builder):
        item = builder(str(self.root))
        receipt = store_layout._decode_idle_receipt(
            self.root.with_name(self.root.name + '.idle-receipt').read_bytes())
        self.start(receipt=receipt)
        client = self.client()
        key = api_client.read_key_file(item.reader_key)
        self.success(client.call('session.resume', {
            'key_id': key.key_id, 'key_secret': key.key_secret,
            'session_id': item.reader_session_id,
        }))
        return client, item.relay_ids[0]

    def success(self, response):
        self.assertNotIsInstance(response, api_client.ApiErrorValue, response)
        return response

    def refuse(self, response, code, cause):
        self.assertIsInstance(response, api_client.ApiErrorValue)
        self.assertEqual((response.code, dict(response.cause)), (code, {'kind': cause}))

    def read(self, client, relay):
        return client.call('relay.read', {'relay_id': relay})

    def submit(self, client, content, key):
        return self.success(client.call('relay.submit', {
            'draft_base64': base64.b64encode(content).decode('ascii'),
            'submission_key': key,
        }))

    def write(self, fn):
        writer = self.service.writer
        return writer.submit_callee(PROJECT, SimpleNamespace(
            files=frozenset({writer.ledger_file(PROJECT)}), run=fn))

    def observe(self, fn):
        return self.service.writer.read(PROJECT, fn)

    def markers(self, relay, seat='implementer'):
        def state(conn):
            with (closing(conn.execute('SELECT opened_at FROM recipients WHERE relay_id=? AND seat_id=?',
                                       (relay, seat))) as opened,
                  closing(conn.execute('SELECT last_request_at FROM seat_activity WHERE seat_id=?',
                                       (seat,))) as activity):
                return opened.fetchone(), activity.fetchone()
        return self.observe(state)

    def clear_fixture_import_open(self, relay, seat):
        # Import archives recipients with a historical open. Start the refusal
        # witness with no open so an erroneous call to the opener is visible.
        def clear(conn):
            with closing(conn.execute(
                'UPDATE recipients SET opened_at=NULL WHERE relay_id=? AND seat_id=?',
                (relay, seat),
            )) as cursor:
                self.assertEqual(cursor.rowcount, 1)
        self.write(clear)

    def simple(self, body='sample café\n'):
        writer = self.sign_in('planner')
        reader = self.sign_in('implementer')
        relay = self.submit(writer, draft(to='platform.implementer', cc='operator',
                                         subject='ordinary', body=body), 'root')
        return reader, relay

    def add_team(self, conn, name):
        conn.execute("INSERT INTO team_instances (id,project_id,name,level,opened_at,opened_by)"
                     " VALUES (?,?,?,'pair',1,'operator')", (name, PROJECT, name)).close()
        conn.execute("INSERT INTO seats (id,team_instance_id,role,state)"
                     " VALUES (?,?,'planner','vacant')", (name + '-planner', name)).close()

    def test_read_to_reader_opens_once(self):
        self.start()
        client, relay = self.simple()
        rid = relay['relay_id']
        self.assertEqual(self.markers(rid), ((None,), None))
        first = self.success(self.read(client, rid))
        self.assertEqual(base64.b64decode(first['body_base64']), 'sample café\n'.encode())
        self.assertEqual(set(first), {'relay_id', 'envelope', 'body_base64', 'unopened', 'refs', 'findings'})
        self.assertEqual(set(first['envelope']), set(ENVELOPE_KEYS))
        self.assertEqual(first['envelope'], {
            'handle': relay['handle'], 'kind': 'SITREP', 'sender': 'platform.planner@project',
            'to': ['platform.implementer@project'], 'cc': ['operator'], 'subject': 'ordinary',
            'parent': None, 'parent_id': None, 'verdict': None, 'needs': 'none',
            'created_at': relay['created_at'], 'imported': 'false',
        })
        opened, _ = self.markers(rid)
        self.assertIsNotNone(opened[0])
        self.success(self.read(client, rid))
        self.assertEqual(self.markers(rid)[0], opened)

    def test_read_review_pointers_readable(self):
        self.start()
        writer, reader = self.sign_in('planner'), self.sign_in('implementer')
        ids = []
        for index in range(3):
            ancestor = self.submit(writer, draft(to='platform.implementer',
                parent='#' + ids[-1]['handle'] if ids else None), f'ancestor-{index}')
            ids.append(ancestor)
        request = self.submit(writer, draft(kind='REVIEW', to='platform.implementer',
            needs='review', parent='#' + ids[-1]['handle']), 'request')
        result = self.success(self.read(reader, request['relay_id']))
        self.assertEqual(result['unopened'], [
            {'relay_id': row['relay_id'], 'handle': row['handle']} for row in ids])
        for pointer in result['unopened']:
            self.assertEqual(self.success(self.read(reader, pointer['relay_id']))['relay_id'],
                             pointer['relay_id'])

    def test_read_unknown_relay(self):
        self.start()
        client, relay = self.simple()
        snapshot = self.write(lambda conn: conn.total_changes)
        self.refuse(self.read(client, 'missing'), 'E_API_NOT_FOUND', 'relay_unknown')
        self.assertEqual(self.write(lambda conn: conn.total_changes), snapshot)
        self.assertEqual(self.markers(relay['relay_id']), ((None,), None))

    def test_read_unsigned_not_signed_in(self):
        self.start()
        _, relay = self.simple()
        rid = relay['relay_id']
        self.refuse(self.read(self.client(), rid), 'E_API_AUTH', 'not_signed_in')
        self.assertEqual(self.markers(rid), ((None,), None))

    def test_read_stale_binding_refused(self):
        for cause in ('key_replaced', 'seat_stood_down', 'session_not_current', 'current'):
            with self.subTest(cause=cause):
                self.root = self.base / cause
                self.start()
                client, relay = self.simple()
                rid = relay['relay_id']
                if cause == 'key_replaced':
                    def replace(conn):
                        with conn:
                            conn.execute('BEGIN IMMEDIATE').close()
                            replace_on_connection(conn, 'implementer', next(self.clock))
                    self.write(replace)
                elif cause == 'seat_stood_down':
                    def stand_down(conn):
                        conn.execute("UPDATE seats SET state='stood-down' WHERE id='implementer'").close()
                    self.write(stand_down)
                elif cause == 'session_not_current':
                    self.sign_in('implementer', suffix='newer')
                if cause == 'current':
                    self.success(self.read(client, rid))
                    self.assertIsNotNone(self.markers(rid)[0][0])
                else:
                    refused = self.read(client, rid)
                    self.assertEqual(self.markers(rid), ((None,), None))
                    self.refuse(refused, 'E_API_AUTH', cause)
                    self.refuse(self.read(client, rid), 'E_API_AUTH', 'not_signed_in')

    def test_read_again_same_answer(self):
        self.start()
        reader, relay = self.simple()
        rid = relay['relay_id']
        first = self.success(self.read(reader, rid))
        first_open, first_activity = self.markers(rid)
        self.assertEqual(first_open, first_activity)
        second = self.success(self.read(reader, rid))
        second_open, second_activity = self.markers(rid)
        self.assertEqual(first, second)
        self.assertEqual(first_open, second_open)
        self.assertGreater(second_activity[0], first_activity[0])

    def test_read_imported_metadata_over_bound(self):
        ids = []

        def seed(conn):
            for index in range(2048):
                self.add_team(conn, f'group-{index:04d}')
            base = ('implementer',)
            others = tuple(f'group-{index:04d}-planner' for index in range(2048))
            for subject, parties in (('s' * 16385, base), ('s', base + others),
                                     ('s' * 16384, base), ('s', base + others[:-1])):
                ids.append(f._import(conn, next(self.clock), subject, parties))
        self.start(seed=seed)
        client = self.sign_in('implementer')
        for rid in ids[:2]:
            self.clear_fixture_import_open(rid, 'implementer')
            self.refuse(self.read(client, rid), 'E_API_INTERNAL', 'handler_fault')
            self.assertEqual(self.markers(rid), ((None,), None))
        self.assertEqual(len(self.success(self.read(client, ids[2]))['envelope']['subject']), 16384)
        self.assertEqual(len(self.success(self.read(client, ids[3]))['envelope']['to']), 2048)
        writer = self.sign_in('planner')
        to = ', '.join(['platform.implementer'] + [f'group-{i:04d}.planner' for i in range(64)])
        live = self.submit(writer, draft(to=to), 'many')
        self.assertEqual(len(self.success(self.read(client, live['relay_id']))['envelope']['to']), 65)

    def test_read_live_request_at_caps(self):
        client, rid = self.pinned(f.live_request_store)
        result = self.success(self.read(client, rid))
        self.assertEqual(len(result['unopened']), 255)
        self.assertEqual(len(base64.b64decode(result['body_base64'])), f.BODY_BYTES)
        self.assertGreater(len(result['refs']), 0)
        self.assertEqual(result['findings'], [])

    def test_read_live_reply_at_caps(self):
        client, rid = self.pinned(f.live_reply_store)
        result = self.success(self.read(client, rid))
        self.assertEqual(len(base64.b64decode(result['body_base64'])), f.BODY_BYTES)
        self.assertGreater(len(result['refs']), 0)
        self.assertEqual(len(result['findings']), 200)
        self.assertEqual(result['unopened'], [])

    def test_read_import_at_value_bounds(self):
        client, rid = self.pinned(f.import_bound_store)
        result = self.success(self.read(client, rid))
        self.assertEqual(len(base64.b64decode(result['body_base64'])), f.IMPORT_BYTES)
        envelope = result['envelope']
        self.assertEqual(envelope['subject'], f.IMPORT_SUBJECT)
        self.assertEqual(envelope['sender'], 'operator')
        for role, field in (('planner', 'to'), ('implementer', 'cc')):
            self.assertEqual(envelope[field], [f'{f._team(i)}.{role}@{f.PROJECT_NAME}'
                                               for i in range(2048)])

    def test_read_body_over_bound_refused(self):
        client, rid = self.pinned(f.import_over_body_store)
        self.clear_fixture_import_open(rid, f._seat(0, 'planner'))
        self.refuse(self.read(client, rid), 'E_API_INTERNAL', 'handler_fault')
        self.assertEqual(self.markers(rid, f._seat(0, 'planner')), ((None,), None))

    def boundary_candidate(self, relay_id, extra=0):
        targets = ['x'] * 4096
        result = {'relay_id': relay_id, 'envelope': {
            'handle': '1', 'kind': 'SITREP', 'sender': 'operator',
            'to': targets[:2048], 'cc': targets[2048:], 'subject': '',
            'parent': None, 'parent_id': None, 'verdict': None, 'needs': 'none',
            'created_at': '1', 'imported': 'true',
        }, 'body_base64': '', 'unopened': [], 'refs': [], 'findings': []}
        body_width = 4 * ((f.IMPORT_BYTES + 2) // 3)
        delta = (api_wire.MAX_API_FRAME + extra - body_width -
                 len(jcs.jcs_encode({'request_id': MAX_REQUEST, 'result': result})))
        for index in range(len(targets)):
            if not delta:
                break
            count = min(255, delta // 6)
            if count:
                targets[index] += chr(1) * count
                delta -= count * 6
            else:
                targets[index] += 'x' * delta
                delta = 0
        self.assertEqual(delta, 0)
        result['envelope']['to'] = targets[:2048]
        result['envelope']['cc'] = targets[2048:]
        return result

    def test_answer_size_matches_jcs_at_frame(self):
        from relay_engine.api_relay_reads_service import answer_size
        registry = api_platform.build_platform().registry
        body = base64.b64encode(b'b' * f.IMPORT_BYTES).decode('ascii')
        for extra in (0, 1):
            with self.subTest(extra=extra):
                candidate = self.boundary_candidate('relay', extra)
                answer = {'request_id': MAX_REQUEST, 'result': candidate | {'body_base64': body}}
                valid = api_registry.validate_operation_result(registry, 'relay.read', answer['result'], None)
                self.assertIsInstance(valid, api_registry.ValidatedResult)
                measured = len(jcs.jcs_encode(answer))
                self.assertEqual(measured, api_wire.MAX_API_FRAME + extra)
                self.assertEqual(answer_size(candidate, f.IMPORT_BYTES), measured)
                if extra:
                    with self.assertRaises(api_wire.ApiFrameError):
                        api_wire.encode_api_response(answer, registry, 'relay.read', None)
                else:
                    self.assertEqual(len(api_wire.encode_api_response(answer, registry, 'relay.read', None)),
                                     measured + 4)

    def test_over_frame_values_never_open(self):
        from relay_engine import api_relay_reads_service as service
        self.start()
        client, relay = self.simple()
        rid = relay['relay_id']
        oversized = self.boundary_candidate(rid, 1)
        opener = Mock(wraps=queries.read)
        job = service.RelayReadJob

        def inject(*args, **kwargs):
            return job(*args, **kwargs, values=lambda *_: service.ReadCandidate(oversized, f.IMPORT_BYTES),
                       opener=opener)
        with patch.object(service, 'RelayReadJob', side_effect=inject):
            refused = self.read(client, rid)
        opener.assert_not_called()
        self.refuse(refused, 'E_API_INTERNAL', 'handler_fault')
        self.assertEqual(self.markers(rid), ((None,), None))

    def review_pair(self):
        self.start(seed=lambda conn: self.add_team(conn, 'closed'))
        planner, implementer = self.sign_in('planner'), self.sign_in('implementer')
        oldest = self.submit(planner, draft(to='platform.implementer'), 'oldest')
        ancestor = self.submit(planner, draft(to='platform.implementer',
            parent='#' + oldest['handle']), 'ancestor')
        evidence = self.base / 'check'
        evidence.write_text('FAILED\n')
        refs = ('#' + oldest['handle'], 'file:' + str(evidence))
        request = self.submit(planner, draft(kind='REVIEW', needs='review',
            to='platform.implementer', cc='closed.planner, operator',
            parent='#' + ancestor['handle'], refs=refs), 'request')
        opened = {
            'planner': planner, 'implementer': implementer, 'oldest': oldest,
            'ancestor': ancestor, 'request': request, 'refs': refs,
        }
        return opened

    def reply(self, pair):
        return self.submit(pair['implementer'], draft(kind='REVIEW', to='platform.planner',
            cc='closed.planner, operator', parent='#' + pair['request']['handle'],
            verdict='must-revise', refs=pair['refs'], findings=('blocking 2 a', 'advisory b')), 'reply')

    def close_team(self):
        def close(conn):
            conn.execute("UPDATE team_instances SET closed_at=? WHERE id='closed'",
                         (next(self.clock),)).close()
        self.write(close)

    def test_read_refs_and_findings(self):
        pair = self.review_pair()
        reply = self.reply(pair)
        rid = reply['relay_id']
        self.submit(pair['planner'], draft(kind='REVIEW-FOLD', to='platform.implementer',
                    parent='#' + reply['handle'], fixes=('F1 ' + 'c' * 40,)), 'fold')
        engine = self.write(lambda conn: queries.read(conn, rid, 'planner', next(self.clock)))
        self.assertEqual(engine.refs, (queries.RefLine(1, pair['refs'][0]),
                                       queries.RefLine(2, pair['refs'][1])))
        self.assertEqual(engine.findings, (queries.RaisedFinding(1, 'blocking', 'fixed', 'a', 2),
                                           queries.RaisedFinding(2, 'advisory', 'open', 'b', None)))
        answer = self.success(self.read(pair['planner'], rid))
        self.assertEqual(answer['refs'], [{'ordinal': str(line.ordinal), 'spelling': line.spelling}
                                          for line in engine.refs])
        self.assertEqual(answer['findings'], [
            {'handle': str(item.handle), 'severity': item.severity, 'status': item.status,
             'summary': item.summary, 'check': None if item.check is None else str(item.check)}
            for item in engine.findings])

    def compare_answered(self, relay, seat, reader):
        from relay_engine.api_relay_reads_service import read_candidate
        rid = relay['relay_id']
        self.close_team()
        candidate = self.observe(lambda conn: read_candidate(conn, rid, seat))
        answer = self.success(self.read(reader, rid))
        self.assertEqual(tuple(candidate.result['envelope']), ENVELOPE_KEYS)
        self.assertEqual(candidate.result, answer | {'body_base64': ''})
        self.assertEqual(candidate.body_bytes, len(base64.b64decode(answer['body_base64'])))
        self.assertEqual(answer['envelope']['cc'], ['closed.planner@project', 'operator'])
        return answer

    def test_checked_values_equal_answered_request(self):
        pair = self.review_pair()
        answer = self.compare_answered(pair['request'], 'implementer', pair['implementer'])
        self.assertEqual(answer['envelope']['to'], ['platform.implementer@project'])
        self.assertEqual(answer['unopened'], [
            {key: pair[which][key] for key in ('relay_id', 'handle')}
            for which in ('oldest', 'ancestor')])
        self.assertEqual([ref['spelling'] for ref in answer['refs']], list(pair['refs']))
        self.assertEqual(answer['findings'], [])

    def test_checked_values_equal_answered_reply(self):
        pair = self.review_pair()
        answer = self.compare_answered(self.reply(pair), 'planner', pair['planner'])
        self.assertEqual(answer['envelope']['to'], ['platform.planner@project'])
        self.assertEqual(answer['unopened'], [])
        self.assertEqual([ref['spelling'] for ref in answer['refs']], list(pair['refs']))
        self.assertEqual([item['summary'] for item in answer['findings']], ['a', 'b'])

    def test_read_metadata(self):
        registry = api_platform.build_platform().registry
        op = registry.operation('relay.read')
        self.assertIsNotNone(op, 'missing relay.read')
        self.assertEqual((op.principal, op.effect, op.retry, op.browser_get),
                         ('seat_session', 'relay_open', 'read_repeat', 'none'))
        self.assertEqual((op.request_ref.name, op.result_ref.name, op.error_ref.name),
                         ('RelayReadArgs', 'RelayReadResult', 'ApiError'))

    def test_read_empty_body(self):
        self.start()
        reader, relay = self.simple(body='')
        self.assertEqual(self.success(self.read(reader, relay['relay_id']))['body_base64'], '')

    def test_fixture_ledgers_hold_one_project(self):
        builders = (f.live_request_store, f.live_reply_store, f.import_bound_store,
                    f.import_over_body_store, f.frame_page_store)
        for builder in builders:
            with self.subTest(builder=builder.__name__):
                root = self.base / builder.__name__
                builder(str(root))
                path = f.scratch_copy_path(root)
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
                    self.assertEqual(conn.execute('SELECT id FROM projects').fetchall(), [(PROJECT,)])
                    seats = {row[0] for row in conn.execute(
                        'SELECT s.id FROM seats s JOIN team_instances t ON t.id=s.team_instance_id'
                        ' WHERE t.project_id=?', (PROJECT,))}
                    parties = conn.execute('SELECT sender_seat_id, sender_special FROM relays'
                                           ' UNION SELECT seat_id, special FROM recipients')
                    self.assertTrue(all((special == 'operator' and seat is None) or
                                        (special is None and seat in seats)
                                        for seat, special in parties))

    def test_read_result_fields_known(self):
        self.assertEqual([field.name for field in fields(queries.ReadResult)],
                         ['body', 'envelope', 'meta', 'unopened', 'refs', 'findings', 'repository'])

    def test_read_projection_drops_non_null_repository(self):
        from dataclasses import replace
        from relay_engine import api_relay_reads_service as service
        self.start()
        client, relay = self.simple()
        repository = 'private-repository-for-projection-test'
        job = service.RelayReadJob

        def open_with_repository(*args):
            return replace(queries.read(*args), repository=repository)

        opener = Mock(side_effect=open_with_repository)

        def inject(*args):
            return job(*args, opener=opener)

        with patch.object(service, 'RelayReadJob', side_effect=inject):
            result = self.success(self.read(client, relay['relay_id']))
        opener.assert_called_once()
        self.assertEqual(set(result), {'relay_id', 'envelope', 'body_base64',
                                       'unopened', 'refs', 'findings'})
        self.assertEqual(set(result['envelope']), set(ENVELOPE_KEYS))
        self.assertNotIn(repository, result.values())
        self.assertNotIn(repository, result['envelope'].values())
