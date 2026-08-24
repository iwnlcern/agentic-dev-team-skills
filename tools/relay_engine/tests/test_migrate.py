import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import unittest.mock

from relay_engine import client, daemon, migrate, reconcile
from relay_engine.jcs import jcs_encode
from relay_engine.ledger import epoch_state, init_schema
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.tests.check_crash_matrix import matrix_case
from relay_engine.tests.test_identity_matrix import cid_did


CID, DID = cid_did()


DRAFT = """## relay

ROLE: Orchestrator Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: migration-fixture
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: v29.orchestrator-planner
TO: v29-a.planner
SUBJECT: migration fixture

body
"""

INDEX = b"""# INDEX

| time | phase | role | dispatch | to | owner | status | file |
|---|---|---|---|---|---|---|---|
"""


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Path(self.temp.name, "INDEX.md").write_bytes(INDEX)
        Path(self.temp.name, "legacy").mkdir()
        self.relay_path = (
            "legacy/DESIGN-orchestrator-planner-20260808-010000.md")
        Path(self.temp.name, self.relay_path).write_text(DRAFT)
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def _check(self):
        return migrate.check(self.ledger, self.root)

    def _test_id(self):
        return (self.__class__.__module__ + "." +
                self.__class__.__name__ + "." + self._testMethodName)

    def test_check_snapshot_receipt_and_zero_live_ledger_write(self):
        before = self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0]
        result = self._check()
        self.assertEqual(result["verdict"], "green")
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0], before)
        receipt = json.loads(self.root.open_read(result["receipt"]))
        self.assertEqual(receipt["scratch_row_count"], 1)
        self.assertEqual(receipt["snapshot_manifest_sha256"],
                         hashlib.sha256(jcs_encode(
                             receipt["snapshot_manifest"])).hexdigest())

    def test_equal_size_preserved_mtime_content_change_refused(self):
        result = self._check()
        path = Path(self.temp.name, self.relay_path)
        info = path.stat()
        body = path.read_bytes()
        path.write_bytes(body[:-1] + (b"X" if body[-1:] != b"X" else b"Y"))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        before = self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            migrate.cutover(self.ledger, self.root, result["receipt"])
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0], before)
        self.assertEqual(Path(self.temp.name, "INDEX.md").read_bytes(), INDEX)

    def test_cutover_imports_losslessly_and_preserves_arity(self):
        result = self._check()
        cutover = migrate.cutover(self.ledger, self.root, result["receipt"])
        self.assertEqual(cutover["imported"], [self.relay_path])
        self.assertEqual(epoch_state(self.ledger), "active")
        self.assertEqual(self.ledger.execute(
            "SELECT body FROM relays").fetchone()[0], DRAFT.encode())
        self.assertEqual(self.ledger.execute(
            "SELECT value FROM meta WHERE key='index_arity'").fetchone()[0],
                         "8")

    def test_malformed_legacy_inventoried_green_and_cutover_excludes(self):
        bad = Path(self.temp.name,
                   "legacy/PLAN-planner-20260808-010001.md")
        bad_body = b"not an envelope"
        bad.write_bytes(bad_body)
        result = self._check()
        self.assertEqual(result["verdict"], "green")
        self.assertEqual(result["census"]["malformed_inventoried"], 1)
        self.assertEqual(
            result["census"]["malformed"],
            ["legacy/PLAN-planner-20260808-010001.md"])
        cutover = migrate.cutover(self.ledger, self.root, result["receipt"])
        self.assertEqual(cutover["imported"], [self.relay_path])
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0], 1)
        self.assertEqual(bad.read_bytes(), bad_body)

    def test_cutover_refuses_when_malformed_set_drifts_from_receipt(self):
        bad = Path(self.temp.name,
                   "legacy/PLAN-planner-20260808-010001.md")
        bad.write_bytes(b"not an envelope")
        result = self._check()
        real = reconcile.prepare_candidates

        def drifted(ledger, root, paths):
            candidates, malformed = real(ledger, root, paths)
            return candidates, []

        before = self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0]
        with unittest.mock.patch.object(
                reconcile, "prepare_candidates", drifted):
            with self.assertRaisesRegex(ValueError,
                                        "candidate set mismatch"):
                migrate.cutover(self.ledger, self.root, result["receipt"])
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM relays").fetchone()[0], before)

    def test_pinned_three_row_same_stamp_corpus(self):
        fixture_root = Path(__file__).parents[2] / "relay-engine-fixtures"
        expected = {
            "harness-sample": "cb957fe0b25c831fdca57f790fe50d22808e6cf7a1b868ee3225db8440b9103d",
            "pdc-sample": "cdc0b17abb199ab55b688f0ea966b79ed1ed7dfc67b9ba8bac0c1bd78c4116c4",
            "s375-sample": "21d501f1f40553f77f4911c673e4c9d7a579cfe2174601c639fdec2ac63b2891",
        }
        copied = []
        for lane, digest in expected.items():
            source = (fixture_root / "corpora" / lane /
                      "DESIGN-orchestrator-planner-20260725-194014.md")
            body = source.read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
            target_lane = Path(self.temp.name, lane)
            target_lane.mkdir()
            target = target_lane / source.name
            target.write_bytes(body)
            copied.append(str(target.relative_to(self.temp.name)))
        result = self._check()
        cutover = migrate.cutover(self.ledger, self.root, result["receipt"])
        for path in copied:
            self.assertIn(path, cutover["imported"])
        rows = self.ledger.execute(
            "SELECT stamp,rendered_path,body FROM relays WHERE stamp=? "
            "ORDER BY rendered_path", ("20260725-194014",)).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row[1] for row in rows], sorted(copied))
        for _, path, body in rows:
            self.assertEqual(body, Path(self.temp.name, path).read_bytes())

    def test_migration_census_golden(self):
        result = self._check()
        self.assertEqual(result["census"]["ingested"], 1)
        results_root = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
        matrix_run = os.environ.get("RELAY_ENGINE_MATRIX_RUN")
        if results_root and matrix_run:
            body = ("# v29 engine migration census\n\n"
                    "run: %s\nverdict: %s\ningested: %d\n"
                    "malformed: %d\n" % (
                        matrix_run, result["verdict"],
                        result["census"]["ingested"],
                        result["census"]["malformed_inventoried"])
                    ).encode("ascii")
            path = Path(results_root, "v29-engine-migration-census.md")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                         os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)

    def test_live_check_routes_daemon_and_maintenance_lock_refuses(self):
        socket_name = os.path.join(self.temp.name, "socket-dir", "s")
        read_fd, write_fd = os.pipe()
        thread = threading.Thread(
            target=daemon.start, args=(self.temp.name,), kwargs={
                "ready_fd": write_fd, "socket_override": socket_name,
                "run_id": "v29", "did": DID})
        thread.start()
        self.assertEqual(os.read(read_fd, 1), b"R")
        os.close(read_fd)
        try:
            result = client.request(
                self.temp.name, "migrate.check", {}, cid=CID)
            self.assertEqual(result["verdict"], "green")
            with self.assertRaises(BlockingIOError):
                daemon.acquire_lease(self.root)
            client.request(self.temp.name, "daemon.stop", {}, cid=CID)
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_concurrent_maintenance_starters_refused(self):
        first = daemon.acquire_lease(self.root)
        try:
            with self.assertRaises(BlockingIOError):
                daemon.acquire_lease(self.root)
        finally:
            first.close()

    def test_rollback_detail_pairing_and_startup_completion(self):
        result = self._check()
        cutover = migrate.cutover(self.ledger, self.root, result["receipt"])
        Path(self.temp.name, "INDEX.md").write_bytes(b"candidate\n")
        with self.assertRaisesRegex(RuntimeError,
                                   "rollback-inert-pre-restore"):
            migrate.rollback(self.ledger, self.root, cutover["archive"],
                             fault="rollback-inert-pre-restore")
        pending = migrate.pending_rollback(self.ledger)
        self.assertIsNotNone(pending)
        wrong = migrate.rollback_detail("wrong.archive", "restored")
        self.ledger.execute(
            "INSERT INTO migration_events(event,digest,detail) "
            "VALUES('rollback',?,?)", (pending["digest"], wrong))
        self.assertIsNotNone(migrate.pending_rollback(self.ledger))
        reconcile.startup_recovery({"ledger": self.ledger,
                                    "root": self.root})
        self.assertEqual(Path(self.temp.name, "INDEX.md").read_bytes(), INDEX)
        self.assertIsNone(migrate.pending_rollback(self.ledger))
        rows = self.ledger.execute(
            "SELECT digest,detail FROM migration_events "
            "WHERE event='rollback' ORDER BY seq").fetchall()
        self.assertIn((pending["digest"], migrate.rollback_detail(
            cutover["archive"], "inert")), rows)
        self.assertIn((pending["digest"], migrate.rollback_detail(
            cutover["archive"], "restored")), rows)

    def test_cutover_pre_crash(self):
        with matrix_case("cutover-pre", self._test_id()):
            result = self._check()
            with self.assertRaisesRegex(RuntimeError, "cutover-pre"):
                migrate.cutover(self.ledger, self.root, result["receipt"],
                                fault="cutover-pre")
            self.assertEqual(epoch_state(self.ledger), "inert")
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM relays").fetchone()[0], 0)

    def test_cutover_post_crash(self):
        with matrix_case("cutover-post", self._test_id()):
            result = self._check()
            with self.assertRaisesRegex(RuntimeError, "cutover-post"):
                migrate.cutover(self.ledger, self.root, result["receipt"],
                                fault="cutover-post")
            self.assertEqual(epoch_state(self.ledger), "active")
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM relays").fetchone()[0], 1)

    def test_rollback_inert_pre_restore_crash(self):
        with matrix_case("rollback-inert-pre-restore", self._test_id()):
            result = self._check()
            cutover = migrate.cutover(
                self.ledger, self.root, result["receipt"])
            with self.assertRaisesRegex(RuntimeError,
                                       "rollback-inert-pre-restore"):
                migrate.rollback(
                    self.ledger, self.root, cutover["archive"],
                    fault="rollback-inert-pre-restore")
            self.assertEqual(epoch_state(self.ledger), "inert")
            self.assertIsNotNone(migrate.pending_rollback(self.ledger))


if __name__ == "__main__":
    unittest.main()
