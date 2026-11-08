"""The discovery graph must be closed, immutable and dependency-neutral."""
import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
import sys
import unittest

import relay_engine.api_definitions as d


class TestDefinitions(unittest.TestCase):
    def test_descriptors_are_frozen_and_closed(self):
        # A mutable graph could change the advertised contract after compilation.
        expected = {
            d.TypeRef: ('name',),
            d.TypeDef: ('name', 'shape', 'rules'),
            d.OperationDef: ('name', 'request_ref', 'result_ref', 'error_ref',
                             'principal', 'effect', 'retry', 'browser_get', 'rules'),
            d.ErrorBranchDef: ('cause_kind', 'remedy'),
            d.ErrorVariantDef: ('code', 'api_class', 'cause_ref', 'branches'),
            d.ClosedErrorDefinitions: ('types', 'variants'),
            d.DefinitionGraph: ('types', 'operations', 'error_ref'),
        }
        for cls, names in expected.items():
            with self.subTest(descriptor=cls.__name__):
                self.assertEqual(tuple(f.name for f in fields(cls)), names)
        graph = d.discovery_definitions()
        def visit(value):
            if is_dataclass(value):
                first = fields(value)[0].name
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, first, None)
                for field in fields(value):
                    visit(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    visit(item)
        visit(graph)
        for value in (d.ErrorBranchDef('handler_fault', 'report_nonsecret_failure'),
                      d.ErrorVariantDef('E_API_INTERNAL', 'internal', d.TypeRef('Cause'), ()),
                      d.ClosedErrorDefinitions((), ())):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, fields(value)[0].name, None)

    def test_discovery_graph_has_exactly_two_operations(self):
        graph = d.discovery_definitions()
        rows = [(o.name, o.principal, o.effect, o.retry, o.browser_get)
                for o in graph.operations]
        self.assertEqual(rows, [
            ('protocol.connect', 'unsigned', 'connection_metadata', 'read_repeat', 'connect'),
            ('protocol.schema', 'unsigned', 'pure_observation', 'read_repeat', 'schema'),
        ])
        self.assertEqual(graph.error_ref, d.TypeRef('ApiError'))
        self.assertEqual(graph, d.discovery_definitions())

    def test_discovery_types_cover_base_section_3_and_4(self):
        graph = d.discovery_definitions()
        names = [t.name for t in graph.types]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({
            'UInt64', 'RequestId', 'OpaqueId', 'Digest', 'ProtocolVersion',
            'ProtocolWindow', 'EngineVersion', 'OperationName', 'Capability',
            'DeprecationNotice', 'CanonicalSchemaBase64', 'ConnectArgs',
            'ConnectResult', 'SchemaArgs', 'SchemaResult',
        }.issubset(names))
        def visit(value):
            if isinstance(value, d.TypeRef):
                self.assertIn(value.name, set(names) | {'ApiError'})
            elif is_dataclass(value):
                for field in fields(value):
                    visit(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    visit(item)
        visit(graph)
        types = {t.name: t for t in graph.types}
        args = types['ConnectArgs'].shape
        self.assertEqual([(f.name, f.optional) for f in args.fields], [
            ('supported', False), ('required_build_id', True),
            ('required_schema_digest', True),
        ])
        self.assertEqual(types['SchemaArgs'].shape.fields, ())
        self.assertEqual([f.name for f in types['SchemaResult'].shape.fields], [
            'protocol', 'schema_digest', 'schema_encoding',
            'schema_byte_count', 'schema_bytes_base64',
        ])
        self.assertIsInstance(types['ProtocolWindow'].rules[0], d.ProtocolWindowV1)
        self.assertIsInstance(types['SchemaResult'].rules[0], d.SchemaPayloadV1)
        self.assertIsInstance(graph.operations[1].rules[0], d.NegotiatedSchemaV1)

    def test_module_imports_only_stdlib(self):
        def imports(path):
            result = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    result.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    result.update((node.module or '') + '.' + a.name for a in node.names)
            return result
        engine = Path(__file__).parents[1]
        actual = imports(engine / 'api_definitions.py')
        self.assertTrue(all(n.split('.')[0] in sys.stdlib_module_names for n in actual))
        self.assertIn('relay_engine.strings', imports(engine / 'errors.py'))


class TestErrorCatalog(unittest.TestCase):
    def test_catalog_has_exact_thirteen_pairs(self):
        from relay_engine import errors
        catalog = errors.api_error_catalog()
        pairs = {(v.code, v.api_class, b.cause_kind, b.remedy)
                 for v in catalog.variants for b in v.branches}
        self.assertEqual(len(catalog.variants), 5)
        self.assertEqual(pairs, {
            ('E_API_PROTOCOL', 'wire', 'malformed_frame', 'resend_valid_request'),
            ('E_API_PROTOCOL', 'wire', 'malformed_json', 'resend_valid_request'),
            ('E_API_PROTOCOL', 'wire', 'invalid_envelope', 'resend_valid_request'),
            ('E_API_PROTOCOL', 'wire', 'invalid_args', 'resend_valid_request'),
            ('E_API_PROTOCOL', 'wire', 'unknown_operation', 'refresh_schema'),
            ('E_API_STATE', 'wire', 'handshake_required', 'reconnect_explicitly'),
            ('E_API_STATE', 'wire', 'already_connected', 'reconnect_explicitly'),
            ('E_API_STATE', 'wire', 'non_increasing_request_id', 'reconnect_explicitly'),
            ('E_API_INCOMPATIBLE', 'version', 'protocol_window', 'update_client'),
            ('E_API_INCOMPATIBLE', 'version', 'pin_mismatch', 'select_matching_candidate'),
            ('E_API_UNAVAILABLE', 'unavailable', 'runtime_not_ready', 'restore_expected_runtime'),
            ('E_API_UNAVAILABLE', 'unavailable', 'schema_unavailable', 'restore_expected_runtime'),
            ('E_API_INTERNAL', 'internal', 'handler_fault', 'report_nonsecret_failure'),
        })

    def test_catalog_cause_payloads_match_base(self):
        from relay_engine import errors
        catalog = errors.api_error_catalog()
        types = {t.name: t for t in catalog.types}
        for variant in catalog.variants:
            cause = types[variant.cause_ref.name].shape
            self.assertIsInstance(cause, d.TaggedUnion)
            self.assertEqual(cause.tag, 'kind')
            kinds = []
            for ref in cause.arms:
                record = types[ref.name].shape
                self.assertIsInstance(record, d.Record)
                self.assertTrue(all(not f.optional for f in record.fields))
                kind_field = record.fields[0]
                self.assertEqual(kind_field.name, 'kind')
                literal = types[kind_field.type_ref.name].shape
                self.assertIsInstance(literal, d.LiteralSet)
                self.assertEqual(len(literal.values), 1)
                kind = literal.values[0]
                kinds.append(kind)
                payload = {f.name: f.type_ref.name for f in record.fields[1:]}
                if kind == 'protocol_window':
                    self.assertEqual(payload, {'requested': 'ProtocolWindow', 'supported': 'ProtocolWindow'})
                elif kind == 'pin_mismatch':
                    self.assertEqual(set(payload), {'field', 'required', 'actual'})
                    self.assertEqual(payload['required'], 'Digest')
                    self.assertEqual(payload['actual'], 'Digest')
                    self.assertEqual(types[payload['field']].shape.values, ('build_id', 'schema_digest'))
                else:
                    self.assertEqual(payload, {})
            self.assertEqual(sorted(kinds), [b.cause_kind for b in variant.branches])

    def test_catalog_is_stable_and_types_unique(self):
        from relay_engine import errors
        catalog = errors.api_error_catalog()
        self.assertEqual(catalog, errors.api_error_catalog())
        names = [t.name for t in catalog.types]
        self.assertEqual(len(names), len(set(names)))
        def visit(value):
            if isinstance(value, d.TypeRef):
                self.assertIn(value.name, names)
            elif is_dataclass(value):
                for field in fields(value):
                    visit(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    visit(item)
        visit(catalog)

    def test_legacy_registry_unchanged(self):
        from dataclasses import asdict
        import hashlib
        import json
        from relay_engine import errors, strings
        value = {
            'errors': {k: asdict(v) for k, v in sorted(errors.ERRORS.items())},
            'variants': [[list(k), asdict(v)] for k, v in sorted(errors.ERROR_VARIANTS.items())],
            'policy': sorted(errors.POLICY_CODES), 'codes': sorted(strings._CODE_VALUES),
        }
        def digest(data):
            return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest(value), '2c559d858771e30693daeb618c0f722cb797d702e2158de3854f3d0b38d2681c')
        self.assertFalse(any(k.startswith('E_API_') for k in errors.ERRORS))
        changed = dict(value, errors=dict(value['errors'], E_API_INTERNAL={}))
        self.assertNotEqual(digest(changed), digest(value))

    def test_legacy_as_dict_unchanged(self):
        from relay_engine import errors, strings
        result = errors.error_for('E-WIRE-OP', rejected_op=strings.rejected_value('0123456789ab', 7)).as_dict()
        self.assertEqual(result, {
            'code': 'E-WIRE-OP',
            'cause': 'unknown op unrecognized-input (sha256:0123456789ab, length 7)',
            'remedy': 'use an op from the daemon op table', 'cls': 'wire',
        })
