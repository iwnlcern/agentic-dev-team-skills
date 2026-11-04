"""Tests for the platform address validator (DD-v3-platform section Joint lock J2).

Non-ASCII inputs are written as escapes so that no confusable character
reads as its ASCII look-alike in this file.
"""

import ast
import copy
import dataclasses
import pickle
import sys
import unittest
from pathlib import Path

from relay_engine import addresses
from relay_engine.addresses import (
    Address, AddressRefused, Reason, RETIRED_ROLE_ALIASES, RESERVED_SENDERS,
    ROLE_WORDS, SPECIAL_ADDRESSES, parse_address, parse_address_list, render,
    validate_name)


ENGINE_DIR = Path(__file__).resolve().parent.parent

CANONICAL_NINE = {
    "planner", "implementer", "reviewer", "orchestrator-planner",
    "orchestrator-reviewer", "master-planner", "master-reviewer",
    "domain-planner", "domain-reviewer",
}

RUN_SEATS = ["v3.orchestrator-planner", "v3.orchestrator-reviewer"] + [
    "%s.%s" % (team, role)
    for team in ("v3-engine", "v3-kit", "v3-adapter", "v3-tests",
                 "v3-platform", "v3-authority", "v3-console")
    for role in ("planner", "implementer")
]

ACCEPTED = [
    "v3-platform.planner",
    "master.orchestrator-planner@harness",
    "operator",
    "x.planner@pdc",
    "a" * 64 + ".reviewer@" + "p" * 32,
] + RUN_SEATS

REFUSALS = {
    Reason.NOT_AN_ADDRESS: [
        "orchestrator", "v3-platform", "system", "", ".planner", "x.", ".",
        "x..planner", ".x.planner",
    ],
    Reason.AT_EMPTY_OR_REPEATED: ["x.planner@", "@pdc", "x.planner@pdc@hq"],
    Reason.UPPERCASE: ["X.planner", "x.Planner", "x.planner@PDC"],
    Reason.BAD_CHARACTER: [
        "harness.master.orchestrator-planner", "x_y.planner", "-x.planner",
        "x.planner@p_d", "x.planner@-pdc", "x.pl_anner", "x.plan!ner",
    ],
    Reason.UNKNOWN_ROLE: ["x.wizard", "x.pair-planner", "x.pair-implementer",
                          "x.0", "x.planner2", "x.-planner"],
    Reason.NAME_TOO_LONG: [
        "a" * 65 + ".planner", "x.planner@" + "p" * 33,
    ],
    Reason.WHITESPACE_IN_TOKEN: ["x. planner", "x.planner @pdc", "x.planner\t"],
    Reason.OPERATOR_QUALIFIED: ["operator@pdc"],
}


class RefusalAssertions:
    def assertRefused(self, call, token, reason, canonical=None):
        with self.assertRaises(AddressRefused) as caught:
            call(token)
        self.assertIs(caught.exception.reason, reason)
        self.assertEqual(caught.exception.token, token)
        self.assertEqual(caught.exception.canonical, canonical)
        return caught.exception


class TestConstants(unittest.TestCase):
    def test_role_words_are_the_canonical_nine(self):
        self.assertEqual(ROLE_WORDS, CANONICAL_NINE)
        self.assertIsInstance(ROLE_WORDS, frozenset)
        self.assertNotIn("pair-planner", ROLE_WORDS)
        self.assertNotIn("pair-implementer", ROLE_WORDS)
        self.assertEqual(dict(RETIRED_ROLE_ALIASES), {
            "pair-planner": "planner", "pair-implementer": "implementer"})
        self.assertEqual(SPECIAL_ADDRESSES, frozenset({"operator"}))
        self.assertEqual(RESERVED_SENDERS, frozenset({"system"}))

    def test_constants_are_read_only(self):
        with self.assertRaises(TypeError):
            RETIRED_ROLE_ALIASES["pair-reviewer"] = "reviewer"
        address = parse_address("x.planner")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            address.role = "reviewer"


