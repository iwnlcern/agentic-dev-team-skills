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
