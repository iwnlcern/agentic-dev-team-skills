"""Recursive literal-keyed principal construction census and discriminating controls."""
import ast
import importlib
from pathlib import Path
import tempfile
import unittest

PACKAGE = Path(__file__).parents[1]
CONSTRUCTION_SITES = {'ServicePrincipal': {'approval_seal': 'seal_worker.py'},
                      'OperatorPrincipal': {}}


def constructions(root):
    found = []
    for path in sorted(Path(root).rglob('*.py')):
        relative = path.relative_to(root)
        if 'tests' in relative.parts or path.name == 'api_principals.py':
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        names = {'ServicePrincipal': 'ServicePrincipal', 'OperatorPrincipal': 'OperatorPrincipal'}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in names:
                        names[alias.asname or alias.name] = alias.name
        def resolve(node):
            if isinstance(node, ast.Name): return names.get(node.id)
            if isinstance(node, ast.Attribute) and node.attr in CONSTRUCTION_SITES: return node.attr
            return None
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = resolve(node.value)
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if value and isinstance(target, ast.Name) and names.get(target.id) != value:
                            names[target.id] = value; changed = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (kind := resolve(node.func)):
                value = node.args[0] if len(node.args) == 1 and not node.keywords else None
                literal = value.value if isinstance(value, ast.Constant) and type(value.value) is str else None
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
        self.assertEqual(sites,[('seal_worker.py','ServicePrincipal','approval_seal')])
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
        self.assertEqual(len(constructions(PACKAGE)),1)


if __name__ == '__main__':unittest.main()
