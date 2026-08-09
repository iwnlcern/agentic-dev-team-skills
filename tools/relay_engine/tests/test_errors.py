import contextlib
import io
import os
import subprocess
import sys
import unittest

from relay_engine import errors, strings
from relay_engine.jcs import jcs_encode


POLICY_CODES = {
    "E-KEY-MISMATCH",
    "E-ID-COLLISION",
    "E-SUPERSEDED",
    "E-PATH-ESCAPE",
}

ALL_CODES = POLICY_CODES | {
    "E-HEADER",
    "E-ENVELOPE",
    "E-REPLAY-MISMATCH",
    "E-STORAGE",
    "E-DAEMON-DOWN",
    "seat-occupied",
    "commission-conflict",
    "commission-late",
    "run-id-mismatch",
    "run-id-uninitialized",
    "run-id-invalid",
    "E-FRAMING",
    "E-WIRE-VERSION",
    "E-WIRE-OP",
    "E-WIRE-ARGS",
    "E-DAEMON-STOPPING",
}


class TestErrorRegistry(unittest.TestCase):
    def test_complete_registry_and_exact_policy_partition(self):
        self.assertEqual(set(errors.ERRORS), ALL_CODES)
        self.assertEqual(errors.POLICY_CODES, POLICY_CODES)
        for code, spec in errors.ERRORS.items():
            self.assertIn(spec.cls, {"policy", "integrity", "client",
                                     "registration", "command", "wire"})
            self.assertIn(spec.cause_key, strings.INVENTORY)
            self.assertIn(spec.remedy_key, strings.INVENTORY)
            self.assertTrue(strings.INVENTORY[spec.cause_key])
            self.assertTrue(strings.INVENTORY[spec.remedy_key])

    def test_commissioning_refusals_are_command_class(self):
        for code in ("commission-conflict", "commission-late",
                     "run-id-mismatch", "run-id-uninitialized",
                     "run-id-invalid"):
            self.assertEqual(errors.ERRORS[code].cls, "command")

    def test_raw_string_error_construction_is_rejected(self):
        with self.assertRaises(KeyError):
            errors.EngineError("E-FRAMING", "raw cause", "raw remedy",
                               "wire", reason="truncated")


class TestWireTemplates(unittest.TestCase):
    def test_wire_error_objects_byte_exact(self):
        rv = strings.rejected_value("0123456789ab", 7)
        cases = (
            ("E-FRAMING", {"reason": "truncated"},
             {"code": "E-FRAMING",
              "cause": "unparseable frame: truncated",
              "remedy": "send one complete frame: 4-byte big-endian length prefix + UTF-8 JSON",
              "cls": "wire"}),
            ("E-WIRE-VERSION", {"rejected_version": rv},
             {"code": "E-WIRE-VERSION",
              "cause": "unsupported protocol version unrecognized-input (sha256:0123456789ab, length 7)",
              "remedy": "send v:1", "cls": "wire"}),
            ("E-WIRE-OP", {"rejected_op": rv},
             {"code": "E-WIRE-OP",
              "cause": "unknown op unrecognized-input (sha256:0123456789ab, length 7)",
              "remedy": "use an op from the daemon op table",
              "cls": "wire"}),
            ("E-WIRE-ARGS", {"op": "submit",
                              "detail": "body_b64:base64"},
             {"code": "E-WIRE-ARGS",
              "cause": "args schema violation for submit: body_b64:base64",
              "remedy": "match the op's exact args schema",
              "cls": "wire"}),
            ("E-DAEMON-STOPPING", {},
             {"code": "E-DAEMON-STOPPING",
              "cause": "daemon is draining its stop barrier",
              "remedy": "retry after restart; replay semantics make the retry safe",
              "cls": "wire"}),
        )
        for code, params, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(errors.error_for(code, **params).as_dict(),
                                 expected)

    def test_every_template_placeholder_has_one_validator(self):
        expected = set()
        for key, template in strings.INVENTORY.items():
            for field in strings._placeholders(template):
                expected.add((key, field))
        self.assertEqual(set(strings.VALIDATOR_IDENTITIES), expected)

    def test_unknown_wire_values_cannot_echo_client_text(self):
        for value in ("database", "SQLite", "ledger", "credential",
                      "token"):
            digest = __import__("hashlib").sha256(value.encode()).hexdigest()[:12]
            rendered = errors.error_for(
                "E-WIRE-OP",
                rejected_op=strings.rejected_value(digest, len(value)),
            ).as_dict()["cause"]
            self.assertNotIn(value.lower(), rendered.lower())


