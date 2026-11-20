"""Canonical discovery artifact and generated-consumer conformance controls."""
import base64
import ast
import contextlib
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_schema_digest_changed_and_check_clean(self):
        # The producer of the old pin is core discovery's committed manifest.
        old_digest = '9445baeef11240763072427eee4f9f629979f0a3cc03a6951428dc005784044a'
        artifact = self.export()
        self.assertNotEqual(artifact.digest, old_digest)
        self.assertEqual(len(json.loads(artifact.canonical_bytes)['$defs']['ApiError']['oneOf']), 44)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(load_generator().main(['--check']), 0)

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
        self.assertEqual(len(doc['$defs']['ApiError']['oneOf']), 44)
        for item in doc['$defs'].values():
            if item.get('type') == 'object':
                self.assertIs(item['additionalProperties'], False)
                self.assertIn('required', item)
        self.assertNotIn('required_build_id', doc['$defs']['ConnectArgs']['required'])
        self.assertEqual(doc['$defs']['ConnectArgs']['properties']['required_build_id'], {'$ref': '#/$defs/Digest'})

    def test_generated_queue_operation_metadata(self):
        from relay_engine.api_platform import build_platform
        compiled = build_platform().registry
        artifact = self.export(compiled)
        document = json.loads(artifact.canonical_bytes)
        expected = {
            'queue.raise': ('service', 'catalogue_write', 'operation_key', 'none'),
            'queue.withdraw': ('seat_session', 'catalogue_write', 'operation_key', 'none'),
            'queue.answer': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.dismiss': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.open': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.dismiss-all': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.get': ('unsigned', 'pure_observation', 'read_repeat', 'queue.get'),
            'queue.list': ('unsigned', 'pure_observation', 'read_repeat', 'queue.list'),
        }
        fields = ('principal', 'effect', 'retry', 'browser_get')
        rows = {row['name']: tuple(row[key] for key in fields)
                for row in document['x-adt']['operations'] if row['name'].startswith('queue.')}
        self.assertEqual(rows, expected)
        self.assertEqual({name for name in compiled.capabilities() if name.startswith('queue.')},
                         set(expected))
        path = TOOLS / 'relay_engine' / 'generated' / 'api'
        self.assertEqual((path / 'schema-3.0.json').read_bytes(), artifact.canonical_bytes)
        manifest = json.loads((path / 'manifest.json').read_bytes())
        self.assertEqual(manifest['schema_digest'], artifact.digest)
        self.assertEqual(sorted(name for name in manifest['operations'] if name.startswith('queue.')),
                         sorted(expected))
        for binding in ('api_3_0.rs', 'api_3_0.ts'):
            body = (path / binding).read_text()
            self.assertIn('"queue.dismiss-all"', body)
            self.assertNotIn('"queue.dismiss_all"', body)

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


TOOLS = Path(__file__).resolve().parents[2]


