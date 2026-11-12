"""v3 submit pipeline (DD-v3-engine § 4, § 5, § 8, E-OP2; plan Task 4 with erratum 1)."""

import hashlib
from dataclasses import replace
import inspect
import os
import re
import sqlite3
import tempfile
import threading
import traceback
import unittest
import multiprocessing
from pathlib import Path
from unittest import mock

from relay_engine import addresses
from relay_engine import errors
from relay_engine import header
from relay_engine import refs
from relay_engine import schema
from relay_engine import submit
from relay_engine.tests.test_addresses import ACCEPTED, REFUSALS
from relay_engine.tests.test_refs import Repo, clean_git_environment, git

PROJECT = "p"
TEAMS = {"v3-engine": "t1", "v3-other": "t2", "v3": "t3"}
SEATS = {
    address: submit.SeatRef("seat-" + address, address, address.partition(".")[2],
                            TEAMS[address.partition(".")[0]], PROJECT)
    for address in ("v3-engine.planner", "v3-engine.implementer", "v3-other.planner",
                    "v3.orchestrator-planner")
}
SEATS["operator"] = submit.SeatRef(submit.OPERATOR_SEAT_ID, "operator", "operator", None, PROJECT)
FOREIGN = submit.SeatRef("seat-x", "v3-engine.planner@q", "planner", "t9", "q")


def platform_rows(conn):
    """The platform rows the engine's keys need (J3): projects p and q, one open team instance
    per fixture team, every fixture seat with an open occupant, and repository bivpak."""
    conn.executemany("INSERT INTO projects (id, name, created_at) VALUES (?, ?, 1)",
                     [("p", "p"), ("q", "q")])
    conn.executemany("INSERT INTO team_instances (id, project_id, name, level, opened_at,"
                     " opened_by) VALUES (?, ?, ?, 'pair', 1, 'operator')",
                     [(tid, PROJECT, name) for name, tid in TEAMS.items()] + [("t9", "q", "v3-engine")])
    for seat in [seat for seat in SEATS.values() if seat.team_instance_id] + [FOREIGN]:
        conn.execute("INSERT INTO seats (id, team_instance_id, role, state) VALUES (?, ?, ?,"
                     " 'occupied')", (seat.seat_id, seat.team_instance_id, seat.role))
        conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                     " VALUES (?, ?, ?, 'v', 1)", ("o-" + seat.seat_id, seat.seat_id,
                                                   "k-" + seat.seat_id))
    # the same name in q first, so a lookup that ignored the project would find it
    conn.execute("INSERT INTO repositories (id, project_id, name) VALUES ('repo-q', 'q', 'bivpak')")
    conn.execute("INSERT INTO repositories (id, project_id, name) VALUES ('repo-1', ?, 'bivpak')",
                 (PROJECT,))
    conn.commit()
PLANNER = SEATS["v3-engine.planner"]
IMPL = SEATS["v3-engine.implementer"]
OTHER = SEATS["v3-other.planner"]
ORCH = SEATS["v3.orchestrator-planner"]
TABLES = ("relays", "bodies", "recipients", "edges", "git_states", "bundles",
          "findings", "finding_events", "refusals")
GIT_STATE = submit.GitState("repo-1", "a" * 40, "main", 2, "0" * 64)


class FakeDirectory:
    def __init__(self, checkouts=None, repositories=None):
        self.checkouts = dict(checkouts or {})
        self.repositories = None if repositories is None else set(repositories)

    def has_repository(self, repo):
        return repo in (self.checkouts if self.repositories is None else self.repositories)

    def resolve(self, address, sender):
        seat = SEATS.get(address) or {FOREIGN.address: FOREIGN}.get(address)
        if seat is None:
            raise header.AddressRefused("no seat has this address in project %s" % sender.project_id)
        return seat

    def checkout(self, repo):
        return self.checkouts.get(repo)


def draft(kind="SITREP", to="v3-engine.planner", needs="none", subject="s", bundle="b1",
          parent=None, cc=None, verdict=None, refs=(), findings=(), fixes=(), resolves=(),
          body="body\n"):
    lines = ["KIND: " + kind, "TO: " + to]
    if cc:
        lines.append("CC: " + cc)
    if parent:
        lines.append("PARENT: " + parent)
    elif bundle:
        lines.append("BUNDLE: " + bundle)
    if verdict:
        lines.append("VERDICT: " + verdict)
    lines += ["NEEDS: " + needs, "SUBJECT: " + subject]
    lines += ["REF: " + ref for ref in refs]
    lines += ["FINDING: " + finding for finding in findings]
    lines += ["FIXES: " + fix for fix in fixes]
    lines += ["RESOLVE: " + resolve for resolve in resolves]
    return ("\n".join(lines) + "\n\n" + body).encode("utf-8")


