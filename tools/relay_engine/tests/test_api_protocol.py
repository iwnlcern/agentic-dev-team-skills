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


class TestInProcessPrincipals(unittest.TestCase):
    def build(self):
        from relay_engine import api_protocol as p, api_principals
        self.p = p
        self.principals = api_principals
        self.calls = []
        graph = d.discovery_definitions()
        operations = tuple(d.OperationDef('test.' + kind.replace('_', '-'), d.TypeRef('SchemaArgs'),
            d.TypeRef('SchemaArgs'), d.TypeRef('ApiError'), kind,
            'pure_observation', 'read_repeat', 'none')
            for kind in ('operator', 'service', 'unsigned', 'seat_key', 'seat_session'))
        def handler(args, principal, port):
            self.calls.append((principal, port))
            return {}
        graph = replace(graph, operations=graph.operations + operations)
        handlers = dict(p.discovery_handlers(), **{op.name: handler for op in operations})
        try:
            self.registry = r.compile_registry(graph, handlers, (errors.api_error_catalog(),))
        except r.RegistryCompileError as exc:
            self.fail(f'in-process principals unavailable: {exc.reason}')
        self.runtime = make_runtime(self.registry)
        self.ports = {op.name: object() for op in operations}
        self.state = p.dispatch_api(p.initial_connection_state(), request(),
            self.runtime, self.registry, self.ports).state
        self.assertIn('principal', __import__('inspect').signature(p.dispatch_api).parameters)

    def call(self, kind, principal):
        return self.p.dispatch_api(self.state, request('test.' + kind.replace('_', '-'), '2', {}),
            self.runtime, self.registry, self.ports, principal=principal)

    def test_in_process_principal_reaches_handler(self):
        self.build()
        operator = self.principals.OperatorPrincipal('page')
        service = self.principals.ServicePrincipal('approval_seal')
        binding = self.p.SeatBinding('project', 'team', 'seat', 'occupant', 'key', 'session', 1)
        for kind, accepted in (('operator', operator), ('service', service)):
            with self.subTest(kind=kind):
                answer = self.call(kind, accepted)
                self.assertEqual(answer.response['result'], {})
                self.assertIs(self.calls[-1][0], accepted)
                self.assertIs(self.calls[-1][1], self.ports['test.' + kind.replace('_', '-')])
            for rejected in (None, service if kind == 'operator' else operator,
                             binding, object(), 'supervision', {'surface': 'page'}):
                with self.subTest(kind=kind, rejected=type(rejected).__name__):
                    before = len(self.calls)
                    answer = self.call(kind, rejected)
                    self.assertEqual(answer.response['error']['cause']['kind'], 'not_signed_in')
                    self.assertEqual(len(self.calls), before)
        for kind in ('unsigned', 'seat_key', 'seat_session'):
            for supplied in (operator, service):
                with self.subTest(kind=kind, supplied=supplied):
                    before = len(self.calls)
                    with self.assertRaises(ValueError):
                        self.call(kind, supplied)
                    self.assertEqual(len(self.calls), before)
        # Discovery also declares unsigned: the early discovery path cannot bypass K7.
        with self.assertRaises(ValueError):
            self.p.dispatch_api(self.p.initial_connection_state(), request(), self.runtime,
                                self.registry, self.ports, principal=operator)

    def test_client_cannot_present_operator_or_service(self):
        self.build()
        for kind in ('operator', 'service'):
            answer = self.p.dispatch_api(self.state, request('test.' + kind.replace('_', '-'), '2', {}),
                                         self.runtime, self.registry, self.ports)
            self.assertEqual(answer.response['error']['cause']['kind'], 'not_signed_in')
            for forged in ({'principal': 'operator'}, {'principal': 'service'},
                           {'principal': 'supervision'}, {'surface': 'page'}):
                raw = dict(request_id='2', op='test.' + kind.replace('_', '-'), args={}, **forged)
                decoded = w.decode_api_frame(json.dumps(raw).encode(), self.registry)
                answer = self.p.dispatch_api(self.state, decoded, self.runtime,
                                             self.registry, self.ports)
                self.assertIn('error', answer.response)
        self.assertEqual(self.calls, [])


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
            @property
            def sessions(self):
                return self

            def run(self, operation, args, binding):
                owner.calls.append((operation, args, binding))
                if isinstance(owner.answer, Exception):
                    raise owner.answer
                if operation == 'test.seat' and isinstance(owner.answer, p.SessionOutcome):
                    return owner.answer.result
                return owner.answer
        self.port = Port()
        graph = d.discovery_definitions()
        template = graph.operations[1]
        operations = tuple(replace(template, name=name, result_ref=d.TypeRef('SchemaArgs'),
                                   principal=principal, effect='connection_binding',
                                   browser_get='none', rules=()) for name, principal in (
            ('seat.identify', 'seat_key'), ('session.end', 'seat_key'), ('session.resume', 'seat_key'),
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
                                   self.registry if registry is None else registry,
                                   {name: self.port for name in (self.registry if registry is None else registry).handlers})

    def refusal(self, result, kind, close=False):
        self.assertEqual(result.response['error']['cause']['kind'], kind)
        self.assertEqual(result.close, close)
        self.assertEqual(result.state.phase, 'closed' if close else 'connected')
        w.encode_api_response(result.response, self.registry, None, None)

    def test_identify_handler_forwards_without_binding(self):
        binding = self.p.SeatBinding('p', 't', 's', 'o', 'k', 'ss', 1)
        calls = []
        class Port:
            @property
            def sessions(self):
                return self

            def run(self, *args):
                calls.append(args)
                return {'seat_id': 's', 'seat_address': 't.s', 'project_id': 'p'}
        args = {'key_id': 'key', 'key_secret': 'secret'}
        result = self.p.session_handlers()['seat.identify'](args, binding, Port())
        self.assertEqual(result, {'seat_id': 's', 'seat_address': 't.s', 'project_id': 'p'})
        self.assertEqual(calls, [('seat.identify', args, None)])

    def test_discovery_operations_unchanged_through_dispatch_api(self):
        state = self.p.initial_connection_state()
        for req in (request(), request('protocol.schema', '3')):
            old = self.p.dispatch_discovery(state, req, self.runtime, self.registry)
            new = self.p.dispatch_api(state, req, self.runtime, self.registry,
                                      {name: self.port for name in self.registry.handlers})
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
            result = self.p.dispatch_api(self.connected, req, self.runtime, self.registry,
                                      {name: self.port for name in self.registry.handlers})
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
        self.assertEqual(set(handlers), {'seat.identify', 'session.end', 'session.resume', 'session.sign-in'})
        for name, handler in handlers.items():
            args = {'probe': name}
            self.assertIs(handler(args, self.binding, self.port), self.answer)
            self.assertEqual(self.calls[-1], (name, args, None if name == 'seat.identify' else self.binding))
        self.assertEqual(len(self.calls), 4)

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


class TestComponentDispatch(unittest.TestCase):
    def setUp(self):
        from relay_engine import api_compose as c, api_protocol as p
        self.p = p
        self.binding = p.SeatBinding('project', 'team', 'seat', 'occupant', 'key', 'session', 1)
        self.calls = {'alpha.read': [], 'beta.read': []}
        self.answers = {'alpha.read': {}, 'beta.read': {}}
        self.ports = {}
        owner = self
        class Port:
            def __init__(self, name):
                self.name = name
            def run(self, args, principal):
                owner.calls[self.name].append((args, principal, self))
                return owner.answers[self.name]
        graph = d.discovery_definitions()
        components = [c.ApiComponent('platform', graph, p.discovery_handlers(), lambda ctx: None)]
        for name, principal in (('alpha', 'unsigned'), ('beta', 'seat_session')):
            operation = name + '.read'
            arg = d.TypeDef(name.title() + 'Args', d.Record(()))
            result = d.TypeDef(name.title() + 'Result', d.Record(()))
            op = d.OperationDef(operation, d.TypeRef(arg.name), d.TypeRef(result.name),
                d.TypeRef('ApiError'), principal, 'pure_observation', 'read_repeat', 'none')
            definitions = d.DefinitionGraph((arg, result), (op,), d.TypeRef('ApiError'))
            components.append(c.ApiComponent(name, definitions,
                {operation: lambda args, principal, port: port.run(args, principal)}, lambda ctx: None))
            self.ports[operation] = Port(operation)
        composed = c.compose_components(components)
        self.registry = r.compile_registry(composed.definitions, composed.handlers, (errors.api_error_catalog(),))
        self.runtime = make_runtime(self.registry)
        self.connected = p.dispatch_discovery(p.initial_connection_state(), request(),
            self.runtime, self.registry).state
        self.bound = replace(self.connected, binding=self.binding)

    def call(self, op='beta.read', state=None, request_id='2'):
        return self.p.dispatch_api(self.bound if state is None else state,
            request(op, request_id, {}), self.runtime, self.registry, self.ports)

    def test_each_operation_receives_its_own_port(self):
        for operation in self.ports:
            result = self.call(operation)
            self.assertEqual(result.response, {'request_id': '2', 'result': {}})
            self.assertIs(self.calls[operation][0][2], self.ports[operation])
        self.assertEqual({k: len(v) for k, v in self.calls.items()}, {'alpha.read': 1, 'beta.read': 1})

    def test_unsigned_component_gets_none_on_bound_connection(self):
        result = self.call('alpha.read')
        self.assertIn('result', result.response)
        self.assertIsNone(self.calls['alpha.read'][0][1])
        self.assertEqual(result.state.binding, self.binding)

    def test_seat_operation_gets_binding(self):
        result = self.call()
        self.assertIn('result', result.response)
        self.assertEqual(self.calls['beta.read'][0][1], self.binding)
        self.assertEqual(result.state.binding, self.binding)

    def test_component_refusal_resolves_complete_arm(self):
        self.answers['beta.read'] = self.p.ComponentRefusal('E_API_ADMISSION', 'policy',
                                                          {'engine_code': 'E-ADDRESS'})
        result = self.call()
        self.assertEqual(result.response['error'], self.p.api_error(self.registry, 'policy', engine_code='E-ADDRESS'))
        for code, kind in (('E_API_INTERNAL', 'unknown_cause'), ('E_API_CONFLICT', 'policy')):
            self.answers['beta.read'] = self.p.ComponentRefusal(code, kind)
            self.assertEqual(self.call().response['error']['cause']['kind'], 'handler_fault')

    def test_clearing_auth_causes_clear_binding_from_any_component(self):
        for kind in ('key_replaced', 'seat_stood_down', 'session_not_current'):
            with self.subTest(kind=kind):
                self.answers['beta.read'] = self.p.ComponentRefusal('E_API_AUTH', kind)
                result = self.call()
                self.assertEqual(result.response['error'], self.p.api_error(self.registry, kind))
                self.assertIsNone(result.state.binding)
                before = len(self.calls['beta.read'])
                next_result = self.call(state=result.state, request_id='3')
                self.assertEqual(next_result.response['error']['cause']['kind'], 'not_signed_in')
                self.assertEqual(len(self.calls['beta.read']), before)

    def test_ordinary_refusal_keeps_binding(self):
        self.answers['beta.read'] = self.p.ComponentRefusal('E_API_CONFLICT', 'submission_key_reused')
        result = self.call()
        self.assertEqual(result.response['error'], self.p.api_error(self.registry, 'submission_key_reused'))
        self.assertEqual(result.state.binding, self.binding)

    def test_foreign_session_outcome_is_handler_fault(self):
        for action in ('set', 'clear', 'keep'):
            with self.subTest(action=action):
                self.answers['alpha.read'] = self.p.SessionOutcome({}, action, self.binding)
                result = self.call('alpha.read', state=self.connected)
                self.assertEqual(result.response['error']['cause']['kind'], 'handler_fault')
                self.assertIsNone(result.state.binding)

    def test_discovery_routes_unchanged(self):
        state = self.p.initial_connection_state()
        for req in (request(), request('protocol.schema', '3')):
            expected = self.p.dispatch_discovery(state, req, self.runtime, self.registry)
            actual = self.p.dispatch_api(state, req, self.runtime, self.registry, self.ports)
            self.assertEqual(actual, expected)
            context = r.DiscoveryValidationContext('schema_reply', '3.0', actual.state.negotiated_schema_digest)
            self.assertEqual(w.encode_api_response(actual.response, self.registry, req.op, context),
                             w.encode_api_response(expected.response, self.registry, req.op, context))
            state = actual.state
        self.assertEqual(self.calls, {'alpha.read': [], 'beta.read': []})

    def test_unsigned_component_reaches_its_port(self):
        result = self.call('alpha.read', state=self.connected)
        self.assertEqual(result.response, {'request_id': '2', 'result': {}})
        self.assertEqual(self.calls['alpha.read'], [({}, None, self.ports['alpha.read'])])
        self.assertEqual(self.calls['beta.read'], [])


class TestDispatcherHardening(unittest.TestCase):
    """Review 161645 items 1 and 2: the runtime context and unknown refusals."""

    def setUp(self):
        import relay_engine.api_protocol as p
        self.p = p
        self.registry = make_registry()
        self.runtime = make_runtime(self.registry)
        fixture = TestComponentDispatch('test_each_operation_receives_its_own_port')
        fixture.setUp()
        self.composed = fixture.registry
        self.composed_runtime = fixture.runtime

    def test_runtime_context_validates_at_construction(self):
        artifact = self.runtime.schemas['3.0']
        for build_id in ('a' * 63, 'A' * 64, 'g' * 64, 'a' * 65, 7):
            with self.subTest(build_id=build_id):
                with self.assertRaises(ValueError) as caught:
                    replace(self.runtime, build_id=build_id)
                self.assertEqual(caught.exception.args, ('invalid_runtime_context',))
        for digest in ('f' * 63, 'F' * 64, 'z' * 64):
            with self.subTest(digest=digest):
                with self.assertRaises(ValueError) as caught:
                    replace(self.runtime, schemas={'3.0': replace(artifact, digest=digest)})
                self.assertEqual(caught.exception.args, ('invalid_runtime_context',))
        with self.assertRaises(ValueError) as caught:
            replace(self.runtime, schemas={'3.1': artifact})
        self.assertEqual(caught.exception.args, ('invalid_runtime_context',))
        changed = api_schema.SchemaArtifact('3.0', b'{}', 'f' * 64, 2)
        self.assertEqual(replace(self.runtime, schemas={'3.0': changed}).schemas['3.0'], changed)
        self.assertEqual(make_runtime(self.registry).build_id, 'a' * 64)

    def test_unknown_protocol_refusal_is_handler_fault(self):
        cases = [(None, 'not_a_decoder_kind'), ('7', 'not_a_decoder_kind'),
                 (None, 'key_rejected'), ('not-an-id', 'not_a_decoder_kind')]
        for entry in ('discovery', 'api'):
            for request_id, kind in cases:
                with self.subTest(entry=entry, request_id=request_id, kind=kind):
                    registry = self.registry if entry == 'discovery' else self.composed
                    runtime = self.runtime if entry == 'discovery' else self.composed_runtime
                    refusal = w.ProtocolRefusal(request_id, kind, False)
                    if entry == 'discovery':
                        result = self.p.dispatch_discovery(self.p.initial_connection_state(), refusal,
                                                           runtime, registry)
                    else:
                        result = self.p.dispatch_api(self.p.initial_connection_state(), refusal,
                                                     runtime, registry, {})
                    self.assertEqual(result.response['request_id'], request_id if request_id == '7' else None)
                    self.assertEqual(result.response['error']['code'], 'E_API_INTERNAL')
                    self.assertEqual(result.response['error']['cause'], {'kind': 'handler_fault'})
                    self.assertTrue(result.close)
            for kind in ('malformed_frame', 'malformed_json', 'invalid_envelope'):
                with self.subTest(entry=entry, decoder_kind=kind):
                    refusal = w.ProtocolRefusal(None, kind, True)
                    if entry == 'discovery':
                        result = self.p.dispatch_discovery(self.p.initial_connection_state(), refusal,
                                                           self.runtime, self.registry)
                    else:
                        result = self.p.dispatch_api(self.p.initial_connection_state(), refusal,
                                                     self.composed_runtime, self.composed, {})
                    self.assertEqual(result.response['error']['cause']['kind'], kind)
                    self.assertTrue(result.close)

    def test_decoder_kinds_constant_matches_wire(self):
        import ast
        with open(w.__file__, encoding='utf-8') as handle:
            tree = ast.parse(handle.read())
        built = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'ProtocolRefusal'
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
                built.add(node.args[1].value)
        self.assertEqual(built, {'malformed_frame', 'malformed_json', 'invalid_envelope'})
        self.assertEqual(set(self.p.DECODER_REFUSAL_KINDS), built)


if __name__ == '__main__':
    unittest.main()
