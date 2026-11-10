"""Frozen operator and service principal types with closed value lists."""
import ast
import dataclasses
import os
import tempfile
import unittest

from relay_engine import api_principals as p

PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"OperatorPrincipal", "ServicePrincipal"}


def principal_calls(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in NAMES:
            found.append(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in NAMES:
            found.append(func.attr)
    return found


class TestOperatorPrincipal(unittest.TestCase):
    def test_operator_principal_accepts_each_surface(self):
        self.assertEqual(p.OPERATOR_SURFACES, ("frank_panel", "page"))
        for surface in ("page", "frank_panel"):
            principal = p.OperatorPrincipal(surface)
            self.assertEqual(principal.surface, surface)
            self.assertEqual(principal, p.OperatorPrincipal(surface))
            self.assertEqual(hash(principal), hash(p.OperatorPrincipal(surface)))
        self.assertNotEqual(p.OperatorPrincipal("page"), p.OperatorPrincipal("frank_panel"))

    def test_operator_principal_refuses_other_values(self):
        for value in ("Page", "page ", "operator", None, b"page"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    p.OperatorPrincipal(value)
                self.assertEqual(caught.exception.args, ("invalid_operator_surface",))


class TestServicePrincipal(unittest.TestCase):
    def test_service_principal_accepts_each_service(self):
        expected = ("authority_service", "console_queue", "engine_liveness", "xproject_transfer")
        self.assertEqual(p.SERVICE_NAMES, expected)
        for service in expected:
            principal = p.ServicePrincipal(service)
            self.assertEqual(principal.service, service)
            self.assertEqual(principal, p.ServicePrincipal(service))
            self.assertEqual(hash(principal), hash(p.ServicePrincipal(service)))

    def test_service_principal_refuses_other_values(self):
        for value in ("Console_Queue", "platform", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    p.ServicePrincipal(value)
                self.assertEqual(caught.exception.args, ("invalid_service_principal",))


class TestShape(unittest.TestCase):
    def test_principals_are_frozen(self):
        operator = p.OperatorPrincipal("page")
        service = p.ServicePrincipal("console_queue")
        attempts = (
            (operator, "surface", "frank_panel"),
            (service, "service", "engine_liveness"),
            (operator, "extra", "x"),
            (service, "extra", "x"),
        )
        for instance, name, value in attempts:
            with self.subTest(instance=instance, name=name):
                with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
                    setattr(instance, name, value)
        self.assertEqual(operator.surface, "page")
        self.assertEqual(service.service, "console_queue")

    def test_module_imports_only_stdlib(self):
        with open(p.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                modules.add(node.module)
        self.assertTrue(modules)
        self.assertLessEqual(modules, {"dataclasses", "__future__"})

    def test_nothing_constructs_principals_outside_tests(self):
        own = os.path.abspath(p.__file__)
        for name in sorted(os.listdir(PACKAGE)):
            path = os.path.join(PACKAGE, name)
            if not name.endswith(".py") or os.path.abspath(path) == own:
                continue
            with self.subTest(path=name):
                self.assertEqual(principal_calls(path), [])
        with tempfile.TemporaryDirectory() as folder:
            control = os.path.join(folder, "control.py")
            with open(control, "w", encoding="utf-8") as handle:
                handle.write("OperatorPrincipal('page')\n"
                             "ServicePrincipal('console_queue')\n"
                             "api_principals.OperatorPrincipal('page')\n"
                             "relay_engine.api_principals.ServicePrincipal('console_queue')\n")
            self.assertEqual(principal_calls(control),
                             ["OperatorPrincipal", "ServicePrincipal",
                              "OperatorPrincipal", "ServicePrincipal"])


if __name__ == "__main__":
    unittest.main()
