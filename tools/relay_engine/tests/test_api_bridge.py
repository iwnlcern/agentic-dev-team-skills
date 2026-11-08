"""GET translation against a real pure discovery backend and hostile byte replies."""
from dataclasses import replace
import json
import unittest

from relay_engine import api_protocol as p, api_registry as r, api_wire as w, jcs
from relay_engine.tests.test_api_protocol import make_registry, make_runtime


class Backend:
    def __init__(self, registry, runtime, override=None):
        self.registry, self.runtime = registry, runtime
        self.state = p.initial_connection_state()
        self.requests, self.closed = [], 0
        self.override = override

    def exchange(self, frame):
        self.requests.append(frame)
        decoder = w.ApiFrameDecoder()
        bodies = decoder.feed(frame)
        decoder.finish()
        if len(bodies) != 1:
            raise AssertionError('not one request')
        request = w.decode_api_frame(bodies[0], self.registry)
        result = p.dispatch_discovery(self.state, request, self.runtime, self.registry)
        self.state = result.state
        context = r.DiscoveryValidationContext('schema_reply', self.state.negotiated_protocol,
                                                self.state.negotiated_schema_digest)
        response = result.response
        if self.override is not None:
            response = self.override(request, response)
        if isinstance(response, bytes):
            return response
        return w.encode_api_response(response, self.registry, request.op, context)

    def close(self):
        self.closed += 1


