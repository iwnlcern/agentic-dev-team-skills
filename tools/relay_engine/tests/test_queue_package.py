"""Package inclusion and transaction boundaries, independently of queue code."""
import ast
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from relay_engine.tests import test_strings_gate


def executable_nodes(path):
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    return [n for n in ast.walk(tree) if id(n) not in docstrings]


def transaction_violations(package):
    found = []
    for path in sorted(package.glob('*.py')):
        for node in executable_nodes(path):
            reason = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = node.value.upper()
                if re.search(r'\b(?:BEGIN\s+IMMEDIATE|COMMIT)\b', sql):
                    reason = 'write transaction'
                if path.name != 'liveness.py' and re.search(r'\b(?:BEGIN\s+DEFERRED|ROLLBACK)\b', sql):
                    reason = 'read transaction outside liveness'
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ('executescript','commit'):
                    reason = node.func.attr
            if isinstance(node, ast.With):
                if any(isinstance(i.context_expr, ast.Name) and i.context_expr.id == 'conn'
                       for i in node.items):
                    reason = 'with conn'
            if reason:
                found.append((path.name, node.lineno, reason))
    return found


def immutable_record_violations(package):
    found = []
    expression = re.compile(r'\b(?:UPDATE\s+|DELETE\s+FROM\s+)(?:queue_store_item_sources|queue_ingest_skips)\b', re.I)
    for path in sorted(package.glob('*.py')):
        for node in executable_nodes(path):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and expression.search(node.value):
                found.append((path.name, node.lineno))
    return found


class TestQueuePackage(unittest.TestCase):
    def test_package_in_strings_gate(self):
        package = Path(__file__).parents[1] / 'operator_queue'
        self.assertTrue((package / '__init__.py').is_file())
        self.assertIn(str(package / '__init__.py'), test_strings_gate.TestGate.production_paths())
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / 'relay_engine'
            nested = engine / 'operator_queue'
            nested.mkdir(parents=True)
            probe = nested / 'probe.py'
            probe.write_text('import subprocess\n')
            with mock.patch.object(test_strings_gate, '__file__', str(engine / 'tests/test_strings_gate.py')):
                self.assertIn(str(probe), test_strings_gate.TestGate.production_paths())
            self.assertTrue(test_strings_gate.gate_violations([str(probe)]))
            top = engine / 'probe.py'
            top.write_text(probe.read_text())
            self.assertTrue(test_strings_gate.gate_violations([str(top)]))

    def test_queue_issues_no_transaction_control(self):
        package = Path(__file__).parents[1] / 'operator_queue'
        self.assertTrue(package.is_dir())
        self.assertEqual(transaction_violations(package), [])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            probe = folder / 'probe.py'
            probe.write_text('"""COMMIT in a docstring is harmless."""\n# BEGIN IMMEDIATE\nx = 1\n')
            self.assertEqual(transaction_violations(folder), [])
            for source in ('conn.execute("COMMIT")', 'conn.execute("BEGIN IMMEDIATE")',
                           'conn.executescript("SELECT 1")', 'conn.commit()',
                           'with conn:\n    pass', 'conn.execute("BEGIN DEFERRED")',
                           'conn.execute("ROLLBACK")'):
                with self.subTest(source=source):
                    probe.write_text(source)
                    self.assertTrue(transaction_violations(folder))
            probe.unlink()
            (folder / 'liveness.py').write_text('conn.execute("BEGIN DEFERRED")\nconn.execute("ROLLBACK")')
            self.assertEqual(transaction_violations(folder), [])


if __name__ == '__main__':
    unittest.main()