class TestRedactionOracle(unittest.TestCase):
    def test_escaped_equivalents_one_digest(self):
        import hashlib
        first = jcs_encode(__import__("json").loads('"a"'))
        second = jcs_encode(__import__("json").loads('"\\u0061"'))
        self.assertEqual(first, b'"a"')
        self.assertEqual(second, b'"a"')
        self.assertEqual(hashlib.sha256(first).hexdigest()[:12],
                         "ac8d8342bbb2")

    def test_cross_type_rendering_byte_exact(self):
        import hashlib
        cases = (
            ("a", b'"a"', "ac8d8342bbb2"),
            (1, b"1", "6b86b273ff34"),
            (True, b"true", "b5bea41b6c62"),
            (None, b"null", "74234e98afe7"),
            ([1, "a"], b'[1,"a"]', "2010945388e2"),
            ({"a": 1}, b'{"a":1}', "015abd7f5cc5"),
        )
        for value, encoded, digest in cases:
            with self.subTest(value=value):
                actual = jcs_encode(value)
                self.assertEqual(actual, encoded)
                self.assertEqual(hashlib.sha256(actual).hexdigest()[:12],
                                 digest)
                self.assertEqual(
                    str(strings.rejected_value(digest, len(encoded))),
                    "unrecognized-input (sha256:%s, length %d)" %
                    (digest, len(encoded)),
                )


class TestDiagnosticAndEmission(unittest.TestCase):
    def tearDown(self):
        strings.set_diagnostic_sink(None)

    def test_report_diagnostic_passes_exception_object_unchanged(self):
        seen = []
        strings.set_diagnostic_sink(seen.append)
        exc = RuntimeError("database is locked")
        self.assertIsNone(strings.report_diagnostic(exc))
        self.assertEqual(seen, [exc])

    def test_default_diagnostic_sink_is_silent(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            strings.report_diagnostic(RuntimeError("database is locked"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_keyed_emit_and_verbatim_channel(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            strings.emit("stdout", "explain-heading", code="E-KEY-MISMATCH")
        self.assertEqual(out.getvalue(), "E-KEY-MISMATCH\n")
        raw = io.BytesIO()
        strings.emit_verbatim(raw, b"body\x00bytes\n")
        self.assertEqual(raw.getvalue(), b"body\x00bytes\n")

    def test_free_text_key_or_unvalidated_parameter_is_rejected(self):
        with self.assertRaises(KeyError):
            strings.render("not-in-the-inventory")
        with self.assertRaises(ValueError):
            strings.render("explain-heading", code="database")


class TestExplainCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = os.path.dirname(os.path.dirname(os.path.dirname(
            __file__)))
        cls.relay = os.path.join(cls.tools, "relay")

    def run_relay(self, *args):
        return subprocess.run([sys.executable, self.relay, *args],
                              capture_output=True, text=True)

    def test_explain_known_code(self):
        out = self.run_relay("explain", "E-KEY-MISMATCH")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("E-KEY-MISMATCH", out.stdout)
        self.assertIn("registration tag", out.stdout)
        self.assertEqual(out.stderr, "")

    def test_explain_unknown_code_is_usage(self):
        out = self.run_relay("explain", "NO-SUCH-CODE")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout, "")
        self.assertNotIn("NO-SUCH-CODE", out.stderr)

    def test_daemon_down_escalation_sentence_is_pinned(self):
        self.assertEqual(
            errors.E_DAEMON_DOWN_ESCALATION,
            "Hand-relay this escalation to the eligible starter: master-planner if a master tier exists (never the orchestrator session), else orchestrator-planner, else the operator. This escalation is the sole exception to daemon admission.",
        )


if __name__ == "__main__":
    unittest.main()
