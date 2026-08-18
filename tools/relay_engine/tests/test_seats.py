import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import io
import json
from contextlib import redirect_stdout

from relay_engine import cli, client, daemon, errors, seats
from relay_engine.ledger import init_schema, open_ledger
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.tests.check_crash_matrix import matrix_case
from relay_engine.tests.test_identity_matrix import cid_did


CID, DID = cid_did()


class TestSeats(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        self.ledger.execute(
            "INSERT INTO meta(key,value) VALUES('run_id','v29')")

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def test_registration_boot_relay_and_occupancy_are_bound(self):
        result = seats.register(
            self.ledger, self.root, "v29-a.planner", "Planner",
            "v29-a-boot")
        key = Path(self.temp.name, result["key_path"])
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        row = self.ledger.execute(
            "SELECT event,occupant_id,key_id,boot_relay_seq "
            "FROM seat_events").fetchone()
        self.assertEqual(row[0], "occupied")
        self.assertEqual(row[1], result["occupant_id"])
        self.assertIsNotNone(row[2])
        self.assertEqual(self.ledger.execute(
            "SELECT rendered_path FROM relays WHERE seq=?",
            (row[3],)).fetchone()[0], result["boot_relay"])
        body = Path(self.temp.name, result["boot_relay"]).read_text()
        self.assertIn(result["occupant_id"], body)
        self.assertNotIn(key.read_text().strip(), body)
        self.assertTrue(Path(self.temp.name, "SEATS.md").exists())

    def test_duplicate_replace_old_key_and_stand_down(self):
        first = seats.register(
            self.ledger, self.root, "v29-a.implementer", "Implementer")
        old_key = Path(self.temp.name, first["key_path"])
        old_bytes = old_key.read_bytes()
        with self.assertRaises(errors.EngineError) as occupied:
            seats.register(self.ledger, self.root,
                           "v29-a.implementer", "Implementer")
        self.assertEqual(occupied.exception.code, "seat-occupied")
        second = seats.replace(
            self.ledger, self.root, "v29-a.implementer", "Implementer")
        self.assertEqual(old_key.read_bytes(), old_bytes)
        self.assertFalse(seats.tag_is_current(
            self.ledger, self.root, "v29-a.implementer",
            old_bytes.decode("ascii").strip()))
        new_tag = Path(self.temp.name, second["key_path"]).read_text().strip()
        self.assertTrue(seats.tag_is_current(
            self.ledger, self.root, "v29-a.implementer", new_tag))
        self.assertEqual([row[0] for row in self.ledger.execute(
            "SELECT event FROM seat_events ORDER BY seq")],
            ["occupied", "replaced", "occupied"])
        seats.stand_down(self.ledger, self.root, "v29-a.implementer")
        self.assertEqual(seats.show(
            self.ledger, "v29-a.implementer")["history"][-1]["event"],
            "stood-down")

    def test_top_seat_has_no_boot_relay_row(self):
        result = seats.register(
            self.ledger, self.root, "v29.orchestrator-planner",
            "Orchestrator Planner", top=True)
        self.assertIsNone(result["boot_relay"])
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0], 0)
        self.assertEqual(self.ledger.execute(
            "SELECT event,boot_relay_seq FROM seat_events").fetchone(),
            ("occupied", None))
        self.assertIn("v29.orchestrator-planner",
                      Path(self.temp.name, "SEATS.md").read_text())

    def test_daemon_top_seat_and_full_wire_surface(self):
        self.ledger.close()
        self.ledger = open_ledger(self.engine_fd)
        socket_name = os.path.join(self.temp.name, "socket-dir", "s")
        read_fd, write_fd = os.pipe()
        thread = threading.Thread(
            target=daemon.start, args=(self.temp.name,),
            kwargs={"ready_fd": write_fd, "socket_override": socket_name,
                    "top_seat": "v29.orchestrator-planner", "did": DID})
        thread.start()
        self.assertEqual(os.read(read_fd, 1), b"R")
        os.close(read_fd)
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = cli.main([
                    "seat", "register", "v29-b.planner", "--role",
                    "Planner", "--root", self.temp.name])
            self.assertEqual(status, 0)
            issued = json.loads(stdout.getvalue())
            self.assertTrue(Path(self.temp.name,
                                 issued["key_path"]).exists())
            replaced = client.seat_register(
                self.temp.name, "v29-b.planner", "Planner", replace=True,
                cid=CID)
            self.assertNotEqual(issued["occupant_id"],
                                replaced["occupant_id"])
            history = client.seat_show(
                self.temp.name, "v29-b.planner", cid=CID)["history"]
            self.assertEqual([row["event"] for row in history],
                             ["occupied", "replaced", "occupied"])
            roster = client.request(self.temp.name, "roster", {}, cid=CID)
            self.assertEqual(roster["count"], 2)
            client.seat_stand_down(
                self.temp.name, "v29-b.planner", cid=CID)
            client.request(self.temp.name, "daemon.stop", {}, cid=CID)
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())
        top = self.ledger.execute(
            "SELECT boot_relay_seq FROM seat_events WHERE address=?",
            ("v29.orchestrator-planner",)).fetchone()
        self.assertEqual(top, (None,))

    def test_registration_down_has_zero_effects(self):
        before = sorted(
            (path.relative_to(self.temp.name), path.read_bytes())
            for path in Path(self.temp.name).rglob("*") if path.is_file())
        with self.assertRaises(client.RemoteError) as down:
            client.seat_register(
                self.temp.name, "v29-c.implementer", "Implementer")
        self.assertEqual(down.exception.code, "E-DAEMON-DOWN")
        after = sorted(
            (path.relative_to(self.temp.name), path.read_bytes())
            for path in Path(self.temp.name).rglob("*") if path.is_file())
        self.assertEqual(after, before)

    def test_registration_txn_crash(self):
        test_id = (self.__class__.__module__ + "." +
                   self.__class__.__name__ + "." + self._testMethodName)
        with matrix_case("registration-txn", test_id):
            before = self.ledger.execute(
                "SELECT COUNT(*) FROM seat_events").fetchone()[0]
            with self.assertRaises(Exception):
                from relay_engine.ledger import append_seat_events
                append_seat_events(self.ledger, [{
                    "address": "v29-crash.planner", "event": "occupied",
                    "occupant_id": "one", "key_id": "one",
                }, {
                    "address": "v29-crash.planner", "event": "invalid",
                    "occupant_id": "two", "key_id": "two",
                }])
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM seat_events").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
