"""Strict discovery framing, parsing and response validation at the byte boundary."""
import json
import unittest

from relay_engine import api_definitions as d, api_registry as r, errors


def registry():
    def must_not_run(*args):
        raise AssertionError('codec called a handler')
    return r.compile_registry(d.discovery_definitions(),
                              {'protocol.connect': must_not_run, 'protocol.schema': must_not_run},
                              (errors.api_error_catalog(),))


def connect_result():
    return {'protocol': '3.0', 'engine_version': '3.0.0', 'build_id': 'a' * 64,
            'schema_digest': 'b' * 64, 'store_id': 'selected-store', 'store_kind': 'isolated',
            'runtime_instance_id': 'selected-runtime', 'supported': {'min': '3.0', 'max': '3.0'},
            'capabilities': [{'operation': 'protocol.connect'}, {'operation': 'protocol.schema'}],
            'deprecation': None}


class TestWire(unittest.TestCase):
    def setUp(self):
        import relay_engine.api_wire as wire
        self.w = wire
        self.registry = registry()

    def test_strict_json_refusals(self):
        cases = [(b'\xef\xbb\xbf{}', 'bom'), (b'"\xff"', 'invalid-utf8'),
                 (b'{"a":{"x":"a","x":"b"}}', 'duplicate-key'),
                 (b'{"a":"\\ud800"}', 'lone-surrogate'),
                 (b'{"\\udfff":"x"}', 'lone-surrogate'),
                 (b'NaN', 'number'), (b'Infinity', 'number'), (b'-Infinity', 'number'),
                 (b'[0]', 'number'), (b'[1.5]', 'number'), (b'[1e999]', 'number'),
                 (b'{} {}', 'trailing-data'), (b'{}x', 'trailing-data'),
                 (b'{', 'syntax'), (b'\xc2\xa0{}', 'syntax')]
        for body, kind in cases:
            with self.subTest(kind=kind, body=body):
                with self.assertRaises(self.w.StrictJsonError) as caught:
                    self.w.strict_json_loads(body)
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(str(caught.exception), kind)
        self.assertEqual(self.w.strict_json_loads(b' \n{"a":{"x":"ok"},"b":[true,null]}\t'),
                         {'a': {'x': 'ok'}, 'b': [True, None]})
        self.assertEqual(self.w.strict_json_loads(b'"\\ud83d\\ude00"'), '\U0001f600')

    def test_escaped_duplicate_key_refuses(self):
        body = b'{"request_id":"1","reque\\u0073t_id":"2","op":"protocol.schema","args":{}}'
        with self.assertRaises(self.w.StrictJsonError) as caught:
            self.w.strict_json_loads(body)
        self.assertEqual(caught.exception.kind, 'duplicate-key')
        refused = self.w.decode_api_frame(body, self.registry)
        self.assertEqual(refused, self.w.ProtocolRefusal(None, 'malformed_json', True))
        valid = self.w.decode_api_frame(b'{"reque\\u0073t_id":"1","op":"protocol.schema","args":{}}', self.registry)
        self.assertEqual(valid.request_id, '1')

    def test_frame_bounds(self):
        self.assertEqual(self.w.MAX_API_FRAME, 16777216)
        for size in (0, 16777217):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.w.ApiFrameDecoder().feed(size.to_bytes(4, 'big'))
        body = b'x' * 16777216
        decoder = self.w.ApiFrameDecoder()
        self.assertEqual(decoder.feed(len(body).to_bytes(4, 'big') + body), [body])
        self.assertIsNone(decoder.finish())
        for partial in (b'\0', b'\0\0\0', b'\0\0\0\x03xy'):
            decoder = self.w.ApiFrameDecoder()
            self.assertEqual(decoder.feed(partial), [])
            with self.assertRaises(ValueError):
                decoder.finish()
        with self.assertRaises(ValueError):
            decoder.feed(b'z')

    def test_fragmented_prefix_and_utf8_split_decode_identically(self):
        payload = {'request_id': '1', 'op': 'example.read', 'args': {'label': '雪'}}
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        frame = len(body).to_bytes(4, 'big') + body
        expected = self.w.ApiRequest('1', 'example.read', {'label': '雪'})
        for cut in range(len(frame) + 1):
            with self.subTest(cut=cut):
                decoder = self.w.ApiFrameDecoder()
                bodies = decoder.feed(frame[:cut]) + decoder.feed(frame[cut:])
                self.assertEqual(bodies, [body])
                self.assertEqual(self.w.decode_api_frame(bodies[0], self.registry), expected)
                decoder.finish()
        decoder = self.w.ApiFrameDecoder()
        chunks = []
        for byte in frame:
            chunks.extend(decoder.feed(bytes((byte,))))
        self.assertEqual(chunks, [body])
        self.assertEqual(self.w.ApiFrameDecoder().feed(frame + frame), [body, body])

    def test_envelope_correlation(self):
        invalid = [b'{', b'[]', b'{"request_id":"1","op":"protocol.schema"}',
                   b'{"request_id":"0","op":"protocol.schema","args":{}}',
                   b'{"request_id":"18446744073709551616","op":"protocol.schema","args":{}}',
                   b'{"request_id":"1","op":"Bad Op","args":{}}',
                   b'{"request_id":"1","op":"protocol.schema","args":{},"actor":"seat"}']
        for body in invalid:
            with self.subTest(body=body):
                value = self.w.decode_api_frame(body, self.registry)
                self.assertIsInstance(value, self.w.ProtocolRefusal)
                self.assertIsNone(value.request_id)
                self.assertTrue(value.close)
        body = b'{"request_id":"7","op":"protocol.connect","args":{"required_build_id":null}}'
        request = self.w.decode_api_frame(body, self.registry)
        self.assertEqual(request, self.w.ApiRequest('7', 'protocol.connect', {'required_build_id': None}))
        # Dispatch validates args after phase/id checks; this codec must preserve the trusted id.
        self.assertIsInstance(r.validate_type(self.registry, d.TypeRef('ConnectArgs'), request.args), r.ValidationFailure)
        self.assertEqual(self.w.decode_api_frame(b'', self.registry), self.w.ProtocolRefusal(None, 'malformed_frame', True))

    def test_response_validation_before_bytes(self):
        valid = {'request_id': '1', 'result': connect_result()}
        frame = self.w.encode_api_response(valid, self.registry, 'protocol.connect', None)
        self.assertEqual(int.from_bytes(frame[:4], 'big'), len(frame) - 4)
        self.assertEqual(self.w.strict_json_loads(frame[4:]), valid)
        bad_result = dict(connect_result(), capabilities=[{'operation': 'protocol.schema'}, {'operation': 'protocol.connect'}])
        invalid = [({'request_id': '1', 'result': bad_result}, 'protocol.connect'),
                   ({'request_id': None, 'result': connect_result()}, 'protocol.connect'),
                   (dict(valid, error={}), 'protocol.connect'), (valid, None)]
        for response, operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.w.encode_api_response(response, self.registry, operation, None)
        error = {'request_id': None, 'error': {'code': 'E_API_PROTOCOL', 'class': 'wire',
                 'cause': {'kind': 'unknown_operation'}, 'remedy': 'refresh_schema'}}
        self.assertEqual(self.w.strict_json_loads(self.w.encode_api_response(error, self.registry, None, None)[4:]), error)
        error['error']['remedy'] = 'resend_valid_request'
        with self.assertRaises(ValueError):
            self.w.encode_api_response(error, self.registry, None, None)


if __name__ == '__main__':
    unittest.main()
