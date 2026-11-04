"""v3 tables: STRICT, idempotent, indexed (DD-v3-engine § 4, § 8, § 9)."""

import sqlite3
import unittest
from unittest import mock

from relay_engine import errors
from relay_engine import schema


TABLES = {"relays", "bodies", "recipients", "edges", "git_states", "findings",
          "finding_events", "bundles", "refusals", "seat_activity",
          "seat_sessions", "token_counts"}

INDEXES = {
    "relays": {("parent_id",), ("bundle_id",), ("project_id", "handle")},
    "recipients": {("seat_id",)},
    "edges": {("from_relay_id",), ("target_relay_id",),
              ("target_repository_id", "target_path"), ("target_path",)},
    "findings": {("bundle_id", "status")},
}


def connect():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    schema.init_v3(conn)
    return conn


def objects(conn):
    return sorted(conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall())


def index_columns(conn, table):
    found = set()
    for (name,) in conn.execute(
            "SELECT name FROM pragma_index_list(?)", (table,)).fetchall():
        found.add(tuple(row[0] for row in conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,))))
    return found


class TestSchema(unittest.TestCase):
    def test_tables_are_strict(self):
        conn = connect()
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        self.assertEqual({name for name, _ in rows}, TABLES)
        for name, sql in rows:
            with self.subTest(table=name):
                self.assertTrue(sql.rstrip().endswith("STRICT"), sql)
                strict = conn.execute(
                    "SELECT strict FROM pragma_table_list WHERE name = ?",
                    (name,)).fetchone()
                self.assertEqual(strict, (1,))

    def test_wrong_type_refused(self):
        conn = connect()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO refusals (project_id, seat_id, code, line, at, draft_sha256)"
                " VALUES ('p', 's', 'E-HEADER-GRAMMAR', 'line three', 1, 'd')")
        # positive control: the same row with an integer line is stored
        conn.execute(
            "INSERT INTO refusals (project_id, seat_id, code, line, at, draft_sha256)"
            " VALUES ('p', 's', 'E-HEADER-GRAMMAR', 3, 1, 'd')")
        self.assertEqual(conn.execute("SELECT line FROM refusals").fetchone(), (3,))

    def test_enums_follow_the_header_registry(self):
        conn = connect()
        row = ("r1", 1, "p", None, None, "SITREP", None, "none", "s", None,
               "a.planner", None, 1, "0" * 64, 0, 10)
        sql = ("INSERT INTO relays (id, handle, project_id, team_instance_id,"
               " bundle_id, kind, verdict, needs, subject, parent_id,"
               " sender_seat_id, sender_role, created_at, body_sha256,"
               " body_bytes, header_bytes) VALUES (%s)" % ",".join("?" * 16))
        conn.execute(sql, row)
        for column, value in ((5, "NOTE"), (7, "maybe"), (6, "approved")):
            bad = list(row)
            bad[0], bad[1], bad[12], bad[column] = "r2", 2, 2, value
            with self.subTest(value=value):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql, bad)

    def test_init_idempotent(self):
        conn = connect()
        before = objects(conn)
        schema.init_v3(conn)
        self.assertEqual(objects(conn), before)
        names = [name for _, name, _ in before]
        self.assertEqual(len(names), len(set(names)))

    def test_indexes_present(self):
        conn = connect()
        for table, wanted in INDEXES.items():
            with self.subTest(table=table):
                self.assertLessEqual(wanted, index_columns(conn, table))

    def test_old_sqlite_refused(self):
        with self.assertRaises(errors.EngineError) as caught:
            schema.require_sqlite((999, 0, 0))
        self.assertEqual(caught.exception.code, "E-SQLITE-VERSION")
        self.assertIn("999.0.0", caught.exception.cause)
        schema.require_sqlite()  # the running library meets the default minimum
        with mock.patch.object(schema.sqlite3, "sqlite_version_info", (3, 36, 9)):
            with self.assertRaises(errors.EngineError):
                schema.require_sqlite()

    def test_init_checks_the_version(self):
        with mock.patch.object(schema.sqlite3, "sqlite_version_info", (3, 36, 0)):
            with self.assertRaises(errors.EngineError):
                schema.init_v3(sqlite3.connect(":memory:"))


if __name__ == "__main__":
    unittest.main()
