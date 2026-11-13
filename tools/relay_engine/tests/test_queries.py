"""v3 queries and metrics (DD-v3-engine § 7, § 8, § 9, § 11, § 15 item 6; plan Task 5)."""

import difflib
import hashlib
import inspect
import json
from dataclasses import FrozenInstanceError, asdict
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from relay_engine import queries
from relay_engine import addresses, errors
from relay_engine import refs
from relay_engine import schema
from relay_engine import submit
from relay_engine.tests import test_submit as submit_tests
from relay_engine.tests.test_refs import Repo, git
from unittest import mock
from relay_engine.tests.test_submit import (FOREIGN, IMPL, ORCH, OTHER, PLANNER, SEATS, Store, GitStore,
                                            draft, vm_steps)

OPERATOR = SEATS["operator"]

ENVELOPE = {"handle", "kind", "sender", "to", "cc", "subject", "parent", "verdict", "needs",
            "created_at"}


def retain_query_evidence(name, states):
    """Retain these focused controls' actual row snapshots when a receipt root is supplied."""
    root = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
    if root:
        with open(os.path.join(root, name + ".json"), "w") as stream:
            json.dump(states, stream, indent=2)
            stream.write("\n")


class TestReads(Store):
    def actor_state(self):
        return {"activity": self.conn.execute(
                    "SELECT * FROM seat_activity ORDER BY seat_id").fetchall(),
                "recipients": self.conn.execute(
                    "SELECT * FROM recipients ORDER BY rowid").fetchall()}

    def test_default_read_envelope(self):
        root = self.submit(draft(to="v3-engine.implementer"))
        child = self.submit(draft(to="v3-engine.implementer, v3-other.planner",
                                  cc="v3.orchestrator-planner", parent="#1", needs="act",
                                  subject="please act", body="the body\n"))
        result = queries.read(self.conn, child.relay_id, IMPL.seat_id, 5000)
        self.assertEqual(result.body, b"the body\n")
        self.assertEqual(set(result.envelope), ENVELOPE)
        self.assertEqual(result.envelope, {
            "handle": 2, "kind": "SITREP", "sender": PLANNER.seat_id,
            "to": [IMPL.seat_id, OTHER.seat_id], "cc": [ORCH.seat_id],
            "subject": "please act", "parent": 1, "verdict": None, "needs": "act",
            "created_at": child.created_at_ms})
        self.assertIsNone(result.meta)
        opened = lambda: self.conn.execute(
            "SELECT opened_at FROM recipients WHERE relay_id = ? AND seat_id = ?",
            (child.relay_id, IMPL.seat_id)).fetchone()[0]
        self.assertEqual(opened(), 5000)
        queries.read(self.conn, child.relay_id, IMPL.seat_id, 9000)
        self.assertEqual(opened(), 5000)  # set once
        self.assertIsNone(queries.read(self.conn, root.relay_id, IMPL.seat_id, 1).envelope["parent"])

    def test_meta_read_adds(self):
        self.submit(draft(to="v3-engine.implementer"))
        admitted = self.submit(draft(parent="#1", to="v3-engine.implementer", refs=("#1",)))
        result = queries.read(self.conn, admitted.relay_id, IMPL.seat_id, 7000, meta=True)
        self.assertEqual(set(result.envelope), ENVELOPE)
        self.assertEqual(set(result.meta), {"digests", "edges", "read_state", "git_state"})
        self.assertEqual(set(result.meta["digests"]), {"body_sha256", "meta_sha256"})
        self.assertEqual([(e.kind, e.spelling) for e in result.meta["edges"]],
                         [("parent", "#1"), ("ref", "#1")])
        self.assertEqual(result.meta["read_state"], [
            {"seat": IMPL.seat_id, "role": "to", "delivered_at": None, "opened_at": 7000,
             "answered_by": None}])
        self.assertEqual(result.meta["git_state"]["head_commit"], "a" * 40)

    def test_read_by_a_stranger_sets_nothing(self):
        admitted = self.submit(draft(to="v3-engine.implementer"))
        queries.read(self.conn, admitted.relay_id, OTHER.seat_id, 5)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM recipients WHERE opened_at"
                                           " IS NOT NULL").fetchone()[0], 0)

    def test_read_refuses_unrelated_project(self):
        """PR3 R7, carried to Task6: a foreign non-recipient gets no relay or side effects."""
        admitted = self.submit(draft(to="v3-engine.implementer", body="local-only body\n"))
        before_activity = self.conn.execute("SELECT * FROM seat_activity ORDER BY seat_id").fetchall()
        before_recipients = self.conn.execute("SELECT * FROM recipients ORDER BY rowid").fetchall()
        for meta in (False, True):
            with self.subTest(meta=meta):
                with self.assertRaises(ValueError):
                    queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 77, meta=meta)
                self.assertEqual(self.conn.execute(
                    "SELECT * FROM seat_activity ORDER BY seat_id").fetchall(), before_activity)
                self.assertEqual(self.conn.execute(
                    "SELECT * FROM recipients ORDER BY rowid").fetchall(), before_recipients)

    def test_foreign_release_callback_is_read_only(self):
        """Blanket denial and caught write/transaction-control bypasses both fail."""
        admitted = self.submit(draft(to=FOREIGN.address, body="released body\n"))
        self.assertIn("foreign_release", inspect.signature(queries.read).parameters,
                      "mixed read must accept the current-release predicate")
        # Prepare transaction statements before the callback's permission frame.
        for statement in ("COMMIT", "ROLLBACK"):
            self.conn.execute("BEGIN")
            self.conn.execute(statement)

        snapshots = []
        for statement in ("UPDATE recipients SET opened_at = 999", "COMMIT", "ROLLBACK",
                          "DELETE FROM recipients", "SAVEPOINT blocked", "RELEASE blocked",
                          "PRAGMA user_version", "ATTACH DATABASE ':memory:' AS blocked",
                          "CREATE TABLE blocked(id)"):
            with self.subTest(denied=statement):
                activity = self.conn.execute("SELECT * FROM seat_activity ORDER BY seat_id"
                                             ).fetchall()
                recipients = self.conn.execute("SELECT * FROM recipients ORDER BY rowid"
                                               ).fetchall()
                denied = []

                def caught_denial(conn, target):
                    try:
                        conn.execute(statement)
                    except sqlite3.DatabaseError:
                        denied.append(statement)
                    return True  # Catching the error must not authorize the read.

                with self.assertRaises(ValueError):
                    queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 77,
                                 meta=True, foreign_release=caught_denial)
                self.assertEqual(denied, [statement])
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(self.conn.execute("SELECT * FROM seat_activity ORDER BY seat_id"
                                                   ).fetchall(), activity)
                self.assertEqual(self.conn.execute("SELECT * FROM recipients ORDER BY rowid"
                                                   ).fetchall(), recipients)
                snapshots.append({"statement": statement, "denied": denied,
                                  "activity_before": activity,
                                  "activity_after": self.conn.execute(
                                      "SELECT * FROM seat_activity ORDER BY seat_id").fetchall(),
                                  "recipients_before": recipients,
                                  "recipients_after": self.conn.execute(
                                      "SELECT * FROM recipients ORDER BY rowid").fetchall(),
                                  "in_transaction_after": self.conn.in_transaction})
                # An actual write proves authorizer cleanup, not merely SELECT permission.
                queries.note_activity(self.conn, IMPL.seat_id, 99)
                self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity"
                                                   " WHERE seat_id = ?", (IMPL.seat_id,)
                                                   ).fetchone(), (99,))

        observed = []

        def released(conn, target):
            observed.append((target.relay_id, target.body_sha256, target.source_project_id,
                             target.destination_project_id, target.recipient_seat_id,
                             target.recipient_role, conn.in_transaction))
            row = conn.execute("SELECT project_id FROM relays WHERE id = ?",
                               (target.relay_id,)).fetchone()
            return row == ("p",)

        result = queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 100,
                              foreign_release=released)
        self.assertEqual(result.body, b"released body\n")
        self.assertEqual(observed, [(admitted.relay_id,
                                    hashlib.sha256(b"released body\n").hexdigest(),
                                    "p", "q", FOREIGN.seat_id, "to", True)])
        self.assertEqual(self.conn.execute("SELECT delivered_at, opened_at, answered_by"
                                           " FROM recipients WHERE relay_id = ?",
                                           (admitted.relay_id,)).fetchone(), (None, 100, None))
        self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity"
                                           " WHERE seat_id = ?", (FOREIGN.seat_id,)
                                           ).fetchone(), (100,))
        self.assertFalse(self.conn.in_transaction)
        retain_query_evidence("callback-read-only", {
            "refusals": snapshots, "target": observed,
            "released_recipients": self.conn.execute(
                "SELECT * FROM recipients ORDER BY rowid").fetchall(),
            "released_activity": self.conn.execute(
                "SELECT * FROM seat_activity ORDER BY seat_id").fetchall()})

    def test_foreign_recipient_release_bound_and_atomic(self):
        """A recipient/history is not a grant; exact current TO/CC release is usable."""
        snapshots = []
        for role in ("to", "cc"):
            admitted = self.submit(draft(
                to=FOREIGN.address if role == "to" else IMPL.address,
                cc=FOREIGN.address if role == "cc" else None, body="bound body\n"))
            expected = {"relay_id": admitted.relay_id,
                        "body_sha256": hashlib.sha256(b"bound body\n").hexdigest(),
                        "source_project_id": "p", "destination_project_id": "q",
                        "recipient_seat_id": FOREIGN.seat_id, "recipient_role": role}
            cases = [(status, {}, False) for status in
                     ("held", "denied", "revoked", "unknown", "unavailable", "ineligible")]
            cases += [("wrong-" + field, {field: value}, True) for field, value in (
                ("relay_id", "another-relay"), ("body_sha256", "0" * 64),
                ("destination_project_id", "p"), ("source_project_id", "q"),
                ("recipient_seat_id", IMPL.seat_id),
                ("recipient_role", "cc" if role == "to" else "to"))]
            cases.append(("wrong-direction", {"source_project_id": "q",
                                              "destination_project_id": "p"}, True))
            for meta in (False, True):
                for status, mismatch, allowed in cases:
                    with self.subTest(role=role, meta=meta, release=status):
                        before = self.actor_state()
                        seen = []
                        targets = []

                        def release(conn, target):
                            self.assertIs(conn, self.conn)
                            self.assertTrue(conn.in_transaction)
                            seen.append(asdict(target))
                            targets.append(target)
                            # Controlled owner seam only, not a release-storage implementation.
                            return allowed and asdict(target) == dict(expected, **mismatch)

                        with self.assertRaises(ValueError):
                            queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 70,
                                         meta=meta, foreign_release=release)
                        self.assertEqual(seen, [expected])
                        self.assertEqual(len(targets), 1)
                        # Keep this assertion outside callback Exception -> ValueError translation.
                        with self.assertRaises(FrozenInstanceError):
                            targets[0].body_sha256 = "0" * 64
                        self.assertEqual(self.actor_state(), before)
                        self.assertFalse(self.conn.in_transaction)
                        snapshots.append({"role": role, "meta": meta, "case": status,
                                          "before": before, "after": self.actor_state()})
                before_absent = self.actor_state()
                with self.assertRaises(ValueError):
                    queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 71, meta=meta)
                self.assertEqual(self.actor_state(), before_absent)
                self.assertFalse(self.conn.in_transaction)

            # Fail after the activity write: all helper effects must roll back together.
            self.conn.execute("CREATE TEMP TRIGGER reject_open BEFORE UPDATE OF opened_at"
                              " ON recipients WHEN NEW.opened_at = 72"
                              " BEGIN SELECT RAISE(ABORT, 'fixture open fault'); END")
            before = self.actor_state()
            with self.assertRaises(sqlite3.IntegrityError):
                queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 72,
                             foreign_release=lambda conn, target: True)
            self.assertEqual(self.actor_state(), before)
            self.assertFalse(self.conn.in_transaction)
            self.conn.execute("DROP TRIGGER reject_open")
            result = queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 80, meta=True,
                                  foreign_release=lambda conn, target: asdict(target) == expected)
            self.assertEqual(result.body, b"bound body\n")
            self.assertEqual(self.conn.execute("SELECT delivered_at, opened_at, answered_by"
                                               " FROM recipients WHERE relay_id = ?"
                                               " AND seat_id = ?",
                                               (admitted.relay_id, FOREIGN.seat_id)).fetchone(),
                             (None, 80, None))
            # Even prior delivery/opening cannot stand in for a current release.
            self.conn.execute("UPDATE recipients SET delivered_at = 81 WHERE relay_id = ?"
                              " AND seat_id = ?", (admitted.relay_id, FOREIGN.seat_id))
            self.conn.commit()
            before = self.actor_state()
            with self.assertRaises(ValueError):
                queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 90,
                             foreign_release=lambda conn, target: False)
            self.assertEqual(self.actor_state(), before)

        authored = self.submit(draft(to=IMPL.address, body="foreign author only\n"))
        # A stored cross-project author alone does not confer recipient eligibility.
        self.conn.execute("UPDATE relays SET sender_seat_id = ?, team_instance_id = ?"
                          " WHERE id = ?", (FOREIGN.seat_id, FOREIGN.team_instance_id,
                                             authored.relay_id))
        self.conn.commit()
        before = self.actor_state()
        calls = []
        for meta in (False, True):
            with self.assertRaises(ValueError):
                queries.read(self.conn, authored.relay_id, FOREIGN.seat_id, 95, meta=meta,
                             foreign_release=lambda conn, target: calls.append(target) or True)
        self.assertEqual(calls, [])
        self.assertEqual(self.actor_state(), before)
        retain_query_evidence("foreign-release-matrix", snapshots)

    def test_dirty_caller_transaction_is_preserved(self):
        """Refusal must neither commit nor roll back caller work nor invoke release."""
        admitted = self.submit(draft(to=FOREIGN.address))
        self.conn.execute("UPDATE seat_occupants SET issued_at = 321 WHERE seat_id = ?",
                          (FOREIGN.seat_id,))
        before = self.actor_state()
        calls = []
        for meta in (False, True):
            with self.assertRaises(ValueError):
                queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 10, meta=meta,
                             foreign_release=lambda conn, target: calls.append(target) or True)
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants"
                                               " WHERE seat_id = ?", (FOREIGN.seat_id,)
                                               ).fetchone(), (321,))
            self.assertEqual(self.actor_state(), before)
        self.assertEqual(calls, [])
        self.conn.rollback()  # The caller alone disposes of its pending transaction.
        self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants"
                                           " WHERE seat_id = ?", (FOREIGN.seat_id,)
                                           ).fetchone(), (1,))
        self.assertEqual(queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 11,
                                     foreign_release=lambda conn, target: True).body, b"body\n")

    def test_callback_refusal_cleans_connection(self):
        """Strict booleans and callback faults leave no effects or poisoned connection."""
        admitted = self.submit(draft(to=FOREIGN.address))
        interrupt = KeyboardInterrupt("fixture interrupt")
        snapshots = []
        for outcome in (False, None, 0, 1, "true", [], RuntimeError("private detail"), interrupt):
            for meta in (False, True):
                with self.subTest(outcome=repr(outcome), meta=meta):
                    before = self.actor_state()

                    def release(conn, target):
                        conn.execute("SELECT id FROM relays WHERE id = ?", (target.relay_id,))
                        if isinstance(outcome, BaseException):
                            raise outcome
                        return outcome

                    with self.assertRaises(KeyboardInterrupt if outcome is interrupt
                                           else ValueError) as caught:
                        queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 20,
                                     meta=meta, foreign_release=release)
                    if outcome is interrupt:
                        self.assertIs(caught.exception, interrupt)
                    else:
                        self.assertNotIn("private detail", str(caught.exception))
                    self.assertFalse(self.conn.in_transaction)
                    self.assertEqual(self.actor_state(), before)
                    snapshots.append({"outcome": repr(outcome), "meta": meta,
                                      "before": before, "after": self.actor_state()})
                    queries.note_activity(self.conn, IMPL.seat_id, 21)
        self.assertEqual(queries.read(self.conn, admitted.relay_id, FOREIGN.seat_id, 22,
                                     foreign_release=lambda conn, target: True).body, b"body\n")
        retain_query_evidence("callback-cleanup-matrix", snapshots)

    def test_operator_read_has_no_activity(self):
        """Real operator access remains; system and missing seats get no bypass."""
        admitted = self.submit(draft(to="operator", cc=IMPL.address))
        self.assertEqual(queries.read(self.conn, admitted.relay_id, OPERATOR.seat_id, 30,
                                     meta=True).body, b"body\n")
        self.assertEqual(self.conn.execute("SELECT opened_at FROM recipients"
                                           " WHERE relay_id = ? AND special = 'operator'",
                                           (admitted.relay_id,)).fetchone(), (30,))
        self.assertEqual(self.conn.execute("SELECT * FROM seat_activity WHERE seat_id = ?",
                                           (OPERATOR.seat_id,)).fetchall(), [])
        before = self.actor_state()
        for seat in ("system", "missing-seat"):
            with self.assertRaises(ValueError):
                queries.read(self.conn, admitted.relay_id, seat, 31)
            self.assertEqual(self.actor_state(), before)
        with self.assertRaises(KeyError):
            queries.read(self.conn, "missing-relay", IMPL.seat_id, 32)
        self.assertFalse(self.conn.in_transaction)

    def test_stateful_cc_read_only_opens(self):
        """Local CC reads first-open their own row without delivery/answer or TO effects."""
        admitted = self.submit(draft(to=IMPL.address, cc=OTHER.address))
        result = queries.read(self.conn, admitted.relay_id, OTHER.seat_id, 40, meta=True)
        self.assertEqual(result.body, b"body\n")
        self.assertEqual(self.conn.execute("SELECT seat_id, role, delivered_at, opened_at,"
                                           " answered_by FROM recipients WHERE relay_id = ?"
                                           " ORDER BY role", (admitted.relay_id,)).fetchall(),
                         [(OTHER.seat_id, "cc", None, 40, None),
                          (IMPL.seat_id, "to", None, None, None)])
        queries.read(self.conn, admitted.relay_id, OTHER.seat_id, 50)
        self.assertEqual(self.conn.execute("SELECT opened_at FROM recipients WHERE relay_id = ?"
                                           " AND seat_id = ?", (admitted.relay_id, OTHER.seat_id)
                                           ).fetchone(), (40,))

    def test_marks(self):
        admitted = self.submit(draft(to="v3-engine.implementer", cc="v3-other.planner"))
        for seat in (IMPL, OTHER):
            queries.mark_delivered(self.conn, admitted.relay_id, seat.seat_id, 10)
            queries.mark_delivered(self.conn, admitted.relay_id, seat.seat_id, 20)
        rows = dict(self.conn.execute("SELECT seat_id, delivered_at FROM recipients"
                                      " WHERE relay_id = ?", (admitted.relay_id,)).fetchall())
        self.assertEqual(rows, {IMPL.seat_id: 10, OTHER.seat_id: None})  # CC never delivered


