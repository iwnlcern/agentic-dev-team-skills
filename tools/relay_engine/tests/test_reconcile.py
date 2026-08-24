import argparse
import base64
import io
import os
from pathlib import Path
import tempfile
import unittest

from relay_engine import cli, client, migrate, reconcile
from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, init_schema
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.render import render_relay
from relay_engine.tests.test_identity_matrix import cid_did


CID, DID = cid_did()


DRAFT = """## relay

ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: reconcile-plan
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: v29-a.planner
TO: v29-a.implementer
SUBJECT: reconcile fixture

body
"""


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        self.row = admit(
            self.ledger, self.root, parse_draft(DRAFT), DRAFT.encode(),
            "reconcile-sid")
        render_relay(self.ledger, self.root, self.row.seq)

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def test_verify_repairs_missing_and_detects_modified(self):
        path = Path(self.temp.name, self.row.rendered_path)
        path.unlink()
        result = reconcile.verify(self.ledger, self.root)
        self.assertEqual(path.read_bytes(), DRAFT.encode())
        self.assertIn("missing", [entry["event"]
                                  for entry in result["dispositions"]])
        path.write_bytes(b"changed")
        result = reconcile.verify(self.ledger, self.root)
        self.assertFalse(result["ok"])
        self.assertIn("modified", [entry["event"]
                                   for entry in result["dispositions"]])
        self.assertEqual(path.read_bytes(), b"changed")

    def test_four_projection_classes_share_reader_contract(self):
        self.ledger.executemany(
            "INSERT INTO projection_events("
            "relay_seq,target,event,digest,detail) VALUES(?,?,?,?,?)", [
                (self.row.seq, "relay", "conflict", "a" * 64,
                 self.row.rendered_path),
                (None, "index", "divergence", "b" * 64, "INDEX.md"),
                (None, "seats", "rendered", "c" * 64, "SEATS.md"),
                (None, "foreign-path", "divergence", "d" * 64,
                 "foreign/occupied.md"),
            ])
        entries = reconcile.projection_entries(self.ledger)[-4:]
        self.assertEqual({entry["target"]: entry["path"] for entry in entries}, {
            "relay": self.row.rendered_path,
            "index": "INDEX.md", "seats": "SEATS.md",
            "foreign-path": "foreign/occupied.md",
        })
        status = reconcile.status(self.ledger, {"state": "ready"})
        verified = reconcile.verify(self.ledger, self.root)
        for target in {"relay", "index", "seats", "foreign-path"}:
            self.assertIn(target, {entry["target"]
                                   for entry in status["projection_events"]})
            self.assertIn(target, {entry["target"]
                                   for entry in verified["dispositions"]})

    def test_reconcile_ingests_only_admissible_namespace(self):
        hand = DRAFT.replace("reconcile-plan", "hand-plan")
        lane = Path(self.temp.name, "hand")
        lane.mkdir()
        path = lane / "PLAN-planner-20260808-010000.md"
        path.write_text(hand)
        Path(self.temp.name, "notes.txt").write_text("inventory")
        result = reconcile.reconcile(self.ledger, self.root)
        self.assertEqual(result["ingested"], [
            "hand/PLAN-planner-20260808-010000.md"])
        self.assertIn("notes.txt", result["inventoried"])
        again = reconcile.reconcile(self.ledger, self.root)
        self.assertEqual(again["ingested"], [])

    def test_reconcile_batch_is_atomic_and_path_ordered(self):
        lane = Path(self.temp.name, "hand")
        lane.mkdir()
        for stamp in ("20260808-010002", "20260808-010001"):
            path = lane / ("PLAN-planner-%s.md" % stamp)
            path.write_text(DRAFT.replace("reconcile-plan",
                                          "batch-" + stamp))
        trace = []
        self.ledger.set_trace_callback(trace.append)
        try:
            result = reconcile.reconcile(self.ledger, self.root)
        finally:
            self.ledger.set_trace_callback(None)
        self.assertEqual(result["ingested"], [
            "hand/PLAN-planner-20260808-010001.md",
            "hand/PLAN-planner-20260808-010002.md"])
        self.assertEqual(sum(value == "BEGIN IMMEDIATE" for value in trace), 1)
        self.assertEqual(sum(value == "COMMIT" for value in trace), 1)

    def test_legacy_inert_startup_preserves_projections(self):
        self.ledger.execute(
            "INSERT INTO migration_events(event,digest,detail) "
            "VALUES('rollback',?,?)",
            ("e" * 64, migrate.rollback_detail("missing", "restored")))
        index = Path(self.temp.name, "INDEX.md")
        seats = Path(self.temp.name, "SEATS.md")
        index.write_bytes(b"legacy index\n")
        seats.write_bytes(b"legacy seats\n")
        reconcile.startup_recovery({"ledger": self.ledger,
                                    "root": self.root})
        self.assertEqual(index.read_bytes(), b"legacy index\n")
        self.assertEqual(seats.read_bytes(), b"legacy seats\n")

    def test_interrupted_rollback_restores_before_inert_reconcile(self):
        archive = Path(self.temp.name, ".engine", "archive")
        archive.mkdir()
        archived = b"restored legacy index\n"
        archive_path = ".engine/archive/index.archive"
        Path(self.temp.name, archive_path).write_bytes(archived)
        digest = __import__("hashlib").sha256(archived).hexdigest()
        self.ledger.execute(
            "INSERT INTO migration_events(event,digest,detail) "
            "VALUES('rollback',?,?)",
            (digest, migrate.rollback_detail(archive_path, "inert")))
        Path(self.temp.name, "INDEX.md").write_bytes(b"candidate\n")
        reconcile.startup_recovery({"ledger": self.ledger,
                                    "root": self.root})
        self.assertEqual(Path(self.temp.name, "INDEX.md").read_bytes(),
                         archived)
        self.assertIsNone(migrate.pending_rollback(self.ledger))


class _CapturedText:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, value):
        raise AssertionError("authored text reached verbatim stream")

    def flush(self):
        pass


class TestShowBody(unittest.TestCase):
    def test_show_body_byte_exact(self):
        self.assertEqual(CID, DID)
        body = b"\x00database-shaped\nraw\xff"
        real_request = client.request
        real_stdout = cli.sys.stdout
        client.request = lambda *args, **kwargs: {
            "seq": 1, "body_b64": base64.b64encode(body).decode("ascii")}
        captured = _CapturedText()
        cli.sys.stdout = captured
        try:
            with tempfile.TemporaryDirectory() as root:
                Path(root, ".engine").mkdir()
                args = argparse.Namespace(
                    root=root, target="1", body=True, timeout=1.0)
                self.assertEqual(cli.cmd_show(args), 0)
        finally:
            cli.sys.stdout = real_stdout
            client.request = real_request
        self.assertEqual(captured.buffer.getvalue(), body)

    def test_show_body_no_authored_text_route(self):
        self.assertNotIn("emit_verbatim", cli._report_command_error.__code__
                         .co_names)


if __name__ == "__main__":
    unittest.main()