class TestParse(RefusalAssertions, unittest.TestCase):
    def test_parses_in_project_address(self):
        self.assertEqual(
            parse_address("v3-platform.planner"),
            Address(team="v3-platform", role="planner", project=None,
                    special=None))

    def test_parses_cross_project_address(self):
        address = parse_address("master.orchestrator-planner@harness")
        self.assertEqual(address.project, "harness")
        self.assertEqual(address.team, "master")
        self.assertEqual(address.role, "orchestrator-planner")
        self.assertIsNone(address.special)

    def test_operator_is_the_only_special(self):
        self.assertEqual(parse_address("operator"),
                         Address(team=None, role=None, project=None,
                                 special="operator"))
        self.assertRefused(parse_address, "system", Reason.NOT_AN_ADDRESS)
        self.assertRefused(parse_address, "orchestrator",
                           Reason.NOT_AN_ADDRESS)
        self.assertRefused(parse_address, "Operator", Reason.UPPERCASE)

    def test_render_round_trips(self):
        for token in ACCEPTED:
            with self.subTest(token=token):
                self.assertEqual(render(parse_address(token)), token)

    def test_refusal_table(self):
        self.assertEqual(set(REFUSALS), set(Reason),
                         "every Reason member needs at least one row")
        for reason, tokens in REFUSALS.items():
            self.assertTrue(tokens, reason)
            for token in tokens:
                with self.subTest(reason=reason.name, token=token):
                    with self.assertRaises(AddressRefused) as caught:
                        parse_address(token)
                    self.assertIs(caught.exception.reason, reason)
                    self.assertEqual(caught.exception.token, token)

    def test_retired_alias_names_canonical(self):
        self.assertRefused(parse_address, "x.pair-planner",
                           Reason.UNKNOWN_ROLE, canonical="planner")
        self.assertRefused(parse_address, "x.pair-implementer",
                           Reason.UNKNOWN_ROLE, canonical="implementer")
        for token in ("x.wizard", "x.pair-reviewer", "x.pair"):
            with self.subTest(token=token):
                self.assertRefused(parse_address, token, Reason.UNKNOWN_ROLE,
                                   canonical=None)

    def test_precedence_is_declared_order(self):
        for token, reason in (
                ("X Y.planner@", Reason.WHITESPACE_IN_TOKEN),
                ("X.wizard@", Reason.UPPERCASE),
                ("operator@@pdc", Reason.AT_EMPTY_OR_REPEATED),
                ("x_y.wizard", Reason.BAD_CHARACTER),
                ("a" * 65 + ".planner@p_d", Reason.BAD_CHARACTER),
                ("x_y.planner@" + "p" * 33, Reason.BAD_CHARACTER),
                ("a" * 65 + ".wizard", Reason.NAME_TOO_LONG),
                ("x.wizard@" + "p" * 33, Reason.NAME_TOO_LONG)):
            with self.subTest(token=token):
                self.assertRefused(parse_address, token, reason)

    def test_non_ascii_is_refused(self):
        bad_character = [
            "pd\u0441.planner",           # Cyrillic small es in a team
            "x.planner@pd\u0441",         # Cyrillic small es in a project
            "x\u0410.planner",            # Cyrillic capital A
            "x\u00c9.planner",            # Latin capital E with acute
            "x\u03a3.planner",            # Greek capital sigma
            "x\uff21.planner",            # full-width capital A
            "x.planner\uff20pdc",         # full-width commercial at
            "x\uff11.planner",            # full-width digit one
        ]
        for token in bad_character:
            with self.subTest(token=ascii(token)):
                self.assertRefused(parse_address, token, Reason.BAD_CHARACTER)
        for token in ("x.\u00a0planner", "x.plan\u2003ner"):
            with self.subTest(token=ascii(token)):
                self.assertRefused(parse_address, token,
                                   Reason.WHITESPACE_IN_TOKEN)

    def test_this_runs_addresses_parse(self):
        self.assertEqual(len(RUN_SEATS), 16)
        for token in RUN_SEATS:
            with self.subTest(token=token):
                address = parse_address(token)
                self.assertEqual(render(address), token)
                self.assertIsNone(address.project)


