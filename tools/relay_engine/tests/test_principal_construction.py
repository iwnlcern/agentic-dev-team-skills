"""Recursive literal-keyed principal construction census and discriminating controls."""
import ast
import importlib
from pathlib import Path
import tempfile
import unittest

PACKAGE = Path(__file__).parents[1]
CONSTRUCTION_SITES = {'ServicePrincipal': {'approval_seal': 'seal_worker.py'},
                      'OperatorPrincipal': {'page': 'api_operator_bridge.py',
                                            'terminal': 'operator_terminal.py'}}


def _resolved_kinds(node, names):
    if isinstance(node, ast.Name):
        return names.get(node.id, frozenset())
    if isinstance(node, ast.Attribute) and node.attr in CONSTRUCTION_SITES:
        return frozenset({node.attr})
    return frozenset()


def alias_kinds(tree, *, max_passes: int) -> dict[str, frozenset[str]]:
    names = {kind: {kind} for kind in CONSTRUCTION_SITES}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in CONSTRUCTION_SITES:
                    names.setdefault(alias.asname or alias.name, set()).add(alias.name)
    for _ in range(max_passes):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = _resolved_kinds(node.value, names)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if value and isinstance(target, ast.Name):
                        kinds = names.setdefault(target.id, set())
                        if value - kinds:
                            kinds.update(value)
                            changed = True
        if not changed:
            return {name: frozenset(kinds) for name, kinds in names.items()}
    raise RuntimeError('construction_scan_did_not_end')


def constructions(root):
    found = []
    for path in sorted(Path(root).rglob('*.py')):
        relative = path.relative_to(root)
        if 'tests' in relative.parts or path.name == 'api_principals.py':
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        assignments = sum(isinstance(node, (ast.Assign, ast.AnnAssign)) for node in ast.walk(tree))
        names = alias_kinds(tree, max_passes=2 * assignments + 2)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                value = node.args[0] if len(node.args) == 1 and not node.keywords else None
                literal = value.value if isinstance(value, ast.Constant) and type(value.value) is str else None
                for kind in sorted(_resolved_kinds(node.func, names)):
                    found.append((relative.as_posix(), node.lineno, kind, literal))
    return found


def violations(root):
    return [row for row in constructions(root)
            if CONSTRUCTION_SITES[row[2]].get(row[3]) != row[0]]


class TestPrincipalConstruction(unittest.TestCase):
    def setUp(self):
        # The worker is the new permitted site and must exist for this census.
        self.worker = importlib.import_module('relay_engine.seal_worker')

    def test_only_seal_worker_constructs_approval_seal(self):
        self.assertEqual(violations(PACKAGE), [])
        sites=[(p,k,v) for p,n,k,v in constructions(PACKAGE)]
        self.assertCountEqual(sites, [
            ('api_operator_bridge.py', 'OperatorPrincipal', 'page'),
            ('operator_terminal.py', 'OperatorPrincipal', 'terminal'),
            ('seal_worker.py', 'ServicePrincipal', 'approval_seal'),
        ])
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'other.py';p.write_text("ServicePrincipal('approval_seal')\nServicePrincipal(name)\n")
            self.assertEqual(len(violations(d)),2)

    def test_construction_scan_recursive(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'nested').mkdir()
            fixtures={
                'alias.py':"from relay_engine.api_principals import ServicePrincipal as SP\nSP('approval_seal')\n",
                'assigned.py':"from relay_engine import api_principals as p\nctor=p.ServicePrincipal\ncopy=ctor\ncopy('approval_seal')\n",
                'nested/worker.py':"ServicePrincipal('approval_seal')\n",
                'dynamic.py':"ServicePrincipal(name)\n",
                'operator.py':"from relay_engine.api_principals import OperatorPrincipal as OP\nf=OP\nf('page')\n",
                'seal_worker.py':"ServicePrincipal('approval_seal')\n"}
            for f,src in fixtures.items():(root/f).write_text(src)
            got=violations(root)
            self.assertEqual({r[0] for r in got},set(fixtures)-{'seal_worker.py'})
            self.assertEqual(len(got),5)
        self.assertEqual(violations(PACKAGE),[])
        self.assertEqual(len(constructions(PACKAGE)), 3)


    def test_construction_scan_ends_on_rebinding(self):
        from relay_engine.tests.test_principal_construction import alias_kinds
        def overwriting_alias_kinds(tree, *, max_passes):
            names = {'ServicePrincipal': 'ServicePrincipal', 'OperatorPrincipal': 'OperatorPrincipal'}
            def resolve(node):
                if isinstance(node, ast.Name):
                    return names.get(node.id)
                if isinstance(node, ast.Attribute) and node.attr in CONSTRUCTION_SITES:
                    return node.attr
                return None
            for _ in range(max_passes):
                changed = False
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        value = resolve(node.value)
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if value and isinstance(target, ast.Name) and names.get(target.id) != value:
                                names[target.id] = value
                                changed = True
                if not changed:
                    return names
            raise RuntimeError('construction_scan_did_not_end')
        fixtures = {
            'rebinding.py': "ctor = ServicePrincipal\nctor = OperatorPrincipal\nctor('page')\n",
            'scopes.py': "def first():\n    ctor = ServicePrincipal\n    ctor('approval_seal')\ndef second():\n    ctor = OperatorPrincipal\n    ctor('approval_seal')\n"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in fixtures.items():
                (root / name).write_text(source)
                tree = ast.parse(source)
                assignments = sum(isinstance(n, (ast.Assign, ast.AnnAssign)) for n in ast.walk(tree))
                bound = 2 * assignments + 2
                self.assertEqual(alias_kinds(tree, max_passes=bound)['ctor'],
                                 frozenset({'ServicePrincipal', 'OperatorPrincipal'}))
                with self.assertRaisesRegex(RuntimeError, '^construction_scan_did_not_end$'):
                    overwriting_alias_kinds(tree, max_passes=bound)
            rows = constructions(root)
            self.assertEqual(len(rows), 6)
            operator_rows = [row for row in violations(root) if row[2] == 'OperatorPrincipal']
            self.assertEqual({row[0] for row in operator_rows}, set(fixtures))
            self.assertEqual(len(operator_rows), 3)

if __name__ == '__main__':unittest.main()