class TestLineage(Store):
    def chain(self, length):
        first = self.submit(draft(to="v3-engine.implementer"))
        last = first
        for _ in range(length - 1):
            last = self.submit(draft(parent=last.relay_id, to="v3-engine.implementer"))
        return first, last

    def test_lineage_bounded(self):
        _, last = self.chain(300)
        rows = queries.lineage(self.conn, last.relay_id)
        self.assertEqual(len(rows), 256)
        self.assertEqual(rows[0].id, last.relay_id)
        self.assertEqual([r.handle for r in rows], list(range(300, 44, -1)))
        self.assertEqual(len(queries.lineage(self.conn, last.relay_id, max_depth=3)), 3)

    def test_candidates_exact(self):
        to_me = self.submit(draft(to="v3-engine.implementer"), sender=OTHER)
        cc_me = self.submit(draft(to="v3-other.planner", cc="v3-engine.implementer"))
        mine = self.submit(draft(to="v3-other.planner"), sender=IMPL)
        self.submit(draft(to="v3-other.planner"))  # not addressed to IMPL
        review = self.submit(draft(kind="REVIEW", needs="review", to="v3-engine.implementer",
                                   bundle="b2"))
        self.assertEqual(queries.parent_candidates(self.conn, IMPL.seat_id),
                         [to_me.handle, cc_me.handle, mine.handle, review.handle])
        bundle = self.relay(review.relay_id)["bundle_id"]
        self.assertEqual(queries.parent_candidates(self.conn, IMPL.seat_id, bundle_id=bundle),
                         [review.handle])
        self.assertEqual(queries.parent_candidates(self.conn, IMPL.seat_id, kind="REVIEW"),
                         [review.handle])
        # every candidate admits as PARENT; nothing else does
        for handle in range(1, 6):
            data = draft(parent="#%d" % handle, to="v3-other.planner")
            if handle in queries.parent_candidates(self.conn, IMPL.seat_id):
                self.submit(data, sender=IMPL)
            else:
                self.refused("E-PARENT-NOT-MEMBER", data, sender=IMPL)

    def test_candidates_by_project(self):  # PR-3 fold R4 (planner ruling)
        q_impl = FOREIGN  # project q's seat
        self.submit(draft(to="v3-engine.implementer"))
        mine = self.submit(draft(kind="QUESTION", to="operator", needs="answer"), sender=IMPL)
        theirs = self.submit(draft(kind="QUESTION", to="operator", needs="answer"), sender=q_impl)
        self.assertEqual((mine.handle, theirs.handle), (2, 1))
        with self.assertRaises(ValueError):  # the operator's queue spans projects
            queries.parent_candidates(self.conn, OPERATOR.seat_id)
        self.assertEqual(queries.parent_candidates(self.conn, OPERATOR.seat_id, project_id="p"),
                         [mine.handle])
        self.assertEqual(queries.parent_candidates(self.conn, OPERATOR.seat_id, project_id="q"),
                         [theirs.handle])
        self.assertEqual(queries.parent_candidates(self.conn, IMPL.seat_id), [1, 2])
        self.assertEqual(queries.parent_candidates(self.conn, IMPL.seat_id, project_id="q"), [])
        self.assertEqual(queries.parent_candidates(self.conn, IMPL.seat_id, project_id="p"), [1, 2])

    def test_unopened_pointers_on_review(self):
        first = self.submit(draft(to="v3-engine.implementer"))
        second = self.submit(draft(parent="#1", to="v3-engine.implementer"))
        third = self.submit(draft(parent="#2", to="v3-other.planner"))  # not IMPL's
        review = self.submit(draft(kind="REVIEW", needs="review", parent="#3",
                                   to="v3-engine.implementer"), sender=OTHER)
        queries.mark_opened(self.conn, first.relay_id, IMPL.seat_id, 1)
        self.assertEqual(queries.unopened_in_lineage(self.conn, review.relay_id, IMPL.seat_id),
                         [second.handle])
        result = queries.read(self.conn, review.relay_id, IMPL.seat_id, 2)
        self.assertEqual(result.unopened, [second.handle])
        plain = self.submit(draft(parent="#3", to="v3-engine.implementer"), sender=OTHER)
        self.assertEqual(queries.read(self.conn, plain.relay_id, IMPL.seat_id, 3).unopened, [])
        self.assertIsNotNone(third)


class TestFlags(Store):
    """Amendment 4 A4.4 (operator P9): 15 minutes unopened from admission; 60 minutes idle
    with open must-act work; a seat waiting on its operator QUESTION is never flagged."""
    MIN = 60_000

    def flags(self, now):
        return [(f.rule, f.handle, f.seat_id)
                for f in queries.unopened_flags(self.conn, PLANNER.project_id, now)]

    def test_unopened_flag_from_admission(self):
        start = self.clock
        review = self.submit(draft(to="v3-engine.implementer", cc="v3-other.planner",
                                   needs="review"))
        self.submit(draft(to="v3-engine.implementer", needs="none"))
        self.assertEqual(self.flags(start + 15 * self.MIN - 1), [])
        self.assertEqual(self.flags(start + 15 * self.MIN),
                         [("unopened", review.handle, IMPL.seat_id)])  # CC, NEEDS none: never
        queries.note_activity(self.conn, IMPL.seat_id, start + 15 * self.MIN - 1)  # active seat
        queries.mark_delivered(self.conn, review.relay_id, IMPL.seat_id, start)  # delivered
        self.assertEqual(self.flags(start + 15 * self.MIN),
                         [("unopened", review.handle, IMPL.seat_id)])
        queries.mark_opened(self.conn, review.relay_id, IMPL.seat_id, start + 16 * self.MIN)
        self.assertEqual(self.flags(start + 16 * self.MIN), [])

    def test_unopened_flag_needs_no_delivery(self):  # a seat whose session is down
        start = self.clock
        review = self.submit(draft(to="v3-engine.implementer", needs="review"))
        self.assertEqual(self.flags(start + 24 * 60 * self.MIN),
                         [("unopened", review.handle, IMPL.seat_id)])

    def test_idle_flag_from_later_start(self):
        start = self.clock
        first = self.submit(draft(to="v3-engine.implementer", needs="act"))
        self.clock += 1
        newest = self.submit(draft(to="v3-engine.implementer", needs="answer"))
        for relay in (first, newest):  # opened, so only the idle rule can fire
            queries.mark_opened(self.conn, relay.relay_id, IMPL.seat_id, start + 2)
        queries.note_activity(self.conn, IMPL.seat_id, start + 2)
        self.assertEqual(self.flags(start + 2 + 60 * self.MIN - 1), [])
        self.assertEqual(self.flags(start + 2 + 60 * self.MIN),
                         [("idle", newest.handle, IMPL.seat_id)])  # names the newest
        queries.note_activity(self.conn, IMPL.seat_id, start + 2 + 59 * self.MIN)  # a request at 59
        self.assertEqual(self.flags(start + 2 + 60 * self.MIN), [])
        # a must-act relay admitted to a seat idle for two hours starts the clock at admission
        self.clock = start + 2 + 59 * self.MIN + 120 * self.MIN
        late = self.submit(draft(to="v3-engine.implementer", needs="act"))
        queries.mark_opened(self.conn, late.relay_id, IMPL.seat_id, self.clock)
        self.assertEqual(self.flags(self.clock + 60 * self.MIN - 1), [])
        self.assertEqual(self.flags(self.clock + 60 * self.MIN),
                         [("idle", late.handle, IMPL.seat_id)])
        # answering all of it ends the idle rule
        for relay in (first, newest, late):
            self.submit(draft(parent="#%d" % relay.handle, to="v3-engine.planner"), sender=IMPL)
        self.assertEqual(self.flags(self.clock + 61 * self.MIN), [])

    def test_review_is_not_must_act(self):
        start = self.clock
        review = self.submit(draft(to="v3-engine.implementer", needs="review"))
        queries.mark_opened(self.conn, review.relay_id, IMPL.seat_id, start)
        self.assertEqual(self.flags(start + 24 * 60 * self.MIN), [])

    def test_operator_question_exempts_seat(self):
        start = self.clock
        relays = [self.submit(draft(to="v3-engine.implementer", needs=needs))
                  for needs in ("act", "answer")]
        late = start + 24 * 60 * self.MIN
        self.submit(draft(to="v3-engine.planner", needs="answer"), sender=IMPL)  # not a QUESTION
        mine = lambda: [flag for flag in self.flags(late) if flag[2] == IMPL.seat_id]
        self.assertEqual(len(mine()), 3)  # an unanswered SITREP of its own exempts nothing
        question = self.submit(draft(kind="QUESTION", to="operator", needs="answer"),
                               sender=IMPL)
        self.assertEqual(mine(), [])  # neither rule, for the whole seat
        self.assertEqual([f for f in self.flags(late) if f[2] == OPERATOR.seat_id], [])  # not a seat
        self.submit(draft(parent="#%d" % question.handle, to="v3-engine.implementer"),
                    sender=OPERATOR)  # the operator answers
        self.assertEqual(sorted(mine()),
                         sorted([("unopened", relays[0].handle, IMPL.seat_id),
                                 ("unopened", relays[1].handle, IMPL.seat_id),
                                 ("idle", relays[1].handle, IMPL.seat_id)]))

    def test_answered_relay_is_not_unopened(self):  # PR-3 fold R1 (planner ruling)
        start = self.clock
        answered = self.submit(draft(to="v3-engine.implementer", needs="review"))
        waiting = self.submit(draft(to="v3-engine.implementer", needs="review"))
        self.submit(draft(parent="#%d" % answered.handle, to="v3-engine.planner"), sender=IMPL)
        self.assertEqual(self.flags(start + 15 * self.MIN + 1),
                         [("unopened", waiting.handle, IMPL.seat_id)])  # neither opened

    def test_question_exempts_only_its_project(self):  # PR-3 fold R12
        start = self.clock
        act = self.submit(draft(to="v3-engine.implementer", needs="act"))
        elsewhere = submit.SeatRef(IMPL.seat_id, IMPL.address, IMPL.role, "t9", "q")
        self.submit(draft(kind="QUESTION", to="operator", needs="answer"), sender=elsewhere)
        self.assertEqual(self.flags(start + 24 * 60 * self.MIN),
                         [("idle", act.handle, IMPL.seat_id),
                          ("unopened", act.handle, IMPL.seat_id)])
        self.submit(draft(kind="QUESTION", to="operator", needs="answer"), sender=IMPL)
        self.assertEqual(self.flags(start + 24 * 60 * self.MIN), [])  # control: its own project

    def test_read_is_activity(self):
        start = self.clock
        act = self.submit(draft(to="v3-engine.implementer", needs="act"))
        other = self.submit(draft(to="v3-engine.implementer", needs="none"))
        queries.mark_opened(self.conn, act.relay_id, IMPL.seat_id, start)
        queries.read(self.conn, other.relay_id, IMPL.seat_id, start + 59 * self.MIN)
        self.assertEqual(self.flags(start + 60 * self.MIN), [])

    def test_activity_keeps_latest(self):
        queries.note_activity(self.conn, IMPL.seat_id, 50)
        queries.note_activity(self.conn, IMPL.seat_id, 20)
        self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity").fetchall(),
                         [(50,)])


