"""v3 relay header: field registry, the one parser, and its refusal codes (DD-v3-engine § 3, § 5)."""

from pathlib import Path
import subprocess
import sys
import tempfile
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
        error = self.refused(draft(replace("BUNDLE", None) + ["PARENT: twelve"]),
                             "E-FIELD-SHAPE", line=5)
        self.assertIn("#<n>", error.cause)

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
            "E-FIELD-SHAPE": (BASE[:4] + ["BUNDLE: B_1"], 5, BASE[:4] + ["BUNDLE: b-1"]),
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
                 "~~~\nDISPATCH IMPL\n~~~\nFINDING: blocking 9 x\n"
                 "\nKIND: AUDIT\nTO: x.planner\n\nSUBJECT: after a second empty line\n")
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
        self.refused(draft(review + ["FINDING: blocking"]), "E-FIELD-SHAPE", line=7)
        self.refused(draft(BASE + ["FINDING: advisory x"]), "E-FIELD-FORBIDDEN", line=6)
        request = ["KIND: PLAN-REVIEW", "TO: v3-engine.planner", "NEEDS: review",
                   "SUBJECT: s", "PARENT: #3", "FINDING: advisory x"]
        self.refused(draft(request), "E-FIELD-FORBIDDEN", line=6)

    def test_fixes_and_resolves(self):
        fold = ["KIND: REVIEW-FOLD", "TO: v3-engine.planner", "NEEDS: review",
                "SUBJECT: s", "PARENT: #3"]
        parsed = header.parse(draft(fold + ["FIXES: F4 0123456789abcdef0123456789abcdef01234567"]))
        self.assertEqual(parsed.fixes, (("F4", "0123456789abcdef0123456789abcdef01234567"),))
        self.refused(draft(fold + ["FIXES: F4 not-a-commit"]), "E-FIELD-SHAPE", line=6)
        self.refused(draft(BASE + ["FIXES: F4 0123456"]), "E-FIELD-FORBIDDEN", line=6)
        parsed = header.parse(draft(BASE + ["RESOLVE: F4 withdrawn the probe was wrong"]))
        self.assertEqual(parsed.resolves, (("F4", "withdrawn", "the probe was wrong"),))
        self.refused(draft(BASE + ["RESOLVE: F4 rejected why"]), "E-FIELD-ENUM", line=6)
        self.refused(draft(BASE + ["RESOLVE: F4 withdrawn"]), "E-FIELD-SHAPE", line=6)


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
        seen = []

        def recording(value):
            seen.append(value)
            header.syntactic_address(value)

        for value in ("a.planner, , b.planner", "a.planner,", ", a.planner"):
            with self.subTest(value=value):
                with self.assertRaises(errors.EngineError) as caught:
                    header.parse(draft(replace("TO", value)), validate_address=recording)
                self.assertEqual(caught.exception.code, "E-ADDRESS")
                self.assertEqual(caught.exception.params["line"], 2)
                self.assertEqual(caught.exception.params["reason"], "empty list item")
        self.assertNotIn("", seen)  # the validator never sees an empty item
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

    def test_question_to_operator_only(self):
        question = ["KIND: QUESTION", "NEEDS: answer", "SUBJECT: decide", "BUNDLE: b1"]
        for to in ("v3-engine.implementer", "operator, v3-engine.implementer"):
            with self.subTest(to=to):
                error = self.refused(draft(question + ["TO: " + to]), "E-QUESTION-ADDRESS", line=5)
                self.assertIn("operator", error.remedy)
        parsed = header.parse(draft(question + ["TO: operator", "CC: v3.orchestrator-planner"]))
        self.assertEqual((parsed.to, parsed.cc), (("operator",), ("v3.orchestrator-planner",)))

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
        for char in ("\r", "\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"):
            with self.subTest(char=repr(char)):
                self.refused(draft(replace("SUBJECT", "a" + char + "b")),
                             "E-HEADER-CONTROL", line=4)
        self.assertEqual(header.parse(draft(replace("SUBJECT", "a\tb"))).subject, "a\tb")

    def test_grammar_variants(self):
        for line in ("SUBJECT:hello", "SUBJECT:  hello", " SUBJECT: hello",
                     "SUBJECT： hello", "SUBJECT: hello ", "ＳUBJECT: hello"):
            with self.subTest(line=line):
                error = self.refused(draft(BASE[:3] + [line] + BASE[4:]),
                                     "E-HEADER-GRAMMAR", line=4)
                self.assertIn("space", error.remedy)

    def test_draft_must_be_utf8(self):
        error = self.refused(b"KIND: SITREP\nTO: \xff\n\nbody", "E-DRAFT-UTF8")
        self.assertEqual(error.params["offset"], 17)
        good = draft()
        error = self.refused(good + b"tail \xc3(", "E-DRAFT-UTF8")
        self.assertEqual(error.params["offset"], len(good) + 5)
        # positive control: non-ASCII UTF-8 in header and body parses
        parsed = header.parse(draft(replace("SUBJECT", "caf\u00e9"), body="na\u00efve \u2014 ok\n"))
        self.assertEqual(parsed.body, "na\u00efve \u2014 ok\n".encode("utf-8"))
        # size is still checked first
        self.refused(draft(body="x" * 65537) + b"\xff", "E-BODY-SIZE")

    def test_every_refusal_renders(self):
        for code in ("E-HEADER-GRAMMAR", "E-HEADER-UNKNOWN-KEY", "E-HEADER-DUPLICATE",
                     "E-HEADER-EMPTY", "E-HEADER-CONTROL", "E-HEADER-TITLE-LINE",
                     "E-HEADER-CRLF", "E-HEADER-BOM", "E-HEADER-SIZE", "E-BODY-SIZE",
                     "E-FIELD-ENUM", "E-FIELD-REQUIRED", "E-FIELD-FORBIDDEN",
                     "E-FIELD-LENGTH", "E-FIELD-SHAPE", "E-ADDRESS",
                     "E-FINDING-NO-CHECK", "E-DRAFT-UTF8", "E-QUESTION-ADDRESS"):
            with self.subTest(code=code):
                self.assertIn(code, errors.ERRORS)
                cause, remedy = errors.explain_for(code)
                self.assertTrue(cause and remedy)



