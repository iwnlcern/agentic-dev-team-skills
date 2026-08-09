import os
from pathlib import Path
import tempfile
import unittest

from relay_engine.jcs import commissioned_by_value, jcs_encode
from relay_engine.ledger import init_schema
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.render import (detect_arity, index_rows, render_index,
                                 render_seats, seats_rows)


class TestProjections(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        self.ledger.execute(
            "INSERT INTO meta(key,value) VALUES('run_id','v29')")
        commissioned = commissioned_by_value(
            "123e4567-e89b-12d3-a456-426614174000",
            "parent/IMPL-planner-20260808-000000.md", "a" * 64,
            "b" * 64).decode("utf-8")
        self.ledger.execute(
            "INSERT INTO runs(run_id,commissioned_by) VALUES('v29',?)",
            (commissioned,))

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def add_relay(self, seq, stamp, phase, role, dispatch, parent, sender,
                  recipients, copied, status, path):
        self.ledger.execute(
            "INSERT INTO relays("
            "seq,submission_id,stamp,rendered_path,admits_against_seq,phase,"
            "role,dispatch_id,parent_dispatch_id,run_id,from_seat,to_seats,"
            "cc_seats,status,subject,headers_json,body,body_sha256,"
            "content_hash,origin,occupancy_ref,advisories_json"
            ") VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, "sid-%d" % seq, stamp, path, phase, role, dispatch, parent,
             "v29", sender, jcs_encode(recipients).decode("utf-8"),
             jcs_encode(copied).decode("utf-8"), status, "subject",
             "{}", b"body", "0" * 64, "%064x" % seq, "hand", None,
             "[]"))

    def populate_relays(self):
        values = [
            (1, "20260808-010000", "PLAN", "Planner", "d1", None,
            "v29-a.planner", ["v29-a.implementer"], [], "active", "lane/one.md"),
            (2, "20260808-010001", "PLAN-REVIEW", "Implementer", "d2",
             "d1", "v29-a.implementer", ["v29-a.planner"], ["v29-b.implementer"], "must-revise",
             "lane/two.md"),
            (3, "20260808-010002", "PLAN", "Planner", "d3", "d2",
             "v29-a.planner", ["v29-a.implementer", "v29-b.implementer"], [], "needs|review",
             "lane/three.md"),
            (4, "20260808-010003", "IMPL", "Implementer", "d4", "d3",
             "v29-a.implementer", ["v29-a.planner"], [], "active", "lane/four.md"),
            (5, "20260808-010004", "REVIEW", "Implementer", "d5", "d4",
             "v29-b.implementer", ["v29-a.planner"], ["v29-a.implementer"], "done", "lane/five.md"),
            (6, "20260808-010005", "SITREP", "Planner", "d6", None,
             "v29.orchestrator-planner", ["v29-a.planner"], [], None, "lane/six.md"),
        ]
        for row in values:
            self.add_relay(*row)
        self.ledger.execute(
            "INSERT INTO supersession_edges"
            "(target_seq,ruling_seq,source,applied) VALUES(2,5,'local',1)")

    def golden(self, name):
        return Path(__file__).parents[2].joinpath(
            "relay-engine-fixtures", "golden", name).read_bytes()

    def test_detect_arity_and_eight_column_preservation(self):
        ten = "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n"
        eight = "| time | phase | role | dispatch | to | owner | status | file |\n"
        self.assertEqual(detect_arity(ten), 10)
        self.assertEqual(detect_arity(eight.encode("utf-8")), 8)
        for invalid in ("", "| a | b |\n", "not a table"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                detect_arity(invalid)
        self.populate_relays()
        self.ledger.execute(
            "INSERT INTO meta(key,value) VALUES('index_arity','8')")
        rows = index_rows(self.ledger)
        self.assertTrue(all(len(row) == 8 for row in rows))
        render_index(self.root, rows, 8, "active")
        rendered = Path(self.temp.name, "INDEX.md").read_text()
        self.assertIn("| time | phase | role | dispatch | to | owner | status | file |",
                      rendered)
        self.assertNotIn("| parent |", rendered)

    def test_six_row_index_is_golden_superseded_and_pipe_escaped(self):
        self.populate_relays()
        rows = index_rows(self.ledger)
        self.assertEqual(len(rows), 6)
        result = render_index(self.root, rows, 10, "active")
        rendered = Path(self.temp.name, "INDEX.md").read_bytes()
        self.assertEqual(rendered, self.golden("INDEX-10col.md"))
        self.assertIn(b"| superseded |", rendered)
        self.assertIn(b"needs\\|review", rendered)
        again = render_index(self.root, index_rows(self.ledger), 10,
                             "active")
        self.assertEqual(again.digest, result.digest)
        self.assertIsNone(again.archive_path)

        self.add_relay(
            7, "20260808-010006", "IMPL", "Implementer", "d7", "d6",
            "v29-a.implementer", ["v29-a.planner"], [], "done",
            "lane/seven.md")
        updated = render_index(self.root, index_rows(self.ledger), 10,
                               "active")
        self.assertIsNone(updated.archive_path)
        edited = b"edited index bytes\n"
        Path(self.temp.name, "INDEX.md").write_bytes(edited)
        repaired = render_index(self.root, index_rows(self.ledger), 10,
                                "active")
        self.assertEqual(Path(self.temp.name, repaired.archive_path).read_bytes(),
                         edited)
        self.assertEqual(self.ledger.execute(
            "SELECT relay_seq,target,event FROM projection_events "
            "WHERE target='index' ORDER BY seq").fetchall(), [
                (None, "index", "rendered"),
                (None, "index", "rendered"),
                (None, "index", "rendered"),
                (None, "index", "divergence"),
                (None, "index", "rendered"),
            ])

    def test_seats_are_event_derived_with_activity_label_and_golden(self):
        self.populate_relays()
        events = [
            ("v29-a.planner", "occupied", "occ-a", None),
            ("v29-a.implementer", "occupied", "occ-old", 2),
            ("v29-a.implementer", "replaced", "occ-old", 2),
            ("v29-a.implementer", "occupied", "occ-new", 4),
            ("v29-b.implementer", "occupied", "occ-c", 5),
            ("v29-b.implementer", "stood-down", "occ-c", 5),
            ("v29.orchestrator-planner", "occupied", "occ-top", None),
        ]
        for address, event, occupant, boot in events:
            self.ledger.execute(
                "INSERT INTO seat_events"
                "(address,event,occupant_id,key_id,boot_relay_seq,cause_seq,detail) "
                "VALUES(?,?,?,NULL,?,NULL,NULL)",
                (address, event, occupant, boot))
        rows = seats_rows(self.ledger)
        rendered = render_seats(self.root, rows, "active")
        actual = Path(self.temp.name, "SEATS.md").read_bytes()
        self.assertEqual(actual, self.golden("SEATS.golden.md"))
        self.assertIn(b"last submission (activity)", actual)
        self.assertIn(b"stood-down", actual)
        again = render_seats(self.root, seats_rows(self.ledger), "active")
        self.assertEqual(again.digest, rendered.digest)
        self.assertIsNone(again.archive_path)
        self.assertEqual(self.ledger.execute(
            "SELECT relay_seq,target,event FROM projection_events "
            "WHERE target='seats' ORDER BY seq").fetchall(), [
                (None, "seats", "rendered"),
                (None, "seats", "rendered"),
            ])


if __name__ == "__main__":
    unittest.main()