class TestFindings(GitStore):
    def review_with(self, findings, refs_=(), parent=None, to="v3-engine.implementer",
                    sender=PLANNER):
        request = self.submit(draft(kind="REVIEW", needs="review", to=sender.address,
                                    bundle="loop"), sender=IMPL)
        return self.submit(draft(kind="REVIEW", verdict="must-revise",
                                 parent=parent or "#%d" % request.handle, to=to,
                                 refs=refs_, findings=findings), sender=sender)

    def test_blocking_findings_states(self):
        check = self.directory_file(b"FAILED\n")
        review = self.review_with(("blocking 1 a", "blocking 1 b", "blocking 1 c", "blocking 1 d",
                                   "advisory e"), refs_=("file:" + check,))
        fold = lambda fixes: self.submit(draft(kind="REVIEW-FOLD", parent="#%d" % review.handle,
                                               to="v3-engine.planner", fixes=fixes), sender=IMPL)
        fold(("F2 " + "c" * 40, "F3 " + "c" * 40))
        self.submit(draft(parent="#%d" % review.handle, to="v3-engine.implementer",
                          resolves=("F3 fix-accepted ok", "F4 withdrawn reading only")))
        bundle = self.relay(review.relay_id)["bundle_id"]
        rows = queries.blocking_findings(self.conn, bundle)
        self.assertEqual([(r.handle, r.status) for r in rows], [(1, "open"), (2, "fixed")])

    def directory_file(self, data):
        import tempfile
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder))
        path = os.path.join(os.path.realpath(folder), "check.txt")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_loop_flag(self):
        repo = Repo({"a.py": "a\n", "b.py": "b\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        first = self.review_with(("blocking 1 a.py breaks",), refs_=("bivpak@main:a.py",))
        repo.commit_files({"a.py": "a fixed\n"})
        fix = git(repo.path, "rev-parse", "HEAD")
        self.submit(draft(kind="REVIEW-FOLD", parent="#%d" % first.handle, to="v3-engine.planner",
                          fixes=("F1 " + fix,)), sender=IMPL)
        second = self.submit(draft(kind="REVIEW", verdict="must-revise",
                                   parent="#%d" % first.handle, to="v3-engine.implementer",
                                   refs=("bivpak@main:a.py", "bivpak@main:b.py"),
                                   findings=("blocking 1 a.py again", "blocking 2 b.py new")))
        bundle = self.relay(second.relay_id)["bundle_id"]
        refusals = self.counts()["refusals"]
        self.assertEqual(queries.loop_flags(self.conn, bundle, repo.path), [2])
        self.assertEqual(self.counts()["refusals"], refusals)  # nothing refused

    def test_loop_flag_subject_path(self):  # PR-3 fold R3: "check or subject path"
        repo = Repo({"a.py": "a\n", "b.py": "b\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        first = self.review_with(("blocking 2 a.py breaks",),
                                 refs_=("bivpak@main:a.py", "file:" + self.directory_file(b"F\n")))
        repo.commit_files({"a.py": "a fixed\n"})
        self.submit(draft(kind="REVIEW-FOLD", parent="#%d" % first.handle, to="v3-engine.planner",
                          fixes=("F1 " + git(repo.path, "rev-parse", "HEAD"),)), sender=IMPL)
        again = self.directory_file(b"F again\n")
        second = self.submit(draft(kind="REVIEW", verdict="must-revise",
                                   parent="#%d" % first.handle, to="v3-engine.implementer",
                                   refs=("bivpak@main:a.py", "file:" + again),
                                   findings=("blocking 2 a.py breaks again",)))
        third = self.submit(draft(kind="REVIEW", verdict="must-revise",
                                  parent="#%d" % first.handle, to="v3-engine.implementer",
                                  refs=("bivpak@main:b.py", "file:" + again),
                                  findings=("blocking 2 b.py is new",)))
        bundle = self.relay(second.relay_id)["bundle_id"]
        self.assertEqual(queries.loop_flags(self.conn, bundle, repo.path), [2])  # not F3
        self.assertIsNotNone(third)

    def test_bundle_metrics(self):
        check = self.directory_file(b"x\n")
        review = self.review_with(("blocking 1 a", "advisory b"), refs_=("file:" + check,))
        bundle = self.relay(review.relay_id)["bundle_id"]
        metrics = queries.bundle_metrics(self.conn, bundle)
        sizes = self.conn.execute("SELECT kind, sum(body_bytes + header_bytes) FROM relays"
                                  " WHERE bundle_id = ? GROUP BY kind", (bundle,)).fetchall()
        self.assertEqual(metrics.bytes_by_kind, dict(sizes))
        self.assertEqual(metrics.rounds, 1)
        self.assertEqual(metrics.cited_check_share, 0.5)
        self.submit(draft(kind="REVIEW", verdict="approve", parent="#%d" % review.handle,
                          to="v3-engine.implementer"))  # an approval is not a round
        self.assertEqual(queries.bundle_metrics(self.conn, bundle).rounds, 1)

    def test_refusal_counts(self):
        self.refused("E-PARENT-UNKNOWN", draft(parent="#9"))
        self.refused("E-PARENT-UNKNOWN", draft(parent="#8"))
        self.refused("E-HEADER-UNKNOWN-KEY", b"KIND: SITREP\nROLE: x\n\nbody")
        self.assertEqual(queries.refusal_counts(self.conn, PLANNER.project_id),
                         {"E-PARENT-UNKNOWN": 2, "E-HEADER-UNKNOWN-KEY": 1})
        self.assertEqual(queries.refusal_counts(self.conn, "elsewhere"), {})


class TestEdges(GitStore):
    def test_points_to_and_from(self):
        repo = Repo({"a.py": "1\n2\n3\n4\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        target = self.submit(draft())
        citing = self.submit(draft(refs=(target.relay_id, "bivpak@main:a.py#L2-L3",
                                         "bivpak@main:a.py")))
        self.assertEqual([e.spelling for e in queries.points_to(self.conn, citing.relay_id)],
                         [target.relay_id, "bivpak@main:a.py#L2-L3", "bivpak@main:a.py"])
        by_id = queries.pointed_from(self.conn, refs.parse_ref(target.relay_id))
        by_handle = queries.pointed_from(self.conn, refs.parse_ref("#1@p"))
        self.assertEqual([e.from_relay_id for e in by_id], [citing.relay_id])
        self.assertEqual(by_handle, by_id)
        line4 = queries.pointed_from(self.conn, refs.parse_ref("bivpak@main:a.py#L4"))
        self.assertEqual([e.spelling for e in line4], ["bivpak@main:a.py"])  # whole file only
        line3 = queries.pointed_from(self.conn, refs.parse_ref("bivpak@main:a.py#L3"))
        self.assertEqual(len(line3), 2)
        with self.assertRaises(ValueError):
            queries.pointed_from(self.conn, refs.parse_ref("#1"))
        # a repository rename keeps its edges, matched by J3's id within the project
        self.conn.execute("UPDATE repositories SET name = 'bivpak2' WHERE id = 'repo-1'")
        renamed = refs.parse_ref("bivpak2@main:a.py#L3")
        self.assertEqual(len(queries.pointed_from(self.conn, renamed, project_id=PLANNER.project_id)),
                         2)
        self.assertEqual(queries.pointed_from(self.conn, renamed, project_id="q"), [])

    def test_changed_paths(self):
        repo = Repo({"a.py": "a\n", "d/b.py": "b\n"})
        self.addCleanup(repo.close)
        root = git(repo.path, "rev-parse", "HEAD")
        repo.commit_files({"a.py": "a2\n"})
        self.assertEqual(refs.changed_paths(repo.path, root), {"a.py", "d/b.py"})
        self.assertEqual(refs.changed_paths(repo.path, git(repo.path, "rev-parse", "HEAD")),
                         {"a.py"})

    def test_cited_range_changed(self):
        repo = Repo({"a.py": "1\n2\n3\n4\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        citing = self.submit(draft(refs=("bivpak@main:a.py#L2-L3", "bivpak@main:a.py")))
        ranged, whole = [e.id for e in queries.points_to(self.conn, citing.relay_id)]
        repo.commit_files({"a.py": "1\n2\n3\nfour\n"})  # outside the cited lines
        other = git(repo.path, "rev-parse", "HEAD")
        self.assertFalse(queries.cited_range_changed(self.conn, ranged, other, repo.path))
        self.assertTrue(queries.cited_range_changed(self.conn, whole, other, repo.path))
        repo.commit_files({"a.py": "1\ntwo\n3\nfour\n"})  # inside them
        inside = git(repo.path, "rev-parse", "HEAD")
        self.assertTrue(queries.cited_range_changed(self.conn, ranged, inside, repo.path))
        git(repo.path, "rm", "-q", "a.py")
        git(repo.path, "commit", "-q", "-m", "gone")
        gone = git(repo.path, "rev-parse", "HEAD")
        self.assertTrue(queries.cited_range_changed(self.conn, ranged, gone, repo.path))


    def test_cited_range_maps_through_hunks(self):  # PR-3 fold R5
        repo = Repo({"a.py": "1\n2\n3\n4\n5\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        citing = self.submit(draft(refs=("bivpak@main:a.py#L3-L4",)))
        (edge,) = [e.id for e in queries.points_to(self.conn, citing.relay_id)]
        cases = (("0\n1\n2\n3\n4\n5\n", False),  # a line inserted above
                 ("2\n3\n4\n5\n", False),  # a line removed above
                 ("1\n2\n3\n4\n5\n6\n", False),  # appended below
                 ("1\n2\nx\n3\n4\n5\n", False),  # inserted just above the range
                 ("1\n2\n3\n4\nx\n5\n", False),  # inserted just below it
                 ("1\n2\n3\nx\n4\n5\n", True),  # inserted inside it
                 ("1\n2\nthree\n4\n5\n", True),  # edited inside it
                 ("1\n2\n3\n5\n", True),  # a cited line removed
                 ("0\n1\n2\n3\nfour\n5\n", True))  # shifted and edited
        for text, changed in cases:
            with self.subTest(text=text):
                repo.commit_files({"a.py": text})
                commit = git(repo.path, "rev-parse", "HEAD")
                self.assertIs(queries.cited_range_changed(self.conn, edge, commit, repo.path),
                              changed)


class TestRecords(Store):
    def test_session_dirty_transaction_is_preserved(self):
        """Neither session helper may dispose of a caller-owned transaction."""
        current = queries.record_session(self.conn, "host", "codex", IMPL.seat_id, 10)
        before = self.conn.execute("SELECT * FROM seat_sessions ORDER BY id").fetchall()
        self.conn.execute("UPDATE seat_occupants SET issued_at = 321 WHERE seat_id = ?",
                          (IMPL.seat_id,))
        for operation in (lambda: queries.record_session(self.conn, "next", "codex",
                                                        IMPL.seat_id, 20),
                          lambda: queries.end_session(self.conn, current, 20)):
            with self.assertRaises(ValueError):
                operation()
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants"
                                               " WHERE seat_id = ?", (IMPL.seat_id,)
                                               ).fetchone(), (321,))
            self.assertEqual(self.conn.execute("SELECT * FROM seat_sessions ORDER BY id"
                                               ).fetchall(), before)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                           (IMPL.seat_id,)).fetchone(), (1,))
        queries.end_session(self.conn, current, 30)
        next_id = queries.record_session(self.conn, "next", "codex", IMPL.seat_id, 40)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions"
                                           " ORDER BY signed_in_at").fetchall(),
                         [(current, 30), (next_id, None)])

    def test_token_sources(self):
        bundle = self.relay(self.submit(draft()).relay_id)["bundle_id"]
        counts = queries.TokenCounts(input=10, output=5, cache_read=2, cache_write=1)
        queries.record_token_counts(self.conn, IMPL.seat_id, bundle, "host-log", counts, at_ms=1)
        queries.record_token_counts(self.conn, IMPL.seat_id, bundle, "host-reported", counts,
                                    at_ms=2)
        self.assertEqual(self.conn.execute("SELECT source, input, output, cache_read, cache_write,"
                                           " copied_at FROM token_counts ORDER BY id").fetchall(),
                         [("host-log", 10, 5, 2, 1, 1), ("host-reported", 10, 5, 2, 1, 2)])
        with self.assertRaises(ValueError):
            queries.record_token_counts(self.conn, IMPL.seat_id, bundle, "guess", counts, at_ms=3)
        self.assertEqual(self.counts_of("token_counts"), 2)
        with self.assertRaises(TypeError):  # PR-3 fold R13: the caller's clock, never a default
            queries.record_token_counts(self.conn, IMPL.seat_id, bundle, "host-log", counts)

    def counts_of(self, table):
        return self.conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]

    SESSION = ("SELECT host_session_id, host, seat_id, occupant_id, generation, signed_in_at,"
               " last_seen_at, ended_at FROM seat_sessions ORDER BY generation")

    def test_session_rows(self):  # Task 6: platform's seat_sessions (J3 with J-A1)
        session_id = queries.record_session(self.conn, "s1", "claude-code", IMPL.seat_id, 100)
        queries.end_session(self.conn, session_id, 200)
        self.assertEqual(self.conn.execute(self.SESSION).fetchall(),
                         [("s1", "claude-code", IMPL.seat_id, "o-" + IMPL.seat_id, 1, 100, 100,
                           200)])
        for session in (session_id, "absent"):
            with self.subTest(session=session):
                with self.assertRaises(ValueError):
                    queries.end_session(self.conn, session, 300)
        self.assertEqual(self.conn.execute("SELECT ended_at FROM seat_sessions").fetchone(),
                         (200,))

    def test_shared_host_session_end_targets_internal_id(self):
        """A host-text selector or absent internal-id return cannot end just seat A."""
        first = queries.record_session(self.conn, "shared", "codex", IMPL.seat_id, 10)
        second = queries.record_session(self.conn, "shared", "frank", PLANNER.seat_id, 20)
        self.assertIsInstance(first, str)
        self.assertIsInstance(second, str)
        self.assertNotEqual(first, second)
        before = self.conn.execute("SELECT id, host_session_id, ended_at FROM seat_sessions"
                                   " ORDER BY signed_in_at").fetchall()
        self.assertEqual(before, [(first, "shared", None), (second, "shared", None)])
        with self.assertRaises(ValueError):
            queries.end_session(self.conn, "shared", 25)
        self.assertEqual(self.conn.execute("SELECT id, host_session_id, ended_at"
                                           " FROM seat_sessions ORDER BY signed_in_at").fetchall(),
                         before)
        queries.end_session(self.conn, first, 30)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions"
                                           " ORDER BY signed_in_at").fetchall(),
                         [(first, 30), (second, None)])
        queries.end_session(self.conn, second, 40)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions"
                                           " ORDER BY signed_in_at").fetchall(),
                         [(first, 30), (second, 40)])
        retain_query_evidence("shared-host-internal-id", {
            "before": before, "after": self.conn.execute(
                "SELECT id, host_session_id, ended_at FROM seat_sessions"
                " ORDER BY signed_in_at").fetchall()})

    def test_stale_session_id_cannot_end_replacement(self):
        """A stale internal id cannot end the current generation or another seat."""
        old = queries.record_session(self.conn, "reused", "codex", IMPL.seat_id, 10)
        current = queries.record_session(self.conn, "reused", "codex", IMPL.seat_id, 20)
        peer = queries.record_session(self.conn, "reused", "other", PLANNER.seat_id, 30)
        self.assertIsInstance(current, str)
        before = self.conn.execute("SELECT id, generation, ended_at FROM seat_sessions"
                                   " ORDER BY signed_in_at").fetchall()
        self.assertEqual(before, [(old, 1, 20), (current, 2, None), (peer, 1, None)])
        for stale in (old, "missing", "reused"):
            with self.subTest(stale=stale):
                with self.assertRaises(ValueError):
                    queries.end_session(self.conn, stale, 40)
                self.assertEqual(self.conn.execute("SELECT id, generation, ended_at"
                                                   " FROM seat_sessions ORDER BY signed_in_at"
                                                   ).fetchall(), before)
        queries.end_session(self.conn, current, 50)
        self.assertEqual(self.conn.execute("SELECT id, generation, ended_at FROM seat_sessions"
                                           " ORDER BY signed_in_at").fetchall(),
                         [(old, 1, 20), (current, 2, 50), (peer, 1, None)])
        retain_query_evidence("stale-internal-id", {
            "before": before, "after": self.conn.execute(
                "SELECT id, generation, ended_at FROM seat_sessions"
                " ORDER BY signed_in_at").fetchall()})

    def test_sign_in_generations(self):  # J-T10: 1, 2, 3, each closing the previous
        for generation, at in ((1, 10), (2, 20), (3, 30)):
            queries.record_session(self.conn, "h%d" % generation, "codex", IMPL.seat_id, at)
        self.assertEqual([row[4:] for row in self.conn.execute(self.SESSION)],
                         [(1, 10, 10, 20), (2, 20, 20, 30), (3, 30, 30, None)])
        queries.record_session(self.conn, "other", "frank", PLANNER.seat_id, 40)  # per seat
        self.assertEqual(self.conn.execute("SELECT generation FROM seat_sessions WHERE seat_id = ?",
                                           (PLANNER.seat_id,)).fetchall(), [(1,)])
        self.conn.execute("UPDATE seat_occupants SET ended_at = 50 WHERE seat_id = ?",
                          (IMPL.seat_id,))
        self.conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier,"
                          " issued_at) VALUES ('o-new', ?, 'k-new', 'v', 51)", (IMPL.seat_id,))
        self.conn.commit()
        queries.record_session(self.conn, "h4", "codex", IMPL.seat_id, 60)  # across occupants
        self.assertEqual(self.conn.execute(
            "SELECT occupant_id, generation FROM seat_sessions WHERE host_session_id = 'h4'"
        ).fetchone(), ("o-new", 4))

    def test_sign_in_refusals(self):
        for host, seat in (("vim", IMPL.seat_id), ("codex", "seat-nobody")):
            with self.subTest(host=host, seat=seat):
                with self.assertRaises(ValueError):
                    queries.record_session(self.conn, "h", host, seat, 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM seat_sessions").fetchone(), (0,))

    def test_sign_in_serializes_occupant_replacement(self):
        """C1: a replacement cannot commit between selecting and binding the occupant."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sessions.db")
            signing_in = sqlite3.connect(path)
            replacing = sqlite3.connect(path, timeout=0.02)
            try:
                self.conn.backup(signing_in)
                signing_in.execute("PRAGMA foreign_keys = ON")
                replacing.execute("PRAGMA foreign_keys = ON")
                self.assertEqual(signing_in.execute("PRAGMA journal_mode = WAL").fetchone(),
                                 ("wal",))
                attempts = []

                def replace():
                    replacing.execute("BEGIN IMMEDIATE")
                    replacing.execute("UPDATE seat_occupants SET ended_at = 25"
                                      " WHERE seat_id = ? AND ended_at IS NULL", (IMPL.seat_id,))
                    replacing.execute("INSERT INTO seat_occupants"
                                      " (id, seat_id, key_id, key_verifier, issued_at)"
                                      " VALUES ('o-replacement', ?, 'k-replacement', 'v', 26)",
                                      (IMPL.seat_id,))
                    replacing.commit()

                def after_selection(cursor, row):
                    # SQLite's row factory returns the real selected row after fetching it.
                    # WAL lets the other writer commit here unless sign-in owns its lock.
                    if not attempts and [col[0] for col in cursor.description] == ["id"]:
                        try:
                            replace()
                        except sqlite3.OperationalError as error:
                            attempts.append(error.sqlite_errorcode)
                            replacing.rollback()
                        else:
                            attempts.append("replacement committed")
                    return row

                signing_in.row_factory = after_selection
                queries.record_session(signing_in, "first-host", "codex", IMPL.seat_id, 10)
                signing_in.row_factory = None
                self.assertEqual(signing_in.execute(
                    "SELECT s.occupant_id, o.ended_at, s.generation, s.ended_at"
                    " FROM seat_sessions s JOIN seat_occupants o ON o.id = s.occupant_id"
                ).fetchall(), [("o-" + IMPL.seat_id, None, 1, None)])
                self.assertEqual(attempts, [sqlite3.SQLITE_BUSY])
                replace()  # positive control: replacement succeeds after sign-in commits
                queries.record_session(signing_in, "second-host", "codex", IMPL.seat_id, 40)
                self.assertEqual(signing_in.execute(
                    "SELECT occupant_id, generation, ended_at FROM seat_sessions"
                    " ORDER BY generation").fetchall(),
                    [("o-" + IMPL.seat_id, 1, 40), ("o-replacement", 2, None)])
            finally:
                signing_in.close()
                replacing.close()

    def test_overlapping_sign_ins_keep_generations(self):
        """J-A1 J-T10: two overlapping sign-ins both succeed and leave one open session."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sessions.db")
            first = sqlite3.connect(path)
            self.conn.backup(first)
            first.execute("PRAGMA foreign_keys = ON")
            selected = threading.Event()
            attempted = threading.Event()
            failures = []

            def second_sign_in():
                conn = sqlite3.connect(path, timeout=2)
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                    if not selected.wait(2):
                        raise AssertionError("first sign-in never selected its occupant")
                    conn.set_trace_callback(lambda sql: attempted.set()
                                            if sql == "BEGIN IMMEDIATE" else None)
                    queries.record_session(conn, "second-host", "codex", IMPL.seat_id, 20)
                except BaseException as error:
                    failures.append(error)
                finally:
                    conn.close()

            def after_selection(cursor, row):
                if [col[0] for col in cursor.description] == ["id"]:
                    selected.set()
                    if not attempted.wait(2):
                        raise AssertionError("second sign-in never attempted its transaction")
                return row

            worker = threading.Thread(target=second_sign_in)
            first.row_factory = after_selection
            worker.start()
            try:
                queries.record_session(first, "first-host", "codex", IMPL.seat_id, 10)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                first.row_factory = None
                self.assertEqual(first.execute(
                    "SELECT host_session_id, generation, ended_at FROM seat_sessions"
                    " ORDER BY generation").fetchall(),
                    [("first-host", 1, 20), ("second-host", 2, None)])
            finally:
                selected.set()
                worker.join(3)
                first.close()