REVIEW = ["KIND: PLAN-REVIEW", "TO: v3-engine.planner", "NEEDS: act", "SUBJECT: s",
          "PARENT: #3", "VERDICT: must-revise"]


class TestPanelFold1(Refused):
    """PR-1 panel fold 1 (v3-engine-pr1-review-1): M1-M6, M9 boundaries, O6, O7."""

    def sized(self, header_bytes):
        """A valid draft whose header block, with its empty line, is exactly header_bytes long."""
        lines = list(BASE)
        size = len("\n".join(lines)) + 2
        pad = header_bytes - size - len("\nREF: file:/")
        lines.append("REF: file:/" + "x" * pad)
        data = draft(lines)
        self.assertEqual(data.index(b"\n\n") + 2, header_bytes)
        return data

    def test_long_key_is_typed(self):  # M1
        for key in ("A" * 64, "A" * 65, "A" * 8000):
            with self.subTest(length=len(key)):
                error = self.refused(draft(BASE + [key + ": x"]), "E-HEADER-UNKNOWN-KEY", line=6)
                self.assertLessEqual(len(error.params["key"]), 64)
                error = self.refused(draft(BASE + [key + ":"]), "E-HEADER-EMPTY", line=6)
                self.assertLessEqual(len(error.params["key"]), 64)

    def test_parse_raises_only_typed_errors(self):  # M1 corpus
        import random
        rng = random.Random(20261104)
        seeds = [draft(), draft(REVIEW + ["REF: file:/a", "FINDING: blocking 1 probe fails"]),
                 draft(["KIND: REVIEW-FOLD", "TO: a.planner", "NEEDS: review", "SUBJECT: s",
                        "PARENT: #2", "FIXES: F1 " + "0" * 40, "RESOLVE: F2 withdrawn r"])]
        alphabet = b"AZ:_ -#\n\r\t\x00\xc3\xa9\xff09@.,F" + bytes(range(32, 127))
        outcomes = {"draft": 0, "refused": 0}
        for _ in range(4000):
            data = bytearray(rng.choice(seeds))
            for _ in range(rng.randint(1, 6)):
                op, at = rng.randrange(3), rng.randrange(len(data) + 1)
                if op == 0:
                    data[at:at] = bytes([rng.choice(alphabet)]) * rng.choice((1, 1, 70))
                elif op == 1 and data:
                    del data[min(at, len(data) - 1)]
                elif data:
                    data[min(at, len(data) - 1)] = rng.choice(alphabet)
            try:
                header.parse(bytes(data))
                outcomes["draft"] += 1
            except errors.EngineError:
                outcomes["refused"] += 1
        self.assertGreater(outcomes["draft"], 0)
        self.assertGreater(outcomes["refused"], 0)

    def test_unterminated_header_causes(self):  # M2
        body = "x" * 9000 + "\n"
        crlf = ("\r\n".join(BASE) + "\r\n\r\n" + body).encode()
        self.refused(crlf, "E-HEADER-CRLF", line=1)
        spaced = ("\n".join(BASE) + "\n \n" + body).encode()
        self.refused(spaced, "E-HEADER-GRAMMAR", line=6)
        plain = ("\n".join(BASE) + "\n" + "REF: file:/" + "y" * 9000 + "\n").encode()
        error = self.refused(plain, "E-HEADER-SIZE")
        self.assertIn("8192", error.cause)
        self.assertNotIn(str(len(plain)), error.cause)  # never the whole draft's size

    def test_unterminated_header_names_any_line_rule(self):  # M2, superset
        body = "x" * 9000 + "\n"
        v2 = ("## SITREP — title\nROLE: Planner\n" + "\n".join(BASE) + "\n" + body).encode()
        self.refused(v2, "E-HEADER-TITLE-LINE", line=1)
        unknown = ("\n".join(BASE) + "\nROLE: Planner\n" + body).encode()
        self.refused(unknown, "E-HEADER-UNKNOWN-KEY", line=6)
        bad = ("\n".join(BASE) + "\n").encode() + b"SUBJECT: \xff\n" + body.encode()
        error = self.refused(bad, "E-DRAFT-UTF8")
        self.assertEqual(error.params["line"], 6)
        # a long last line cut at the cap is never judged on its cut bytes
        cut = ("\n".join(BASE) + "\nREF: file:/" + "\u00e9" * 5000 + "\n").encode()
        self.refused(cut, "E-HEADER-SIZE")

    def test_header_cap_boundary(self):  # M3, H6, M13b
        header.parse(self.sized(8192))
        error = self.refused(self.sized(8193), "E-HEADER-SIZE")
        self.assertIn("8193", error.cause)

    def test_value_pieces_are_shape(self):  # M4
        for line in ("RESOLVE: F1", "RESOLVE: F1  withdrawn r", "RESOLVE: F1 withdrawn  r"):
            with self.subTest(line=line):
                self.refused(draft(BASE + [line]), "E-FIELD-SHAPE", line=6)
        for line in ("FINDING: blocking  1 s", "FINDING: advisory  s", "FINDING: advisory s  t",
                     "FINDING: blocking 1234567 s", "FINDING: blocking 0 s", "FINDING: blocking 01 s"):
            with self.subTest(line=line):
                self.refused(draft(REVIEW + ["REF: file:/a", line]), "E-FIELD-SHAPE", line=8)
        fold = ["KIND: REVIEW-FOLD", "TO: a.planner", "NEEDS: review", "SUBJECT: s", "PARENT: #2"]
        self.refused(draft(fold + ["FIXES: F1  " + "0" * 40]), "E-FIELD-SHAPE", line=6)
        parsed = header.parse(draft(REVIEW + ["REF: file:/a", "FINDING: advisory s t"]))
        self.assertEqual(parsed.findings[0].summary, "s t")

    def test_remedies_are_true(self):  # M6
        first = self.refused(draft(["# title"] + BASE), "E-HEADER-TITLE-LINE", line=1)
        self.assertIn("SUBJECT", first.remedy)
        late = self.refused(draft(BASE + ["# Findings"]), "E-HEADER-TITLE-LINE", line=6)
        self.assertIn("empty line", late.remedy)
        self.assertNotIn("SUBJECT", late.remedy)
        advisory = self.refused(draft(REVIEW + ["FINDING: advisory 2 flaky runs seen"]),
                                "E-FINDING-NO-CHECK", line=7)
        self.assertNotIn("advisory", advisory.remedy)
        blocking = self.refused(draft(REVIEW + ["FINDING: blocking probe fails"]),
                                "E-FINDING-NO-CHECK", line=7)
        self.assertIn("advisory", blocking.remedy)
        both = self.refused(draft(BASE + ["PARENT: #2"]), "E-FIELD-FORBIDDEN", line=5)
        self.assertIn("inherits", both.remedy)
        self.assertNotIn("KIND", both.remedy)

    def test_boundaries(self):  # M9 header survivors H1-H5, H7, H10
        fold = ["KIND: REVIEW-FOLD", "TO: a.planner", "NEEDS: review", "SUBJECT: s", "PARENT: #2"]
        ok, bad = header.parse, self.refused
        ok(draft(REVIEW + ["FINDING: advisory " + "s" * 200]))
        bad(draft(REVIEW + ["FINDING: advisory " + "s" * 201]), "E-FIELD-LENGTH", line=7)
        ok(draft(BASE + ["RESOLVE: F1 withdrawn " + "r" * 200]))
        bad(draft(BASE + ["RESOLVE: F1 withdrawn " + "r" * 201]), "E-FIELD-LENGTH", line=6)
        ok(draft(REVIEW + ["REF: file:/a", "FINDING: blocking 1 s"]))
        bad(draft(REVIEW + ["REF: file:/a", "FINDING: blocking 0 s"]), "E-FIELD-SHAPE", line=8)
        ok(draft(replace("BUNDLE", None) + ["PARENT: #1"]))
        bad(draft(replace("BUNDLE", None) + ["PARENT: #0"]), "E-FIELD-SHAPE", line=5)
        ok(draft(fold + ["FIXES: F1 " + "a" * 40]))
        ok(draft(fold + ["FIXES: F1 " + "a" * 64]))
        for commit in ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40):
            with self.subTest(commit=len(commit)):
                bad(draft(fold + ["FIXES: F1 " + commit]), "E-FIELD-SHAPE", line=6)
        bad(draft(fold + ["FIXES: F0 " + "a" * 40]), "E-FIELD-SHAPE", line=6)
        ok(draft(BASE + ["RESOLVE: F1 withdrawn r"]))
        bad(draft(BASE + ["RESOLVE: F0 withdrawn r"]), "E-FIELD-SHAPE", line=6)
        ok(draft(replace("BUNDLE", "b" * 48)))
        bad(draft(replace("BUNDLE", "b" * 49)), "E-FIELD-SHAPE", line=5)
        ok(draft(REVIEW + ["REF: file:/a", "FINDING: advisory 1 s"]))
        bad(draft(REVIEW + ["REF: file:/a", "FINDING: advisory 5 s"]), "E-FINDING-NO-CHECK", line=8)

    def test_display_escapes(self):  # M9 H8, O6
        for char, shown in (("\t", "\\u0009"), ("\u00a0", "\\u00a0"), ("\u200b", "\\u200b"),
                            ("\ufeff", "\\ufeff"), ("\u2060", "\\u2060")):  # U+202E is refused since A12
            with self.subTest(char=repr(char)):
                error = self.refused(draft(replace("KIND", "SITREP" + char + "X")), "E-FIELD-ENUM", line=1)
                self.assertIn(shown, error.cause)
                self.assertNotIn(char, error.cause)

    def test_utf8_names_the_header_line(self):  # O6
        error = self.refused(b"KIND: SITREP\nTO: \xff\n\nbody", "E-DRAFT-UTF8")
        self.assertEqual((error.params["offset"], error.params["line"]), (17, 2))
        self.assertIn("header line 2", error.cause)
        error = self.refused(draft() + b"\xff", "E-DRAFT-UTF8")
        self.assertIn("body", error.cause)

    def test_alias_reason_matches_platform(self):  # O7
        error = self.refused(draft(replace("TO", "v3-engine.pair-planner")), "E-ADDRESS", line=2)
        self.assertEqual(error.params["reason"], "unknown role word")
        self.assertIn("planner", error.remedy)



