import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from relay_engine import (
    api_compose, api_definitions as d, api_platform, api_protocol as p,
    api_registry, api_schema, api_service, api_terminal as terminal,
    api_wire as w, errors, jcs, operator_terminal,
)


CHALLENGE = 'operator.terminal-challenge'
ACT = 'operator.terminal-act'
PURPOSE = 'queue.go_ahead_without_review'
REFUSALS = (
    ('E_TERMINAL_CHALLENGE', 'conflict', 'challenge_unknown', 'request_new_challenge'),
    ('E_TERMINAL_CHALLENGE', 'conflict', 'challenge_used', 'request_new_challenge'),
    ('E_TERMINAL_CHALLENGE', 'conflict', 'challenge_expired', 'request_new_challenge'),
    ('E_TERMINAL_LIMIT', 'unavailable', 'challenge_limit', 'retry_after_challenge_expiry'),
    ('E_TERMINAL_CHECK', 'auth', 'bad_signature', 'confirm_again'),
    ('E_TERMINAL_CHECK', 'auth', 'key_unsafe', 'reinstall_operator_check'),
    ('E_TERMINAL_INVALID', 'wire', 'args_not_canonical', 'send_canonical_args'),
    ('E_TERMINAL_INVALID', 'wire', 'purpose_unhandled', 'update_kit'),
)


def b64(value):
    return base64.b64encode(value).decode('ascii')


def arguments(challenge, *, raw=b'{}', purpose=PURPOSE, signature=b'checker'):
    return dict(challenge_id=challenge['challenge_id'], purpose=purpose,
                args_base64=b64(raw), signature_base64=b64(signature))


class TerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = api_platform.build_platform()
        cls.registry = cls.build.registry
        cls.artifact = api_schema.export_schema(cls.registry, '3.0')
        cls.runtime = p.RuntimeContext('store', 'isolated', 'runtime', '3.0.0', 'a' * 64,
                                       True, {'3.0': cls.artifact})

    def setUp(self):
        self.now = 0
        self.checked, self.received = [], []
        self.acts = terminal.TerminalActs()
        self.acts.register(PURPOSE, self.run_act, max_result_bytes=256)

        def read_catalogue(read):
            with closing(sqlite3.connect(':memory:')) as conn:
                conn.execute('CREATE TABLE catalogue_identity (id INTEGER, store_id TEXT)').close()
                conn.execute("INSERT INTO catalogue_identity VALUES (1, 'store')").close()
                return read(conn)

        context = SimpleNamespace(port=SimpleNamespace(read_catalogue=read_catalogue))
        self.port = terminal.TerminalService(context, self.acts,
                                             clock=lambda: self.now, verify=self.verify)

    def verify(self, message, signature):
        self.checked.append((message, signature))
        if signature in (b'bad_signature', b'key_unsafe'):
            raise operator_terminal.OperatorCheckRefused(signature.decode('ascii'))
        return SimpleNamespace(surface='terminal')

    def run_act(self, principal, args):
        self.received.append((principal.surface, args))
        return {'done': True}

    def connect(self):
        request = w.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}})
        answer = p.dispatch_discovery(p.initial_connection_state(), request, self.runtime, self.registry)
        self.assertEqual(answer.state.phase, 'connected')
        return answer.state

    def dispatch(self, state, op, args, number=None):
        number = str(state.last_request_id + 1) if number is None else str(number)
        return p.dispatch_api(state, w.ApiRequest(number, op, args), self.runtime, self.registry,
                              {CHALLENGE: self.port, ACT: self.port})

    def assert_cause(self, result, kind):
        self.assertIsInstance(result, p.ComponentRefusal)
        self.assertEqual(result.cause_kind, kind)

    def test_operations_compile(self):
        self.assertEqual({self.build.composition.owners[name] for name in (CHALLENGE, ACT)}, {'terminal'})
        component = terminal.terminal_component(self.acts)
        graph = component.definitions
        unknown = replace(graph.operations[0], effect='future_effect')
        broken = replace(component, definitions=replace(graph, operations=(unknown, *graph.operations[1:])))
        components = (*api_platform.platform_components()[:-1], broken)
        composition = api_compose.compose_components(components)
        with self.assertRaisesRegex(api_registry.RegistryCompileError, 'invalid_definition'):
            api_registry.compile_registry(composition.definitions, composition.handlers,
                                          (errors.api_error_catalog(),))
        duplicate = replace(component, definitions=replace(
            graph, types=(*graph.types, d.TypeDef('OpaqueId', d.String('.*')))))
        with self.assertRaisesRegex(api_compose.CompositionError, 'duplicate_type'):
            api_compose.compose_components((*components[:-1], duplicate))

    def test_challenge_single_use(self):
        challenge = self.port.challenge()
        release = threading.Barrier(2)

        def attempt():
            release.wait(timeout=5)
            return self.port.act(arguments(challenge))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(attempt), pool.submit(attempt)
            results = (first.result(5), second.result(5))
        self.assertEqual(sum(isinstance(outcome, dict) for outcome in results), 1)
        refused = next(outcome for outcome in results if not isinstance(outcome, dict))
        self.assert_cause(refused, 'challenge_used')
        self.assertEqual(self.received, [('terminal', {})])

    def test_challenge_limit_per_service(self):
        states = [self.connect() for _ in range(3)]
        challenges = []
        for connection in (0, 1):
            for _ in range(8):
                answer = self.dispatch(states[connection], CHALLENGE, {})
                states[connection] = answer.state
                challenges.append(answer.response['result'])
        self.assertEqual(len({row['challenge_id'] for row in challenges}), 16)
        denied = self.dispatch(states[2], CHALLENGE, {})
        self.assertEqual(denied.response['error']['cause']['kind'], 'challenge_limit')
        self.assertIn('result', self.dispatch(denied.state, ACT, arguments(challenges[0])).response)
        self.now = 119_000_000_000
        self.assertIsInstance(self.port.challenge(), dict)
        self.now = 121_000_000_000
        self.assertIsInstance(self.port.challenge(), dict)

    def test_challenge_limit_concurrent_at_15(self):
        for _ in range(15):
            self.assertIsInstance(self.port.challenge(), dict)
        release = threading.Barrier(2)
        second_trying = threading.Event()
        guard, arrivals_guard = threading.Lock(), threading.Lock()
        arrivals, attempts = [], []

        class ObservedLock:
            def __enter__(self):
                with arrivals_guard:
                    arrivals.append(threading.get_ident())
                    if len(arrivals) == 2:
                        second_trying.set()
                guard.acquire()
                return self

            def __exit__(self, *_):
                guard.release()

        token = terminal.secrets.token_hex

        def issue(n):
            if not attempts:
                attempts.append(True)
                self.assertTrue(second_trying.wait(5))
            return token(n)

        def challenge():
            release.wait(timeout=5)
            return self.port.challenge()

        with patch.object(self.port, '_lock', ObservedLock()), patch.object(terminal.secrets, 'token_hex', issue):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(challenge), pool.submit(challenge)
                results = (first.result(6), second.result(6))
        self.assertEqual(sum(isinstance(outcome, dict) for outcome in results), 1)
        self.assert_cause(next(outcome for outcome in results if not isinstance(outcome, dict)), 'challenge_limit')

    def test_act_after_delay_within_challenge_life(self):
        first, second = self.port.challenge(), self.port.challenge()
        self.now = 100_000_000_000
        self.assertIsInstance(self.port.act(arguments(first)), dict)
        self.now = 121_000_000_000
        self.assert_cause(self.port.act(arguments(second)), 'challenge_expired')
        self.assertEqual(len(self.received), 1)

    def test_foreign_value_refused(self):
        challenge = self.port.challenge()
        wrong = operator_terminal.build_message(PURPOSE, {}, store_id=challenge['store_id'], value=b'X' * 32)

        def check(message, signed):
            if message != signed:
                raise operator_terminal.OperatorCheckRefused('bad_signature')
            return SimpleNamespace(surface='terminal')

        with patch.object(self.port, '_verify', check):
            self.assert_cause(self.port.act(arguments(challenge, signature=wrong)), 'bad_signature')
        self.assert_cause(self.port.act(arguments(challenge)), 'challenge_used')
        self.assertEqual(self.received, [])

    def test_purpose_unhandled(self):
        challenge = self.port.challenge()
        args = arguments(challenge, purpose='seat_unseat')
        self.assert_cause(self.port.act(args), 'purpose_unhandled')
        self.assertEqual(len(self.checked), 1)
        self.assert_cause(self.port.act(args), 'challenge_used')

    def test_register_refuses_unlisted_purpose(self):
        with self.assertRaises(ValueError):
            self.acts.register('authority.waive', self.run_act, max_result_bytes=256)
        self.acts.register('seat_unseat', self.run_act, max_result_bytes=256)
        with self.assertRaises(ValueError):
            self.acts.register('seat_unseat', self.run_act, max_result_bytes=256)

    def test_no_principal_on_the_wire(self):
        document = json.loads(self.artifact.canonical_bytes)
        for name in ('TerminalChallengeArgs', 'TerminalChallengeResult', 'TerminalActArgs', 'TerminalActResult'):
            self.assertNotIn('principal', json.dumps(document['$defs'][name]))

    def test_terminal_wire_metadata(self):
        path = Path(__file__).resolve().parents[1] / 'generated/api/schema-3.0.json'
        document = json.loads(path.read_bytes())
        fields = ('principal', 'effect', 'retry', 'browser_get')
        expected = {CHALLENGE: ('unsigned', 'operator_challenge', 'none', 'none'),
                    ACT: ('unsigned', 'operator_confirmed_act', 'none', 'none')}

        def check(doc):
            self.assertEqual({row['name']: tuple(row[key] for key in fields)
                              for row in doc['x-adt']['operations'] if row['name'] in expected}, expected)

        check(document)
        next(row for row in document['x-adt']['operations'] if row['name'] == ACT)['retry'] = 'read_repeat'
        with self.assertRaises(AssertionError):
            check(document)

    def test_terminal_refusals_spelled(self):
        cold, expired, used = self.port.challenge(), self.port.challenge(), self.port.challenge()
        self.port.act(arguments(used))
        self.now = 121_000_000_000
        state = self.connect()
        for code, cls, kind, remedy in REFUSALS:
            with self.subTest(kind=kind):
                if kind == 'challenge_limit':
                    for _ in range(16):
                        self.assertIsInstance(self.port.challenge(), dict)
                    answer = self.dispatch(state, CHALLENGE, {})
                    self.now += 121_000_000_000
                else:
                    if kind == 'challenge_unknown':
                        args = arguments(dict(cold, challenge_id='0' * 32))
                    elif kind == 'challenge_used':
                        args = arguments(used)
                    elif kind == 'challenge_expired':
                        args = arguments(expired)
                    else:
                        args = arguments(self.port.challenge(), raw=b'{ }' if kind == 'args_not_canonical' else b'{}',
                                         purpose='seat_unseat' if kind == 'purpose_unhandled' else PURPOSE,
                                         signature=kind.encode('ascii'))
                    answer = self.dispatch(state, ACT, args)
                self.assertEqual(answer.response['error'],
                                 {'code': code, 'class': cls, 'cause': {'kind': kind}, 'remedy': remedy})
                state = answer.state
        with patch.object(self.port, 'act', return_value=p.ComponentRefusal('E_TERMINAL_CHECK', 'passwordless')):
            answer = self.dispatch(state, ACT, arguments(self.port.challenge()))
        self.assertEqual(answer.response['error']['cause']['kind'], 'handler_fault')

    def test_args_must_be_canonical(self):
        challenge = self.port.challenge()
        for raw in (b'{"z":1,"a":2}', b'{"a": 2,"z":1}', b'null', b'{'):
            with self.subTest(raw=raw):
                self.assert_cause(self.port.act(arguments(challenge, raw=raw)), 'args_not_canonical')
        self.assertEqual(self.checked, [])
        self.assertIsInstance(self.port.act(arguments(challenge, raw=b'{"a":2,"z":1}')), dict)

    def test_act_refusal_order(self):
        challenge = self.port.challenge()
        self.now = 121_000_000_000
        malformed = arguments(challenge, raw=b'{ }', signature=b'key_unsafe', purpose='seat_unseat')
        self.assert_cause(self.port.act(malformed), 'args_not_canonical')
        self.assert_cause(self.port.act(arguments(challenge, signature=b'key_unsafe')), 'challenge_expired')
        fresh = self.port.challenge()
        self.assert_cause(self.port.act(arguments(fresh, signature=b'key_unsafe',
                                                  purpose='seat_unseat')), 'key_unsafe')
        self.assert_cause(self.port.act(arguments(fresh, purpose='seat_unseat')), 'challenge_used')
        self.assertEqual(len(self.checked), 1)
        self.assertEqual(self.received, [])

    def test_terminal_frame_budget(self):
        state = self.connect()
        limit = 10240
        request_id = 10000000000000000000

        def frame(value):
            body = jcs.jcs_encode(dict(request_id=str(request_id), op=ACT, args=value))
            return len(body).to_bytes(4, 'big') + body

        def submit(raw):
            request = api_service.ApiService._read_request(
                SimpleNamespace(registry=self.registry, _exact=api_service.ApiService._exact),
                SimpleNamespace(recv=BytesIO(raw).read), state)
            return p.dispatch_api(state, request, self.runtime, self.registry, {ACT: self.port})

        def args(value, n=limit):
            raw = b'{"x":"' + b'x' * (n - 8) + b'"}'
            self.assertEqual(len(raw), n)
            return arguments(value, raw=raw, signature=b's' * 1024)

        challenge = self.port.challenge()
        valid = frame(args(challenge))
        self.assertEqual(len(valid) - 4, 15230)
        self.assertLessEqual(len(valid) - 4, api_service.REQUEST_BUDGET_UNBOUND)
        self.assertGreater(len(frame(args(challenge, 12288))) - 4, api_service.REQUEST_BUDGET_UNBOUND)
        for bad, reason in ((args(challenge, limit + 1), 'invalid_args'),
                            (args(dict(challenge, challenge_id='\x01' * 256)), 'malformed_frame'),
                            (args(dict(challenge, challenge_id='\x01' * 32)), 'invalid_args')):
            with self.subTest(reason=reason):
                bad_frame = frame(bad)
                if reason == 'malformed_frame':
                    self.assertEqual(len(bad_frame) - 4, 16734)
                self.assertEqual(submit(bad_frame).response['error']['cause']['kind'], reason)
        opaque = self.registry.types['OpaqueId']
        self.assertNotIsInstance(api_registry.validate_type(self.registry, d.TypeRef(opaque.name),
                                                             '\x01' * 32), api_registry.ValidationFailure)
        self.assertEqual(self.checked, [])
        self.assertIn('result', submit(valid).response)
        self.assertEqual(len(self.received), 1)

    def test_result_bound(self):
        for value in (0, 65537):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.acts.register('seat_unseat', self.run_act, max_result_bytes=value)
        self.acts.register('seat_unseat', lambda *_: {'x': '1'}, max_result_bytes=8)
        outcome = self.port.act(arguments(self.port.challenge(), purpose='seat_unseat'))
        self.assert_cause(outcome, 'handler_fault')
        self.assertEqual(outcome.code, 'E_API_INTERNAL')