class TestSessionSteps(Store):
    SESSION = ("SELECT host_session_id, host, seat_id, occupant_id, generation, signed_in_at,"
               " last_seen_at, ended_at FROM seat_sessions ORDER BY generation")

    def test_session_steps_need_a_transaction(self):
        """M1/M2: a clean connection must not acquire a step-owned transaction."""
        current = queries.record_session(self.conn, "old", "codex", IMPL.seat_id, 10)
        before = self.conn.execute("SELECT * FROM seat_sessions").fetchall()
        for operation in (lambda: queries.record_session_step(
                self.conn, "new", "codex", IMPL.seat_id, 20),
                lambda: queries.end_session_step(self.conn, current, 30)):
            with self.assertRaisesRegex(ValueError, "session step needs an open transaction"):
                operation()
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT * FROM seat_sessions").fetchall(), before)

    def test_session_step_joins_caller_transaction(self):
        """M3: a record step must preserve caller writes and the caller's commit choice."""
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE seat_occupants SET issued_at = 321 WHERE seat_id = ?",
                          (PLANNER.seat_id,))
        first = queries.record_session_step(self.conn, "first", "codex", IMPL.seat_id, 10)
        self.assertTrue(self.conn.in_transaction)
        self.assertIsInstance(first, str)
        self.assertEqual(self.conn.execute(self.SESSION).fetchall(),
                         [("first", "codex", IMPL.seat_id, "o-" + IMPL.seat_id,
                           1, 10, 10, None)])
        self.assertEqual(self.conn.execute("SELECT id FROM seat_sessions").fetchone(), (first,))
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                           (PLANNER.seat_id,)).fetchone(), (321,))
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE seat_occupants SET issued_at = 456 WHERE seat_id = ?",
                          (PLANNER.seat_id,))
        second = queries.record_session_step(self.conn, "rolled-back", "frank", IMPL.seat_id, 20)
        self.assertTrue(self.conn.in_transaction)
        self.assertNotEqual(first, second)
        self.conn.rollback()
        self.assertEqual(self.conn.execute(self.SESSION).fetchall(),
                         [("first", "codex", IMPL.seat_id, "o-" + IMPL.seat_id,
                           1, 10, 10, None)])
        self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                           (PLANNER.seat_id,)).fetchone(), (321,))
        self.conn.execute("BEGIN IMMEDIATE")
        second = queries.record_session_step(self.conn, "second", "frank", IMPL.seat_id, 30)
        third = queries.record_session_step(self.conn, "third", "other", IMPL.seat_id, 40)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(len({first, second, third}), 3)
        self.conn.commit()
        self.assertEqual(self.conn.execute(self.SESSION).fetchall(), [
            ("first", "codex", IMPL.seat_id, "o-" + IMPL.seat_id, 1, 10, 10, 30),
            ("second", "frank", IMPL.seat_id, "o-" + IMPL.seat_id, 2, 30, 30, 40),
            ("third", "other", IMPL.seat_id, "o-" + IMPL.seat_id, 3, 40, 40, None)])

    def test_end_session_step_joins_caller_transaction(self):
        """M4: ending a session is provisional until its caller commits."""
        current = queries.record_session(self.conn, "host", "codex", IMPL.seat_id, 10)
        self.conn.execute("BEGIN IMMEDIATE")
        self.assertIsNone(queries.end_session_step(self.conn, current, 20))
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT ended_at FROM seat_sessions").fetchone(), (20,))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT ended_at FROM seat_sessions").fetchone(), (None,))
        self.conn.execute("BEGIN IMMEDIATE")
        queries.end_session_step(self.conn, current, 30)
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT ended_at FROM seat_sessions").fetchone(), (30,))

    def test_session_step_refusal_keeps_caller_writes(self):
        """M5/M6: refused record/end writes cannot survive a caller's later commit."""
        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)
        peer = queries.record_session(self.conn, "peer", "frank", PLANNER.seat_id, 11)
        self.conn.execute("UPDATE seat_occupants SET ended_at = 12 WHERE seat_id = ?",
                          (IMPL.seat_id,))
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE seat_occupants SET issued_at = 321 WHERE seat_id = ?",
                          (PLANNER.seat_id,))
        # Capturing the real result makes a missing refusal an assertion, not an ERROR.
        for operation, message in (
                (lambda: queries.record_session_step(self.conn, "bad-host", "vim",
                                                       IMPL.seat_id, 20), "unknown host"),
                (lambda: queries.record_session_step(self.conn, "no-occupant", "codex",
                                                       IMPL.seat_id, 20), "has no open occupant"),
                (lambda: queries.end_session_step(self.conn, "absent", 20), "no open session")):
            with self.assertRaisesRegex(ValueError, message):
                operation()
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                               (PLANNER.seat_id,)).fetchone(), (321,))
            self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions"
                                               " ORDER BY signed_in_at").fetchall(),
                             [(previous, None), (peer, None)])
        queries.end_session_step(self.conn, peer, 21)
        with self.assertRaisesRegex(ValueError, "no open session"):
            queries.end_session_step(self.conn, peer, 22)
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions"
                                           " ORDER BY signed_in_at").fetchall(),
                         [(previous, None), (peer, 21)])
        self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                           (PLANNER.seat_id,)).fetchone(), (321,))

    def test_session_step_serializes_in_deferred_transaction(self):
        """M7: the occupant read must follow the write lock, even in plain BEGIN."""
        step = queries.record_session_step
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sessions.db")
            signing_in = sqlite3.connect(path)
            replacing = sqlite3.connect(path, timeout=0.02)
            try:
                self.conn.backup(signing_in)
                signing_in.execute("PRAGMA foreign_keys = ON")
                replacing.execute("PRAGMA foreign_keys = ON")
                self.assertEqual(signing_in.execute("PRAGMA journal_mode = WAL").fetchone(), ("wal",))
                attempts = []

                def replace(occupant, at):
                    replacing.execute("BEGIN IMMEDIATE")
                    replacing.execute("UPDATE seat_occupants SET ended_at = ?"
                                      " WHERE seat_id = ? AND ended_at IS NULL", (at, IMPL.seat_id))
                    replacing.execute("INSERT INTO seat_occupants"
                                      " (id, seat_id, key_id, key_verifier, issued_at)"
                                      " VALUES (?, ?, ?, 'v', ?)",
                                      (occupant, IMPL.seat_id, 'k-' + occupant, at + 1))
                    replacing.commit()

                def after_selection(cursor, row):
                    if not attempts and [col[0] for col in cursor.description] == ["id"]:
                        try:
                            replace("o-replacement", 25)
                        except sqlite3.OperationalError as error:
                            attempts.append(error.sqlite_errorcode)
                            replacing.rollback()
                        else:
                            attempts.append("replacement committed")
                    return row

                signing_in.execute("BEGIN")
                signing_in.row_factory = after_selection
                try:
                    outcome = step(signing_in, "first", "codex", IMPL.seat_id, 10)
                except BaseException as error:
                    outcome = error
                signing_in.row_factory = None
                self.assertIsInstance(outcome, str, repr(outcome))
                self.assertTrue(signing_in.in_transaction)
                self.assertEqual(attempts, [sqlite3.SQLITE_BUSY])
                self.assertEqual(signing_in.execute(
                    "SELECT occupant_id, generation, ended_at FROM seat_sessions").fetchall(),
                    [("o-" + IMPL.seat_id, 1, None)])
                signing_in.commit()
                replace("o-replacement", 25)  # Positive control: the writer works after commit.
                signing_in.execute("BEGIN")
                signing_in.execute("SELECT id FROM seat_occupants").fetchall()
                replace("o-newer", 35)
                before = signing_in.execute("SELECT * FROM seat_sessions").fetchall()
                try:
                    outcome = step(signing_in, "stale", "codex", IMPL.seat_id, 40)
                except BaseException as error:
                    outcome = error
                self.assertIsInstance(outcome, sqlite3.OperationalError, repr(outcome))
                self.assertEqual(getattr(outcome, "sqlite_errorcode", None), 517)
                self.assertTrue(signing_in.in_transaction)
                self.assertEqual(signing_in.execute("SELECT * FROM seat_sessions").fetchall(), before)
                signing_in.rollback()
                self.assertEqual(replacing.execute(
                    "SELECT host_session_id FROM seat_sessions").fetchall(), [("first",)])
            finally:
                signing_in.close()
                replacing.close()

    def test_session_step_non_value_errors_roll_back(self):
        """M9/M10/M14: any failure after the closing UPDATE rolls the step back and
        re-raises the same object."""
        class Refusal(ValueError):
            pass

        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)
        failures = (KeyboardInterrupt("fixture interrupt"),
                    sqlite3.OperationalError("fixture operational error"),
                    Refusal("fixture refusal"))
        for issued, failure in enumerate(failures, start=321):
            def occupant_read(cursor, row, failure=failure):
                if [column[0] for column in cursor.description] == ["id"]:
                    raise failure
                return row

            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("UPDATE seat_occupants SET issued_at = ? WHERE seat_id = ?",
                              (issued, PLANNER.seat_id))
            self.conn.row_factory = occupant_read
            try:
                outcome = queries.record_session_step(self.conn, "failing", "codex",
                                                      IMPL.seat_id, 20)
            except BaseException as error:
                outcome = error
            finally:
                self.conn.row_factory = None
            self.assertIs(outcome, failure)
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                               (PLANNER.seat_id,)).fetchone(), (issued,))
            self.conn.commit()
            self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                             [(previous, None)], repr(failure))

    def test_record_session_refusal_ends_transaction(self):
        """M11: a refused sign-in leaves no transaction open on the connection."""
        self.conn.execute("UPDATE seat_occupants SET ended_at = 12 WHERE seat_id = ?",
                          (IMPL.seat_id,))
        self.conn.commit()
        for seat in (IMPL.seat_id, "v3-unknown.seat"):
            with self.assertRaisesRegex(ValueError, "has no open occupant"):
                queries.record_session(self.conn, "refused-" + seat, "codex", seat, 20)
            self.assertFalse(self.conn.in_transaction, seat)
        current = queries.record_session(self.conn, "after-refusals", "codex",
                                         PLANNER.seat_id, 30)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT id, host_session_id FROM seat_sessions")
                         .fetchall(), [(current, "after-refusals")])

    def test_session_step_release_is_outside_cleanup(self):
        """M8: an interrupt just after the step's RELEASE must not run its cleanup
        against the caller's same-named savepoint."""
        interrupt = KeyboardInterrupt("fixture interrupt after release")

        class InterruptAfterRelease:
            def __init__(self, conn):
                self._conn = conn
                self.fired = False

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args):
                result = self._conn.execute(sql, *args)
                if sql == "RELEASE engine_record_session" and not self.fired:
                    self.fired = True
                    raise interrupt
                return result

        proxy = InterruptAfterRelease(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("SAVEPOINT engine_record_session")
        self.conn.execute("UPDATE seat_occupants SET issued_at = 654 WHERE seat_id = ?",
                          (PLANNER.seat_id,))
        try:
            outcome = queries.record_session_step(proxy, "inside", "codex", IMPL.seat_id, 10)
        except BaseException as error:
            outcome = error
        self.assertIs(outcome, interrupt)
        self.assertTrue(proxy.fired)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT issued_at FROM seat_occupants WHERE seat_id = ?",
                                           (PLANNER.seat_id,)).fetchone(), (654,))
        # Capturing the caller's RELEASE makes a missing savepoint an assertion, not an ERROR.
        try:
            self.conn.execute("RELEASE engine_record_session")
            released = None
        except sqlite3.Error as error:
            released = error
        self.assertIsNone(released)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_end_step_refuses_host_session_id(self):
        """M12: the end step matches only the internal session id."""
        current = queries.record_session(self.conn, "host-unique", "codex", IMPL.seat_id, 10)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "no open session"):
            queries.end_session_step(self.conn, "host-unique", 20)
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                         [(current, None)])

    def test_step_checks_host_before_transaction(self):
        """M13: an unknown host is refused before the transaction check."""
        with self.assertRaisesRegex(ValueError, "unknown host"):
            queries.record_session_step(self.conn, "bad-host", "vim", IMPL.seat_id, 10)
        self.assertFalse(self.conn.in_transaction)

    def test_session_step_release_failure_rolls_back(self):
        """M15: a success RELEASE that fails inside SQLite rolls the step back and
        leaves no step savepoint."""
        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE seat_occupants SET issued_at = 321 WHERE seat_id = ?",
                          (PLANNER.seat_id,))
        self.conn.execute("SAVEPOINT engine_record_session")
        self.conn.execute("UPDATE seat_occupants SET issued_at = 654 WHERE seat_id = ?",
                          (IMPL.seat_id,))
        armed = []

        def trace(statement):
            # A progress handler never runs inside a RELEASE; an interrupt at its start does.
            if statement == "RELEASE engine_record_session" and not armed:
                armed.append(statement)
                self.conn.interrupt()

        self.conn.set_trace_callback(trace)
        try:
            outcome = queries.record_session_step(self.conn, "failing", "codex", IMPL.seat_id, 20)
        except BaseException as error:
            outcome = error
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(armed, ["RELEASE engine_record_session"])
        self.assertIsInstance(outcome, sqlite3.OperationalError, repr(outcome))
        self.assertEqual(getattr(outcome, "sqlite_errorname", None), "SQLITE_INTERRUPT")
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT seat_id, issued_at FROM seat_occupants"
                                           " WHERE seat_id IN (?, ?) ORDER BY seat_id",
                                           (PLANNER.seat_id, IMPL.seat_id)).fetchall(),
                         sorted([(PLANNER.seat_id, 321), (IMPL.seat_id, 654)]))
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                         [(previous, None)])
        # Capturing the caller's RELEASE makes a missing savepoint an assertion, not an ERROR.
        try:
            self.conn.execute("RELEASE engine_record_session")
            released = None
        except sqlite3.Error as error:
            released = error
        self.assertIsNone(released)
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such savepoint"):
            self.conn.execute("RELEASE engine_record_session")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_session_helpers_end_transaction_on_base_exceptions(self):
        """M17/M18/M19: an interrupt inside either helper ends its transaction."""
        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)
        record_interrupt = KeyboardInterrupt("fixture record interrupt")

        def occupant_read(cursor, row):
            if [column[0] for column in cursor.description] == ["id"]:
                raise record_interrupt
            return row

        self.conn.row_factory = occupant_read
        try:
            outcome = queries.record_session(self.conn, "interrupted", "codex", IMPL.seat_id, 20)
        except BaseException as error:
            outcome = error
        finally:
            self.conn.row_factory = None
        self.assertIs(outcome, record_interrupt)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                         [(previous, None)])
        other = queries.record_session(self.conn, "after-record", "codex", PLANNER.seat_id, 30)
        self.assertIsInstance(other, str)

        end_interrupt = KeyboardInterrupt("fixture end interrupt")
        real_step = queries.end_session_step

        def ending(conn, session_id, end_ms):
            real_step(conn, session_id, end_ms)
            raise end_interrupt

        with mock.patch.object(queries, "end_session_step", side_effect=ending):
            try:
                outcome = queries.end_session(self.conn, previous, 40)
            except BaseException as error:
                outcome = error
        self.assertIs(outcome, end_interrupt)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT ended_at FROM seat_sessions WHERE id = ?",
                                           (previous,)).fetchone(), (None,))
        queries.end_session(self.conn, previous, 50)
        self.assertEqual(self.conn.execute("SELECT ended_at FROM seat_sessions WHERE id = ?",
                                           (previous,)).fetchone(), (50,))

    def test_session_step_keeps_error_when_sqlite_ends_transaction(self):
        """M20: when SQLite itself ends the transaction, the step raises SQLite's error."""
        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)

        class InterruptInsert:
            def __init__(self, conn):
                self._conn = conn
                self.fired = False

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args):
                if not sql.startswith("INSERT INTO seat_sessions"):
                    return self._conn.execute(sql, *args)
                self.fired = True
                self._conn.set_progress_handler(lambda: 1, 1)
                try:
                    return self._conn.execute(sql, *args)
                finally:
                    self._conn.set_progress_handler(None, 1)

        proxy = InterruptInsert(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            outcome = queries.record_session_step(proxy, "interrupted", "codex", IMPL.seat_id, 20)
        except BaseException as error:
            outcome = error
        self.assertTrue(proxy.fired)
        self.assertIsInstance(outcome, sqlite3.OperationalError, repr(outcome))
        self.assertEqual(str(outcome), "interrupted")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                         [(previous, None)])

    def test_session_step_release_ending_transaction_keeps_error(self):
        """M21/M22: a success RELEASE failure that ends the transaction re-raises
        SQLite's own error object, with no context."""
        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)
        failure = sqlite3.OperationalError("database or disk is full")

        class EndingRelease:
            def __init__(self, conn):
                self._conn = conn
                self.fired = False

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args):
                # Only the step's success RELEASE, once; the error is raised outside any except.
                if sql != "RELEASE engine_record_session" or self.fired:
                    return self._conn.execute(sql, *args)
                self.fired = True
                self._conn.execute("ROLLBACK")
                raise failure

        proxy = EndingRelease(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            outcome = queries.record_session_step(proxy, "failing", "codex", IMPL.seat_id, 20)
        except BaseException as error:
            outcome = error
        self.assertTrue(proxy.fired)
        self.assertIs(outcome, failure)
        self.assertIsNone(outcome.__context__)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                         [(previous, None)])

    def test_record_session_rolls_back_after_completed_step(self):
        """M23: a sign-in interrupted after its step completed commits nothing."""
        previous = queries.record_session(self.conn, "previous", "codex", IMPL.seat_id, 10)
        step = queries.record_session_step
        interrupt = KeyboardInterrupt()
        completed = []

        def step_then_interrupt(*args, **kwargs):
            completed.append(step(*args, **kwargs))
            raise interrupt

        # The interrupt is caught here: one escaping a test would abort the whole run.
        with mock.patch.object(queries, "record_session_step", step_then_interrupt):
            try:
                outcome = queries.record_session(self.conn, "interrupted", "codex", IMPL.seat_id, 20)
            except BaseException as error:
                outcome = error
        self.assertEqual(len(completed), 1)
        self.assertIs(outcome, interrupt)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions").fetchall(),
                         [(previous, None)])
        following = queries.record_session(self.conn, "following", "codex", IMPL.seat_id, 30)
        self.assertEqual(self.conn.execute("SELECT id, ended_at FROM seat_sessions"
                                           " ORDER BY generation").fetchall(),
                         [(previous, 30), (following, None)])


