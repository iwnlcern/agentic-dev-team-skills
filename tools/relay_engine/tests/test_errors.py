import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest

from relay_engine import client, errors, strings
from relay_engine.paths import Root
from relay_engine.jcs import jcs_encode


POLICY_CODES = {
    "E-KEY-MISMATCH",
    "E-ID-COLLISION",
    "E-SUPERSEDED",
    "E-PATH-ESCAPE",
    "E-VERSION-MISMATCH",
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
    "E-CONTEXT-BUDGET",
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
            self.assertIn(spec.explain_key, strings.INVENTORY)
            self.assertTrue(strings.INVENTORY[spec.cause_key])
            self.assertTrue(strings.INVENTORY[spec.remedy_key])
            self.assertTrue(strings.INVENTORY[spec.explain_key])
            self.assertEqual(strings._placeholders(
                strings.INVENTORY[spec.explain_key]), ())

    def test_commissioning_refusals_are_command_class(self):
        for code in ("commission-conflict", "commission-late",
                     "run-id-mismatch", "run-id-uninitialized",
                     "run-id-invalid"):
            self.assertEqual(errors.ERRORS[code].cls, "command")

    def test_raw_string_error_construction_is_rejected(self):
        with self.assertRaises(KeyError):
            errors.EngineError("E-FRAMING", "raw cause", "raw remedy",
                               "wire", reason="truncated")

    def test_not_found_variant_is_registered_and_renders(self):
        exc = errors.error_for("E-PATH-ESCAPE", variant="not-found",
                               field="draft", rel="x/y.md")
        self.assertEqual(exc.code, "E-PATH-ESCAPE")
        self.assertEqual(exc.cls, "policy")
        self.assertIn("draft", exc.cause)
        self.assertIn("x/y.md", exc.cause)
        self.assertIn("root-relative", exc.remedy)

    def test_not_found_rel_rejects_line_separators(self):
        for rel in ("a\nb.md", "a\rb.md"):
            with self.subTest(rel=repr(rel)):
                exc = errors.error_for("E-PATH-ESCAPE", variant="not-found",
                                       field="key", rel=rel)
                self.assertNotIn("\n", exc.cause)
                self.assertNotIn("\r", exc.cause)
                self.assertIn("sha256", exc.cause)

    def test_path_escape_regression_unchanged(self):
        with tempfile.TemporaryDirectory() as root_name:
            with Root(root_name) as root:
                with self.assertRaises(errors.EngineError) as ctx:
                    client._relative(root, "/outside/abs/path.md")
        self.assertEqual(
            ctx.exception.as_dict(),
            {"code": "E-PATH-ESCAPE",
             "cause": "draft path is outside the canonical root",
             "remedy": "use the canonical drafts location",
             "cls": "policy"},
        )

    def test_id_collision_remedy_names_the_flag_argument(self):
        exc = errors.error_for("E-ID-COLLISION")
        self.assertIn("--admits-against", exc.remedy)
        self.assertIn("root-relative", exc.remedy)

    def test_context_budget_error_is_a_registered_wire_refusal(self):
        self.assertEqual(
            errors.error_for("E-CONTEXT-BUDGET").as_dict(),
            {"code": "E-CONTEXT-BUDGET",
             "cause": "context page exceeded the byte budget",
             "remedy": "a single record entry or accounting disagreement exceeded the per-page byte budget",
             "cls": "wire"},
        )


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
              "remedy": "send v:2", "cls": "wire"}),
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

    def test_wire_version_explain_uses_v2_contract(self):
        explain, remedy = errors.explain_for("E-WIRE-VERSION")
        self.assertEqual(explain, "wire requests use protocol version two")
        self.assertEqual(remedy, "send v:2")

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


class TestIdentityValidators(unittest.TestCase):
    def test_install_display_grammar_has_named_adversarial_outcomes(self):
        cases = (
            ("ASCII control", "/Users/Jane/\x1fskills", False),
            ("U+0085", "/Users/Jane/\u0085skills", False),
            ("U+202E", "/Users/Jane/\u202eskills", False),
            ("non-ASCII letters", "/Users/José/skills", True),
            ("noncanonical", "/a/../b", True),
            ("relative", "Users/Jane/skills", False),
            ("over 512 bytes", "/" + ("a" * 512), False),
        )
        for name, value, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(strings.valid_install(value), expected)

    def test_kit_grammar_is_canonical_and_bounded(self):
        cases = (
            ("short", "2.9", False),
            ("prefix", "v2.9.1", False),
            ("four tuple", "2.9.1.0", False),
            ("leading zeros", "02.009.0001", False),
            ("empty", "", False),
            ("release", "2.9.1", True),
            ("zero", "0.0.0", True),
            ("maximum component", "999999.0.1", True),
        )
        for name, value, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(strings.valid_kit(value), expected)

    def test_fingerprint_grammar_is_lowercase_64_hex_bytes(self):
        cases = (
            ("valid", "a" * 64, True),
            ("uppercase", "A" * 64, False),
            ("63 bytes", "a" * 63, False),
            ("65 bytes", "a" * 65, False),
        )
        for name, value, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(strings.valid_fp(value), expected)


