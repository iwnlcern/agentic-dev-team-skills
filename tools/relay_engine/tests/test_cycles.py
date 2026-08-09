import os
import tempfile
import unittest

from relay_engine import cycles, errors, supersede
from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, init_schema
from relay_engine.paths import Root, ensure_engine_dir


def draft(dispatch, sender, to, *, status="active", extra=""):
    return ("## relay\n\n"
            "ROLE: Planner\n"
            "PHASE: PLAN\n"
            "AUTHORITY: plan-only\n"
            "DISPATCH_ID: %s\n"
            "RUN_ID: v29\n"
            "CEREMONY_TIER: large\n"
            "EVIDENCE_TARGET: E2\n"
            "HUMAN_GATE_REQUIRED: no\n"
            "FROM: %s\n"
            "TO: %s\n"
            "CC: context.only\n"
            "STATUS: %s\n"
            "%s"
            "SUBJECT: cycle fixture\n\nbody\n" % (
                dispatch, sender, to, status, extra))


class TestCycles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def send(self, text, sid, edge=None):
        body = text.encode()
        envelope = parse_draft(text)
        events, advisories = cycles.prepare(self.ledger, envelope, edge)
        edges, edge_advisories = supersede.prepare(self.ledger, envelope)
        return admit(
            self.ledger, self.root, envelope, body, sid,
            admits_against=edge, prechecks=(cycles.precheck,),
            cycle_events=events, supersession_edges=edges,
            advisories=tuple(advisories) + tuple(edge_advisories))

    def test_participants_cc_exclusion_close_reopen_and_handoff_scopes(self):
        opener = self.send(
            draft("cycle-plan", "v29-a.planner", "v29-a.implementer"),
            "sid-open")
        successor = self.send(
            draft("cycle-plan", "v29-a.implementer", "v29-b.planner"),
            "sid-next", opener.rendered_path)
        self.assertEqual(cycles.participants(self.ledger, "cycle-plan"), {
            "v29-a.planner", "v29-a.implementer", "v29-b.planner"})
        with self.assertRaises(errors.EngineError) as cc_only:
            self.send(draft("cycle-plan", "context.only", "v29-a.planner"),
                      "sid-cc", successor.rendered_path)
        self.assertEqual(cc_only.exception.code, "E-ID-COLLISION")

        closed = self.send(draft(
            "cycle-plan", "v29-a.planner", "v29-a.implementer",
            extra="CYCLE_TERMINUS: withdrawn\n"),
            "sid-close", successor.rendered_path)
        self.assertEqual(cycles.state(self.ledger, "cycle-plan"), "closed")
        with self.assertRaises(errors.EngineError):
            self.send(draft("cycle-plan", "v29-a.implementer",
                            "v29-a.planner"), "sid-after",
                      closed.rendered_path)
        reopened = self.send(draft(
            "cycle-plan", "v29-a.planner", "v29-a.implementer",
            extra="CYCLE_REOPEN: correction\n"),
            "sid-reopen", closed.rendered_path)
        self.assertEqual(cycles.state(self.ledger, "cycle-plan"), "open")
        self.send(draft("cycle-plan", "v29-a.implementer",
                        "v29-a.planner"), "sid-resumed",
                  reopened.rendered_path)

        for dispatch in ("cycle-plan-review", "cycle-impl"):
            opened = self.send(
                draft(dispatch, "v29-a.planner", "v29-a.implementer"),
                "sid-" + dispatch)
            row = self.ledger.execute(
                "SELECT admits_against_seq FROM relays WHERE seq=?",
                (opened.seq,)).fetchone()
            self.assertEqual(row, (None,))
            self.assertEqual(cycles.state(self.ledger, dispatch), "open")

    def test_local_supersession_binds_one_exact_edge(self):
        target = self.send(
            draft("target", "v29-a.planner", "v29-a.implementer"),
            "sid-target")
        ruling = self.send(draft(
            "ruling", "operator", "v29-a.planner",
            extra="SUPERSEDES: %s\n" % target.rendered_path),
            "sid-ruling")
        self.assertEqual(self.ledger.execute(
            "SELECT target_seq,ruling_seq,source,applied "
            "FROM supersession_edges").fetchone(),
            (target.seq, ruling.seq, "local", 1))
        with self.assertRaises(errors.EngineError) as refused:
            self.send(draft("target", "v29-a.implementer",
                            "v29-a.planner"), "sid-reply",
                      target.rendered_path)
        self.assertEqual(refused.exception.code, "E-SUPERSEDED")


if __name__ == "__main__":
    unittest.main()
