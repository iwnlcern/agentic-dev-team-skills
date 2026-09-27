"""Boot acknowledgments preserve each boot relay's cycle identity."""

import os
from pathlib import Path
import tempfile
import unittest

from relay_engine import cycles, errors, seats, supersede
from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, init_schema
from relay_engine.paths import Root, ensure_engine_dir


class TestBootAcknowledgment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        self.ledger.execute("INSERT INTO meta(key,value) VALUES('run_id','v29')")
        self.boots = {}
        for address, role, dispatch in (
                ("v29-a.planner", "Planner", "v29-boot-v29-a-planner"),
                ("v29-b.implementer", "Implementer",
                 "v29-boot-v29-b-implementer")):
            result = seats.register(self.ledger, self.root, address, role,
                                    dispatch)
            self.boots[address] = (dispatch, result["boot_relay"])

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def acknowledge(self, address, dispatch, boot_path, *, edge=None):
        body = ("## SITREP — boot acknowledgment\n\n"
                "ROLE: %s\nPHASE: SITREP\nAUTHORITY: report-only\n"
                "DISPATCH_ID: %s\nIN_REPLY_TO: %s\nRUN_ID: v29\n"
                "CEREMONY_TIER: large\nEVIDENCE_TARGET: E1\n"
                "HUMAN_GATE_REQUIRED: no\nFROM: %s\n"
                "TO: v29.orchestrator-planner\n"
                "SUBJECT: boot acknowledgment\n\n"
                "Delivery: waiting-for-binding (this filing binds).\n" % (
                    address.rsplit(".", 1)[1].title(), dispatch,
                    boot_path, address)).encode()
        envelope = parse_draft(body.decode())
        events, advisories = cycles.prepare(self.ledger, envelope, edge)
        supersession_edges, more = supersede.prepare(self.ledger, envelope)
        return admit(self.ledger, self.root, envelope, body,
                     "sid-" + address + "-" + dispatch,
                     admits_against=edge, prechecks=(cycles.precheck,),
                     cycle_events=events, supersession_edges=supersession_edges,
                     advisories=tuple(advisories) + tuple(more))

    def test_two_seats_acknowledge_their_boot_relays(self):
        for address, (dispatch, path) in self.boots.items():
            boot = Path(self.temp.name, path).read_text()
            self.assertIn("DISPATCH_ID: " + dispatch + "\n", boot)
            ack = self.acknowledge(address, dispatch, path, edge=path)
            self.assertEqual(cycles.state(self.ledger, dispatch), "open")
            self.assertEqual(self.ledger.execute(
                "SELECT admits_against_seq FROM relays WHERE seq=?",
                (ack.seq,)).fetchone(),
                self.ledger.execute(
                    "SELECT seq FROM relays WHERE rendered_path=?",
                    (path,)).fetchone())

    def test_acknowledgment_without_edge_is_refused(self):
        dispatch, path = self.boots["v29-a.planner"]
        with self.assertRaises(errors.EngineError) as caught:
            self.acknowledge("v29-a.planner", dispatch, path)
        self.assertEqual(caught.exception.code, "E-ID-COLLISION")

    def test_shared_boot_cycle_id_collides(self):
        first = self.acknowledge("v29-a.planner", "v29-boot",
                                 self.boots["v29-a.planner"][1])
        self.assertIsNotNone(first.rendered_path)
        with self.assertRaises(errors.EngineError) as caught:
            self.acknowledge("v29-b.implementer", "v29-boot",
                             self.boots["v29-b.implementer"][1])
        self.assertEqual(caught.exception.code, "E-ID-COLLISION")


if __name__ == "__main__":
    unittest.main()