class TestVersionMismatch(unittest.TestCase):
    CLIENT_INSTALL = "/Users/José/skills"
    DAEMON_INSTALL = "/a/../b"
    CLIENT_FP = "a" * 64
    DAEMON_FP = "b" * 64

    def mismatch(self, client_kit, daemon_kit, **overrides):
        params = {
            "client_install": self.CLIENT_INSTALL,
            "client_kit": client_kit,
            "client_fp": self.CLIENT_FP,
            "daemon_install": self.DAEMON_INSTALL,
            "daemon_kit": daemon_kit,
            "daemon_fp": self.DAEMON_FP,
        }
        params.update(overrides)
        return errors.error_for("E-VERSION-MISMATCH", **params)

    def test_remedy_names_the_client_install_when_client_kit_is_lower(self):
        exc = self.mismatch("2.9.0", "2.9.1")
        self.assertEqual(
            exc.remedy,
            "update the client install at /Users/José/skills, then retry",
        )

    def test_remedy_names_the_daemon_install_when_daemon_kit_is_lower(self):
        exc = self.mismatch("2.9.2", "2.9.1")
        self.assertEqual(
            exc.remedy,
            "update the daemon install at /a/../b, then retry",
        )

    def test_remedy_refreshes_both_installs_when_attribution_is_indeterminate(self):
        exc = self.mismatch("2.9.1", "2.9.1")
        self.assertEqual(
            exc.remedy,
            "refresh both installs: client at /Users/José/skills; daemon at /a/../b; then retry",
        )

    def test_malformed_identity_values_render_only_as_rejected_values(self):
        exc = self.mismatch(
            "02.009.0001",
            "2.9.1",
            client_install="/Users/Jane/\u202eskills",
            client_fp="A" * 64,
        )
        rendered = "\n".join((exc.cause, exc.remedy))
        self.assertIn("unrecognized-input (sha256:", rendered)
        self.assertNotIn("02.009.0001", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("A" * 64, rendered)
        self.assertRegex(
            exc.remedy,
            r"^refresh both installs: client at unrecognized-input "
            r"\(sha256:[0-9a-f]{12}, length [0-9]+\); daemon at "
            r"/a/\.\./b; then retry$",
        )

    def test_registry_rejects_nested_remedy_values_that_could_echo_raw_identity(self):
        cases = (
            errors._VersionMismatchRemedy(
                "client", "/bad\nRAW-INSTALL", self.DAEMON_INSTALL),
            errors._VersionMismatchRemedy(
                "daemon", self.CLIENT_INSTALL, "/bad\nRAW-INSTALL"),
            errors._VersionMismatchRemedy(
                "attacker", self.CLIENT_INSTALL, self.DAEMON_INSTALL),
        )
        for remedy in cases:
            with self.subTest(remedy=remedy):
                with self.assertRaises(ValueError):
                    strings.render("error-version-mismatch-remedy",
                                   remedy=remedy)

    def test_registry_rejects_direct_rejected_values_without_digest_invariants(self):
        common = {
            "client_install": self.CLIENT_INSTALL,
            "client_kit": "2.9.1",
            "client_fp": self.CLIENT_FP,
            "daemon_install": self.DAEMON_INSTALL,
            "daemon_kit": "2.9.1",
            "daemon_fp": self.DAEMON_FP,
        }
        cases = (
            strings.RejectedValue("bad\nRAW-DIGEST", 1),
            strings.RejectedValue("a" * 12, -1),
            strings.RejectedValue("a" * 12, True),
        )
        for rejected in cases:
            with self.subTest(rejected=rejected):
                params = dict(common, client_install=rejected)
                with self.assertRaises(ValueError):
                    strings.render("error-version-mismatch-cause", **params)


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

    def test_every_registered_code_is_explainable_without_incident_values(self):
        for code in sorted(ALL_CODES):
            with self.subTest(code=code):
                out = self.run_relay("explain", code)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(code, out.stdout)
                self.assertEqual(out.stderr, "")

    def test_daemon_down_escalation_sentence_is_pinned(self):
        self.assertEqual(
            errors.E_DAEMON_DOWN_ESCALATION,
            "Hand-relay this escalation to the eligible starter: master-planner if a master tier exists (never the orchestrator session), else orchestrator-planner, else the operator. This escalation is the sole exception to daemon admission.",
        )


if __name__ == "__main__":
    unittest.main()