class TestGateAdvisories(Refused):
    """PR-1 gate advisories A11, A12 (v3-engine/SITREP-pair-planner-20261104-061814.md)."""

    def test_address_list_whitespace_refused(self):  # A11
        for value in ("a.planner, b.planner", "a.planner,b.planner"):  # positive controls
            with self.subTest(value=value):
                self.assertEqual(header.parse(draft(replace("TO", value))).to,
                                 ("a.planner", "b.planner"))
        for value, shown in (("a.planner,\u00a0b.planner", "\\u00a0"),
                             ("a.planner,\u3000b.planner", "\\u3000"),
                             ("a.planner,\tb.planner", "\\u0009"),
                             ("a.planner ,b.planner", "a.planner ,"),
                             ("a.planner,  b.planner", ",  b")):
            with self.subTest(value=repr(value)):
                error = self.refused(draft(replace("TO", value)), "E-FIELD-SHAPE", line=2)
                self.assertEqual(error.params["field"], "TO")
                self.assertIn(shown, error.cause)
        # whitespace at either end of the value is the line grammar's refusal, as before
        error = self.refused(draft(replace("TO", "a.planner, b.planner\u00a0")),
                             "E-HEADER-GRAMMAR", line=2)
        self.assertIn("\\u00a0", error.cause)
        # whitespace inside an item is the address grammar's refusal, as before
        self.refused(draft(replace("TO", "v3 engine.planner")), "E-ADDRESS", line=2)
        cc = BASE + ["CC: c.planner,\u00a0d.planner"]
        error = self.refused(draft(cc), "E-FIELD-SHAPE", line=6)
        self.assertEqual(error.params["field"], "CC")

    def test_bidi_controls_refused(self):  # A12
        for code in (0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                     0x2066, 0x2067, 0x2068, 0x2069):
            with self.subTest(code="U+%04X" % code):
                error = self.refused(draft(replace("SUBJECT", "he" + chr(code) + "llo")),
                                     "E-HEADER-CONTROL", line=4)
                self.assertEqual(error.params["codepoint"], "U+%04X" % code)
                self.assertEqual(error.params["column"], 12)
        # other format characters stay admitted, and refusal text escapes them
        self.assertEqual(header.parse(draft(replace("SUBJECT", "he\u200bllo"))).subject,
                         "he\u200bllo")
        error = self.refused(draft(replace("NEEDS", "no\u200bne")), "E-FIELD-ENUM", line=3)
        self.assertIn("\\u200b", error.cause)



