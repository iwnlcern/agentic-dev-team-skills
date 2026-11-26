"""Composition preserves component ownership without runtime dependencies."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import json
import subprocess
import sys
import unittest

from relay_engine import api_compose as c, api_definitions as d

TOOLS = Path(__file__).resolve().parents[2]


def handler(*args):
    return None


def factory(context):
    return object()


def component(owner):
    args = d.TypeDef(owner + 'Args', d.Record(()))
    result = d.TypeDef(owner + 'Result', d.Record(()))
    operation = d.OperationDef(owner.lower() + '.read', d.TypeRef(args.name),
        d.TypeRef(result.name), d.TypeRef('ApiError'), 'unsigned',
        'pure_observation', 'read_repeat', 'none')
    graph = d.DefinitionGraph((args, result), (operation,), d.TypeRef('ApiError'))
    return c.ApiComponent(owner, graph, {operation.name: handler}, factory)


class TestComposition(unittest.TestCase):
    def test_compose_merges_two_components_in_order(self):
        a, b = component('Alpha'), component('Beta')
        for components in ((a, b), (b, a)):
            with self.subTest(order=tuple(x.owner for x in components)):
                merged = c.compose_components(components)
                self.assertEqual(merged.definitions.types,
                                 components[0].definitions.types + components[1].definitions.types)
                self.assertEqual(merged.definitions.operations,
                                 components[0].definitions.operations + components[1].definitions.operations)
                self.assertEqual(dict(merged.handlers), {'alpha.read': handler, 'beta.read': handler})
                self.assertEqual(dict(merged.owners), {'alpha.read': 'Alpha', 'beta.read': 'Beta'})
                self.assertEqual(dict(merged.port_factories), {'Alpha': factory, 'Beta': factory})
                self.assertEqual(merged.definitions.error_ref, d.TypeRef('ApiError'))

    def test_owners_map_every_operation(self):
        a, b = component('Alpha'), component('Beta')
        for components in ((a, b), (b, a)):
            merged = c.compose_components(components)
            self.assertEqual(set(merged.owners), {op.name for op in merged.definitions.operations})
            for item in components:
                for op in item.definitions.operations:
                    self.assertEqual(merged.owners[op.name], item.owner)

    def test_each_refusal_reason(self):
        a, b = component('Alpha'), component('Beta')
        cases = [
            ('duplicate_owner', (a, replace(b, owner=a.owner))),
            ('duplicate_type', (a, replace(b, definitions=replace(b.definitions,
                types=b.definitions.types + (a.definitions.types[0],))))),
            ('duplicate_operation', (a, replace(b, definitions=replace(b.definitions,
                operations=a.definitions.operations), handlers=a.handlers))),
            ('undefined_handler', (a, replace(b, handlers={'alpha.read': handler}))),
            ('missing_port_factory', (replace(a, port_factory=None),)),
        ]
        for reason, components in cases:
            with self.subTest(reason=reason), self.assertRaises(c.CompositionError) as caught:
                c.compose_components(components)
            self.assertIsInstance(caught.exception, ValueError)
            self.assertEqual(caught.exception.reason, reason)

    def test_cross_component_type_reference(self):
        from relay_engine import api_registry as r, errors, api_protocol
        core = d.discovery_definitions()
        a = c.ApiComponent('platform', core, api_protocol.discovery_handlers(), factory)
        b = component('Other')
        request = replace(b.definitions.types[0], shape=d.Record((
            d.FieldDef('id', d.TypeRef('OpaqueId')),)))
        b = replace(b, definitions=replace(b.definitions,
            types=(request, b.definitions.types[1])))
        for components in ((a, b), (b, a)):
            merged = c.compose_components(components)
            compiled = r.compile_registry(merged.definitions, merged.handlers, (errors.api_error_catalog(),))
            self.assertEqual(compiled.operation('other.read'), b.definitions.operations[0])
            self.assertEqual(compiled.types['OtherArgs'], request)
            self.assertEqual(compiled.types['OpaqueId'], next(t for t in core.types if t.name == 'OpaqueId'))

    def test_composition_is_immutable(self):
        a = component('Alpha')
        merged = c.compose_components((a,))
        for mapping in (merged.owners, merged.port_factories, merged.handlers):
            with self.assertRaises(TypeError):
                mapping['changed'] = None
        with self.assertRaises(FrozenInstanceError):
            merged.definitions = None
        with self.assertRaises(FrozenInstanceError):
            a.owner = 'changed'
        a.handlers.clear()
        self.assertEqual(dict(merged.handlers), {'alpha.read': handler})

    def test_compose_imports_no_runtime_module(self):
        for module, expected in (('api_compose', False), ('api_writer', True)):
            script = ('import sys,json;sys.path.insert(0,' + repr(str(TOOLS)) + ');'
                      'import relay_engine.' + module + ';print(json.dumps(sorted(sys.modules)))')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', script],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            modules = set(json.loads(result.stdout))
            if expected:
                self.assertIn('sqlite3', modules)
            else:
                self.assertFalse(modules & {'sqlite3', 'socket', 'relay_engine.submit',
                                            'relay_engine.api_writer'})


if __name__ == '__main__':
    unittest.main()
