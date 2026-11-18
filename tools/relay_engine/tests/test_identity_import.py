"""The identity import step: noncommitting writes of a moved project's identity rows."""
import ast
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid

from relay_engine import identity_import as ii
from relay_engine import schema

TABLES = ("projects", "team_instances", "seats")
SENTINEL = ("INSERT INTO refusals (special, code, at, draft_sha256) "
            "VALUES ('operator', ?, 1, ?)")


def u7(n):
    """A canonical lowercase UUIDv7 string, distinct for each n."""
    return str(uuid.UUID(int=(n << 80) | (7 << 76) | (2 << 62) | n))


def instance(n, name, level, parent=None, opened_at=100, closed_at=None):
    return ii.ImportedTeamInstance(u7(n), name, level, parent, opened_at, closed_at)


def seat(n, team, role):
    return ii.ImportedSeat(u7(n), team.id, role)


PROJECT = u7(1)
MASTER = instance(10, "master", "master")
MASTER_PLANNER = seat(20, MASTER, "master-planner")
MASTER_REVIEWER = seat(21, MASTER, "master-reviewer")


def valid(**changes):
    call = {"project_id": PROJECT, "project_name": "moved", "created_at": 100,
            "instances": [MASTER], "seats": [MASTER_PLANNER, MASTER_REVIEWER],
            "top_seat_id": MASTER_PLANNER.id}
    call.update(changes)
    return call


