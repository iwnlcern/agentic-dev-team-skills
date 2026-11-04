"""v3 relay header: field registry, the one parser, and its refusal codes (DD-v3-engine § 3, § 5)."""

import time
import unittest

from relay_engine import errors
from relay_engine import header


BASE = [
    "KIND: SITREP",
    "TO: v3-engine.planner",
    "NEEDS: none",
    "SUBJECT: hello",
    "BUNDLE: b1",
]


def draft(lines=None, body="body text\n", *, sep="\n"):
    lines = BASE if lines is None else lines
    return (sep.join(lines) + sep + sep + body).encode("utf-8")


def replace(key, value, lines=None):
    lines = list(BASE if lines is None else lines)
    out = []
    for line in lines:
        if line.startswith(key + ":"):
            if value is not None:
                out.append("%s: %s" % (key, value))
        else:
            out.append(line)
    return out


class Refused(unittest.TestCase):
    def refused(self, data, code, line=None):
        with self.assertRaises(errors.EngineError) as caught:
            header.parse(data)
        self.assertEqual(caught.exception.code, code, caught.exception.cause)
        if line is not None:
            self.assertEqual(caught.exception.params.get("line"), line,
                             caught.exception.cause)
        return caught.exception


class TestRegistry(unittest.TestCase):
    def test_fields_registry_names_reader(self):
        names = [spec.name for spec in header.FIELDS]
        self.assertEqual(sorted(names), sorted({
            "KIND", "TO", "CC", "PARENT", "BUNDLE", "VERDICT", "NEEDS",
            "SUBJECT", "REF", "FINDING", "FIXES", "RESOLVE"}))
        self.assertEqual(len(names), len(set(names)))
        for spec in header.FIELDS:
            self.assertTrue(spec.reader, spec.name)
            self.assertIn(spec.count, {"1", "0-1", "0-n"}, spec.name)

    def test_enum_values_match_design(self):
        self.assertEqual(header.KINDS, frozenset(
            "AUDIT DESIGN DESIGN-REVIEW PLAN PLAN-REVIEW IMPL REVIEW "
            "REVIEW-FOLD MERGE-GATE LIVE-VERIFY SITREP RECONCILE QUESTION"
            .split()))
        self.assertEqual(header.NEEDS, frozenset({"act", "answer", "review", "none"}))
        review = frozenset({"approve", "must-revise", "reject-narrow",
                            "human-decision-required"})
        self.assertEqual(header.VERDICTS, {
            "DESIGN-REVIEW": review, "PLAN-REVIEW": review, "REVIEW": review,
            "MERGE-GATE": frozenset({"merge-ready", "merge-blocked",
                                     "human-decision-required"}),
            "LIVE-VERIFY": frozenset({"live-verified", "failed-live-verification",
                                      "merged-not-deployed",
                                      "deployed-not-live-verified"}),
        })


