import ast
import argparse
import os
from pathlib import Path
import re
import tempfile
import unittest

from relay_engine import cli, strings


FORBIDDEN_ATTRS = {"__closure__", "__globals__", "__dict__", "__wrapped__",
                   "__getattribute__"}
FORBIDDEN_IMPORTS = {"logging", "subprocess", "inspect"}
SINK_ATTRS = {"stdout", "stderr", "__stdout__", "__stderr__"}


def storage_term_matches(readme_text=None, inventory=None, parser=None):
    expression = re.compile(r"sqlite|database|ledger", re.IGNORECASE)
    package = Path(__file__).parents[1]
    if readme_text is None:
        readme_text = (package / "README.md").read_text(encoding="utf-8")
    if inventory is None:
        inventory = strings.INVENTORY
    if parser is None:
        parser = cli.build_parser()
    surface = [("README", readme_text),
               ("help:<root>", parser.format_help())]
    surface.extend(("inventory:" + key, value)
                   for key, value in sorted(inventory.items()))

    def subparsers(parser):
        return next((action for action in parser._actions
                     if isinstance(action, argparse._SubParsersAction)), None)

    def visit(parser, prefix=()):
        action = subparsers(parser)
        if action is None:
            return
        for name, child in sorted(action.choices.items()):
            command = prefix + (name,)
            surface.append(("help:" + " ".join(command),
                            child.format_help()))
            if any(isinstance(action, argparse._SubParsersAction)
                   for action in child._actions):
                visit(child, command)

    visit(parser)
    return [(name, match.group(0)) for name, value in surface
            for match in expression.finditer(value)]


def gate_violations(paths):
    violations = []
    for path in paths:
        module = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as source:
            tree = ast.parse(source.read(), filename=path)
        sys_aliases = {"sys"}
        os_aliases = {"os"}
        direct_sys_sinks = set()
        for imported in ast.walk(tree):
            if isinstance(imported, ast.Import):
                for alias in imported.names:
                    if alias.name == "sys":
                        sys_aliases.add(alias.asname or alias.name)
                    if alias.name == "os":
                        os_aliases.add(alias.asname or alias.name)
            elif (isinstance(imported, ast.ImportFrom) and
                  imported.module == "sys"):
                for alias in imported.names:
                    if alias.name in SINK_ATTRS:
                        direct_sys_sinks.add(alias.asname or alias.name)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def function_name(node):
            cur = node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return cur.name
            return None

        for node in ast.walk(tree):
            fn = function_name(node)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name.split(".")[0] for a in node.names]
                         if isinstance(node, ast.Import) else
                         [str(node.module).split(".")[0]])
                for name in names:
                    if name in FORBIDDEN_IMPORTS:
                        violations.append((module, node.lineno,
                                           "forbidden import " + name))
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    violations.append((module, node.lineno, "print call"))
                if isinstance(node.func, ast.Attribute):
                    base = node.func.value
                    if (isinstance(base, ast.Name) and
                            base.id in os_aliases and
                            node.func.attr in {"write", "fdopen"} and
                            not (module == "daemon" and
                                 fn == "_log_diagnostic")):
                        violations.append((module, node.lineno,
                                           "raw os sink"))
                    if (isinstance(base, ast.Name) and
                            base.id in direct_sys_sinks and
                            node.func.attr == "write"):
                        violations.append((module, node.lineno,
                                           "raw sys sink"))
                    if (node.func.attr == "attrgetter" and node.args and
                            isinstance(node.args[0], ast.Constant) and
                            node.args[0].value in FORBIDDEN_ATTRS):
                        violations.append((module, node.lineno,
                                           "reflective attrgetter"))
                if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                    if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                        violations.append((module, node.lineno,
                                           "dynamic getattr"))
                    elif node.args[1].value in FORBIDDEN_ATTRS:
                        violations.append((module, node.lineno,
                                           "reflective getattr"))
                call_name = None
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                if call_name in {"render", "emit"}:
                    key_index = 1 if call_name == "emit" else 0
                    internal_dispatch = (module in {"strings", "errors"})
                    if (not internal_dispatch and
                            (len(node.args) <= key_index or
                            not isinstance(node.args[key_index], ast.Constant) or
                            not isinstance(node.args[key_index].value, str) or
                            node.args[key_index].value not in strings.INVENTORY)):
                        violations.append((module, node.lineno,
                                           "non-inventory emission key"))
            if isinstance(node, ast.Attribute):
                if node.attr in FORBIDDEN_ATTRS:
                    violations.append((module, node.lineno,
                                       "reflective attribute"))
                if (isinstance(node.value, ast.Name) and
                        node.value.id in sys_aliases and
                        node.attr in SINK_ATTRS and
                        not ((module == "strings" and
                              fn in {"_make_emitter", "emit"}) or
                             (module == "cli" and fn == "cmd_show"))):
                    violations.append((module, node.lineno, "raw sys sink"))
    return violations


