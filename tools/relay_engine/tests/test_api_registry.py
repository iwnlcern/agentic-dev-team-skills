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
    def test_catalog_has_exact_twenty_pairs(self):
        from relay_engine import errors
        catalog = errors.api_error_catalog()
        pairs = {(v.code, v.api_class, b.cause_kind, b.remedy)
                 for v in catalog.variants for b in v.branches}
        self.assertEqual(len(catalog.variants), 7)
        self.assertEqual(pairs, {
            ('E_API_AUTH', 'auth', 'key_rejected', 'check_key_file'),
            ('E_API_AUTH', 'auth', 'key_replaced', 'obtain_current_key'),
            ('E_API_AUTH', 'auth', 'seat_stood_down', 'ask_top_seat'),
            ('E_API_AUTH', 'auth', 'session_not_current', 'sign_in_again'),
            ('E_API_AUTH', 'auth', 'not_signed_in', 'sign_in_first'),
            ('E_API_AUTH', 'auth', 'already_bound', 'open_new_connection'),
            ('E_API_CONFLICT', 'conflict', 'operation_key_reused', 'use_new_operation_key'),
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

    def test_auth_and_conflict_arms(self):
        from relay_engine import api_protocol as p, api_registry as r, errors
        registry = r.compile_registry(d.discovery_definitions(), p.discovery_handlers(),
                                      (errors.api_error_catalog(),))
        expected = {
            ('E_API_AUTH', 'auth', 'key_rejected', 'check_key_file'),
            ('E_API_AUTH', 'auth', 'key_replaced', 'obtain_current_key'),
            ('E_API_AUTH', 'auth', 'seat_stood_down', 'ask_top_seat'),
            ('E_API_AUTH', 'auth', 'session_not_current', 'sign_in_again'),
            ('E_API_AUTH', 'auth', 'not_signed_in', 'sign_in_first'),
            ('E_API_AUTH', 'auth', 'already_bound', 'open_new_connection'),
            ('E_API_CONFLICT', 'conflict', 'operation_key_reused', 'use_new_operation_key'),
        }
        observed = set()
        for arm in registry.error_arms():
            if arm.code not in ('E_API_AUTH', 'E_API_CONFLICT'):
                continue
            cause = registry.types[arm.cause_type.name].shape
            self.assertEqual(tuple(f.name for f in cause.fields), ('kind',))
            kind = registry.types[cause.fields[0].type_ref.name].shape.values[0]
            observed.add((arm.code, arm.wire_class, kind, arm.remedy))
            error = p.api_error(registry, kind)
            self.assertIsInstance(r.validate_type(registry, d.TypeRef('ApiError'), error),
                                  r.ValidatedValue)
            wrong = dict(error, remedy='unknown_remedy')
            self.assertIsInstance(r.validate_type(registry, d.TypeRef('ApiError'), wrong),
                                  r.ValidationFailure)
        self.assertEqual(observed, expected)

    def test_capabilities_unchanged_by_error_arms(self):
        from relay_engine import api_protocol as p, api_registry as r, errors
        registry = r.compile_registry(d.discovery_definitions(), p.discovery_handlers(),
                                      (errors.api_error_catalog(),))
        self.assertEqual(registry.capabilities(), ('protocol.connect', 'protocol.schema'))
        self.assertIsNone(registry.operation('seat.sign_in'))

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
        self.assertEqual(digest(value), '7991ec5d4b116490537c695227395e3090a0dbc612b391bdf2687dade11d07e2')
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


class TestCompile(unittest.TestCase):
    def setUp(self):
        from relay_engine import errors
        import relay_engine.api_registry as registry
        self.r = registry
        self.graph = d.discovery_definitions()
        self.catalog = errors.api_error_catalog()
        self.handlers = {'protocol.connect': lambda args: None, 'protocol.schema': lambda args: None}

    def compile(self, graph=None, catalog=None, handlers=None):
        return self.r.compile_registry(graph or self.graph,
                                       self.handlers if handlers is None else handlers,
                                       (catalog or self.catalog,))

    def test_seat_operation_metadata_compiles(self):
        from dataclasses import replace
        from itertools import product
        for principal, effect, retry in product(
                ('unsigned', 'seat_key', 'seat_session'),
                ('connection_metadata', 'pure_observation', 'session_write', 'connection_binding'),
                ('read_repeat', 'operation_key')):
            with self.subTest(principal=principal, effect=effect, retry=retry):
                op = replace(self.graph.operations[1], name='test.seat',
                             principal=principal, effect=effect, retry=retry,
                             browser_get='none', rules=())
                graph = replace(self.graph, operations=(op,))
                handler = lambda args: None
                registry = self.compile(graph=graph, handlers={op.name: handler})
                self.assertEqual(registry.operation(op.name), op)
                self.assertIs(registry.handlers[op.name], handler)
                self.assertEqual(registry.capabilities(), ('test.seat',))
        self.assertEqual(self.compile().capabilities(), ('protocol.connect', 'protocol.schema'))

    def test_unknown_metadata_still_refuses(self):
        from dataclasses import replace
        for field in ('principal', 'effect', 'retry', 'browser_get'):
            with self.subTest(field=field):
                op = replace(self.graph.operations[1], **{field: 'unknown'})
                with self.assertRaises(self.r.RegistryCompileError) as caught:
                    self.compile(graph=replace(self.graph, operations=(self.graph.operations[0], op)))
                self.assertEqual(caught.exception.reason, 'invalid_definition')

    def test_browser_route_requires_unsigned(self):
        from dataclasses import replace
        for principal in ('seat_key', 'seat_session'):
            for route in ('connect', 'schema'):
                with self.subTest(principal=principal, browser_get=route):
                    op = replace(self.graph.operations[1], principal=principal, browser_get=route)
                    with self.assertRaises(self.r.RegistryCompileError) as caught:
                        self.compile(graph=replace(self.graph, operations=(self.graph.operations[0], op)))
                    self.assertEqual(caught.exception.reason, 'invalid_definition')

    def test_handler_coverage_unchanged(self):
        cases = [
            {'protocol.connect': self.handlers['protocol.connect']},
            dict(self.handlers, extra=lambda args: None),
            dict(self.handlers, **{'protocol.schema': None}),
        ]
        for handlers in cases:
            with self.subTest(handlers=tuple(handlers)):
                with self.assertRaises(self.r.RegistryCompileError) as caught:
                    self.compile(handlers=handlers)
                self.assertEqual(caught.exception.reason, 'unbound_handler')
        registry = self.compile()
        self.assertEqual(registry.capabilities(), ('protocol.connect', 'protocol.schema'))
        self.assertEqual(set(registry.handlers), set(self.handlers))

    def test_compile_refuses_graph_defects(self):
        from dataclasses import replace
        cases = [
            (replace(self.graph, types=self.graph.types + (self.graph.types[0],)), self.catalog, self.handlers),
            (replace(self.graph, types=self.graph.types + (d.TypeDef('Dangling', d.Nullable(d.TypeRef('Missing'))),)), self.catalog, self.handlers),
            (self.graph, self.catalog, {}),
            (self.graph, replace(self.catalog, variants=self.catalog.variants[:-1]), self.handlers),
            (self.graph, replace(self.catalog, variants=self.catalog.variants + (replace(self.catalog.variants[0], code='E_API_EXTRA'),)), self.handlers),
        ]
        reasons = []
        for graph, catalog, handlers in cases:
            with self.assertRaises(self.r.RegistryCompileError) as caught:
                self.compile(graph, catalog, handlers)
            reasons.append(caught.exception.reason)
        self.assertEqual(len(set(reasons)), len(cases))
        self.assertEqual(self.compile().capabilities(), ('protocol.connect', 'protocol.schema'))

    def test_compile_refuses_malformed_descriptors_with_closed_reason(self):
        from dataclasses import replace
        for shape, rules in ((d.Record((object(),)), ()), (d.String('.*'), (object(),))):
            graph = replace(self.graph, types=self.graph.types + (d.TypeDef('Malformed', shape, rules),))
            with self.subTest(shape=type(shape).__name__):
                with self.assertRaises(self.r.RegistryCompileError) as caught:
                    self.compile(graph)
                self.assertIn(caught.exception.reason, self.r.COMPILE_REASONS)
        self.assertEqual(len(self.compile().error_arms()), 20)

    def test_compile_refuses_branch_defects(self):
        from dataclasses import replace
        v = self.catalog.variants[0]
        variants = [replace(v, branches=v.branches[:-1]),
                    replace(v, branches=v.branches + (v.branches[0],)),
                    replace(v, branches=(d.ErrorBranchDef('unknown_kind', v.branches[0].remedy),) + v.branches[1:]),
                    replace(v, branches=(replace(v.branches[0], remedy='unknown_remedy'),) + v.branches[1:]),
                    replace(v, api_class='wire')]
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(self.r.RegistryCompileError):
                    self.compile(catalog=replace(self.catalog, variants=(variant,) + self.catalog.variants[1:]))
        for bad_shape in [d.String('.*'), d.Record((d.FieldDef('kind', d.TypeRef('Digest')),))]:
            types = tuple(replace(t, shape=bad_shape) if t.name == v.cause_ref.name else t
                          for t in self.catalog.types)
            with self.assertRaises(self.r.RegistryCompileError):
                self.compile(catalog=replace(self.catalog, types=types))

    def test_compile_refuses_rule_scope_errors(self):
        from dataclasses import replace
        # Graph-only node: catalogue interning must not mask rule validation.
        ref = d.TypeRef('RuleScopeProbe')
        rules = [d.Utf8MaxBytes(ref, 256), d.CanonicalUInt64(ref),
                 d.CanonicalBase64(ref), d.SortedUnique(ref),
                 d.ProtocolWindowV1(ref), d.SchemaPayloadV1(ref),
                 d.NegotiatedSchemaV1(ref)]
        for rule in rules:
            graph = replace(self.graph, types=self.graph.types +
                            (d.TypeDef(ref.name, d.String('.*'), (rule,)),))
            with self.subTest(rule=rule.kind):
                with self.assertRaises(self.r.RegistryCompileError) as caught:
                    self.compile(graph=graph)
                self.assertEqual(caught.exception.reason, 'invalid_rule')
        wrong = replace(self.graph.operations[1], rules=(d.NegotiatedSchemaV1(ref),))
        with self.assertRaises(self.r.RegistryCompileError) as caught:
            self.compile(graph=replace(self.graph, operations=(self.graph.operations[0], wrong)))
        self.assertEqual(caught.exception.reason, 'invalid_rule')

    def test_synthetic_error_name_collisions_are_refused(self):
        from dataclasses import replace
        graph = replace(self.graph, types=self.graph.types +
                        (d.TypeDef('ApiErrorIncompatiblePinMismatch', d.Record(())),))
        with self.assertRaises(self.r.RegistryCompileError) as caught:
            self.compile(graph=graph)
        self.assertEqual(caught.exception.reason, 'invalid_error')

    def test_invalid_synthetic_error_names_are_refused(self):
        from relay_engine import errors
        from dataclasses import replace
        from unittest.mock import patch
        variant = next(v for v in self.catalog.variants if v.code == 'E_API_INCOMPATIBLE')
        cause = next(t for t in self.catalog.types if t.name == variant.cause_ref.name)
        leaf_ref = cause.shape.arms[0]
        leaf = next(t for t in self.catalog.types if t.name == leaf_ref.name)
        kind_ref = next(f.type_ref for f in leaf.shape.fields if f.name == 'kind')
        kind = next(t for t in self.catalog.types if t.name == kind_ref.name)
        original = kind.shape.values[0]
        # The last spelling keeps the record name in bounds but overflows its
        # generated Code/Class/Remedy type names.
        for invalid in ('pin-mismatch', 'pin.mismatch', 'x' * 110):
            catalog = replace(self.catalog,
                types=tuple(replace(t, shape=d.LiteralSet((invalid,))) if t == kind else t
                            for t in self.catalog.types),
                variants=tuple(replace(v, branches=tuple(
                    replace(branch, cause_kind=invalid) if branch.cause_kind == original else branch
                    for branch in v.branches)) if v == variant else v for v in self.catalog.variants))
            with self.subTest(kind=invalid), patch.object(errors, 'api_error_catalog', return_value=catalog):
                with self.assertRaises(self.r.RegistryCompileError) as caught:
                    self.compile(catalog=catalog)
                self.assertEqual(caught.exception.reason, 'invalid_error')

    def test_capabilities_are_exactly_two(self):
        registry = self.compile()
        self.assertEqual(registry.capabilities(), ('protocol.connect', 'protocol.schema'))
        self.assertIsNone(registry.operation('queue.submit'))
        self.assertEqual(registry.operation('protocol.schema').result_ref, d.TypeRef('SchemaResult'))
        with self.assertRaises(TypeError):
            registry.types['Other'] = d.TypeDef('Other', d.Record(()))

    def test_error_arms_are_complete_records(self):
        registry = self.compile()
        self.assertEqual(len(registry.error_arms()), 20)
        for arm in registry.error_arms():
            leaf = registry.types[arm.cause_type.name].shape
            kind = registry.types[leaf.fields[0].type_ref.name].shape.values[0]
            if kind == 'unknown_operation':
                self.assertEqual(arm.remedy, 'refresh_schema')
            if kind == 'pin_mismatch':
                self.assertEqual(arm.remedy, 'select_matching_candidate')

    def test_shared_core_nodes_are_interned_and_conflicts_refuse(self):
        from dataclasses import replace
        self.compile()  # Catalog carries the same producer's transitive core nodes.
        changed = tuple(replace(t, shape=d.String('.*')) if t.name == 'Digest' else t
                        for t in self.catalog.types)
        with self.assertRaises(self.r.RegistryCompileError):
            self.compile(catalog=replace(self.catalog, types=changed))


class TestValidate(unittest.TestCase):
    def setUp(self):
        from relay_engine import errors
        import relay_engine.api_registry as registry
        self.r = registry
        self.registry = registry.compile_registry(d.discovery_definitions(),
            {'protocol.connect': lambda args: None, 'protocol.schema': lambda args: None},
            (errors.api_error_catalog(),))

    def check(self, name, value, accepted=True):
        result = self.r.validate_type(self.registry, d.TypeRef(name), value)
        self.assertEqual(isinstance(result, self.r.ValidationFailure), not accepted,
                         (name, result))
        return result

    def test_validate_refuses_recursively(self):
        valid = {'supported': {'min': '3.0', 'max': '3.0'}}
        self.check('ConnectArgs', valid)
        for bad in [dict(valid, unexpected='x'), dict(valid, required_build_id=None),
                    {'supported': {'min': '3.0', 'max': '3.0', 'extra': 1}},
                    {'supported': []}]:
            self.check('ConnectArgs', bad, False)
        self.check('UInt64', 7, False)
        self.check('OpaqueId', '\ud800', False)
        self.check('ApiError', {'code': 'E_API_PROTOCOL', 'class': 'wire',
                               'cause': {'kind': 'unknown_operation'},
                               'remedy': 'refresh_schema'})
        self.check('ApiError', {'code': 'E_API_PROTOCOL', 'class': 'wire',
                               'cause': {'kind': 'unknown_operation'},
                               'remedy': 'resend_valid_request'}, False)

    def test_uint64_boundaries(self):
        for value in ['0', '1', '18446744073709551615']:
            self.check('UInt64', value)
        for value in ['18446744073709551616', '01', '+1', '1e3', '-1', '', True]:
            self.check('UInt64', value, False)
        self.check('RequestId', '18446744073709551615')
        self.check('RequestId', '0', False)

    def test_opaque_id_counts_utf8_bytes(self):
        self.check('OpaqueId', '\U0001f600' * 64)
        self.check('OpaqueId', '\U0001f600' * 64 + 'a', False)
        self.check('OpaqueId', '', False)
        self.check('OpaqueId', 'a\0b', False)

    def test_protocol_window_relation(self):
        for lo, hi in [('3.0', '3.0'), ('2.0', '2.9'), ('3.9', '3.10')]:
            self.check('ProtocolWindow', {'min': lo, 'max': hi})
        for lo, hi in [('3.1', '3.0'), ('2.0', '3.0')]:
            result = self.check('ProtocolWindow', {'min': lo, 'max': hi}, False)
            self.assertEqual(result.rule, 'protocol_window_v1')
        for value in ['03.0', '3.00', '65536.0', '3.0.0']:
            self.check('ProtocolVersion', value, False)
        self.check('ProtocolVersion', '65535.65535')

    def schema_reply(self):
        return {'protocol': '3.0', 'schema_digest': '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
                'schema_encoding': 'base64', 'schema_byte_count': '2', 'schema_bytes_base64': 'e30='}

    def test_schema_payload_relation(self):
        good = self.schema_reply()
        self.check('SchemaResult', good)
        for bad in [dict(good, schema_byte_count='1'), dict(good, schema_digest='0' * 64),
                    dict(good, schema_bytes_base64='e31=')]:
            self.check('SchemaResult', bad, False)

    def test_negotiated_schema_requires_context(self):
        good = self.schema_reply()
        ctx = self.r.DiscoveryValidationContext('schema_reply', '3.0', good['schema_digest'])
        for context in [None, self.r.DiscoveryValidationContext('wrong', '3.0', good['schema_digest']),
                        self.r.DiscoveryValidationContext('schema_reply', '3.1', good['schema_digest']),
                        self.r.DiscoveryValidationContext('schema_reply', '3.0', '0' * 64)]:
            result = self.r.validate_operation_result(self.registry, 'protocol.schema', good, context)
            self.assertIsInstance(result, self.r.ValidationFailure)
        result = self.r.validate_operation_result(self.registry, 'protocol.schema', good, ctx)
        self.assertIsInstance(result, self.r.ValidatedResult)
