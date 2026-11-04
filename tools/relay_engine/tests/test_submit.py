"""v3 submit pipeline (DD-v3-engine § 4, § 5, § 8, E-OP2; plan Task 4 with erratum 1)."""

import hashlib
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import multiprocessing
from pathlib import Path
from unittest import mock

from relay_engine import errors
from relay_engine import header
from relay_engine import refs
from relay_engine import schema
from relay_engine import submit
from relay_engine.tests.test_refs import Repo, clean_git_environment, git

PROJECT = "p"
SEATS = {
    address: submit.SeatRef("seat-" + address, address, address.partition(".")[2] or address, "t1",
                            PROJECT)
    for address in ("v3-engine.planner", "v3-engine.implementer", "v3-other.planner",
                    "v3.orchestrator-planner")
}
SEATS["operator"] = submit.SeatRef(submit.OPERATOR_SEAT_ID, "operator", "operator", "t1", PROJECT)
PLANNER = SEATS["v3-engine.planner"]
IMPL = SEATS["v3-engine.implementer"]
OTHER = SEATS["v3-other.planner"]
ORCH = SEATS["v3.orchestrator-planner"]
FOREIGN = submit.SeatRef("seat-x", "v3-engine.planner", "planner", "t9", "q")
TABLES = ("relays", "bodies", "recipients", "edges", "git_states", "bundles",
          "findings", "finding_events", "refusals")
GIT_STATE = submit.GitState("repo-1", "a" * 40, "main", 2, "0" * 64)


class FakeDirectory:
    def __init__(self, checkouts=None):
        self.checkouts = dict(checkouts or {})

    def resolve(self, address, sender):
        seat = SEATS.get(address)
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
        self.clock = 1_700_000_000_000
        self.directory = FakeDirectory()
        clean_git_environment(self)

    def now(self):
        return self.clock

    def ctx(self, sender=PLANNER, git_state=GIT_STATE):
        return submit.SubmitContext(sender, git_state, self.now, self.directory)

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
            "SELECT project_id, seat_id, code, line, draft_sha256 FROM refusals"
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


class TestAdmit(Store):
    def test_admit_writes_all_tables(self):
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
            "SELECT kind, ordinal, target_type, spelling, target_relay_id, target_repository_id,"
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
        with self.assertRaises(ValueError):
            submit.submit(conn, self.ctx(), draft())
        with tempfile.TemporaryDirectory() as name:
            store = submit.open_store(os.path.join(name, "store"))
            try:
                self.assertEqual(store.execute("PRAGMA foreign_keys").fetchone(), (1,))
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
        foreign = submit.SeatRef("seat-x", "v3-engine.planner", "planner", "t9", "q")
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
        self.assertEqual(self.conn.execute("SELECT project_id, team_instance_id, name, opened_by"
                                           " FROM bundles").fetchall(),
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
        foreign = submit.SeatRef("seat-x", "v3-engine.planner", "planner", "t9", "q")
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
    ctx = submit.SubmitContext(SEATS[address], None, lambda: 1_700_000_000_000, FakeDirectory())
    seen = []
    try:
        barrier.wait(60)
        for _ in range(count):
            try:
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
            submit.open_store(path).close()
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
            submit.open_store(path).close()
            inside, release, result = threading.Event(), threading.Event(), {}
            digest = refs.file_digest

            def slow(target):
                inside.set()
                release.wait(30)
                return digest(target)

            def first():
                conn = submit.open_store(path)
                try:
                    result["a"] = submit.submit(conn, self.ctx(), draft(refs=("file:" + str(check),)))
                except BaseException as error:
                    result["a"] = error
                finally:
                    conn.close()

            with mock.patch.object(refs, "file_digest", slow):
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
            " VALUES (?, ?, 'p', 'SITREP', 'none', 's', 'old', ?, ?, ?, 0, 0)",
            [(rid, n, at, "a" * 64, "a" * 64) for rid, n, at in rows])
        self.conn.executemany(
            "INSERT INTO findings (id, handle, project_id, raised_in, raiser_seat_id, severity,"
            " summary, status) VALUES (?, ?, 'p', ?, 'old', 'advisory', 's', 'withdrawn')",
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
                submit.SubmitContext(PLANNER, state, self.now, self.directory)
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
                    submit.SubmitContext(PLANNER, state, self.now, self.directory)
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

    def test_digest_over_resolved_targets(self):  # PR-3 fold S10, DD § 5
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


if __name__ == "__main__":
    unittest.main()