class TestStoreSupport(Store):
    """Breaks: the storage pair's sweep reads (slice 2a D3, D4) and the closed-bundle guarantee."""

    def setUp(self):
        super().setUp()
        self.rows = 0

    def snapshot_rows(self, relay_id, digest, has_bytes, count=1):
        """Fixture snapshots of one digest, each on its own path edge of the relay."""
        first = self.rows + 1
        self.rows += count
        numbers = range(first, self.rows + 1)
        self.conn.executemany(
            "INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling, target_path)"
            " VALUES (?, ?, ?, 'ref', 'path', 'fixture', 'fixture')",
            [("edge-%d" % n, relay_id, n) for n in numbers])
        self.conn.executemany(
            "INSERT INTO snapshots (id, handle, relay_id, edge_id, artifact, content_sha256, bytes_len,"
            " has_bytes) VALUES (?, ?, ?, ?, 'fixture', ?, 0, ?)",
            [("snapshot-%d" % n, n, relay_id, "edge-%d" % n, digest, has_bytes) for n in numbers])
        self.conn.commit()

    def statements(self, call):
        """The outcome of a call and the SQL statements it ran."""
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            return snapshot_outcome(call), seen
        finally:
            self.conn.set_trace_callback(None)

    def bundle(self, name, sender=PLANNER):
        relay = self.submit(draft(bundle=name), sender=sender)
        return self.relay(relay.relay_id)["bundle_id"]

    def test_evidence_pointed(self):
        pointed = queries.evidence_pointed  # read first: until it exists, the test errors here
        mine = self.submit(draft()).relay_id
        theirs = self.submit(draft(), sender=FOREIGN).relay_id
        stored, digest_only, foreign, absent = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
        self.snapshot_rows(mine, stored, 1)
        self.snapshot_rows(mine, digest_only, 0)
        self.snapshot_rows(theirs, foreign, 1)
        outcome = snapshot_outcome(lambda: pointed(
            self.conn, submit_tests.PROJECT, [stored, digest_only, foreign, absent, stored]))
        self.assertEqual(outcome, ("ok", frozenset({stored})))
        self.assertEqual(snapshot_outcome(lambda: pointed(self.conn, "q", [stored, foreign])),
                         ("ok", frozenset({foreign})))
        self.assertEqual(snapshot_outcome(lambda: pointed(self.conn, submit_tests.PROJECT, [])),
                         ("ok", frozenset()))
        many = ["%064x" % n for n in range(501)]
        for refused in (many, ["A" * 64], ["a" * 63], [stored, 7]):
            (kind, _), seen = self.statements(lambda: pointed(
                self.conn, submit_tests.PROJECT, refused))
            self.assertEqual((kind, seen), ("ValueError", []), refused[:2])

    def test_evidence_pointed_bounded(self):
        pointed = queries.evidence_pointed  # read first: until it exists, the test errors here
        mine = self.submit(draft()).relay_id
        target = "a" * 64
        candidates = [target] + ["%064x" % n for n in range(1, 500)]
        self.snapshot_rows(mine, target, 1)
        answers = []

        def call():
            answers.append(snapshot_outcome(lambda: pointed(
                self.conn, submit_tests.PROJECT, candidates)))
        lean = vm_steps(self.conn, call)
        self.conn.execute("DELETE FROM snapshots")
        self.conn.commit()
        # The digest-only rows come first, so a probe that cannot skip them visits every one.
        self.snapshot_rows(mine, target, 0, 5000)
        self.snapshot_rows(mine, target, 1)
        heavy = vm_steps(self.conn, call)
        self.assertEqual(answers, [("ok", frozenset({target}))] * 2)
        self.assertLessEqual(heavy, lean + 200, (lean, heavy))

    def test_bundle_closed_at(self):
        closed_at = queries.bundle_closed_at  # read first: until it exists, the test errors here
        opened, closed = self.bundle("open-one"), self.bundle("closed-one")
        theirs = self.bundle("theirs", sender=FOREIGN)
        self.conn.execute("UPDATE bundles SET closed_at = 77 WHERE id = ?", (closed,))
        self.conn.commit()
        outcome = snapshot_outcome(lambda: closed_at(
            self.conn, submit_tests.PROJECT, [opened, closed, opened]))
        self.assertEqual(outcome, ("ok", {opened: None, closed: 77}))
        for unknown, text in ((["no-such-bundle"], "1 bundle id names no bundle of this project"),
                              ([opened, theirs], "1 bundle id names no bundle of this project"),
                              ([theirs, "no-such-bundle", closed],
                               "2 bundle ids name no bundle of this project")):
            kind, error = snapshot_outcome(lambda: closed_at(
                self.conn, submit_tests.PROJECT, unknown))
            self.assertEqual((kind, str(error)), ("ValueError", text), unknown)
        for refused in (["bundle-%d" % n for n in range(501)], [""], [opened, None]):
            (kind, _), seen = self.statements(lambda: closed_at(
                self.conn, submit_tests.PROJECT, refused))
            self.assertEqual((kind, seen), ("ValueError", []), refused[:2])

    def test_new_readers_are_pure(self):
        pointed, closed_at = queries.evidence_pointed, queries.bundle_closed_at  # errors here until both exist
        relay = self.submit(draft(bundle="pure")).relay_id
        bundle = self.relay(relay)["bundle_id"]
        self.snapshot_rows(relay, "a" * 64, 1)
        for call in (lambda: pointed(self.conn, submit_tests.PROJECT, ["a" * 64]),
                     lambda: closed_at(self.conn, submit_tests.PROJECT, [bundle])):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("SELECT count(*) FROM relays").fetchone()
                changes = self.conn.total_changes
                outcome = snapshot_outcome(call)
                state = (outcome[0], self.conn.in_transaction, self.conn.total_changes)
            finally:
                self.conn.execute("ROLLBACK")
            self.assertEqual(state, ("ok", True, changes), outcome)

    def test_bundle_close_is_write_once(self):
        bundle = self.bundle("closing")
        update = "UPDATE bundles SET closed_at = ? WHERE id = ?"
        for value in (10, 10):  # setting it from NULL, then writing the same value
            outcome = snapshot_outcome(lambda: self.conn.execute(update, (value, bundle)))
            self.assertEqual(outcome[0], "ok", outcome)
            self.conn.commit()
        for value in (None, 11):  # clearing it, and changing it
            kind, error = snapshot_outcome(lambda: self.conn.execute(update, (value, bundle)))
            self.conn.rollback()
            self.assertEqual((kind, str(error) if kind != "ok" else None),
                             ("IntegrityError", "a closed bundle stays closed"), value)
        self.assertEqual(self.conn.execute("SELECT closed_at FROM bundles WHERE id = ?",
                                           (bundle,)).fetchone(), (10,))