def load_generator():
    spec = importlib.util.spec_from_file_location('discovery_generator', TOOLS / 'generate-api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGenerator(unittest.TestCase):
    def test_typescript_ends_with_exactly_one_newline(self):
        from relay_engine import api_schema
        generator = load_generator()
        compiled = generator.build_registry()
        artifact = api_schema.export_schema(compiled, '3.0')
        output = generator.emit_typescript(compiled, artifact)
        self.assertTrue(output.endswith(b'\n'))
        self.assertFalse(output.endswith(b'\n\n'))

    def test_committed_schema_equals_export(self):
        from relay_engine import api_platform, api_schema
        generator = load_generator()
        compiled = generator.build_registry()
        self.assertEqual(dict(compiled.handlers), dict(api_platform.build_platform().registry.handlers))
        expected = api_schema.export_schema(compiled, '3.0').canonical_bytes
        with patch('builtins.open', side_effect=AssertionError('generator opened a file')):
            generated = generator.generate(TOOLS)
        key = 'tools/relay_engine/generated/api/schema-3.0.json'
        self.assertEqual(generated[key], expected)
        self.assertEqual((TOOLS.parent / key).read_bytes(), expected)
        self.assertFalse(expected.endswith(b'\n'))

    def test_binding_names_stay_stable_when_literals_and_catalog_grow(self):
        from relay_engine import api_schema
        generator = load_generator()
        original = generator.build_registry()
        catalogue = errors.api_error_catalog()
        variant = catalogue.variants[0]
        cause = next(t for t in catalogue.types if t.name == variant.cause_ref.name)
        kind = d.TypeDef('EarlierKind', d.LiteralSet(('aaa_earlier',)))
        leaf = d.TypeDef('EarlierCause', d.Record((d.FieldDef('kind', d.TypeRef(kind.name)),)))
        updated = replace(catalogue, types=tuple(replace(t, shape=replace(cause.shape,
            arms=cause.shape.arms + (d.TypeRef(leaf.name),))) if t == cause else t for t in catalogue.types) + (kind, leaf),
            variants=(replace(variant, branches=variant.branches +
                      (d.ErrorBranchDef('aaa_earlier', variant.branches[0].remedy),)),) + catalogue.variants[1:])
        with patch.object(errors, 'api_error_catalog', return_value=updated):
            grown = generator.build_registry()
        old_names = {ref.name for ref in original.types['ApiError'].shape.arms}
        self.assertTrue(old_names <= {ref.name for ref in grown.types['ApiError'].shape.arms})
        self.assertIn('ApiErrorIncompatiblePinMismatch', old_names)
        for compiled in (original, grown):
            artifact = api_schema.export_schema(compiled, '3.0')
            for emit in (generator.emit_rust, generator.emit_typescript):
                source = emit(compiled, artifact).decode()
                self.assertIn('ApiErrorIncompatiblePinMismatch', source)
                self.assertNotIn('__ApiError0', source)
        types = dict(original.types)
        node = types['SchemaEncoding']
        types[node.name] = replace(node, shape=d.LiteralSet(('aaa_earlier', 'base64')))
        before = generator._rust_types(original)
        after = generator._rust_types(replace(original, types=types))
        for source in (before, after):
            self.assertIn('Self::Base64=>text("base64")', source)
            self.assertIn('IncompatiblePinMismatch(ApiErrorIncompatiblePinMismatch)', source)

    def test_binding_name_collisions_are_refused(self):
        from relay_engine import api_schema
        generator = load_generator()
        compiled = generator.build_registry()
        for values in (('a-b', 'a_b'), ('self', 'ValueSelf')):
            types = dict(compiled.types, Collision=d.TypeDef('Collision', d.LiteralSet(values)))
            collision = replace(compiled, types=types)
            artifact = api_schema.export_schema(collision, '3.0')
            for emit in (generator.emit_rust, generator.emit_typescript):
                with self.subTest(language=emit.__name__, values=values):
                    with self.assertRaisesRegex(ValueError, '^binding_name_collision$'):
                        emit(collision, artifact)

    def test_rust_keyword_identifier_refused(self):
        from relay_engine import api_schema
        generator = load_generator()
        original = generator.build_registry()
        for field in ('type', 'match', 'self'):
            compiled = replace(original, types=dict(original.types,
                KeywordRecord=d.TypeDef('KeywordRecord', d.Record((
                    d.FieldDef(field, d.TypeRef('OpaqueId')),)))))
            artifact = api_schema.export_schema(compiled, '3.0')
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '^rust_reserved_identifier$'):
                generator.emit_rust(compiled, artifact)
        compiled = replace(original, types=dict(original.types,
            Self=d.TypeDef('Self', d.Record(()))))
        artifact = api_schema.export_schema(compiled, '3.0')
        with self.subTest(type_name='Self'), self.assertRaisesRegex(ValueError, '^rust_reserved_identifier$'):
            generator.emit_rust(compiled, artifact)
        compiled = replace(original, types=dict(original.types,
            KeywordRecord=d.TypeDef('KeywordRecord', d.Record((
                d.FieldDef('kind', d.TypeRef('OpaqueId')),)))))
        self.assertIn(b'pub kind: OpaqueId', generator.emit_rust(compiled,
            api_schema.export_schema(compiled, '3.0')))

    def test_runtime_constants_refuses_unknown_token(self):
        from relay_engine import api_wire
        generator = load_generator()
        with self.assertRaisesRegex(ValueError, '^unknown_template_token$'):
            generator._runtime_constants('a @NOT_A_TOKEN@ b')
        self.assertEqual(generator._runtime_constants('@MAX_API_FRAME@'), str(api_wire.MAX_API_FRAME))

    def test_templates_fill_in_one_pass(self):
        from relay_engine import api_schema
        generator = load_generator()
        original = generator.build_registry()
        compiled = replace(original, types=dict(original.types,
            Marker=d.TypeDef('Marker', d.LiteralSet(('@SCHEMA@', '@RESPONSE_ARMS@')))))
        artifact = api_schema.export_schema(compiled, '3.0')
        source = generator.emit_typescript(compiled, artifact).decode()
        def embedded(text):
            return text.split('const schema: unknown = ', 1)[1].split(';\nconst document = ', 1)[0].encode()
        self.assertEqual(embedded(source), artifact.canonical_bytes)
        rust = generator.emit_rust(compiled, artifact)
        for literal in (b'"@SCHEMA@"', b'"@RESPONSE_ARMS@"'):
            self.assertIn(literal, rust)
        variants, arms, _ = generator._response_bindings(compiled, 'ts')
        overwriting = generator._runtime_constants(generator.TYPESCRIPT_RUNTIME, {
            'SCHEMA': '@SCHEMA@', 'RESPONSE_VARIANTS': '@RESPONSE_VARIANTS@',
            'RESPONSE_ARMS': '@RESPONSE_ARMS@'}).replace(
            '@SCHEMA@', artifact.canonical_bytes.decode()).replace(
            '@RESPONSE_VARIANTS@', variants).replace('@RESPONSE_ARMS@', arms)
        self.assertNotEqual(embedded(overwriting), artifact.canonical_bytes)

    def test_leading_regex_flag_refused(self):
        generator = load_generator()
        for pattern in ('(?i)abc', '(?x)abc'):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, '^unsupported_regex_flags$'):
                generator._rust_regex(pattern)
        self.assertIn('Rx::Seq', generator._rust_regex('abc'))

    def test_runtime_placeholders_do_not_rewrite_graph_literals(self):
        from relay_engine import api_schema
        generator = load_generator()
        compiled = generator.build_registry()
        types = dict(compiled.types, Marker=d.TypeDef('Marker', d.LiteralSet(('@MAX_API_FRAME@',))))
        compiled = replace(compiled, types=types)
        artifact = api_schema.export_schema(compiled, '3.0')
        for emit in (generator.emit_rust, generator.emit_typescript):
            self.assertIn(b'"@MAX_API_FRAME@"', emit(compiled, artifact))

    def test_generated_runtime_bounds_follow_python_constants(self):
        from relay_engine import api_schema, api_wire
        generator = load_generator()
        compiled = generator.build_registry()
        artifact = api_schema.export_schema(compiled, '3.0')
        cases = [(api_wire, 'MAX_API_FRAME', 101, ('rs', 'ts')),
                 (d, 'MAX_SCHEMA_BYTES', 123, ('rs', 'ts')),
                 (d, 'MAX_SCHEMA_BASE64_CHARS', 164, ('rs', 'ts')),
                 (d, 'MAX_JSON_DEPTH', 7, ('rs', 'ts')),
                 (d, 'MAX_SAFE_JSON_INTEGER', 127, ('rs',)),
                 (d, 'MAX_UINT64', 999, ('ts',))]
        for owner, name, value, languages in cases:
            for language in languages:
                with self.subTest(constant=name, language=language):
                    emit = generator.emit_rust if language == 'rs' else generator.emit_typescript
                    original = emit(compiled, artifact)
                    with patch.object(owner, name, value):
                        self.assertNotEqual(emit(compiled, artifact), original)
        with patch.object(api_wire, 'MAX_API_FRAME', 1):
            with self.assertRaisesRegex(api_schema.SchemaExportError, 'schema_reply_too_large'):
                api_schema.export_schema(compiled, '3.0')

    def test_check_mode_detects_drift(self):
        generator = load_generator()
        with tempfile.TemporaryDirectory() as temp:
            tools = Path(temp) / 'tools'
            destination = tools / 'relay_engine/generated/api'
            shutil.copytree(TOOLS / 'relay_engine/generated/api', destination)
            with patch.object(generator, 'TOOLS_DIR', tools), contextlib.redirect_stdout(io.StringIO()) as output:
                before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in destination.iterdir()}
                self.assertEqual(generator.main(['--check']), 0)
                self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in destination.iterdir()}, before)
                file = destination / 'schema-3.0.json'
                original = file.read_bytes()
                file.write_bytes(b'!' + original[1:])
                changed = file.read_bytes()
                self.assertEqual(generator.main(['--check']), 1)
                self.assertIn('tools/relay_engine/generated/api/schema-3.0.json', output.getvalue())
                self.assertEqual(file.read_bytes(), changed)
                self.assertEqual(generator.main(['--write']), 0)
                self.assertEqual(file.read_bytes(), original)
                self.assertEqual(generator.main(['--check']), 0)

    def test_generator_import_surface(self):
        path = TOOLS / 'generate-api.py'
        tree = ast.parse(path.read_text())
        forbidden = {'daemon', 'cli', 'client', 'ledger', 'socket', 'sqlite3', 'submit', 'schema', 'queries'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {part for alias in node.names for part in alias.name.split('.')}
            elif isinstance(node, ast.ImportFrom):
                names = set((node.module or '').split('.')) | {alias.name for alias in node.names}
            else:
                continue
            self.assertFalse(names & forbidden)
        probe = '''import importlib.util,sys
from pathlib import Path
tools=Path(sys.argv[1]);sys.path.insert(0,str(tools))
spec=importlib.util.spec_from_file_location("producer",tools/"generate-api.py")
producer=importlib.util.module_from_spec(spec);spec.loader.exec_module(producer)
assert producer.generate(tools)
forbidden={"sqlite3","_sqlite3","socket"}|{"relay_engine."+x for x in ("daemon","cli","client","ledger","submit","schema","queries")}
assert not forbidden.intersection(sys.modules),sorted(forbidden.intersection(sys.modules))
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(TOOLS)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        control = probe.replace('assert not forbidden.intersection', 'import sqlite3\nassert not forbidden.intersection')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', control, str(TOOLS)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sqlite3', result.stderr)


TOOL_VERSIONS = {'rustc': 'rustc 1.99.1 (58cf8f9c3 2026-10-13)', 'node': 'v26.13.0', 'tsc': '7.0.2'}


def toolchain():
    root = Path(os.environ.get('ADT_TOOLCHAINS', Path.home() / '.local/share/adt-toolchains'))
    return {'rustc': [str(root / 'rust-1.99.1/bin/rustc')],
            'node': [str(root / 'node-v26.13.0/bin/node')],
            'tsc': [str(root / 'node-v26.13.0/bin/node'),
                    str(root / 'typescript-7.0.2/node_modules/typescript/bin/tsc')]}


def exact_version(tool, banner):
    if tool == 'tsc':
        match = re.fullmatch(r'Version (\d+\.\d+\.\d+)', banner.strip())
        return bool(match and match[1] == TOOL_VERSIONS[tool])
    return banner.strip() == TOOL_VERSIONS[tool]


class TestToolchain(unittest.TestCase):
    def test_dockerfile_asserts_every_tool_version(self):
        text = (TOOLS / 'release-check.Dockerfile').read_text()
        commands = {
            'rustc': '"$ADT_TOOLCHAINS/rust-1.99.1/bin/rustc" --version',
            'node': '"$ADT_TOOLCHAINS/node-v26.13.0/bin/node" --version',
            'tsc': '"$ADT_TOOLCHAINS/node-v26.13.0/bin/node" "$ADT_TOOLCHAINS/typescript-7.0.2/node_modules/typescript/bin/tsc" --version',
        }
        pattern = r"""test "\$\((.*?)\)" = '([^']+)'"""
        def check(source):
            assertions = re.findall(pattern, source)
            for name, command in commands.items():
                banners = [banner for actual, banner in assertions if actual == command]
                self.assertEqual(len(banners), 1, name)
                self.assertTrue(exact_version(name, banners[0]), name)
        check(text)
        without_tsc = '\n'.join(line for line in text.splitlines()
                                if commands['tsc'] not in line)
        with self.assertRaisesRegex(AssertionError, 'tsc'):
            check(without_tsc)

    def test_pinned_toolchain_present(self):
        for name, command in toolchain().items():
            with self.subTest(tool=name):
                try:
                    result = subprocess.run(command + ['--version'], capture_output=True, text=True, timeout=30)
                except OSError as error:
                    self.fail(name + ': ' + type(error).__name__)
                self.assertEqual(result.returncode, 0, name + ': ' + result.stderr)
                self.assertTrue(exact_version(name, result.stdout), name + ': ' + result.stdout)

    def test_version_comparison_is_exact(self):
        good = [TOOL_VERSIONS['rustc'], TOOL_VERSIONS['node'], 'Version 7.0.2']
        bad = ['rustc 1.99.10 (58cf8f9c3 2026-10-13)', 'v26.13.01', 'Version 7.0.20']
        for name, yes, no in zip(('rustc', 'node', 'tsc'), good, bad):
            self.assertTrue(exact_version(name, yes))
            self.assertFalse(exact_version(name, no))
            self.assertFalse(exact_version(name, 'unparseable'))


def consumer_scenarios():
    from relay_engine import api_protocol as p, api_schema, api_wire as w
    compiled = load_generator().build_registry()
    artifact = api_schema.export_schema(compiled, '3.0')
    runtime = p.RuntimeContext('consumer-store', 'isolated', 'consumer-runtime', '3.0.0',
                               'a' * 64, True, {'3.0': artifact})
    args = {'supported': {'min': '3.0', 'max': '3.0'},
            'required_build_id': runtime.build_id, 'required_schema_digest': artifact.digest}
    connected = p.dispatch_discovery(p.initial_connection_state(), w.ApiRequest('1', 'protocol.connect', args), runtime, compiled)
    schema = p.dispatch_discovery(connected.state, w.ApiRequest('2', 'protocol.schema', {}), runtime, compiled)
    cases = []
    def frame(value):
        raw = jcs.jcs_encode(value)
        return (len(raw).to_bytes(4, 'big') + raw).hex()
    def add(scenario, expected):
        cases.append((scenario, expected))
    for op, request_id, body in [('protocol.connect', '1', args), ('protocol.schema', '2', {})]:
        expected = {'request_id': request_id, 'op': op, 'args': body}
        encoded = frame(expected)
        parsed = w.decode_api_frame(bytes.fromhex(encoded)[4:], compiled)
        self_expected = w.ApiRequest(request_id, op, body)
        assert parsed == self_expected
        add({'action': 'encode', 'operation': op, 'id': request_id, 'value': body}, {'hex': encoded})
    body = {'draft_base64': base64.b64encode(b'draft').decode(), 'submission_key': 'consumer-key'}
    add({'action': 'encode', 'operation': 'relay.submit', 'id': '3', 'value': body},
        {'hex': frame({'request_id': '3', 'op': 'relay.submit', 'args': body})})
    connect_frame = frame(connected.response)
    def decode(response, operation='protocol.schema', outstanding='2', context=connect_frame, valid=True):
        case = {'action': 'decode', 'operation': operation, 'id': outstanding,
                'frame': frame(response), 'connect': context}
        if not valid:
            expected = {'unavailable': True}
        elif 'error' in response:
            expected = {'error': response['error']}
        elif operation == 'protocol.schema':
            expected = {'result': {k: response['result'][k] for k in ('protocol', 'schema_digest', 'schema_byte_count')}}
        else:
            expected = {'result': response['result']}
        add(case, expected)
    decode(connected.response, 'protocol.connect', '1', '')
    decode(schema.response)
    decode({'request_id': '3', 'result': {'relay_id': 'relay', 'handle': '1', 'created_at': '2'}},
           'relay.submit', '3')
    item_id = '00000000-0000-7000-8000-000000000001'
    action = dict(item_id=item_id, expected_revision='1', operation_key='act')
    subject = dict(kind='doorbell_off', seat_id='s-a', generation='7')
    action_result = dict(item_id=item_id, revision='2', state='dismissed',
                         receipt='receipt', replayed='false')
    read_result = dict(store_id='store', observed_revision='2',
                       queue_event_position='2', continuation=None)
    item = dict(item_id=item_id, revision='2', state='dismissed', kind='notice',
                subkind='doorbell_off', project_id='p', raiser='engine_liveness',
                asking_seat_id=None, actor_address=None, fields=None, answer_to=None,
                source_handle=None, source_relay_id=None, texts='{"sentence":"Doorbell off"}',
                popup_text=None, choices=None, choices_reason=None,
                pins='{"generation":7,"seat_id":"s-a"}',
                condition_key='{"generation":7,"seat_id":"s-a"}',
                raised_position='1', raised_at_ms='10', events_position='2')
    queue_rows = (
        ('queue.answer', dict(action, answer={'text': 'Yes'}),
         dict(action_result, state='applying', answer_id='answer')),
        ('queue.dismiss', action, action_result),
        ('queue.dismiss-all', dict(scope='machine', up_to_event_position='1', operation_key='all'),
         dict(count='1', item_ids=[item_id], more_remaining='false',
              receipt='receipt', replayed='false')),
        ('queue.get', dict(item_id=item_id, history_page_size='100', history_continuation=None),
         dict(read_result, item=dict(item, answer=None, job=None, outcome=None), events=[])),
        ('queue.list', dict(scope='machine', state_filter=None, kind_filter=None, page_size='100',
                            continuation=None), dict(read_result, items=[item])),
        ('queue.open', action, action_result),
        ('queue.raise', dict(kind='notice', subkind='doorbell_off', project_id='p',
                            texts={'sentence': 'Doorbell off'}, pins=subject,
                            condition_key=subject, actor_address=None, operation_key='raise'),
         dict(item_id=item_id, revision='1', state='pending', receipt='original')),
        ('queue.withdraw', dict(action, reason_text=None), dict(action_result, state='withdrawn')),
    )
    for op, args, result in queue_rows:
        request = frame({'request_id': '4', 'op': op, 'args': args})
        response = {'request_id': '4', 'result': result}
        spec = compiled.operation(op)
        add({'action': 'encode_checked', 'operation': op, 'id': '4', 'value': args},
            {'hex': request})
        add({'action': 'decode_request', 'operation': op, 'id': '4',
             'type': spec.request_ref.name, 'frame': request}, {'args': args})
        add({'action': 'encode_result', 'id': '4', 'type': spec.result_ref.name, 'value': result},
            {'hex': frame(response)})
        decode(response, op, '4')

    for arm in compiled.arms:
        cause = compiled.types[arm.cause_type.name].shape
        kind_ref = next(f.type_ref for f in cause.fields if f.name == 'kind')
        kind = compiled.types[kind_ref.name].shape.values[0]
        code, api_class, remedy = arm.code, arm.wire_class, arm.remedy
        payload = {}
        if kind == 'protocol_window':
            payload = {'requested': {'min': '2.0', 'max': '2.9'}, 'supported': {'min': '3.0', 'max': '3.0'}}
        elif kind == 'pin_mismatch':
            payload = {'field': 'build_id', 'required': 'f' * 64, 'actual': 'a' * 64}
        elif kind == 'revision_mismatch':
            payload = {'current_revision': '1', 'current_state': 'pending'}
        if api_class == 'admission':
            payload = {'engine_code': 'E-ADDRESS'}
        error = p.api_error(compiled, kind, **payload)
        assert (error['code'], error['class'], error['remedy']) == (code, api_class, remedy)
        decode({'request_id': '2', 'error': error})
        wrong = json.loads(json.dumps(error));wrong['remedy'] = 'unknown_remedy'
        decode({'request_id': '2', 'error': wrong}, valid=False)
    for field, bad in [('schema_byte_count', str(artifact.byte_count + 1)), ('schema_digest', '0' * 64)]:
        wrong = json.loads(json.dumps(schema.response));wrong['result'][field] = bad
        decode(wrong, valid=False)
    for field, bad in [('protocol', '2.0'), ('schema_digest', '0' * 64)]:
        wrong = json.loads(json.dumps(connected.response));wrong['result'][field] = bad
        decode(schema.response, context=frame(wrong), valid=False)
    decode(schema.response, context='', valid=False)
    decode(schema.response, outstanding='3', valid=False)
    error = p.api_error(compiled, 'handshake_required')
    for field, bad in [('class', 'version'), ('remedy', 'resend_valid_request')]:
        wrong = dict(error, **{field: bad});decode({'request_id': '2', 'error': wrong}, valid=False)
    wrong = json.loads(json.dumps(error));wrong['cause']['kind'] = 'unknown_kind'
    decode({'request_id': '2', 'error': wrong}, valid=False)
    values = [('UInt64', x) for x in ('0', '18446744073709551615', '18446744073709551616', '01', '-1', '1\n')]
    values += [('RequestId', x) for x in ('1', '0', '18446744073709551615', '18446744073709551616', '01', '+1', '1e3')]
    values += [('OpaqueId', x) for x in ('x', 'é' * 128, 'é' * 129, '😀' * 64, '😀' * 64 + 'a', '', 'a\x00b')]
    values += [('ProtocolWindow', x) for x in ({'min': '3.0', 'max': '3.1'}, {'min': '3.1', 'max': '3.0'}, {'min': '2.0', 'max': '3.0'})]
    values += [('CanonicalSchemaBase64', x) for x in ('e30=', 'e31=', 'e30', 'e30=\n', '-w==')]
    for name, value in values:
        expected = not isinstance(r.validate_type(compiled, d.TypeRef(name), value), r.ValidationFailure)
        add({'action': 'validate', 'type': name, 'value': value}, {'valid': expected})
    for raw in ['{"x":"a","\\u0078":"b"}', '{"x":1}', '{"x":1.0}', '{"x":true}', '"\\ud800"', '"\\ud83d\\ude00"', '{} {}', '\ufeff{}']:
        try:
            w.strict_json_loads(raw.encode());valid = True
        except w.StrictJsonError:
            valid = False
        add({'action': 'parse', 'text': raw}, {'valid': valid})
    known = json.loads(artifact.canonical_bytes)
    add({'action': 'schema', 'schema': json.dumps(known)}, {'valid': True})
    duplicate = json.loads(json.dumps(known));duplicate['x-adt']['rules'][-1] = duplicate['x-adt']['rules'][0]
    add({'action': 'schema', 'schema': json.dumps(duplicate)}, {'valid': False})
    known['x-adt']['rules'].append({'kind': 'future_required_kind'})
    add({'action': 'schema', 'schema': json.dumps(known)}, {'valid': False})
    # All payload and context checks pass; only the decode-path rule guard can refuse.
    altered = jcs.jcs_encode(known)
    digest = hashlib.sha256(altered).hexdigest()
    altered_connect = json.loads(json.dumps(connected.response))
    altered_connect['result']['schema_digest'] = digest
    altered_schema = json.loads(json.dumps(schema.response))
    altered_schema['result'].update(schema_digest=digest, schema_byte_count=str(len(altered)),
                                   schema_bytes_base64=base64.b64encode(altered).decode())
    decode(altered_schema, context=frame(altered_connect), valid=False)
    cases[-1][0]['control'] = 'unknown_rule_decode'
    return cases


def null_id_consumer_scenarios():
    from relay_engine import api_protocol as p, api_schema, api_wire as w
    compiled = load_generator().build_registry()
    artifact = api_schema.export_schema(compiled, '3.0')
    runtime = p.RuntimeContext('null-id-store', 'isolated', 'null-id-runtime', '3.0.0',
                               'a' * 64, True, {'3.0': artifact})
    def frame(value):
        raw = jcs.jcs_encode(value)
        return (len(raw).to_bytes(4, 'big') + raw).hex()
    def scenario(response):
        return {'action': 'decode', 'operation': 'protocol.connect', 'id': '7',
                'connect': '', 'frame': frame(response)}
    decoder = w.ApiFrameDecoder()
    try:
        decoder.feed((w.MAX_API_FRAME + 1).to_bytes(4, 'big'))
    except w.ApiFrameError:
        oversized = w.ProtocolRefusal(None, 'malformed_frame', True)
    else:
        raise AssertionError('oversized prefix was accepted')
    cases = []
    for refusal in (w.decode_api_frame(b'{', compiled), oversized):
        answer = p.dispatch_discovery(p.initial_connection_state(), refusal, runtime, compiled).response
        assert answer['request_id'] is None
        cases.append((scenario(answer), {'connection_error': answer['error']}))
        invalid = dict(answer['error'])
        del invalid['remedy']
        cases.append((scenario({'request_id': None, 'error': invalid}), {'unavailable': True}))
        cases.append((scenario(dict(answer, request_id='8')), {'unavailable': True}))
    connected = p.dispatch_discovery(p.initial_connection_state(), w.ApiRequest('1',
        'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}), runtime, compiled).response
    cases.append((scenario(dict(connected, request_id=None)), {'unavailable': True}))
    return cases


class TestGeneratedConsumers(unittest.TestCase):
    def test_null_request_id_failure_decodes_typed(self):
        cases = null_id_consumer_scenarios()
        self.assertEqual([answer['connection_error']['cause']['kind']
                          for _, answer in cases if 'connection_error' in answer],
                         ['malformed_json', 'malformed_frame'])
        for language in ('rs', 'ts'):
            with self.subTest(language=language):
                self.run_consumer(language, cases=cases, controls=False)

    def test_schema_export_carries_max_decoded(self):
        from relay_engine import api_schema
        from relay_engine.tests.test_api_registry import TestSliceBRules
        compiled = TestSliceBRules().compile()
        rules = json.loads(api_schema.export_schema(compiled, '3.0').canonical_bytes)['x-adt']['rules']
        actual = {rule['type_ref']: rule['max_decoded'] for rule in rules
                  if rule['kind'] == 'canonical_base64'}
        self.assertEqual(actual, {'CanonicalSchemaBase64': 8388608, 'DraftBase64': 131072})

    def test_emitters_enforce_per_type_decoded_bound(self):
        from relay_engine import api_schema
        from relay_engine.tests.test_api_registry import TestSliceBRules
        compiled = TestSliceBRules().compile()
        artifact = api_schema.export_schema(compiled, '3.0')
        generator = load_generator()
        vectors = [('DraftBase64', n, n in (1, 131072)) for n in (0, 1, 131072, 131073)]
        vectors += [('CanonicalSchemaBase64', n, n <= 8388608) for n in (1, 8388608, 8388609)]
        cases = [({'action': 'validate_base64_length', 'type': name, 'length': str(n)},
                  {'valid': valid}) for name, n, valid in vectors]
        for language, emit in [('rs', generator.emit_rust), ('ts', generator.emit_typescript)]:
            with self.subTest(language=language):
                self.run_consumer(language, cases=cases, binding=emit(compiled, artifact), controls=False)

    def test_emitter_refuses_unknown_rule_descriptor(self):
        from relay_engine import api_schema
        from relay_engine.tests.test_api_registry import TestSliceBRules
        compiled = TestSliceBRules().compile()
        artifact = api_schema.export_schema(compiled, '3.0')
        generator = load_generator()
        for field in ('unknown_kind', 'unknown_field'):
            document = json.loads(artifact.canonical_bytes)
            rule = document['x-adt']['rules'][0]
            if field == 'unknown_kind': rule['kind'] = 'future_required_rule'
            else: rule['future_field'] = 'unknown'
            altered = replace(artifact, canonical_bytes=jcs.jcs_encode(document))
            for emit in (generator.emit_rust, generator.emit_typescript):
                with self.subTest(field=field, emitter=emit.__name__), self.assertRaises(ValueError):
                    emit(compiled, altered)

    def test_session_request_and_result_consumer_vectors(self):
        from relay_engine import api_platform, api_protocol as p, api_schema, api_wire as w
        compiled = api_platform.build_platform().registry
        artifact = api_schema.export_schema(compiled, '3.0')
        runtime = p.RuntimeContext('store', 'isolated', 'runtime', '3.0.0', 'a' * 64,
                                   True, {'3.0': artifact})
        connected = p.dispatch_discovery(p.initial_connection_state(), w.ApiRequest('1',
            'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}), runtime, compiled)
        def frame(value):
            raw = jcs.jcs_encode(value)
            return (len(raw).to_bytes(4, 'big') + raw).hex()
        key = {'key_id': '00000000-0000-7000-8000-000000000001', 'key_secret': 'A' * 43}
        common = dict(project_id='p', team_instance_id='t', seat_id='s', occupant_id='o',
                      session_id='session', generation='1', signed_in_at='2000')
        rows = [
            ('seat.identify', 'SeatIdentifyArgs', 'SeatIdentifyResult', dict(key),
             dict(seat_id='s', seat_address='t.s', project_id='p')),
            ('session.sign-in', 'SessionSignInArgs', 'SessionSignInResult',
             dict(key, host='codex', host_session_id='host', operation_key='op'),
             dict(common, receipt='original', session_state='open')),
            ('session.resume', 'SessionResumeArgs', 'SessionResumeResult',
             dict(key, session_id='session'), common),
            ('session.end', 'SessionEndArgs', 'SessionEndResult',
             dict(key, session_id='session', operation_key='op'),
             dict(session_id='session', ended_at='2001', receipt='original')),
        ]
        cases = []
        for operation, args_type, result_type, args, result in rows:
            bad_args, bad_result = dict(args, extra='no'), dict(result, extra='no')
            cases.extend([
                ({'action': 'encode_checked', 'id': '2', 'operation': operation, 'value': args},
                 {'hex': frame({'request_id': '2', 'op': operation, 'args': args})}),
                ({'action': 'encode_checked', 'id': '2', 'operation': operation, 'value': bad_args},
                 {'invalid': True}),
                ({'action': 'decode_request', 'id': '2', 'operation': operation, 'type': args_type,
                  'frame': frame({'request_id': '2', 'op': operation, 'args': args})}, {'args': args}),
                ({'action': 'decode_request', 'id': '2', 'operation': operation, 'type': args_type,
                  'frame': frame({'request_id': '2', 'op': operation, 'args': bad_args})}, {'unavailable': True}),
                ({'action': 'encode_result', 'id': '2', 'type': result_type, 'value': result},
                 {'hex': frame({'request_id': '2', 'result': result})}),
                ({'action': 'encode_result', 'id': '2', 'type': result_type, 'value': bad_result},
                 {'invalid': True}),
                ({'action': 'decode', 'id': '2', 'operation': operation,
                  'connect': frame(connected.response),
                  'frame': frame({'request_id': '2', 'result': result})}, {'result': result}),
                ({'action': 'decode', 'id': '2', 'operation': operation,
                  'connect': frame(connected.response),
                  'frame': frame({'request_id': '2', 'result': bad_result})}, {'unavailable': True}),
            ])
        for language in ('rs', 'ts'):
            self.run_consumer(language, cases=cases, controls=False)
            # Each vector is beside its negative; wrong correlation must discriminate.
            for offset in range(0, len(rows) * 8, 8):
                control = [(cases[offset][0], {'hex': frame({'request_id': '3', 'op':
                            rows[offset // 8][0], 'args': rows[offset // 8][3]})})]
                with self.assertRaises(AssertionError):
                    self.run_consumer(language, cases=control, controls=False)

    def test_auth_and_conflict_consumer_vectors(self):
        rows = [('key_rejected', 'check_key_file'), ('key_replaced', 'obtain_current_key'),
                ('seat_stood_down', 'ask_top_seat'), ('session_not_current', 'sign_in_again'),
                ('not_signed_in', 'sign_in_first'), ('already_bound', 'open_new_connection'),
                ('operation_key_reused', 'use_new_operation_key')]
        cases = []
        def frame(value):
            raw = jcs.jcs_encode(value)
            return (len(raw).to_bytes(4, 'big') + raw).hex()
        for kind, remedy in rows:
            conflict = kind == 'operation_key_reused'
            error = {'code': 'E_API_CONFLICT' if conflict else 'E_API_AUTH',
                     'class': 'conflict' if conflict else 'auth',
                     'cause': {'kind': kind}, 'remedy': remedy}
            envelope = {'request_id': '1', 'error': error}
            bad = dict(error, remedy='unknown_remedy')
            cases.extend([
                ({'action': 'encode_error', 'id': '1', 'value': error}, {'hex': frame(envelope)}),
                ({'action': 'encode_error', 'id': '1', 'value': bad}, {'invalid': True}),
                ({'action': 'decode', 'id': '1', 'operation': 'protocol.connect', 'connect': '',
                  'frame': frame(envelope)}, {'error': error}),
                ({'action': 'decode', 'id': '1', 'operation': 'protocol.connect', 'connect': '',
                  'frame': frame({'request_id': '1', 'error': bad})}, {'unavailable': True}),
            ])
        for language in ('rs', 'ts'):
            with self.subTest(language=language):
                # Every positive differs from the negative beside it. Compare a
                # deliberately wrong expectation too, so passthrough cannot pass.
                self.run_consumer(language, cases=cases, controls=False)
                control = list(cases)
                control[0] = (cases[0][0], {'hex': frame({'request_id': '2', 'error':
                                                       cases[0][0]['value']})})
                with self.assertRaises(AssertionError):
                    self.run_consumer(language, cases=control, controls=False)

    def test_manifest_matches_artifacts(self):
        from relay_engine import api_schema
        root = TOOLS / 'relay_engine/generated/api'
        artifact = api_schema.export_schema(load_generator().build_registry(), '3.0')
        manifest = json.loads((root / 'manifest.json').read_bytes())
        self.assertEqual(manifest['schema_digest'], artifact.digest)
        self.assertEqual(manifest['schema_byte_count'], str(artifact.byte_count))
        self.assertEqual(manifest['protocol'], '3.0')
        self.assertEqual(manifest['operations'], [
            'protocol.connect', 'protocol.schema', 'queue.answer', 'queue.dismiss',
            'queue.dismiss-all', 'queue.get', 'queue.list', 'queue.open', 'queue.raise',
            'queue.withdraw', 'relay.submit', 'seat.identify', 'session.end',
            'session.resume', 'session.sign-in',
        ])
        self.assertEqual(set(manifest['bindings']), {'api_3_0.rs', 'api_3_0.ts'})
        for name, digest in manifest['bindings'].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)

    def run_consumer(self, language, only_schema=False, cases=None, binding=None, controls=True):
        cases = consumer_scenarios() if cases is None else cases
        if only_schema:
            cases = [case for case in cases if case[0]['action'] == 'schema' or case[0].get('control') == 'unknown_rule_decode']
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp);scenario_file = folder / 'scenarios.jsonl'
            scenario_file.write_bytes(b'\n'.join(jcs.jcs_encode(s) for s, _ in cases) + b'\n')
            consumer = TOOLS / 'relay_engine/tests/api_consumers' / ('consumer.' + language)
            if binding is not None:
                generated = folder / ('api_3_0.' + language)
                generated.write_bytes(binding)
                copied = folder / ('consumer.' + language)
                copied.write_text(consumer.read_text().replace('../../generated/api/api_3_0.' + language, str(generated)))
                consumer = copied
            tools = toolchain()
            if language == 'rs':
                binary = folder / 'consumer'
                command = tools['rustc'] + ['--edition', '2021', '-D', 'warnings', str(consumer), '-o', str(binary)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                command = [str(binary), str(scenario_file)]
            else:
                command = tools['tsc'] + ['--strict', '--noEmit', '--target', 'es2022', '--module', 'nodenext', '--allowImportingTsExtensions', str(consumer)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                command = tools['node'] + [str(consumer), str(scenario_file)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines();expected = [jcs.jcs_encode(value).decode() for _, value in cases]
            self.assertEqual(lines, expected)
            if controls and not only_schema and any(s['action'] == 'encode' for s, _ in cases):
                for action, field in [('encode', 'hex'), ('decode', 'result')]:
                    index = next(i for i, (s, e) in enumerate(cases) if s['action'] == action and field in e)
                    changed = expected.copy();wrong = json.loads(expected[index])
                    if action == 'encode':
                        raw = bytes.fromhex(wrong['hex']); body = json.loads(raw[4:]);body['op'] = 'protocol.changed'
                        raw = jcs.jcs_encode(body);wrong['hex'] = (len(raw).to_bytes(4, 'big') + raw).hex()
                    else:
                        wrong['result']['schema_digest'] = '0' * 64
                    changed[index] = jcs.jcs_encode(wrong).decode()
                    with self.assertRaises(AssertionError):
                        self.assertEqual(lines, changed)
                changed = expected.copy();index = next(i for i, (s, _) in enumerate(cases) if s.get('id') == '2' and s['action'] == 'decode')
                wrong = json.loads(expected[index]);wrong['request_id'] = '3'
                changed[index] = jcs.jcs_encode(wrong).decode()
                with self.assertRaises(AssertionError):
                    self.assertEqual(lines, changed)

    def test_self_literal_compiles_in_both_bindings(self):
        from relay_engine import api_schema
        generator = load_generator()
        compiled = generator.build_registry()
        compiled = replace(compiled, types=dict(compiled.types,
            SelfLiteral=d.TypeDef('SelfLiteral', d.LiteralSet(('self',)))))
        artifact = api_schema.export_schema(compiled, '3.0')
        cases = [({'action': 'validate', 'type': 'SelfLiteral', 'value': 'self'}, {'valid': True}),
                 ({'action': 'validate', 'type': 'SelfLiteral', 'value': 'ValueSelf'}, {'valid': False})]
        for language, emit in [('rs', generator.emit_rust), ('ts', generator.emit_typescript)]:
            with self.subTest(language=language):
                binding = emit(compiled, artifact)
                self.run_consumer(language, cases=cases, binding=binding)
                self.assertTrue(b'ValueSelf' in binding, 'reserved literal must use ValueSelf')

    def test_third_operation_round_trips_in_both_bindings(self):
        from relay_engine import api_protocol as p, api_schema
        generator = load_generator()
        graph = d.discovery_definitions()
        op = replace(graph.operations[1], name='test.echo',
                     request_ref=d.TypeRef('EchoArgs'), result_ref=d.TypeRef('EchoResult'), rules=())
        graph = replace(graph, types=graph.types + (
            d.TypeDef('EchoArgs', d.Record((d.FieldDef('message', d.TypeRef('OpaqueId')),))),
            d.TypeDef('EchoResult', d.Record((d.FieldDef('reply', d.TypeRef('Digest')),)))),
            operations=graph.operations + (op,))
        compiled = r.compile_registry(graph, dict(p.discovery_handlers(), **{'test.echo': lambda args: None}),
                                      (errors.api_error_catalog(),))
        artifact = api_schema.export_schema(compiled, '3.0')
        def frame(value):
            raw = jcs.jcs_encode(value)
            return (len(raw).to_bytes(4, 'big') + raw).hex()
        error = p.api_error(compiled, 'unknown_operation')
        cases = [({'action': 'encode', 'operation': op.name, 'id': '3', 'value': {'message': 'hello'}},
                  {'hex': frame({'request_id': '3', 'op': op.name, 'args': {'message': 'hello'}})}),
                 ({'action': 'decode', 'operation': op.name, 'id': '3', 'connect': '',
                   'frame': frame({'request_id': '3', 'result': {'reply': 'b' * 64}})}, {'result': {'reply': 'b' * 64}}),
                 ({'action': 'decode', 'operation': op.name, 'id': '3', 'connect': '',
                   'frame': frame({'request_id': '3', 'error': error})}, {'error': error})]
        for language, emit in [('rs', generator.emit_rust), ('ts', generator.emit_typescript)]:
            with self.subTest(language=language):
                self.run_consumer(language, cases=cases, binding=emit(compiled, artifact), controls=False)

    def test_generated_base64_size_contract(self):
        compiled = load_generator().build_registry()
        values = ['A' * 8388608,
                  base64.b64encode(b'\x00' * 8388608).decode(),
                  base64.b64encode(b'\x00' * 8388609).decode(),
                  'A' * 8388606 + 'B=']
        cases = []
        for value in values:
            valid = not isinstance(r.validate_type(compiled, d.TypeRef('CanonicalSchemaBase64'), value), r.ValidationFailure)
            cases.append(({'action': 'validate', 'type': 'CanonicalSchemaBase64', 'value': value}, {'valid': valid}))
        self.assertEqual([expected['valid'] for _, expected in cases], [True, True, False, False])
        self.run_consumer('ts', cases=cases)
        self.run_consumer('rs', cases=cases)

    def test_rust_parser_large_valid_string(self):
        cases = [({'action': 'parse', 'text': json.dumps('x' * 1048576)}, {'valid': True})]
        self.run_consumer('rs', cases=cases)

    def test_rust_consumer_compiles_and_agrees(self):
        self.run_consumer('rs')

    def test_typescript_consumer_typechecks_and_agrees(self):
        self.run_consumer('ts')

    def test_bindings_refuse_unknown_rule_kind(self):
        self.run_consumer('rs', only_schema=True)
        self.run_consumer('ts', only_schema=True)


if __name__ == '__main__':
    unittest.main()