class TestGate(unittest.TestCase):
    @classmethod
    def production_paths(cls):
        package = os.path.dirname(os.path.dirname(__file__))
        paths = [os.path.join(package, name) for name in os.listdir(package)
                 if name.endswith(".py")]
        paths.append(os.path.join(os.path.dirname(package), "relay"))
        return paths

    def test_production_tree_uses_only_keyed_output(self):
        self.assertEqual(gate_violations(self.production_paths()), [])

    def test_engine_surface_has_no_storage_terms(self):
        self.assertEqual(storage_term_matches(), [])
        self.assertEqual(
            storage_term_matches(readme_text="sQlItE", inventory={}),
            [("README", "sQlItE")])
        probe_key = "test-storage-term-probe"
        strings.INVENTORY[probe_key] = "DaTaBaSe"
        try:
            self.assertEqual(storage_term_matches(),
                             [("inventory:" + probe_key, "DaTaBaSe")])
        finally:
            del strings.INVENTORY[probe_key]
        parser = cli.build_parser()
        subparsers = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction))
        probe = subparsers.add_parser(
            "synthetic-storage-probe", description="LeDgEr")
        probe.set_defaults(handler=lambda _args: 0)
        self.assertEqual(
            storage_term_matches(readme_text="", inventory={}, parser=parser),
            [("help:synthetic-storage-probe", "LeDgEr")])

    def test_storage_terms_in_root_help_are_detected(self):
        parser = cli.build_parser()
        subparsers = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction))
        subparsers.add_parser(
            "synthetic-root-storage-probe",
            help="sQlItE root sentinel")
        self.assertIn("sQlItE root sentinel", parser.format_help())
        self.assertEqual(
            storage_term_matches(readme_text="", inventory={}, parser=parser),
            [("help:<root>", "sQlItE")])

        root_only = argparse.ArgumentParser(
            epilog="DaTaBaSe root-only sentinel")
        self.assertEqual(
            storage_term_matches(
                readme_text="", inventory={}, parser=root_only),
            [("help:<root>", "DaTaBaSe")])

    def check_bad(self, source, needle):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "bad.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(source)
            self.assertTrue(any(needle in v[2]
                                for v in gate_violations([path])))

    def test_accidental_output_paths_are_detected(self):
        self.check_bad("import sys\nsys.__stdout__.write('x')\n",
                       "raw sys sink")
        self.check_bad("import os\nos.write(1, b'x')\n", "raw os sink")
        self.check_bad("print ('x')\n", "print call")
        self.check_bad("import logging\n", "forbidden import")
        self.check_bad("import subprocess\n", "forbidden import")

    def test_function_reflection_is_detected(self):
        self.check_bad("from relay_engine import strings\nx = strings.emit.__closure__\n",
                       "reflective attribute")
        self.check_bad("import inspect\n", "forbidden import")

    def test_dynamic_getattr_closure_banned(self):
        self.check_bad("from relay_engine import strings\nx = getattr(strings.emit, '__closure__')\n",
                       "reflective getattr")
        self.check_bad("from relay_engine import strings\nx = getattr(strings.emit, name)\n",
                       "dynamic getattr")

    def test_object_getattribute_banned(self):
        self.check_bad("from relay_engine import strings\nx = object.__getattribute__(strings.emit, '__closure__')\n",
                       "reflective attribute")

    def test_bound_getattribute_and_attrgetter_banned(self):
        self.check_bad("from relay_engine import strings\nx = strings.emit.__getattribute__('__closure__')\n",
                       "reflective attribute")
        self.check_bad("import operator\nfrom relay_engine import strings\nx = operator.attrgetter('__closure__')(strings.emit)\n",
                       "reflective attrgetter")

    def test_import_alias_sink_forms_banned(self):
        self.check_bad("import sys as s\ns.stdout.write('x')\n",
                       "raw sys sink")
        self.check_bad("from sys import stdout as out\nout.write('x')\n",
                       "raw sys sink")
        self.check_bad("import os as operating\noperating.write(1, b'x')\n",
                       "raw os sink")

    def test_fresh_factory_invocation_raises(self):
        with self.assertRaises(RuntimeError):
            strings._make_emitter()

    def test_emit_verbatim_single_reference(self):
        refs = []
        for path in self.production_paths():
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and
                        node.attr == "emit_verbatim"):
                    refs.append(path)
        self.assertEqual(refs, [os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "cli.py")])

    def test_diagnostic_sink_binding_site_is_unique(self):
        bindings = []
        for path in self.production_paths():
            module = os.path.splitext(os.path.basename(path))[0]
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), filename=path)
            parents = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node

            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and
                        isinstance(node.func, ast.Attribute) and
                        node.func.attr == "set_diagnostic_sink" and
                        node.args and isinstance(node.args[0], ast.Name) and
                        node.args[0].id == "_log_diagnostic"):
                    continue
                owner = node
                while owner in parents:
                    owner = parents[owner]
                    if isinstance(owner, (ast.FunctionDef,
                                          ast.AsyncFunctionDef)):
                        break
                bindings.append((module, owner.name, node.lineno))
        self.assertEqual([(module, owner) for module, owner, _ in bindings],
                         [("daemon", "start")])


if __name__ == "__main__":
    unittest.main()