class TestQueueReaders(Store):
    def test_operator_waiting_replaces_question_rule(self):
        # Breaks: the caller exemption is unioned rather than replacing QUESTION.
        start = self.clock
        work = self.submit(draft(to=IMPL.address, needs="act"))
        late = start + 24 * 60 * 60_000
        flags = queries.unopened_flags(self.conn, PLANNER.project_id, late,
                                       operator_waiting=frozenset({IMPL.seat_id}))
        self.assertEqual(flags, [])
        self.submit(draft(kind="QUESTION", to="operator", needs="answer"), sender=IMPL)
        flags = queries.unopened_flags(self.conn, PLANNER.project_id, late,
                                       operator_waiting=frozenset())
        self.assertEqual({f.rule for f in flags if f.seat_id == IMPL.seat_id},
                         {"idle", "unopened"})
        for bad in ([], set()):
            with self.assertRaises(ValueError):
                queries.unopened_flags(self.conn, PLANNER.project_id, late, operator_waiting=bad)


    def queue_row(self, *, conn=None, kind="QUESTION", sender=PLANNER, parent=None,
                  body=b"exact\x00body\n", created=None, to=None, cc=None):
        conn = self.conn if conn is None else conn
        handle = conn.execute("SELECT coalesce(max(handle),0)+1 FROM relays WHERE project_id=?",
                              (sender.project_id,)).fetchone()[0]
        rid = "00000000-0000-7000-8000-%012x" % (
            handle + (0 if sender.project_id == "p" else 1 << 32))
        at = conn.execute("SELECT coalesce(max(created_at),0)+1 FROM relays").fetchone()[0]
        if created is not None:
            at = created
        special = sender.seat_id if sender.seat_id in schema.SPECIALS else None
        conn.execute("INSERT INTO relays (id,handle,project_id,team_instance_id,kind,needs,"
                     "subject,parent_id,sender_seat_id,sender_special,sender_role,created_at,"
                     "body_sha256,meta_sha256,body_bytes,header_bytes) "
                     "VALUES (?,?,?,?,?,'act','queue',?,?,?,?,?,?,?, ?,10)",
                     (rid,handle,sender.project_id,sender.team_instance_id,kind,parent,
                      None if special else sender.seat_id,special,sender.role,at,
                      hashlib.sha256(body).hexdigest(),"a"*64,len(body)))
        conn.execute("INSERT INTO bodies VALUES (?,?)", (rid,body))
        for seat,role in ((to,'to'),(cc,'cc')):
            if seat:
                conn.execute("INSERT INTO recipients (relay_id,seat_id,role) VALUES (?,?,?)",
                             (rid,seat.seat_id,role))
        return submit.Admitted(rid,handle,at)

    def queue_session(self, seat=IMPL, generation=1, signed=9000, *, conn=None, ended=None):
        conn = self.conn if conn is None else conn
        conn.execute("INSERT INTO seat_sessions (id,seat_id,occupant_id,host,host_session_id,"
                     "generation,signed_in_at,last_seen_at,ended_at) "
                     "VALUES (?,?,?,'codex',?,?,?,?,?)",
                     ("session-"+seat.seat_id+str(generation),seat.seat_id,"o-"+seat.seat_id,
                      "host-"+str(generation),generation,signed,signed,ended))

    def reopen_queue_team(self, old=PLANNER, *, conn=None):
        conn = self.conn if conn is None else conn
        name = old.address.rsplit('.',1)[0]
        tid = "new-"+old.team_instance_id
        sid = "new-"+old.seat_id
        conn.execute("INSERT INTO team_instances (id,project_id,name,level,opened_at,opened_by) "
                     "VALUES (?,?,?,'pair',999,'operator')", (tid,old.project_id,name))
        conn.execute("INSERT INTO seats VALUES (?,?,?,'occupied')", (sid,tid,old.role))
        conn.execute("INSERT INTO seat_occupants (id,seat_id,key_id,key_verifier,issued_at) "
                     "VALUES (?,?,?,'v',999)",("o-"+sid,sid,"k-"+sid))
        return submit.SeatRef(sid,old.address,old.role,tid,old.project_id)

    def observed_queue_call(self, call):
        try:
            value = call()
        except Exception as error:
            value = error
        self.assertNotIsInstance(value, Exception, repr(value))
        return value

    def test_operator_questions(self):
        # Breaks: kind/project/page filters, answer-party/project/kind predicates or writes.
        q1 = self.queue_row(to=IMPL)
        q2 = self.queue_row(body=b"second\n")
        self.queue_row(kind="SITREP")
        self.queue_row(sender=FOREIGN)
        before = [self.conn.execute("SELECT * FROM "+t).fetchall()
                  for t in ('recipients','seat_activity')]
        changes = self.conn.total_changes
        page = queries.operator_questions(self.conn, PLANNER.project_id, 0, 500)
        self.assertEqual([r.relay_id for r in page], [q1.relay_id,q2.relay_id])
        self.assertEqual(page[0].body, b"exact\x00body\n")
        self.assertIs(page[0].answered, False)
        self.assertEqual([r.handle for r in queries.operator_questions(self.conn,'p',q1.handle,1)],
                         [q2.handle])
        self.assertEqual([self.conn.execute("SELECT * FROM "+t).fetchall()
                          for t in ('recipients','seat_activity')], before)
        self.assertEqual(self.conn.total_changes, changes)
        for after,limit in ((-1,1),(0,0),(0,501)):
            with self.assertRaises(ValueError):
                queries.operator_questions(self.conn,'p',after,limit)
        # Real admission and its derived mark are independently controlled.
        self.conn.commit()
        q = self.submit(draft(kind="QUESTION",to="operator",needs="answer"))
        self.submit(draft(parent="#%d"%q.handle,to=PLANNER.address),sender=OPERATOR)
        self.conn.execute("UPDATE recipients SET answered_by=NULL WHERE relay_id=?",(q.relay_id,))
        self.conn.commit()
        def answered(rid):
            return next(r.answered for r in queries.operator_questions(self.conn,'p',0,500)
                        if r.relay_id==rid)
        self.assertIs(answered(q.relay_id), True)
        audit = self.queue_row(sender=OPERATOR,kind="AUDIT",parent=q2.relay_id)
        self.assertIs(answered(q2.relay_id), True)
        clean = self.queue_row()
        self.queue_row(kind="SITREP",sender=PLANNER,parent=clean.relay_id)
        self.assertIs(answered(clean.relay_id), False)
        self.queue_row(kind="SITREP",sender=IMPL,parent=clean.relay_id)
        self.assertIs(answered(clean.relay_id), False)
        foreign_op = submit.SeatRef('operator','operator','operator',None,'q')
        self.queue_row(sender=foreign_op,kind="SITREP",parent=clean.relay_id)
        self.assertIs(answered(clean.relay_id), False)
        system = submit.SeatRef('system','system','system',PLANNER.team_instance_id,'p')
        self.queue_row(sender=system,kind="SITREP",parent=clean.relay_id)
        self.assertIs(answered(clean.relay_id), False)

    def test_operator_questions_sender_address(self):
        # Breaks: rendering closed/reopened or special senders as routable seat addresses.
        self.directory = submit.TableDirectory(self.conn, PLANNER.project_id)
        self.conn.execute("UPDATE team_instances SET name='team-a' WHERE id=?",
                          (PLANNER.team_instance_id,))
        asker = submit.SeatRef(PLANNER.seat_id,'team-a.planner','planner',PLANNER.team_instance_id,'p')
        q = self.queue_row(sender=asker)
        page = queries.operator_questions(self.conn,'p',0,500)
        self.assertEqual(page[0]._fields[-2:], ('answered','sender_address'))
        self.assertEqual(page[0].sender_address,'team-a.planner')
        self.assertEqual(page[0].sender_address,addresses.render(addresses.Address('team-a','planner')))
        op = self.queue_row(sender=OPERATOR)
        null = self.queue_row()
        self.conn.execute("UPDATE relays SET team_instance_id=NULL,sender_role=NULL WHERE id=?",
                          (null.relay_id,))
        system = submit.SeatRef('system','system','system',asker.team_instance_id,'p')
        sysrow = self.queue_row(sender=system)
        page = self.observed_queue_call(lambda: queries.operator_questions(self.conn,'p',0,500))
        self.assertEqual(len(page),4)
        self.assertEqual([r.sender_address for r in page],['team-a.planner',None,None,None])
        self.conn.execute("UPDATE team_instances SET closed_at=100 WHERE id=?",
                          (asker.team_instance_id,));self.conn.commit()
        self.assertIsNone(queries.operator_questions(self.conn,'p',0,1)[0].sender_address)
        self.refused('E-ADDRESS',draft(to='team-a.planner'),sender=OPERATOR)
        new = self.reopen_queue_team(asker);self.conn.commit()
        newq = self.queue_row(sender=new);self.conn.commit()
        page = queries.operator_questions(self.conn,'p',0,500)
        self.assertIsNone(page[0].sender_address)
        self.assertEqual(page[-1].sender_address,'team-a.planner')
        sent = self.submit(draft(to='team-a.planner'),sender=OPERATOR)
        self.assertEqual(self.conn.execute("SELECT seat_id FROM recipients WHERE relay_id=?",
                                           (sent.relay_id,)).fetchall(),[(new.seat_id,)])

    def test_operator_admission(self):
        # Breaks: replay ignores party or bytes, depends on team state, or filters project.
        data = draft(to='operator')
        self.assertIsNone(queries.operator_admission(self.conn,'K',data))
        self.submit(data,submission_key='seat-only')
        self.assertIsNone(queries.operator_admission(self.conn,'seat-only',data))
        first = self.submit(data,sender=OPERATOR,submission_key='K')
        replay = self.submit(data,sender=OPERATOR,submission_key='K')
        self.assertEqual(queries.operator_admission(self.conn,'K',data),replay)
        for call in (lambda: queries.operator_admission(self.conn,'K',data+b'x'),
                     lambda: self.submit(data+b'x',sender=OPERATOR,submission_key='K')):
            with self.assertRaises(errors.EngineError) as caught:call()
            self.assertEqual(caught.exception.code,'E-SUBMISSION-KEY-REUSED')
        q = self.submit(draft(kind='QUESTION',to='operator',needs='answer'))
        answer = draft(parent='#%d'%q.handle,to=PLANNER.address)
        admitted = self.submit(answer,sender=OPERATOR,submission_key='answer')
        self.conn.execute('UPDATE team_instances SET closed_at=100 WHERE id=?',
                          (PLANNER.team_instance_id,))
        self.reopen_queue_team();self.conn.commit()
        self.assertEqual(queries.operator_admission(self.conn,'answer',answer),admitted)
        foreign_op = submit.SeatRef('operator','operator','operator',None,'q')
        self.assertEqual(self.submit(answer,sender=foreign_op,submission_key='answer'),admitted)
        self.assertEqual(queries.operator_admission(self.conn,'answer',answer),admitted)
        for key,body in (('',data),('a\x00b',data),('é'*129,data),('K','text')):
            with self.assertRaises(ValueError):queries.operator_admission(self.conn,key,body)
        self.conn.execute('BEGIN');changes=self.conn.total_changes
        self.assertEqual(queries.operator_admission(self.conn,'K',data),first)
        self.assertTrue(self.conn.in_transaction);self.assertEqual(self.conn.total_changes,changes)
        self.conn.rollback()

    def test_doorbell_off(self):
        # Breaks: session/role/opened filters, age, oldest row, closed-team display.
        self.conn.execute("UPDATE team_instances SET name='team-a' WHERE id=?",
                          (PLANNER.team_instance_id,))
        seat=submit.SeatRef(PLANNER.seat_id,'team-a.planner','planner',PLANNER.team_instance_id,'p')
        self.queue_session(seat,generation=1,signed=9000,ended=9999)
        self.queue_session(seat,generation=2,signed=9999)
        old=self.queue_row(kind='SITREP',created=100,to=seat)
        young=self.queue_row(kind='SITREP',created=150,to=seat)
        self.assertEqual(queries.doorbell_off(self.conn,'p',199,100),[])
        rows=queries.doorbell_off(self.conn,'p',200,100)
        self.assertEqual([tuple(r[:5]) for r in rows],[(seat.seat_id,2,old.relay_id,old.handle,100)])
        self.assertEqual(rows[0]._fields[-1],'seat_address')
        self.assertEqual(rows[0].seat_address,'team-a.planner')
        self.assertEqual(rows[0].seat_address,addresses.render(addresses.Address('team-a','planner')))
        with self.assertRaises(ValueError):queries.doorbell_off(self.conn,'p',200,0)
        self.conn.execute('UPDATE recipients SET opened_at=199 WHERE relay_id=?',(old.relay_id,))
        self.assertEqual(queries.doorbell_off(self.conn,'p',200,100),[])
        self.assertEqual(queries.doorbell_off(self.conn,'p',250,100)[0].relay_id,young.relay_id)
        self.conn.execute('UPDATE recipients SET delivered_at=250 WHERE relay_id=?',(young.relay_id,))
        self.assertEqual(queries.doorbell_off(self.conn,'p',10000,100),[])
        self.conn.execute('UPDATE recipients SET opened_at=NULL WHERE relay_id=?',(old.relay_id,))
        self.assertEqual(queries.doorbell_off(self.conn,'p',200,100)[0].relay_id,old.relay_id)
        self.conn.execute('UPDATE seats SET state=\'stood-down\' WHERE id=?',(seat.seat_id,))
        self.assertEqual(queries.doorbell_off(self.conn,'p',200,100)[0].seat_address,'team-a.planner')
        self.conn.execute('UPDATE team_instances SET closed_at=200 WHERE id=?',(seat.team_instance_id,))
        self.assertIsNone(queries.doorbell_off(self.conn,'p',200,100)[0].seat_address)
        new=self.reopen_queue_team(seat);self.queue_session(new)
        self.queue_row(kind='SITREP',created=160,to=new)
        rows={r.seat_id:r for r in queries.doorbell_off(self.conn,'p',300,100)}
        self.assertIsNone(rows[seat.seat_id].seat_address)
        self.assertEqual(rows[new.seat_id].seat_address,'team-a.planner')
        # Exclusion controls are otherwise old enough and signed in where applicable.
        self.conn.execute('UPDATE recipients SET delivered_at=300')
        self.queue_row(kind='SITREP',created=170,cc=new)
        self.queue_row(kind='SITREP',created=180,to=OTHER)  # no open session
        self.queue_row(kind='SITREP',created=190,to=new,sender=FOREIGN)  # another project
        self.assertEqual(queries.doorbell_off(self.conn,'p',10000,100),[])
        self.conn.execute('UPDATE seat_sessions SET ended_at=10001')
        self.conn.execute('UPDATE recipients SET delivered_at=NULL WHERE relay_id=?',(old.relay_id,))
        self.assertEqual(queries.doorbell_off(self.conn,'p',10000,100),[])

    def test_doorbell_off_bounded(self):
        # Breaks: removing or weakening the partial index makes history cost grow.
        self.queue_session()
        live=self.queue_row(kind='SITREP',created=10,to=IMPL)
        self.assertEqual(len(queries.doorbell_off(self.conn,'p',100,20)),1)
        base=vm_steps(self.conn,lambda: queries.doorbell_off(self.conn,'p',100,20))
        for n in range(5000):
            row=self.queue_row(kind='SITREP',created=1000+n,to=IMPL)
            column='delivered_at' if n<2500 else 'opened_at'
            self.conn.execute('UPDATE recipients SET '+column+'=1 WHERE relay_id=?',(row.relay_id,))
        # A fresh prepare prevents a stale query plan from masking a removed index.
        self.conn.commit()
        cost=vm_steps(self.conn,lambda: queries.doorbell_off(self.conn,'p',100,20))
        self.assertLessEqual(cost,base+200,(base,cost))
        self.assertEqual([r.relay_id for r in queries.doorbell_off(self.conn,'p',100,20)],
                         [live.relay_id])

    def test_queue_readers_are_pure(self):
        # Breaks: writes/transaction control, or fields read on a fresh snapshot.
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'queue.sqlite3')
            c=sqlite3.connect(path);self.addCleanup(c.close)
            c.execute('PRAGMA journal_mode=WAL');self.conn.backup(c)
            w=sqlite3.connect(path);self.addCleanup(w.close)
            w.execute('PRAGMA foreign_keys=ON')
            self.queue_session(conn=w)
            q=self.queue_row(conn=w,to=IMPL,created=10);w.commit()
            queries.operator_questions(c,'p',0,500)  # missing name remains the C1 error
            c.execute('BEGIN');c.execute('SELECT count(*) FROM relays').fetchone()
            changes=c.total_changes
            fresh=self.queue_row(conn=w,to=OTHER,created=20)
            self.queue_session(OTHER,conn=w);w.commit()
            page=queries.operator_questions(c,'p',0,500)
            bell=queries.doorbell_off(c,'p',10000000,1)
            flags=queries.unopened_flags(c,'p',10000000,unopened_ms=1,idle_ms=1,
                                         operator_waiting=frozenset())
            self.assertEqual([r.relay_id for r in page],[q.relay_id])
            self.assertEqual([r.relay_id for r in bell],[q.relay_id])
            self.assertEqual({r.relay_id for r in flags},{q.relay_id})
            self.assertTrue(c.in_transaction);self.assertEqual(c.total_changes,changes)
            c.rollback()
            self.assertEqual(len(queries.operator_questions(c,'p',0,500)),2)
            # The newly signed-in recipient is visible only after ending the snapshot.
            self.assertEqual({r.relay_id for r in queries.doorbell_off(c,'p',10000000,1)},
                             {q.relay_id,fresh.relay_id})
            self.assertIn(fresh.relay_id,{f.relay_id for f in queries.unopened_flags(
                c,'p',10000000,unopened_ms=1,idle_ms=1,operator_waiting=frozenset())})
            for change in ('answer','asking-team','doorbell-team'):
                c.execute('BEGIN');c.execute('SELECT count(*) FROM relays').fetchone()
                before=c.total_changes
                if change=='answer':
                    self.queue_row(conn=w,sender=OPERATOR,kind='SITREP',parent=q.relay_id,created=30)
                elif change=='asking-team':
                    w.execute('UPDATE team_instances SET closed_at=100 WHERE id=?',
                              (PLANNER.team_instance_id,))
                else:
                    # Recipient shares the Engine instance; reopen its observed snapshot
                    # for this independent field control before pinning the next snapshot.
                    c.rollback();w.execute('UPDATE team_instances SET closed_at=NULL WHERE id=?',
                                           (IMPL.team_instance_id,));w.commit()
                    c.execute('BEGIN');c.execute('SELECT count(*) FROM relays').fetchone()
                    w.execute('UPDATE team_instances SET closed_at=101 WHERE id=?',
                              (IMPL.team_instance_id,))
                w.commit()
                page=self.observed_queue_call(lambda: queries.operator_questions(c,'p',0,500))
                bell=self.observed_queue_call(lambda: queries.doorbell_off(c,'p',10000000,1))
                if change=='answer':self.assertIs(page[0].answered,False)
                elif change=='asking-team':self.assertEqual(page[0].sender_address,PLANNER.address)
                else:self.assertEqual(bell[0].seat_address,IMPL.address)
                self.assertTrue(c.in_transaction);self.assertEqual(c.total_changes,before)
                c.rollback()
                if change=='answer':self.assertIs(queries.operator_questions(c,'p',0,500)[0].answered,True)
                elif change=='asking-team':self.assertIsNone(queries.operator_questions(c,'p',0,500)[0].sender_address)
                else:self.assertIsNone(queries.doorbell_off(c,'p',10000000,1)[0].seat_address)
            w.close();c.close()