class Store(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        schema.init_v3(self.conn)
        platform_rows(self.conn)
        self.clock = 1_700_000_000_000
        self.directory = FakeDirectory()
        self._submission_sequence = 0

    def now(self):
        return self.clock

    def ctx(self, sender=PLANNER, git_state=GIT_STATE, submission_key=None):
        if submission_key is None:
            self._submission_sequence += 1
            submission_key = "fixture-%d" % self._submission_sequence
        return submit.SubmitContext(sender, git_state, self.now, self.directory,
                                    submission_key=submission_key)

    def submit(self, data, sender=PLANNER, **kwargs):
        return submit.submit(self.conn, self.ctx(sender, **kwargs), data)

    def refused(self, code, data, sender=PLANNER, line=None):
        before = self.counts()
        with self.assertRaises(errors.EngineError) as caught:
            self.submit(data, sender)
        self.assertEqual(caught.exception.code, code, caught.exception.cause)
        after = self.counts()
        self.assertEqual(after.pop("refusals"), before.pop("refusals") + 1)
        self.assertEqual(after, before)  # no other row written
        row = self.conn.execute(
            "SELECT project_id, coalesce(seat_id, special), code, line, draft_sha256 FROM refusals"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[:3], (sender.project_id, sender.seat_id, code))
        self.assertEqual(row[4], hashlib.sha256(data).hexdigest())
        if line is not None:
            self.assertEqual(row[3], line)
        return caught.exception

    def counts(self):
        return {table: self.conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
                for table in TABLES}

    def relay(self, relay_id):
        self.conn.row_factory = sqlite3.Row
        try:
            return dict(self.conn.execute("SELECT * FROM relays WHERE id = ?",
                                          (relay_id,)).fetchone())
        finally:
            self.conn.row_factory = None


class GitStore(Store):
    def setUp(self):
        super().setUp()
        clean_git_environment(self)


class TestAdmit(Store):
    def test_admit_writes_all_tables(self):
        clean_git_environment(self)
        repo = Repo({"src/a.py": "one\ntwo\nthree\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        with tempfile.TemporaryDirectory() as name:
            check = Path(os.path.realpath(name)) / "check.txt"
            check.write_bytes(b"Ran 3 tests\nOK\n")
            request = self.submit(draft(kind="REVIEW", needs="review", to="v3-engine.implementer"))
            self.clock += 1
            reply = self.submit(draft(
                kind="REVIEW", verdict="must-revise", parent="#%d" % request.handle,
                to="v3-engine.planner", cc="v3.orchestrator-planner",
                refs=("file:" + str(check), "bivpak@main:src/a.py#L2", "#1"),
                findings=("blocking 1 the parser drops the second line",)),
                sender=IMPL)
        counts = self.counts()
        self.assertEqual(counts, {"relays": 2, "bodies": 2, "recipients": 3, "edges": 4,
                                  "git_states": 2, "bundles": 1, "findings": 1,
                                  "finding_events": 1, "refusals": 0})
        row = self.relay(reply.relay_id)
        self.assertEqual((row["handle"], row["kind"], row["verdict"], row["sender_seat_id"],
                          row["sender_role"], row["project_id"], row["team_instance_id"]),
                         (2, "REVIEW", "must-revise", IMPL.seat_id, "implementer", PROJECT, "t1"))
        self.assertEqual(row["body_sha256"], hashlib.sha256(b"body\n").hexdigest())
        self.assertEqual(self.conn.execute("SELECT body FROM bodies WHERE relay_id = ?",
                                           (reply.relay_id,)).fetchone()[0], b"body\n")
        edges = self.conn.execute(
            "SELECT kind, ordinal, target_type, spelling, target_relay_id, target_repository_name,"
            " target_commit, target_path, target_line_start, target_line_end, digest_at_submit"
            " FROM edges WHERE from_relay_id = ? ORDER BY kind, ordinal",
            (reply.relay_id,)).fetchall()
        commit = git(repo.path, "rev-parse", "main")
        blob = git(repo.path, "rev-parse", "main:src/a.py")
        self.assertEqual(edges, [
            ("parent", 1, "relay", "#1", request.relay_id, None, None, None, None, None, None),
            ("ref", 1, "path", "file:" + str(check), None, None, None, str(check), None, None,
             hashlib.sha256(b"Ran 3 tests\nOK\n").hexdigest()),
            ("ref", 2, "git", "bivpak@main:src/a.py#L2", None, "bivpak", commit, "src/a.py",
             2, 2, blob),
            ("ref", 3, "relay", "#1", request.relay_id, None, None, None, None, None, None),
        ])
        self.assertEqual(self.conn.execute(  # J3's id in the sender's project, not repo-q
            "SELECT target_repository_id FROM edges WHERE target_type = 'git'").fetchall(),
            [("repo-1",)])
        finding = self.conn.execute(
            "SELECT handle, raised_in, raiser_seat_id, severity, summary, status, bundle_id,"
            " (SELECT ordinal FROM edges WHERE id = check_edge_id) FROM findings").fetchone()
        self.assertEqual(finding, (1, reply.relay_id, IMPL.seat_id, "blocking",
                                   "the parser drops the second line", "open",
                                   self.relay(request.relay_id)["bundle_id"], 1))
        state = self.conn.execute("SELECT repository_id, head_commit, branch, dirty_paths,"
                                  " porcelain_sha256 FROM git_states WHERE id = ?",
                                  (row["git_state_id"],)).fetchone()
        self.assertEqual(state, ("repo-1", "a" * 40, "main", 2, "0" * 64))

    def test_fault_before_commit_leaves_nothing(self):
        before = self.counts()
        with mock.patch.object(submit, "_write_transitions", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.submit(draft())
        self.assertEqual(self.counts(), before)  # no relay rows and no refusal row
        self.assertFalse(self.conn.in_transaction)
        self.submit(draft())  # positive control: the same draft then admits
        self.assertEqual(self.counts()["relays"], 1)

    def test_git_state_optional(self):
        admitted = self.submit(draft(), git_state=submit.GitState(None, None, None, 0, "0" * 64))
        self.assertIsNone(self.relay(admitted.relay_id)["git_state_id"])
        self.assertEqual(self.counts()["git_states"], 0)

    def test_foreign_keys_required(self):  # O5
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        schema.init_v3(conn)
        platform_rows(conn)
        with self.assertRaises(ValueError):
            submit.submit(conn, self.ctx(), draft())
        with tempfile.TemporaryDirectory() as name:
            store = submit.open_store(os.path.join(name, "store"))
            try:
                self.assertEqual(store.execute("PRAGMA foreign_keys").fetchone(), (1,))
                platform_rows(store)
                submit.submit(store, self.ctx(), draft())  # positive control
            finally:
                store.close()


class TestLineage(Store):
    def test_parent_membership(self):
        to_planner = self.submit(draft(to="v3-engine.planner"), sender=OTHER)
        cc_planner = self.submit(draft(to="v3-engine.implementer", cc="v3-engine.planner"),
                                 sender=OTHER)
        by_planner = self.submit(draft(to="v3-engine.implementer"), sender=PLANNER)
        elsewhere = self.submit(draft(to="v3-engine.implementer"), sender=OTHER)
        for parent in (to_planner, cc_planner, by_planner):
            for spelling in ("#%d" % parent.handle, parent.relay_id):
                with self.subTest(parent=spelling):
                    self.submit(draft(parent=spelling), sender=PLANNER)
        error = self.refused("E-PARENT-NOT-MEMBER", draft(parent="#%d" % elsewhere.handle),
                             line=3)
        self.assertIn("#%d" % elsewhere.handle, error.cause)
        self.refused("E-PARENT-UNKNOWN", draft(parent="#99"), line=3)
        self.refused("E-PARENT-UNKNOWN", draft(parent="0190a1b2-c3d4-7e5f-8a9b-0123456789ab"),
                     line=3)

    def test_parent_in_another_project_unknown(self):
        foreign = FOREIGN
        other = self.submit(draft(), sender=foreign)
        self.refused("E-PARENT-UNKNOWN", draft(parent=other.relay_id), line=3)

    def test_bundle_inherited(self):
        root = self.submit(draft(bundle="b1"))
        child = self.submit(draft(parent="#%d" % root.handle))
        grandchild = self.submit(draft(parent="#%d" % child.handle))
        bundle = self.relay(root.relay_id)["bundle_id"]
        self.assertIsNotNone(bundle)
        self.assertEqual({self.relay(r.relay_id)["bundle_id"] for r in (child, grandchild)},
                         {bundle})
        self.assertEqual(self.conn.execute("SELECT project_id, team_instance_id, name,"
                                           " opened_by_seat_id FROM bundles").fetchall(),
                         [(PROJECT, "t1", "b1", PLANNER.seat_id)])
        # A2: a second root naming the bundle joins it
        again = self.submit(draft(bundle="b1"), sender=OTHER)
        self.assertEqual(self.relay(again.relay_id)["bundle_id"], bundle)
        other = self.submit(draft(bundle="b2"))
        self.assertNotEqual(self.relay(other.relay_id)["bundle_id"], bundle)
        self.assertEqual(self.counts()["bundles"], 2)
        # E2: BUNDLE beside PARENT is the parser's refusal
        data = draft(parent="#1").replace(b"PARENT: #1\n", b"PARENT: #1\nBUNDLE: b1\n")
        self.refused("E-FIELD-FORBIDDEN", data, line=4)

    def test_answered_by_set(self):
        request = self.submit(draft(to="v3-engine.implementer, v3-engine.planner", needs="act"),
                              sender=OTHER)
        first = self.submit(draft(parent="#1", to="v3-other.planner"), sender=IMPL)
        self.submit(draft(parent="#1", to="v3-other.planner"), sender=IMPL)
        rows = dict(self.conn.execute("SELECT seat_id, answered_by FROM recipients"
                                      " WHERE relay_id = ?", (request.relay_id,)).fetchall())
        self.assertEqual(rows, {IMPL.seat_id: first.relay_id, PLANNER.seat_id: None})

    def test_cc_not_delivered(self):
        admitted = self.submit(draft(to="v3-engine.implementer", cc="v3.orchestrator-planner"))
        rows = self.conn.execute("SELECT seat_id, role, delivered_at, opened_at, answered_by"
                                 " FROM recipients WHERE relay_id = ? ORDER BY role",
                                 (admitted.relay_id,)).fetchall()
        self.assertEqual(rows, [(ORCH.seat_id, "cc", None, None, None),
                                (IMPL.seat_id, "to", None, None, None)])


class TestRefusals(Store):
    def test_refusal_recorded(self):
        cases = (
            ("E-HEADER-UNKNOWN-KEY", b"KIND: SITREP\nROLE: x\n\nbody", 2),
            ("E-ADDRESS", draft(to="v3-engine.planner, v3-gone.planner"), 2),
            ("E-ADDRESS", draft(cc="v3-gone.reviewer"), 3),
            ("E-PARENT-UNKNOWN", draft(parent="#7"), 3),
            ("E-REF-UNRESOLVED", draft(refs=("#1", "nowhere@main:x")), 7),
            ("E-REF-UNRESOLVED", draft(refs=("#5",)), 6),
        )
        review = ["KIND: REVIEW", "TO: v3-engine.planner", "NEEDS: act", "SUBJECT: s",
                  "PARENT: #1", "VERDICT: must-revise"]
        cases += (  # PR-3 fold S12: the parser's and the rules' codes, each through submit()
            ("E-HEADER-CRLF", b"KIND: SITREP\r\nTO: v3-engine.planner\n\nbody", 1),
            ("E-HEADER-CONTROL", draft(subject="a\x0bb"), 5),
            ("E-FIELD-ENUM", draft(needs="soon"), 4),
            ("E-FIELD-SHAPE", draft(parent="#0"), 3),
            ("E-FINDING-NO-CHECK", ("\n".join(review + ["REF: #1", "FINDING: blocking 1 x"])
                                    + "\n\nbody").encode(), 8),
            ("E-REF-SPELLING", draft(refs=("nope",)), 6),
            ("E-PARENT-NOT-MEMBER", draft(parent="#2"), 3),
            ("E-FINDING-UNKNOWN", draft(resolves=("F9 withdrawn moot",)), 6),
        )
        self.submit(draft())  # handle 1 exists for the REF #1 control
        self.submit(draft(to="v3-other.planner"), sender=OTHER)  # #2, which PLANNER is not on
        for code, data, line in cases:
            with self.subTest(code=code, line=line):
                self.refused(code, data, line=line)

    def test_same_seat_twice_refused(self):
        self.directory.resolve = lambda address, sender: PLANNER
        self.refused("E-ADDRESS", draft(to="v3-engine.planner, v3-engine.implementer"), line=2)

    def test_home_expansion(self):  # fold R8
        with tempfile.TemporaryDirectory() as name:
            home = Path(os.path.realpath(name))
            (home / "notes.txt").write_bytes(b"n\n")
            with mock.patch.object(submit, "_home", return_value=str(home)), \
                    mock.patch.dict(os.environ, {"HOME": "/nonexistent"}):
                admitted = self.submit(draft(refs=("file:~/notes.txt",)))
                self.refused("E-REF-UNRESOLVED", draft(refs=("file:~/absent.txt",)), line=6)
        edge = self.conn.execute("SELECT spelling, target_path, digest_at_submit FROM edges"
                                 " WHERE from_relay_id = ?", (admitted.relay_id,)).fetchone()
        self.assertEqual(edge, ("file:~/notes.txt", str(home / "notes.txt"),
                                hashlib.sha256(b"n\n").hexdigest()))

    def test_home_is_the_password_entry(self):
        # the entry is mocked, so the assertion holds on a host whose uid has
        # none (the linux release leg's container); it still ignores $HOME
        with tempfile.TemporaryDirectory() as name:
            entry = mock.Mock(pw_dir=name)
            with mock.patch.dict(os.environ, {"HOME": "/nonexistent"}), \
                    mock.patch("pwd.getpwuid", return_value=entry) as getpwuid:
                self.assertEqual(submit._home(), os.path.realpath(
                    __import__("pwd").getpwuid(os.getuid()).pw_dir))
            self.assertEqual(getpwuid.call_args_list[0], mock.call(os.getuid()))

    def test_no_password_entry_refuses_home_path(self):  # PR-3 fold Q1(a)
        missing = KeyError("getpwuid(): uid not found: %d" % os.getuid())
        with mock.patch("pwd.getpwuid", side_effect=missing):
            error = self.refused("E-REF-UNRESOLVED", draft(refs=("file:~/notes.txt",)), line=6)
            self.assertIn("no home directory in the password database", error.cause)
            self.submit(draft())  # control: a draft with no ~/ path still admits


class TestIds(Store):
    def test_ids_monotonic_same_ms(self):
        admitted = [self.submit(draft()) for _ in range(1000)]
        self.assertEqual([a.handle for a in admitted], list(range(1, 1001)))
        ids = [a.relay_id for a in admitted]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 1000)
        times = [a.created_at_ms for a in admitted]
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertEqual(times[0], self.clock)

    def test_handles_per_project(self):
        foreign = FOREIGN
        self.assertEqual(self.submit(draft()).handle, 1)
        self.assertEqual(self.submit(draft(), sender=foreign).handle, 1)
        self.assertEqual(self.submit(draft()).handle, 2)

    def test_clock_going_back_still_increases(self):
        first = self.submit(draft())
        self.clock -= 5000
        second = self.submit(draft())
        self.assertGreater(second.created_at_ms, first.created_at_ms)
        self.assertGreater(second.relay_id, first.relay_id)

    def test_new_uuid7(self):
        shape = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
        last = None
        for now in (5, 5, 5, 4, 6):
            value = submit.new_uuid7(now, last)
            self.assertRegex(value, shape)
            if last is not None:
                self.assertGreater(value, last)
            last = value
        self.assertEqual(int(submit.new_uuid7(0x0123456789AB).replace("-", "")[:12], 16),
                         0x0123456789AB)
        top = "%08x-%04x-7fff-bfff-ffffffffffff" % (0x00000001, 0x0000)
        self.assertGreater(submit.new_uuid7(1, top), top)  # counter overflow moves to the next ms


class TestFindings(Store):
    def setUp(self):
        super().setUp()
        self.check = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.check))
        (self.check / "out.txt").write_bytes(b"FAILED\n")
        request = self.submit(draft(kind="REVIEW", needs="review", to="v3-engine.implementer"))
        self.review = self.submit(draft(
            kind="REVIEW", verdict="must-revise", parent="#%d" % request.handle,
            refs=("file:%s/out.txt" % self.check,),
            findings=("blocking 1 first", "advisory second")), sender=IMPL)

    def status(self, handle):
        return self.conn.execute("SELECT status, fixed_commit, resolved_in, reason FROM findings"
                                 " WHERE handle = ?", (handle,)).fetchone()

    def fold(self, fixes, sender=PLANNER):
        return self.submit(draft(kind="REVIEW-FOLD", parent="#%d" % self.review.handle,
                                 to="v3-engine.implementer", fixes=fixes), sender=sender)

    def resolve(self, resolves, sender=IMPL, parent=None):
        return self.submit(draft(parent=parent or "#%d" % self.review.handle,
                                 to="v3-engine.planner", resolves=resolves), sender=sender)

    def test_finding_lifecycle(self):
        commit = "c" * 40
        fold = self.fold(("F1 " + commit,))
        self.assertEqual(self.status(1), ("fixed", commit, None, None))
        self.resolve(("F1 fix-refused the second line still drops",))
        self.assertEqual(self.status(1)[0], "open")
        self.fold(("F1 " + "d" * 40,))
        accepted = self.resolve(("F1 fix-accepted now kept",))
        self.assertEqual(self.status(1), ("fix-accepted", "d" * 40, accepted.relay_id, "now kept"))
        withdrawn = self.resolve(("F2 withdrawn reading only",))
        self.assertEqual(self.status(2), ("withdrawn", None, withdrawn.relay_id, "reading only"))
        events = self.conn.execute("SELECT f.handle, e.transition, e.from_status, e.to_status"
                                   " FROM finding_events e JOIN findings f ON f.id = e.finding_id"
                                   " ORDER BY e.id").fetchall()
        self.assertEqual(events, [
            (1, "raised", "open", "open"), (2, "raised", "open", "open"),
            (1, "fixes", "open", "fixed"), (1, "fix-refused", "fixed", "open"),
            (1, "fixes", "open", "fixed"), (1, "fix-accepted", "fixed", "fix-accepted"),
            (2, "withdrawn", "open", "withdrawn")])
        self.assertIsNotNone(fold)

    def test_resolve_needs_raiser_or_orchestrator(self):
        self.fold(("F1 " + "c" * 40,))
        own = self.submit(draft(to="v3-engine.implementer"), sender=OTHER)
        self.refused("E-RESOLVE-NOT-RAISER", draft(
            parent=own.relay_id, to="v3-engine.implementer",
            resolves=("F1 fix-accepted looks fine",)), sender=OTHER, line=6)
        # OTHER was not addressed by the review, so it names a relay addressed to it
        self.resolve(("F1 fix-accepted the orchestrator accepts",), sender=ORCH,
                     parent=self.submit(draft(to="v3.orchestrator-planner"), sender=IMPL).relay_id)
        self.assertEqual(self.status(1)[0], "fix-accepted")

    def test_resolve_state_and_unknown(self):
        self.refused("E-RESOLVE-STATE", draft(parent="#%d" % self.review.handle,
                                               to="v3-engine.planner",
                                               resolves=("F1 fix-accepted not fixed yet",)),
                     sender=IMPL, line=6)
        self.refused("E-FINDING-UNKNOWN", draft(kind="REVIEW-FOLD",
                                                parent="#%d" % self.review.handle,
                                                to="v3-engine.implementer",
                                                fixes=("F9 " + "c" * 40,)), line=6)
        self.resolve(("F2 withdrawn reading only",))
        self.refused("E-RESOLVE-STATE", draft(kind="REVIEW-FOLD",
                                              parent="#%d" % self.review.handle,
                                              to="v3-engine.implementer",
                                              fixes=("F2 " + "c" * 40,)), line=6)


    def raise_finding(self):
        """A fresh review by IMPL with one blocking finding; its handle text."""
        request = self.submit(draft(kind="REVIEW", needs="review", to="v3-engine.implementer"))
        review = self.submit(draft(
            kind="REVIEW", verdict="must-revise", parent="#%d" % request.handle,
            refs=("file:%s/out.txt" % self.check,), findings=("blocking 1 x",)), sender=IMPL)
        return "F%d" % self.conn.execute("SELECT handle FROM findings WHERE raised_in = ?",
                                         (review.relay_id,)).fetchone()[0], review

    def bring(self, status):
        handle, review = self.raise_finding()
        steps = {"open": [], "fixed": ["fixes"], "fix-accepted": ["fixes", "fix-accepted"],
                 "withdrawn": ["withdrawn"]}[status]
        for step in steps:
            if step == "fixes":
                self.fold((handle + " " + "c" * 40,))
            else:
                self.resolve((handle + " " + step + " r",), parent="#%d" % review.handle)
        return handle, review

    def test_every_transition_pinned(self):  # PR-3 fold S4 (M1, M2, M14), DD § 8
        allowed = {("fixes", "open"): "fixed", ("fix-accepted", "fixed"): "fix-accepted",
                   ("fix-refused", "fixed"): "open", ("withdrawn", "open"): "withdrawn",
                   ("withdrawn", "fixed"): "withdrawn"}
        for transition in ("fixes", "fix-accepted", "fix-refused", "withdrawn"):
            for status in ("open", "fixed", "fix-accepted", "withdrawn"):
                with self.subTest(transition=transition, status=status):
                    handle, review = self.bring(status)
                    number = int(handle[1:])
                    if transition == "fixes":
                        data = draft(kind="REVIEW-FOLD", parent="#%d" % review.handle,
                                     to="v3-engine.implementer", fixes=(handle + " " + "e" * 40,))
                        sender = PLANNER
                    else:
                        data = draft(parent="#%d" % review.handle, to="v3-engine.planner",
                                     resolves=(handle + " " + transition + " why",))
                        sender = IMPL
                    if (transition, status) not in allowed:
                        self.refused("E-RESOLVE-STATE", data, sender=sender, line=6)
                        self.assertEqual(self.status(number)[0], status)
                        continue
                    admitted = self.submit(data, sender=sender)
                    to_status, fixed, resolved_in, reason = self.status(number)
                    self.assertEqual(to_status, allowed[(transition, status)])
                    closed = to_status in ("fix-accepted", "withdrawn")
                    self.assertEqual(resolved_in, admitted.relay_id if closed else None)
                    if transition == "fixes":
                        self.assertEqual(fixed, "e" * 40)
                    else:
                        self.assertEqual(reason, "why")

    def test_check_edge_is_the_cited_ref(self):  # panel survivor M6
        with tempfile.NamedTemporaryFile(dir=self.check, delete=False) as handle:
            handle.write(b"second check\n")
        request = self.submit(draft(kind="REVIEW", needs="review", to="v3-engine.implementer"))
        review = self.submit(draft(
            kind="REVIEW", verdict="must-revise", parent="#%d" % request.handle,
            refs=("file:%s/out.txt" % self.check, "file:" + handle.name),
            findings=("blocking 2 second", "blocking 1 first")), sender=IMPL)
        self.assertEqual(self.conn.execute(
            "SELECT f.summary, e.ordinal FROM findings f JOIN edges e ON e.id = f.check_edge_id"
            " WHERE f.raised_in = ? ORDER BY f.handle", (review.relay_id,)).fetchall(),
            [("second", 2), ("first", 1)])

    def test_status_carried_across_lines(self):  # PR-3 fold S4 (M15)
        self.fold(("F1 " + "c" * 40,))
        self.refused("E-RESOLVE-STATE", draft(
            parent="#%d" % self.review.handle, to="v3-engine.planner",
            resolves=("F1 fix-refused not yet", "F1 fix-accepted now")), sender=IMPL, line=7)
        both = self.resolve(("F1 fix-refused not yet", "F1 withdrawn moot"))
        self.assertEqual(self.status(1), ("withdrawn", "c" * 40, both.relay_id, "moot"))

    def ordered(self, *lines):
        head = ["KIND: REVIEW-FOLD", "TO: v3-engine.planner",
                "PARENT: #%d" % self.review.handle, "NEEDS: none", "SUBJECT: s"]
        return ("\n".join(head + list(lines)) + "\n\nbody\n").encode("utf-8")

    def events(self, relay_id):
        return self.conn.execute(
            "SELECT f.handle, e.transition, e.from_status, e.to_status FROM finding_events e"
            " JOIN findings f ON f.id = e.finding_id WHERE e.relay_id = ? ORDER BY e.id",
            (relay_id,)).fetchall()

    def test_header_line_order(self):  # PR-3 fold S8
        self.refused("E-RESOLVE-STATE", self.ordered("RESOLVE: F1 withdrawn moot",
                                                     "FIXES: F1 " + "c" * 40), sender=IMPL, line=7)
        fixed_then = self.submit(self.ordered("FIXES: F1 " + "c" * 40, "RESOLVE: F1 withdrawn moot"),
                                 sender=IMPL)
        self.assertEqual(self.events(fixed_then.relay_id),
                         [(1, "fixes", "open", "fixed"), (1, "withdrawn", "fixed", "withdrawn")])
        handle, review = self.bring("fixed")
        self.review = review
        refused_then = self.submit(self.ordered("RESOLVE: %s fix-refused again" % handle,
                                                "FIXES: %s %s" % (handle, "d" * 40)), sender=IMPL)
        number = int(handle[1:])
        self.assertEqual(self.events(refused_then.relay_id),
                         [(number, "fix-refused", "fixed", "open"), (number, "fixes", "open", "fixed")])
        self.assertEqual(self.status(number), ("fixed", "d" * 40, None, "again"))


class TestProjectScope(Store):  # PR-3 fold S5: one witness per project filter
    def test_bundle_name_per_project(self):  # M3
        mine = self.submit(draft(bundle="b1"))
        theirs = self.submit(draft(bundle="b1"), sender=FOREIGN)
        self.assertNotEqual(self.relay(mine.relay_id)["bundle_id"],
                            self.relay(theirs.relay_id)["bundle_id"])
        self.assertEqual(sorted(self.conn.execute("SELECT project_id, name FROM bundles")),
                         [("p", "b1"), ("q", "b1")])

    def test_handle_per_project(self):  # M4
        self.submit(draft(), sender=FOREIGN)  # q's #1 is the store's first row
        mine = self.submit(draft(to="v3-engine.implementer"))
        child = self.submit(draft(parent="#1", refs=("#1",)))
        self.assertEqual(self.relay(child.relay_id)["parent_id"], mine.relay_id)
        self.assertEqual(self.conn.execute(
            "SELECT target_relay_id FROM edges WHERE from_relay_id = ? AND kind = 'ref'",
            (child.relay_id,)).fetchone()[0], mine.relay_id)

    def test_ref_names_its_project(self):  # M5
        self.submit(draft(), sender=FOREIGN)
        q2 = self.submit(draft(), sender=FOREIGN)
        self.submit(draft())
        p2 = self.submit(draft())
        cites = self.submit(draft(refs=("#2@q", "#2@p", "#2")))
        self.assertEqual([row[0] for row in self.conn.execute(
            "SELECT target_relay_id FROM edges WHERE from_relay_id = ? ORDER BY ordinal",
            (cites.relay_id,))], [q2.relay_id, p2.relay_id, p2.relay_id])

    def review_with_finding(self, requester, reviewer, to):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"FAILED\n")
        self.addCleanup(os.unlink, handle.name)
        request = self.submit(draft(kind="REVIEW", needs="review", to=reviewer.address),
                              sender=requester)
        return self.submit(draft(kind="REVIEW", verdict="must-revise",
                                 parent="#%d" % request.handle, to=to,
                                 refs=("file:" + os.path.realpath(handle.name),),
                                 findings=("blocking 1 x",)), sender=reviewer)

    def test_finding_handles_per_project(self):  # M20, M21
        self.review_with_finding(FOREIGN, FOREIGN, "v3-engine.planner")  # q's F1 comes first
        review = self.review_with_finding(PLANNER, IMPL, "v3-engine.planner")
        self.assertEqual(self.conn.execute("SELECT project_id, handle FROM findings ORDER BY rowid")
                         .fetchall(), [("q", 1), ("p", 1)])
        self.submit(draft(parent="#%d" % review.handle, resolves=("F1 withdrawn moot",)),
                    sender=IMPL)
        self.assertEqual(sorted(self.conn.execute("SELECT project_id, status FROM findings")),
                         [("p", "withdrawn"), ("q", "open")])


