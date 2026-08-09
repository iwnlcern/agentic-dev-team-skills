import os
from pathlib import Path
import tempfile
import unittest

from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, init_schema
from relay_engine.paths import Root, TempWrite, ensure_engine_dir
from relay_engine.reconcile import startup_recovery
from relay_engine.render import render_relay
from relay_engine.tests.check_crash_matrix import matrix_case


DRAFT = """## relay

ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: crash-plan
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: v29-a.planner
TO: v29-a.implementer
SUBJECT: crash state

body
"""


class TestCrashStates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        self.row = admit(self.ledger, self.root, parse_draft(DRAFT),
                         DRAFT.encode(), "crash-sid")

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    def _case(self, point):
        return matrix_case(point, "%s.%s" % (
            self.__class__.__module__ + "." + self.__class__.__name__,
            self._testMethodName))

    def test_pre_file_render(self):
        with self._case("pre-file-render"):
            startup_recovery({"ledger": self.ledger, "root": self.root})
            self.assertEqual(Path(self.temp.name, self.row.rendered_path)
                             .read_bytes(), DRAFT.encode())

    def test_mid_file_write(self):
        with self._case("mid-file-write"):
            lane = Path(self.temp.name, self.row.rendered_path).parent
            lane.mkdir()
            Path(lane, ".relay-tmp-interrupted").write_bytes(b"partial")
            startup_recovery({"ledger": self.ledger, "root": self.root})
            self.assertFalse(Path(lane, ".relay-tmp-interrupted").exists())

    def test_pre_index_render(self):
        with self._case("pre-index-render"):
            render_relay(self.ledger, self.root, self.row.seq)
            startup_recovery({"ledger": self.ledger, "root": self.root})
            self.assertTrue(Path(self.temp.name, "INDEX.md").is_file())

    def test_mid_index_write(self):
        with self._case("mid-index-write"):
            with TempWrite(self.root, "INDEX.md") as write:
                write.write(b"partial")
            startup_recovery({"ledger": self.ledger, "root": self.root})
            self.assertNotEqual(Path(self.temp.name, "INDEX.md").read_bytes(),
                                b"partial")

    def test_mid_commit(self):
        with self._case("mid-commit"):
            self.ledger.execute("BEGIN IMMEDIATE")
            self.ledger.execute(
                "INSERT INTO projection_events(seq,relay_seq,target,event) "
                "VALUES(1,?,'relay','failed')", (self.row.seq,))
            self.ledger.execute("ROLLBACK")
            self.assertEqual(self.ledger.execute(
                "SELECT COUNT(*) FROM projection_events").fetchone()[0], 0)

    def test_render_fail_loop(self):
        with self._case("render-fail-loop"):
            path = Path(self.temp.name, self.row.rendered_path)
            path.parent.mkdir()
            path.write_bytes(b"foreign")
            first = render_relay(self.ledger, self.root, self.row.seq)
            second = render_relay(self.ledger, self.root, self.row.seq)
            self.assertEqual((first.event, second.event),
                             ("conflict", "conflict"))
            self.assertEqual(path.read_bytes(), b"foreign")


if __name__ == "__main__":
    unittest.main()