class TestProjectSettings(Store):
    def setting_outcome(self, action):
        try:
            return ("ok", action())
        except errors.EngineError as exc:
            return (exc.code, exc)
        except Exception as exc:
            return (type(exc).__name__, exc)

    def setting_rows(self):
        return (self.conn.execute("SELECT * FROM project_settings ORDER BY project_id,key").fetchall(),
                self.conn.execute("SELECT * FROM settings_history ORDER BY project_id,revision").fetchall())

    def test_settings_defaults(self):
        settings = queries.project_settings(self.conn, PLANNER.project_id)
        self.assertEqual(settings.values, {"jev_record": False, "jev_act": False,
                                           "doorbell_off_minutes": 15})
        self.assertIs(type(settings.values["jev_record"]), bool)
        self.assertIs(type(settings.values["doorbell_off_minutes"]), int)
        self.assertEqual(settings.revision, 0)
        self.assertEqual(queries.settings_history(self.conn, PLANNER.project_id, 500), [])
        self.assertEqual(self.setting_rows(), ([], []))
        for action in (lambda: queries.project_settings(self.conn, "absent"),
                       lambda: queries.settings_history(self.conn, "absent", 1)):
            self.assertEqual(self.setting_outcome(action)[0], "ValueError")
        for limit in (0, 501, True, "1"):
            self.assertEqual(self.setting_outcome(lambda: queries.settings_history(
                self.conn, PLANNER.project_id, limit))[0], "ValueError")

    def test_setting_set_and_history(self):
        step = queries.set_project_setting_step
        self.conn.execute("BEGIN")
        def change(key, value, at):
            return self.setting_outcome(lambda: step(self.conn, PLANNER.project_id, key, value,
                principal="operator", now_ms=at))
        self.assertEqual(change("doorbell_off_minutes", 30, 100), ("ok", 1))
        rows = self.setting_rows()
        self.assertEqual(rows, ([(PLANNER.project_id, "doorbell_off_minutes", "30")],
            [(PLANNER.project_id, 1, "doorbell_off_minutes", None, "30", "operator", 100)]))
        self.assertEqual(change("doorbell_off_minutes", 30, 101), ("ok", 1))
        self.assertEqual(change("jev_record", False, 102), ("ok", 1))
        self.assertEqual(self.setting_rows(), rows)
        self.assertEqual(change("jev_record", True, 103), ("ok", 2))
        self.assertEqual(change("jev_act", True, 104), ("ok", 3))
        settings = queries.project_settings(self.conn, PLANNER.project_id)
        self.assertEqual((settings.values, settings.revision),
                         ({"jev_record": True, "jev_act": True, "doorbell_off_minutes": 30}, 3))
        history = queries.settings_history(self.conn, PLANNER.project_id, 500)
        self.assertEqual([(h.revision, h.key, h.old, h.new, h.by_principal, h.at_ms) for h in history],
                         [(3, "jev_act", None, True, "operator", 104),
                          (2, "jev_record", None, True, "operator", 103),
                          (1, "doorbell_off_minutes", None, 30, "operator", 100)])
        self.assertIs(type(history[0].new), bool)
        self.assertIs(type(history[2].new), int)
        self.assertEqual(queries.settings_history(self.conn, PLANNER.project_id, 1), history[:1])
        self.assertEqual(queries.project_settings(self.conn, FOREIGN.project_id).revision, 0)
        self.assertEqual(queries.settings_history(self.conn, FOREIGN.project_id, 1), [])

    def test_setting_refusals(self):
        step = queries.set_project_setting_step
        self.conn.execute("BEGIN")
        cases = [("jev_record", True, PLANNER.address, "E-SETTING-OPERATOR-ONLY"),
                 ("unknown", True, "operator", "E-SETTING-UNKNOWN"),
                 *[("doorbell_off_minutes", v, "operator", "E-SETTING-VALUE")
                   for v in (0, 1441, "15", True)],
                 ("jev_record", 1, "operator", "E-SETTING-VALUE"),
                 ("jev_act", True, "operator", "E-JEV-ACT-NEEDS-RECORD")]
        for key, value, principal, code in cases:
            with self.subTest(key=key, value=value, principal=principal):
                before = self.setting_rows()
                revision = queries.project_settings(self.conn, PLANNER.project_id).revision
                outcome = self.setting_outcome(lambda: step(self.conn, PLANNER.project_id,
                    key, value, principal=principal, now_ms=100))
                self.assertEqual(outcome[0], code, outcome)
                self.assertEqual(self.setting_rows(), before)
                self.assertEqual(queries.project_settings(self.conn, PLANNER.project_id).revision,
                                 revision)
        for key in ("jev_record", "jev_act"):
            self.assertEqual(self.setting_outcome(lambda: step(self.conn, PLANNER.project_id,
                key, True, principal="operator", now_ms=101))[0], "ok")
        before = self.setting_rows()
        outcome = self.setting_outcome(lambda: step(self.conn, PLANNER.project_id,
            "jev_record", False, principal="operator", now_ms=102))
        self.assertEqual(outcome[0], "E-JEV-ACT-NEEDS-RECORD", outcome)
        self.assertEqual(self.setting_rows(), before)
        self.assertEqual(queries.project_settings(self.conn, PLANNER.project_id).revision, 2)
        self.assertEqual(self.setting_outcome(lambda: step(self.conn, "absent", "jev_record",
            True, principal="operator", now_ms=102))[0], "ValueError")
        self.assertEqual(self.setting_rows(), before)

    def test_setting_step_needs_transaction(self):
        step = queries.set_project_setting_step
        self.conn.commit()
        before = self.setting_rows()
        outcome = self.setting_outcome(lambda: step(self.conn, PLANNER.project_id,
            "doorbell_off_minutes", 30, principal="operator", now_ms=100))
        self.assertEqual(outcome[0], "ValueError", outcome)
        self.assertEqual(self.setting_rows(), before)
        self.conn.execute("BEGIN")
        outcome = self.setting_outcome(lambda: step(self.conn, PLANNER.project_id,
            "doorbell_off_minutes", 30, principal="operator", now_ms=101))
        self.assertEqual(outcome, ("ok", 1))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.setting_rows(), before)
        self.assertEqual(queries.project_settings(self.conn, PLANNER.project_id).revision, 0)

    def test_settings_schema(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"project_settings", "settings_history"} <= tables)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO project_settings VALUES (?, 'unknown', 'true')",
                              (PLANNER.project_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO settings_history VALUES (?,1,'jev_record',NULL,'true',?,1)",
                              (PLANNER.project_id, PLANNER.address))
        self.conn.execute("INSERT INTO settings_history VALUES (?,1,'jev_record',NULL,'true','operator',1)",
                          (PLANNER.project_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO settings_history VALUES (?,1,'jev_act',NULL,'true','operator',2)",
                              (PLANNER.project_id,))
        for table in ("project_settings", "settings_history"):
            self.assertEqual(schema.TABLE_OWNERS[table], "engine")
            self.assertEqual(next(row[-1] for row in self.conn.execute("PRAGMA table_list")
                                  if row[1] == table), 1)


class TestGitIsolation(unittest.TestCase):
    def test_store_tests_ignore_ambient_git_names(self):
        # Breaks: a Store consumer reaches Git through an unisolated fixture.
        from relay_engine.tests import test_queries

        loader = unittest.TestLoader()
        selected = []
        for module in (test_queries, submit_tests):
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if cls.__module__ == module.__name__ and issubclass(cls, Store):
                    selected.extend(cls(name) for name in loader.getTestCaseNames(cls))
        self.assertTrue(selected)
        for case in selected:
            with self.subTest(test=case.id()), mock.patch.dict(os.environ, {"GIT_PAGER": "cat"}):
                result = unittest.TestResult()
                case.run(result)
                traces = "\n".join(text for _, text in result.errors + result.failures)
                self.assertFalse(result.errors + result.failures, traces)
                self.assertFalse(result.skipped, result.skipped)


class TestQueryCost(unittest.TestCase):
    """DD § 15 item 6: flat cost at 1,206 and 50,000 relays.  Work is SQLite VM
    steps, which grow with the rows a query visits (PR-3 fold R14: a floored
    timing ratio cannot fail); timings are recorded beside it."""

    def store(self, count):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        schema.init_v3(conn)
        conn.executescript("""
            INSERT INTO projects (id, name, created_at) VALUES ('p', 'p', 1);
            INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)
                VALUES ('t', 'p', 't', 'pair', 1, 'operator');
            INSERT INTO seats (id, team_instance_id, role, state) VALUES ('s', 't', 'planner',
                'occupied');
            INSERT INTO seats (id, team_instance_id, role, state) VALUES ('s2', 't',
                'implementer', 'occupied');
            INSERT INTO bundles (id, project_id, name, opened_by_seat_id) VALUES ('b', 'p', 'b',
                's');
        """)
        sha = "a" * 64
        rows = []
        for n in range(1, count + 1):  # the last 300 relays form one chain and are open work
            chained = n > count - 300 + 1
            recent = n > count - 300
            rows.append(("r%07d" % n, n, "p", "b" if recent else None,
                         "act" if recent else "none", "s", n, sha, sha,
                         "r%07d" % (n - 1) if chained else None))
        conn.executemany(
            "INSERT INTO relays (id, handle, project_id, bundle_id, kind, verdict, needs, subject,"
            " sender_seat_id, created_at, body_sha256, meta_sha256, body_bytes, header_bytes,"
            " parent_id) VALUES (?, ?, ?, ?, 'SITREP', NULL, ?, ?, 's', ?, ?, ?, 0, 10, ?)", rows)
        conn.executemany(  # every earlier relay was opened and answered
            "INSERT INTO recipients (relay_id, seat_id, role, opened_at, answered_by)"
            " VALUES (?, 's2', 'to', ?, ?)",
            [("r%07d" % n, None if n > count - 300 else n,
              None if n > count - 300 else "r%07d" % n) for n in range(1, count + 1)])
        conn.executemany(
            "INSERT INTO findings (id, handle, project_id, bundle_id, raised_in, raiser_seat_id,"
            " severity, summary, status) VALUES (?, ?, 'p', ?, ?, 's', 'blocking', 's', 'open')",
            [("f%07d" % n, n, "b" if n > count - 300 else None, "r%07d" % n)
             for n in range(1, count + 1, 10)])
        conn.commit()
        return conn, "r%07d" % count

    def timed(self, call):
        best = float("inf")
        for _ in range(20):
            start = time.perf_counter()
            call()
            best = min(best, time.perf_counter() - start)
        return best * 1000

    def test_query_cost_flat(self):
        record = {}
        for count in (1206, 50000):
            conn, leaf = self.store(count)
            calls = {"lineage": lambda: queries.lineage(conn, leaf),
                     "blocking": lambda: queries.blocking_findings(conn, "b"),
                     "flags": lambda: queries.unopened_flags(conn, "p", 10 ** 12)}
            record[count] = {name: {"steps": vm_steps(conn, call), "ms": self.timed(call),
                                    "rows": len(call())}
                             for name, call in calls.items()}
        print("query cost (VM steps; min of 20 ms):", json.dumps(record, sort_keys=True))
        for name in ("lineage", "blocking", "flags"):
            small, large = (record[n][name] for n in (1206, 50000))
            self.assertLess(large["steps"], 2 * small["steps"], record)
            self.assertLess(large["ms"], 10.0, record)
            self.assertEqual(small["rows"], large["rows"], record)
        self.assertEqual(record[50000]["lineage"]["rows"], 256)
        self.assertEqual(record[50000]["flags"]["rows"], 301)  # 300 unopened, 1 idle (newest)


def snapshot_outcome(action):
    try:
        return ("ok", action())
    except Exception as exc:
        return (getattr(exc, "code", type(exc).__name__), exc)


class TestLockedSnapshots(submit_tests.Snapshots):
    def locked(self, ref, project="p"):
        return queries.locked_snapshot(self.conn, ref[5:], project_id=project)

    def test_locked_snapshot(self):
        ref = self.artifact("a", b"x")
        self.assertIsNone(self.locked(ref))
        first = self.request(ref)
        self.assertIsNone(self.locked(ref))
        self.approve(first, ref)
        second = self.request(ref)
        reply = self.approve(second, ref)
        self.request(ref)
        row = self.locked(ref)
        self.assertIsNotNone(row)
        self.assertEqual((row.relay_id, row.locked_by), (second.relay_id, reply.relay_id))
        self.assertTrue(row.has_bytes)

    def test_locked_snapshot_latest_lock_wins(self):
        ref = self.artifact("a", b"x")
        a = self.request(ref)
        self.artifact("a", b"y")
        b = self.request(ref)
        self.approve(b, ref)
        self.artifact("a", b"x")
        reply = self.approve(a, ref)
        row = self.locked(ref)
        self.assertIsNotNone(row)
        self.assertEqual((row.relay_id, row.locked_by), (a.relay_id, reply.relay_id))

    def test_locked_snapshot_project_scoped(self):
        ref = self.artifact("a", b"x")
        request = submit.submit(self.conn, self.ctx(FOREIGN, git_state=None), draft(kind="PLAN",
            needs="review", to=FOREIGN.address, refs=(ref,)))
        reply = submit.submit(self.conn, self.ctx(FOREIGN, git_state=None), draft(kind="PLAN-REVIEW",
            verdict="approve", parent="#" + str(request.handle), to=FOREIGN.address, refs=(ref,)))
        self.assertIsNone(self.locked(ref))
        self.assertEqual(self.locked(ref, "q").locked_by, reply.relay_id)
        p = self.request(ref)
        r = self.approve(p, ref)
        self.assertEqual(self.locked(ref).locked_by, r.relay_id)


class TestSnapshotCopies(submit_tests.Snapshots):
    def snapshots_pair(self, first=b"one\n", second=b"two\n"):
        ref = self.artifact("d", first)
        self.request(ref)
        self.artifact("d", second)
        self.request(ref)
        return ref

    def test_second_snapshot_and_diff(self):
        self.snapshots_pair()
        self.assertEqual(queries.snapshot_diff(self.conn, 1, 2, self.store,
            project_id=submit_tests.PROJECT), "".join(difflib.unified_diff(["one\n"], ["two\n"],
            fromfile="snapshot 1", tofile="snapshot 2")))
        self.request(self.artifact("d", b"\xff"))
        result = snapshot_outcome(lambda: queries.snapshot_diff(self.conn, 1, 3, self.store,
            project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "E-SNAPSHOT-NOT-TEXT")
        self.request(self.artifact("other", b"other"))
        result = snapshot_outcome(lambda: queries.snapshot_diff(self.conn, 1, 4, self.store,
            project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "ValueError", result)

    def test_clean_copy_exact(self):
        self.snapshots_pair(first=b"bytes\x00\xff")
        out = self.folder / "copy"
        queries.write_snapshot(self.conn, 1, out, self.store, project_id=submit_tests.PROJECT)
        self.assertEqual(out.read_bytes(), b"bytes\x00\xff")
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 2, out, self.store,
            project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "E-OUTPUT-EXISTS")
        self.assertEqual(out.read_bytes(), b"bytes\x00\xff")
        queries.write_snapshot(self.conn, 2, out, self.store, True, project_id=submit_tests.PROJECT)
        self.assertEqual(out.read_bytes(), b"two\n")
        link = self.folder / "link"
        link.symlink_to(out)
        for overwrite in (False, True):
            result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, link, self.store,
                overwrite, project_id=submit_tests.PROJECT))
            if overwrite:
                self.assertEqual(result[0], "ok", result)
                self.assertFalse(link.is_symlink())
                self.assertEqual(link.read_bytes(), b"bytes\x00\xff")
            else:
                self.assertEqual(result[0], "E-OUTPUT-EXISTS", result)
                self.assertTrue(link.is_symlink())
            self.assertEqual(out.read_bytes(), b"two\n")

    def test_large_copy_no_bytes(self):
        self.request(self.artifact("d", b"x" * (submit_tests.MIB + 1)))
        for action in (lambda: queries.write_snapshot(self.conn, 1, self.folder / "copy", self.store,
            project_id=submit_tests.PROJECT), lambda: queries.snapshot_diff(self.conn, 1, 1, self.store,
            project_id=submit_tests.PROJECT)):
            result = snapshot_outcome(action)
            self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES")
            self.assertIn("the file was over 1 MiB when the request was submitted", result[1].cause)

    def test_no_store_copy_no_bytes(self):
        self.store = None
        self.request(self.artifact("d", b"one"))
        for action in (lambda: queries.write_snapshot(self.conn, 1, self.folder / "copy", None,
            project_id=submit_tests.PROJECT), lambda: queries.snapshot_diff(self.conn, 1, 1, None,
            project_id=submit_tests.PROJECT)):
            result = snapshot_outcome(action)
            self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES")
            self.assertIn("no evidence store was configured when the request was submitted", result[
                1].cause)
        self.request(self.artifact("exact", b"x" * submit_tests.MIB))
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 2,
            self.folder / "exact-copy", None, project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES", result)
        self.assertIn("no evidence store was configured", result[1].cause)

    def test_unknown_handle(self):
        write = queries.write_snapshot
        diff = queries.snapshot_diff
        for action in (lambda: write(self.conn, 99, self.folder / "copy", self.store,
            project_id=submit_tests.PROJECT), lambda: diff(self.conn, 99, 100, self.store,
            project_id=submit_tests.PROJECT)):
            self.assertEqual(snapshot_outcome(action)[0], "KeyError")

    def test_store_bytes_typed(self):
        self.snapshots_pair()
        out = self.folder / "typed-copy"
        actions = (lambda store: queries.write_snapshot(self.conn, 1, out, store, True,
            project_id=submit_tests.PROJECT), lambda store: queries.snapshot_diff(self.conn, 1, 2, store,
            project_id=submit_tests.PROJECT))
        cases = [(None, "no evidence store was given to read its bytes"), (submit_tests.FakeStore(),
            "the evidence store does not hold its bytes")]
        for store, reason in cases:
            for action in actions:
                result = snapshot_outcome(lambda: action(store))
                self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES", result)
                self.assertIn(reason, result[1].cause)
                self.assertIsNone(result[1].__cause__)
                self.assertIsNone(result[1].__context__)
                self.assertFalse(out.exists())
        marker = OSError("fixture store unavailable")
        with mock.patch.object(self.store, "get", side_effect=marker):
            for existing in (False, True):
                if existing:
                    out.write_bytes(b"unchanged")
                for action in actions:
                    result = snapshot_outcome(lambda: action(self.store))
                    self.assertIs(result[1], marker, result)
                    if existing:
                        self.assertEqual(out.read_bytes(), b"unchanged")
                    else:
                        self.assertFalse(out.exists())
        out.unlink()
        with mock.patch.object(self.store, "get", return_value=b"wrong"):
            for action in actions:
                result = snapshot_outcome(lambda: action(self.store))
                self.assertEqual(result[0], "ValueError", result)
                self.assertIn("snapshot 1", str(result[1]))
                self.assertFalse(out.exists())

    def test_store_unavailable_typed(self):
        self.snapshots_pair()
        out = self.folder / "unavailable-copy"
        texts = {"missing": "the evidence store reports its bytes missing",
                 "corrupt": "the evidence store reports its bytes corrupt",
                 "unsupported-codec": "the evidence store cannot decode its bytes"}
        actions = (lambda: queries.write_snapshot(self.conn, 1, out, self.store,
            project_id=submit_tests.PROJECT), lambda: queries.snapshot_diff(self.conn, 1, 2, self.store,
            project_id=submit_tests.PROJECT))
        for literal, text in texts.items():
            unavailable = submit.EvidenceUnavailable(literal)
            self.assertEqual(unavailable.reason, literal)
            with mock.patch.object(self.store, "get", side_effect=unavailable):
                for action in actions:
                    result = snapshot_outcome(action)
                    self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES", result)
                    self.assertIn(text, result[1].cause)
                    self.assertIsNone(result[1].__cause__)
                    self.assertIsNone(result[1].__context__)
                    self.assertFalse(out.exists())
        for other in ("other", "", None):
            with self.assertRaises(ValueError, msg=repr(other)):
                submit.EvidenceUnavailable(other)

    def test_overwrite_replaces_entry(self):
        self.snapshots_pair()
        target = self.folder / "target"
        target.write_bytes(b"keep target")
        other = self.folder / "other-link"
        other.write_bytes(b"keep hard link")
        for kind in ("fifo", "symlink", "hardlink"):
            out = self.folder / kind
            if kind == "fifo":
                os.mkfifo(out)
            elif kind == "symlink":
                out.symlink_to(target)
            else:
                os.link(other, out)
            if kind == "fifo":
                reader = os.open(out, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    result = snapshot_outcome(lambda: queries.write_snapshot(
                        self.conn, 1, out, self.store, True, project_id=submit_tests.PROJECT))
                    self.assertEqual(result[0], "ok", result)
                    self.assertEqual(os.read(reader, 100), b"")
                finally:
                    os.close(reader)
            else:
                pid = os.fork()
                if pid == 0:
                    try:
                        queries.write_snapshot(self.conn, 1, out, self.store, True,
                            project_id=submit_tests.PROJECT)
                    except BaseException:
                        os._exit(1)
                    os._exit(0)
                status = None
                deadline = time.monotonic() + 2
                try:
                    while time.monotonic() < deadline:
                        done, value = os.waitpid(pid, os.WNOHANG)
                        if done:
                            status = value
                            break
                        time.sleep(0.01)
                finally:
                    if status is None:
                        os.kill(pid, 9)
                        os.waitpid(pid, 0)
                self.assertIsNotNone(status, "overwrite blocked on " + kind)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, kind)
            self.assertFalse(out.is_symlink())
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_bytes(), b"one\n")
            self.assertEqual(target.read_bytes(), b"keep target")
            self.assertEqual(other.read_bytes(), b"keep hard link")
        directory = self.folder / "directory"
        directory.mkdir()
        before = set(self.folder.iterdir())
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, directory, self.store,
            True, project_id=submit_tests.PROJECT))
        self.assertIsInstance(result[1], OSError, result)
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(set(self.folder.iterdir()), before)

    def test_overwrite_write_failure_keeps_entry(self):
        self.snapshots_pair()
        out = self.folder / "old-entry"
        out.write_bytes(b"old bytes")
        before = set(self.folder.iterdir())
        for operation in ("write", "fsync"):
            marker = OSError("fixture " + operation)
            with mock.patch.object(queries.os, operation, side_effect=marker):
                result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, out, self.store,
                    True, project_id=submit_tests.PROJECT))
            self.assertIs(result[1], marker, result)
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_bytes(), b"old bytes")
            self.assertEqual(set(self.folder.iterdir()), before)

    def test_create_write_failure_removes_partial(self):
        self.snapshots_pair()
        out = self.folder / "partial"
        before = set(self.folder.iterdir())
        marker = OSError("fixture partial write")
        real_write = os.write

        def partial(fd, data):
            real_write(fd, data[:1])
            raise marker
        with mock.patch.object(queries.os, "write", side_effect=partial):
            result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, out, self.store,
                project_id=submit_tests.PROJECT))
        self.assertIs(result[1], marker, result)
        self.assertFalse(out.exists())
        self.assertEqual(set(self.folder.iterdir()), before)
        out.write_bytes(b"not ours")
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, out, self.store,
            project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "E-OUTPUT-EXISTS", result)
        self.assertEqual(out.read_bytes(), b"not ours")

    def test_create_spares_concurrent_overwrite(self):
        self.snapshots_pair()
        for operation in ("write", "fsync"):
            out = self.folder / ("race-" + operation)
            before = set(self.folder.iterdir())
            marker = OSError("fixture interleaved write")
            real_operation = getattr(os, operation)
            triggered = False

            def interleave(*args):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    queries.write_snapshot(self.conn, 2, out, self.store, True,
                        project_id=submit_tests.PROJECT)
                    if operation == "write":
                        raise marker
                return real_operation(*args)

            with mock.patch.object(queries.os, operation, side_effect=interleave):
                result = snapshot_outcome(lambda: queries.write_snapshot(
                    self.conn, 1, out, self.store, project_id=submit_tests.PROJECT))
            if operation == "write":
                self.assertIs(result[1], marker, result)
            else:
                self.assertEqual(result[0], "E-OUTPUT-EXISTS", result)
            self.assertTrue(out.exists(), result)
            self.assertEqual(out.read_bytes(), b"two\n")
            self.assertEqual(set(self.folder.iterdir()), before | {out})

    def test_copy_modes(self):
        self.snapshots_pair()
        previous = os.umask(0o022)
        try:
            out = self.folder / "modes"
            queries.write_snapshot(self.conn, 1, out, self.store,
                project_id=submit_tests.PROJECT)
            self.assertEqual(out.stat().st_mode & 0o7777, 0o644)
            out.chmod(0o640)
            queries.write_snapshot(self.conn, 2, out, self.store, True,
                project_id=submit_tests.PROJECT)
            self.assertEqual(out.stat().st_mode & 0o7777, 0o640)
            target = self.folder / "mode-target"
            target.write_bytes(b"keep")
            target.chmod(0o600)
            link = self.folder / "mode-link"
            link.symlink_to(target)
            queries.write_snapshot(self.conn, 1, link, self.store, True,
                project_id=submit_tests.PROJECT)
            self.assertEqual(link.stat().st_mode & 0o7777, 0o644)
            self.assertEqual(target.stat().st_mode & 0o7777, 0o600)
            out.chmod(0o4750)
            self.assertEqual(out.stat().st_mode & 0o7777, 0o4750,
                "special-mode fixture precondition")
            queries.write_snapshot(self.conn, 1, out, self.store, True,
                project_id=submit_tests.PROJECT)
            self.assertEqual(out.stat().st_mode & 0o7777, 0o750)
        finally:
            os.umask(previous)

        previous = os.umask(0o000)
        try:
            out = self.folder / "mode-without-umask"
            queries.write_snapshot(self.conn, 1, out, self.store,
                project_id=submit_tests.PROJECT)
            self.assertEqual(out.stat().st_mode & 0o7777, 0o644)
        finally:
            os.umask(previous)

    def test_overwrite_keeps_other_bits(self):
        # Breaks: masking the kept mode, or not keeping it; umask 0o022 never gives 0o664.
        self.snapshots_pair()
        previous = os.umask(0o022)
        try:
            out = self.folder / "other-bits"
            out.write_bytes(b"old")
            out.chmod(0o664)
            result = snapshot_outcome(lambda: queries.write_snapshot(
                self.conn, 1, out, self.store, True, project_id=submit_tests.PROJECT))
            self.assertEqual(result[0], "ok", result)
            self.assertEqual(out.stat().st_mode & 0o7777, 0o664)
            self.assertEqual(out.read_bytes(), b"one\n")
        finally:
            os.umask(previous)

    def test_overwrite_mode_set_once_after_writes(self):
        # Breaks: changing the stage's mode more than once, or before the last write.
        self.snapshots_pair()
        real_fchmod, real_write = os.fchmod, os.write
        events = []

        def changing(fd, mode):
            events.append(("fchmod", mode))
            return real_fchmod(fd, mode)

        def writing(fd, data):
            # One byte per call, so the write loop runs once per byte.
            events.append(("write", None))
            return real_write(fd, bytes(data[:1]))

        out = self.folder / "mode-once"
        out.write_bytes(b"old")
        out.chmod(0o600)
        with mock.patch.object(queries.os, "fchmod", side_effect=changing), \
                mock.patch.object(queries.os, "write", side_effect=writing):
            result = snapshot_outcome(lambda: queries.write_snapshot(
                self.conn, 1, out, self.store, True, project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "ok", result)
        self.assertIn(("write", None), events)
        self.assertEqual([event for event in events if event[0] == "fchmod"],
                         [("fchmod", 0o600)])
        self.assertEqual(events[-1], ("fchmod", 0o600))
        self.assertEqual(out.stat().st_mode & 0o7777, 0o600)
        self.assertEqual(out.read_bytes(), b"one\n")

    def test_overwrite_stage_private_from_creation(self):
        # Breaks: opening a private replacement's stage wider, or widening before writing.
        self.snapshots_pair()
        real_open, real_write = os.open, os.write
        opened, written = [], []

        def opening(path, flags, mode=0o777, **kwargs):
            fd = real_open(path, flags, mode, **kwargs)
            opened.append((mode, os.fstat(fd).st_mode & 0o7777))
            return fd

        def writing(fd, data):
            written.append(os.fstat(fd).st_mode & 0o7777)
            return real_write(fd, data)

        previous = os.umask(0o022)
        try:
            for final_mode in (0o600, 0o640):
                out = self.folder / ("private-%o" % final_mode)
                out.write_bytes(b"old")
                out.chmod(final_mode)
                opened.clear()
                written.clear()
                with mock.patch.object(queries.os, "open", side_effect=opening), \
                        mock.patch.object(queries.os, "write", side_effect=writing):
                    result = snapshot_outcome(lambda: queries.write_snapshot(
                        self.conn, 1, out, self.store, True, project_id=submit_tests.PROJECT))
                self.assertEqual(opened, [(0o600, 0o600)])
                self.assertEqual(result[0], "ok", result)
                self.assertTrue(written)
                self.assertEqual(written, [0o600] * len(written))
                self.assertEqual(out.stat().st_mode & 0o7777, final_mode)
                self.assertEqual(out.read_bytes(), b"one\n")
            opened.clear()
            written.clear()
            out = self.folder / "public-create"
            with mock.patch.object(queries.os, "open", side_effect=opening), \
                    mock.patch.object(queries.os, "write", side_effect=writing):
                result = snapshot_outcome(lambda: queries.write_snapshot(
                    self.conn, 1, out, self.store, project_id=submit_tests.PROJECT))
            self.assertEqual(result[0], "ok", result)
            self.assertEqual(opened, [(0o644, 0o644)])
            self.assertTrue(written)
            self.assertEqual(written, [0o644] * len(written))
            self.assertEqual(out.read_bytes(), b"one\n")
        finally:
            os.umask(previous)

    def test_create_link_error_propagates(self):
        # Breaks: translating a non-EEXIST link failure or leaving staged bytes behind.
        import errno
        self.snapshots_pair()
        out = self.folder / "link-refused"
        before = set(self.folder.iterdir())
        marker = PermissionError(errno.EPERM, "fixture link refusal")
        with mock.patch.object(queries.os, "link", side_effect=marker):
            result = snapshot_outcome(lambda: queries.write_snapshot(
                self.conn, 1, out, self.store, project_id=submit_tests.PROJECT))
        self.assertIs(result[1], marker, result)
        self.assertFalse(out.exists())
        self.assertEqual(set(self.folder.iterdir()), before)

    def test_copy_project_scoped(self):
        ref = self.snapshots_pair()
        request = submit.submit(self.conn, self.ctx(FOREIGN, git_state=None), draft(kind="PLAN",
            needs="review", to=FOREIGN.address, refs=(ref,)))
        foreign = self.conn.execute("SELECT handle FROM snapshots WHERE relay_id = ?", (request.relay_id,
            )).fetchone()[0]
        out = self.folder / "scoped-copy"
        actions = [lambda: queries.write_snapshot(self.conn, foreign, out, self.store, project_id="p")]
        for a, b in ((1, foreign), (foreign, 1)):
            actions.append(lambda a=a, b=b: queries.snapshot_diff(self.conn, a, b, self.store,
                project_id="p"))
        for action in actions:
            result = snapshot_outcome(action)
            self.assertEqual(result[0], "KeyError", result)
            self.assertFalse(out.exists())
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, foreign, out, self.store,
            project_id="q"))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(out.read_bytes(), b"two\n")
        result = snapshot_outcome(lambda: queries.snapshot_diff(self.conn, foreign, foreign, self.store,
            project_id="q"))
        self.assertEqual(result, ("ok", ""))
        result = snapshot_outcome(lambda: queries.snapshot_diff(self.conn, 1, 2, self.store,
            project_id="p"))
        self.assertEqual(result[0], "ok", result)
        for action in (lambda: queries.write_snapshot(self.conn, 999, out, self.store, project_id="p"),
            lambda: queries.snapshot_diff(self.conn, 1, 999, self.store, project_id="p")):
            self.assertEqual(snapshot_outcome(action)[0], "KeyError")