class TestParse(Refused):
    def test_valid_minimal_draft(self):
        parsed = header.parse(draft())
        self.assertEqual(parsed.kind, "SITREP")
        self.assertEqual(parsed.to, ("v3-engine.planner",))
        self.assertEqual(parsed.cc, ())
        self.assertEqual(parsed.needs, "none")
        self.assertEqual(parsed.subject, "hello")
        self.assertEqual(parsed.bundle, "b1")
        self.assertIsNone(parsed.parent)
        self.assertIsNone(parsed.verdict)
        self.assertEqual(parsed.body, b"body text\n")
        self.assertEqual(parsed.header_bytes, len("\n".join(BASE)) + 2)
        # negative control: no BUNDLE and no PARENT
        self.refused(draft(replace("BUNDLE", None)), "E-FIELD-REQUIRED")

    def test_parent_instead_of_bundle(self):
        lines = replace("BUNDLE", None) + ["PARENT: #12"]
        self.assertEqual(header.parse(draft(lines)).parent, "#12")
        uuid = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"
        lines = replace("BUNDLE", None) + ["PARENT: " + uuid]
        self.assertEqual(header.parse(draft(lines)).parent, uuid)
        self.refused(draft(replace("BUNDLE", None) + ["PARENT: twelve"]),
                     "E-FIELD-ENUM", line=5)

    def test_each_refusal_rule(self):
        good = draft()
        header.parse(good)
        subject = BASE[3]
        cases = {  # rule -> (refused lines, refused line number, corrected lines)
            "E-HEADER-GRAMMAR": (BASE[:4] + ["Bundle: b1"], 5, BASE),
            "E-HEADER-UNKNOWN-KEY": (BASE + ["ROLE: Planner"], 6, BASE + ["REF: file:/r"]),
            "E-HEADER-DUPLICATE": (BASE + ["SUBJECT: again"], 6, BASE + ["CC: x.reviewer"]),
            "E-HEADER-EMPTY": (BASE[:4] + ["BUNDLE:"], 5, BASE),
            "E-HEADER-CONTROL": (BASE[:3] + ["SUBJECT: he\x07llo"] + BASE[4:], 4,
                                 BASE[:3] + ["SUBJECT: he\tllo"] + BASE[4:]),
            "E-FIELD-ENUM": (["KIND: NOTE"] + BASE[1:], 1, BASE),
            "E-FIELD-REQUIRED": (BASE[:2] + BASE[3:], None, BASE),
            "E-FIELD-FORBIDDEN": (BASE + ["VERDICT: approve"], 6, BASE),
            "E-FIELD-LENGTH": (BASE[:3] + ["SUBJECT: " + "x" * 401] + BASE[4:], 4,
                               BASE[:3] + ["SUBJECT: " + "x" * 400] + BASE[4:]),
            "E-FIELD-ENUM (shape)": (BASE[:4] + ["BUNDLE: B_1"], 5, BASE[:4] + ["BUNDLE: b-1"]),
            "E-ADDRESS": (["KIND: SITREP", "TO: v3-engine.Planner"] + BASE[2:], 2, BASE),
        }
        self.assertEqual(subject, "SUBJECT: hello")
        for name, (lines, line, fixed) in cases.items():
            with self.subTest(rule=name):
                self.refused(draft(lines), name.split()[0], line=line)
                header.parse(draft(fixed))
        # rules that need a whole-draft shape, each with its positive control
        self.refused(draft(["## v2 title"] + BASE), "E-HEADER-TITLE-LINE", line=1)
        self.refused(draft(sep="\r\n"), "E-HEADER-CRLF", line=1)
        self.refused(b"\xef\xbb\xbf" + good, "E-HEADER-BOM", line=1)
        self.refused(draft(BASE + ["REF: file:/x"] * 800), "E-HEADER-SIZE")
        self.refused(draft(body="x" * 65537), "E-BODY-SIZE")
        self.refused(draft(BASE[:4] + ["BUNDLE: b1", "FINDING: blocking summary"],
                           ), "E-FIELD-FORBIDDEN")  # FINDING on SITREP
        review = ["KIND: DESIGN-REVIEW", "TO: v3-engine.planner", "NEEDS: act",
                  "SUBJECT: s", "PARENT: #3", "VERDICT: must-revise"]
        header.parse(draft(review + ["REF: file:/tmp/out.txt",
                                     "FINDING: blocking 1 the check fails"]))
        self.refused(draft(review + ["FINDING: blocking the check fails"]),
                     "E-FINDING-NO-CHECK", line=7)
        header.parse(draft(BASE, body="x" * 65536))  # at the cap

    def test_body_never_read(self):
        noisy = ("VERDICT: approve\nKIND: MERGE-GATE\n```\nTO: operator\n```\n"
                 "~~~\nDISPATCH IMPL\n~~~\nFINDING: blocking 9 x\n")
        with_body = header.parse(draft(body=noisy))
        empty = header.parse(draft(body=""))
        for name in ("kind", "to", "cc", "parent", "bundle", "verdict", "needs",
                     "subject", "refs", "findings", "fixes", "resolves",
                     "header_bytes"):
            self.assertEqual(getattr(with_body, name), getattr(empty, name), name)
        self.assertEqual(with_body.body, noisy.encode("utf-8"))

    def test_verdict_request_vs_reply(self):
        gate = ["KIND: MERGE-GATE", "TO: v3.orchestrator-reviewer", "SUBJECT: gate",
                "PARENT: #4"]
        parsed = header.parse(draft(gate + ["NEEDS: review"]))
        self.assertIsNone(parsed.verdict)
        self.refused(draft(gate + ["NEEDS: review", "VERDICT: merge-ready"]),
                     "E-FIELD-FORBIDDEN", line=6)
        self.refused(draft(gate + ["NEEDS: act"]), "E-FIELD-REQUIRED")
        self.assertEqual(header.parse(draft(gate + ["NEEDS: act", "VERDICT: merge-ready"]))
                         .verdict, "merge-ready")
        self.refused(draft(gate + ["NEEDS: act", "VERDICT: approve"]), "E-FIELD-ENUM", line=6)

    def test_finding_rules(self):
        review = ["KIND: PLAN-REVIEW", "TO: v3-engine.planner", "NEEDS: act",
                  "SUBJECT: s", "PARENT: #3", "VERDICT: must-revise"]
        self.refused(draft(review + ["FINDING: blocking no check named"]),
                     "E-FINDING-NO-CHECK", line=7)
        self.refused(draft(review + ["REF: file:/a", "FINDING: blocking 2 index past the refs"]),
                     "E-FINDING-NO-CHECK", line=8)
        parsed = header.parse(draft(review + ["FINDING: advisory reading only"]))
        self.assertEqual(parsed.findings, (header.FindingSpec("advisory", None, "reading only"),))
        parsed = header.parse(draft(review + ["REF: file:/a", "FINDING: blocking 1 probe fails"]))
        self.assertEqual(parsed.findings, (header.FindingSpec("blocking", 1, "probe fails"),))
        self.refused(draft(BASE + ["FINDING: advisory x"]), "E-FIELD-FORBIDDEN", line=6)
        request = ["KIND: PLAN-REVIEW", "TO: v3-engine.planner", "NEEDS: review",
                   "SUBJECT: s", "PARENT: #3", "FINDING: advisory x"]
        self.refused(draft(request), "E-FIELD-FORBIDDEN", line=6)

    def test_fixes_and_resolves(self):
        fold = ["KIND: REVIEW-FOLD", "TO: v3-engine.planner", "NEEDS: review",
                "SUBJECT: s", "PARENT: #3"]
        parsed = header.parse(draft(fold + ["FIXES: F4 0123456789abcdef0123456789abcdef01234567"]))
        self.assertEqual(parsed.fixes, (("F4", "0123456789abcdef0123456789abcdef01234567"),))
        self.refused(draft(fold + ["FIXES: F4 not-a-commit"]), "E-FIELD-ENUM", line=6)
        self.refused(draft(BASE + ["FIXES: F4 0123456"]), "E-FIELD-FORBIDDEN", line=6)
        parsed = header.parse(draft(BASE + ["RESOLVE: F4 withdrawn the probe was wrong"]))
        self.assertEqual(parsed.resolves, (("F4", "withdrawn", "the probe was wrong"),))
        self.refused(draft(BASE + ["RESOLVE: F4 rejected why"]), "E-FIELD-ENUM", line=6)
        self.refused(draft(BASE + ["RESOLVE: F4 withdrawn"]), "E-FIELD-ENUM", line=6)