class Ledger(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        conn = sqlite3.connect(os.path.join(folder.name, "ledger.sqlite3"),
                               isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=ON")
        schema.init_v3(conn)
        self.conn = conn

    def begin(self):
        self.conn.execute("BEGIN")
        self.conn.execute(SENTINEL, ("sentinel", "0" * 64))

    def rows(self):
        return {table: list(self.conn.execute("SELECT * FROM %s ORDER BY id" % table))
                for table in TABLES}

    def sentinels(self):
        return [code for (code,) in self.conn.execute(
            "SELECT code FROM refusals ORDER BY id")]

    def assert_caller_intact(self, sentinels=("sentinel",)):
        self.assertEqual(self.sentinels(), list(sentinels))
        self.assertTrue(self.conn.in_transaction)
        self.conn.execute(SENTINEL, ("after", "1" * 64))
        self.assertEqual(self.sentinels(), list(sentinels) + ["after"])


class TestRefusals(Ledger):
    def fixtures(self):
        open_x = instance(11, "x", "orchestrator")
        open_x_too = instance(12, "x", "pair")
        cycle_a = instance(13, "a", "pair", parent=u7(14), closed_at=200)
        cycle_b = instance(14, "b", "pair", parent=u7(13), closed_at=200)
        second_master = instance(15, "other-master", "master")
        return {
            "project_exists": valid(),
            "invalid_id": valid(instances=[ii.ImportedTeamInstance(
                str(uuid.uuid4()), "master", "master", None, 100, None)],
                seats=[], top_seat_id=None),
            "invalid_name": valid(project_name="Moved Project"),
            "invalid_role": valid(seats=[MASTER_PLANNER, seat(22, MASTER, "captain")]),
            "invalid_level": valid(instances=[MASTER, instance(11, "sprint", "sprint",
                                                               closed_at=200)]),
            "invalid_time": valid(created_at=True),
            "unknown_parent": valid(instances=[MASTER, instance(11, "s", "orchestrator",
                                                                parent=u7(99),
                                                                closed_at=200)]),
            "parent_cycle": valid(instances=[MASTER, cycle_a, cycle_b]),
            "open_name_duplicate": valid(instances=[MASTER, open_x, open_x_too]),
            "unknown_instance": valid(seats=[MASTER_PLANNER,
                                             ii.ImportedSeat(u7(22), u7(98), "reviewer")]),
            "duplicate_seat": valid(seats=[MASTER_PLANNER,
                                           seat(22, MASTER, "master-planner")]),
            "top_seat_unresolved": valid(instances=[MASTER, second_master],
                                         seats=[MASTER_PLANNER,
                                                seat(22, second_master, "master-planner")]),
            "top_seat_mismatch": valid(top_seat_id=MASTER_REVIEWER.id),
        }

    def test_each_refusal_writes_nothing(self):
        fixtures = self.fixtures()
        self.assertEqual(set(fixtures), {
            "project_exists", "invalid_id", "invalid_name", "invalid_role",
            "invalid_level", "invalid_time", "unknown_parent", "parent_cycle",
            "open_name_duplicate", "unknown_instance", "duplicate_seat",
            "top_seat_unresolved", "top_seat_mismatch"})
        for code, call in sorted(fixtures.items()):
            with self.subTest(code=code):
                self.setUp()
                self.begin()
                if code == "project_exists":
                    self.conn.execute(
                        "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                        (u7(2), "seeded", 1))
                before = self.rows()
                if code == "project_exists":
                    self.assertEqual(before["projects"], [(u7(2), "seeded", 1, None)])
                else:
                    self.assertEqual(before, {table: [] for table in TABLES})
                with self.assertRaises(ii.IdentityImportRefused) as caught:
                    ii.import_identities_step(self.conn, **call)
                self.assertEqual(caught.exception.reason, code)
                self.assertEqual(caught.exception.args, (code,))
                self.assertEqual(self.rows(), before)
                self.assert_caller_intact()


class TestWrites(Ledger):
    def test_seat_state_follows_instance(self):
        closed = instance(11, "sprint", "orchestrator", parent=MASTER.id, closed_at=200)
        self.begin()
        ii.import_identities_step(self.conn, **valid(
            instances=[MASTER, closed],
            seats=[MASTER_PLANNER, MASTER_REVIEWER,
                   seat(22, closed, "orchestrator-planner"),
                   seat(23, closed, "orchestrator-reviewer")]))
        states = dict(self.conn.execute("SELECT id, state FROM seats"))
        self.assertEqual(states, {MASTER_PLANNER.id: "vacant", MASTER_REVIEWER.id: "vacant",
                                  u7(22): "stood-down", u7(23): "stood-down"})

    def test_census_shaped_import(self):
        child = instance(14, "pair-a", "pair", parent=u7(11), opened_at=120, closed_at=150)
        sprint_1 = instance(11, "sprint-1", "orchestrator", parent=MASTER.id,
                            opened_at=110, closed_at=160)
        sprint_2 = instance(12, "sprint-2", "orchestrator", parent=MASTER.id,
                            opened_at=170, closed_at=180)
        sprint_3 = instance(13, "sprint-3", "orchestrator", parent=MASTER.id,
                            opened_at=190, closed_at=195)
        seats = [MASTER_PLANNER, MASTER_REVIEWER,
                 seat(22, sprint_1, "orchestrator-planner"),
                 seat(23, sprint_2, "orchestrator-planner"),
                 seat(24, sprint_3, "orchestrator-reviewer"),
                 seat(25, child, "planner"), seat(26, child, "implementer")]
        self.begin()
        ii.import_identities_step(self.conn, **valid(
            instances=[child, sprint_3, sprint_1, sprint_2, MASTER], seats=seats))
        self.assertEqual(self.rows(), {
            "projects": [(PROJECT, "moved", 100, MASTER_PLANNER.id)],
            "team_instances": sorted([
                (MASTER.id, PROJECT, "master", "master", None, 100, "operator", None),
                (sprint_1.id, PROJECT, "sprint-1", "orchestrator", MASTER.id, 110,
                 "operator", 160),
                (sprint_2.id, PROJECT, "sprint-2", "orchestrator", MASTER.id, 170,
                 "operator", 180),
                (sprint_3.id, PROJECT, "sprint-3", "orchestrator", MASTER.id, 190,
                 "operator", 195),
                (child.id, PROJECT, "pair-a", "pair", sprint_1.id, 120, "operator", 150)]),
            "seats": sorted([
                (MASTER_PLANNER.id, MASTER.id, "master-planner", "vacant"),
                (MASTER_REVIEWER.id, MASTER.id, "master-reviewer", "vacant"),
                (u7(22), sprint_1.id, "orchestrator-planner", "stood-down"),
                (u7(23), sprint_2.id, "orchestrator-planner", "stood-down"),
                (u7(24), sprint_3.id, "orchestrator-reviewer", "stood-down"),
                (u7(25), child.id, "planner", "stood-down"),
                (u7(26), child.id, "implementer", "stood-down")]),
        })


class TestTopSeat(Ledger):
    def test_top_seat_follows_level_mapping(self):
        self.assertIsInstance(ii.TOP_SEAT_ROLES, types.MappingProxyType)
        self.assertEqual(dict(ii.TOP_SEAT_ROLES), {
            "master": "master-planner", "domain": "domain-planner",
            "orchestrator": "orchestrator-planner", "pair": "planner"})
        closed = instance(30, "old", "orchestrator", closed_at=200)
        closed_seat = seat(31, closed, "orchestrator-planner")
        selected = {}
        for level, role in sorted(ii.TOP_SEAT_ROLES.items()):
            team = instance(11, "running", level)
            top = seat(22, team, role)
            other = seat(23, team, "reviewer" if level == "pair" else "implementer")
            selected[level] = ([closed, team], [closed_seat, top, other], top.id)
        orchestrator = instance(12, "sprint", "orchestrator", parent=MASTER.id)
        selected["master over an open orchestrator"] = (
            [MASTER, orchestrator],
            [MASTER_PLANNER, seat(24, orchestrator, "orchestrator-planner")],
            MASTER_PLANNER.id)
        selected["no open instance"] = ([closed], [closed_seat], None)
        for case, (instances, seats, expected) in sorted(selected.items()):
            with self.subTest(case=case):
                self.assertEqual(ii.derive_top_seat(instances, seats), expected)
                self.setUp()
                self.begin()
                ii.import_identities_step(self.conn, **valid(
                    instances=instances, seats=seats, top_seat_id=expected))
                self.assertEqual(
                    list(self.conn.execute("SELECT top_seat_id FROM projects")),
                    [(expected,)])
        open_a = instance(11, "a", "orchestrator")
        open_b = instance(12, "b", "domain")
        second_master = instance(13, "other-master", "master")
        unresolved = {
            "two open non-master": ([open_a, open_b],
                                    [seat(22, open_a, "orchestrator-planner"),
                                     seat(23, open_b, "domain-planner")]),
            "two open masters": ([MASTER, second_master],
                                 [MASTER_PLANNER,
                                  seat(22, second_master, "master-planner")]),
            "open master without master-planner": ([MASTER], [MASTER_REVIEWER]),
        }
        for case, (instances, seats) in sorted(unresolved.items()):
            with self.subTest(case=case):
                with self.assertRaises(ii.IdentityImportRefused) as caught:
                    ii.derive_top_seat(instances, seats)
                self.assertEqual(caught.exception.reason, "top_seat_unresolved")

    def test_top_seat_mismatch_refused(self):
        closed = instance(11, "old", "orchestrator", closed_at=200)
        cases = {
            "another existing seat": valid(top_seat_id=MASTER_REVIEWER.id),
            "none where one is derived": valid(top_seat_id=None),
            "a seat where none is derived": valid(
                instances=[closed], seats=[seat(22, closed, "orchestrator-planner")],
                top_seat_id=u7(22)),
        }
        for case, call in sorted(cases.items()):
            with self.subTest(case=case):
                self.setUp()
                self.begin()
                with self.assertRaises(ii.IdentityImportRefused) as caught:
                    ii.import_identities_step(self.conn, **call)
                self.assertEqual(caught.exception.reason, "top_seat_mismatch")
                self.assertEqual(self.rows(), {table: [] for table in TABLES})
                self.assert_caller_intact()

    def test_retired_alias_refused(self):
        alias = seat(22, MASTER, "pair-planner")
        with self.assertRaises(ii.IdentityImportRefused) as caught:
            ii.derive_top_seat([MASTER], [MASTER_PLANNER, alias])
        self.assertEqual((caught.exception.reason, caught.exception.item),
                         ("invalid_role", "planner"))
        self.begin()
        with self.assertRaises(ii.IdentityImportRefused) as caught:
            ii.import_identities_step(self.conn, **valid(seats=[MASTER_PLANNER, alias]))
        self.assertEqual((caught.exception.reason, caught.exception.item),
                         ("invalid_role", "planner"))
        self.assertEqual(self.rows(), {table: [] for table in TABLES})


class TestTransaction(Ledger):
    def test_requires_open_transaction(self):
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(ValueError) as caught:
            ii.import_identities_step(self.conn, **valid())
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, ("transaction_required",))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.rows(), {table: [] for table in TABLES})

    def test_failure_after_writes_rolls_back_only_step(self):
        self.begin()
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_second_seat BEFORE INSERT ON seats "
            "WHEN (SELECT count(*) FROM seats) >= 1 "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            ii.import_identities_step(self.conn, **valid())
        self.assertIn("injected", str(caught.exception))
        self.assertEqual(self.rows(), {table: [] for table in TABLES})
        self.assert_caller_intact()

    def test_success_leaves_transaction_open(self):
        self.begin()
        ii.import_identities_step(self.conn, **valid())
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(len(self.rows()["seats"]), 2)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.rows(), {table: [] for table in TABLES})
        self.assertEqual(self.sentinels(), [])


