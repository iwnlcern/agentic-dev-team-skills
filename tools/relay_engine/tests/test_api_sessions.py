"""Real-ledger sessions, occupant receipts and writer ordering through dispatch_api."""
import base64
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import replace
import hashlib
import itertools
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from relay_engine import api_keys as keys, api_platform as platform, api_sessions as sessions
from relay_engine import api_protocol as p, api_schema, api_wire as w, api_writer as writer
from relay_engine import jcs, store_layout

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
TOOLS = Path(__file__).resolve().parents[2]


class Job:
    def __init__(self, run):
        self.run = run


def replace_on_connection(conn, seat_id, at_ms):
    conn.execute('UPDATE seat_occupants SET ended_at=? WHERE seat_id=? AND ended_at IS NULL',
                 (at_ms, seat_id)).close()
    conn.execute("UPDATE seats SET state='vacant' WHERE id=?", (seat_id,)).close()
    return keys.issue_occupant_key(conn, seat_id, at_ms)


def replace_occupant_for_test(root, project_id, seat_id, at_ms):
    conn = store_layout.open_ledger(root, project_id)
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE').close()
        value = replace_on_connection(conn, seat_id, at_ms)
        conn.commit()
        return value
    finally:
        conn.close()


class SessionValueTests(unittest.TestCase):
    @staticmethod
    def instances():
        binding = p.SeatBinding('project', 'team', 'seat', 'occupant', 'key', 'session', 1)
        return (binding, p.SessionRefusal('key_rejected', 'auth'),
                p.SessionOutcome({}, 'keep', None),
                sessions._SessionJob(None, 'session.resume', {}, binding))

    def test_session_types_are_frozen(self):
        # Principals erratum 1 E3 names the exact supported-interpreter errors.
        non_field_error = (dataclasses.FrozenInstanceError
                           if sys.version_info >= (3, 14) else TypeError)
        for instance in self.instances():
            fields = dataclasses.fields(instance)
            field = fields[0].name
            original = tuple(getattr(instance, item.name) for item in fields)
            attempts = ((lambda: setattr(instance, field, object()), dataclasses.FrozenInstanceError),
                        (lambda: setattr(instance, 'extra', object()), non_field_error),
                        (lambda: delattr(instance, field), dataclasses.FrozenInstanceError),
                        (lambda: delattr(instance, 'extra'), non_field_error))
            with self.subTest(value_type=type(instance).__name__):
                for index, (attempt, error) in enumerate(attempts):
                    with self.subTest(attempt=index):
                        with self.assertRaises(error) as caught:
                            attempt()
                        self.assertIs(type(caught.exception), error)
                self.assertEqual(tuple(getattr(instance, item.name) for item in fields), original)
                self.assertFalse(hasattr(instance, 'extra'))
                # A test-local mutable slotted copy accepts the same assignment.
                mutable = dataclasses.make_dataclass('MutableCopy',
                    [(item.name, item.type) for item in fields], frozen=False, slots=True)
                control = mutable(*original)
                changed = object()
                setattr(control, field, changed)
                self.assertIs(getattr(control, field), changed)

    def test_session_types_keep_the_api_shape(self):
        for instance in self.instances():
            with self.subTest(value_type=type(instance).__name__):
                self.assertIn('__slots__', type(instance).__dict__)
                self.assertFalse(hasattr(instance, '__dict__'))
                fields = dataclasses.fields(instance)
                # A frozen copy without slots fails both shape conditions.
                unslotted = dataclasses.make_dataclass('UnslottedCopy',
                    [(item.name, item.type) for item in fields], frozen=True)
                control = unslotted(*(getattr(instance, item.name) for item in fields))
                self.assertNotIn('__slots__', unslotted.__dict__)
                self.assertTrue(hasattr(control, '__dict__'))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        conn = store_layout.open_ledger(self.root, P, create=True)
        try:
            conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (P, 'test')).close()
            conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
                         " VALUES ('team',?,'test','pair',1,'operator')", (P,)).close()
            conn.commit()
        finally:
            conn.close()
        self.n = itertools.count(2000)
        self.seat_n = itertools.count()
        self.phase = None
        self.step = None
        self.worker_conn = None
        self.returned = []
        self.raised = []
        self.writer = writer.StoreWriter(self.root, P,
            phase_hook=lambda phase: self.phase(phase) if self.phase else None)
        self.writer.start()
        self.addCleanup(self.writer.close)
        self.writer.open_project(P)
        owner = self
        class Probe:
            def submit(self, job):
                def run(conn):
                    owner.worker_conn = conn
                    return job.run(conn)
                try:
                    answer = owner.writer.submit(Job(run))
                except BaseException as exc:
                    owner.raised.append(exc)
                    raise
                owner.returned.append(answer)
                return answer
        self.service = sessions.SessionService(Probe(), lambda: next(self.n),
            step_hook=lambda phase: self.step(phase) if self.step else None)
        self.registry = platform.build_platform_registry()
        artifact = api_schema.export_schema(self.registry, '3.0')
        self.runtime = p.RuntimeContext('store', 'isolated', 'runtime', '3.0.0', 'a' * 64,
                                       True, {'3.0': artifact})
        self.key = self.add_seat()

    def connection(self):
        return p.dispatch_api(p.initial_connection_state(),
            w.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
            self.runtime, self.registry, self.service).state

    def add_seat(self):
        seat = 'seat' + str(next(self.seat_n))
        conn = store_layout.open_ledger(self.root, P)
        try:
            conn.execute("INSERT INTO seats(id,team_instance_id,role,state) VALUES (?,'team',?,'vacant')",
                         (seat, seat)).close()
            key_id, secret = keys.issue_occupant_key(conn, seat, next(self.n))
            conn.commit()
        finally:
            conn.close()
        return {'seat': seat, 'key_id': key_id, 'key_secret': secret}

    def args(self, key=None, operation_key='operation', **changes):
        key = self.key if key is None else key
        return dict(key_id=key['key_id'], key_secret=key['key_secret'], host='codex',
                    host_session_id='host-session', operation_key=operation_key, **changes)

    def call(self, op='session.sign-in', args=None, state=None):
        state = self.connection() if state is None else state
        return p.dispatch_api(state, w.ApiRequest(str(state.last_request_id + 1), op,
            self.args() if args is None else args), self.runtime, self.registry, self.service)

    def success(self, result):
        self.assertIn('result', result.response, result.response)
        self.assertFalse(result.close)
        return result.response['result']

    def refusal(self, result, kind):
        self.assertEqual(result.response['error']['cause']['kind'], kind)
        self.assertFalse(result.close)
        return result

    def rows(self, sql, values=()):
        conn = store_layout.open_ledger(self.root, P)
        try:
            return conn.execute(sql, values).fetchall()
        finally:
            conn.close()

    def counts(self):
        return tuple(self.rows('SELECT count(*) FROM ' + name)[0][0]
                     for name in ('seat_sessions', 'mutation_receipts'))

    def resume_args(self, signed, key=None):
        key = self.key if key is None else key
        return {k: key[k] for k in ('key_id', 'key_secret')} | {'session_id': signed['session_id']}

    def end_args(self, signed, key=None, operation_key='end'):
        return self.resume_args(signed, key) | {'operation_key': operation_key}

    def test_session_operations_metadata(self):
        expected = {'session.sign-in': ('seat_key', 'session_write', 'operation_key', 'none'),
                    'session.resume': ('seat_key', 'connection_binding', 'read_repeat', 'none'),
                    'session.end': ('seat_key', 'session_write', 'operation_key', 'none')}
        for name, row in expected.items():
            op = self.registry.operation(name)
            self.assertEqual((op.principal, op.effect, op.retry, op.browser_get), row)

    def test_capabilities_are_exactly_the_handled_operations(self):
        names = ('protocol.connect', 'protocol.schema', 'session.end', 'session.resume', 'session.sign-in')
        self.assertEqual(self.registry.capabilities(), names)
        self.assertEqual(set(platform.platform_handlers()), set(names))
        with patch.object(p, 'session_handlers', return_value={'protocol.connect': lambda: None}):
            with self.assertRaises(ValueError):
                platform.platform_handlers()

    def test_dispatch_and_generation_share_handlers(self):
        for name, handler in p.session_handlers().items():
            self.assertIs(self.registry.handlers[name], handler)
        with patch.object(self.service, 'run', wraps=self.service.run) as run:
            signed = self.call()
            self.success(signed)
            run.assert_called_once_with('session.sign-in', self.args(), None)

    def test_generation_import_boundary_fresh_process(self):
        probe = '''import sys,json
sys.path.insert(0,sys.argv[1])
from relay_engine.api_platform import build_platform_registry
from relay_engine.api_schema import export_schema
assert export_schema(build_platform_registry(),'3.0').canonical_bytes
EXTRA
print(json.dumps(sorted(sys.modules)))
'''
        forbidden = {'sqlite3', '_sqlite3', 'socket'} | {'relay_engine.' + x for x in
            ('api_sessions', 'api_writer', 'api_keys', 'submit', 'schema', 'queries', 'daemon', 'cli', 'client')}
        def inspect(extra):
            result = subprocess.run([sys.executable, '-I', '-c', probe.replace('EXTRA', extra),
                                     str(TOOLS)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return forbidden & set(json.loads(result.stdout))
        self.assertEqual(inspect(''), set())
        self.assertIn('sqlite3', inspect('import sqlite3'))

    def test_key_secret_shape_fails_validation(self):
        for value in ('short', 'A' * 42, 'A' * 44, '+' * 43, 'é' * 43):
            with patch.object(self.service, 'run', wraps=self.service.run) as run:
                args = self.args(); args['key_secret'] = value
                self.refusal(self.call(args=args), 'invalid_args'); run.assert_not_called()
        self.success(self.call())

    def test_key_id_must_be_canonical_lowercase_uuid(self):
        for value in (self.key['key_id'].upper(), self.key['key_id'].replace('-', ''), 'unknown'):
            with patch.object(self.service, 'run', wraps=self.service.run) as run:
                args = self.args(); args['key_id'] = value
                self.refusal(self.call(args=args), 'invalid_args'); run.assert_not_called()
        self.success(self.call())

    def test_host_outside_four_values_refuses(self):
        for host in ('Claude-Code', 'browser'):
            args = self.args(); args['host'] = host
            self.refusal(self.call(args=args), 'invalid_args')
        for host in ('claude-code', 'codex', 'frank', 'other'):
            args = self.args(operation_key=host); args['host'] = host
            self.success(self.call(args=args))

    def test_key_check_outcomes(self):
        signed = self.call(); self.success(signed)
        wrong = self.args(); wrong['key_secret'] = 'A' * 43
        unknown = self.args(); unknown['key_id'] = '00000000-0000-4000-8000-000000000000'
        a = self.refusal(self.call(args=wrong), 'key_rejected')
        b = self.refusal(self.call(args=unknown), 'key_rejected')
        self.assertEqual(jcs.jcs_encode(a.response), jcs.jcs_encode(b.response))
        self.assertEqual(self.success(self.call())['receipt'], 'replayed')
        replace_occupant_for_test(self.root, P, self.key['seat'], next(self.n))
        self.refusal(self.call(), 'key_replaced')
        new = self.add_seat()
        self.writer.submit(Job(lambda c: (c.execute("UPDATE seats SET state='stood-down' WHERE id=?",
            (new['seat'],)).close(), writer.JobOutcome(True, None))[1]))
        self.refusal(self.call(args=self.args(new)), 'seat_stood_down')

    def test_secret_absent_everywhere(self):
        with patch('relay_engine.strings.report_diagnostic') as diagnostic:
            result = self.call(); signed = self.success(result)
            end = self.call('session.end', self.end_args(signed), result.state); self.success(end)
            bad = self.args(); bad['key_secret'] = 'A' * 43
            denied = self.call(args=bad)
        data = [w.encode_api_response(x.response, self.registry,
                'session.end' if x is end else 'session.sign-in', None) for x in (result, end, denied)]
        data += [repr(row).encode() for row in self.rows('SELECT * FROM mutation_receipts')]
        data += [repr(x).encode() for x in diagnostic.call_args_list + self.raised]
        secret = self.key['key_secret']
        for value in data:
            self.assertNotIn(secret.encode(), value)
            self.assertNotIn(base64.urlsafe_b64decode(secret + '='), value)
        args = self.args(); expected = dict(args); expected.pop('key_secret')
        self.assertEqual(sessions.request_digest('session.sign-in', args),
                         hashlib.sha256(jcs.jcs_encode(expected)).hexdigest())
        args['key_secret'] = 'A' * 43
        self.assertEqual(sessions.request_digest('session.sign-in', args),
                         sessions.request_digest('session.sign-in', self.args()))

    def test_generations_and_overlapping_sign_ins(self):
        first = self.call(); self.assertEqual(self.success(first)['generation'], '1')
        for generation in (2, 3):
            value = self.success(self.call(args=self.args(operation_key=str(generation))))
            self.assertEqual(value['generation'], str(generation))
        self.refusal(self.call('session.end', self.end_args(first.response['result']), first.state),
                     'session_not_current')
        barrier = threading.Barrier(2)
        def sign(i):
            barrier.wait(timeout=5)
            return self.call(args=self.args(operation_key=str(i)))
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(sign, (4, 5)))
        self.assertEqual(sorted(int(self.success(v)['generation']) for v in values), [4, 5])
        self.assertEqual(self.rows('SELECT count(*) FROM seat_sessions WHERE ended_at IS NULL'), [(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM seat_sessions WHERE ended_at IS NOT NULL'), [(4,)])

    def test_resume_binds_same_session_without_write(self):
        result = self.call(); signed = self.success(result); before = self.counts()
        resumed = self.call('session.resume', self.resume_args(signed)); value = self.success(resumed)
        self.assertEqual(value['session_id'], signed['session_id'])
        self.assertEqual(value['generation'], signed['generation'])
        self.assertEqual(resumed.state.binding, result.state.binding)
        self.assertEqual(self.counts(), before)
        other = self.add_seat(); other_signed = self.success(self.call(args=self.args(other)))
        self.refusal(self.call('session.resume', self.resume_args(other_signed)), 'session_not_current')
        self.success(self.call('session.resume', self.resume_args(other_signed, other)))
        self.success(self.call('session.end', self.end_args(signed), result.state))
        self.refusal(self.call('session.resume', self.resume_args(signed)), 'session_not_current')

    def test_sign_in_retry_replays_and_changed_args_conflict(self):
        first = self.success(self.call()); replay = self.success(self.call())
        self.assertEqual(dict(replay, receipt='original'), first)
        self.assertEqual(self.counts(), (1, 1))
        args = self.args(); args['host_session_id'] = 'different'
        self.refusal(self.call(args=args), 'operation_key_reused')
        self.assertEqual(self.counts(), (1, 1))

    def test_replayed_sign_in_after_newer_is_ended_and_unbound(self):
        first = self.success(self.call())
        self.success(self.call(args=self.args(operation_key='new')))
        replay = self.call(); value = self.success(replay)
        self.assertEqual(value['session_id'], first['session_id'])
        self.assertEqual((value['receipt'], value['session_state']), ('replayed', 'ended'))
        self.assertIsNone(replay.state.binding)

    def test_bound_sign_in_follows_receipt_first_order(self):
        for case in ('same', 'changed', 'wrong', 'new'):
            key = self.add_seat(); args = self.args(key); original = self.call(args=args)
            signed = self.success(original); changed = dict(args)
            if case == 'changed': changed['host_session_id'] = 'changed'
            if case == 'wrong': changed['key_secret'] = 'A' * 43
            if case == 'new': changed['operation_key'] = 'new'
            answer = self.call(args=changed, state=original.state)
            if case == 'same': self.assertEqual(self.success(answer)['receipt'], 'replayed')
            else: self.refusal(answer, {'changed': 'operation_key_reused', 'wrong': 'key_rejected',
                                       'new': 'already_bound'}[case])
            self.assertEqual(answer.state.binding, original.state.binding)
            self.success(self.call('session.end', self.end_args(signed, key), answer.state))

    def test_writer_ordering_queued_and_outside(self):
        for before in (True, False):
            key = self.add_seat(); entered, release, queued = (threading.Event() for _ in range(3))
            seen = []
            def phase(name):
                if name == 'dequeued' and not seen:
                    seen.append(True); entered.set(); self.assertTrue(release.wait(5))
                if name == 'queued': queued.set()
            self.phase = phase
            def replacement():
                return self.writer.submit(Job(lambda c: writer.JobOutcome(True,
                    replace_on_connection(c, key['seat'], next(self.n))))).value
            with ThreadPoolExecutor(2) as pool:
                try:
                    one = pool.submit(replacement if before else lambda: self.call(args=self.args(key)))
                    self.assertTrue(entered.wait(5)); queued.clear()
                    two = pool.submit((lambda: self.call(args=self.args(key))) if before else replacement)
                    self.assertTrue(queued.wait(5)); release.set()
                    a, b = one.result(5), two.result(5)
                finally:
                    release.set(); self.phase = None
            if before: self.refusal(b, 'key_replaced'); new_key = a
            else: self.success(a); new_key = b
            self.success(self.call(args=self.args(dict(key, key_id=new_key[0], key_secret=new_key[1]))))
        for operation in ('session.sign-in', 'session.resume', 'session.end'):
            key = self.add_seat(); original = self.call(args=self.args(key)); signed = self.success(original)
            entered, release = threading.Event(), threading.Event()
            def phase(name):
                if name == 'dequeued': entered.set(); self.assertTrue(release.wait(5))
            self.phase = phase
            args = self.args(key, 'new') if operation == 'session.sign-in' else (
                self.resume_args(signed, key) if operation == 'session.resume' else self.end_args(signed, key))
            with ThreadPoolExecutor(1) as pool:
                try:
                    future = pool.submit(self.call, operation, args,
                                         original.state if operation == 'session.end' else None)
                    self.assertTrue(entered.wait(5))
                    new_id, new_secret = replace_occupant_for_test(self.root, P, key['seat'], next(self.n))
                    release.set(); denied = future.result(5)
                finally:
                    release.set(); self.phase = None
            self.refusal(denied, 'key_replaced')
            self.assertEqual(self.rows('SELECT count(*) FROM seat_sessions WHERE seat_id=?',
                                      (key['seat'],)), [(1,)])
            fresh = dict(key, key_id=new_id, key_secret=new_secret)
            control = self.call(args=self.args(fresh)); current = self.success(control)
            self.success(self.call('session.resume', self.resume_args(current, fresh)))
            self.success(self.call('session.end', self.end_args(current, fresh), control.state))

    def test_current_binding_contract(self):
        result = self.call(); signed = self.success(result)
        conn = store_layout.open_ledger(self.root, P)
        try:
            self.assertEqual(sessions.current_binding(conn, signed['session_id']), result.state.binding)
            def reason(expected, session_id=signed['session_id']):
                with self.assertRaises(sessions.BindingNotCurrent) as caught:
                    sessions.current_binding(conn, session_id)
                self.assertEqual(caught.exception.reason, expected)
            reason('session_unknown', 'missing')
            conn.execute('UPDATE seat_occupants SET ended_at=9999'); reason('key_replaced'); conn.rollback()
            conn.execute("UPDATE seats SET state='stood-down'"); reason('seat_stood_down'); conn.rollback()
            conn.execute('UPDATE seat_sessions SET ended_at=9999'); reason('session_not_current'); conn.rollback()
            conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                                else sqlite3.SQLITE_OK)
            with self.assertRaises(sessions.BindingUnavailable):
                sessions.current_binding(conn, signed['session_id'])
        finally:
            conn.close()

    def test_unsigned_new_end_with_valid_key_is_not_signed_in(self):
        signed = self.success(self.call()); before = self.counts()
        self.refusal(self.call('session.end', self.end_args(signed)), 'not_signed_in')
        self.assertEqual(self.counts(), before)

    def test_fault_between_step_and_receipt_commits_neither(self):
        original = self.call(); signed = self.success(original); before = self.counts()
        def hook(phase):
            if phase == 'stepped': raise RuntimeError('private failure')
        self.step = hook
        self.refusal(self.call(args=self.args(operation_key='new')), 'handler_fault')
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows('SELECT ended_at FROM seat_sessions WHERE id=?',
                                   (signed['session_id'],)), [(None,)])
        self.step = None
        self.success(self.call(args=self.args(operation_key='new')))

    def test_base_exception_rolls_back(self):
        original = self.call(); before = self.counts()
        marker = KeyboardInterrupt('test interruption')
        def hook(phase):
            if phase == 'stepped': raise marker
        self.step = hook
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.call(args=self.args(operation_key='interrupted'))
        self.assertIs(caught.exception, marker); self.assertIs(self.raised[-1], marker)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows('SELECT ended_at FROM seat_sessions WHERE id=?',
                        (original.response['result']['session_id'],)), [(None,)])
        self.step = None
        self.success(self.call(args=self.args(operation_key='interrupted')))

    def test_interrupt_ended_transaction_answers_indeterminate(self):
        trace, armed = [], [False]
        def hook(phase):
            if phase == 'checked':
                def traced(sql):
                    trace.append(sql)
                    if sql.startswith('UPDATE seat_sessions'): armed[0] = True
                def progress():
                    if armed[0]: armed[0] = False; return 1
                    return 0
                self.worker_conn.set_trace_callback(traced)
                self.worker_conn.set_progress_handler(progress, 1)
        self.step = hook
        result = self.call(); self.refusal(result, 'handler_fault')
        outcome = self.returned[-1]
        self.assertFalse(outcome.commit); self.assertTrue(outcome.indeterminate)
        self.assertEqual(outcome.fault, 'job_ended_transaction')
        self.assertIsInstance(outcome.value, sqlite3.OperationalError)
        self.assertEqual(str(outcome.value), 'interrupted')
        self.assertTrue(trace[-1].startswith('UPDATE seat_sessions'), trace)
        self.assertFalse(any(sql.startswith(('ROLLBACK', 'ROLLBACK TO')) for sql in trace))
        def clear(conn):
            conn.set_progress_handler(None, 0); conn.set_trace_callback(None)
            return writer.JobOutcome(False, None)
        self.writer.submit(Job(clear)); self.step = None
        self.assertEqual(self.counts(), (0, 0))
        self.success(self.call())

    def test_session_jobs_create_no_platform_savepoint(self):
        trace = []
        self.writer.submit(Job(lambda c: (c.set_trace_callback(trace.append), writer.JobOutcome(False, None))[1]))
        result = self.call(); signed = self.success(result)
        self.success(self.call('session.resume', self.resume_args(signed)))
        self.success(self.call('session.end', self.end_args(signed), result.state))
        self.writer.submit(Job(lambda c: (c.set_trace_callback(None), writer.JobOutcome(False, None))[1]))
        starts = [sql.split()[1] for sql in trace if sql.startswith('SAVEPOINT ')]
        self.assertEqual(len(starts), 1)
        for sql in trace:
            if sql.startswith(('RELEASE ', 'ROLLBACK TO ')): self.assertEqual(sql.split()[-1], starts[0])
        self.assertNotIn('SAVEPOINT', Path(sessions.__file__).read_text())
        self.assertNotEqual(len(starts + ['platform_probe']), 1)

    def test_end_retry_after_lost_response(self):
        result = self.call(); signed = self.success(result); args = self.end_args(signed)
        ended = self.call('session.end', args, result.state); value = self.success(ended)
        for state in (ended.state, self.connection()):
            replay = self.call('session.end', args, state)
            self.assertEqual(self.success(replay), dict(value, receipt='replayed'))
            self.assertIsNone(replay.state.binding)
        self.refusal(self.call('session.end', dict(args, session_id='changed')), 'operation_key_reused')
        key_id, secret = replace_occupant_for_test(self.root, P, self.key['seat'], next(self.n))
        self.refusal(self.call('session.end', args), 'key_replaced')
        fresh = dict(self.key, key_id=key_id, key_secret=secret)
        self.refusal(self.call('session.end', self.end_args(signed, fresh)), 'not_signed_in')
        new = self.call(args=self.args(fresh)); current = self.success(new)
        self.success(self.call('session.end', self.end_args(current, fresh, 'new'), new.state))

    def test_operation_key_scope_includes_operation(self):
        result = self.call(); signed = self.success(result)
        self.success(self.call('session.end', self.end_args(signed, operation_key='operation'), result.state))
        self.assertEqual(self.rows('SELECT operation FROM mutation_receipts ORDER BY operation'),
                         [('session.end',), ('session.sign-in',)])

    def test_end_after_newer_sign_in_is_not_current(self):
        original = self.call(); signed = self.success(original)
        newer = self.call(args=self.args(operation_key='new')); new = self.success(newer)
        self.refusal(self.call('session.end', self.end_args(signed), original.state), 'session_not_current')
        self.success(self.call('session.end', self.end_args(new), newer.state))

    def test_busy_store_is_unavailable_then_retry_commits_once(self):
        conn = store_layout.open_ledger(self.root, P)
        try:
            conn.execute('BEGIN IMMEDIATE').close()
            self.refusal(self.call(), 'runtime_not_ready')
        finally:
            conn.rollback(); conn.close()
        self.success(self.call()); self.success(self.call())
        self.assertEqual(self.counts(), (1, 1))
        for reason in ('busy', 'closed', 'rollback_failed', 'project_not_served', 'store_held',
                       'catalogue_faulted', 'project_fenced', 'catalogue_unopenable'):
            with patch.object(self.writer, 'submit', side_effect=writer.WriterUnavailable(reason)):
                self.refusal(self.call(), 'runtime_not_ready')

    def test_occupant_mismatch_rolls_back_as_handler_fault(self):
        other = self.add_seat(); existing = self.success(self.call(args=self.args(other)))
        before = self.counts(); original_step = sessions.record_session_step
        def mismatch(*args):
            original_step(*args)
            return existing['session_id']
        with patch.object(sessions, 'record_session_step', side_effect=mismatch):
            self.refusal(self.call(), 'handler_fault')
        self.assertEqual(self.counts(), before)
        self.success(self.call())


if __name__ == '__main__':
    unittest.main()
