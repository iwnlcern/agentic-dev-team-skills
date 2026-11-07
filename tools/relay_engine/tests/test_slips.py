import dataclasses
import importlib
import os
import unittest
from contextlib import ExitStack
from unittest import mock

from relay_engine.guardrail import EFFECT_CLASSES


class TestValues(unittest.TestCase):
    def values(self):
        try:
            return importlib.import_module("relay_engine.slips")
        except ModuleNotFoundError as error:
            if error.name != "relay_engine.slips":
                raise
            self.fail("reviewed Answer/Effect constructors are not implemented")

    def test_answer_valid_outcomes(self):
        Answer = self.values().Answer
        self.assertEqual(tuple(f.name for f in dataclasses.fields(Answer)),
                         ("outcome", "code", "reason", "unblocker", "slip_id", "revision"))
        cases = [("allow", None, None, None, None, None),
                 ("allow", None, "reviewed", "operator", "fake-slip", 0),
                 ("deny", "authority.slip-missing", "No slip", "fake.planner", None, None),
                 ("held", "authority.plan-changed", "Changed plan", "fake.planner", "fake-slip", 3)]
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertEqual(dataclasses.astuple(Answer(*fields)), fields)

    def test_answer_unknown_outcome_rejected(self):
        Answer = self.values().Answer
        for outcome in ("unknown", "", None, [], 1):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                Answer(outcome, None, None, None, None, None)
        for outcome in ("allow", "deny", "held"):
            code = None if outcome == "allow" else "authority.synthetic"
            self.assertEqual(Answer(outcome, code, "reason", "operator", None, None).outcome, outcome)

    def test_answer_nonallow_details_required(self):
        Answer = self.values().Answer
        for outcome in ("deny", "held"):
            valid = [outcome, "authority.synthetic", "reason", "operator", None, None]
            self.assertEqual(dataclasses.astuple(Answer(*valid)), tuple(valid))
            for index in (1, 2, 3):
                for bad in (None, "", 1, []):
                    fields = valid.copy()
                    fields[index] = bad
                    with self.subTest(outcome=outcome, index=index, bad=bad), self.assertRaises(ValueError):
                        Answer(*fields)
        for code in ("authority.synthetic", "", 1):
            with self.subTest(code=code), self.assertRaises(ValueError):
                Answer("allow", code, None, None, None, None)

    def test_answer_slip_identity_is_paired(self):
        Answer = self.values().Answer
        for identity in ((None, 1), ("fake-slip", None), ("", 1), (4, 1),
                         ("fake-slip", "1"), ("fake-slip", 1.5), ("fake-slip", True)):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                Answer("allow", None, None, None, *identity)
        for identity in ((None, None), ("fake-slip", -2), ("fake-slip", 0), ("fake-slip", 7)):
            for outcome in ("allow", "deny", "held"):
                code = None if outcome == "allow" else "authority.synthetic"
                answer = Answer(outcome, code, "reason", "operator", *identity)
                self.assertEqual((answer.slip_id, answer.revision), identity)

    def test_effect_closed_kinds(self):
        Effect = self.values().Effect
        for kind in EFFECT_CLASSES:
            self.assertEqual(Effect(kind, None).kind, kind)
        for kind in ("unknown", "", None, [], 1):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                Effect(kind, None)

    def test_effect_optional_context_is_passive(self):
        Effect = self.values().Effect
        self.assertEqual(tuple(f.name for f in dataclasses.fields(Effect)),
                         ("kind", "repository_id", "ref", "pr", "head", "path"))
        self.assertEqual(dataclasses.astuple(Effect("commit", None)),
                         ("commit", None, None, None, None, None))
        fields = ("pr-merge", "fake repository", "refs/heads/fake", -1, "opaque head", "/fake/missing")
        self.assertEqual(dataclasses.astuple(Effect(*fields)), fields)
        self.assertEqual(dataclasses.astuple(Effect("commit", "")),
                         ("commit", "", None, None, None, None))

    def test_value_objects_are_immutable(self):
        values = self.values()
        for cls, args, field, replacement in (
                (values.Answer, ("allow", None, None, None, None, None), "reason", "changed"),
                (values.Effect, ("commit", None), "path", "/fake/changed")):
            first, second = cls(*args), cls(*args)
            self.assertIsNot(first, second)
            self.assertEqual(first, second)
            changed = dataclasses.replace(first, **{field: replacement})
            self.assertNotEqual(first, changed)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(first, field, replacement)
            self.assertEqual(first, second)

    def test_constructors_do_no_io(self):
        values = self.values()  # Ordinary import is outside the constructor guard.

        class NoEnvironment(dict):
            def forbidden(self, *args, **kwargs):
                raise AssertionError("constructor read environment")
            __getitem__ = __contains__ = __iter__ = __len__ = get = copy = forbidden

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "environ", NoEnvironment()))
            for target in ("builtins.open", "io.open", "os.open", "os.fdopen", "os.read",
                           "os.write", "os.close", "os.stat", "os.lstat", "os.fstat", "os.getenv",
                           "os.system", "os.popen", "subprocess.run", "subprocess.Popen",
                           "time.time", "time.monotonic", "time.perf_counter"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("constructor performed I/O")))
            answer = values.Answer("held", "authority.synthetic", "reason", "operator", "fake-slip", 2)
            effect = values.Effect("commit", "fake-repository", path="/fake/missing")
            with self.assertRaises(ValueError):
                values.Answer("unknown", None, None, None, None, None)
            with self.assertRaises(ValueError):
                values.Effect("unknown", None)
        self.assertEqual(dataclasses.astuple(answer),
                         ("held", "authority.synthetic", "reason", "operator", "fake-slip", 2))
        self.assertEqual(dataclasses.astuple(effect),
                         ("commit", "fake-repository", None, None, None, "/fake/missing"))
