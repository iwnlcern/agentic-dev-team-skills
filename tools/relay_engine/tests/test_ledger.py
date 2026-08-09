import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from relay_engine import errors
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.jcs import commissioned_by_value, frame_record
from relay_engine.ledger import (InjectedFault, admit, epoch_state,
                                 establish_run_identity, init_schema,
                                 open_ledger)
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.tests.check_crash_matrix import matrix_case


DRAFT = """## relay

ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: v29-engine-plan-1
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: v29-gates.planner
TO: v29-gates.implementer
CC: v29.orchestrator-planner
STATUS: active
SUBJECT: exercise ledger

relay body
"""
BODY = DRAFT.encode("utf-8")
WALL = time.mktime((2026, 8, 8, 12, 0, 0, 0, 0, -1))


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.envelope = parse_draft(DRAFT)
        self.ledger = None

    def tearDown(self):
        if self.ledger is not None:
            self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def initialize(self, legacy=False):
        if legacy:
            Path(self.temp.name, "INDEX.md").write_bytes(b"legacy\n")
        self.ledger = init_schema(self.engine_fd, self.root.path)
        return self.ledger

    def submit(self, submission_id="sid-1", **kwargs):
        claims = {
            "claimed_body_sha256": body_sha256(BODY),
            "claimed_content_hash": content_hash(
                self.envelope, BODY, kwargs.get("admits_against")),
        }
        claims.update(kwargs)
        return admit(self.ledger, self.root, self.envelope, BODY,
                     submission_id, **claims)