def _hammer(path, address, count, barrier, queue):
    """One submitter process for the two-process admission test (S3)."""
    conn = submit.open_store(path)
    seen = []
    try:
        barrier.wait(60)
        for iteration in range(count):
            try:
                ctx = submit.SubmitContext(
                    SEATS[address], None, lambda: 1_700_000_000_000, FakeDirectory(),
                    submission_key="hammer:%s:%d" % (address, iteration))
                submit.submit(conn, ctx, draft())
            except Exception as error:  # every failure is counted, typed or not
                seen.append("%s: %s" % (type(error).__name__, error))
    finally:
        conn.close()
        queue.put((address, seen))


class TestConcurrency(Store):  # PR-3 fold S2, S3
    def test_two_processes_admit_every_draft(self):  # S3, acceptance 9
        spawn = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as name:
            path = os.path.join(name, "store")
            seeded = submit.open_store(path)
            platform_rows(seeded)
            seeded.commit()
            seeded.close()
            barrier, queue = spawn.Barrier(2), spawn.Queue()
            workers = [spawn.Process(target=_hammer, args=(path, address, 300, barrier, queue))
                       for address in ("v3-engine.planner", "v3-engine.implementer")]
            for worker in workers:
                worker.start()
            results = dict(queue.get(timeout=120) for _ in workers)
            for worker in workers:
                worker.join(30)
            self.assertEqual(results, {"v3-engine.planner": [], "v3-engine.implementer": []})
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute("SELECT handle, id, created_at, sender_seat_id FROM relays"
                                    " ORDER BY handle").fetchall()
            finally:
                conn.close()
        self.assertEqual([row[0] for row in rows], list(range(1, 601)))
        self.assertEqual([row[1] for row in rows], sorted(row[1] for row in rows))
        self.assertTrue(all(b[2] > a[2] for a, b in zip(rows, rows[1:])))
        senders = [row[3] for row in rows]
        self.assertTrue(any(a != b for a, b in zip(senders, senders[1:])))  # they interleaved

    def test_reference_resolution_holds_no_write_lock(self):  # S2
        with tempfile.TemporaryDirectory() as name:
            path = os.path.join(name, "store")
            check = Path(os.path.realpath(name)) / "check.txt"
            check.write_bytes(b"ok\n")
            seeded = submit.open_store(path)
            platform_rows(seeded)
            seeded.commit()
            seeded.close()
            inside, release, result = threading.Event(), threading.Event(), {}
            read_file = refs.read_file

            def slow(target, keep):
                inside.set()
                release.wait(30)
                return read_file(target, keep)

            def first():
                conn = submit.open_store(path)
                try:
                    result["a"] = submit.submit(conn, self.ctx(), draft(refs=("file:" + str(check),)))
                except BaseException as error:
                    result["a"] = error
                finally:
                    conn.close()

            with mock.patch.object(refs, "read_file", slow):
                thread = threading.Thread(target=first)
                thread.start()
                try:
                    self.assertTrue(inside.wait(30))
                    conn = sqlite3.connect(path, timeout=0.5)  # a held write lock fails fast
                    conn.execute("PRAGMA foreign_keys = ON")
                    try:
                        second = submit.submit(conn, self.ctx(IMPL), draft())
                        with self.assertRaises(errors.EngineError) as caught:
                            submit.submit(conn, self.ctx(IMPL), b"KIND: SITREP\nROLE: x\n\nbody")
                        self.assertEqual(caught.exception.code, "E-HEADER-UNKNOWN-KEY")
                        self.assertEqual(conn.execute("SELECT count(*) FROM refusals").fetchone(),
                                         (1,))
                    finally:
                        conn.close()
                finally:
                    release.set()
                    thread.join(30)
        self.assertIsInstance(result["a"], submit.Admitted)
        self.assertEqual((second.handle, result["a"].handle), (1, 2))


def vm_steps(conn, call):
    """SQLite virtual-machine steps a call runs: work that grows with the rows
    a statement visits, and no wall clock (PR-3 fold S7, R14)."""
    count = [0]

    def tick():
        count[0] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        call()
    finally:
        conn.set_progress_handler(None, 1)
    return count[0]


