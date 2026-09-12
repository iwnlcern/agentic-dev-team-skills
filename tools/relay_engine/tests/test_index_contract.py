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

class TestRegistrationOrderAndSupersession(IndexContractCase):
    def test_ordinary_registration_appends_one_boot_row_top_adds_none(self):
        first = self.register("v29-a.planner")
        rows_after_first = self.rows()
        self.assertEqual(len(rows_after_first), 1)
        self.assertEqual(rows_after_first[0][1], "BOOT")
        self.assertEqual(rows_after_first[0][9], first["boot_relay"])
        second = self.register("v29-a.implementer", "Implementer")
        rows_after_second = self.rows()
        self.assertEqual(len(rows_after_second), 2)
        self.assertEqual(rows_after_second[0], rows_after_first[0])
        self.assertEqual(rows_after_second[1][9], second["boot_relay"])
        digest_before_top = self.index_digest()
        top = seats.register(self.ledger, self.root,
                             "v29.orchestrator-planner",
                             "Orchestrator Planner", top=True)
        self.assertIsNone(top["boot_relay"])
        self.assertEqual(self.rows(), rows_after_second)
        self.assertEqual(self.index_digest(), digest_before_top)

    def test_rows_are_in_filing_order_with_unique_file_cells(self):
        self.register("v29-a.planner")
        self.register("v29-a.implementer", "Implementer")
        boot_rows = self.rows()
        self.assertEqual(len(boot_rows), 2)
        w1, _ = self.admit_relay(draft("v29-work-1", "v29-a.planner",
                                       "v29-a.implementer"), "w1")
        w2, _ = self.admit_relay(draft("v29-work-2", "v29-a.implementer",
                                       "v29-a.planner"), "w2")
        w3, _ = self.admit_relay(draft("v29-work-3", "v29-a.planner",
                                       "v29-a.implementer"), "w3")
        rows = self.rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[:2], boot_rows)
        self.assertEqual([row[9] for row in rows[2:]],
                         [w1.rendered_path, w2.rendered_path,
                          w3.rendered_path])
        self.assertEqual(len({row[9] for row in rows}), 5)
        self.assertEqual([row[0] for row in rows],
                         sorted(row[0] for row in rows))

    def test_effective_supersession_appends_and_changes_only_target_status(self):
        self.register("v29-a.planner")
        self.register("v29-a.implementer", "Implementer")
        w1, _ = self.admit_relay(draft("v29-work-1", "v29-a.planner",
                                       "v29-a.implementer"), "w1")
        self.admit_relay(draft("v29-work-2", "v29-a.implementer",
                               "v29-a.planner"), "w2")
        self.admit_relay(draft("v29-work-3", "v29-a.planner",
                               "v29-a.implementer"), "w3")
        before = self.rows()
        self.assertEqual(len(before), 5)
        self.assertEqual(before[2][9], w1.rendered_path)
        self.assertEqual(before[2][8], "—")
        ruling, _ = self.admit_relay(
            draft("v29-rule-1", "operator", "v29-a.planner",
                  supersedes=w1.rendered_path, subject="ruling"), "r1")
        after = self.rows()
        self.assertEqual(len(after), 6)
        self.assertEqual(after[5][9], ruling.rendered_path)
        for index, (old, new) in enumerate(zip(before, after[:5])):
            if index == 2:
                self.assertEqual(new[8], "superseded")
                self.assertEqual(old[:8] + ["—"] + old[9:],
                                 new[:8] + ["—"] + new[9:])
            else:
                self.assertEqual(old, new)
        self.assertEqual(self.ledger.execute(
            "SELECT target_seq,applied FROM supersession_edges").fetchall(),
            [(w1.seq, 1)])

    def test_pair_seat_supersession_is_ineffective_and_changes_no_existing_cell(self):
        self.register("v29-a.planner")
        self.register("v29-a.implementer", "Implementer")
        self.admit_relay(draft("v29-work-1", "v29-a.planner",
                               "v29-a.implementer"), "w1")
        w2, _ = self.admit_relay(draft("v29-work-2", "v29-a.implementer",
                                       "v29-a.planner"), "w2")
        before = self.rows()
        # Reviewer's note: the control targets an UNSUPERSEDED relay so an
        # accidentally effective edge cannot hide behind an existing
        # superseded status.
        self.assertEqual(before[3][9], w2.rendered_path)
        self.assertEqual(before[3][8], "—")
        control, _ = self.admit_relay(
            draft("v29-rule-2", "v29-a.implementer", "v29-a.planner",
                  supersedes=w2.rendered_path, subject="not a ruling"),
            "r2")
        after = self.rows()
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1][9], control.rendered_path)
        self.assertEqual(self.ledger.execute(
            "SELECT COUNT(*) FROM supersession_edges").fetchone()[0], 0)
