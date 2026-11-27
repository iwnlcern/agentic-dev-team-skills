import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

from relay_engine import client, errors, strings
from relay_engine.paths import Root
from relay_engine.jcs import jcs_encode


POLICY_CODES = {
    "E-SETTING-OPERATOR-ONLY",
    "E-JEV-ACT-NEEDS-RECORD",
    "E-DELIVERY-INTERVAL-MARGIN",
    "E-KEY-MISMATCH",
    "E-ID-COLLISION",
    "E-SUPERSEDED",
    "E-PATH-ESCAPE",
    "E-VERSION-MISMATCH",
    "E-SENDER-NOT-CURRENT",
}

ALL_CODES = POLICY_CODES | {
    'E-AUTHORITY-PROJECT-STOPPED',
    'E-AUTHORITY-CROSS-PROJECT-COMMIT',
    'E-AUTHORITY-SESSION-NOT-CURRENT',
    'E-AUTHORITY-PROTECTED-REF-PUSH',
    'E-AUTHORITY-API-MERGE-REFUSED',
    'E-AUTHORITY-MERGE-HEAD-UNPINNED',
    'E-AUTHORITY-MERGE-FLAG-REFUSED',
    'E-AUTHORITY-HOOK-BYPASS',
    'E-AUTHORITY-HOOK-DISABLE',
    'E-AUTHORITY-KEY-FILE-READ',
    'E-AUTHORITY-OPERATOR-ENDPOINT',
    'E-AUTHORITY-OPERATOR-ONLY',
    'E-AUTHORITY-CHECKOUT-UNREGISTERED',

    "E-SETTING-UNKNOWN",
    "E-SETTING-VALUE",
    "E-AUTHORITY-SLIP-MISSING",
    "E-AUTHORITY-SLIP-INVALID",
    "E-AUTHORITY-LINEAGE-INCOMPLETE",
    "E-AUTHORITY-NOT-ISSUER",
    "E-AUTHORITY-PLAN-CHANGED",
    "E-AUTHORITY-DESIGN-CHANGED",
    "E-AUTHORITY-VERIFICATION-PENDING",
    "E-AUTHORITY-VERIFICATION-OBJECTED",
    "E-AUTHORITY-CHECK-UNAVAILABLE",
    "E-AUTHORITY-RESTORE-HELD",
    "E-AUTHORITY-NO-BUNDLE",

    "E-SNAPSHOT-STALE",
    "E-SNAPSHOT-NOT-TO",
    "E-SNAPSHOT-NO-REF",
    "E-SNAPSHOT-NO-BYTES",
    "E-SNAPSHOT-NOT-TEXT",
    "E-OUTPUT-EXISTS",
    "E-OUTPUT-OWNERSHIP",

    "E-SUBMISSION-KEY-REUSED",
    "E-BUNDLE-PARENT-HAS-BUNDLE",
    "E-BUNDLE-PARENT-NOT-IMPORTED",
    "E-BUNDLE-NAME-TAKEN",
    "E-ADMISSION-HOOK",
    "E-ADMISSION-COMMIT",
    "E-ADMISSION-UNKNOWN",
    "E-STORE-BUSY",
    "no-top-seat",
    "E-HEADER",
    "E-DRAFT-SHAPE",
    "E-ENVELOPE",
    "E-REPLAY-MISMATCH",
    "E-STORAGE",
    "E-DAEMON-DOWN",
    "E-NO-ROOT",
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
    "E-HEADER-GRAMMAR",
    "E-HEADER-UNKNOWN-KEY",
    "E-HEADER-DUPLICATE",
    "E-HEADER-EMPTY",
    "E-HEADER-CONTROL",
    "E-HEADER-TITLE-LINE",
    "E-HEADER-CRLF",
    "E-HEADER-BOM",
    "E-HEADER-SIZE",
    "E-BODY-SIZE",
    "E-FIELD-ENUM",
    "E-FIELD-REQUIRED",
    "E-FIELD-FORBIDDEN",
    "E-FIELD-LENGTH",
    "E-ADDRESS",
    "E-FINDING-NO-CHECK",
    "E-FIELD-SHAPE",
    "E-DRAFT-UTF8",
    "E-QUESTION-ADDRESS",
    "E-SQLITE-VERSION",
    "E-SCHEMA-MISMATCH",
    "E-PARENT-UNKNOWN", "E-REPO-UNKNOWN",
    "E-PARENT-NOT-MEMBER",
    "E-ANSWERS-UNKNOWN",
    "E-ANSWERS-NOT-ADDRESSED",
    "E-ANSWERS-UNOPENED",
    "E-ANSWERS-REPEATED",
    "E-FINDING-UNKNOWN",
    "E-RESOLVE-NOT-RAISER",
    "E-RESOLVE-STATE",
    "E-REF-SPELLING",
    "E-REF-UNRESOLVED",
    "E-REF-AMBIGUOUS",
    "E-REF-RANGE",
    "E-REF-TIMEOUT",
    "E-REF-GIT-ENVIRONMENT",
    "E-FILE-MISSING",
    "E-ROOT-NOT-FOUND",
    "E-CARRIER-INVALID",
    "E-CARRIER-FIELD",
}