class TestSubmitCost(Store):  # PR-3 fold S7
    def seed(self, count):
        """count relays of project p from earlier seats, one finding per ten."""
        rows, last = [], None
        for n in range(1, count + 1):
            last = submit.new_uuid7(1_000 + n, last)
            rows.append((last, n, 1_000 + n))
        self.conn.executemany(
            "INSERT INTO relays (id, handle, project_id, kind, needs, subject, sender_seat_id,"
            " created_at, body_sha256, meta_sha256, body_bytes, header_bytes)"
            " VALUES (?, ?, 'p', 'SITREP', 'none', 's', '%s', ?, ?, ?, 0, 0)" % OTHER.seat_id,
            [(rid, n, at, "a" * 64, "a" * 64) for rid, n, at in rows])
        self.conn.executemany(
            "INSERT INTO findings (id, handle, project_id, raised_in, raiser_seat_id, severity,"
            " summary, status) VALUES (?, ?, 'p', ?, '%s', 'advisory', 's', 'withdrawn')" % OTHER.seat_id,
            [("f%07d" % n, n // 10 + 1, rid) for rid, n, _ in rows if n % 10 == 0])
        self.conn.commit()

    def steps(self, count):
        self.setUp()
        self.seed(count)
        check = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(lambda: __import__("shutil").rmtree(check))
        (check / "out.txt").write_bytes(b"FAILED\n")
        request = {}
        root = vm_steps(self.conn, lambda: request.setdefault("r", self.submit(draft(
            kind="REVIEW", needs="review", to="v3-engine.implementer"))))
        reply = vm_steps(self.conn, lambda: self.submit(draft(
            kind="REVIEW", verdict="must-revise", parent="#%d" % request["r"].handle,
            refs=("file:%s/out.txt" % check, "#%d" % (count // 2)),
            findings=("blocking 1 x",)), sender=IMPL))
        return {"root": root, "reply": reply}

    def test_submit_cost_flat(self):
        record = {count: self.steps(count) for count in (1_000, 50_000)}
        print("submit cost (SQLite VM steps):", record)
        for name in ("root", "reply"):
            small, large = record[1_000][name], record[50_000][name]
            self.assertLess(large, small * 1.5, record)


class TestContext(Store):  # PR-3 fold S6
    def test_git_state_validated(self):
        good = (GIT_STATE, None, submit.GitState(None, None, None, 0, "0" * 64),
                submit.GitState("repo-1", "b" * 64, None, 0, "f" * 64))
        for state in good:
            with self.subTest(state=state):
                submit.SubmitContext(PLANNER, state, self.now, self.directory,
                                         submission_key="git-state-fixture")
        bad = (submit.GitState("repo-1", "abc", "main", 2, "0" * 64),
               submit.GitState("repo-1", "A" * 40, "main", 2, "0" * 64),
               submit.GitState("repo-1", "a" * 40, "main", -1, "0" * 64),
               submit.GitState("repo-1", "a" * 40, "main", "2", "0" * 64),
               submit.GitState("repo-1", "a" * 40, "main", True, "0" * 64),
               submit.GitState("repo-1", "a" * 40, "main", 2, "F" * 64),
               submit.GitState("repo-1", "a" * 40, "main", 2, "0" * 63),
               submit.GitState("", "a" * 40, "main", 2, "0" * 64),
               submit.GitState("repo-1", "a" * 40, "", 2, "0" * 64),
               submit.GitState(None, "a" * 40, None, 0, "0" * 64),
               submit.GitState(None, None, "main", 0, "0" * 64),
               submit.GitState(None, None, None, 3, "0" * 64),
               ("repo-1", "a" * 40, "main", 2, "0" * 64))
        for state in bad:
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    submit.SubmitContext(PLANNER, state, self.now, self.directory,
                                         submission_key="git-state-fixture")
        self.assertEqual(self.counts(), dict.fromkeys(TABLES, 0))


class TestActivity(Store):  # PR-3 fold R6
    def test_admission_is_activity(self):
        admitted = self.submit(draft())
        self.assertEqual(self.conn.execute("SELECT seat_id, last_request_at FROM seat_activity")
                         .fetchall(), [(PLANNER.seat_id, admitted.created_at_ms)])
        self.clock -= 10_000  # an earlier clock never moves activity back
        later = self.submit(draft())
        self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity")
                         .fetchone()[0], later.created_at_ms)
        with mock.patch.object(submit, "_write_answered_by", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.submit(draft(parent="#1"), sender=PLANNER)
        self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity")
                         .fetchone()[0], later.created_at_ms)  # it commits with the admission
        self.refused("E-PARENT-UNKNOWN", draft(parent="#99"), sender=IMPL, line=3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM seat_activity").fetchone(), (1,))
        # a later activity, such as a read, is never moved back by an admission
        self.conn.execute("UPDATE seat_activity SET last_request_at = ?", (later.created_at_ms + 10**6,))
        self.conn.commit()
        self.submit(draft())
        self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity")
                         .fetchone()[0], later.created_at_ms + 10**6)


class TestDigest(Store):
    def test_metadata_digest_converges(self):  # erratum E1
        tight = self.submit(draft(to="v3-engine.planner,v3-engine.implementer"))
        spaced = self.submit(draft(to="v3-engine.planner, v3-engine.implementer"))
        other = self.submit(draft(to="v3-engine.planner, v3-other.planner"))
        copied = self.submit(draft(to="v3-engine.planner,v3-engine.implementer",
                                   cc="v3.orchestrator-planner"))
        digest = lambda a: self.relay(a.relay_id)["meta_sha256"]
        self.assertEqual(digest(tight), digest(spaced))
        self.assertNotEqual(digest(tight), digest(other))
        self.assertNotEqual(digest(tight), digest(copied))  # CC is a field of the digest
        self.assertRegex(digest(tight), r"\A[0-9a-f]{64}\Z")

    def test_digest_over_resolved_targets(self):
        clean_git_environment(self)  # PR-3 fold S10, DD § 5
        root = self.submit(draft())
        digest = lambda a: self.relay(a.relay_id)["meta_sha256"]
        by_handle = self.submit(draft(parent="#1", refs=("#1",)))
        by_id = self.submit(draft(parent=root.relay_id, refs=("#1@p",)))
        self.assertEqual(digest(by_handle), digest(by_id))
        other = self.submit(draft(parent="#1", refs=("#2",)))
        self.assertNotEqual(digest(by_handle), digest(other))
        with tempfile.TemporaryDirectory() as name:
            home = Path(os.path.realpath(name))
            (home / "n.txt").write_bytes(b"n\n")
            with mock.patch.object(submit, "_home", return_value=str(home)):
                tilde = self.submit(draft(refs=("file:~/n.txt",)))
                plain = self.submit(draft(refs=("file:" + str(home / "n.txt"),)))
        self.assertEqual(digest(tilde), digest(plain))
        repo = Repo({"src/a.py": "one\ntwo\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        commit = git(repo.path, "rev-parse", "main")
        named = self.submit(draft(refs=("bivpak@main:src/a.py#L2",)))
        pinned = self.submit(draft(refs=("bivpak@%s:src/a.py#L2" % commit,)))
        self.assertEqual(digest(named), digest(pinned))
        wider = self.submit(draft(refs=("bivpak@main:src/a.py#L1-L2",)))
        self.assertNotEqual(digest(named), digest(wider))



class TestTableDirectory(Store):
    """Task 6: addresses resolve over platform's tables as the joint lock states."""

    def setUp(self):
        super().setUp()
        self.table = submit.TableDirectory(self.conn, PROJECT)

    def test_table_directory_resolves(self):
        self.assertEqual(self.table.resolve("v3-engine.implementer", PLANNER), IMPL)
        self.assertEqual(self.table.resolve("v3-engine.planner@q", PLANNER), FOREIGN)
        operator = self.table.resolve("operator", IMPL)
        self.assertEqual((operator.seat_id, operator.address, operator.role, operator.project_id),
                         (submit.OPERATOR_SEAT_ID, "operator", "operator", PROJECT))
        self.conn.execute("UPDATE team_instances SET closed_at = 5 WHERE id = 't2'")
        self.conn.execute("UPDATE seats SET state = 'stood-down' WHERE id = ?", (ORCH.seat_id,))
        for address, reason in (
                ("orchestrator", "not an address"), ("v3-engine.pair-planner", "unknown role word"),
                ("v3-engine.pair-implementer", "unknown role word"),
                ("v3-engine.reviewer", "no seat"), ("nope.planner", "no open team"),
                ("v3-other.planner", "no open team"), ("v3.orchestrator-planner", "stood down"),
                ("v3-engine.planner@zz", "no project"), ("operator@q", "operator qualified with @")):
            with self.subTest(address=address):
                with self.assertRaises(header.AddressRefused) as caught:
                    self.table.resolve(address, PLANNER)
                self.assertIn(reason, caught.exception.reason)

    # J2's reason words, one per platform reason; header.parse names them in E-ADDRESS.
    J2_REASONS = {
        addresses.Reason.NOT_AN_ADDRESS: "not an address: no .<role> part and not operator",
        addresses.Reason.AT_EMPTY_OR_REPEATED: "empty or repeated @",
        addresses.Reason.UPPERCASE: "uppercase",
        addresses.Reason.BAD_CHARACTER: "a character outside the grammar",
        addresses.Reason.UNKNOWN_ROLE: "unknown role word",
        addresses.Reason.NAME_TOO_LONG: "name too long",
        addresses.Reason.WHITESPACE_IN_TOKEN: "whitespace inside a token",
        addresses.Reason.OPERATOR_QUALIFIED: "operator qualified with @",
    }

    def test_validator_is_platform_s(self):
        """header.parse refuses what platform's validator refuses, on platform's fixture list
        (DD-v3-platform J1, J2): the default validator is platform's parse_address, and every
        refused token is E-ADDRESS with J2's reason, or a header refusal that comes first."""
        default = inspect.signature(header.parse).parameters["validate_address"].default
        self.assertIs(default, addresses.parse_address)
        self.assertEqual(set(self.J2_REASONS), set(addresses.Reason))
        self.assertEqual(set(REFUSALS), set(addresses.Reason))
        for token in ACCEPTED:
            with self.subTest(accepted=token):
                self.assertEqual(header.parse(draft(to=token)).to, (token,))
        # Tokens the header line refuses before any address is read: an empty value, and a
        # value with whitespace at its end (header grammar).
        before = {"": "E-HEADER-EMPTY", "x.planner\t": "E-HEADER-GRAMMAR"}
        for reason, tokens in REFUSALS.items():
            for token in tokens:
                with self.subTest(reason=reason, token=token):
                    with self.assertRaises(addresses.AddressRefused):
                        addresses.parse_address(token)
                    with self.assertRaises(errors.EngineError) as caught:
                        header.parse(draft(to=token))
                    if token in before:
                        self.assertEqual(caught.exception.code, before[token])
                        continue
                    self.assertEqual(caught.exception.code, "E-ADDRESS")
                    self.assertEqual(caught.exception.params["reason"], self.J2_REASONS[reason])
        self.assertEqual(set(before), {token for tokens in REFUSALS.values() for token in tokens
                                       if token in before})

    def test_header_keeps_no_address_grammar(self):
        """J1, J-T1: the parser calls the validator and keeps no grammar of its own."""
        for name in ("ROLE_WORDS", "_ALIASES", "syntactic_address", "_address_problem"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(header, name))
        source = Path(header.__file__).read_text(encoding="utf-8")
        for word in ("pair-planner", "orchestrator-reviewer", "domain-planner"):
            with self.subTest(word=word):
                self.assertNotIn(word, source)

    def test_alias_names_canonical_word(self):
        with self.assertRaises(header.AddressRefused) as caught:
            self.table.resolve("v3-engine.pair-planner", PLANNER)
        self.assertEqual(caught.exception.canonical, "planner")

    def test_reopened_team_resolves_to_new_instance(self):
        self.conn.execute("UPDATE team_instances SET closed_at = 5 WHERE id = 't2'")
        self.conn.execute("INSERT INTO team_instances (id, project_id, name, level, opened_at,"
                          " opened_by) VALUES ('t2b', 'p', 'v3-other', 'pair', 6, 'operator')")
        self.conn.execute("INSERT INTO seats (id, team_instance_id, role, state) VALUES"
                          " ('seat-new', 't2b', 'planner', 'vacant')")
        seat = self.table.resolve("v3-other.planner", PLANNER)
        self.assertEqual((seat.seat_id, seat.team_instance_id), ("seat-new", "t2b"))

    def test_checkout(self):
        self.assertIsNone(self.table.checkout("bivpak"))
        self.conn.executemany("INSERT INTO repository_checkouts (repository_id, path,"
                              " last_seen_at, checkout_id, route_created_at) VALUES ('repo-1', ?, ?, 'table-checkout', 1)",
                              [("/w/old", 1), ("/w/new", 9)])
        self.assertEqual(self.table.checkout("bivpak"), Path("/w/new"))
        self.conn.execute("UPDATE repository_checkouts SET ended_at = 10 WHERE path = '/w/new'")
        self.assertEqual(self.table.checkout("bivpak"), Path("/w/old"))
        self.conn.execute("UPDATE repository_checkouts SET ended_at = 11 WHERE path = '/w/old'")
        self.assertIsNone(self.table.checkout("bivpak"))
        self.assertIsNone(self.table.checkout("nope"))
        self.assertTrue(self.table.has_repository("bivpak"))
        self.assertFalse(self.table.has_repository("nope"))
        self.assertIsNone(submit.TableDirectory(self.conn, "q").checkout("bivpak"))

    def test_submit_through_tables(self):
        admitted = submit.submit(self.conn, submit.SubmitContext(
            PLANNER, GIT_STATE, self.now, self.table, submission_key="table-directory"),
            draft(to="v3-engine.implementer", cc="operator"))
        rows = self.conn.execute("SELECT seat_id, special, role FROM recipients WHERE relay_id = ?"
                                 " ORDER BY role DESC", (admitted.relay_id,)).fetchall()
        self.assertEqual(rows, [(IMPL.seat_id, None, "to"), (None, "operator", "cc")])


class TestParties(Store):
    """Planner Q1: a party is a seat or a special, written to its exclusive arc."""

    def test_operator_and_system_as_parties(self):
        question = self.submit(draft(kind="QUESTION", to="operator", needs="answer"), sender=IMPL)
        answer = self.submit(draft(parent="#%d" % question.handle, to="v3-engine.implementer"),
                             sender=SEATS["operator"])
        self.assertEqual(self.conn.execute(
            "SELECT seat_id, special, answered_by FROM recipients WHERE relay_id = ?",
            (question.relay_id,)).fetchall(), [(None, "operator", answer.relay_id)])
        self.assertEqual(self.conn.execute(
            "SELECT sender_seat_id, sender_special FROM relays WHERE id = ?",
            (answer.relay_id,)).fetchone(), (None, "operator"))
        self.assertEqual(self.conn.execute(
            "SELECT opened_by_seat_id, opened_by_special FROM bundles").fetchall(),
            [(IMPL.seat_id, None)])
        self.refused("E-PARENT-UNKNOWN", draft(parent="#9"), sender=SEATS["operator"])
        self.assertEqual(self.conn.execute(
            "SELECT seat_id, special FROM refusals").fetchall(), [(None, "operator")])
        self.assertEqual(submit.party_columns("system"), (None, "system"))
        from relay_engine import queries
        queries.read(self.conn, question.relay_id, "operator", 77)  # the operator reads
        self.assertEqual(self.conn.execute(
            "SELECT opened_at FROM recipients WHERE relay_id = ?", (question.relay_id,)).fetchone(),
            (77,))
        self.assertEqual(self.conn.execute("SELECT seat_id FROM seat_activity").fetchall(),
                         [(IMPL.seat_id,)])  # the QUESTION's admission (PR-3 R6); none for the operator
        self.assertEqual(submit.party_columns(IMPL.seat_id), (IMPL.seat_id, None))

    def test_unknown_repository_refused(self):
        self.directory.checkouts["nope"] = "/x"
        error = self.refused("E-REF-UNRESOLVED", draft(refs=("nope@main:a.py",)), line=6)
        self.assertIn("not known to this project", error.cause)

    def test_known_repository_without_open_checkout_refused(self):
        error = self.refused("E-REF-UNRESOLVED", draft(refs=("bivpak@main:a.py",)), line=6)
        self.assertIn("repository bivpak has no registered checkout in this project", error.cause)
        self.assertNotIn("not known to this project", error.cause)


class TestAdmissionReplay(Store):
    """Breaks: lost receipt, party collisions, poisoned keys and missed lock recheck."""

    def require_replay(self):
        self.assertIn("submission_key", submit.SubmitContext.__dataclass_fields__,
                      "external admission requires the reviewed submission_key field")

    def full_state(self, conn=None):
        conn = self.conn if conn is None else conn
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table: sorted(conn.execute('SELECT * FROM "' + table + '"').fetchall(),
                              key=repr) for table in tables}

    def test_replay_returns_exact_receipt(self):
        self.require_replay()
        data = draft(subject="original")
        first = self.submit(data, submission_key="retry")
        state = self.full_state()
        self.clock += 500
        self.assertEqual(self.submit(data, submission_key="retry"), first)
        self.assertEqual(self.full_state(), state)
        for changed in (draft(subject="changed"), b"not a valid header\n"):
            before = self.full_state()
            with self.assertRaises(errors.EngineError) as caught:
                self.submit(changed, submission_key="retry")
            self.assertEqual(caught.exception.code, "E-SUBMISSION-KEY-REUSED")
            after = self.full_state()
            self.assertEqual(len(after.pop("refusals")), len(before.pop("refusals")) + 1)
            self.assertEqual(after, before)
            self.assertEqual(self.conn.execute(
                "SELECT code, draft_sha256 FROM refusals ORDER BY id DESC LIMIT 1"
            ).fetchone(), ("E-SUBMISSION-KEY-REUSED", hashlib.sha256(changed).hexdigest()))

    def test_replay_party_uniqueness(self):
        self.require_replay()
        data = draft(to="operator")
        receipts = []
        for party in (PLANNER, IMPL, SEATS["operator"]):
            first = self.submit(data, sender=party, submission_key="shared")
            state = self.full_state()
            self.assertEqual(self.submit(data, sender=party, submission_key="shared"), first)
            self.assertEqual(self.full_state(), state)
            receipts.append(first.relay_id)
        self.assertEqual(len(set(receipts)), 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM relays").fetchone(), (3,))
        self.assertEqual(self.conn.execute(
            "SELECT sender_seat_id, sender_special FROM relays WHERE id=?",
            (receipts[-1],)).fetchone(), (None, "operator"))

    def test_replay_refusal_does_not_reserve_key(self):
        self.require_replay()
        bad = draft(parent="#999")
        with self.assertRaises(errors.EngineError) as caught:
            self.submit(bad, submission_key="correctable")
        self.assertEqual(caught.exception.code, "E-PARENT-UNKNOWN")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM relays").fetchone(), (0,))
        good = self.submit(draft(), submission_key="correctable")
        self.assertEqual(good.handle, 1)
        self.assertEqual(self.conn.execute("SELECT code, draft_sha256 FROM refusals").fetchall(),
                         [("E-PARENT-UNKNOWN", hashlib.sha256(bad).hexdigest())])
        state = self.full_state()
        self.assertEqual(self.submit(draft(), submission_key="correctable"), good)
        self.assertEqual(self.full_state(), state)

    def test_replay_serialized_connections(self):
        self.require_replay()

        class SchedulingConnection(sqlite3.Connection):
            before_begin = None

            def execute(connection, sql, parameters=()):
                callback = connection.before_begin
                if sql == "BEGIN IMMEDIATE" and callback is not None:
                    connection.before_begin = None
                    callback()
                return super().execute(sql, parameters)

        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as name:
                path = str(Path(name) / "replay.sqlite")
                first = sqlite3.connect(path, factory=SchedulingConnection)
                second = None
                try:
                    first.execute("PRAGMA foreign_keys = ON")
                    self.assertEqual(first.execute("PRAGMA journal_mode=WAL").fetchone(),
                                     ("wal",))
                    schema.init_v3(first)
                    platform_rows(first)
                    second = submit.open_store(path)
                    data = draft(subject="winner")
                    incoming = draft(subject="loser") if conflict else data
                    winner = []
                    second_ctx = self.ctx(submission_key="race", git_state=None)
                    first_ctx = self.ctx(submission_key="race", git_state=None)
                    first.before_begin = lambda: winner.append(
                        submit.submit(second, second_ctx, data))
                    if conflict:
                        with self.assertRaises(errors.EngineError) as caught:
                            submit.submit(first, first_ctx, incoming)
                        self.assertEqual(caught.exception.code, "E-SUBMISSION-KEY-REUSED")
                    else:
                        receipt = submit.submit(first, first_ctx, incoming)
                        self.assertEqual(receipt, winner[0])
                    self.assertEqual(len(winner), 1)
                    self.assertEqual(first.execute("SELECT count(*) FROM relays").fetchone(), (1,))
                    self.assertEqual(first.execute("SELECT count(*) FROM bodies").fetchone(), (1,))
                    self.assertEqual(first.execute("SELECT count(*) FROM refusals").fetchone(),
                                     (int(conflict),))
                    state = self.full_state(first)
                    self.assertEqual(submit.submit(first, first_ctx, data), winner[0])
                    self.assertEqual(self.full_state(first), state)
                    self.assertFalse(first.in_transaction)
                    self.assertFalse(second.in_transaction)
                finally:
                    if second is not None:
                        second.close()
                    first.close()

    def test_key_shape_and_digest(self):
        self.require_replay()
        with self.assertRaises(TypeError):
            submit.SubmitContext(PLANNER, GIT_STATE, self.now, self.directory)
        for invalid in (None, "", False, 1, b"key", [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                submit.SubmitContext(PLANNER, GIT_STATE, self.now, self.directory,
                                     submission_key=invalid)
        self.assertEqual(self.full_state()["relays"], [])
        data = draft(cc="operator", body="exact original bytes\n")
        receipt = self.submit(data, submission_key="digest")
        self.assertEqual(self.conn.execute(
            "SELECT submission_key, draft_sha256 FROM relays WHERE id=?",
            (receipt.relay_id,)).fetchone(),
            ("digest", hashlib.sha256(data).hexdigest()))

    def test_sql_fixture_does_not_normalize_environment(self):
        # The real fixture must succeed while normalization/mutation fail closed.
        with mock.patch(__name__ + ".clean_git_environment",
                        side_effect=AssertionError("fixture normalized environment")), \
                mock.patch.object(os, "putenv", side_effect=AssertionError("putenv")), \
                mock.patch.object(os, "unsetenv", side_effect=AssertionError("unsetenv")):
            fixture = Store("runTest")
            try:
                fixture.setUp()
                self.assertEqual(fixture.conn.execute("PRAGMA foreign_keys").fetchone(), (1,))
                self.assertEqual(fixture.conn.execute("SELECT count(*) FROM projects").fetchone(),
                                 (2,))
                self.assertEqual(fixture.conn.execute(
                    "SELECT count(*) FROM schema_version").fetchone(), (1,))
                self.assertEqual(fixture.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                fixture.doCleanups()
        self.assertNotIn("clean_git_environment(", inspect.getsource(Store.setUp))

    def test_fixture_keys_distinguish_new_and_retry(self):
        self.require_replay()
        a, b = self.ctx(), self.ctx()
        self.assertTrue(a.submission_key)
        self.assertTrue(b.submission_key)
        self.assertNotEqual(a.submission_key, b.submission_key)
        first = submit.submit(self.conn, a, draft())
        second = submit.submit(self.conn, b, draft())
        self.assertNotEqual(first.relay_id, second.relay_id)
        explicit = self.ctx(submission_key="intentional-retry")
        receipt = submit.submit(self.conn, explicit, draft())
        state = self.full_state()
        self.assertEqual(submit.submit(
            self.conn, self.ctx(submission_key="intentional-retry"), draft()), receipt)
        self.assertEqual(self.full_state(), state)
        with self.assertRaises(ValueError):
            submit.SubmitContext(PLANNER, None, self.now, self.directory, submission_key="")



class TestAdmissionHook(Store):
    """Breaks: post-commit callbacks, cached authority, partial writes and unsafe errors."""

    def require_hook(self):
        self.assertIn("after_admit", submit.SubmitContext.__dataclass_fields__,
                      "atomic admission requires the reviewed pre-commit hook")

    def hook_ctx(self, hook, writes=frozenset(), **kwargs):
        return replace(self.ctx(**kwargs), after_admit=hook, after_admit_writes=writes)

    def canaries(self):
        self.conn.executescript(
            "CREATE TABLE fixture_owned (id TEXT PRIMARY KEY, relay_id TEXT REFERENCES relays(id));"
            "CREATE TABLE fixture_other (id TEXT PRIMARY KEY);")
        patch = mock.patch.dict(schema.TABLE_OWNERS, {"fixture_owned": "authority"})
        patch.start()
        self.addCleanup(patch.stop)

    def assert_free(self):
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone(), (1,))
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS fixture_cleanup (n INTEGER)")
        self.conn.execute("BEGIN")
        self.conn.execute("ROLLBACK")

    def reject_hook(self, hook, code="E-ADMISSION-HOOK"):
        before = self.counts()
        owned = self.conn.execute("SELECT * FROM fixture_owned ORDER BY id").fetchall()
        other = self.conn.execute("SELECT * FROM fixture_other ORDER BY id").fetchall()
        activity = self.conn.execute("SELECT * FROM seat_activity ORDER BY seat_id").fetchall()
        with self.assertRaises(errors.EngineError) as caught:
            submit.submit(self.conn, self.hook_ctx(hook, frozenset({"fixture_owned"})), draft())
        self.assertEqual(caught.exception.code, code)
        after = self.counts()
        self.assertEqual(after.pop("refusals"), before.pop("refusals") + 1)
        self.assertEqual(after, before)
        self.assertEqual(self.conn.execute("SELECT * FROM fixture_owned ORDER BY id").fetchall(), owned)
        self.assertEqual(self.conn.execute("SELECT * FROM fixture_other ORDER BY id").fetchall(), other)
        self.assertEqual(self.conn.execute("SELECT * FROM seat_activity ORDER BY seat_id").fetchall(), activity)
        self.assert_free()
        return caught.exception

    def test_hook_timing_and_same_transaction(self):
        self.require_hook()
        request = self.submit(draft(kind="REVIEW", needs="review", to=IMPL.address))
        seen = []
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "check.txt"
            path.write_bytes(b"real check\n")
            def hook(conn, admitted, parsed, ctx):
                self.assertIs(conn, self.conn)
                self.assertTrue(conn.in_transaction)
                self.assertEqual(conn.execute("SELECT subject FROM relays WHERE id=?",
                                             (admitted.relay_id,)).fetchone(), ("checked",))
                self.assertEqual(conn.execute("SELECT raised_in FROM findings").fetchall(),
                                 [(admitted.relay_id,)])
                self.assertEqual(conn.execute("SELECT answered_by FROM recipients WHERE relay_id=?",
                                             (request.relay_id,)).fetchone(), (admitted.relay_id,))
                self.assertEqual(conn.execute("SELECT last_request_at FROM seat_activity WHERE seat_id=?",
                                             (IMPL.seat_id,)).fetchone(), (admitted.created_at_ms,))
                self.assertEqual(parsed.subject, "checked")
                seen.append(admitted)
            ctx = self.hook_ctx(hook, sender=IMPL, submission_key="hook-retry")
            data = draft(kind="REVIEW", to=PLANNER.address, parent="#%d" % request.handle,
                         verdict="must-revise", subject="checked", refs=("file:" + str(path),),
                         findings=("blocking 1 actual check",))
            got = submit.submit(self.conn, ctx, data)
            self.assertEqual(seen, [got])
            self.assertEqual(submit.submit(self.conn, ctx, data), got)
            with self.assertRaises(errors.EngineError):
                submit.submit(self.conn, replace(ctx, submission_key="bad"), b"bad")
            self.assertEqual(seen, [got])
        self.assert_free()

    def test_hook_declared_owned_fk_atomic(self):
        self.require_hook()
        self.canaries()
        def good(conn, admitted, parsed, ctx):
            conn.execute("INSERT INTO fixture_owned VALUES (?,?)", ("positive", admitted.relay_id))
        admitted = submit.submit(self.conn, self.hook_ctx(good, frozenset({"fixture_owned"})), draft())
        self.assertEqual(self.conn.execute("SELECT * FROM fixture_owned").fetchall(),
                         [("positive", admitted.relay_id)])
        self.assert_free()
        def bad(conn, admitted, parsed, ctx):
            conn.execute("INSERT INTO fixture_owned VALUES (?,?)", ("negative", admitted.relay_id))
            raise RuntimeError("must not survive")
        self.reject_hook(bad)

    def test_hook_engineerror_refusal(self):
        self.require_hook()
        self.canaries()
        original = errors.error_for("E-SUBMISSION-KEY-REUSED")
        def typed(conn, admitted, parsed, ctx):
            conn.execute("INSERT INTO fixture_owned VALUES (?,?)", ("typed", admitted.relay_id))
            submit.file_system_relay(conn, ctx, draft(bundle="system"))
            raise original
        self.assertIs(self.reject_hook(typed, original.code), original)
        def plain(conn, admitted, parsed, ctx):
            conn.execute("INSERT INTO fixture_owned VALUES (?,?)", ("plain", admitted.relay_id))
            submit.file_system_relay(conn, ctx, draft(bundle="system"))
            raise RuntimeError("PRIVATE-MESSAGE-MUST-NOT-ESCAPE")
        error = self.reject_hook(plain)
        self.assertEqual(error.params["kind"], "RuntimeError")
        self.assertNotIn("PRIVATE-MESSAGE", repr(error.as_dict()) + repr(error.params))
        self.assertEqual(self.conn.execute("SELECT code FROM refusals ORDER BY id").fetchall(),
                         [(original.code,), ("E-ADMISSION-HOOK",)])

    def test_hook_warm_transaction_escapes(self):
        self.require_hook()
        self.canaries()
        self.conn.executescript(
            "BEGIN; COMMIT; BEGIN; ROLLBACK; begin; commit; SAVEPOINT warmed; RELEASE warmed;")
        self.conn.execute("ATTACH ':memory:' AS warmed")
        self.conn.execute("DETACH warmed")
        self.conn.execute("PRAGMA user_version")
        self.conn.execute("CREATE TABLE fixture_ddl (n INTEGER)")
        self.conn.execute("DROP TABLE fixture_ddl")
        def commit_begin(c, ctx):
            c.commit()
            c.execute("BEGIN")
        def rollback(c, ctx):
            c.rollback()
        def savepoint(c, ctx):
            c.execute("SAVEPOINT warmed")
            c.execute("RELEASE warmed")
        operations = [
            ("COMMIT", lambda c, x: c.execute("COMMIT")),
            ("COMMIT; BEGIN", lambda c, x: c.executescript("COMMIT; BEGIN")),
            ("conn.commit();BEGIN", commit_begin),
            ("ROLLBACK;BEGIN", lambda c, x: c.executescript("ROLLBACK; BEGIN")),
            ("lowercase", lambda c, x: c.executescript("commit; begin")),
            ("conn.rollback", rollback),
            ("SAVEPOINT/RELEASE", savepoint),
            ("ATTACH", lambda c, x: c.execute("ATTACH ':memory:' AS warmed")),
            ("DETACH", lambda c, x: c.execute("DETACH warmed")),
            ("PRAGMA", lambda c, x: c.execute("PRAGMA user_version")),
            ("DDL", lambda c, x: c.execute("CREATE TABLE fixture_ddl (n INTEGER)")),
            ("nested", lambda c, x: submit.submit(c, x, draft(subject="nested"))),
        ]
        for number, (label, operation) in enumerate(operations):
            with self.subTest(escape=label):
                def hook(conn, admitted, parsed, ctx):
                    conn.execute("INSERT INTO fixture_owned VALUES (?,?)", ("escape-%d" % number, admitted.relay_id))
                    operation(conn, ctx)
                try:
                    outcome = self.reject_hook(hook)
                except BaseException as error:
                    outcome = error
                self.assertIsInstance(outcome, errors.EngineError)

    def test_hook_write_set_trigger_cascade(self):
        self.require_hook()
        self.canaries()
        existing = self.submit(draft(subject="original"))
        self.conn.executescript(
            "CREATE TABLE fixture_parent (id TEXT PRIMARY KEY);"
            "CREATE TABLE fixture_child (id TEXT PRIMARY KEY, parent TEXT REFERENCES"
            " fixture_parent(id) ON DELETE CASCADE);"
            "INSERT INTO fixture_parent VALUES ('p'); INSERT INTO fixture_child VALUES ('c','p');")
        with mock.patch.dict(schema.TABLE_OWNERS, {"fixture_parent": "authority"}):
            operations = [
                lambda c, a: c.execute("UPDATE relays SET subject='forged'"),
                lambda c, a: c.execute("DELETE FROM relays"),
                lambda c, a: c.execute("INSERT INTO fixture_other VALUES ('forged')"),
                lambda c, a: c.execute("DELETE FROM fixture_parent WHERE id='p'"),
            ]
            for operation in operations:
                with self.subTest(operation=operations.index(operation)):
                    def hook(c, a, d, x):
                        operation(c, a)
                    before = self.counts()
                    with self.assertRaises(errors.EngineError) as caught:
                        submit.submit(self.conn, self.hook_ctx(
                            hook, frozenset({"fixture_owned", "fixture_parent"})), draft())
                    self.assertEqual(caught.exception.code, "E-ADMISSION-HOOK")
                    self.assertEqual(caught.exception.params["kind"], "DatabaseError")
                    after = self.counts()
                    self.assertEqual(after.pop("refusals"), before.pop("refusals") + 1)
                    self.assertEqual(after, before)
                    self.assertEqual(self.relay(existing.relay_id)["subject"], "original")
                    self.assertEqual(self.conn.execute("SELECT * FROM fixture_child").fetchall(), [("c", "p")])
                    self.assert_free()
        for target_sql in ("UPDATE relays SET subject='triggered'",
                           "INSERT INTO fixture_other VALUES ('triggered')"):
            with self.subTest(trigger=target_sql):
                self.conn.execute("CREATE TRIGGER fixture_trigger AFTER INSERT ON fixture_owned"
                                  " BEGIN " + target_sql + "; END")
                def hook(c, a, d, x):
                    c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("trigger", a.relay_id))
                self.assertEqual(self.reject_hook(hook).params["kind"], "DatabaseError")
                self.conn.execute("DROP TRIGGER fixture_trigger")
        def direct(c, a, d, x):
            c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("direct", a.relay_id))
        submit.submit(self.conn, self.hook_ctx(direct, frozenset({"fixture_owned"})), draft())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM fixture_owned").fetchone(), (1,))
        self.assert_free()

    def test_system_relay_hook_only_atomic(self):
        self.require_hook()
        self.canaries()
        calls, inner = [], []
        def hook(c, a, d, x):
            calls.append(a)
            inner.append(submit.file_system_relay(c, x, draft(bundle="system")))
        ctx = self.hook_ctx(hook)
        outer = submit.submit(self.conn, ctx, draft())
        self.assertEqual(calls, [outer])
        self.assertEqual(len(inner), 1)
        row = self.relay(inner[0].relay_id)
        self.assertEqual((row["sender_seat_id"], row["sender_special"],
                          row["submission_key"], row["draft_sha256"]), (None, "system", None, None))
        with self.assertRaises(RuntimeError):
            submit.file_system_relay(self.conn, ctx, draft())
        self.assert_free()
        for data, code in [
            (draft(to="v3-absent.planner"), "E-ADDRESS"),
            (draft(parent="#999999"), "E-PARENT-UNKNOWN"),
            (draft(parent="#%d" % outer.handle), "E-PARENT-NOT-MEMBER"),
            (draft(refs=("not-a-reference",)), "E-REF-SPELLING"),
        ]:
            with self.subTest(code=code):
                def bad(c, a, d, x):
                    submit.file_system_relay(c, x, draft(bundle="system-first"))
                    submit.file_system_relay(c, x, data)
                self.reject_hook(bad, code)

    def test_system_relay_cached_sql_not_forgeable(self):
        self.require_hook()
        class RecordingConnection(sqlite3.Connection):
            def execute(conn, sql, params=()):
                if sql.startswith("INSERT INTO relays "):
                    conn.last_relay_insert = (sql, params)
                return super().execute(sql, params)
        self.conn = sqlite3.connect(":memory:", factory=RecordingConnection)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        schema.init_v3(self.conn)
        platform_rows(self.conn)
        self.canaries()
        attempted = []
        def hook(c, a, d, x):
            submit.file_system_relay(c, x, draft(bundle="system"))
            sql, params = c.last_relay_insert
            forged = list(params)
            forged[0], forged[1], forged[8], forged[13] = "forged", 999999, "forged", self.clock + 1000000
            attempted.append(sql)
            c.execute(sql, forged)  # identical cached engine INSERT text, new operands
        self.assertEqual(self.reject_hook(hook).params["kind"], "DatabaseError")
        self.assertEqual(len(attempted), 1)
        self.assertTrue(attempted[0].startswith("INSERT INTO relays "))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM relays WHERE id='forged'").fetchone(), (0,))

    def test_hook_baseexception_and_cleanup(self):
        self.require_hook()
        self.canaries()
        for signal in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(signal=type(signal).__name__):
                before = self.counts()
                def hook(c, a, d, x):
                    c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("signal", a.relay_id))
                    submit.file_system_relay(c, x, draft(bundle="system"))
                    raise signal
                with self.assertRaises(type(signal)) as caught:
                    submit.submit(self.conn, self.hook_ctx(hook, frozenset({"fixture_owned"})), draft())
                self.assertIs(caught.exception, signal)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.conn.execute("SELECT * FROM fixture_owned").fetchall(), [])
                self.assert_free()
        submit.submit(self.conn, self.hook_ctx(lambda c, a, d, x: None), draft())
        self.assert_free()
        with mock.patch.object(submit, "_write_transitions", side_effect=RuntimeError("engine fault")):
            with self.assertRaises(RuntimeError):
                self.submit(draft())
        self.assert_free()

    def test_hook_ended_transaction_guard(self):
        self.require_hook()
        self.canaries()
        self.conn.execute("INSERT INTO fixture_owned VALUES ('duplicate',NULL)")
        self.conn.commit()
        states = []
        def hook(c, a, d, x):
            # SQLite itself rolls the transaction back on this conflict.
            try:
                c.execute("INSERT OR ROLLBACK INTO fixture_owned VALUES ('duplicate',?)", (a.relay_id,))
            except sqlite3.DatabaseError:
                pass
            states.append(c.in_transaction)
        try:
            outcome = self.reject_hook(hook)
        except BaseException as error:
            outcome = error
        self.assertEqual(states, [False])
        self.assertIsInstance(outcome, errors.EngineError)
        self.assertEqual(outcome.cause_key, "error-admission-hook-ended-transaction-cause")
        self.assertNotIn("kind", outcome.params)

    def test_hook_ended_transaction_guard_uncaught(self):
        self.require_hook()
        self.canaries()
        self.conn.execute("INSERT INTO fixture_owned VALUES ('duplicate',NULL)")
        self.conn.commit()
        tables = (*TABLES, "fixture_owned", "fixture_other")
        before = {table: self.conn.execute("SELECT * FROM " + table).fetchall()
                  for table in tables}
        def hook(c, a, d, x):
            c.execute("INSERT OR ROLLBACK INTO fixture_owned VALUES ('duplicate',?)",
                      (a.relay_id,))
        try:
            outcome = submit.submit(
                self.conn, self.hook_ctx(hook, frozenset({"fixture_owned"})), draft())
        except BaseException as error:
            outcome = error
        self.assertIsInstance(outcome, errors.EngineError)
        self.assertEqual(outcome.code, "E-ADMISSION-HOOK")
        self.assertEqual(outcome.cause_key, "error-admission-hook-ended-transaction-cause")
        self.assertNotIn("kind", outcome.params)
        for table, rows in before.items():
            after = self.conn.execute("SELECT * FROM " + table).fetchall()
            if table == "refusals":
                self.assertEqual(len(after), len(rows) + 1)
                self.assertEqual(after[:-1], rows)
            else:
                self.assertEqual(after, rows, table)
        self.assertEqual(self.conn.execute(
            "SELECT code FROM refusals ORDER BY id DESC LIMIT 1").fetchone(),
            ("E-ADMISSION-HOOK",))
        self.assert_free()

    def test_hook_allowed_actions_admit(self):
        self.require_hook()
        self.canaries()
        expected_recursive = [(n,) for n in range(1, 4)]
        expected_count = self.conn.execute("SELECT count(*) FROM relays").fetchone()[0] + 1
        seen = {}
        def hook(c, a, d, x):
            c.execute("INSERT INTO fixture_owned VALUES ('updated',?)", (a.relay_id,))
            c.execute("INSERT INTO fixture_owned VALUES ('deleted',?)", (a.relay_id,))
            c.execute("UPDATE fixture_owned SET id='kept' WHERE id='updated'")
            c.execute("DELETE FROM fixture_owned WHERE id='deleted'")
            seen["recursive"] = c.execute(
                "WITH RECURSIVE seq(n) AS "
                "(SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<3) "
                "SELECT n FROM seq ORDER BY n").fetchall()
            seen["count"] = c.execute("SELECT count(*) FROM relays").fetchone()[0]
        try:
            outcome = submit.submit(
                self.conn, self.hook_ctx(hook, frozenset({"fixture_owned"})), draft())
        except BaseException as error:
            outcome = error
        self.assertIsInstance(outcome, submit.Admitted)
        self.assertEqual(seen.get("recursive"), expected_recursive)
        self.assertEqual(seen.get("count"), expected_count)
        self.assertEqual(
            self.conn.execute("SELECT * FROM fixture_owned ORDER BY id").fetchall(),
            [("kept", outcome.relay_id)])
        self.assert_free()

    def test_hook_default_preserves_existing(self):
        self.require_hook()
        ctx = self.ctx()
        self.assertIsNone(ctx.after_admit)
        self.assertEqual(ctx.after_admit_writes, frozenset())
        got = submit.submit(self.conn, ctx, draft())
        self.assertEqual(self.relay(got.relay_id)["subject"], "s")
        self.assert_free()


class TestAdmissionHookAtomicity(Store):
    """Breaks: post-rollback durability, partial internal filing and unsafe hook errors."""

    def test_submit_without_autocommit_attribute_admits(self):
        # Breaks: admission reads the 3.12-only attribute on a 3.11 connection.
        class WithoutAutocommit(sqlite3.Connection):
            @property
            def autocommit(self):
                raise AttributeError("autocommit is unavailable")

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "legacy.db"
            conn = sqlite3.connect(path, factory=WithoutAutocommit)
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                schema.init_v3(conn)
                platform_rows(conn)
                receipt = submit.submit(conn, self.ctx(), draft())
            finally:
                conn.close()
            fresh = sqlite3.connect(path)
            try:
                self.assertEqual(fresh.execute("SELECT count(*) FROM relays").fetchone(), (1,))
                self.assertEqual(fresh.execute("SELECT id FROM relays").fetchone(),
                                 (receipt.relay_id,))
            finally:
                fresh.close()

    def file_store(self, path, *, mode="legacy", conflict_rollback=False):
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=ON")
        schema.init_v3(conn)
        platform_rows(conn)
        conflict = " ON CONFLICT ROLLBACK" if conflict_rollback else ""
        conn.executescript(
            "CREATE TABLE fixture_owned (id TEXT PRIMARY KEY" + conflict
            + ", relay_id TEXT REFERENCES relays(id));"
            "INSERT INTO fixture_owned VALUES ('duplicate',NULL);")
        if mode == "isolation-none":
            conn.isolation_level = None
        elif mode == "autocommit-true":
            conn.autocommit = True
        elif mode == "autocommit-false":
            conn.autocommit = False
        return conn

    def file_state(self, path):
        conn = sqlite3.connect(path)
        try:
            return {table: conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                    for table in TABLES + ("fixture_owned",)}
        finally:
            conn.close()

    def assert_file_free(self, conn):
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone(), (1,))
        conn.execute("BEGIN")
        conn.execute("ROLLBACK")

    def test_hook_rows_never_outlive_rolled_back_admission(self):
        # A cached VM must not write after SQLite rolls back the admission.
        observed, expected = [], []
        modes = ("legacy", "isolation-none")
        if hasattr(sqlite3.Connection, "autocommit"):
            modes += ("autocommit-true",)
        attacks = ("fresh-cte", "cached-cte", "table-rollback", "plain")
        cte = "WITH value(id,relay_id) AS (VALUES (?,?)) INSERT INTO fixture_owned SELECT * FROM value"
        with tempfile.TemporaryDirectory() as name, mock.patch.dict(
                schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            for mode in modes:
                path = Path(name) / (mode + "-control.db")
                conn = self.file_store(path, mode=mode)
                def control(c, a, d, x):
                    c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("control", a.relay_id))
                ctx = replace(self.ctx(), after_admit=control,
                              after_admit_writes=frozenset({"fixture_owned"}))
                submit.submit(conn, ctx, draft())
                state = self.file_state(path)
                self.assertEqual((state["relays"], state["fixture_owned"], state["refusals"]),
                                 (1, 2, 0))
                self.assert_file_free(conn)
                for attack in attacks:
                    path = Path(name) / (mode + "-" + attack + ".db")
                    conn = self.file_store(path, mode=mode,
                                           conflict_rollback=attack == "table-rollback")
                    before = self.file_state(path)
                    def hook(c, a, d, x):
                        if attack == "cached-cte":
                            c.execute(cte, ("warmed", a.relay_id))
                        sql = ("INSERT INTO fixture_owned VALUES ('duplicate',NULL)"
                               if attack == "table-rollback" else
                               "INSERT OR ROLLBACK INTO fixture_owned VALUES ('duplicate',NULL)")
                        try:
                            c.execute(sql)
                        except sqlite3.DatabaseError:
                            pass
                        try:
                            c.execute("INSERT INTO fixture_owned VALUES (?,?)"
                                      if attack == "plain" else cte, ("after", None))
                        except sqlite3.DatabaseError:
                            pass
                    ctx = replace(self.ctx(), after_admit=hook,
                                  after_admit_writes=frozenset({"fixture_owned"}))
                    refusal = None
                    try:
                        submit.submit(conn, ctx, draft())
                    except BaseException as error:
                        refusal = error.code if isinstance(error, errors.EngineError) else type(error).__name__
                    state = self.file_state(path)
                    want = dict(before)
                    want["refusals"] += 1
                    observed.append((mode, attack, refusal, state))
                    expected.append((mode, attack, "E-ADMISSION-HOOK", want))
                    self.assert_file_free(conn)
            if hasattr(sqlite3.Connection, "autocommit"):
                path = Path(name) / "unsupported.db"
                conn = self.file_store(path, mode="autocommit-false")
                before, statements = self.file_state(path), []
                conn.set_trace_callback(statements.append)
                outcome = None
                try:
                    submit.submit(conn, self.ctx(), draft())
                except (ValueError, sqlite3.OperationalError) as error:
                    outcome = type(error).__name__
                finally:
                    conn.set_trace_callback(None)
                observed.append(("autocommit-false", outcome, statements, self.file_state(path)))
                expected.append(("autocommit-false", "ValueError", [], before))
            else:
                self.assertFalse(hasattr(self.conn, "autocommit"))
        self.assertEqual(observed, expected,
                         "hook writes must not survive a rolled-back admission in any supported mode")

    def test_system_filing_failure_leaves_no_rows(self):
        # A caught internal FK failure must not commit its earlier table writes.
        snapshots, filed = [], []
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "caught.db"
            conn = self.file_store(path)
            check = Path(name) / "check.txt"
            check.write_text("real finding evidence\n")
            bad = draft(kind="REVIEW", verdict="must-revise", bundle="failed-system", subject="failed-system",
                        refs=("file:" + str(check),), findings=("blocking 1 failed filing",))
            def hook(c, a, d, x):
                before = {table: c.execute("SELECT count(*) FROM " + table).fetchone()[0]
                          for table in TABLES}
                try:
                    submit.file_system_relay(c, x, bad)
                except sqlite3.IntegrityError:
                    pass
                else:
                    self.fail("the system finding must fail its real seat foreign key")
                after = {table: c.execute("SELECT count(*) FROM " + table).fetchone()[0]
                         for table in TABLES}
                snapshots.append((before, after))
                filed.append(submit.file_system_relay(
                    c, x, draft(bundle="valid-system", subject="valid-system")))
            # This atomicity fixture deliberately counts every table, including bodies.
            submit.submit(conn, replace(self.ctx(), after_admit=hook,
                                        after_admit_reads_denied=frozenset()), draft())
            self.assertEqual(len(filed), 1)
            reader = sqlite3.connect(path)
            try:
                self.assertEqual(reader.execute(
                    "SELECT count(*) FROM relays WHERE subject='valid-system'").fetchone(), (1,))
                failed_rows = reader.execute(
                    "SELECT count(*) FROM relays WHERE subject='failed-system'").fetchone()[0]
            finally:
                reader.close()
            self.assert_file_free(conn)
            control_path = Path(name) / "propagated.db"
            control = self.file_store(control_path)
            before = self.file_state(control_path)
            def propagates(c, a, d, x):
                submit.file_system_relay(c, x, bad)
            with self.assertRaises(errors.EngineError):
                submit.submit(control, replace(self.ctx(), after_admit=propagates), draft())
            want = dict(before)
            want["refusals"] += 1
            self.assertEqual(self.file_state(control_path), want)
            self.assert_file_free(control)
        self.assertEqual((snapshots[0][1], failed_rows), (snapshots[0][0], 0),
                         "caught system filing failure must roll back all of its own rows")

    def test_hook_exception_kind_fallback(self):
        observed = []
        for kind in (type("ÖFault", (Exception,), {}), type("bad-name", (Exception,), {})):
            before = self.counts()
            private_message = "PRIVATE-HOOK-PAYLOAD-739"
            def hook(c, a, d, x):
                raise kind(private_message)
            error = None
            try:
                submit.submit(self.conn, replace(self.ctx(), after_admit=hook), draft())
            except (errors.EngineError, ValueError) as caught:
                error = caught
            want = dict(before)
            want["refusals"] += 1
            observed.append((isinstance(error, errors.EngineError),
                             getattr(error, "code", None),
                             getattr(error, "params", {}).get("kind"),
                             self.counts() == want,
                             private_message not in "".join(traceback.format_exception(error))))
            TestAdmissionHook.assert_free(self)
        self.assertEqual(observed, [(True, "E-ADMISSION-HOOK", "Exception", True, True)] * 2)

    def test_hook_name_fallback(self):
        observed = []
        for raises in (True, False):
            def hook(c, a, d, x):
                if raises:
                    raise RuntimeError("private hook failure")
            hook.__module__ = None
            before = self.counts()
            outcome = None
            try:
                outcome = submit.submit(self.conn, replace(self.ctx(), after_admit=hook), draft())
            except (errors.EngineError, TypeError) as error:
                outcome = error
            if raises:
                want = dict(before)
                want["refusals"] += 1
                observed.append((getattr(outcome, "code", None),
                                 getattr(outcome, "params", {}).get("hook"),
                                 self.counts() == want))
            else:
                observed.append((isinstance(outcome, submit.Admitted),
                                 self.counts()["relays"] == before["relays"] + 1))
            TestAdmissionHook.assert_free(self)
        self.assertEqual(observed, [("E-ADMISSION-HOOK", "relay_engine.submit.unknown_hook", True),
                                    (True, True)])

    def test_hook_closing_connection_raising_discloses_nothing(self):
        private_message = "PRIVATE-CLOSED-HOOK-PAYLOAD-918"
        disclosed = []
        with tempfile.TemporaryDirectory() as name, mock.patch.dict(
                schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            for closing in (True, False):
                path = Path(name) / ("closed-raise.db" if closing else "open-raise.db")
                conn = self.file_store(path)
                before = self.file_state(path)
                def hook(c, a, d, x):
                    c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("raised", a.relay_id))
                    if closing:
                        c.close()
                    raise RuntimeError(private_message)
                outcome = None
                try:
                    outcome = submit.submit(conn, replace(self.ctx(), after_admit=hook,
                                  after_admit_writes=frozenset({"fixture_owned"})), draft())
                except BaseException as caught:
                    outcome = caught
                want = dict(before)
                if closing:
                    self.assertIsInstance(outcome, sqlite3.ProgrammingError)
                    self.assertEqual(str(outcome), "the admission hook closed the store connection")
                else:
                    self.assertIsInstance(outcome, errors.EngineError)
                    self.assertEqual(outcome.code, "E-ADMISSION-HOOK")
                    self.assertEqual(outcome.params["kind"], "RuntimeError")
                    want["refusals"] += 1
                    self.assert_file_free(conn)
                self.assertEqual(self.file_state(path), want)
                self.assertNotIn(conn, submit._HOOK_FRAMES)
                pending, seen = [outcome], set()
                while pending:
                    error = pending.pop()
                    if error is None or id(error) in seen:
                        continue
                    seen.add(id(error))
                    self.assertNotIn(private_message, str(error))
                    pending.extend((error.__cause__, error.__context__))
                disclosed.append(private_message in "".join(traceback.format_exception(outcome)))
        self.assertEqual(disclosed, [False, False],
                         "closed and open hook failures must not disclose the private payload")

    def test_hook_closing_connection_cleans_frame(self):
        with tempfile.TemporaryDirectory() as name, mock.patch.dict(
                schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            path = Path(name) / "closed.db"
            conn = self.file_store(path)
            before = self.file_state(path)
            def hook(c, a, d, x):
                c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("closed", a.relay_id))
                c.close()
            outcome = None
            try:
                outcome = submit.submit(conn, replace(self.ctx(), after_admit=hook,
                              after_admit_writes=frozenset({"fixture_owned"})), draft())
            except BaseException as caught:
                outcome = caught
            self.assertIsInstance(outcome, sqlite3.ProgrammingError)
            self.assertEqual(str(outcome), "the admission hook closed the store connection")
            self.assertIsNone(outcome.__cause__)
            self.assertIsNone(outcome.__context__)
            self.assertEqual(self.file_state(path), before)
            self.assertNotIn(conn, submit._HOOK_FRAMES,
                             "closing a hook connection must not leak its ownership frame")

    def test_hook_closing_connection_interrupt_propagates(self):
        with tempfile.TemporaryDirectory() as name, mock.patch.dict(
                schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            path = Path(name) / "closed-interrupt.db"
            conn = self.file_store(path)
            before = self.file_state(path)
            interrupt = KeyboardInterrupt("closed-hook-interrupt")
            def hook(c, a, d, x):
                c.execute("INSERT INTO fixture_owned VALUES (?,?)", ("interrupt", a.relay_id))
                c.close()
                raise interrupt
            outcome = None
            try:
                outcome = submit.submit(conn, replace(self.ctx(), after_admit=hook,
                              after_admit_writes=frozenset({"fixture_owned"})), draft())
            except BaseException as caught:
                outcome = caught
            self.assertIs(outcome, interrupt)
            self.assertEqual(self.file_state(path), before)
            self.assertNotIn(conn, submit._HOOK_FRAMES)

    def test_hook_writes_outside_main_refused(self):
        with tempfile.TemporaryDirectory() as name, mock.patch.dict(
                schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            for database in ("fixture_attached", "temp"):
                with self.subTest(database=database):
                    path = Path(name) / (database + ".db")
                    conn = self.file_store(path)
                    conn.execute("ATTACH ':memory:' AS fixture_attached")
                    conn.execute("CREATE TABLE fixture_attached.fixture_owned (id TEXT PRIMARY KEY, relay_id TEXT)")
                    conn.execute("CREATE TEMP TABLE fixture_owned (id TEXT PRIMARY KEY, relay_id TEXT)")
                    before = self.file_state(path)
                    def hook(c, a, d, x):
                        c.execute("INSERT INTO " + database + ".fixture_owned VALUES (?,?)",
                                  ("outside", a.relay_id))
                    outcome = None
                    try:
                        outcome = submit.submit(conn, replace(self.ctx(), after_admit=hook,
                                      after_admit_writes=frozenset({"fixture_owned"})), draft())
                    except BaseException as caught:
                        outcome = caught
                    self.assertIsInstance(outcome, errors.EngineError)
                    self.assertEqual(outcome.code, "E-ADMISSION-HOOK")
                    want = dict(before)
                    want["refusals"] += 1
                    self.assertEqual(self.file_state(path), want)
                    for target in ("fixture_attached", "temp"):
                        self.assertEqual(conn.execute("SELECT * FROM " + target + ".fixture_owned").fetchall(), [])
                    self.assert_file_free(conn)

    def test_system_filing_with_foreign_context_refused(self):
        with tempfile.TemporaryDirectory() as name:
            conn = self.file_store(Path(name) / "foreign-context.db")
            foreign_ctx = self.ctx(sender=FOREIGN, git_state=None)
            attempts = []
            def hook(c, a, d, x):
                try:
                    attempts.append(submit.file_system_relay(
                        c, foreign_ctx, draft(to=FOREIGN.address, bundle="foreign-system")))
                except BaseException as caught:
                    attempts.append(caught)
            outcome = None
            try:
                outcome = submit.submit(conn, replace(self.ctx(), after_admit=hook), draft())
            except BaseException as caught:
                outcome = caught
            self.assertIsInstance(outcome, submit.Admitted)
            self.assertEqual(len(attempts), 1)
            self.assertIsInstance(attempts[0], RuntimeError)
            self.assertEqual(str(attempts[0]),
                             "system filing requires this connection's active admission hook")
            self.assertEqual(conn.execute("SELECT project_id, count(*) FROM relays GROUP BY project_id").fetchall(),
                             [("p", 1)])
            self.assert_file_free(conn)


    def test_hook_closing_connection_engine_error_discloses_nothing(self):
        private_message = "PRIVATE-CLOSED-ENGINEERROR-PAYLOAD-467"
        with tempfile.TemporaryDirectory() as name, mock.patch.dict(
                schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            for route in ("raised", "system-filing"):
                with self.subTest(route=route):
                    path = Path(name) / (route + ".db")
                    conn = self.file_store(path)
                    before = self.file_state(path)
                    def hook(c, a, d, x):
                        c.execute("INSERT INTO fixture_owned VALUES (?,?)",
                                  ("engine-error", a.relay_id))
                        if route == "raised":
                            c.close()
                            try:
                                raise RuntimeError(private_message)
                            except RuntimeError:
                                raise errors.error_for("E-SUBMISSION-KEY-REUSED")
                        else:
                            try:
                                submit.file_system_relay(
                                    c, x, ("invalid " + private_message + "\n").encode())
                            except errors.EngineError:
                                c.close()
                                raise
                    outcome = None
                    try:
                        outcome = submit.submit(conn, replace(self.ctx(), after_admit=hook,
                                      after_admit_writes=frozenset({"fixture_owned"})), draft())
                    except BaseException as caught:
                        outcome = caught
                    self.assertIsInstance(outcome, sqlite3.ProgrammingError)
                    self.assertEqual(str(outcome), "the admission hook closed the store connection")
                    self.assertIsNone(outcome.__cause__)
                    self.assertIsNone(outcome.__context__)
                    pending, seen = [outcome], set()
                    while pending:
                        error = pending.pop()
                        if error is None or id(error) in seen:
                            continue
                        seen.add(id(error))
                        self.assertNotIn(private_message, str(error))
                        pending.extend((error.__cause__, error.__context__))
                    self.assertNotIn(private_message, "".join(traceback.format_exception(outcome)))
                    self.assertEqual(self.file_state(path), before)
                    self.assertNotIn(conn, submit._HOOK_FRAMES)


class TestHookWriteOwners(Store):
    """Breaks: a hook may write a table whose declared owner is not Authority (slice 2a D1)."""

    def test_hook_write_owner_allowlist(self):
        message = "after_admit_writes names only declared hook-owned tables"
        with mock.patch.dict(schema.TABLE_OWNERS, {"fixture_owned": "authority"}):
            ctx = replace(self.ctx(), after_admit_writes=frozenset({"fixture_owned"}))
            self.assertEqual(ctx.after_admit_writes, frozenset({"fixture_owned"}))
        for owner in ("console", "store", "test-only"):
            with mock.patch.dict(schema.TABLE_OWNERS, {"fixture_owned": owner}):
                with self.assertRaises(ValueError, msg=owner) as caught:
                    replace(self.ctx(), after_admit_writes=frozenset({"fixture_owned"}))
                self.assertEqual(str(caught.exception), message)
        for table in sorted(schema.TABLE_OWNERS):
            with self.assertRaises(ValueError, msg=table):
                replace(self.ctx(), after_admit_writes=frozenset({table}))


class TestSenderCheck(Store):
    """Breaks: a served sender replays or admits on a binding that is no longer current, or its
    check writes, nests or holds a transaction across preparation (slice 2a D6, D7)."""

    def checked(self, check, **kwargs):
        return replace(self.ctx(**kwargs), sender_check=check)

    def recorder(self, raising=()):
        """A check counting its calls and recording the transaction state at each; it raises
        E-SENDER-NOT-CURRENT on the calls numbered in raising."""
        calls = []

        def check(conn):
            calls.append(conn.in_transaction)
            if len(calls) in raising:
                raise errors.error_for("E-SENDER-NOT-CURRENT", reason="key_replaced")
        return check, calls

    def last_refusal(self):
        return self.conn.execute("SELECT project_id, coalesce(seat_id, special), code FROM refusals"
                                 " ORDER BY id DESC LIMIT 1").fetchone()

    def test_sender_check_once_per_path(self):
        self.checked(None)  # the field exists, or this test errors here
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, "store.sqlite3")
        conn = submit.open_store(path)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode = WAL").fetchone()
        platform_rows(conn)
        other = sqlite3.connect(path, timeout=0, isolation_level=None)
        self.addCleanup(other.close)
        calls, writer, parsed = [], [], []

        def check(c):
            calls.append(c.in_transaction)
            c.execute("SELECT count(*) FROM seats").fetchone()
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                writer.append("free")
            except sqlite3.OperationalError as error:
                writer.append("busy" if "locked" in str(error) else str(error))
        real = header.parse

        def parse(data):
            parsed.append(conn.in_transaction)
            return real(data)
        ctx = replace(submit.SubmitContext(PLANNER, GIT_STATE, self.now, self.directory,
                                           submission_key="once"), sender_check=check)
        data = draft(subject="once")
        with mock.patch.object(header, "parse", side_effect=parse):
            first = snapshot_outcome(lambda: submit.submit(conn, ctx, data))
            self.assertIsInstance(first[1], submit.Admitted, first)
            self.assertEqual((calls, writer, parsed), ([True, True], ["free", "busy"], [False]))
            self.assertFalse(conn.in_transaction)
            del calls[:], writer[:], parsed[:]
            again = snapshot_outcome(lambda: submit.submit(conn, ctx, data))
        self.assertEqual(again, ("ok", first[1]))
        self.assertEqual((calls, writer, parsed), ([True], ["free"], []))
        self.assertFalse(conn.in_transaction)

    def test_sender_check_refuses_stale_sender(self):
        stale, stale_calls = self.recorder(raising={1})
        ctx = self.checked(stale, submission_key="committed")
        data = draft(subject="stale")
        self.assertIsInstance(self.submit(data, submission_key="committed"), submit.Admitted)
        cases = ((ctx, data, stale_calls, 1),)
        fresh, fresh_calls = self.recorder(raising={1})
        cases += ((self.checked(fresh, submission_key="fresh"), draft(subject="fresh"), fresh_calls, 1),)
        late, late_calls = self.recorder(raising={2})
        cases += ((self.checked(late, submission_key="late"), draft(subject="late"), late_calls, 2),)
        for case_ctx, case_data, calls, expected_calls in cases:
            before = self.counts()
            kind, _ = snapshot_outcome(lambda: submit.submit(self.conn, case_ctx, case_data))
            after = self.counts()
            refusals = after.pop("refusals") - before.pop("refusals")
            self.assertEqual((kind, len(calls), refusals, after), ("E-SENDER-NOT-CURRENT", expected_calls, 1,
                                                                   before), case_ctx.submission_key)
            self.assertEqual(self.last_refusal(), (PROJECT, PLANNER.seat_id, "E-SENDER-NOT-CURRENT"))
            self.assertFalse(self.conn.in_transaction)

    def test_sender_check_replay_without_preparation(self):
        passing, calls = self.recorder()
        hooked = []
        ctx = replace(self.checked(passing, submission_key="cited"),
                      after_admit=lambda conn, admitted, parsed, context: hooked.append(admitted))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cited = Path(os.path.realpath(folder.name)) / "cited"
        cited.write_bytes(b"cited\n")
        data = draft(subject="cited", refs=("file:" + str(cited),))
        first = snapshot_outcome(lambda: submit.submit(self.conn, ctx, data))
        self.assertIsInstance(first[1], submit.Admitted, first)
        self.assertEqual(len(hooked), 1)
        cited.unlink()
        del calls[:]
        before = self.counts()
        again = snapshot_outcome(lambda: submit.submit(self.conn, ctx, data))
        self.assertEqual((again, len(calls), len(hooked), self.counts()), (("ok", first[1]), 1, 1, before))
        other = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft(subject="other")))
        self.assertEqual(other[0], "E-SUBMISSION-KEY-REUSED", other)
        self.assertEqual(len(hooked), 1)

    def test_sender_check_read_only(self):
        denied = "not authorized"
        attempts = {
            "insert": (lambda c: c.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                                           " VALUES ('seat-new', 't1', 'planner', 'occupied')"), denied),
            "commit": (lambda c: c.execute("COMMIT"), denied),
            "attach": (lambda c: c.execute("ATTACH DATABASE ':memory:' AS attached"), denied),
            "pragma": (lambda c: c.execute("PRAGMA foreign_keys = OFF"), denied),
            "bodies": (lambda c: c.execute("SELECT body FROM bodies LIMIT 1").fetchone(),
                       "access to bodies.body is prohibited"),
        }
        for name, (attempt, message) in attempts.items():
            calls = []
            ctx = self.checked(lambda c: calls.append(name) or attempt(c))
            before = self.counts()
            outcome = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft(subject=name)))
            self.assertIsInstance(outcome[1], sqlite3.DatabaseError, (name, outcome))
            # The pre-lock check's own attempt is refused, so the check runs once and the lock is never taken.
            self.assertEqual((str(outcome[1]), len(calls)), (message, 1), name)
            self.assertEqual(self.counts(), before, name)
            self.assertFalse(self.conn.in_transaction, name)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM seats WHERE id = 'seat-new'")
                             .fetchone(), (0,), name)
            self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone(), (1,), name)
        ctx = self.checked(lambda c: c.execute("SELECT count(*) FROM seats").fetchone())
        outcome = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft(subject="reads")))
        self.assertIsInstance(outcome[1], submit.Admitted, outcome)

    def test_sender_check_cannot_nest(self):
        inner = []
        nesting = self.checked(lambda c: inner.append(
            snapshot_outcome(lambda: submit.submit(c, self.ctx(), draft(subject="inner")))))
        outcome = snapshot_outcome(lambda: submit.submit(self.conn, nesting, draft(subject="outer")))
        self.assertIsInstance(outcome[1], submit.Admitted, outcome)
        self.assertEqual([(kind, str(error)) for kind, error in inner],
                         [("DatabaseError", "nested admission is not allowed during a sender check")] * 2)
        filing = self.checked(lambda c: inner.append(
            snapshot_outcome(lambda: submit.file_system_relay(c, filing, draft(bundle="system")))))
        del inner[:]
        outcome = snapshot_outcome(lambda: submit.submit(self.conn, filing, draft(subject="filing")))
        self.assertIsInstance(outcome[1], submit.Admitted, outcome)
        self.assertEqual([kind for kind, _ in inner], ["RuntimeError"] * 2)
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        schema.init_v3(conn)
        platform_rows(conn)
        closing = self.checked(lambda c: c.close())
        outcome = snapshot_outcome(lambda: submit.submit(conn, closing, draft(subject="closing")))
        self.assertEqual((outcome[0], str(outcome[1])),
                         ("ProgrammingError", "the sender check closed the store connection"))

    def test_sender_check_read_guard_keeps_frame(self):
        """Breaks: a nested read guard replaces or clears the sender check's authorizer."""
        with self.subTest(case="write_after_guard"):
            seen = []

            def write_after_guard(c):
                with submit.read_guard(c):
                    seen.append(c.execute("SELECT count(*) FROM seats").fetchone())
                c.execute("INSERT INTO seats (id, team_instance_id, role, state)"
                          " VALUES ('seat-check-guard', 't9', 'implementer', 'occupied')")
            before = self.counts()
            outcome = snapshot_outcome(lambda: submit.submit(
                self.conn, self.checked(write_after_guard), draft(subject="guard-write")))
            self.assertIsInstance(outcome[1], sqlite3.DatabaseError, outcome)
            self.assertEqual(str(outcome[1]), "not authorized")
            self.assertEqual(len(seen), 1)
            self.assertEqual(self.counts(), before)
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM seats WHERE id='seat-check-guard'").fetchone(), (0,))
            self.assertFalse(self.conn.in_transaction)

        with self.subTest(case="extra_denials"):
            def extra_denials(c):
                with submit.read_guard(c, frozenset({"bodies", "edges"})):
                    c.execute("SELECT count(*) FROM seats").fetchone()
            before = self.counts()
            ctx = replace(self.checked(extra_denials),
                          after_admit_reads_denied=frozenset({"bodies"}))
            outcome = snapshot_outcome(lambda: submit.submit(
                self.conn, ctx, draft(subject="guard-extra-denials")))
            self.assertIsInstance(outcome[1], RuntimeError, outcome)
            self.assertEqual(str(outcome[1]),
                "a read guard inside a sender check cannot deny more than the check's own read denials")
            self.assertEqual(self.counts(), before)
            self.assertFalse(self.conn.in_transaction)

        with self.subTest(case="subset_restores"):
            seen = []

            def subset(c):
                with submit.read_guard(c, frozenset({"bodies"})):
                    seen.append(c.execute("SELECT count(*) FROM seats").fetchone())
            outcome = snapshot_outcome(lambda: submit.submit(
                self.conn, self.checked(subset), draft(subject="guard-subset")))
            self.assertIsInstance(outcome[1], submit.Admitted, outcome)
            self.assertEqual(len(seen), 2)
            self.assertFalse(self.conn.in_transaction)
            changed = self.conn.execute("UPDATE seats SET state = state WHERE id = ?",
                                        (PLANNER.seat_id,))
            self.assertEqual(changed.rowcount, 1)
            self.conn.rollback()

    def test_sender_check_none_unchanged(self):
        ctx = replace(self.ctx(submission_key="plain"), sender_check=None)
        data = draft(subject="plain")
        first = submit.submit(self.conn, ctx, data)
        parsed = []
        real = header.parse
        with mock.patch.object(header, "parse", side_effect=lambda d: parsed.append(d) or real(d)):
            outcome = snapshot_outcome(lambda: submit.submit(self.conn, ctx, data))
        self.assertEqual((outcome, parsed), (("ok", first), []))


