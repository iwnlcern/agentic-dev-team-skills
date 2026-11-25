"""Service-owned admission contexts on real project ledgers."""
import dataclasses
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from relay_engine import api_keys, api_sessions, queries, slips, store_evidence, store_layout
from relay_engine import submit
from relay_engine import api_platform, api_protocol as p, api_schema, api_wire, api_writer
from relay_engine import api_registry, api_definitions as d, api_compose, errors, strings
from relay_engine.tests.writer_support import serve_for_test
from relay_engine.tests.test_submit import draft

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


class SubmitContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'store'
        self.conn = store_layout.open_ledger(self.root, P, create=True)
        self.addCleanup(self.conn.close)
        self.conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)',
                          (P, 'project')).close()
        self.conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
                          " VALUES ('team',?,'platform','pair',1,'operator')", (P,)).close()
        self.conn.execute("INSERT INTO seats(id,team_instance_id,role,state)"
                          " VALUES ('seat','team','implementer','vacant')").close()
        api_keys.issue_occupant_key(self.conn, 'seat', 1000)
        session_id = queries.record_session_step(self.conn, 'host-session', 'codex', 'seat', 2000)
        self.conn.commit()
        self.binding = api_sessions.current_binding(self.conn, session_id)
        self.sender = submit.SeatRef('seat', 'platform.implementer', 'implementer', 'team', P)

    def service(self, port=None, clock=lambda: 7890):
        from relay_engine.api_submit import ServiceContext
        return ServiceContext(port, clock, str(self.root), P)

    def test_submit_context_installs_authority_hook(self):
        service = self.service()
        def check(conn):
            api_sessions.require_current_binding(conn, self.binding)
        ctx = service.submit_context(self.conn, P, self.sender, 'submission', sender_check=check)
        self.assertIs(ctx.after_admit, slips.on_admitted)
        self.assertEqual(ctx.after_admit_writes, slips.AFTER_ADMIT_WRITES)
        self.assertEqual(ctx.after_admit_reads_denied, slips.AFTER_ADMIT_READS_DENIED)
        self.assertIs(ctx.sender_check, check)
        self.assertIsNone(ctx.after_rollback)
        self.assertIs(ctx.directory.conn, self.conn)
        self.assertEqual(ctx.directory.project_id, P)
        self.assertEqual(ctx.directory.resolve('platform.implementer', self.sender), self.sender)

    def test_serving_never_builds_store_none(self):
        service = self.service()
        def assert_evidence():
            ctx = service.submit_context(self.conn, P, self.sender, 'submission')
            self.assertIsInstance(ctx.store, store_evidence.LedgerEvidence)
            self.assertIs(ctx.store.conn, self.conn)
            self.assertEqual(ctx.store.root, self.root)
            self.assertEqual(ctx.store.project_id, P)
            self.assertEqual(ctx.store.capture,
                store_evidence.Capture('snapshot', 'host-observed', 'seat', None, None))
            return ctx
        for key in ('first', 'second'):
            with self.subTest(use=key):
                ctx = assert_evidence()
                digest = ctx.store.put(key.encode())
                self.assertEqual(ctx.store.get(digest), key.encode())
        self.assertEqual(self.conn.execute(
            'SELECT DISTINCT party_seat_id,source,captured_by FROM evidence_captures').fetchall(),
            [('seat', 'snapshot', 'host-observed')])
        operator = submit.SeatRef('operator', 'operator', 'operator', None, P)
        operator_ctx = service.submit_context(self.conn, P, operator, 'operator-submission')
        self.assertEqual(operator_ctx.store.capture,
            store_evidence.Capture('snapshot', 'host-observed', None, 'operator', None))
        operator_ctx.store.put(b'operator evidence')
        self.assertEqual(self.conn.execute(
            "SELECT party_seat_id,party_special FROM evidence_captures"
            " WHERE party_special='operator'").fetchall(), [(None, 'operator')])
        with patch('relay_engine.api_submit.LedgerEvidence', return_value=None):
            with self.assertRaises(AssertionError):
                assert_evidence()

    def test_submit_context_uses_service_clock_and_no_git_state(self):
        ticks = iter((7890, 7891))
        clock = lambda: next(ticks)
        service = self.service(clock=clock)
        ctx = service.submit_context(self.conn, P, self.sender, 'submission')
        self.assertIs(ctx.now_ms, clock)
        self.assertEqual((ctx.now_ms(), ctx.now_ms()), (7890, 7891))
        self.assertIsNone(ctx.git_state)
        self.assertEqual(ctx.sender, self.sender)
        self.assertEqual(ctx.submission_key, 'submission')

    def test_sender_ref_from_binding(self):
        from relay_engine.api_submit import sender_ref
        self.assertEqual(sender_ref(self.conn, self.binding),
            submit.SeatRef('seat', 'platform.implementer', 'implementer', 'team', P))
        self.conn.execute("UPDATE team_instances SET name='renamed' WHERE id='team'").close()
        self.assertEqual(sender_ref(self.conn, self.binding),
            submit.SeatRef('seat', 'renamed.implementer', 'implementer', 'team', P))

    def test_service_context_reads_the_port_each_use(self):
        class RecordingPort:
            def __init__(self):
                self.version = 1
                self.calls = []
            def store_id(self):
                self.calls.append(('store_id', self.version))
                return 'store-' + str(self.version)
            def ledger_file(self, project_id):
                self.calls.append(('ledger_file', project_id, self.version))
                return '/ledger-' + str(self.version)
        port = RecordingPort()
        service = self.service(port=port)
        self.assertEqual([field.name for field in dataclasses.fields(service)],
                         ['port', 'clock', 'store_root', 'project_id'])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            service.port = None
        observed = []
        for version in (1, 2):
            port.version = version
            service.submit_context(self.conn, P, self.sender, 'submission')
            observed.append((service.port.store_id(), service.port.ledger_file(P)))
        self.assertEqual(observed, [('store-1', '/ledger-1'), ('store-2', '/ledger-2')])
        self.assertEqual(port.calls, [('store_id', 1), ('ledger_file', P, 1),
                                      ('store_id', 2), ('ledger_file', P, 2)])


class SubmitServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        catalogue_conn = store_layout.open_catalogue(self.root, kind='isolated')
        catalogue_conn.close()
        self.tick = itertools.count(10000)
        self.keys = {}
        conn = store_layout.open_ledger(self.root, P, create=True)
        try:
            conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (P, 'project')).close()
            conn.execute('INSERT INTO repositories(id,project_id,name) VALUES (?,?,?)',
                ('platform-repository', P, 'platform-fixture')).close()
            for team, name in [('team', 'platform'), ('root-team', 'v3')]:
                conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
                    " VALUES (?,?,?,'pair',1,'operator')", (team, P, name)).close()
            for role in ('planner', 'implementer', 'reviewer', 'orchestrator-planner'):
                team = 'root-team' if role == 'orchestrator-planner' else 'team'
                conn.execute("INSERT INTO seats(id,team_instance_id,role,state) VALUES (?,?,?,'vacant')",
                    (role, team, role)).close()
                self.keys[role] = api_keys.issue_occupant_key(conn, role, next(self.tick))
            conn.commit()
            from relay_engine import claims
            claims.assign_reviewer(conn, 'team', 'reviewer', 'operator', next(self.tick))
        finally:
            conn.close()
        self.build = api_platform.build_platform()
        self.registry = self.build.registry
        self.phase = None
        self.writer = api_writer.StoreWriter(self.root, runtime_instance_id='test',
            phase_hook=lambda name: self.phase(name) if self.phase else None)
        self.writer.start()
        self.addCleanup(self.writer.close)
        serve_for_test(self.writer, P)
        from relay_engine.api_submit import ServiceContext
        self.context = ServiceContext(self.writer, lambda: next(self.tick), str(self.root), P)
        owner_ports = {owner: factory(self.context)
                       for owner, factory in self.build.composition.port_factories.items()}
        self.ports = {op: owner_ports[owner] for op, owner in self.build.composition.owners.items()}
        artifact = api_schema.export_schema(self.registry, '3.0')
        self.runtime = p.RuntimeContext('scratch-store', 'isolated', 'test', '3.0.0',
                                       'a' * 64, True, {'3.0': artifact})
        self.states = {}
        for role, (key_id, secret) in self.keys.items():
            connected = p.dispatch_api(p.initial_connection_state(), api_wire.ApiRequest('1',
                'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
                self.runtime, self.registry, self.ports)
            signed = p.dispatch_api(connected.state, api_wire.ApiRequest('2', 'session.sign-in',
                dict(key_id=key_id, key_secret=secret, host='codex', host_session_id=role,
                     operation_key=role)), self.runtime, self.registry, self.ports)
            self.assertIn('result', signed.response, signed.response)
            self.states[role] = signed.state

    def rows(self, sql, args=()):
        conn = store_layout.open_ledger(self.root, P)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def call(self, data=None, key='submission', role='planner', state=None):
        data = draft(to='platform.implementer') if data is None else data
        state = self.states[role] if state is None else state
        result = p.dispatch_api(state, api_wire.ApiRequest(str(state.last_request_id + 1),
            'relay.submit', {'draft_base64': base64.b64encode(data).decode(), 'submission_key': key}),
            self.runtime, self.registry, self.ports)
        self.states[role] = result.state
        return result

    def admitted(self, result):
        self.assertIn('result', result.response, result.response)
        value = result.response['result']
        row = self.rows('SELECT id,handle,created_at FROM relays WHERE id=?', (value['relay_id'],))[0]
        self.assertEqual(value, {'relay_id': row[0], 'handle': str(row[1]), 'created_at': str(row[2])})
        return value

    def test_submit_admits_once_and_runs_authority_hook(self):
        plan_file = Path(self.tmp.name) / 'plan.md'
        plan_file.write_bytes(b'one plan\n')
        ref = 'file:' + str(plan_file)
        plan_data = draft(kind='PLAN', to='platform.implementer', needs='review', refs=(ref,))
        plan = self.admitted(self.call(plan_data, 'plan'))
        review_data = draft(kind='PLAN-REVIEW', to='platform.planner', parent=plan['relay_id'],
                            verdict='approve', refs=(ref,), cc='v3.orchestrator-planner')
        review = self.admitted(self.call(review_data, 'review', 'implementer'))
        requests = self.rows('SELECT request_relay_id,reviewer_seat_id FROM verification_requests'
                             ' WHERE claim_relay_id=?', (review['relay_id'],))
        self.assertEqual(len(requests), 1)
        request_id, reviewer = requests[0]
        self.assertEqual(reviewer, 'reviewer')
        self.assertEqual(self.rows("SELECT r.kind,r.needs,r.sender_special,c.seat_id FROM relays r"
            " JOIN recipients c ON c.relay_id=r.id WHERE r.id=? AND c.role='to'", (request_id,)),
            [('REVIEW', 'review', 'system', 'reviewer')])
        self.assertEqual(self.rows("SELECT count(*) FROM relays WHERE sender_special='system'"), [(1,)])
        impl_data = draft(kind='IMPL', needs='act', to='platform.implementer', parent=review['relay_id'])
        # Claims are relay rows plus verification/waiver state, not a separate claims table.
        untouched_tables = ('relays', 'bodies', 'recipients', 'edges', 'snapshots',
            'verification_requests', 'reviewer_assignments', 'waivers', 'slips',
            'slip_events', 'bundles', 'findings', 'finding_events',
            'seat_activity', 'token_counts', 'evidence_objects', 'evidence_captures')
        before = {table: self.rows('SELECT * FROM ' + table + ' ORDER BY rowid')
                  for table in untouched_tables}
        refusal_sql = ('SELECT project_id,seat_id,special,code,at,draft_sha256'
                       ' FROM refusals ORDER BY id')
        prior_refusals = self.rows(refusal_sql)
        refused_at = next(self.tick) + 1
        refused = self.call(impl_data, 'impl-refused', 'orchestrator-planner')
        self.assertEqual(refused.response['error']['code'], 'E_API_ADMISSION')
        self.assertEqual(refused.response['error']['cause']['engine_code'], 'E-FIELD-REQUIRED')
        self.assertEqual(self.rows(refusal_sql), prior_refusals + [
            (P, 'orchestrator-planner', None, 'E-FIELD-REQUIRED', refused_at,
             hashlib.sha256(impl_data).hexdigest())])
        for table, rows in before.items():
            with self.subTest(refused_table=table):
                self.assertEqual(self.rows('SELECT * FROM ' + table + ' ORDER BY rowid'), rows)
        self.assertEqual(self.rows(
            'SELECT id FROM relays WHERE submission_key=?', ('impl-refused',)), [])
        self.assertEqual(self.rows('SELECT count(*) FROM slips'), [(0,)])
        conn = store_layout.open_ledger(self.root, P)
        try:
            conn.execute('BEGIN IMMEDIATE').close()
            answer = slips.act(conn, 'implementer', slips.Effect('commit', None), next(self.tick),
                               digest=lambda path: None)
            self.assertEqual((answer.outcome, answer.code, answer.reason),
                ('deny', 'authority.slip-missing', strings.render('authority-missing')))
            conn.rollback()
        finally:
            conn.close()
        impl_data = impl_data.replace(b'\n\n', b'\nREPO: platform-fixture\n\n', 1)
        impl = self.admitted(self.call(impl_data, 'impl', 'orchestrator-planner'))
        self.assertEqual(self.rows('SELECT repository_id FROM relays WHERE id=?',
                                  (impl['relay_id'],)), [('platform-repository',)])
        expected_slip = [('implement', 'implementer', P, 'platform-repository', impl['relay_id'])]
        self.assertEqual(self.rows(
            'SELECT kind,seat_id,project_id,repository_id,issued_in FROM slips'), expected_slip)
        issued_events = self.rows('SELECT * FROM slip_events ORDER BY rowid')
        self.assertEqual(len(issued_events), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM relays WHERE sender_special='system'"), [(1,)])
        self.assertEqual(self.rows('SELECT request_relay_id,reviewer_seat_id FROM verification_requests'
                                  ' WHERE claim_relay_id=?', (review['relay_id'],)), requests)
        self.assertEqual(self.admitted(self.call(review_data, 'review', 'implementer')), review)
        self.assertEqual(self.admitted(self.call(impl_data, 'impl', 'orchestrator-planner')), impl)
        self.assertEqual(self.rows(
            'SELECT kind,seat_id,project_id,repository_id,issued_in FROM slips'), expected_slip)
        self.assertEqual(self.rows('SELECT * FROM slip_events ORDER BY rowid'), issued_events)
        self.assertEqual(self.rows("SELECT count(*) FROM relays WHERE sender_special='system'"), [(1,)])
        self.assertEqual(self.rows('SELECT request_relay_id,reviewer_seat_id FROM verification_requests'
                                  ' WHERE claim_relay_id=?', (review['relay_id'],)), requests)
        # Independent ledgers make both controls prove the absence of a system REVIEW.
        for no_hook in (True, False):
            with self.subTest(control='no-hook' if no_hook else 'no-reviewer'):
                root = Path(self.tmp.name) / ('control-' + str(no_hook).lower())
                conn = store_layout.open_ledger(root, P, create=True)
                try:
                    conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (P, 'control')).close()
                    conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
                        " VALUES ('team',?,'platform','pair',1,'operator')", (P,)).close()
                    roles = ('planner', 'implementer', 'reviewer') if no_hook else ('planner', 'implementer')
                    for role in roles:
                        conn.execute("INSERT INTO seats(id,team_instance_id,role,state) VALUES (?,'team',?,'vacant')",
                                     (role, role)).close()
                    conn.commit()
                    if no_hook:
                        from relay_engine import claims
                        claims.assign_reviewer(conn, 'team', 'reviewer', 'operator', next(self.tick))
                    from relay_engine.api_submit import ServiceContext
                    service = ServiceContext(None, lambda: next(self.tick), str(root), P)
                    def context(role, key):
                        sender = submit.SeatRef(role, 'platform.' + role, role, 'team', P)
                        ctx = service.submit_context(conn, P, sender, key)
                        return dataclasses.replace(ctx, after_admit=None) if no_hook else ctx
                    control_plan = submit.submit(conn, context('planner', 'plan'), plan_data)
                    control_review = draft(kind='PLAN-REVIEW', to='platform.planner', parent=control_plan.relay_id,
                                           verdict='approve', refs=(ref,))
                    value = submit.submit(conn, context('implementer', 'review'), control_review)
                    rows = conn.execute('SELECT request_relay_id,reviewer_seat_id FROM verification_requests'
                                        ' WHERE claim_relay_id=?', (value.relay_id,)).fetchall()
                    self.assertEqual(rows, [] if no_hook else [(None, None)])
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM relays WHERE sender_special='system'").fetchone(), (0,))
                    if no_hook:
                        with self.assertRaises(AssertionError):
                            self.assertEqual(len(rows), 1)
                    else:
                        with self.assertRaises(AssertionError):
                            self.assertIsNotNone(rows[0][0])
                finally:
                    conn.close()

    def test_same_key_same_bytes_replays(self):
        first = self.admitted(self.call())
        self.assertEqual(self.admitted(self.call()), first)
        self.assertEqual(self.rows('SELECT count(*) FROM relays'), [(1,)])

    def test_same_key_other_bytes_conflict(self):
        self.admitted(self.call())
        error = self.call(draft(subject='different')).response['error']
        self.assertEqual((error['code'], error['cause']['kind']),
                         ('E_API_CONFLICT', 'submission_key_reused'))

    def test_replay_survives_removed_cited_file(self):
        file = Path(self.tmp.name) / 'cited.md'
        file.write_bytes(b'cited')
        data = draft(to='platform.implementer', refs=('file:' + str(file),))
        first = self.admitted(self.call(data))
        file.unlink()
        with patch.object(submit, '_resolve', side_effect=AssertionError('resolved replay')), \
             patch.object(slips, 'on_admitted', side_effect=AssertionError('hook replay')):
            self.assertEqual(self.admitted(self.call(data)), first)
        self.assertEqual(self.call(data + b'changed').response['error']['cause']['kind'], 'submission_key_reused')
        from relay_engine.tests.test_api_sessions import replace_occupant_for_test
        replace_occupant_for_test(self.root, P, 'planner', next(self.tick))
        self.assertEqual(self.call(data).response['error']['cause']['kind'], 'key_replaced')

    def test_replaced_occupant_gets_no_replay(self):
        self.admitted(self.call())
        entered, release = threading.Event(), threading.Event()
        def phase(name):
            if name == 'dequeued':
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test release timed out')
        self.phase = phase
        from relay_engine.tests.test_api_sessions import replace_occupant_for_test
        try:
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.call)
                try:
                    self.assertTrue(entered.wait(10))
                    replace_occupant_for_test(self.root, P, 'planner', next(self.tick))
                finally:
                    release.set()
                answer = future.result(10)
            self.assertEqual(answer.response['error']['cause']['kind'], 'key_replaced')
            self.assertIsNone(answer.state.binding)
            self.assertEqual(self.rows('SELECT count(*) FROM relays'), [(1,)])
        finally:
            release.set()
            self.phase = None
        self.admitted(self.call(key='current-control', role='implementer'))

    def test_unsigned_submit_never_calls_submit(self):
        state = dataclasses.replace(self.states['planner'], binding=None)
        with patch.object(submit, 'submit', side_effect=AssertionError('unsigned call')):
            self.assertEqual(self.call(state=state).response['error']['cause']['kind'], 'not_signed_in')

    def test_closed_team_stale_binding_keeps_its_refusal(self):
        from relay_engine.tests.test_api_sessions import replace_occupant_for_test
        for reason in ('key_replaced', 'seat_stood_down'):
            with self.subTest(reason=reason):
                case = type(self)()
                case.setUp()
                try:
                    data = draft(to='platform.implementer')
                    case.admitted(case.call(data))
                    if reason == 'key_replaced':
                        replace_occupant_for_test(case.root, P, 'planner', next(case.tick))
                    conn = store_layout.open_ledger(case.root, P)
                    try:
                        if reason == 'seat_stood_down':
                            conn.execute("UPDATE seats SET state='stood-down' WHERE id='planner'").close()
                        conn.execute('UPDATE team_instances SET closed_at=? WHERE id=?',
                                     (next(case.tick), 'team')).close()
                        conn.commit()
                    finally:
                        conn.close()
                    answer = case.call(data)
                    self.assertEqual(answer.response['error']['code'], 'E_API_AUTH')
                    self.assertEqual(answer.response['error']['cause']['kind'], reason)
                    self.assertIsNone(answer.state.binding)
                    self.assertEqual(case.rows('SELECT count(*) FROM relays'), [(1,)])
                    self.assertEqual(case.call(data).response['error']['cause']['kind'], 'not_signed_in')
                finally:
                    case.doCleanups()

    def test_hook_fault_is_authority_unavailable_then_admits_once(self):
        from relay_engine.claims import AuthorityFilingFault
        class ReservationHold(Exception):
            pass
        for index, fault in enumerate((AuthorityFilingFault, ReservationHold)):
            with self.subTest(case='authority-filing' if index == 0 else 'reservation-hold'):
                key = 'fault-' + str(index)
                before = self.rows('SELECT count(*) FROM relays')
                with patch.object(slips, 'on_admitted', side_effect=fault):
                    error = self.call(key=key).response['error']
                self.assertEqual((error['code'], error['cause']['kind'], error['remedy']),
                    ('E_API_UNAVAILABLE', 'authority_unavailable', 'retry_same_submission'))
                self.assertEqual(self.rows('SELECT count(*) FROM relays'), before)
                self.assertEqual(self.rows('SELECT count(*) FROM refusals'), [(index + 1,)])
                first = self.admitted(self.call(key=key))
                self.assertEqual(self.admitted(self.call(key=key)), first)

    def test_refusal_hides_draft_text(self):
        data = draft(to='absent.planner', body='distinctive-private-draft-token')
        error = self.call(data).response['error']
        self.assertEqual(error['code'], 'E_API_ADMISSION')
        self.assertEqual(error['cause']['engine_code'], 'E-ADDRESS')
        self.assertNotIn('distinctive-private-draft-token', json.dumps(error))

    @staticmethod
    def engine_error(code, spec):
        value = errors.EngineError.__new__(errors.EngineError)
        value.code, value.cause_key, value.remedy_key, value.cls = (
            code, spec.cause_key, spec.remedy_key, spec.cls)
        value.params = {}
        return value

    def mapped(self, error):
        from relay_engine.api_submit import map_refusal
        refusal = map_refusal(error)
        try:
            response = p.api_error(self.registry, refusal.cause_kind, **(refusal.payload or {}))
        except ValueError as invalid:
            if str(invalid) != 'invalid_api_error':
                raise
            self.fail('response for ' + error.code + ' is not a catalogue response')
        self.assertEqual(refusal.code, response['code'])
        self.assertNotIsInstance(api_registry.validate_type(self.registry, d.TypeRef('ApiError'), response),
                                 api_registry.ValidationFailure)
        return response

    def test_admission_codes_answer_unavailable(self):
        codes = ('E-ADMISSION-COMMIT', 'E-ADMISSION-UNKNOWN')
        present = [code in errors.ERRORS for code in codes]
        self.assertEqual(present[0], present[1])
        source = 'registered' if all(present) else 'placeholder'
        if all(present):
            self.assertTrue(any(code == codes[1] for code, variant in errors.ERROR_VARIANTS))
        with ExitStack() as stack:
            if not any(present):
                spec = dataclasses.replace(errors.ERRORS['E-ADMISSION-HOOK'])
                stack.enter_context(patch.dict(errors.ERRORS, dict.fromkeys(codes, spec)))
                stack.enter_context(patch.dict(errors.ERROR_VARIANTS,
                    {('E-ADMISSION-UNKNOWN', 'commit-unconfirmed'): spec}))
            for code, kind in zip(codes, ('admission_not_stored', 'admission_unknown')):
                specs = [(None, errors.ERRORS[code])] + [(v, spec) for (c, v), spec in
                                                       errors.ERROR_VARIANTS.items() if c == code]
                expected = {'code': 'E_API_UNAVAILABLE', 'class': 'unavailable',
                            'cause': {'kind': kind}, 'remedy': 'retry_same_submission'}
                for variant, spec in specs:
                    with self.subTest(code=code, variant=variant, source=source):
                        self.assertEqual(self.mapped(self.engine_error(code, spec)), expected)
                error = self.engine_error(code, errors.ERRORS[code])
                with patch.object(submit, 'submit', side_effect=error):
                    self.assertEqual(self.call(key=code).response['error'], expected)
                self.assertEqual(self.rows('SELECT count(*) FROM relays'), [(0,)])
                self.assertEqual(self.rows('SELECT count(*) FROM refusals'), [(0,)])
                self.assertFalse(self.writer.read(P, lambda conn: conn.in_transaction))

    def test_map_refusal_emits_only_errors_keys(self):
        legacy = {'no-top-seat', 'seat-occupied', 'commission-conflict', 'commission-late',
                  'run-id-mismatch', 'run-id-uninitialized', 'run-id-invalid'}
        self.assertTrue(legacy <= {code for code in errors.ERRORS if not re.fullmatch(r'E-[A-Z0-9-]{1,63}', code)})
        special = {'E-SUBMISSION-KEY-REUSED': 'E_API_CONFLICT', 'E-SENDER-NOT-CURRENT': 'E_API_AUTH',
                   'E-ADMISSION-HOOK': 'E_API_UNAVAILABLE', 'E-ADMISSION-COMMIT': 'E_API_UNAVAILABLE',
                   'E-ADMISSION-UNKNOWN': 'E_API_UNAVAILABLE'}
        for code, variant, spec in [(c, None, s) for c, s in errors.ERRORS.items()] + [
                (c, v, s) for (c, v), s in errors.ERROR_VARIANTS.items()]:
            with self.subTest(code=code, variant=variant):
                error = self.engine_error(code, spec)
                if code == 'E-SENDER-NOT-CURRENT':
                    error.params = {'reason': 'key_replaced'}
                value = self.mapped(error)
                if not re.fullmatch(r'E-[A-Z0-9-]{1,63}', code):
                    self.assertEqual((value['code'], value['cause']['kind']), ('E_API_INTERNAL', 'handler_fault'))
                else:
                    self.assertEqual(value['code'], special.get(code, 'E_API_ADMISSION'))
                    if value['code'] == 'E_API_ADMISSION':
                        self.assertEqual(value['cause'], {'kind': spec.cls, 'engine_code': code})
        self.assertEqual(self.mapped(self.engine_error('E-NOT-REGISTERED', errors.ERRORS['E-ADDRESS']))['cause'],
                         {'kind': 'handler_fault'})

    def test_snapshot_bytes_stored_in_admission_transaction(self):
        path = Path(self.tmp.name) / 'snapshot.md'
        path.write_bytes(b'plan contents')
        data = draft(kind='PLAN', to='platform.implementer', needs='review', refs=('file:' + str(path),))
        first = self.admitted(self.call(data))
        self.assertEqual(self.rows('SELECT content_sha256,has_bytes FROM snapshots WHERE relay_id=?',
                                   (first['relay_id'],)), [(hashlib.sha256(path.read_bytes()).hexdigest(), 1)])
        before = self.rows('SELECT count(*) FROM snapshots')
        real = slips.on_admitted
        def fault(conn, admitted, draft, ctx):
            real(conn, admitted, draft, ctx)
            raise RuntimeError('after hook')
        with patch.object(slips, 'on_admitted', side_effect=fault):
            result = self.call(data.replace(b'BUNDLE: b1', b'BUNDLE: fault'), 'fault')
        self.assertIn('error', result.response)
        self.assertEqual(self.rows('SELECT count(*) FROM snapshots'), before)
        self.assertEqual(self.rows('SELECT count(*) FROM relays'), [(1,)])

    def test_draft_bound_is_api_then_engine(self):
        prefix = draft(to='platform.implementer', body='')
        for size in (131073, 131072):
            data = prefix + b'a' * (size - len(prefix))
            with self.subTest(size=size), patch.object(self.writer, 'submit_callee',
                    wraps=self.writer.submit_callee) as queued:
                result = self.call(data, key=str(size))
                error = result.response['error']
                if size == 131073:
                    self.assertEqual(error['cause']['kind'], 'invalid_args')
                    queued.assert_not_called()
                else:
                    self.assertEqual(error['code'], 'E_API_ADMISSION')
                    self.assertEqual(error['cause']['engine_code'], 'E-BODY-SIZE')
                    queued.assert_called_once()
                self.assertEqual(self.rows('SELECT count(*) FROM relays'), [(0,)])

    def test_platform_is_one_component(self):
        self.assertCountEqual([component.owner for component in api_platform.platform_components()],
                              ['platform', 'console'])
        platform = ('protocol.connect', 'protocol.schema', 'relay.inbox', 'relay.submit', 'seat.identify',
                    'session.end', 'session.resume', 'session.sign-in')
        queue = ('queue.answer', 'queue.dismiss', 'queue.dismiss-all', 'queue.get',
                 'queue.list', 'queue.open', 'queue.raise', 'queue.withdraw')
        owners = dict.fromkeys(platform, 'platform') | dict.fromkeys(queue, 'console')
        self.assertEqual(self.build.composition.owners, owners)
        self.assertIs(self.ports['relay.submit'], self.ports['session.sign-in'])
        self.assertIs(self.ports['relay.inbox'], self.ports['relay.submit'])
        op = d.OperationDef('test.echo', d.TypeRef('EchoArgs'), d.TypeRef('EchoArgs'),
            d.TypeRef('ApiError'), 'unsigned', 'pure_observation', 'read_repeat', 'none')
        graph = d.DefinitionGraph((d.TypeDef('EchoArgs', d.Record(())),), (op,), d.TypeRef('ApiError'))
        seen = []
        other_port = object()
        def echo(args, binding, port):
            seen.append(port)
            return {}
        other = api_compose.ApiComponent('other', graph, {'test.echo': echo}, lambda ctx: other_port)
        for components in ((api_platform.platform_component(), other), (other, api_platform.platform_component())):
            composition = api_compose.compose_components(components)
            registry = api_registry.compile_registry(composition.definitions, composition.handlers,
                                                     (errors.api_error_catalog(),))
            owner_ports = {owner: factory(self.context) for owner, factory in composition.port_factories.items()}
            ports = {name: owner_ports[owner] for name, owner in composition.owners.items()}
            for name, owner in composition.owners.items():
                self.assertIs(ports[name], owner_ports[owner])
            runtime = dataclasses.replace(self.runtime, schemas={'3.0': api_schema.export_schema(registry, '3.0')})
            connected = p.dispatch_api(p.initial_connection_state(), api_wire.ApiRequest('1',
                'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}), runtime, registry, ports)
            result = p.dispatch_api(connected.state, api_wire.ApiRequest('2', 'test.echo', {}), runtime, registry, ports)
            self.assertEqual(result.response['result'], {})
            self.assertIs(seen[-1], other_port)
            self.assertIs(ports['relay.submit'], owner_ports['platform'])

    def test_dispatch_and_generation_share_handlers(self):
        for name, handler in dict(p.session_handlers(), **p.submit_handlers()).items():
            self.assertIs(self.registry.handlers[name], handler)
        with patch.object(self.ports['relay.submit'].submit, 'run', return_value={
                'relay_id': 'test', 'handle': '1', 'created_at': '2'}) as run:
            result = self.call()
        self.assertEqual(result.response['result'], {'relay_id': 'test', 'handle': '1', 'created_at': '2'})
        self.assertEqual(run.call_args.args[1], self.states['planner'].binding)

    def test_generation_import_boundary_fresh_process(self):
        tools = str(Path(__file__).resolve().parents[2])
        script = """import sys,json
sys.path.insert(0,sys.argv[1])
from relay_engine.api_platform import build_platform
from relay_engine.api_schema import export_schema
b=build_platform()
assert export_schema(b.registry,'3.0').canonical_bytes
print(json.dumps(sorted(sys.modules)))
"""
        result = subprocess.run([sys.executable, '-I', '-c', script, tools],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = set(json.loads(result.stdout))
        self.assertFalse(loaded & {'sqlite3', 'socket', 'relay_engine.submit', 'relay_engine.slips',
            'relay_engine.api_submit', 'relay_engine.api_sessions', 'relay_engine.api_writer',
            'relay_engine.api_service', 'relay_engine.store_layout', 'relay_engine.store_evidence'})
        control_script = script + """from types import SimpleNamespace
b.composition.port_factories['platform'](SimpleNamespace(port=None,clock=lambda:0,store_root='',project_id='P'))
print(json.dumps(sorted(sys.modules)))
"""
        control = subprocess.run([sys.executable, '-I', '-c', control_script, tools],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn('relay_engine.api_submit', json.loads(control.stdout.splitlines()[-1]))
