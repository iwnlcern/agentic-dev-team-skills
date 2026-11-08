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


if __name__ == '__main__':
    unittest.main()