class TestAdmissionKeyBoundary(Store):
    """Breaks: accepting a key SQLite cannot store or outside the UTF-8 byte bound."""

    def test_key_boundary_refuses_unstorable_keys(self):
        refused = []
        for key in ("\x00abc", "a\x00", "\udcff", "é" * 128 + "x"):
            try:
                self.ctx(submission_key=key)
            except ValueError:
                refused.append(True)
            else:
                refused.append(False)
        for key in ("é" * 128, "ключ"):
            ctx = self.ctx(submission_key=key)
            data = draft(subject="key boundary " + key[:1])
            admitted = submit.submit(self.conn, ctx, data)
            before = self.counts()
            self.assertEqual(submit.submit(self.conn, ctx, data), admitted)
            self.assertEqual(self.counts(), before)
        self.assertEqual(refused, [True, True, True, True],
                         "SubmitContext must refuse unstorable and over-256-byte keys")


def snapshot_outcome(action):
    try:
        return ("ok", action())
    except Exception as exc:
        return (getattr(exc, "code", type(exc).__name__), exc)


class FakeStore:
    """An in-memory evidence store that counts what it was asked to keep."""

    def __init__(self):
        self.objects, self.puts = ({}, 0)

    def put(self, data):
        self.puts += 1
        key = hashlib.sha256(data).hexdigest()
        self.objects[key] = bytes(data)
        return key

    def get(self, sha256):
        return self.objects[sha256]

    def captured_by(self, sha256):
        return "fixture" if sha256 in self.objects else None

