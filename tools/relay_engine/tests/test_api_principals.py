"""Frozen operator and service principal types with closed value lists."""
import ast
import dataclasses
import os
import sys
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


# Plan r2 erratum 1 section E2: a principal cannot be changed after construction; the error
# type is not part of that contract.  A field is refused with FrozenInstanceError on every
# supported Python; a name that is not a field is refused with TypeError before 3.14, from the
# generated __setattr__ and __delattr__ of a slotted frozen dataclass, and FrozenInstanceError
# from 3.14 (section E1, measured on the release image's 3.11.18 and on 3.14.6).
NON_FIELD_ERROR = dataclasses.FrozenInstanceError if sys.version_info >= (3, 14) else TypeError


def raised(action):
    """Answer the exact type action raised, or None when it raised nothing."""
    try:
        action()
    except Exception as error:
        return type(error)
    return None


def mutation_attempts(instance, field, value):
    """The four section E3 attempts, as (label, exact error type or None)."""
    return [
        ("set field", raised(lambda: setattr(instance, field, value))),
        ("set extra", raised(lambda: setattr(instance, "extra", "x"))),
        ("delete field", raised(lambda: delattr(instance, field))),
        ("delete extra", raised(lambda: delattr(instance, "extra"))),
    ]


@dataclasses.dataclass(frozen=False, slots=True)
class MutableOperatorCopy:
    surface: str


@dataclasses.dataclass(frozen=False, slots=True)
class MutableServiceCopy:
    service: str


@dataclasses.dataclass(frozen=True)
class UnslottedCopy:
    surface: str


# Plan r2 erratum 2 section E2: values that only the type check refuses.
class _Str(str):
    pass


class _AlwaysEqual:
    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0


def construction_error(cls, value):
    """Answer the exception constructing cls(value) raised, or None when it constructed."""
    try:
        cls(value)
    except Exception as error:
        return error
    return None


class TestOperatorPrincipal(unittest.TestCase):
    def test_operator_principal_accepts_each_surface(self):
        for surface in ("page", "frank_panel", "terminal"):
            principal = p.OperatorPrincipal(surface)
            self.assertEqual(principal.surface, surface)
            self.assertEqual(principal, p.OperatorPrincipal(surface))
            self.assertEqual(hash(principal), hash(p.OperatorPrincipal(surface)))
        self.assertNotEqual(p.OperatorPrincipal("page"), p.OperatorPrincipal("frank_panel"))
        self.assertNotEqual(p.OperatorPrincipal("terminal"), p.OperatorPrincipal("page"))
        self.assertNotEqual(p.OperatorPrincipal("terminal"), p.OperatorPrincipal("frank_panel"))
        self.assertEqual(p.OPERATOR_SURFACES, ("frank_panel", "page", "terminal"))

    def test_operator_principal_refuses_other_values(self):
        for value in ("Page", "page ", "Terminal", "terminal ", "", "operator", None,
                      b"page", _Str("page"), _AlwaysEqual()):
            with self.subTest(value=value):
                error = construction_error(p.OperatorPrincipal, value)
                self.assertIs(type(error), ValueError)
                self.assertEqual(error.args, ("invalid_operator_surface",))


class TestServicePrincipal(unittest.TestCase):
    def test_service_principal_accepts_each_service(self):
        expected = ("authority_service", "console_queue", "engine_liveness", "xproject_transfer")
        self.assertEqual(p.SERVICE_NAMES, expected)
        for service in expected:
            principal = p.ServicePrincipal(service)
            self.assertEqual(principal.service, service)
            self.assertEqual(principal, p.ServicePrincipal(service))
            self.assertEqual(hash(principal), hash(p.ServicePrincipal(service)))
        self.assertNotEqual(p.ServicePrincipal("console_queue"), p.ServicePrincipal("engine_liveness"))

    def test_service_principal_refuses_other_values(self):
        for value in ("Console_Queue", "console_queue ", "platform", "", None,
                      _Str("console_queue"), _AlwaysEqual()):
            with self.subTest(value=value):
                error = construction_error(p.ServicePrincipal, value)
                self.assertIs(type(error), ValueError)
                self.assertEqual(error.args, ("invalid_service_principal",))


class TestShape(unittest.TestCase):
    def test_principals_are_frozen(self):
        expected = [("set field", dataclasses.FrozenInstanceError), ("set extra", NON_FIELD_ERROR),
                    ("delete field", dataclasses.FrozenInstanceError), ("delete extra", NON_FIELD_ERROR)]
        cases = ((p.OperatorPrincipal("page"), "surface", "frank_panel", "page"),
                 (p.ServicePrincipal("console_queue"), "service", "engine_liveness", "console_queue"))
        for instance, field, value, constructed in cases:
            with self.subTest(principal=type(instance).__name__):
                for (label, got), (_, want) in zip(mutation_attempts(instance, field, value), expected):
                    with self.subTest(attempt=label):
                        self.assertIs(got, want)
                self.assertEqual(getattr(instance, field), constructed)
                self.assertFalse(hasattr(instance, "extra"))
        # Control: a copy that is not frozen accepts the field assignment, so the
        # assertions above tell a type that can be changed from one that cannot.
        for copy, field, value in ((MutableOperatorCopy("page"), "surface", "frank_panel"),
                                   (MutableServiceCopy("console_queue"), "service", "engine_liveness")):
            with self.subTest(control=type(copy).__name__):
                self.assertIsNone(raised(lambda: setattr(copy, field, value)))
                self.assertEqual(getattr(copy, field), value)

    def test_principals_keep_the_api_shape(self):
        for cls, instance in ((p.OperatorPrincipal, p.OperatorPrincipal("page")),
                              (p.ServicePrincipal, p.ServicePrincipal("console_queue"))):
            with self.subTest(principal=cls.__name__):
                self.assertIn("__slots__", cls.__dict__)
                self.assertFalse(hasattr(instance, "__dict__"))
        # Control: a frozen class without slots fails both checks.
        self.assertNotIn("__slots__", UnslottedCopy.__dict__)
        self.assertTrue(hasattr(UnslottedCopy("page"), "__dict__"))

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