class TestErrorRegistry(unittest.TestCase):
    def test_setting_value_explain_names_every_range(self):
        # Breaks: incomplete setting advice, including a setting registered later.
        from relay_engine import queries
        text = errors.explain_for("E-SETTING-VALUE")[0]
        self.assertEqual(text, (
            "jev_record and jev_act require booleans, doorbell_off_minutes requires an integer from 1 to 1440, "
            "and delivery_interval_seconds requires an integer from 0 to 600"))
        for key, spec in queries.SETTINGS.items():
            self.assertIn(key, text)
            if spec.minimum is not None:
                self.assertIn("from %s to %s" % (spec.minimum, spec.maximum), text)

    def test_repo_unknown_code(self):
        # Breaks: unknown repository is absent, misclassified or gives the wrong remedy.
        self.assertIn("E-REPO-UNKNOWN", errors.ERRORS)
        exc = errors.error_for("E-REPO-UNKNOWN", line=7, value="missing")
        self.assertEqual(exc.cls, "integrity")
        self.assertEqual(exc.cause, "line 7: REPO missing names no repository of this project")
        self.assertEqual(exc.remedy, "name a repository this project has registered, by its name; a path or a URL is not a name")
        self.assertEqual(errors.explain_for("E-REPO-UNKNOWN")[0], "REPO names the repository a go-ahead is for, among the sender's own project's repositories")

    def test_store_busy_code(self):
        # Breaks: an unregistered/misclassified busy answer or unsafe resend advice.
        self.assertIn("E-STORE-BUSY", errors.ERRORS)
        error = errors.error_for("E-STORE-BUSY")
        self.assertEqual(error.cls, "integrity")
        self.assertEqual(error.cause, "the store was busy and this attempt could not finish; this attempt committed nothing")
        self.assertEqual(error.remedy, "send the same draft again with the same submission key")
        self.assertEqual(errors.explain_for("E-STORE-BUSY"), (
            "a busy store stops an attempt before it commits, and an earlier attempt under the same key is answered by its receipt, so sending the same draft again is safe",
            "send the same draft again with the same submission key"))

    def test_setting_codes(self):
        codes = {"E-SETTING-OPERATOR-ONLY": "policy", "E-JEV-ACT-NEEDS-RECORD": "policy",
                 "E-SETTING-UNKNOWN": "command", "E-SETTING-VALUE": "command",
                 "E-DELIVERY-INTERVAL-MARGIN": "policy"}
        self.assertTrue(set(codes) <= set(errors.ERRORS))
        from relay_engine import queries
        for code, kind in codes.items():
            with self.subTest(code=code):
                exc = errors.error_for(code)
                self.assertEqual((exc.code, exc.cls), (code, kind))
                self.assertTrue(exc.cause)
                self.assertTrue(exc.remedy)
                self.assertTrue(errors.explain_for(code)[0])
                if code in {"E-SETTING-UNKNOWN", "E-SETTING-VALUE"}:
                    for key in queries.SETTINGS:
                        self.assertIn(key, exc.remedy)
                    for fixed in ("bool", "int", "1", "1440", "0", "600"):
                        self.assertIn(fixed, exc.remedy)


    def test_snapshot_codes_catalogued(self):
        cases = {"E-SNAPSHOT-STALE": 'integrity',
                 "E-SNAPSHOT-NOT-TO": 'integrity',
                 "E-SNAPSHOT-NO-REF": 'integrity',
                 "E-SNAPSHOT-NO-BYTES": 'command',
                 "E-SNAPSHOT-NOT-TEXT": 'command',
                 "E-OUTPUT-EXISTS": 'command',
                 "E-OUTPUT-OWNERSHIP": 'command'}
        for code, kind in cases.items():
            with self.subTest(code=code):
                self.assertIn(code, errors.ERRORS)
                spec = errors.ERRORS[code]
                self.assertEqual(spec.cls, kind)
                self.assertTrue(strings.INVENTORY[spec.cause_key])
                self.assertTrue(strings.INVENTORY[spec.remedy_key])

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

    def test_sender_not_current_code(self):
        self.assertIn("E-SENDER-NOT-CURRENT", errors.ERRORS)
        self.assertEqual(errors.ERRORS["E-SENDER-NOT-CURRENT"].cls, "policy")
        for reason in ("key_replaced", "seat_stood_down", "session_not_current"):
            error = errors.error_for("E-SENDER-NOT-CURRENT", reason=reason)
            self.assertEqual((error.code, error.cls), ("E-SENDER-NOT-CURRENT", "policy"))
            self.assertIn(reason, error.cause)
            self.assertTrue(error.remedy)
        for reason in ("other", "", None, ["key_replaced"]):
            with self.assertRaises(ValueError, msg=repr(reason)):
                errors.error_for("E-SENDER-NOT-CURRENT", reason=reason)
        explanation = errors.explain_for("E-SENDER-NOT-CURRENT")[0]
        for reason in ("key_replaced", "seat_stood_down", "session_not_current"):
            self.assertIn(reason, explanation)

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