MIB = 1 << 20


class Snapshots(Store):
    """DD § 17: what each review saw, and which copy an approve locked."""

    def counts(self):
        return dict(super().counts(), snapshots=self.conn.execute(
            "SELECT count(*) FROM snapshots").fetchone()[0])

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(os.path.realpath(folder.name))
        self.store = FakeStore()

    def ctx(self, *args, **kwargs):
        ctx = super().ctx(*args, **kwargs)
        return replace(ctx, store=self.store)

    def artifact(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return "file:" + str(path)

    def request(self, *refs_, kind="DESIGN", cc=None):
        result = self.submit(draft(kind=kind, needs="review", to="v3-engine.implementer", cc=cc,
            refs=refs_))
        return result

    def approve(self, request, *refs_, verdict="approve", sender=IMPL):
        return self.submit(self.reply(request, *refs_, verdict=verdict), sender=sender)

    def reply(self, request, *refs_, verdict="approve"):
        return draft(kind="DESIGN-REVIEW", verdict=verdict, parent="#%d" % request.handle,
            to="v3-engine.planner", refs=refs_)

    def rows(self):
        return self.conn.execute("SELECT handle, relay_id, artifact, content_sha256, bytes_len, "
            "has_bytes, locked_by FROM snapshots ORDER BY handle").fetchall()


class TestSnapshots(Snapshots):
    def test_snapshot_on_review_request(self):
        ref = self.artifact("design.md", b"# design\nline\n")
        first = self.request(ref, kind="PLAN")
        sha = hashlib.sha256(b"# design\nline\n").hexdigest()
        self.assertEqual(self.rows(), [(1, first.relay_id, ref[5:], sha, 14, 1, None)])
        self.assertEqual(self.store.get(sha), b"# design\nline\n")
        self.request(ref, kind="DESIGN")
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.store.puts, 1)
        for kind, needs in [("SITREP", "none"), ("DESIGN", "act"), ("AUDIT", "review")]:
            self.submit(draft(kind=kind, needs=needs, refs=(ref,)))
            self.assertEqual(len(self.rows()), 2)

    def test_lock_marks_latest_approved(self):
        ref = self.artifact("d.md", b"v1\n")
        request = self.request(ref, cc="v3-other.planner")
        self.refused("E-SNAPSHOT-NOT-TO", self.reply(request, ref), sender=OTHER)
        self.refused("E-SNAPSHOT-NO-REF", self.reply(request), sender=IMPL)
        Path(ref[5:]).write_bytes(b"v2\n")
        self.refused("E-SNAPSHOT-STALE", self.reply(request, ref), sender=IMPL)
        Path(ref[5:]).write_bytes(b"v1\n")
        reply = self.approve(request, ref)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][6], reply.relay_id)
        self.assertEqual(self.conn.execute("SELECT locked_at FROM snapshots").fetchone(), (
            reply.created_at_ms,))
        self.approve(request, ref)
        self.assertEqual(self.rows()[0][6], reply.relay_id)

    def test_lock_per_artifact(self):
        a = self.artifact("a.md", b"a\n")
        b = self.artifact("b.md", b"b\n")
        request = self.request(a, b)
        reply = self.approve(request, a)
        self.assertEqual([r[6] for r in self.rows()], [reply.relay_id, None])
        second = self.request(a, b)
        Path(b[5:]).write_bytes(b"changed")
        self.refused("E-SNAPSHOT-STALE", self.reply(second, a, b), sender=IMPL)
        self.assertEqual([r[6] for r in self.rows()], [reply.relay_id, None, None, None])

    def test_hook_sees_own_lock(self):
        from relay_engine import queries
        ref = self.artifact("a.md", b"a\n")
        request = self.request(ref)
        seen = []

        def hook(conn, admitted, data, ctx):
            seen.append(queries.locked_snapshot(conn, ref[5:], project_id=PROJECT))
        result = snapshot_outcome(lambda: submit.submit(self.conn, replace(self.ctx(IMPL),
            after_admit=hook), self.reply(request, ref)))
        self.assertEqual(result[0], "ok", result)
        self.assertIsNotNone(seen[0])
        self.assertEqual(seen[0].locked_by, result[1].relay_id)
        second = self.request(ref)
        before = self.conn.execute("SELECT * FROM snapshots").fetchall()

        def failing(conn, admitted, data, ctx):
            hook(conn, admitted, data, ctx)
            raise RuntimeError("fixture")
        result = snapshot_outcome(lambda: submit.submit(self.conn, replace(self.ctx(IMPL),
            after_admit=failing), self.reply(second, ref)))
        self.assertEqual(result[0], "E-ADMISSION-HOOK", result)
        self.assertIn("RuntimeError", result[1].cause)
        self.assertIsNotNone(seen[-1])
        self.assertEqual(self.conn.execute("SELECT * FROM snapshots").fetchall(), before)
        self.assertIsNone(self.conn.execute("SELECT id FROM relays WHERE id=?", (seen[-1].locked_by,
            )).fetchone())

    def test_replayed_approve_locks_nothing(self):
        ref = self.artifact("a.md", b"a")
        request = self.request(ref)
        calls = []
        ctx = replace(self.ctx(IMPL), after_admit=lambda *a: calls.append(1))
        data = self.reply(request, ref)
        reply = submit.submit(self.conn, ctx, data)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][6], reply.relay_id)
        before = self.conn.execute("SELECT * FROM snapshots").fetchall()
        self.assertEqual(submit.submit(self.conn, ctx, data), reply)
        self.assertEqual(self.conn.execute("SELECT * FROM snapshots").fetchall(), before)
        self.assertEqual(calls, [1])

    def test_snapshot_without_store_is_digest_only(self):
        ref = self.artifact("a.md", b"a")
        stored = self.request(ref)
        self.approve(stored, ref)
        self.store = None
        bare = self.request(ref)
        self.approve(bare, ref)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([r[3] for r in self.rows()], [hashlib.sha256(b"a").hexdigest()] * 2)
        self.assertEqual([r[5] for r in self.rows()], [1, 0])
        self.assertTrue(all((r[6] for r in self.rows())))

    def test_large_file_snapshot_digest_only(self):
        big = b"x" * (MIB + 1)
        ref = self.artifact("big", big)
        self.request(ref)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][3:6], (hashlib.sha256(big).hexdigest(), len(big), 0))
        self.assertEqual(self.store.puts, 0)
        self.request(self.artifact("exact", b"y" * MIB))
        self.assertEqual(self.rows()[1][5], 1)
        self.assertEqual(self.store.puts, 1)

    def test_non_approve_replies_lock_nothing(self):
        ref = self.artifact("non-approve", b"same")
        request = self.request(ref)
        self.assertEqual(len(self.rows()), 1)
        for data in (self.reply(request, ref, verdict="must-revise"), draft(parent="#" + str(
            request.handle), refs=(ref,))):
            result = snapshot_outcome(lambda: self.submit(data, sender=IMPL))
            self.assertEqual(result[0], "ok", result)
            self.assertTrue(all((row[6] is None for row in self.rows())))

    def test_approve_with_unsnapshotted_ref_locks(self):
        # Breaks: refusing an extra cited artifact that has no request snapshot.
        a = self.artifact("snapshotted", b"a")
        other = self.artifact("not-snapshotted", b"other")
        request = self.request(a)
        result = snapshot_outcome(lambda: self.approve(request, a, other))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][6], result[1].relay_id)

    def test_unsnapshotted_first_citation_keeps_checking(self):
        # Breaks: stopping the stale check at a cited artifact the request never snapshotted.
        a = self.artifact("keeps-checking", b"a\n")
        request = self.request(a)
        Path(a[5:]).write_bytes(b"changed\n")
        other = self.artifact("never-cited", b"other\n")
        data = self.reply(request, other, a)
        stale = [number for number, text in enumerate(data.split(b"\n"), 1)
                 if text.startswith(b"REF: ")][1]
        self.refused("E-SNAPSHOT-STALE", data, sender=IMPL, line=stale)
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.rows()[0][6])

    def test_not_to_precedes_no_ref(self):
        # Breaks: testing missing references before the approver's TO membership.
        ref = self.artifact("not-to", b"same")
        request = self.request(ref, cc=OTHER.address)
        self.refused("E-SNAPSHOT-NOT-TO", self.reply(request), sender=OTHER)
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.rows()[0][6])

    def test_store_wrong_key_refuses(self):
        # Breaks: accepting a store key different from the captured bytes' SHA256.
        ref = self.artifact("wrong-key", b"same")
        before = self.counts()
        refusals = self.conn.execute("SELECT * FROM refusals").fetchall()
        with mock.patch.object(self.store, "put", return_value="0" * 64):
            result = snapshot_outcome(lambda: self.request(ref))
        self.assertEqual(result[0], "ValueError", result)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.conn.execute("SELECT * FROM refusals").fetchall(), refusals)
        self.assertFalse(self.conn.in_transaction)

    def test_store_dedupe_by_store(self):
        from relay_engine import queries
        ref = self.artifact("dedupe", b"same")
        first_store = self.store
        self.store = None
        self.request(ref)
        self.store = first_store
        self.request(ref)
        self.assertEqual(first_store.puts, 1)
        out = self.folder / "dedupe-copy"
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 2, out, first_store,
            project_id=PROJECT))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(out.read_bytes(), b"same")
        self.request(ref)
        self.assertEqual(first_store.puts, 1)
        second_store = FakeStore()
        self.store = second_store
        self.request(ref)
        self.request(ref)
        self.assertEqual(second_store.puts, 1)
        result = snapshot_outcome(lambda: queries.write_snapshot(self.conn, 4, out, second_store, True,
            project_id=PROJECT))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(out.read_bytes(), b"same")

    def test_resolve_keeps_bytes_only_for_snapshots(self):
        ref = self.artifact("keep", b"bytes")
        keeps = []
        original = refs.read_file

        def read(path, keep):
            keeps.append(keep)
            return original(path, keep)
        with mock.patch.object(refs, "read_file", side_effect=read):
            result = snapshot_outcome(lambda: self.submit(draft(refs=(ref,))))
            self.assertEqual(result[0], "ok", result)
            result = snapshot_outcome(lambda: self.request(ref, kind="PLAN"))
            self.assertEqual(result[0], "ok", result)
        self.assertEqual(keeps, [0, submit.SNAPSHOT_BYTES_MAX])