PDC_SWEEPER = Path("/Users/jack/Programming/pdc/tools/inbox-sweep.py")


class TestMirrorHeader(unittest.TestCase):
    """Task 8: mirror header lines in today's spelling, read by pdc's inbox sweeper."""

    def test_lines_and_order(self):
        self.assertEqual(
            header.render_mirror_header("t.planner", ["t.implementer", "u.planner"],
                                        ["v.orchestrator-planner"], "t/IMPL-x-20261104-010000.md"),
            ["FROM: t.planner", "TO: t.implementer, u.planner", "CC: v.orchestrator-planner",
             "IN_REPLY_TO: t/IMPL-x-20261104-010000.md"])
        self.assertEqual(header.render_mirror_header("t.planner", ["t.implementer"], [], None),
                         ["FROM: t.planner", "TO: t.implementer"])

    def test_refuses_values_that_forge_lines(self):
        good = ("t.planner", ["t.implementer"], [], "t/a.md")
        for index, bad in ((0, "t.planner\nTO: x.y"), (0, ""), (1, ["a.b, c.d"]),
                           (1, ["a b"]), (1, []), (2, ["c.d\r"]), (3, "t/a.md\nFROM: x.y"),
                           (3, ""), (1, ["a.b\u2028"]), (1, ["a.b,c.d"]),
                           (3, "t/a.md\u2028FROM: x.y"), (3, "t/a.md\u2029")):
            args = list(good)
            args[index] = bad
            with self.subTest(index=index, bad=bad):
                with self.assertRaises(ValueError):
                    header.render_mirror_header(*args)

    def sweep(self, seat, reply_parent):
        with tempfile.TemporaryDirectory() as name:
            team = Path(name, "master", "relays", "t")
            team.mkdir(parents=True)
            inbound = "IMPL-pair-planner-20261104-010000.md"
            (team / inbound).write_text("\n".join(header.render_mirror_header(
                "t.planner", [seat], ["v.orchestrator-planner"], None)) + "\n\nplease act\n")
            (team / "IMPL-pair-implementer-20261104-010100.md").write_text("\n".join(
                header.render_mirror_header(seat, ["t.planner"], [],
                                            "t/" + inbound if reply_parent else None))
                + "\n\ndone\n")
            result = subprocess.run(
                [sys.executable, "-I", str(PDC_SWEEPER), seat, "--all", "--root", name],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, cwd=name)
        rows = [line.split() for line in result.stdout.splitlines() if inbound in line]
        return result.returncode, rows

    @unittest.skipUnless(PDC_SWEEPER.is_file(), "pdc checkout absent: %s" % PDC_SWEEPER)
    def test_pdc_sweeper_consumed(self):
        code, rows = self.sweep("t.implementer", reply_parent=True)
        self.assertEqual((code, [row[1] for row in rows]), (0, ["consumed"]))
        code, rows = self.sweep("t.implementer", reply_parent=False)  # control
        self.assertEqual((code, [row[1] for row in rows]), (1, ["UNREAD"]))


if __name__ == "__main__":
    unittest.main()