class TestAdmissionErrors(unittest.TestCase):
    """Breaks: unregistered replay refusal or changed legacy error envelope."""

    def test_imported_parent_bundle_error_catalog(self):
        cases = (
            ("E-BUNDLE-PARENT-HAS-BUNDLE", 3, "#2", "line 3: parent #2 already has a bundle",
             "remove BUNDLE to inherit the parent's bundle"),
            ("E-BUNDLE-PARENT-NOT-IMPORTED", 3, "#2",
             "line 3: parent #2 has no committed import provenance",
             "remove BUNDLE or name a selectable imported relay"),
            ("E-BUNDLE-NAME-TAKEN", 4, "imported-review",
             "line 4: bundle imported-review already exists in this project",
             "choose a fresh BUNDLE name and submit using the normal submission-key rules"),
        )
        for code, line, value, cause, remedy in cases:
            with self.subTest(code=code):
                error = errors.error_for(code, line=line, value=value)
                self.assertEqual((error.code, error.cls, error.cause, error.remedy),
                                 (code, "integrity", cause, remedy))
                self.assertEqual(error.params, {"line": line, "value": value})
                explanation, explained_remedy = errors.explain_for(code)
                self.assertTrue(explanation)
                self.assertEqual(explained_remedy, remedy)
                self.assertIn(code, strings._CODE_VALUES)
                self.assertNotIn(code, errors.POLICY_CODES)
                with self.assertRaisesRegex(ValueError, "invalid inventory parameter"):
                    errors.error_for(code, line=line, value="raw\nprivate text")

    def test_admission_outcome_codes(self):
        self.assertTrue({"E-ADMISSION-COMMIT", "E-ADMISSION-UNKNOWN"} <= set(errors.ERRORS))
        texts = {'error-admission-reservation-undone': 'what the admission hook {hook} reserved was undone by another writer, and the operator was told', 'error-admission-commit-cause': "the store did not complete this admission's commit; nothing from this draft was stored", 'error-admission-commit-remedy': 'submit this draft again', 'error-admission-commit-explain': 'a commit the store did not complete is rolled back, and the refusal written after it makes that rollback permanent, so the draft is refused as a whole', 'error-admission-unknown-cause': 'this draft was not stored, and whether what the admission hook {hook} reserved was released is not yet known', 'error-admission-unknown-commit-unconfirmed-cause': 'whether this draft was stored is not yet known: the store did not confirm its commit', 'error-admission-unknown-remedy': "resubmit this draft unchanged, under its original submission key, and not as a new draft or under a new key: if it was stored you receive its original receipt, and otherwise it is admitted once; if that is refused with authority.reservation-unresolved, the engine start's settlement has not committed yet, so resubmit once that refusal clears", 'error-admission-unknown-explain': "a draft resubmitted unchanged under its key returns a stored relay's receipt before anything is recorded again, or is admitted once; a reservation not released now is released by that admission or settled at the next engine start, which tells the operator"}
        hook = "fixture.module.hook"
        for code, stem in (("E-ADMISSION-COMMIT", "commit"),
                           ("E-ADMISSION-UNKNOWN", "unknown")):
            with self.subTest(code=code):
                error = errors.error_for(code, hook=hook)
                self.assertEqual(error.cls, "integrity")
                self.assertEqual(error.cause, texts["error-admission-" + stem + "-cause"].format(hook=hook))
                self.assertEqual(error.remedy, texts["error-admission-" + stem + "-remedy"])
                self.assertEqual(errors.explain_for(code),
                                 (texts["error-admission-" + stem + "-explain"], error.remedy))
                self.assertIn(code, strings._CODE_VALUES)
                self.assertNotIn(code, errors.POLICY_CODES)
        variant = errors.error_for("E-ADMISSION-UNKNOWN", variant="commit-unconfirmed", hook=hook)
        self.assertEqual(variant.cause, texts["error-admission-unknown-commit-unconfirmed-cause"])
        self.assertEqual(variant.remedy, errors.error_for("E-ADMISSION-UNKNOWN", hook=hook).remedy)
        self.assertEqual(errors.ERROR_VARIANTS[("E-ADMISSION-UNKNOWN", "commit-unconfirmed")].explain_key,
                         errors.ERRORS["E-ADMISSION-UNKNOWN"].explain_key)
        rejected = errors.error_for("E-ADMISSION-UNKNOWN", hook="not a hook:PRIVATE")
        self.assertIn("unrecognized-input (sha256:", rejected.cause)
        self.assertNotIn("PRIVATE", rejected.cause)

    def test_admission_outcome_texts_avoid_banned_words(self):
        # 7a60242 test_header.py:625-627,633 supplies the seven pattern groups.
        import re
        texts = [strings.INVENTORY[key] for key in ['error-admission-reservation-undone', 'error-admission-commit-cause', 'error-admission-commit-remedy', 'error-admission-commit-explain', 'error-admission-unknown-cause', 'error-admission-unknown-commit-unconfirmed-cause', 'error-admission-unknown-remedy', 'error-admission-unknown-explain']]
        texts += ["E-ADMISSION-COMMIT", "E-ADMISSION-UNKNOWN"]
        patterns = (r"sqlite", r"database", r"ledger", r"\bauth\b|\btoken\b",
                    r"fallback|authoring mode", r"credential|tamper-proof|prevents|guarantees exclusivity",
                    r"hand-relay")
        for text in texts:
            for pattern in patterns:
                with self.subTest(text=text, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text, re.I))

    def test_reservation_detail(self):
        error = errors.error_for("E-ADMISSION-HOOK", hook="fixture.module.hook", kind="RuntimeError")
        self.assertIsNone(error.reservation)
        original = error.as_dict()
        self.assertEqual(set(original), {"code", "cause", "remedy", "cls"})
        error.reservation = "undone-with-notice"
        self.assertEqual(error.as_dict(), dict(original, reservation="undone-with-notice",
                         reservation_note="what the admission hook fixture.module.hook reserved was undone by another writer, and the operator was told"))
        for value in ("voided", "", 1, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    error.reservation = value
        error.reservation = None
        self.assertEqual(error.as_dict(), original)

    def test_replay_error_catalog(self):
        self.assertIn("E-SUBMISSION-KEY-REUSED", errors.ERRORS,
                      "reused admission keys require their registered refusal")
        error = errors.error_for("E-SUBMISSION-KEY-REUSED")
        self.assertEqual(error.code, "E-SUBMISSION-KEY-REUSED")
        self.assertEqual(error.cls, "integrity")
        self.assertEqual(set(error.as_dict()), {"code", "cause", "remedy", "cls"})
        self.assertEqual(error.cause,
                         "this sender party already used the submission key for different draft bytes")
        self.assertEqual(error.remedy,
                         "retry the original bytes with this key, or use a new key for a new submission")
        self.assertEqual(errors.explain_for(error.code)[0],
                         "a sender party and submission key identify one exact full input and its original admission receipt")
        spec = errors.ERRORS[error.code]
        self.assertTrue(strings.INVENTORY[spec.explain_key])
        self.assertNotIn(error.code, errors.POLICY_CODES)



    def test_hook_error_catalog(self):
        self.assertIn("E-ADMISSION-HOOK", errors.ERRORS,
                      "hook faults need the registered integrity refusal and ended variant")
        error = errors.error_for("E-ADMISSION-HOOK", hook="fixture.module.hook", kind="RuntimeError")
        self.assertEqual(error.cls, "integrity")
        self.assertEqual(error.cause, "the admission hook fixture.module.hook failed with RuntimeError; nothing from this draft was stored")
        self.assertEqual(error.remedy, "report it to the orchestrator; the draft can be submitted again once the hook is fixed")
        self.assertEqual(errors.explain_for(error.code)[0], "admission and the hook's records commit together, so a failed hook refuses the whole draft rather than storing a relay without them")
        ended = errors.error_for("E-ADMISSION-HOOK", variant="ended-transaction", hook="fixture.module.hook")
        self.assertEqual(ended.cause, "the admission hook fixture.module.hook ended the transaction; nothing from this draft was stored")
        self.assertEqual(set(error.as_dict()), {"code", "cause", "remedy", "cls"})
        self.assertNotIn(error.code, errors.POLICY_CODES)
        self.assertEqual({spec.cls for spec in errors.ERRORS.values()},
                         {"policy", "integrity", "client", "registration", "command", "wire"})
        for params in ({"hook": "x\nPRIVATE", "kind": "RuntimeError"},
                       {"hook": "x.hook", "kind": "RuntimeError:PRIVATE"},
                       {"hook": "", "kind": "RuntimeError"},
                       {"hook": "x.hook", "kind": ""}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    errors.error_for("E-ADMISSION-HOOK", **params)

class TestApiAdmissionCatalog(unittest.TestCase):
    def test_api_admission_cause_fields(self):
        from relay_engine import api_definitions as d
        catalog = errors.api_error_catalog()
        variants = {variant.code: variant for variant in catalog.variants}
        self.assertIn('E_API_ADMISSION', variants)
        variant = variants['E_API_ADMISSION']
        self.assertEqual(variant.api_class, 'admission')
        types = {node.name: node for node in catalog.types}
        cause = types[variant.cause_ref.name].shape
        self.assertEqual(len(cause.arms), 6)
        for ref in cause.arms:
            record = types[ref.name].shape
            self.assertIsInstance(record, d.Record)
            self.assertEqual(tuple(field.name for field in record.fields), ('kind', 'engine_code'))
            self.assertFalse(any(field.optional for field in record.fields))
    def test_revision_mismatch_has_closed_wire_payload(self):
        from relay_engine import api_protocol, api_wire
        registry = api_protocol.discovery_registry()
        error = api_protocol.api_error(registry, 'revision_mismatch',
                                       current_revision='2', current_state='retired')
        self.assertEqual(error, {
            'code': 'E_QUEUE_STATE', 'class': 'conflict', 'remedy': 'refresh_item',
            'cause': {'kind': 'revision_mismatch', 'current_revision': '2',
                      'current_state': 'retired'},
        })
        api_wire.encode_api_response({'request_id': '1', 'error': error}, registry, None, None)
        for extra in ({'current_revision': 2}, {'text': 'private item'}):
            malformed = dict(error, cause=dict(error['cause'], **extra))
            with self.assertRaisesRegex(ValueError, 'invalid_response'):
                api_wire.encode_api_response({'request_id': '1', 'error': malformed},
                                             registry, None, None)

if __name__ == "__main__":
    unittest.main()


class TestAuthorityCatalogueR22(unittest.TestCase):
    def test_authority_catalogue_r22(self):
        rows = [['E-AUTHORITY-PROJECT-STOPPED', 'this project is stopped', 'start the project, then commit again', 'while a project is stopped no agent of it commits, whatever its role'], ['E-AUTHORITY-CROSS-PROJECT-COMMIT', 'this session is a seat of another project', 'commit from a seat of this project', "a seat commits only in its own project's checkouts"], ['E-AUTHORITY-SESSION-NOT-CURRENT', 'this session no longer holds its seat', "sign this session in as its seat, or commit from the seat's current session", "the reason names why: a newer sign-in, the operator's unseat, a sign-in that never committed, a replaced occupant, a stood-down seat, a closed team, or a project that could not be read"], ['E-AUTHORITY-PROTECTED-REF-PUSH', 'a seat may not push to a protected branch', 'merge through the pinned PR merge', 'protected branches change only by a pinned merge'], ['E-AUTHORITY-API-MERGE-REFUSED', 'a seat may not write a protected branch through the API', 'use the pinned PR merge', 'protected branches change only by a pinned merge'], ['E-AUTHORITY-MERGE-HEAD-UNPINNED', "this merge does not pin the pull request's head", 'merge with the head your merge slip names', 'a merge slip allows one head'], ['E-AUTHORITY-MERGE-FLAG-REFUSED', '--auto and --admin merges are refused', 'merge without the flag, under your merge slip', 'a flagged merge lands at a time or head no slip checked'], ['E-AUTHORITY-HOOK-BYPASS', "a seat may not skip the kit's hooks for a command", 'run the command without the bypass', "the hooks check every seat's commit and push"], ['E-AUTHORITY-HOOK-DISABLE', "this would switch the kit's hooks off for every session that works here later", 'if you mean to, do it from your own terminal, which the hooks never check', "the hooks check every agent's commits, pushes and merges, so only the operator's terminal changes them"], ['E-AUTHORITY-KEY-FILE-READ', 'an agent session may not read this key file', "give the key's path to the command that signs, which reads the key itself; never open the file", "a key's contents never enter an agent's conversation, and no agent reads the operator's or another seat's key"], ['E-AUTHORITY-OPERATOR-ENDPOINT', "only the operator answers the kit's questions and records prompts", 'ask the operator; they answer from their own page or terminal', 'an agent cannot answer for the operator'], ['E-AUTHORITY-OPERATOR-ONLY', "this command is the operator's alone", 'ask the operator to run it from their own terminal', 'stopping, starting, updating or installing the engine, registering or unregistering a checkout, unseating a session, claiming a seat from a session that is already one, and opening the next team decide what every agent may do'], ['E-AUTHORITY-CHECKOUT-UNREGISTERED', 'this checkout is not registered with the engine', 'register the checkout, or remove its hooks', 'only the operator registers a checkout, with adt checkout register --project NAME --repository NAME from their own terminal, or removes its hooks with adt checkout unregister']]
        for code, cause, remedy, explain in rows:
            with self.subTest(code=code):
                self.assertIn(code, errors.ERRORS)
                error = errors.error_for(code)
                self.assertEqual((error.cls, error.cause, error.remedy),
                                 ('command', cause, remedy))
                self.assertEqual(errors.explain_for(code), (explain, remedy))

    def test_every_code_has_text(self):
        from relay_engine.slips import ANSWER_CODES
        for code in ANSWER_CODES:
            with self.subTest(code=code):
                key = 'E-AUTHORITY-' + code.removeprefix('authority.').upper()
                self.assertIn(key, errors.ERRORS)
                self.assertTrue(errors.error_for(key).cause)
                self.assertTrue(errors.error_for(key).remedy)
                self.assertTrue(errors.explain_for(key)[0])


    def test_hook_words_step6_codes(self):
        rows = {
            "operator-endpoint": (
                "only the operator answers the kit's questions and records prompts",
                "Stop and leave the decision to the operator; do not try to answer it another way.",
                "The operator answers pending decisions themselves, on the decision page; "
                "tell them a decision is waiting there.",
            ),
            "operator-only": (
                "this command is the operator's alone",
                "ask the operator to run it from their own terminal",
                "The operator runs this command themselves, in their own terminal; "
                "tell them which command you need run and why.",
            ),
            "hook-disable": (
                "this would switch the kit's hooks off for every session that works here later",
                "Leave the hooks as they are; changing them is the operator's to do.",
                "Only the operator changes the kit's hooks, from their own terminal; "
                "tell them what you wanted to change and why.",
            ),
            "key-file-read": (
                "an agent session may not read this key file",
                "Leave the file unopened, and carry on with work that does not need it.",
                "No agent reads this file; tell the operator what your task needs from it, "
                "and they do that step themselves.",
            ),
        }
        hook_words = errors.hook_words
        for name, expected in rows.items():
            with self.subTest(code=name):
                words = hook_words(f"authority.{name}")
                self.assertEqual(words, expected)
                if name == "key-file-read":
                    for text in words:
                        self.assertNotIn("path", text)
                        self.assertNotIn("sign", text)
                elif name == "operator-endpoint":
                    for text in words:
                        self.assertNotIn("terminal", text)
                elif name == "hook-disable":
                    self.assertNotIn("your own terminal", words[1])
        for code in ("authority.protected-ref-push", "authority.unknown"):
            with self.subTest(code=code), self.assertRaises(KeyError):
                hook_words(code)

    def test_existing_authority_remedies_unchanged(self):
        rows = {
            "key-file-read": "give the key's path to the command that signs, "
                             "which reads the key itself; never open the file",
            "operator-endpoint": "ask the operator; they answer from their own page or terminal",
            "hook-disable": "if you mean to, do it from your own terminal, which the hooks never check",
        }
        for name, expected in rows.items():
            with self.subTest(code=name):
                actual = strings.render(f"error-authority-{name}-remedy")
                self.assertEqual(actual.encode("utf-8"), expected.encode("utf-8"))