class GitSnapshots(Snapshots, GitStore):
    def repository(self):
        repo = Repo({"d.md": "one\n"})
        self.addCleanup(repo.close)
        self.directory.checkouts["bivpak"] = repo.path
        return repo


class TestGitSnapshots(GitSnapshots):
    def test_stale_second_citation_refuses(self):
        # Breaks: comparing only the first citation of each snapshotted artifact.
        repo = self.repository()
        one = git(repo.path, "rev-parse", "HEAD")
        original = "bivpak@" + one + ":d.md"
        request = self.request(original)
        repo.commit_files({"d.md": "two\n"})
        self.refused("E-SNAPSHOT-STALE", self.reply(request, original,
            "bivpak@main:d.md"), sender=IMPL)
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.rows()[0][6])

    def test_stale_first_citation_refuses(self):
        # Breaks: comparing only the last citation of each snapshotted artifact.
        repo = self.repository()
        one = git(repo.path, "rev-parse", "HEAD")
        original = "bivpak@" + one + ":d.md"
        request = self.request(original)
        repo.commit_files({"d.md": "two\n"})
        data = self.reply(request, "bivpak@main:d.md", original)
        stale = [number for number, text in enumerate(data.split(b"\n"), 1)
                 if text.startswith(b"REF: ")][0]
        self.refused("E-SNAPSHOT-STALE", data, sender=IMPL, line=stale)
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.rows()[0][6])

    def test_git_snapshot_identity(self):
        repo = self.repository()
        try:
            request = self.request("bivpak@main:d.md")
        except Exception as exc:
            request = exc
        self.assertIsInstance(request, submit.Admitted)
        self.assertEqual(self.rows(), [(1, request.relay_id, "repo-1:d.md", hashlib.sha256(
            b"one\n").hexdigest(), 4, 0, None)])
        blob = git(repo.path, "rev-parse", "main:d.md")
        self.assertNotEqual(self.rows()[0][3], blob)
        self.assertEqual(self.store.puts, 0)

    def test_git_lock_and_changed_blob(self):
        repo = self.repository()
        request = self.request("bivpak@main:d.md")
        commit = git(repo.path, "rev-parse", "HEAD")
        repo.commit_files({"d.md": "two\n"})
        self.refused("E-SNAPSHOT-STALE", self.reply(request, "bivpak@main:d.md"), sender=IMPL)
        reply = self.approve(request, "bivpak@" + commit + ":d.md")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][6], reply.relay_id)

    def test_lock_matches_each_snapshot(self):
        repo = self.repository()
        one = git(repo.path, "rev-parse", "HEAD")
        repo.commit_files({"d.md": "two\n"})
        two = git(repo.path, "rev-parse", "HEAD")
        a, b = ("bivpak@" + one + ":d.md", "bivpak@" + two + ":d.md")
        result = snapshot_outcome(lambda: self.request(a, b))
        self.assertEqual(result[0], "ok", result)
        request = result[1]
        result = snapshot_outcome(lambda: self.approve(request, a))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual([row[6] for row in self.rows()], [result[1].relay_id, None])
        result = snapshot_outcome(lambda: self.request(a, b))
        self.assertEqual(result[0], "ok", result)
        second = result[1]
        result = snapshot_outcome(lambda: self.approve(second, a, b))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual([row[6] for row in self.rows()][2:], [result[1].relay_id] * 2)
        repo.commit_files({"d.md": "three\n"})
        result = snapshot_outcome(lambda: self.request(a, b))
        self.assertEqual(result[0], "ok", result)
        self.refused("E-SNAPSHOT-STALE", self.reply(result[1], "bivpak@main:d.md"), sender=IMPL)