class TestReviewFocus(Refused):
    def test_title_line_refused(self):
        error = self.refused(draft(["## SITREP — boot"] + BASE), "E-HEADER-TITLE-LINE", line=1)
        self.assertIn("SUBJECT", error.remedy)

    def test_crlf_refused(self):
        error = self.refused(draft(sep="\r\n"), "E-HEADER-CRLF", line=1)
        self.assertIn("LF", error.remedy)
        header.parse(draft())

    def test_bom_refused(self):
        error = self.refused(b"\xef\xbb\xbf" + draft(), "E-HEADER-BOM", line=1)
        self.assertIn("byte-order mark", error.cause)

    def test_empty_address_item_refused(self):
        for value in ("a.planner, , b.planner", "a.planner,", ", a.planner"):
            with self.subTest(value=value):
                error = self.refused(draft(replace("TO", value)), "E-ADDRESS", line=2)
                self.assertIn("empty", error.cause)
        self.assertEqual(header.parse(draft(replace("TO", "a.planner, b.planner"))).to,
                         ("a.planner", "b.planner"))

    def test_subject_counts_code_points(self):
        self.assertEqual(len(header.parse(draft(replace("SUBJECT", "é" * 400))).subject), 400)
        self.refused(draft(replace("SUBJECT", "é" * 401)), "E-FIELD-LENGTH", line=4)

    def test_size_checked_before_parse(self):
        malformed = ["kind sitrep", "TO", "!!"]
        self.refused(draft(malformed, body="x" * 65537), "E-BODY-SIZE")
        big = b"KIND: SITREP\n" + b"x" * (8_400_000)
        started = time.perf_counter()
        self.refused(big, "E-HEADER-SIZE")
        self.refused(draft(body="\n" * 8_400_000), "E-BODY-SIZE")
        # bounded time (acceptance 7): the caps are applied before any scan of
        # the rest; one second is far above the measured cost (milliseconds)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_address_aliases_refused(self):
        error = self.refused(draft(replace("TO", "v3-engine.pair-planner")), "E-ADDRESS", line=2)
        self.assertIn("planner", error.remedy)
        self.refused(draft(replace("TO", "orchestrator")), "E-ADDRESS", line=2)
        self.assertEqual(header.parse(draft(replace("TO", "v3-engine.planner"))).to,
                         ("v3-engine.planner",))

    def test_other_address_rules(self):
        self.assertEqual(header.parse(draft(replace("TO", "operator"))).to, ("operator",))
        self.assertEqual(header.parse(draft(replace("TO", "pdc.planner@pdc"))).to,
                         ("pdc.planner@pdc",))
        for bad in ("v3-engine.planner@", "v3-engine.planner@@p", "v3 engine.planner",
                    "v3-engine.poet", "V3.planner"):
            with self.subTest(bad=bad):
                self.refused(draft(replace("TO", bad)), "E-ADDRESS", line=2)
        lines = BASE + ["CC: v3-engine.planner"]
        error = self.refused(draft(lines), "E-ADDRESS", line=6)
        self.assertIn("more than once", error.cause)

    def test_controls_and_separators_in_header(self):
        for char in ("\r", "\x00", "\x1b", "\x7f", "\x85", " ", " "):
            with self.subTest(char=repr(char)):
                self.refused(draft(replace("SUBJECT", "a" + char + "b")),
                             "E-HEADER-CONTROL", line=4)
        self.assertEqual(header.parse(draft(replace("SUBJECT", "a\tb"))).subject, "a\tb")

    def test_grammar_variants(self):
        for line in ("SUBJECT:hello", "SUBJECT:  hello", " SUBJECT: hello",
                     "SUBJECT： hello", "SUBJECT: hello ", "ＳUBJECT: hello"):
            with self.subTest(line=line):
                self.refused(draft(BASE[:3] + [line] + BASE[4:]), "E-HEADER-GRAMMAR", line=4)

    def test_body_bytes_need_not_be_utf8(self):
        self.refused(b"KIND: SITREP\nTO: \xff\n\nbody", "E-HEADER-GRAMMAR", line=2)
        parsed = header.parse(draft()[:-len(b"body text\n")] + b"\xff\xfe raw")
        self.assertEqual(parsed.body, b"\xff\xfe raw")

    def test_every_refusal_renders(self):
        for code in ("E-HEADER-GRAMMAR", "E-HEADER-UNKNOWN-KEY", "E-HEADER-DUPLICATE",
                     "E-HEADER-EMPTY", "E-HEADER-CONTROL", "E-HEADER-TITLE-LINE",
                     "E-HEADER-CRLF", "E-HEADER-BOM", "E-HEADER-SIZE", "E-BODY-SIZE",
                     "E-FIELD-ENUM", "E-FIELD-REQUIRED", "E-FIELD-FORBIDDEN",
                     "E-FIELD-LENGTH", "E-ADDRESS", "E-FINDING-NO-CHECK"):
            with self.subTest(code=code):
                self.assertIn(code, errors.ERRORS)
                cause, remedy = errors.explain_for(code)
                self.assertTrue(cause and remedy)


if __name__ == "__main__":
    unittest.main()
