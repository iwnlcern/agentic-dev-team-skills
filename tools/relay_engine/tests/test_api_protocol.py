"""Discovery state transitions and refusal precedence through the real codec."""
import base64
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from relay_engine import api_definitions as d, api_registry as r, api_schema, api_wire as w, errors


def make_registry(handlers=None):
    from relay_engine.api_protocol import discovery_handlers
    return r.compile_registry(d.discovery_definitions(), handlers or discovery_handlers(),
                              (errors.api_error_catalog(),))


def make_runtime(registry):
    from relay_engine.api_protocol import RuntimeContext
    return RuntimeContext('selected-store', 'isolated', 'selected-runtime', '3.0.0',
                          'a' * 64, True, {'3.0': api_schema.export_schema(registry, '3.0')})


def request(op='protocol.connect', request_id='1', args=None):
    if args is None:
        args = {'supported': {'min': '3.0', 'max': '3.0'}} if op == 'protocol.connect' else {}
    return w.ApiRequest(request_id, op, args)


class TestProtocol(unittest.TestCase):
    def setUp(self):
        import relay_engine.api_protocol as p
        self.p = p
        self.registry = make_registry()
        self.runtime = make_runtime(self.registry)

    def dispatch(self, state=None, req=None, runtime=None, registry=None):
        return self.p.dispatch_discovery(state or self.p.initial_connection_state(),
                                         req or request(), runtime or self.runtime,
                                         registry or self.registry)

    def test_shared_negotiation_and_cached_discovery_registry(self):
        window = {'min': '3.0', 'max': '3.10'}
        self.assertEqual(self.p.negotiate_protocol(window, ('3.2', '3.10', '3.1')), '3.10')
        self.assertIsNone(self.p.negotiate_protocol(window, ('4.0',)))
        self.p.discovery_registry.cache_clear()
        try:
            with patch.object(r, 'compile_registry', wraps=r.compile_registry) as compile_registry:
                first = self.p.discovery_registry()
                self.assertIs(self.p.discovery_registry(), first)
                self.assertEqual(compile_registry.call_count, 1)
                result = self.p.dispatch_discovery(self.p.initial_connection_state(), request(), self.runtime)
                self.assertEqual(result.response['result']['protocol'], '3.0')
                self.assertEqual(compile_registry.call_count, 1)
        finally:
            self.p.discovery_registry.cache_clear()

    def assert_refusal(self, result, kind, close, request_id, remedy=None):
        self.assertEqual(result.response['request_id'], request_id)
        self.assertEqual(result.response['error']['cause']['kind'], kind)
        self.assertEqual(result.close, close)
        self.assertEqual(result.state.phase, 'closed' if close else 'connected')
        self.assertNotIsInstance(r.validate_type(self.registry, d.TypeRef('ApiError'),
                                                result.response['error']), r.ValidationFailure)
        if remedy is not None:
            self.assertEqual(result.response['error']['remedy'], remedy)

    def test_connect_then_schema_success(self):
        result = self.dispatch()
        artifact = self.runtime.schemas['3.0']
        self.assertFalse(result.close)
        self.assertEqual(result.state, self.p.ConnectionState('connected', 1, '3.0', artifact.digest))
        expected = {'protocol': '3.0', 'engine_version': '3.0.0', 'build_id': 'a' * 64,
                    'schema_digest': artifact.digest, 'store_id': 'selected-store',
                    'store_kind': 'isolated', 'runtime_instance_id': 'selected-runtime',
                    'supported': {'min': '3.0', 'max': '3.0'},
                    'capabilities': [{'operation': 'protocol.connect'}, {'operation': 'protocol.schema'}],
                    'deprecation': None}
        self.assertEqual(result.response, {'request_id': '1', 'result': expected})
        schema = self.dispatch(result.state, request('protocol.schema', '9'))
        self.assertFalse(schema.close)
        self.assertEqual(schema.state.last_request_id, 9)
        self.assertEqual(schema.response, {'request_id': '9', 'result': {
            'protocol': '3.0', 'schema_digest': artifact.digest, 'schema_encoding': 'base64',
            'schema_byte_count': str(len(artifact.canonical_bytes)),
            'schema_bytes_base64': base64.b64encode(artifact.canonical_bytes).decode('ascii')}})
        ctx = r.DiscoveryValidationContext('schema_reply', '3.0', artifact.digest)
        self.assertEqual(w.strict_json_loads(w.encode_api_response(schema.response, self.registry,
                         'protocol.schema', ctx)[4:]), schema.response)

    def test_handshake_refusals_close(self):
        window = {'min': '2.0', 'max': '2.9'}
        result = self.dispatch(req=request(args={'supported': window}))
        self.assert_refusal(result, 'protocol_window', True, '1', 'update_client')
        self.assertEqual(result.response['error']['cause'], {
            'kind': 'protocol_window', 'requested': window, 'supported': {'min': '3.0', 'max': '3.0'}})
        for field, actual in [('build_id', self.runtime.build_id),
                              ('schema_digest', self.runtime.schemas['3.0'].digest)]:
            with self.subTest(field=field):
                args = dict(request().args, **{'required_' + field: 'f' * 64})
                refused = self.dispatch(req=request(args=args))
                self.assert_refusal(refused, 'pin_mismatch', True, '1', 'select_matching_candidate')
                self.assertEqual(refused.response['error']['cause'], {
                    'kind': 'pin_mismatch', 'field': field, 'required': 'f' * 64, 'actual': actual})
                args['required_' + field] = actual
                self.assertIn('result', self.dispatch(req=request(args=args)).response)
        for window in ({'min': '3.1', 'max': '3.0'}, {'min': '2.0', 'max': '3.0'}):
            self.assert_refusal(self.dispatch(req=request(args={'supported': window})),
                                'invalid_args', True, '1')
        self.assertIn('result', self.dispatch(req=request(args={'supported': {'min': '3.0', 'max': '3.9'}})).response)

    def test_phase_and_id_ordering(self):
        self.assert_refusal(self.dispatch(req=request('protocol.schema')), 'handshake_required', True, '1')
        # Phase precedes lookup and args on a trusted envelope.
        self.assert_refusal(self.dispatch(req=request('protocol.nope', args=[])), 'handshake_required', True, '1')
        connected = self.dispatch().state
        self.assert_refusal(self.dispatch(connected, request(request_id='2', args=[])),
                            'already_connected', True, '2')
        for request_id in ('1', '0'):
            expected = 'non_increasing_request_id' if request_id == '1' else 'invalid_envelope'
            correlation = '1' if request_id == '1' else None
            self.assert_refusal(self.dispatch(connected, request('protocol.schema', request_id)),
                                expected, True, correlation)
        # Id ordering precedes wrong phase and bad args.
        self.assert_refusal(self.dispatch(connected, request(args=[])), 'non_increasing_request_id', True, '1')
        maximum = self.dispatch(connected, request('protocol.schema', '18446744073709551615'))
        self.assertIn('result', maximum.response)
        self.assertEqual(maximum.state.last_request_id, 18446744073709551615)
        closed = self.dispatch(maximum.state, request('protocol.schema', '2'))
        self.assert_refusal(closed, 'non_increasing_request_id', True, '2')
        self.assertIsNone(self.dispatch(closed.state, request('protocol.schema', '3')).response)

    def test_unknown_operation_keeps_connection(self):
        connected = self.dispatch().state
        unknown = self.dispatch(connected, request('protocol.nope', '3', []))
        self.assert_refusal(unknown, 'unknown_operation', False, '3', 'refresh_schema')
        self.assertEqual(unknown.state.last_request_id, 3)
        invalid = self.dispatch(unknown.state, request('protocol.schema', '4', {'extra': 'x'}))
        self.assert_refusal(invalid, 'invalid_args', False, '4')
        self.assertIn('result', self.dispatch(invalid.state, request('protocol.schema', '5')).response)
        self.assert_refusal(self.dispatch(req=w.ProtocolRefusal(None, 'malformed_json', True)),
                            'malformed_json', True, None)

    def test_runtime_not_ready_refuses_and_closes(self):
        connected = self.dispatch().state
        for state, req in [(None, request()), (connected, request('protocol.schema', '2'))]:
            self.assert_refusal(self.dispatch(state, req, replace(self.runtime, ready=False)),
                                'runtime_not_ready', True, req.request_id)
            self.assert_refusal(self.dispatch(state, req, replace(self.runtime, schemas={})),
                                'schema_unavailable', True, req.request_id)
        changed = api_schema.SchemaArtifact('3.0', b'{}', 'f' * 64, 2)
        self.assert_refusal(self.dispatch(connected, request('protocol.schema', '2'),
                           replace(self.runtime, schemas={'3.0': changed})), 'schema_unavailable', True, '2')

    def test_handler_fault_is_fixed_internal(self):
        marker = 'PRIVATE-handler-payload'
        def broken(*_args):
            raise RuntimeError(marker)
        def invalid(*_args):
            return {'unregistered': marker}
        for handler in (broken, invalid):
            with self.subTest(handler=handler):
                handlers = self.p.discovery_handlers()
                handlers['protocol.schema'] = handler
                registry = make_registry(handlers)
                connected = self.dispatch(registry=registry).state
                with patch('relay_engine.strings.report_diagnostic') as diagnostic:
                    result = self.dispatch(connected, request('protocol.schema', '2'), registry=registry)
                self.assert_refusal(result, 'handler_fault', False, '2', 'report_nonsecret_failure')
                self.assertEqual(result.response['error']['code'], 'E_API_INTERNAL')
                self.assertNotIn(marker, json.dumps(result.response))
                self.assertTrue(self.runtime.ready)
                diagnostic.assert_called_once()
                exc = diagnostic.call_args.args[0]
                self.assertEqual(str(exc), 'E_API_INTERNAL')
                self.assertIsNone(exc.__traceback__)
                self.assertIsNone(exc.__context__)
        handlers = self.p.discovery_handlers()
        handlers['protocol.connect'] = invalid
        self.assert_refusal(self.dispatch(registry=make_registry(handlers)), 'handler_fault', True, '1')

    def test_dispatch_is_pure(self):
        before = (self.runtime, dict(self.runtime.schemas))
        with patch('builtins.open', side_effect=AssertionError('file access')), \
                patch('socket.socket', side_effect=AssertionError('socket access')):
            for _ in range(3):
                state = self.dispatch().state
                for request_id in ('2', '9'):
                    result = self.dispatch(state, request('protocol.schema', request_id))
                    self.assertIn('result', result.response)
                    state = result.state
        self.assertEqual((self.runtime, dict(self.runtime.schemas)), before)
        with self.assertRaises(TypeError):
            self.runtime.schemas['9.0'] = self.runtime.schemas['3.0']