class TestSchemaAndEpoch(LedgerFixture):
    def test_exact_schema_pragmas_meta_and_fresh_epoch(self):
        ledger = self.initialize()
        self.assertEqual(ledger.execute("PRAGMA journal_mode").fetchone()[0],
                         "wal")
        self.assertEqual(ledger.execute("PRAGMA synchronous").fetchone()[0],
                         2)
        self.assertEqual(ledger.execute("PRAGMA foreign_keys").fetchone()[0],
                         1)
        self.assertEqual(
            {row[0] for row in ledger.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")},
            {"relays", "seat_events", "cycle_events",
             "supersession_edges", "projection_events",
             "migration_events", "runs", "commissions", "meta"},
        )
        relay_columns = [row[1] for row in ledger.execute(
            "PRAGMA table_info(relays)")]
        self.assertEqual(
            relay_columns,
            ["seq", "submission_id", "stamp", "rendered_path",
             "admits_against_seq", "phase", "role", "dispatch_id",
             "parent_dispatch_id", "run_id", "from_seat", "to_seats",
             "cc_seats", "status", "subject", "headers_json", "body",
             "body_sha256", "content_hash", "origin", "occupancy_ref",
             "advisories_json"],
        )
        index_sql = ledger.execute(
            "SELECT sql FROM sqlite_master WHERE name=?",
            ("ux_relays_stamp_daemon",)).fetchone()[0]
        self.assertIn("UNIQUE INDEX", index_sql)
        self.assertIn("WHERE origin='daemon'", index_sql)
        meta = dict(ledger.execute("SELECT key,value FROM meta"))
        self.assertEqual(meta["schema_version"], "1")
        self.assertEqual(meta["canonical_root"], self.root.path)
        self.assertEqual(len(meta["root_uuid"]), 36)
        self.assertNotIn("run_id", meta)
        self.assertEqual(
            [row[0] for row in ledger.execute(
                "SELECT event FROM migration_events ORDER BY seq")],
            ["epoch-open", "cutover"],
        )
        self.assertEqual(epoch_state(ledger), "active")

    def test_legacy_root_and_empty_event_stream_are_inert(self):
        ledger = self.initialize(legacy=True)
        self.assertEqual(
            [row[0] for row in ledger.execute(
                "SELECT event FROM migration_events ORDER BY seq")],
            ["epoch-open"],
        )
        self.assertEqual(epoch_state(ledger), "inert")
        ledger.execute("DELETE FROM migration_events")
        self.assertEqual(epoch_state(ledger), "inert")

    def test_init_faults_leave_complete_or_absent_schema(self):
        for point in ("after-ddl", "after-meta", "after-epoch"):
            with self.subTest(point=point):
                temp = tempfile.TemporaryDirectory()
                root = Root(temp.name)
                engine_fd = ensure_engine_dir(root.dirfd)
                try:
                    with self.assertRaises(InjectedFault):
                        connection = init_schema(
                            engine_fd, root.path, fault=point)
                        connection.close()
                    connection = open_ledger(engine_fd)
                    try:
                        tables = {row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table'")}
                        self.assertEqual(tables, set())
                        self.assertEqual(epoch_state(connection), "inert")
                    finally:
                        connection.close()
                finally:
                    os.close(engine_fd)
                    root.close()
                    temp.cleanup()

    def test_open_uses_held_engine_directory_from_unrelated_cwd(self):
        self.initialize().close()
        self.ledger = None
        unrelated = tempfile.TemporaryDirectory()
        old_cwd = os.getcwd()
        try:
            os.chdir(unrelated.name)
            self.ledger = open_ledger(self.engine_fd)
            stored = self.ledger.execute(
                "SELECT value FROM meta WHERE key='canonical_root'").fetchone()
            self.assertEqual(stored, (self.root.path,))
        finally:
            os.chdir(old_cwd)
            unrelated.cleanup()


class TestRunIdentity(LedgerFixture):
    @staticmethod
    def carrier(child="v29"):
        return frame_record({
            "v": 1,
            "parent_root_uuid": "123e4567-e89b-12d3-a456-426614174000",
            "commissioning_path": "v29-engine/IMPL-planner-20260808-000000.md",
            "dispatch_content_digest": "a" * 64,
            "child_run_id": child,
        })

    def test_identity_and_commission_commit_together(self):
        ledger = self.initialize()
        carrier = self.carrier()
        persisted = establish_run_identity(ledger, "v29", carrier)
        record_digest = carrier.splitlines()[1].decode("ascii")
        expected = commissioned_by_value(
            "123e4567-e89b-12d3-a456-426614174000",
            "v29-engine/IMPL-planner-20260808-000000.md", "a" * 64,
            record_digest).decode("utf-8")
        self.assertEqual(persisted, expected)
        self.assertEqual(ledger.execute(
            "SELECT value FROM meta WHERE key='run_id'").fetchone(),
                         ("v29",))
        self.assertEqual(ledger.execute(
            "SELECT run_id,commissioned_by FROM runs").fetchone(),
                         ("v29", expected))
        self.assertEqual(establish_run_identity(ledger, "v29", carrier),
                         expected)
        self.assertEqual(ledger.execute(
            "SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_invalid_carrier_and_identity_fault_roll_back_both(self):
        for carrier, fault in ((self.carrier("other"), None),
                               (self.carrier(), "after-run-id"),
                               (self.carrier(), "after-runs"),
                               (self.carrier(), "pre-commit")):
            with self.subTest(fault=fault, child=carrier[:20]):
                temp = tempfile.TemporaryDirectory()
                root = Root(temp.name)
                engine_fd = ensure_engine_dir(root.dirfd)
                connection = init_schema(engine_fd, root.path)
                try:
                    with self.assertRaises((ValueError, InjectedFault,
                                            errors.EngineError)):
                        establish_run_identity(connection, "v29", carrier,
                                               fault=fault)
                    self.assertIsNone(connection.execute(
                        "SELECT value FROM meta WHERE key='run_id'").fetchone())
                    self.assertEqual(connection.execute(
                        "SELECT count(*) FROM runs").fetchone()[0], 0)
                finally:
                    connection.close()
                    os.close(engine_fd)
                    root.close()
                    temp.cleanup()

    def test_run_id_lifecycle(self):
        ledger = self.initialize()
        self.assertEqual(establish_run_identity(ledger, "v29"), "v29")
        self.assertEqual(establish_run_identity(ledger, None), "v29")
        self.assertEqual(establish_run_identity(ledger, "v29"), "v29")
        with self.assertRaises(errors.EngineError) as mismatch:
            establish_run_identity(ledger, "other")
        self.assertEqual(mismatch.exception.code, "run-id-mismatch")


class TestAdmission(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        os.mkdir(Path(self.temp.name, "v29-engine"))

    def test_monotonic_stamp_replay_and_changed_identity(self):
        first = self.submit(clock=lambda: WALL)
        second = self.submit("sid-2", clock=lambda: WALL - 3600)
        self.assertEqual(first.stamp, "20260808-120000")
        self.assertEqual(second.stamp, "20260808-120001")
        self.assertEqual(
            first.rendered_path,
            "v29-engine/PLAN-planner-20260808-120000.md")
        replay = self.submit(clock=lambda: WALL + 99)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.seq, first.seq)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM relays").fetchone()[0], 2)
        with self.assertRaises(errors.EngineError) as changed:
            admit(self.ledger, self.root, self.envelope, BODY + b"changed",
                  "sid-1", claimed_body_sha256=body_sha256(BODY + b"changed"),
                  claimed_content_hash=content_hash(
                      self.envelope, BODY + b"changed", None),
                  clock=lambda: WALL + 100)
        self.assertEqual(changed.exception.code, "E-REPLAY-MISMATCH")

    def test_server_hashes_and_edge_resolution(self):
        first = self.submit(clock=lambda: WALL)
        second = self.submit("sid-2", admits_against=first.rendered_path,
                             clock=lambda: WALL + 1)
        self.assertEqual(self.ledger.execute(
            "SELECT admits_against_seq FROM relays WHERE seq=?",
            (second.seq,)).fetchone(), (first.seq,))
        with self.assertRaises(errors.EngineError) as changed_edge:
            self.submit("sid-2", admits_against=None,
                        clock=lambda: WALL + 2)
        self.assertEqual(changed_edge.exception.code, "E-REPLAY-MISMATCH")
        for edge in ("missing/relay.md", "../escape.md"):
            with self.subTest(edge=edge), self.assertRaises(
                    errors.EngineError) as bad_edge:
                self.submit("edge-" + edge, admits_against=edge,
                            clock=lambda: WALL + 3)
            self.assertEqual(bad_edge.exception.code, "E-ENVELOPE")
        with self.assertRaises(errors.EngineError) as mismatch:
            admit(self.ledger, self.root, self.envelope, BODY, "bad-hash",
                  claimed_body_sha256="0" * 64,
                  claimed_content_hash=content_hash(
                      self.envelope, BODY, None), clock=lambda: WALL + 4)
        self.assertEqual(mismatch.exception.code, "E-ENVELOPE")
        altered = parse_draft(DRAFT.replace(
            "SUBJECT: exercise ledger", "SUBJECT: changed client envelope"))
        with self.assertRaises(errors.EngineError) as envelope_mismatch:
            admit(
                self.ledger, self.root, altered, BODY, "bad-envelope",
                claimed_body_sha256=body_sha256(BODY),
                claimed_content_hash=content_hash(altered, BODY, None),
                clock=lambda: WALL + 5)
        self.assertEqual(envelope_mismatch.exception.code, "E-ENVELOPE")

    def test_hand_rows_can_share_stamp(self):
        stamp = "20260808-115959"
        for number in range(3):
            result = admit(
                self.ledger, self.root, self.envelope, BODY,
                "hand-%d" % number, origin="hand", stamp=stamp,
                rendered_path="v29-engine/PLAN-planner-%s-%d.md" %
                              (stamp, number),
                claimed_body_sha256=body_sha256(BODY),
                claimed_content_hash=content_hash(
                    self.envelope, BODY, None))
            self.assertEqual(result.stamp, stamp)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM relays WHERE stamp=?", (stamp,)
        ).fetchone()[0], 3)

    def test_concurrent_retry_collapses_to_one_row(self):
        self.ledger.close()
        self.ledger = None
        barrier = threading.Barrier(8)
        results = []
        failures = []

        def worker():
            connection = open_ledger(self.engine_fd)
            try:
                barrier.wait()
                result = admit(
                    connection, self.root, self.envelope, BODY,
                    "concurrent", claimed_body_sha256=body_sha256(BODY),
                    claimed_content_hash=content_hash(
                        self.envelope, BODY, None), clock=lambda: WALL)
                results.append((result.seq, result.rendered_path))
            except BaseException as exc:
                failures.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(set(results)), 1)
        self.ledger = open_ledger(self.engine_fd)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM relays").fetchone()[0], 1)

    def test_occupied_paths_classify_known_and_foreign(self):
        candidate = "v29-engine/PLAN-planner-20260808-120000.md"
        imported = admit(
            self.ledger, self.root, self.envelope, BODY, "hand",
            origin="hand", stamp="20260808-120000", rendered_path=candidate,
            claimed_body_sha256=body_sha256(BODY),
            claimed_content_hash=content_hash(
                self.envelope, BODY, None))
        self.assertGreater(imported.seq, 0)
        Path(self.temp.name, candidate).write_bytes(BODY)
        known = self.submit("known", clock=lambda: WALL)
        self.assertEqual(known.stamp, "20260808-120001")
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM projection_events").fetchone()[0], 0)

        foreign_path = "v29-engine/PLAN-planner-20260808-120002.md"
        foreign_bytes = b"foreign direct bytes\n"
        Path(self.temp.name, foreign_path).write_bytes(foreign_bytes)
        foreign = self.submit("foreign", clock=lambda: WALL)
        self.assertEqual(foreign.stamp, "20260808-120003")
        self.assertEqual(self.ledger.execute(
            "SELECT relay_seq,target,event,digest,detail "
            "FROM projection_events").fetchall(), [
                (None, "foreign-path", "divergence",
                 hashlib.sha256(foreign_bytes).hexdigest(), foreign_path)
            ])

    def test_foreign_event_rolls_back_and_commits_with_admission(self):
        candidate = "v29-engine/PLAN-planner-20260808-120000.md"
        occupant = b"squat\n"
        Path(self.temp.name, candidate).write_bytes(occupant)
        with self.assertRaises(InjectedFault):
            self.submit("fault", clock=lambda: WALL, fault="pre-commit")
        self.ledger.close()
        self.ledger = open_ledger(self.engine_fd)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM relays").fetchone()[0], 0)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM projection_events").fetchone()[0], 0)
        with self.assertRaises(InjectedFault) as committed:
            self.submit("fault", clock=lambda: WALL,
                        fault="post-commit-pre-response")
        self.assertTrue(committed.exception.committed)
        self.ledger.close()
        self.ledger = open_ledger(self.engine_fd)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM relays").fetchone()[0], 1)
        self.assertEqual(self.ledger.execute(
            "SELECT target,event,digest,detail FROM projection_events"
        ).fetchone(),
            ("foreign-path", "divergence",
             hashlib.sha256(occupant).hexdigest(), candidate))

    def test_fault_points_make_row_advisories_and_effects_atomic(self):
        points = ("after-relay-insert", "after-cycle-events",
                  "after-supersession-edges", "pre-commit")
        for index, point in enumerate(points):
            with self.subTest(point=point), self.assertRaises(InjectedFault):
                self.submit(
                    "fault-%d" % index, clock=lambda: WALL + index,
                    advisories=lambda ledger, envelope: [
                        {"code": "advisory-demo"}],
                    cycle_events=[{
                        "dispatch_id": "v29-engine-plan-1",
                        "event": "open", "commissioning_seq": None,
                        "cause_seq": None,
                    }],
                    supersession_edges=[{
                        "target_seq": 41, "ruling_seq": 42,
                        "source": "local", "applied": 1,
                    }], fault=point)
        for table in ("relays", "cycle_events", "supersession_edges"):
            self.assertEqual(self.ledger.execute(
                "SELECT count(*) FROM " + table).fetchone()[0], 0)

        with self.assertRaises(InjectedFault) as committed:
            self.submit(
                "committed", clock=lambda: WALL,
                advisories=lambda ledger, envelope: [{"code": "advisory-demo"}],
                cycle_events=[{
                    "dispatch_id": "v29-engine-plan-1", "event": "open",
                    "commissioning_seq": None, "cause_seq": None,
                }],
                supersession_edges=[{
                    "target_seq": 41, "ruling_seq": 42,
                    "source": "local", "applied": 1,
                }], fault="post-commit-pre-response")
        self.assertTrue(committed.exception.committed)
        self.assertEqual(self.ledger.execute(
            "SELECT advisories_json FROM relays").fetchone(),
            ('[{"code":"advisory-demo"}]',))
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM cycle_events").fetchone()[0], 1)
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM supersession_edges").fetchone()[0], 1)

    def test_after_insert_fault_observes_advisories_in_the_single_row(self):
        observed = []

        def inspect_then_fail(point):
            if point == "after-relay-insert":
                observed.append(self.ledger.execute(
                    "SELECT advisories_json FROM relays").fetchone())
                raise InjectedFault(point)

        with self.assertRaises(InjectedFault):
            self.submit(
                "inspect", clock=lambda: WALL,
                advisories=lambda ledger, envelope: [{"code": "inside"}],
                fault=inspect_then_fail)
        self.assertEqual(observed, [('[{"code":"inside"}]',)])
        self.assertEqual(self.ledger.execute(
            "SELECT count(*) FROM relays").fetchone()[0], 0)

    def test_policy_callbacks_run_before_advisories_and_insert(self):
        trace = []

        def precheck(ledger, envelope, edge_seq):
            trace.append(("policy", edge_seq))

        def advisories(ledger, envelope):
            trace.append(("advisories", ledger.execute(
                "SELECT count(*) FROM relays").fetchone()[0]))
            return []

        self.submit(prechecks=[precheck], advisories=advisories,
                    clock=lambda: WALL)
        self.assertEqual(trace, [("policy", None), ("advisories", 0)])

    def test_ledger_module_has_no_application_update_or_delete(self):
        source = Path(__file__).parents[1].joinpath("ledger.py").read_text()
        upper = source.upper()
        for token in ("UPDATE RELAYS", "DELETE FROM RELAYS",
                      "UPDATE SEAT_EVENTS", "DELETE FROM SEAT_EVENTS",
                      "UPDATE CYCLE_EVENTS", "DELETE FROM CYCLE_EVENTS",
                      "UPDATE SUPERSESSION_EDGES",
                      "DELETE FROM SUPERSESSION_EDGES",
                      "UPDATE PROJECTION_EVENTS",
                      "DELETE FROM PROJECTION_EVENTS",
                      "UPDATE MIGRATION_EVENTS",
                      "DELETE FROM MIGRATION_EVENTS"):
            self.assertNotIn(token, upper)


class TestAdmissionFaults(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.initialize()

    def _identity(self):
        return (self.__class__.__module__ + "." +
                self.__class__.__name__ + "." + self._testMethodName)

    def _rolled_back(self, point, **effects):
        with matrix_case(point, self._identity()):
            with self.assertRaises(InjectedFault):
                self.submit("matrix-" + point, clock=lambda: WALL,
                            fault=point, **effects)
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM relays").fetchone()[0], 0)

    def test_after_relay_insert(self):
        self._rolled_back("after-relay-insert")

    def test_after_cycle_events(self):
        self._rolled_back("after-cycle-events", cycle_events=[{
            "dispatch_id": "v29-engine-plan-1", "event": "open",
            "commissioning_seq": None, "cause_seq": None}])

    def test_after_supersession_edges(self):
        self._rolled_back("after-supersession-edges",
                          supersession_edges=[])

    def test_pre_commit(self):
        self._rolled_back("pre-commit")

    def test_post_commit_pre_response(self):
        point = "post-commit-pre-response"
        with matrix_case(point, self._identity()):
            with self.assertRaises(InjectedFault) as fault:
                self.submit("matrix-" + point, clock=lambda: WALL,
                            fault=point)
            self.assertTrue(fault.exception.committed)
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM relays").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