class TestGitSnapshotCopies(submit_tests.GitSnapshots):
    def test_git_snapshot_copy_and_diff(self):
        repo = self.repository()
        self.request("bivpak@main:d.md")
        repo.commit_files({"d.md": "two\n"})
        self.request("bivpak@main:d.md")
        other = Repo({"unrelated": "none"})
        self.addCleanup(other.close)
        self.conn.executemany(
            "INSERT INTO repository_checkouts (repository_id,path,last_seen_at,checkout_id,route_created_at) VALUES (?,?,1,'snapshot-checkout',1)", [(
            "repo-1", str(repo.path)), ("repo-q", str(other.path))])
        self.conn.commit()
        directory = submit.TableDirectory(self.conn, submit_tests.PROJECT)
        out = self.folder / "git-copy"
        copied = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, out, self.store,
            directory=directory, project_id=submit_tests.PROJECT))
        self.assertEqual(copied[0], "ok", copied)
        self.assertEqual(out.read_bytes(), b"one\n")
        diff = snapshot_outcome(lambda: queries.snapshot_diff(self.conn, 1, 2, self.store, directory,
            project_id=submit_tests.PROJECT))
        self.assertEqual(diff, ("ok", "".join(difflib.unified_diff(["one\n"], ["two\n"],
            fromfile="snapshot 1", tofile="snapshot 2"))))
        with mock.patch.object(refs, "read_blob", return_value=b"wrong"):
            mismatch = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, self.folder / "bad",
                self.store, directory=directory, project_id=submit_tests.PROJECT))
        self.assertEqual(mismatch[0], "ValueError")
        self.assertIn("snapshot 1", str(mismatch[1]))

    def test_git_snapshot_needs_directory(self):
        repo = self.repository()
        self.request("bivpak@main:d.md")
        other = Repo({"unrelated": "none"})
        self.addCleanup(other.close)
        cases = [(None, "which was not given"), (submit_tests.FakeDirectory({"repo-1": repo.path}),
            "not known to this project"), (submit_tests.FakeDirectory({"bivpak": other.path}),
            "the blob cannot be read from"), (submit_tests.FakeDirectory({}, repositories={"bivpak"}),
            "a Git snapshot's repository has no registered checkout in this project")]
        for directory, reason in cases:
            for action in (lambda: queries.write_snapshot(self.conn, 1, self.folder / "copy", self.store,
                directory=directory, project_id=submit_tests.PROJECT), lambda: queries.snapshot_diff(
                self.conn, 1, 1, self.store, directory, project_id=submit_tests.PROJECT)):
                result = snapshot_outcome(action)
                self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES", result)
                self.assertIn(reason, result[1].cause)
                self.assertIsNone(result[1].__cause__)
                self.assertIsNone(result[1].__context__)

    def test_git_snapshot_after_checkout_ended(self):
        repo = self.repository()
        self.request("bivpak@main:d.md")
        self.conn.execute(
            "INSERT INTO repository_checkouts (repository_id, path, last_seen_at, checkout_id,"
            " route_created_at, ended_at) VALUES ('repo-1', ?, 1, 'ended-snapshot-checkout', 1, 2)",
            (str(repo.path),))
        self.conn.commit()
        directory = submit.TableDirectory(self.conn, submit_tests.PROJECT)
        out = self.folder / "ended-git-copy"
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, out, self.store,
            directory=directory, project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "E-SNAPSHOT-NO-BYTES", result)
        self.assertIn("a Git snapshot's repository has no registered checkout in this project",
                      result[1].cause)
        self.conn.execute("UPDATE repository_checkouts SET ended_at = NULL"
                          " WHERE checkout_id = 'ended-snapshot-checkout'")
        self.conn.commit()
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 1, out, self.store,
            directory=directory, project_id=submit_tests.PROJECT))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(out.read_bytes(), b"one\n")

    def test_git_read_errors_propagate(self):
        self.repository()
        self.request("bivpak@main:d.md")
        directory = self.directory
        out = self.folder / "failed-git-copy"
        for marker in (submit_tests.errors.error_for("E-REF-TIMEOUT", value="bivpak@main:d.md"),
            submit_tests.errors.error_for("E-REF-GIT-ENVIRONMENT", value="bivpak@main:d.md", count=1,
            name="GIT_DIR")):
            with mock.patch.object(refs, "read_blob", side_effect=marker):
                actions = (lambda: queries.write_snapshot(self.conn, 1, out, self.store,
                    directory=directory, project_id="p"), lambda: queries.snapshot_diff(self.conn, 1, 1,
                    self.store, directory, project_id="p"))
                for action in actions:
                    result = snapshot_outcome(action)
                    self.assertIs(result[1], marker, result)
                    self.assertFalse(out.exists())

if __name__ == "__main__":
    unittest.main()
