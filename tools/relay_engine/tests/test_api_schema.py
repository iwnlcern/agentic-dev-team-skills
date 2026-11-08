"""Canonical discovery artifact and generated-consumer conformance controls."""
import base64
from dataclasses import replace
import hashlib
import json
import unittest

from relay_engine import api_definitions as d, api_registry as r, errors, jcs


def registry():
    return r.compile_registry(d.discovery_definitions(),
                              {'protocol.connect': lambda args: None,
                               'protocol.schema': lambda args: None},
                              (errors.api_error_catalog(),))


class TestExport(unittest.TestCase):
    def setUp(self):
        from relay_engine import api_schema
        self.s = api_schema
        self.registry = registry()

    def export(self, graph=None):
        return self.s.export_schema(graph or self.registry, '3.0')

    def test_jcs_profile_controls(self):
        vectors = [
            ({'z': None, 'a': [True, False, 3]}, b'{"a":[true,false,3],"z":null}'),
            ({'text': '\b\t\n\f\r"\\\x00/'}, b'{"text":"\\b\\t\\n\\f\\r\\"\\\\\\u0000/"}'),
            ({'\ue000': 'private', '\U00010000': 'astral', '\r': 'control'},
             '{"\\r":"control","\U00010000":"astral","\ue000":"private"}'.encode()),
        ]
        for value, expected in vectors:
            with self.subTest(value=value):
                self.assertEqual(jcs.jcs_encode(value), expected)
        with self.assertRaises(ValueError):
            jcs.jcs_encode({'bad': '\ud800'})

    def test_export_is_deterministic(self):
        a, b = self.export(), self.export(registry())
        self.assertEqual(a, b)
        self.assertEqual(a.protocol, '3.0')
        self.assertEqual(a.byte_count, len(a.canonical_bytes))
        self.assertEqual(a.digest, hashlib.sha256(a.canonical_bytes).hexdigest())
        self.assertFalse(a.canonical_bytes.endswith(b'\n'))
        self.assertEqual(a.canonical_bytes, jcs.jcs_encode(json.loads(a.canonical_bytes)))

    def test_schema_document_shape(self):
        doc = json.loads(self.export().canonical_bytes)
        self.assertEqual(doc['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        self.assertEqual(doc['$id'], 'urn:adt:api:3.0')
        self.assertEqual(doc['oneOf'], [{'$ref': '#/$defs/ApiRequest'}, {'$ref': '#/$defs/ApiResponse'}])
        self.assertEqual(set(doc), {'$schema', '$id', '$defs', 'oneOf', 'x-adt'})
        meta = doc['x-adt']
        self.assertEqual(set(meta), {'format', 'protocol', 'operations', 'rules'})
        self.assertEqual((meta['format'], meta['protocol']), ('adt-api-v1', '3.0'))
        self.assertEqual([o['name'] for o in meta['operations']], ['protocol.connect', 'protocol.schema'])
        for op in meta['operations']:
            self.assertEqual(set(op), {'name', 'request_ref', 'result_ref', 'error_ref', 'principal', 'effect', 'retry', 'browser_get'})
            for key in ('request_ref', 'result_ref', 'error_ref'):
                self.assertIn(op[key], doc['$defs'])
        rules = meta['rules']
        self.assertEqual({rule['kind'] for rule in rules}, {
            'utf8_max_bytes', 'canonical_uint64', 'canonical_base64', 'sorted_unique',
            'protocol_window_v1', 'schema_payload_v1', 'negotiated_schema_v1'})
        scopes = [(rule.get('type_ref', rule.get('array_ref', rule.get('operation'))), rule['kind']) for rule in rules]
        self.assertEqual(scopes, sorted(set(scopes)))
        self.assertEqual(len(doc['$defs']['ApiError']['oneOf']), 13)
        for item in doc['$defs'].values():
            if item.get('type') == 'object':
                self.assertIs(item['additionalProperties'], False)
                self.assertIn('required', item)
        self.assertNotIn('required_build_id', doc['$defs']['ConnectArgs']['required'])
        self.assertEqual(doc['$defs']['ConnectArgs']['properties']['required_build_id'], {'$ref': '#/$defs/Digest'})

    def test_no_remote_refs_or_runtime_values(self):
        artifact = self.export()
        doc = json.loads(artifact.canonical_bytes)
        def walk(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    self.assertTrue(value['$ref'].startswith('#/$defs/'))
                    self.assertIn(value['$ref'][8:], doc['$defs'])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(doc)
        for forbidden in ('private-store-value', 'runtime-incarnation-value', 'abc123' * 10, artifact.digest):
            self.assertNotIn(forbidden.encode(), artifact.canonical_bytes)
        for forbidden in ('manifest', 'timestamp', 'generated_at'):
            self.assertNotIn(forbidden, doc['x-adt'])

    def test_digest_changes_only_with_graph(self):
        from types import SimpleNamespace
        runtime = SimpleNamespace(store_id='private-store-value', build_id='1' * 64)
        original = self.export()
        runtime.store_id = 'different-store-value'
        runtime.build_id = '2' * 64
        with_other_runtime = r.compile_registry(d.discovery_definitions(),
            {'protocol.connect': lambda args: runtime, 'protocol.schema': lambda args: runtime},
            (errors.api_error_catalog(),))
        self.assertEqual(self.export(with_other_runtime).canonical_bytes, original.canonical_bytes)
        types = dict(self.registry.types)
        node = types['SchemaArgs']
        types['SchemaArgs'] = replace(node, shape=d.Record((d.FieldDef('extra', d.TypeRef('Digest'), True),)))
        self.assertNotEqual(self.export(replace(self.registry, types=types)).digest, original.digest)
        # Mutate only the compiled export graph: this is not an owner-catalog registration.
        types = dict(self.registry.types)
        first = types['ApiError'].shape.arms[0]
        record = types[first.name]
        types['ExtraCauseKind'] = d.TypeDef('ExtraCauseKind', d.LiteralSet(('new_branch',)))
        types['ExtraCause'] = d.TypeDef('ExtraCause', d.Record((d.FieldDef('kind', d.TypeRef('ExtraCauseKind')),)))
        fields = tuple(replace(f, type_ref=d.TypeRef('ExtraCause')) if f.name == 'cause' else f for f in record.shape.fields)
        types['ExtraError'] = d.TypeDef('ExtraError', d.Record(fields))
        node = types['ApiError']
        types['ApiError'] = replace(node, shape=d.TaggedUnion('code', node.shape.arms + (d.TypeRef('ExtraError'),)))
        self.assertNotEqual(self.export(replace(self.registry, types=types)).digest, original.digest)

    def test_size_bounds(self):
        artifact = self.export()
        self.assertLessEqual(artifact.byte_count, 8388608)
        reply = {'request_id': '18446744073709551615', 'result': {
            'protocol': '3.0', 'schema_encoding': 'base64', 'schema_digest': artifact.digest,
            'schema_byte_count': str(artifact.byte_count),
            'schema_bytes_base64': base64.b64encode(artifact.canonical_bytes).decode()}}
        self.assertLessEqual(len(jcs.jcs_encode(reply)), 16777216)
        types = dict(self.registry.types, Oversize=d.TypeDef('Oversize', d.LiteralSet(('x' * 8388608,))))
        with self.assertRaises(self.s.SchemaExportError) as caught:
            self.export(replace(self.registry, types=types))
        self.assertEqual(caught.exception.reason, 'schema_too_large')
        self.assertLess(self.export().byte_count, 8388608)

    def test_unrepresentable_profile_refuses(self):
        for invalid in (1.5, 2 ** 53):
            types = dict(self.registry.types, Invalid=d.TypeDef('Invalid', d.Array(d.TypeRef('Digest'), invalid)))
            with self.subTest(invalid=invalid):
                with self.assertRaises(self.s.SchemaExportError) as caught:
                    self.export(replace(self.registry, types=types))
                self.assertEqual(caught.exception.reason, 'unrepresentable_profile')
        self.assertIsInstance(self.export().canonical_bytes, bytes)


if __name__ == '__main__':
    unittest.main()
