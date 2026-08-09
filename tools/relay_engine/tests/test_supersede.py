import base64
import os
import tempfile
import unittest

from relay_engine import commission, errors, supersede
from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, establish_run_identity, init_schema
from relay_engine.paths import Root, ensure_engine_dir


def relay(run_id, dispatch, sender, to, *, extra="", subject="fixture"):
    return ("## relay\n\n"
            "ROLE: Planner\nPHASE: PLAN\nAUTHORITY: plan-only\n"
            "DISPATCH_ID: %s\nRUN_ID: %s\nCEREMONY_TIER: large\n"
            "EVIDENCE_TARGET: E2\nHUMAN_GATE_REQUIRED: no\n"
            "FROM: %s\nTO: %s\n%sSUBJECT: %s\n\nbody\n" % (
                dispatch, run_id, sender, to, extra, subject))


class TestAdoptedSupersession(unittest.TestCase):
    def setUp(self):
        self.parent_temp = tempfile.TemporaryDirectory()
        self.parent = Root(self.parent_temp.name)
        self.parent_engine = ensure_engine_dir(self.parent.dirfd)
        self.parent_ledger = init_schema(
            self.parent_engine, self.parent.path)
        dispatch_text = relay(
            "parent", "commission-child", "parent.planner",
            "parent.implementer")
        dispatched = admit(
            self.parent_ledger, self.parent, parse_draft(dispatch_text),
            dispatch_text.encode(), "dispatch-sid")
        self.record_result = commission.commission(
            self.parent_ledger, self.parent, dispatched.rendered_path,
            "child")
        ruling_text = relay(
            "parent", "operator-ruling", "operator", "child",
            extra="SUPERSEDES: child/target.md\n", subject="bind child")
        ruling = admit(
            self.parent_ledger, self.parent, parse_draft(ruling_text),
            ruling_text.encode(), "ruling-sid")
        self.bundle_result = supersede.export_ruling(
            self.parent_ledger, ruling.rendered_path, "child")

    def tearDown(self):
        self.parent_ledger.close()
        os.close(self.parent_engine)
        self.parent.close()
        self.parent_temp.cleanup()

    def test_adoption_binds_exact_target_and_replays(self):
        with tempfile.TemporaryDirectory() as child_name:
            child = Root(child_name)
            child_engine = ensure_engine_dir(child.dirfd)
            ledger = init_schema(child_engine, child.path)
            try:
                record = base64.b64decode(
                    self.record_result["record_b64"])
                establish_run_identity(ledger, "child", record)
                target_text = relay(
                    "child", "target", "child.planner",
                    "child.implementer")
                target = admit(
                    ledger, child, parse_draft(target_text),
                    target_text.encode(), None, origin="hand",
                    stamp="20260808-010000",
                    rendered_path="child/target.md")
                bundle = base64.b64decode(
                    self.bundle_result["bundle_b64"])
                adopted = supersede.adopt_ruling(ledger, child, bundle)
                self.assertTrue(adopted["applied"])
                self.assertFalse(adopted["duplicate"])
                self.assertEqual(ledger.execute(
                    "SELECT origin,admits_against_seq FROM relays "
                    "WHERE seq=?", (adopted["ruling_seq"],)).fetchone(),
                    ("adopted", None))
                self.assertEqual(ledger.execute(
                    "SELECT target_seq,ruling_seq,source,applied "
                    "FROM supersession_edges").fetchone(),
                    (target.seq, adopted["ruling_seq"], "adopted", 1))
                replay = supersede.adopt_ruling(ledger, child, bundle)
                self.assertTrue(replay["duplicate"])
                self.assertEqual(ledger.execute(
                    "SELECT COUNT(*) FROM supersession_edges").fetchone()[0],
                    1)
            finally:
                ledger.close()
                os.close(child_engine)
                child.close()

    def test_tampered_bundle_refuses(self):
        bundle = bytearray(base64.b64decode(
            self.bundle_result["bundle_b64"]))
        bundle[10] ^= 1
        with tempfile.TemporaryDirectory() as child_name:
            child = Root(child_name)
            child_engine = ensure_engine_dir(child.dirfd)
            ledger = init_schema(child_engine, child.path)
            try:
                with self.assertRaises(ValueError):
                    supersede.adopt_ruling(ledger, child, bytes(bundle))
                self.assertEqual(ledger.execute(
                    "SELECT COUNT(*) FROM supersession_edges").fetchone()[0],
                    0)
            finally:
                ledger.close()
                os.close(child_engine)
                child.close()


if __name__ == "__main__":
    unittest.main()