class TestDerive(unittest.TestCase):
    def test_derive_top_seat_validates_inputs(self):
        bad_id = ii.ImportedTeamInstance(str(uuid.uuid4()), "master", "master",
                                         None, 100, None)
        upper_id = ii.ImportedTeamInstance(u7(10).upper(), "master", "master",
                                           None, 100, None)
        cycle_a = instance(13, "a", "pair", parent=u7(14), closed_at=200)
        cycle_b = instance(14, "b", "pair", parent=u7(13), closed_at=200)
        cases = {
            ("invalid_id", "not version 7"): ([bad_id], []),
            ("invalid_id", "not lowercase"): ([upper_id], []),
            ("invalid_id", "a seat reusing an instance id"): (
                [MASTER], [ii.ImportedSeat(MASTER.id, MASTER.id, "master-planner")]),
            ("invalid_name", "team"): ([instance(10, "Master", "master")], []),
            ("invalid_role", "unknown"): ([MASTER], [seat(22, MASTER, "captain")]),
            ("invalid_level", "unknown"): ([instance(10, "master", "sprint")], []),
            ("invalid_time", "negative"): ([instance(10, "master", "master",
                                                     opened_at=-1)], []),
            ("invalid_time", "bool"): ([instance(10, "master", "master",
                                                 opened_at=True)], []),
            ("invalid_time", "closed before opened"): (
                [instance(10, "master", "master", opened_at=100, closed_at=99)], []),
            ("unknown_parent", "absent"): ([instance(10, "master", "master",
                                                     parent=u7(99))], []),
            ("parent_cycle", "two instances"): ([cycle_a, cycle_b], []),
            ("parent_cycle", "self"): ([instance(13, "a", "pair", parent=u7(13),
                                                 closed_at=200)], []),
            ("open_name_duplicate", "two open"): (
                [MASTER, instance(11, "master", "pair")], []),
            ("unknown_instance", "absent"): (
                [MASTER], [ii.ImportedSeat(u7(22), u7(98), "master-planner")]),
            ("duplicate_seat", "same instance and role"): (
                [MASTER], [MASTER_PLANNER, seat(22, MASTER, "master-planner")]),
        }
        self.assertEqual({code for code, _ in cases}, {
            "invalid_id", "invalid_name", "invalid_role", "invalid_level",
            "invalid_time", "unknown_parent", "parent_cycle", "open_name_duplicate",
            "unknown_instance", "duplicate_seat"})
        for (code, case), (instances, seats) in sorted(cases.items()):
            with self.subTest(code=code, case=case):
                with self.assertRaises(ii.IdentityImportRefused) as caught:
                    ii.derive_top_seat(instances, seats)
                self.assertEqual(caught.exception.reason, code)
        closed_twice = [instance(11, "same", "pair", closed_at=200),
                        instance(12, "same", "pair", closed_at=200)]
        self.assertIsNone(ii.derive_top_seat(closed_twice, []))


class TestBoundary(unittest.TestCase):
    def test_module_boundary(self):
        with open(ii.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        modules = set()
        written = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                if node.module == "relay_engine":
                    modules.update("relay_engine." + alias.name for alias in node.names)
                else:
                    modules.add(node.module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                words = node.value.split()
                if words and words[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                    upper = [word.upper() for word in words]
                    target = (words[upper.index("INTO") + 1] if "INTO" in upper
                              else words[upper.index("FROM") + 1] if upper[0] == "DELETE"
                              else words[1])
                    written.add(target)
                self.assertNotIn(words[0].upper() if words else "",
                                 {"CREATE", "DROP", "ALTER", "COMMIT", "BEGIN"})
        engine = {name for name in modules if name.split(".")[0] == "relay_engine"}
        self.assertEqual(engine, {"relay_engine.addresses", "relay_engine.schema"})
        self.assertLessEqual({name.split(".")[0] for name in modules - engine},
                             set(sys.stdlib_module_names))
        self.assertEqual(written, set(TABLES))


if __name__ == "__main__":
    unittest.main()