class TestList(RefusalAssertions, unittest.TestCase):
    def test_list_parsing(self):
        self.assertEqual(
            parse_address_list(" a.planner ,\tb.implementer@pdc"),
            [Address(team="a", role="planner"),
             Address(team="b", role="implementer", project="pdc")])
        for raw in ("a.planner,,b.planner", "", "a.planner,", ",a.planner"):
            with self.subTest(raw=raw):
                with self.assertRaises(AddressRefused) as caught:
                    parse_address_list(raw)
                self.assertIs(caught.exception.reason, Reason.NOT_AN_ADDRESS)
                self.assertEqual(caught.exception.token, "")
        with self.assertRaises(AddressRefused) as caught:
            parse_address_list("a.planner, x.wizard")
        self.assertIs(caught.exception.reason, Reason.UNKNOWN_ROLE)
        self.assertEqual(caught.exception.token, "x.wizard")
        with self.assertRaises(AddressRefused) as caught:
            parse_address_list("a.planner,\u00a0b.planner")
        self.assertIs(caught.exception.reason, Reason.WHITESPACE_IN_TOKEN)
        self.assertEqual(caught.exception.token, "\u00a0b.planner")
        for raw, token in (("a.planner\r,b.planner", "a.planner\r"),
                           ("a.planner,\nb.planner", "\nb.planner")):
            with self.subTest(raw=raw):
                with self.assertRaises(AddressRefused) as caught:
                    parse_address_list(raw)
                self.assertIs(caught.exception.reason,
                              Reason.WHITESPACE_IN_TOKEN)
                self.assertEqual(caught.exception.token, token)


class TestNames(RefusalAssertions, unittest.TestCase):
    def test_validate_name(self):
        for kind, limit in (("project", 32), ("repository", 32),
                            ("team", 64)):
            with self.subTest(kind=kind):
                for value in ("pdc", "path-a", "0", "a" * limit):
                    validate_name(kind, value)
                refuse = lambda value: validate_name(kind, value)
                self.assertRefused(refuse, "a" * (limit + 1),
                                   Reason.NAME_TOO_LONG)
                for value in ("a@b", "a.b", "a:b", "a/b", "a_b", "-ab", "",
                              "p\u0441"):
                    self.assertRefused(refuse, value, Reason.BAD_CHARACTER)
                self.assertRefused(refuse, "Pdc", Reason.UPPERCASE)
                self.assertRefused(refuse, "a b", Reason.WHITESPACE_IN_TOKEN)
        with self.assertRaises(ValueError) as caught:
            validate_name("seat", "pdc")
        self.assertNotIsInstance(caught.exception, AddressRefused)


class TestModuleBoundary(unittest.TestCase):
    @staticmethod
    def imported_roots(path):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add("." if node.level else node.module.split(".")[0])
        return roots

    def test_imports_only_stdlib(self):
        roots = self.imported_roots(addresses.__file__)
        self.assertTrue(roots, "the module imports something")
        self.assertNotIn("relay_engine", roots)
        self.assertNotIn(".", roots)
        self.assertLessEqual(roots, set(sys.stdlib_module_names))
        control = self.imported_roots(ENGINE_DIR / "seats.py")
        self.assertIn("relay_engine", control)

    @staticmethod
    def dynamic_imports(source):
        """Names through which source could load a module at run time."""
        found = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names
                             if alias.name.split(".")[0] == "importlib")
            elif isinstance(node, ast.ImportFrom) and node.module and (
                    node.module.split(".")[0] == "importlib"):
                found.add(node.module)
            elif isinstance(node, ast.Name) and node.id == "__import__":
                found.add("__import__")
            elif isinstance(node, ast.Attribute) and node.attr == "__import__":
                found.add("__import__")
        return found

    def test_no_dynamic_imports(self):
        source = Path(addresses.__file__).read_text(encoding="utf-8")
        self.assertEqual(self.dynamic_imports(source), set())
        for control in ("import importlib",
                        "from importlib import import_module",
                        "__import__('relay_engine.seats')",
                        "import builtins\nbuiltins.__import__('relay_engine')"):
            with self.subTest(control=control):
                self.assertTrue(self.dynamic_imports(control))


