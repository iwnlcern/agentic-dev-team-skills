"""v3 queries and metrics (DD-v3-engine § 7, § 8, § 9, § 11, § 15 item 6; plan Task 5)."""

import json
import os
import sqlite3
import time
import unittest

from relay_engine import queries
from relay_engine import refs
from relay_engine import schema
from relay_engine.tests.test_refs import Repo, git
from relay_engine.tests.test_submit import (IMPL, ORCH, OTHER, PLANNER, Store, draft)

ENVELOPE = {"handle", "kind", "sender", "to", "cc", "subject", "parent", "verdict", "needs",
            "created_at"}


class TestReads(Store):
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
    QUIET = 1_800_000

    def test_flag_counts_activity(self):
        start = self.clock
        act = self.submit(draft(to="v3-engine.implementer", cc="v3-other.planner", needs="act"))
        self.submit(draft(to="v3-engine.implementer", needs="none"))
        project = PLANNER.project_id
        before = queries.unopened_flags(self.conn, project, start + self.QUIET - 1)
        self.assertEqual(before, [])
        flags = queries.unopened_flags(self.conn, project, start + self.QUIET)
        self.assertEqual([(f.handle, f.seat_id) for f in flags], [(act.handle, IMPL.seat_id)])
        queries.note_activity(self.conn, IMPL.seat_id, start + self.QUIET - 10)
        self.assertEqual(queries.unopened_flags(self.conn, project, start + self.QUIET), [])
        later = start + 2 * self.QUIET  # the activity is now older than the window
        self.assertEqual(len(queries.unopened_flags(self.conn, project, later)), 1)
        queries.mark_opened(self.conn, act.relay_id, IMPL.seat_id, later)
        self.assertEqual(queries.unopened_flags(self.conn, project, later + 1), [])

    def test_a_read_is_activity(self):
        start = self.clock
        act = self.submit(draft(to="v3-engine.implementer", needs="act"))
        other = self.submit(draft(to="v3-engine.implementer", needs="none"))
        queries.read(self.conn, other.relay_id, IMPL.seat_id, start + self.QUIET - 10)
        self.assertEqual(queries.unopened_flags(self.conn, PLANNER.project_id,
                                                start + self.QUIET), [])
        self.assertIsNotNone(act)

    def test_activity_keeps_latest(self):
        queries.note_activity(self.conn, IMPL.seat_id, 50)
        queries.note_activity(self.conn, IMPL.seat_id, 20)
        self.assertEqual(self.conn.execute("SELECT last_request_at FROM seat_activity").fetchall(),
                         [(50,)])


class TestFindings(Store):
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


class TestEdges(Store):
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


class TestRecords(Store):
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

    def counts_of(self, table):
        return self.conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]

    def test_session_rows(self):
        queries.record_session(self.conn, "s1", "claude-code", IMPL.seat_id, 100)
        queries.end_session(self.conn, "s1", 200)
        self.assertEqual(self.conn.execute("SELECT session_id, host, seat_id, start_at, end_at"
                                           " FROM seat_sessions").fetchall(),
                         [("s1", "claude-code", IMPL.seat_id, 100, 200)])
        for session in ("s1", "absent"):
            with self.subTest(session=session):
                with self.assertRaises(ValueError):
                    queries.end_session(self.conn, session, 300)
        self.assertEqual(self.conn.execute("SELECT end_at FROM seat_sessions").fetchone(), (200,))


class TestQueryCost(unittest.TestCase):
    """DD § 15 item 6: flat cost at 1,206 and 50,000 relays (index plans; timings recorded)."""

    def store(self, count):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        schema.init_v3(conn)
        conn.execute("INSERT INTO bundles (id, project_id, name, opened_by) VALUES"
                     " ('b', 'p', 'b', 's')")
        sha = "a" * 64
        rows = []
        for n in range(1, count + 1):  # the last 300 relays form one chain
            chained = n > count - 300 + 1
            rows.append(("r%07d" % n, n, "p", "b" if n > count - 300 else None, "REVIEW", "s",
                         n, sha, sha, "r%07d" % (n - 1) if chained else None))
        conn.executemany(
            "INSERT INTO relays (id, handle, project_id, bundle_id, kind, verdict, needs, subject,"
            " sender_seat_id, created_at, body_sha256, meta_sha256, body_bytes, header_bytes,"
            " parent_id) VALUES (?, ?, ?, ?, ?, NULL, 'none', 's', ?, ?, ?, ?, 0, 10, ?)", rows)
        conn.executemany(
            "INSERT INTO findings (id, handle, project_id, bundle_id, raised_in, raiser_seat_id,"
            " severity, summary, status) VALUES (?, ?, 'p', ?, ?, 's', 'blocking', 's', 'open')",
            [("f%07d" % n, n, "b" if n > count - 300 else None, "r%07d" % n)
             for n in range(1, count + 1, 10)])
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
            plans = [row[3] for sql, params in ((queries._LINEAGE_SQL, (leaf, 256)),
                                                (queries._BLOCKING_SQL, ("b",)))
                     for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
            self.assertFalse([p for p in plans if p.startswith("SCAN relays")
                              or p.startswith("SCAN findings")], plans)
            record[count] = {
                "lineage_ms": self.timed(lambda: queries.lineage(conn, leaf)),
                "blocking_ms": self.timed(lambda: queries.blocking_findings(conn, "b")),
                "lineage_rows": len(queries.lineage(conn, leaf)),
            }
        print("query cost (min of 20, ms):", json.dumps(record, sort_keys=True))
        for name in ("lineage_ms", "blocking_ms"):
            small, large = (max(record[n][name], 1.0) for n in (1206, 50000))
            self.assertLess(large, 10.0, record)
            self.assertLess(large / small, 2.0, record)
        self.assertEqual([record[n]["lineage_rows"] for n in (1206, 50000)], [256, 256])


if __name__ == "__main__":
    unittest.main()
