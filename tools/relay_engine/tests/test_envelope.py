import unittest

from relay_engine import errors
from relay_engine.envelope import (REQUIRED_FIELDS, body_sha256,
                                   content_hash, parse_draft, valid_run_id)


MINIMAL = """## demo

ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: demo-plan-1
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: v29-gates.planner
TO: v29-gates.implementer
SUBJECT: demo

body
"""


WITH_OPTIONALS = """## v29 cycle relay

ROLE: Implementer
PHASE: IMPL
AUTHORITY: implementation
DISPATCH_ID: v29-engine-impl-2
PARENT_DISPATCH_ID: v29-engine-plan-review-38
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: merge only
FROM: v29-gates.planner
TO: v29-gates.implementer
CC: v29.orchestrator-planner, v29.orchestrator-reviewer
STATUS: active
SUBJECT: execute the approved plan

## Required
body
"""


class TestEnvelope(unittest.TestCase):
    def test_submission_required_fields_are_exact(self):
        self.assertEqual(
            REQUIRED_FIELDS,
            ("ROLE", "PHASE", "AUTHORITY", "DISPATCH_ID", "RUN_ID",
             "CEREMONY_TIER", "EVIDENCE_TARGET", "HUMAN_GATE_REQUIRED",
             "FROM", "TO", "SUBJECT"),
        )

    def test_round_trip_cycle_fixture(self):
        envelope = parse_draft(WITH_OPTIONALS)
        self.assertEqual(envelope.phase, "IMPL")
        self.assertEqual(envelope.role, "Implementer")
        self.assertEqual(envelope.authority, "implementation")
        self.assertEqual(envelope.dispatch_id, "v29-engine-impl-2")
        self.assertEqual(envelope.parent_dispatch_id,
                         "v29-engine-plan-review-38")
        self.assertEqual(envelope.run_id, "v29")
        self.assertEqual(envelope.from_seat, "v29-gates.planner")
        self.assertEqual(envelope.to_seats, ("v29-gates.implementer",))
        self.assertEqual(envelope.cc_seats,
                         ("v29.orchestrator-planner",
                          "v29.orchestrator-reviewer"))
        self.assertEqual(envelope.status, "active")
        self.assertEqual(envelope.subject, "execute the approved plan")
        self.assertEqual(envelope.headers["CEREMONY_TIER"], "large")

    def test_missing_from_refuses_with_named_header(self):
        text = MINIMAL.replace("FROM: v29-gates.planner\n", "")
        with self.assertRaises(errors.EngineError) as caught:
            parse_draft(text)
        self.assertEqual(caught.exception.code, "E-HEADER")
        self.assertEqual(caught.exception.cause,
                         "required relay header missing or invalid: FROM")

    def test_duplicate_header_refuses(self):
        text = MINIMAL.replace("SUBJECT: demo\n",
                               "SUBJECT: demo\nFROM: another.seat\n")
        with self.assertRaises(errors.EngineError) as caught:
            parse_draft(text)
        self.assertEqual(caught.exception.code, "E-HEADER")
        self.assertIn("FROM", caught.exception.cause)

    def test_hashes_are_literal_and_edge_bound(self):
        envelope = parse_draft(MINIMAL)
        body = b"body\n"
        self.assertEqual(
            body_sha256(body),
            "9e2ec912af5dff2a72300863864fc4da04e81999339d9fac5c7590ba8a3f4e11",
        )
        first = content_hash(envelope, body, "path-a")
        second = content_hash(envelope, body, "path-b")
        self.assertEqual(
            first,
            "6861550c161b558966b08336b3af63bf9b7a24c190d2ccb37e85c2566290ce7e",
        )
        self.assertEqual(
            second,
            "4c20653e350133b7fc1fec02f8a80c370dca6b2007743b46a0897cba81516010",
        )
        self.assertNotEqual(first, second)

    def test_run_id_grammar_is_exact(self):
        for value in ("v29", "A", "a.b_c-9", "x" * 64):
            with self.subTest(value=value):
                self.assertTrue(valid_run_id(value))
        for value in ("", ".hidden", "../v29", "a/b", "x" * 65,
                      "white space", None):
            with self.subTest(value=value):
                self.assertFalse(valid_run_id(value))


if __name__ == "__main__":
    unittest.main()
