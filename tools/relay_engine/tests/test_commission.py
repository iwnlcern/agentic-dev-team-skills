import base64
import os
from pathlib import Path
import tempfile
import threading
import unittest

from relay_engine import client, commission, daemon, errors
from relay_engine.envelope import parse_draft
from relay_engine.jcs import commissioned_by_value, parse_record
from relay_engine.ledger import admit, establish_run_identity, init_schema
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.tests.check_crash_matrix import matrix_case
from relay_engine.tests.test_identity_matrix import cid_did


CID, DID = cid_did()


DRAFT = """## relay

ROLE: Planner
PHASE: IMPL
AUTHORITY: implementation
DISPATCH_ID: parent-dispatch
RUN_ID: parent
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: parent.planner
TO: parent.implementer
SUBJECT: commission child

body
"""


class TestCommission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        envelope = parse_draft(DRAFT)
        admission = admit(
            self.ledger, self.root, envelope, DRAFT.encode(), "parent-sid")
        self.path = admission.rendered_path

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def test_parent_commit_replay_and_missing_file_rematerialization(self):
        first = commission.commission(
            self.ledger, self.root, self.path, "child-1")
        body = base64.b64decode(first["record_b64"])
        fields, digest = parse_record(body)
        self.assertEqual(fields["child_run_id"], "child-1")
        self.assertEqual(self.ledger.execute(
            "SELECT record_digest FROM commissions").fetchone()[0], digest)
        record_path = Path(self.temp.name, first["record_path"])
        self.assertEqual(record_path.read_bytes(), body)
        record_path.unlink()
        replay = commission.commission(
            self.ledger, self.root, self.path, "child-1")
        self.assertEqual(replay, first)
        self.assertEqual(record_path.read_bytes(), body)
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM commissions").fetchone()[0], 1)

    def test_child_adoption_run_identity_replay_conflict_and_late_gate(self):
        result = commission.commission(
            self.ledger, self.root, self.path, "child-2")
        record = base64.b64decode(result["record_b64"])
        with tempfile.TemporaryDirectory() as child_name:
            child = Root(child_name)
            child_engine = ensure_engine_dir(child.dirfd)
            child_ledger = init_schema(child_engine, child.path)
            try:
                persisted = establish_run_identity(
                    child_ledger, "child-2", record)
                self.assertEqual(establish_run_identity(
                    child_ledger, None, record), persisted)
                self.assertEqual(child_ledger.execute(
                    "SELECT commissioned_by FROM runs").fetchone()[0],
                    persisted)
                fields, digest = parse_record(record)
                expected = commissioned_by_value(
                    fields["parent_root_uuid"], fields["commissioning_path"],
                    fields["dispatch_content_digest"], digest).decode()
                self.assertEqual(persisted, expected)
            finally:
                child_ledger.close()
                os.close(child_engine)
                child.close()

        with tempfile.TemporaryDirectory() as late_name:
            late = Root(late_name)
            late_engine = ensure_engine_dir(late.dirfd)
            late_ledger = init_schema(late_engine, late.path)
            try:
                late_ledger.execute(
                    "INSERT INTO seat_events(address,event) "
                    "VALUES('child.planner','occupied')")
                with self.assertRaises(errors.EngineError) as refused:
                    establish_run_identity(late_ledger, "child-2", record)
                self.assertEqual(refused.exception.code, "commission-late")
                self.assertIsNone(late_ledger.execute(
                    "SELECT value FROM meta WHERE key='run_id'").fetchone())
            finally:
                late_ledger.close()
                os.close(late_engine)
                late.close()

    def test_invalid_child_id_refuses_before_file_or_row(self):
        with self.assertRaises(errors.EngineError) as invalid:
            commission.commission(self.ledger, self.root, self.path, "../bad")
        self.assertEqual(invalid.exception.code, "run-id-invalid")
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM commissions").fetchone()[0], 0)
        self.assertFalse(Path(self.temp.name, ".engine/exports").exists())

    def test_combined_start_orders_identity_commission_and_top_seat(self):
        result = commission.commission(
            self.ledger, self.root, self.path, "child-combined")
        record = base64.b64decode(result["record_b64"])
        with tempfile.TemporaryDirectory() as child_name:
            socket_name = os.path.join(child_name, "socket-dir", "s")
            read_fd, write_fd = os.pipe()
            thread = threading.Thread(
                target=daemon.start, args=(child_name,),
                kwargs={
                    "ready_fd": write_fd, "socket_override": socket_name,
                    "run_id": "child-combined",
                    "commissioning_record": record,
                    "top_seat": "child-combined.orchestrator-planner",
                    "did": DID,
                })
            thread.start()
            self.assertEqual(os.read(read_fd, 1), b"R")
            os.close(read_fd)
            client.request(child_name, "daemon.stop", {}, cid=CID)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            with Root(child_name) as child:
                child_engine = ensure_engine_dir(child.dirfd)
                try:
                    from relay_engine.ledger import open_ledger
                    child_ledger = open_ledger(child_engine)
                    try:
                        persisted = child_ledger.execute(
                            "SELECT commissioned_by FROM runs").fetchone()[0]
                        self.assertEqual(child_ledger.execute(
                            "SELECT value FROM meta WHERE key='run_id'")
                            .fetchone()[0], "child-combined")
                        self.assertEqual(child_ledger.execute(
                            "SELECT COUNT(*) FROM relays").fetchone()[0], 0)
                        self.assertEqual(child_ledger.execute(
                            "SELECT boot_relay_seq FROM seat_events")
                            .fetchone(), (None,))
                    finally:
                        child_ledger.close()
                finally:
                    os.close(child_engine)
            self.assertIn(persisted,
                          Path(child_name, "INDEX.md").read_text())

    def test_invalid_fresh_start_leaves_no_run_record_file(self):
        with tempfile.TemporaryDirectory() as child_name:
            read_fd, write_fd = os.pipe()
            failures = []

            def run():
                try:
                    daemon.start(child_name, ready_fd=write_fd,
                                 run_id="../invalid", did=DID)
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertEqual(os.read(read_fd, 1), b"")
            os.close(read_fd)
            thread.join(5)
            self.assertTrue(failures)
            self.assertEqual(failures[0].code, "run-id-invalid")
            self.assertFalse(Path(child_name,
                                  ".engine/ledger.db").exists())

    def test_commissioning_commands_down_have_zero_effects(self):
        before = sorted(
            (path.relative_to(self.temp.name), path.read_bytes())
            for path in Path(self.temp.name).rglob("*") if path.is_file())
        operations = (
            lambda: client.commission_run(
                self.temp.name, self.path, "child-down", cid=CID),
            lambda: client.adopt_commission(
                self.temp.name, b"record", cid=CID),
            lambda: client.adopt_ruling(
                self.temp.name, b"bundle", cid=CID),
        )
        for operation in operations:
            with self.assertRaises(client.RemoteError) as down:
                operation()
            self.assertEqual(down.exception.code, "E-DAEMON-DOWN")
        after = sorted(
            (path.relative_to(self.temp.name), path.read_bytes())
            for path in Path(self.temp.name).rglob("*") if path.is_file())
        self.assertEqual(after, before)

    def test_commission_txn_crash(self):
        test_id = (self.__class__.__module__ + "." +
                   self.__class__.__name__ + "." + self._testMethodName)
        with matrix_case("commission-txn", test_id):
            self.ledger.execute("BEGIN IMMEDIATE")
            self.ledger.execute(
                "INSERT INTO commissions(child_run_id,dispatch_seq,"
                "dispatch_content_digest,record_digest) VALUES(?,?,?,?)",
                ("crash-child", 1, "a" * 64, "b" * 64))
            self.ledger.execute("ROLLBACK")
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM commissions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