class TestPanelFold(RefusalAssertions, unittest.TestCase):
    """Rows from the PR #49 test-lens panel; each pins a planted fault."""

    def test_every_role_word_parses(self):
        for role in sorted(ROLE_WORDS):
            with self.subTest(role=role):
                self.assertEqual(parse_address("x." + role),
                                 Address(team="x", role=role))

    def test_reason_does_not_flip(self):
        cyrillic_es = chr(0x0441)
        cyrillic_ie = chr(0x0435)
        for token, reason in (
                ("x_y", Reason.NOT_AN_ADDRESS),
                ("x_y@pdc", Reason.NOT_AN_ADDRESS),
                ("p" + cyrillic_es + "c", Reason.NOT_AN_ADDRESS),
                ("a" * 65 + ".pl_anner", Reason.BAD_CHARACTER),
                ("x.planner@p dc", Reason.WHITESPACE_IN_TOKEN),
                ("x.plann" + cyrillic_ie + "r", Reason.BAD_CHARACTER)):
            with self.subTest(token=ascii(token)):
                self.assertRefused(parse_address, token, reason)

    def test_casefold_look_alikes_are_bad_character(self):
        kelvin, dotless_i, long_s = chr(0x212A), chr(0x0131), chr(0x017F)
        for token in ("x" + kelvin + ".planner", dotless_i + "x.planner",
                      "x.planner@" + long_s + "dc", "x.p" + long_s + "anner"):
            with self.subTest(token=ascii(token)):
                self.assertRefused(parse_address, token, Reason.BAD_CHARACTER)
        for kind in ("project", "repository", "team"):
            for value in (kelvin, "p" + dotless_i, long_s + "dc"):
                with self.subTest(kind=kind, value=ascii(value)):
                    self.assertRefused(lambda v: validate_name(kind, v),
                                       value, Reason.BAD_CHARACTER)

    def test_other_whitespace_at_list_edge_is_kept(self):
        for code in (0x0B, 0x0C, 0x1C, 0x1F, 0x85, 0x2028):
            edge = chr(code)
            for raw, token in (("a.planner," + edge + "b.planner",
                                edge + "b.planner"),
                               ("a.planner" + edge + ",b.planner",
                                "a.planner" + edge)):
                with self.subTest(raw=ascii(raw)):
                    with self.assertRaises(AddressRefused) as caught:
                        parse_address_list(raw)
                    self.assertIs(caught.exception.reason,
                                  Reason.WHITESPACE_IN_TOKEN)
                    self.assertEqual(caught.exception.token, token)

    def test_constant_and_exception_types(self):
        for constant in (ROLE_WORDS, SPECIAL_ADDRESSES, RESERVED_SENDERS):
            self.assertIsInstance(constant, frozenset)
        self.assertTrue(issubclass(AddressRefused, ValueError))

    def test_validate_name_kind_must_be_a_known_string(self):
        for kind in (["project"], {"team"}, None, 1, b"project", "Project"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as caught:
                    validate_name(kind, "pdc")
                self.assertNotIsInstance(caught.exception, AddressRefused)

    def test_refusal_survives_copy_and_pickle(self):
        original = None
        try:
            parse_address("x.pair-planner")
        except AddressRefused as refused:
            original = refused
        self.assertIsNotNone(original)
        for clone in (copy.copy(original), copy.deepcopy(original),
                      pickle.loads(pickle.dumps(original))):
            with self.subTest(clone=type(clone)):
                self.assertIsInstance(clone, AddressRefused)
                self.assertIs(clone.reason, Reason.UNKNOWN_ROLE)
                self.assertEqual(clone.token, "x.pair-planner")
                self.assertEqual(clone.canonical, "planner")
                self.assertEqual(str(clone), str(original))

    def test_all_names_the_interface(self):
        self.assertEqual(set(addresses.__all__), {
            "ROLE_WORDS", "RETIRED_ROLE_ALIASES", "SPECIAL_ADDRESSES",
            "RESERVED_SENDERS", "Reason", "AddressRefused", "Address",
            "parse_address", "parse_address_list", "render", "validate_name"})
        for name in addresses.__all__:
            self.assertTrue(hasattr(addresses, name), name)

    def test_render_refuses_an_address_parse_cannot_produce(self):
        for address in (Address(),
                        Address(team="x", role="wizard"),
                        Address(team="x", role="planner", special="operator"),
                        Address(special="system"),
                        Address(team="X", role="planner"),
                        Address(team="x", role="planner", project=""),
                        Address(team=1, role="planner")):
            with self.subTest(address=address):
                with self.assertRaises(ValueError) as caught:
                    render(address)
                self.assertNotIsInstance(caught.exception, AddressRefused)


if __name__ == "__main__":
    unittest.main()