class TestBridge(unittest.TestCase):
    def setUp(self):
        import relay_engine.api_bridge as bridge
        self.b = bridge
        self.registry = make_registry()
        self.runtime = make_runtime(self.registry)
        self.backends = []
        self.override = None
        def open_backend():
            backend = Backend(self.registry, self.runtime, self.override)
            self.backends.append(backend)
            return backend
        self.context = bridge.BridgeContext('127.0.0.1:43210', 'http://127.0.0.1:43210', open_backend)

    def req(self, path='/api/connect', query=None, **kwargs):
        if query is None:
            query = ('min_protocol=3.0&max_protocol=3.0' if path == '/api/connect' else
                     'protocol=3.0&required_build_id=' + self.runtime.build_id +
                     '&required_schema_digest=' + self.runtime.schemas['3.0'].digest)
        return self.b.BridgeRequest(kwargs.get('method', 'GET'), path, query,
                    kwargs.get('headers', (('Host', self.context.advertised_host),)), kwargs.get('body', b''))

    def exchange(self, req=None):
        result = self.b.map_discovery_get(req or self.req(), self.context)
        self.assertEqual(dict(result.headers), {'Content-Type': 'application/json; charset=utf-8',
                                              'Cache-Control': 'no-store'})
        self.assertFalse(any(k.lower().startswith('access-control-') for k, _v in result.headers))
        return result, w.strict_json_loads(result.body)

    def test_connect_mapping(self):
        result, envelope = self.exchange()
        self.assertEqual(result.status, 200)
        self.assertEqual(envelope, p.dispatch_discovery(p.initial_connection_state(),
            w.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
            self.runtime, self.registry).response)
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(self.backends[0].closed, 1)
        self.assertEqual(len(self.backends[0].requests), 1)
        self.assertEqual(w.decode_api_frame(self.backends[0].requests[0][4:], self.registry),
            w.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}))

    def test_schema_mapping(self):
        result, envelope = self.exchange(self.req('/api/schema'))
        self.assertEqual(result.status, 200)
        self.assertEqual(envelope['request_id'], '2')
        self.assertEqual(envelope['result']['schema_digest'], self.runtime.schemas['3.0'].digest)
        backend = self.backends[0]
        self.assertEqual(backend.closed, 1)
        self.assertEqual([w.decode_api_frame(f[4:], self.registry) for f in backend.requests], [
            w.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'},
                'required_build_id': self.runtime.build_id,
                'required_schema_digest': self.runtime.schemas['3.0'].digest}),
            w.ApiRequest('2', 'protocol.schema', {})])
        bad = self.req('/api/schema', query='protocol=3.0&required_build_id=' + 'f' * 64 +
                       '&required_schema_digest=' + self.runtime.schemas['3.0'].digest)
        result, envelope = self.exchange(bad)
        self.assertEqual(result.status, 409)
        self.assertEqual(envelope['request_id'], '2')
        self.assertEqual(envelope['error']['cause']['kind'], 'pin_mismatch')
        self.assertEqual(len(self.backends[-1].requests), 1)
        self.assertEqual(self.backends[-1].closed, 1)

    def test_status_table(self):
        cases = [('invalid_args', {}, 400), ('already_connected', {}, 400),
                 ('pin_mismatch', {'field': 'build_id', 'required': 'f' * 64, 'actual': 'a' * 64}, 409),
                 ('runtime_not_ready', {}, 503), ('handler_fault', {}, 500)]
        for kind, payload, status in cases:
            with self.subTest(kind=kind):
                error = p.api_error(self.registry, kind, **payload)
                self.override = lambda req, _response: {'request_id': req.request_id, 'error': error}
                result, envelope = self.exchange()
                self.assertEqual(result.status, status)
                self.assertEqual(envelope, {'request_id': '1', 'error': error})
                self.assertEqual(self.backends[-1].closed, 1)

    def test_known_route_refusals_keep_route_correlation(self):
        requests = [self.req('/api/schema', headers=(('Host', 'other:43210'),)),
                    self.req(headers=(('Host', self.context.advertised_host), ('Origin', 'http://other'))),
                    self.req(method='POST'), self.req(body=b'x')]
        for req in requests:
            with self.subTest(req=req):
                result, envelope = self.exchange(req)
                self.assertEqual(result.status, 400)
                self.assertEqual(envelope['request_id'], '2' if req.path == '/api/schema' else '1')
                self.assertEqual(envelope['error']['cause']['kind'], 'invalid_envelope')
        self.assertEqual(self.backends, [])
        for path in ('/api/connect', '/api/schema'):
            self.assertEqual(self.exchange(self.req(path))[0].status, 200)

    def test_unknown_route_has_null_correlation(self):
        result, envelope = self.exchange(self.req('/api/other', query=''))
        self.assertEqual(result.status, 400)
        self.assertIsNone(envelope['request_id'])
        self.assertEqual(envelope['error']['cause']['kind'], 'invalid_envelope')
        self.assertEqual(self.backends, [])

    def test_host_and_origin_guards(self):
        host = ('Host', self.context.advertised_host)
        for path in ('/api/connect', '/api/schema'):
            for headers in [(), (host, host), (('Host', 'localhost:43210'),),
                            (host, ('Origin', 'null')), (host, ('Origin', self.context.advertised_origin + '/')),
                            (host, ('Origin', self.context.advertised_origin), ('origin', self.context.advertised_origin))]:
                with self.subTest(path=path, headers=headers):
                    before = len(self.backends)
                    result, _ = self.exchange(self.req(path, headers=headers))
                    self.assertEqual(result.status, 400)
                    self.assertEqual(len(self.backends), before)
            for headers in [(host,), (host, ('Origin', self.context.advertised_origin)),
                            (('hOsT', self.context.advertised_host),)]:
                self.assertEqual(self.exchange(self.req(path, headers=headers))[0].status, 200)
        self.context = replace(self.context, advertised_host='example.org:43210', advertised_origin='http://example.org:43210')
        before = len(self.backends)
        self.assertEqual(self.exchange()[0].status, 400)
        self.assertEqual(len(self.backends), before)

    def test_query_encoding_tricks_refuse_without_backend(self):
        good = self.req().raw_query
        cases = [(self.req(query=q), '1') for q in [good + '&%6din_protocol=3.0',
                 'min_protocol=3.0+&max_protocol=3.0', 'min_protocol=&max_protocol=3.0',
                 good + '&required_build_id=' + 'a' * 64 + '&required_build_id=' + 'a' * 64,
                 good + '&store=other', good + '&', 'min_protocol=3.0',
                 'min_protocol=3.1&max_protocol=3.0', 'min_protocol=2.0&max_protocol=3.0']]
        cases.extend((self.req('/api/schema', query=q), '2') for q in [
                     'protocol=3.0', self.req('/api/schema').raw_query + '&extra=x',
                     self.req('/api/schema').raw_query.replace('protocol=3.0', 'protocol=03.0')])
        for req, request_id in cases:
            with self.subTest(query=req.raw_query):
                result, envelope = self.exchange(req)
                self.assertEqual((result.status, envelope['request_id']), (400, request_id))
                self.assertEqual(envelope['error']['cause']['kind'], 'invalid_args')
        self.assertEqual(self.backends, [])
        self.assertEqual(self.exchange()[0].status, 200)

    def test_credentials_grant_nothing(self):
        first, _ = self.exchange()
        frames = self.backends[-1].requests
        second, _ = self.exchange(self.req(headers=(('Host', self.context.advertised_host),
                                ('Authorization', 'Bearer test-only-marker'), ('Cookie', 'test-only-marker'))))
        self.assertEqual(first, second)
        self.assertEqual(frames, self.backends[-1].requests)

    def test_untrusted_backend_responses_are_unavailable(self):
        def frame(value):
            data = jcs.jcs_encode(value)
            return len(data).to_bytes(4, 'big') + data
        def wrong_id(_req, response):
            return frame(dict(response, request_id='9'))
        def wrong_pin(_req, response):
            return frame({'request_id': response['request_id'],
                          'result': dict(response['result'], build_id='f' * 64)})
        def wrong_context(req, response):
            return frame({'request_id': req.request_id, 'result': dict(response['result'], protocol='3.1')}) if req.op == 'protocol.schema' else response
        for override in (wrong_id, wrong_pin, wrong_context, lambda *_: b'\0\0\0\x02{',
                         lambda *_: b'\0\0\0\x02{}\0\0\0\x02{}'):
            with self.subTest(override=override):
                self.override = override
                result, envelope = self.exchange(self.req('/api/schema'))
                self.assertEqual(result.status, 503)
                self.assertEqual(envelope['request_id'], '2')
                self.assertEqual(envelope['error']['code'], 'E_API_UNAVAILABLE')
                self.assertNotIn('result', envelope)
                self.assertEqual(self.backends[-1].closed, 1)


if __name__ == '__main__':
    unittest.main()