class TestSessionDispatch(unittest.TestCase):
    def setUp(self):
        import relay_engine.api_protocol as p
        self.p = p
        self.binding = p.SeatBinding('project', 'team', 'seat', 'occupant', 'key', 'session', 7)
        self.other = replace(self.binding, session_id='other', generation=8)
        self.calls = []
        self.answer = p.SessionOutcome({}, 'keep', None)
        owner = self
        class Port:
            def run(self, operation, args, binding):
                owner.calls.append((operation, args, binding))
                if isinstance(owner.answer, Exception):
                    raise owner.answer
                return owner.answer
        self.port = Port()
        graph = d.discovery_definitions()
        template = graph.operations[1]
        operations = tuple(replace(template, name=name, result_ref=d.TypeRef('SchemaArgs'),
                                   principal=principal, effect='connection_binding',
                                   browser_get='none', rules=()) for name, principal in (
            ('session.end', 'seat_key'), ('session.resume', 'seat_key'),
            ('session.sign-in', 'seat_key'), ('test.seat', 'seat_session')))
        handlers = dict(p.discovery_handlers(), **p.session_handlers())
        handlers['test.seat'] = lambda args, binding, port: port.run('test.seat', args, binding)
        self.registry = r.compile_registry(replace(graph, operations=graph.operations + operations),
                                           handlers, (errors.api_error_catalog(),))
        self.runtime = make_runtime(self.registry)
        self.connected = p.dispatch_discovery(p.initial_connection_state(), request(),
                                               self.runtime, self.registry).state

    def dispatch(self, op='session.sign-in', state=None, args=None, request_id='2', runtime=None,
                 registry=None):
        return self.p.dispatch_api(self.connected if state is None else state,
                                   request(op, request_id, {} if args is None else args),
                                   self.runtime if runtime is None else runtime,
                                   self.registry if registry is None else registry, self.port)

    def refusal(self, result, kind, close=False):
        self.assertEqual(result.response['error']['cause']['kind'], kind)
        self.assertEqual(result.close, close)
        self.assertEqual(result.state.phase, 'closed' if close else 'connected')
        w.encode_api_response(result.response, self.registry, None, None)

    def test_discovery_operations_unchanged_through_dispatch_api(self):
        state = self.p.initial_connection_state()
        for req in (request(), request('protocol.schema', '3')):
            old = self.p.dispatch_discovery(state, req, self.runtime, self.registry)
            new = self.p.dispatch_api(state, req, self.runtime, self.registry, self.port)
            self.assertEqual(new, old)
            context = r.DiscoveryValidationContext('schema_reply', '3.0', old.state.negotiated_schema_digest)
            self.assertEqual(w.encode_api_response(new.response, self.registry, req.op, context),
                             w.encode_api_response(old.response, self.registry, req.op, context))
            state = old.state
        self.assertEqual(self.calls, [])

    def test_session_routes_phase_and_id_ordering(self):
        self.refusal(self.dispatch(state=self.p.initial_connection_state()), 'handshake_required', True)
        for last, value in ((1, '1'), (9, '2')):
            self.refusal(self.dispatch(state=replace(self.connected, last_request_id=last),
                                      request_id=value, args=[]), 'non_increasing_request_id', True)
        result = self.dispatch(request_id='9')
        self.assertEqual(result.response, {'request_id': '9', 'result': {}})
        self.assertEqual(result.state.last_request_id, 9)
        closed = self.dispatch(state=result.state, request_id='3')
        self.refusal(closed, 'non_increasing_request_id', True)
        self.assertIsNone(self.dispatch(state=closed.state, request_id='10').response)
        self.assertEqual(len(self.calls), 1)
        # Codec refusals and an invalid envelope win before any principal routing.
        for req, kind in ((w.ProtocolRefusal(None, 'malformed_json', True), 'malformed_json'),
                          (object(), 'invalid_envelope'),
                          (request('session.sign-in', '0'), 'invalid_envelope')):
            result = self.p.dispatch_api(self.connected, req, self.runtime, self.registry, self.port)
            self.refusal(result, kind, True)
        self.assertEqual(len(self.calls), 1)

    def test_session_routes_refuse_when_runtime_not_ready(self):
        bound = replace(self.connected, binding=self.binding)
        unavailable = replace(self.runtime, ready=False)
        result = self.dispatch(state=bound, runtime=unavailable)
        self.refusal(result, 'runtime_not_ready', True)
        self.assertEqual(result.state.binding, self.binding)
        self.assertFalse(unavailable.ready)
        self.assertEqual(self.calls, [])
        self.assertIn('result', self.dispatch(state=bound).response)
        self.assertEqual(len(self.calls), 1)

    def test_session_handlers_forward_to_injected_port(self):
        handlers = self.p.session_handlers()
        self.assertEqual(set(handlers), {'session.end', 'session.resume', 'session.sign-in'})
        for name, handler in handlers.items():
            args = {'probe': name}
            self.assertIs(handler(args, self.binding, self.port), self.answer)
            self.assertEqual(self.calls[-1], (name, args, self.binding))
        self.assertEqual(len(self.calls), 3)

    def test_unknown_operation_and_invalid_args_never_call_handler(self):
        self.refusal(self.dispatch('test.missing', args=[]), 'unknown_operation')
        self.refusal(self.dispatch(args={'extra': 'value'}), 'invalid_args')
        self.assertEqual(self.calls, [])
        self.assertIn('result', self.dispatch().response)
        self.assertEqual(len(self.calls), 1)

    def test_seat_session_principal_refuses_unsigned_before_handler(self):
        self.refusal(self.dispatch('test.seat'), 'not_signed_in')
        self.assertEqual(self.calls, [])
        self.assertIn('result', self.dispatch('test.seat', state=replace(
            self.connected, binding=self.binding)).response)
        self.assertEqual(self.calls, [('test.seat', {}, self.binding)])

    def test_resume_on_bound_connection_is_already_bound(self):
        result = self.dispatch('session.resume', state=replace(self.connected, binding=self.binding))
        self.refusal(result, 'already_bound')
        self.assertEqual(result.state.binding, self.binding)
        self.assertEqual(self.calls, [])
        self.assertIn('result', self.dispatch('session.resume').response)

    def test_sign_in_on_bound_connection_reaches_handler(self):
        result = self.dispatch(state=replace(self.connected, binding=self.binding))
        self.assertIn('result', result.response)
        self.assertEqual(self.calls, [('session.sign-in', {}, self.binding)])
        self.assertEqual(result.state.binding, self.binding)

    def test_raising_handler_is_fixed_handler_fault(self):
        self.answer = RuntimeError('PRIVATE-handler-text')
        result = self.dispatch(state=replace(self.connected, binding=self.binding))
        self.refusal(result, 'handler_fault')
        self.assertNotIn('PRIVATE-handler-text', json.dumps(result.response))
        self.assertEqual(result.state.binding, self.binding)
        self.assertTrue(self.runtime.ready)

    def test_invalid_result_with_set_or_clear_changes_no_binding(self):
        bound = replace(self.connected, binding=self.binding)
        for action, binding in (('set', self.other), ('clear', None)):
            with self.subTest(action=action):
                self.answer = self.p.SessionOutcome({'bad': 'result'}, action, binding)
                result = self.dispatch(state=bound)
                self.refusal(result, 'handler_fault')
                self.assertEqual(result.state.binding, self.binding)
                self.answer = self.p.SessionOutcome({}, action, binding)
                valid = self.dispatch(state=bound)
                self.assertIn('result', valid.response)
                self.assertEqual(valid.state.binding, binding)

    def test_refusal_outside_operation_arms_is_handler_fault(self):
        op = replace(self.registry.operations['session.sign-in'],
                     error_ref=d.TypeRef('ApiErrorAuthNotSignedIn'))
        restricted = replace(self.registry, operations=dict(self.registry.operations,
                              **{'session.sign-in': op}))
        for answer in (self.p.SessionRefusal('E_API_AUTH', 'key_rejected'),
                       self.p.SessionRefusal('E_API_CONFLICT', 'key_rejected'),
                       self.p.SessionRefusal('E_API_AUTH', 'unknown_cause')):
            self.answer = answer
            self.refusal(self.dispatch(registry=restricted), 'handler_fault')
        self.answer = self.p.SessionRefusal('E_API_AUTH', 'not_signed_in')
        self.refusal(self.dispatch(registry=restricted), 'not_signed_in')

    def test_not_current_causes_clear_binding_others_keep_it(self):
        clears = {'key_replaced', 'seat_stood_down', 'session_not_current'}
        for kind in sorted(clears | {'key_rejected', 'not_signed_in', 'already_bound',
                                    'operation_key_reused'}):
            with self.subTest(kind=kind):
                code = 'E_API_CONFLICT' if kind == 'operation_key_reused' else 'E_API_AUTH'
                self.answer = self.p.SessionRefusal(code, kind)
                result = self.dispatch(state=replace(self.connected, binding=self.binding))
                self.refusal(result, kind)
                self.assertEqual(result.state.binding, None if kind in clears else self.binding)

    def test_binding_never_taken_from_request_args(self):
        from dataclasses import asdict
        result = self.dispatch(args={'binding': asdict(self.other)},
                               state=replace(self.connected, binding=self.binding))
        self.refusal(result, 'invalid_args')
        self.assertEqual(result.state.binding, self.binding)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
