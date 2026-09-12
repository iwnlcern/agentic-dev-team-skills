"""Pins the INDEX.md guarantees B2's watcher relies on (DD-v295-b1 r3).

Preconditions asserted by every method: active epoch, ten-column arity,
successful render, ordinary (non-top) registration, sole engine writer.
Nothing here is a claim about hand-authored roots.
"""

import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from relay_engine import daemon, seats, supersede
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.ledger import admit, epoch_state, init_schema
from relay_engine.paths import Root, TempWrite, ensure_engine_dir
from relay_engine.render import _cell, index_rows, render_index, render_relay


RUN = "v29"
HEADER = (b"| time | phase | role | dispatch | parent | from | to | cc "
          b"| status | file |\n"
          b"|---|---|---|---|---|---|---|---|---|---|\n")
PREAMBLE = "# INDEX — v29\n\n".encode("utf-8")


def draft(dispatch, sender, to, *, parent=None, cc=None, supersedes=None,
          subject="fixture"):
    lines = ["## relay", "", "ROLE: Planner", "PHASE: PLAN",
             "AUTHORITY: plan-only", "DISPATCH_ID: %s" % dispatch]
    if parent is not None:
        lines.append("PARENT_DISPATCH_ID: %s" % parent)
    lines += ["RUN_ID: %s" % RUN, "CEREMONY_TIER: large",
              "EVIDENCE_TARGET: E2", "HUMAN_GATE_REQUIRED: no",
              "FROM: %s" % sender, "TO: %s" % to]
    if cc is not None:
        lines.append("CC: %s" % cc)
    if supersedes is not None:
        lines.append("SUPERSEDES: %s" % supersedes)
    lines += ["SUBJECT: %s" % subject, "", "body", ""]
    return "\n".join(lines)


def split_row(line):
    """Decode one rendered table row into its cells.

    The renderer escapes a backslash as two backslashes and a pipe as a
    backslash-pipe, so the reader consumes escapes before looking for the
    ` | ` separator. This is the reference decoder for DD-v295-b1
    guarantee 5.
    """
    if not (line.startswith("| ") and line.endswith(" |")):
        raise ValueError("not a table row: %r" % line)
    inner = line[2:-2]
    cells, current, i = [], [], 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner) and inner[i + 1] in "\\|":
            current.append(inner[i + 1])
            i += 2
            continue
        if inner.startswith(" | ", i):
            cells.append("".join(current))
            current = []
            i += 3
            continue
        current.append(ch)
        i += 1
    cells.append("".join(current))
    return cells


class IndexContractCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)
        self.engine_fd = ensure_engine_dir(self.root.dirfd)
        self.ledger = init_schema(self.engine_fd, self.root.path)
        self.ledger.execute(
            "INSERT INTO meta(key,value) VALUES('run_id',?)", (RUN,))
        self.clock = 1786219200.0
        # Precondition: active epoch (fresh schema on a root with no
        # legacy INDEX records the cutover event).
        self.assertEqual(epoch_state(self.ledger), "active")
        # Precondition: arity ten (no stored index_arity).
        self.assertIsNone(self.ledger.execute(
            "SELECT value FROM meta WHERE key='index_arity'").fetchone())

    def tearDown(self):
        self.ledger.close()
        os.close(self.engine_fd)
        self.root.close()
        self.temp.cleanup()

    # --- projection readers -------------------------------------------

    def index_path(self):
        return Path(self.temp.name, "INDEX.md")

    def index_bytes(self):
        return self.index_path().read_bytes()

    def index_digest(self):
        return hashlib.sha256(self.index_bytes()).hexdigest()

    def rows(self):
        text = self.index_bytes().decode("utf-8")
        table = [line for line in text.split("\n") if line.startswith("| ")]
        return [split_row(line) for line in table[1:]]

    def rendered_index_events(self):
        return self.ledger.execute(
            "SELECT COUNT(*) FROM projection_events "
            "WHERE target='index' AND event='rendered'").fetchone()[0]

    # --- direct fixture (renders from the test; NOT the production path)

    def render(self):
        return render_index(self.root, index_rows(self.ledger), 10,
                            epoch_state(self.ledger))

    def admit_relay(self, text, submission_id):
        envelope = parse_draft(text)
        body = text.encode("utf-8")
        edges, _advisories = supersede.prepare(self.ledger, envelope)
        self.clock += 1.0
        admission = admit(
            self.ledger, self.root, envelope, body, submission_id,
            claimed_body_sha256=body_sha256(body),
            claimed_content_hash=content_hash(envelope, body, None),
            supersession_edges=edges, clock=lambda: self.clock)
        if not admission.replay:
            render_relay(self.ledger, self.root, admission.seq)
        return admission, self.render()

    # --- seats and the production submit path -------------------------

    def register(self, address, role="Planner"):
        return seats.register(self.ledger, self.root, address, role)

    def tag_for(self, address):
        current = self.ledger.execute(
            "SELECT occupant_id FROM seat_events WHERE address=? "
            "AND event='occupied' ORDER BY seq DESC LIMIT 1",
            (address,)).fetchone()
        key = Path(self.temp.name, ".engine", "seats", address,
                   current[0] + ".key")
        return key.read_text(encoding="ascii").strip()

    def submit(self, text, submission_id):
        """Production admission: daemon._submit_handler renders the relay
        AND the index itself (daemon.py:815-816). The test never calls
        render() on this path."""
        envelope = parse_draft(text)
        body = text.encode("utf-8")
        args = {
            "envelope": envelope.headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "body_sha256": body_sha256(body),
            "content_hash": content_hash(envelope, body, None),
            "submission_id": submission_id,
            "tag": self.tag_for(envelope.from_seat),
        }
        return daemon._submit_handler(self.ledger, self.root, args)


class TestHeaderAndGrammar(IndexContractCase):
    def test_preamble_and_ten_column_header_are_byte_exact(self):
        self.register("v29-a.planner")
        self.admit_relay(draft("v29-work-1", "v29-a.planner",
                               "v29-a.implementer"), "w1")
        data = self.index_bytes()
        self.assertTrue(data.startswith(PREAMBLE + HEADER),
                        data[:len(PREAMBLE) + len(HEADER)])
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(len(row) == 10 for row in self.rows()))

    def test_cell_grammar_unit_oracles(self):
        self.assertEqual(_cell(None), "—")
        self.assertEqual(_cell("a\\b"), "a\\\\b")
        self.assertEqual(_cell("a|b"), "a\\|b")
        self.assertEqual(_cell("a\nb"), "a b")
        self.assertEqual(split_row("| a\\\\b | c\\|d | — |"),
                         ["a\\b", "c|d", "—"])

    def test_absent_parent_and_cc_render_as_em_dash(self):
        self.register("v29-a.planner")
        self.admit_relay(draft("v29-work-1", "v29-a.planner",
                               "v29-a.implementer"), "w1")
        row = self.rows()[-1]
        self.assertEqual(row[1], "PLAN")
        self.assertEqual(row[4], "—")   # parent
        self.assertEqual(row[7], "—")   # cc
        self.assertEqual(row[8], "—")   # status


if __name__ == "__main__":
    unittest.main()