BODY_READS = ("SELECT body FROM bodies", "SELECT count(*) FROM bodies",
    "SELECT EXISTS(SELECT 1 FROM bodies)", "SELECT 1 FROM bodies",
    "SELECT r.id FROM relays r JOIN bodies b ON b.relay_id=r.id")


class TestHookReadDenials(Store):
    def context(self, hook, denied=None):
        ctx = replace(self.ctx(), after_admit=hook)
        if denied is not None:
            ctx = replace(ctx, after_admit_reads_denied=denied)
        return ctx

    def test_hook_body_read_denied(self):
        for sql in BODY_READS:
            with self.subTest(sql=sql):
                before = self.counts()
                result = snapshot_outcome(lambda: submit.submit(self.conn, self.context(lambda c,
                    *_: c.execute(sql).fetchall(), frozenset({"bodies"})), draft()))[0]
                self.assertEqual(result, "E-ADMISSION-HOOK")
                after = self.counts()
                self.assertEqual(after.pop("refusals"), before.pop("refusals") + 1)
                self.assertEqual(after, before)

    def test_hook_reads_allowed_tables(self):
        seen = []

        def hook(c, a, d, x):
            for table in ("relays", "edges", "findings", "snapshots"):
                seen.append(c.execute("SELECT count(*) FROM " + table).fetchone()[0])
        self.assertIsInstance(snapshot_outcome(lambda: submit.submit(self.conn, self.context(hook,
            frozenset({"bodies"})), draft()))[1], submit.Admitted)
        self.assertEqual(seen, [1, 0, 0, 0])

    def test_hook_read_denied_default(self):
        ctx = self.context(lambda c, *_: c.execute("SELECT count(*) FROM bodies").fetchone())
        self.assertEqual(snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft()))[0],
            "E-ADMISSION-HOOK")

    def test_hook_read_denied_validation(self):
        self.assertIn("after_admit_reads_denied", submit.SubmitContext.__dataclass_fields__)
        for value in (set(), ["bodies"], "bodies", frozenset({"unknown"}), frozenset({1})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.ctx(), after_admit_reads_denied=value)
        for table in schema.TABLE_OWNERS:
            self.assertEqual(replace(self.ctx(), after_admit_reads_denied=frozenset({
                table})).after_admit_reads_denied, frozenset({table}))

    def test_system_filing_under_body_denial(self):
        seen = []

        def hook(c, a, d, x):
            seen.append(submit.file_system_relay(c, x, draft(bundle="system")))
            try:
                c.execute("SELECT 1 FROM bodies").fetchall()
                seen.append("body allowed")
            except sqlite3.DatabaseError:
                seen.append("body denied")
        result = snapshot_outcome(lambda: submit.submit(self.conn, self.context(hook, frozenset({
            "bodies"})), draft()))[1]
        self.assertIsInstance(result, submit.Admitted)
        self.assertEqual(len(seen), 2)
        self.assertIsInstance(seen[0], submit.Admitted)
        self.assertEqual(seen[1], "body denied")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM relays").fetchone(), (2,))


class TestReadGuard(Store):
    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE fixture_guard (value TEXT)")
        self.conn.commit()
        patch = mock.patch.dict(schema.TABLE_OWNERS, {"fixture_guard": "authority"})
        patch.start()
        self.addCleanup(patch.stop)

    def body_denied(self):
        for sql in BODY_READS:
            result = snapshot_outcome(lambda: self.conn.execute(sql).fetchall())
            self.assertNotEqual(result[0], "ok", sql)
            self.assertIsInstance(result[1], sqlite3.DatabaseError)

    def test_read_guard_denies_bodies(self):
        self.submit(draft())
        with submit.read_guard(self.conn):
            self.body_denied()
            for table in ("relays", "edges", "snapshots"):
                self.conn.execute("SELECT count(*) FROM " + table).fetchone()
            self.conn.execute("INSERT INTO fixture_guard VALUES ('owned')")
        self.assertEqual(self.conn.execute("SELECT * FROM fixture_guard").fetchall(), [("owned",)])

    def test_read_guard_restores(self):
        self.submit(draft())
        with submit.read_guard(self.conn):
            self.body_denied()
        normal = snapshot_outcome(lambda: self.conn.execute("SELECT body FROM bodies").fetchall())
        self.assertEqual(normal, ("ok", [(b"body\n",)]))
        marker = RuntimeError("fixture marker")
        caught = None
        try:
            with submit.read_guard(self.conn):
                raise marker
        except RuntimeError as exc:
            caught = exc
        self.assertIs(caught, marker)
        self.assertIsNone(caught.__cause__)
        self.assertIsNone(caught.__context__)
        result = snapshot_outcome(lambda: self.conn.execute("SELECT count(*) FROM bodies").fetchone())
        self.assertEqual(result, ("ok", (1,)))
        self.assertEqual(submit._READ_GUARDS, {})

    def test_read_guard_cached_statement(self):
        self.submit(draft())
        sql = "SELECT body FROM bodies"
        self.assertEqual(self.conn.execute(sql).fetchall(), [(b"body\n",)])
        with submit.read_guard(self.conn):
            result = snapshot_outcome(lambda: self.conn.execute(sql).fetchall())
            self.assertNotEqual(result[0], "ok")
            self.assertIsInstance(result[1], sqlite3.DatabaseError)

    def test_read_guard_nested(self):
        self.submit(draft())
        with submit.read_guard(self.conn):
            with submit.read_guard(self.conn, frozenset()):
                self.body_denied()
            with submit.read_guard(self.conn, frozenset({"relays"})):
                self.body_denied()
                result = snapshot_outcome(lambda: self.conn.execute("SELECT 1 FROM relays").fetchall())
                self.assertNotEqual(result[0], "ok")
                self.assertIsInstance(result[1], sqlite3.DatabaseError)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM relays").fetchone(), (1,))
            self.body_denied()
        self.assertEqual(submit._READ_GUARDS, {})

    def test_read_guard_inside_hook(self):
        observed = []

        def hook(c, a, d, x):
            with submit.read_guard(c, frozenset({"bodies"})):
                c.execute("INSERT INTO fixture_guard VALUES ('owned')")
                c.execute("SELECT count(*) FROM snapshots").fetchone()
                try:
                    c.execute("DELETE FROM relays WHERE id='no-such-relay'")
                    observed.append("write allowed")
                except sqlite3.DatabaseError:
                    observed.append("write denied")
        ctx = replace(self.ctx(), after_admit=hook, after_admit_writes=frozenset({"fixture_guard"}))
        result = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft()))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(observed, ["write denied"])
        self.assertEqual(self.conn.execute("SELECT * FROM fixture_guard").fetchall(), [("owned",)])

        def too_much(c, a, d, x):
            try:
                with submit.read_guard(c, frozenset({"bodies", "relays"})):
                    pass
            except RuntimeError as exc:
                observed.append(str(exc))
                raise
        ctx = replace(self.ctx(), after_admit=too_much)
        result = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft()))
        self.assertNotEqual(result[0], "ok")
        self.assertIsInstance(result[1], errors.EngineError)
        self.assertEqual(result[1].code, "E-ADMISSION-HOOK")
        self.assertIn("RuntimeError", result[1].cause)
        self.assertEqual(observed[-1],
            "a read guard inside a hook cannot deny more than the hook's own read denials")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM relays").fetchone(), (1,))
        observed.clear()

        def own_subset(c, a, d, x):
            with submit.read_guard(c, frozenset({"edges"})):
                observed.append(c.execute("SELECT count(*) FROM snapshots").fetchone())
        ctx = replace(self.ctx(), after_admit=own_subset, after_admit_reads_denied=frozenset({"bodies",
            "edges"}))
        result = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft()))
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(observed, [(0,)])

        def foreign_superset(c, a, d, x):
            with submit.read_guard(c, frozenset({"relays"})):
                pass
        ctx = replace(self.ctx(), after_admit=foreign_superset, after_admit_reads_denied=frozenset({
            "bodies", "edges"}))
        result = snapshot_outcome(lambda: submit.submit(self.conn, ctx, draft()))
        self.assertEqual(result[0], "E-ADMISSION-HOOK", result)
        self.assertEqual(result[1].code, "E-ADMISSION-HOOK")
        self.assertIn("RuntimeError", result[1].cause)

    def test_submit_refused_inside_read_guard(self):
        before = self.counts()
        with submit.read_guard(self.conn):
            result = snapshot_outcome(lambda: self.submit(draft()))
            self.assertNotEqual(result[0], "ok")
            self.assertIsInstance(result[1], sqlite3.DatabaseError)
            self.assertEqual(str(result[1]), "admission is not allowed during a read guard")
            self.body_denied()
        self.assertEqual(self.counts(), before)
        self.assertEqual(submit._READ_GUARDS, {})

    def test_read_guard_closed_connection(self):
        for raises in (False, True):
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            marker = RuntimeError("fixture close")

            def block():
                with submit.read_guard(conn):
                    conn.close()
                    if raises:
                        raise marker
            result = snapshot_outcome(block)
            if raises:
                self.assertEqual(result[0], "RuntimeError")
                self.assertIs(result[1], marker)
                self.assertIsNone(result[1].__context__)
                self.assertIsNone(result[1].__cause__)
            else:
                self.assertEqual(result, ("ok", None))
            self.assertEqual(submit._READ_GUARDS, {})

    def test_read_guard_validation(self):
        guard = submit.read_guard
        for denied in (set(), ["bodies"], "bodies", frozenset({"unknown"}), frozenset({1})):
            with self.subTest(denied=denied), self.assertRaises(ValueError):
                with guard(self.conn, denied):
                    pass
        self.assertEqual(submit._READ_GUARDS, {})

if __name__ == "__main__":
    unittest.main()
