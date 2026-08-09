import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.ledger import admit, init_schema
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.render import (atomic_create, atomic_replace, render_index,
                                 render_relay, render_seats, sweep_tmp)


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
SUBJECT: render fixture

relay body
"""
BODY = DRAFT.encode("utf-8")


class TestRender(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        os.mkdir(Path(self.temp.name, "v29-engine"))
        self.envelope = parse_draft(DRAFT)
        self.row = admit(
            self.ledger, self.root, self.envelope, BODY, "render-1",
            claimed_body_sha256=body_sha256(BODY),
            claimed_content_hash=content_hash(self.envelope, BODY, None),
            clock=lambda: 1786219200.0)

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def path(self, rel):
        return Path(self.temp.name, rel)

    def events(self):
        return self.ledger.execute(
            "SELECT relay_seq,target,event,digest,detail "
            "FROM projection_events ORDER BY seq").fetchall()

    def test_atomic_create_is_no_clobber_and_digest_is_file_bytes(self):
        digest = atomic_create(self.root, self.row.rendered_path, BODY)
        self.assertEqual(digest, hashlib.sha256(BODY).hexdigest())
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), BODY)
        with self.assertRaises(FileExistsError):
            atomic_create(self.root, self.row.rendered_path, b"replacement")
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), BODY)

    def test_render_relay_writes_exact_body_and_event(self):
        result = render_relay(self.ledger, self.root, self.row.seq)
        digest = hashlib.sha256(BODY).hexdigest()
        self.assertEqual(result.event, "rendered")
        self.assertEqual(result.digest, digest)
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), BODY)
        self.assertEqual(self.events(), [
            (self.row.seq, "relay", "rendered", digest,
             self.row.rendered_path)
        ])

    def test_equal_squat_is_recovery_without_conflict(self):
        self.path(self.row.rendered_path).write_bytes(BODY)
        result = render_relay(self.ledger, self.root, self.row.seq)
        self.assertEqual(result.event, "rendered")
        self.assertFalse(any(event[2] == "conflict" for event in self.events()))
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), BODY)

    def test_different_squat_is_exact_conflict_and_untouched(self):
        occupant = b"foreign occupant\n"
        self.path(self.row.rendered_path).write_bytes(occupant)
        result = render_relay(self.ledger, self.root, self.row.seq)
        digest = hashlib.sha256(occupant).hexdigest()
        self.assertEqual(result.event, "conflict")
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), occupant)
        self.assertEqual(self.events(), [
            (self.row.seq, "relay", "conflict", digest,
             self.row.rendered_path)
        ])

    def test_modified_relay_is_archived_recorded_and_never_replaced(self):
        render_relay(self.ledger, self.root, self.row.seq)
        edited = b"hand-edited relay\n"
        self.path(self.row.rendered_path).write_bytes(edited)
        result = render_relay(self.ledger, self.root, self.row.seq)
        digest = hashlib.sha256(edited).hexdigest()
        self.assertEqual(result.event, "modified")
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), edited)
        self.assertIsNotNone(result.archive_path)
        self.assertEqual(self.path(result.archive_path).read_bytes(), edited)
        self.assertEqual(self.events()[-1],
            (self.row.seq, "relay", "modified", digest,
             self.row.rendered_path))
        with self.assertRaises(ValueError):
            atomic_replace(self.root, self.row.rendered_path, BODY, digest)
        self.assertEqual(self.path(self.row.rendered_path).read_bytes(), edited)

    def test_projection_replace_repeat_edit_archive_and_identity(self):
        first_rows = [("t1", "PLAN", "Planner", "d1", "-", "from", "to",
                       "-", "active", "lane/one.md")]
        second_rows = [("t2", "IMPL", "Implementer", "d2", "d1", "from",
                        "to", "cc", "done", "lane/two.md")]
        first = render_index(self.root, first_rows, 10, "active",
                             ledger=self.ledger)
        first_bytes = self.path("INDEX.md").read_bytes()
        self.assertEqual(first.digest,
                         hashlib.sha256(first_bytes).hexdigest())
        second = render_index(self.root, second_rows, 10, "active",
                              ledger=self.ledger)
        self.assertNotEqual(first.digest, second.digest)
        self.assertIn(b"lane/two.md", self.path("INDEX.md").read_bytes())

        edited = b"hand edit to index\n"
        self.path("INDEX.md").write_bytes(edited)
        third = render_index(self.root, second_rows, 10, "active",
                             ledger=self.ledger)
        edited_digest = hashlib.sha256(edited).hexdigest()
        self.assertEqual(self.path(third.archive_path).read_bytes(), edited)
        self.assertEqual(self.ledger.execute(
            "SELECT relay_seq,target,event,digest,detail "
            "FROM projection_events WHERE target='index' ORDER BY seq"
        ).fetchall()[-2:], [
            (None, "index", "divergence", edited_digest, "INDEX.md"),
            (None, "index", "rendered", third.digest, "INDEX.md"),
        ])

        seats_a = [("seat.a", "planner", "online", "session-a", "lane")]
        seats_b = [("seat.a", "planner", "offline", "session-a", "lane")]
        render_seats(self.root, seats_a, "active", ledger=self.ledger)
        rendered = render_seats(self.root, seats_b, "active",
                                ledger=self.ledger)
        self.assertIn(b"offline", self.path("SEATS.md").read_bytes())
        self.assertEqual(self.ledger.execute(
            "SELECT relay_seq,target,event,digest,detail "
            "FROM projection_events WHERE target='seats' ORDER BY seq DESC "
            "LIMIT 1").fetchone(),
            (None, "seats", "rendered", rendered.digest, "SEATS.md"))

    def test_inert_epoch_leaves_projection_bytes_and_events_untouched(self):
        original_index = b"legacy index\n"
        original_seats = b"legacy seats\n"
        self.path("INDEX.md").write_bytes(original_index)
        self.path("SEATS.md").write_bytes(original_seats)
        before = self.events()
        self.assertIsNone(render_index(
            self.root, [("x",) * 10], 10, "inert", ledger=self.ledger))
        self.assertIsNone(render_seats(
            self.root, [("x",) * 5], "inert", ledger=self.ledger))
        self.assertEqual(self.path("INDEX.md").read_bytes(), original_index)
        self.assertEqual(self.path("SEATS.md").read_bytes(), original_seats)
        self.assertEqual(self.events(), before)

    def test_projection_event_identity_classes_are_disjoint(self):
        render_relay(self.ledger, self.root, self.row.seq)
        render_index(self.root, [("x",) * 10], 10, "active",
                     ledger=self.ledger)
        render_seats(self.root, [("x",) * 5], "active",
                     ledger=self.ledger)
        self.ledger.execute(
            "INSERT INTO projection_events"
            "(relay_seq,target,event,digest,detail) "
            "VALUES(NULL,'foreign-path','divergence',?,?)",
            ("f" * 64, "lane/foreign.md"))
        identities = self.ledger.execute(
            "SELECT target,relay_seq FROM projection_events "
            "GROUP BY target,relay_seq ORDER BY target").fetchall()
        self.assertEqual(identities, [
            ("foreign-path", None), ("index", None),
            ("relay", self.row.seq), ("seats", None),
        ])

    def test_sweep_removes_only_regular_owned_temps(self):
        stale_root = self.path(".relay-tmp-stale-root")
        stale_nested = self.path("v29-engine/.relay-tmp-stale-nested")
        keep = self.path("v29-engine/not-a-temp")
        stale_root.write_bytes(b"temp")
        stale_nested.write_bytes(b"temp")
        keep.write_bytes(b"keep")
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            outside_temp = outside / ".relay-tmp-outside"
            outside_temp.write_bytes(b"outside")
            self.path("linked").symlink_to(outside, target_is_directory=True)
            self.assertEqual(sweep_tmp(self.root), 2)
            self.assertFalse(stale_root.exists())
            self.assertFalse(stale_nested.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(outside_temp.exists())


if __name__ == "__main__":
    unittest.main()
