"""v3 tables: STRICT, versioned, atomic, checked against the header registry (DD-v3-engine § 4, § 8, § 9)."""

import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import errors
from relay_engine import header
from relay_engine import schema


TABLES = {"relays", "bodies", "recipients", "edges", "git_states", "findings",
          "finding_events", "bundles", "refusals", "seat_activity",
          "seat_sessions", "token_counts", "schema_version"}

INDEXES = {
    "relays": {("parent_id",), ("bundle_id",), ("project_id", "handle"),
               ("sender_seat_id",)},
    "recipients": {("seat_id",)},
    "edges": {("from_relay_id",), ("target_relay_id",),
              ("target_repository_id", "target_path"), ("target_path",)},
    "findings": {("bundle_id", "status"), ("raised_in",), ("resolved_in",)},
    "finding_events": {("finding_id",)},
    "refusals": {("project_id", "code")},
    "seat_sessions": {("seat_id",)},
    "token_counts": {("bundle_id",)},
}

SHA = "a" * 64


def check_values(conn, column):
    """The value set of `table.column IN (...)` read back from the stored CHECK text."""
    table, name = column.split(".")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    found = re.findall(r"(?<![A-Za-z_])%s IN \(([^)]*)\)" % name, sql)
    assert len(found) == 1, (column, found)
    return set(re.findall(r"'([^']*)'", found[0]))


class Store(unittest.TestCase):
    def connect(self, init=True):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        if init:
            schema.init_v3(conn)
        return conn

    def objects(self, conn):
        return sorted(conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall())

    def relay(self, conn, rid, handle, *, kind="SITREP", verdict=None, needs="none",
              sender="a.planner", created=None, body_sha=SHA, project="p"):
        conn.execute(
            "INSERT INTO relays (id, handle, project_id, kind, verdict, needs, subject,"
            " sender_seat_id, created_at, body_sha256, body_bytes, header_bytes)"
            " VALUES (?, ?, ?, ?, ?, ?, 's', ?, ?, ?, 0, 10)",
            (rid, handle, project, kind, verdict, needs, sender,
             handle if created is None else created, body_sha))

    def refused(self, conn, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, params)


class TestShape(Store):
    def test_tables_are_strict(self):
        conn = self.connect()
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

    def test_no_column_is_untyped(self):
        conn = self.connect()
        for table in TABLES:
            for _, column, kind, *_ in conn.execute(
                    "SELECT * FROM pragma_table_info(?)", (table,)):
                with self.subTest(column="%s.%s" % (table, column)):
                    self.assertIn(kind, {"INTEGER", "TEXT", "BLOB", "REAL"})

    def test_wrong_type_refused(self):
        conn = self.connect()
        sql = ("INSERT INTO refusals (project_id, seat_id, code, line, at, draft_sha256)"
               " VALUES ('p', 's', 'E-HEADER-GRAMMAR', ?, 1, ?)")
        self.refused(conn, sql, ("line three", SHA))
        conn.execute(sql, (3, SHA))  # positive control
        self.assertEqual(conn.execute("SELECT line FROM refusals").fetchone(), (3,))
        self.refused(conn, "INSERT INTO relays (id, handle, project_id, kind, needs, subject,"
                           " sender_seat_id, created_at, body_sha256, body_bytes, header_bytes)"
                           " VALUES ('r9', 'x', 'p', 'SITREP', 'none', 's', 'a.planner', 9, ?, 0, 1)",
                     (SHA,))

    def test_indexes_present(self):
        conn = self.connect()
        for table, wanted in INDEXES.items():
            with self.subTest(table=table):
                found = set()
                for (name,) in conn.execute(
                        "SELECT name FROM pragma_index_list(?)", (table,)).fetchall():
                    found.add(tuple(row[0] for row in conn.execute(
                        "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,))))
                self.assertLessEqual(wanted, found)
        named = {re.search(r"EXISTS (\w+) ON", sql).group(1) for sql in schema._INDEXES}
        stored = {name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(stored, named)
        self.assertEqual(len(named), 15)


class TestChecks(Store):
    def test_enum_checks_equal_the_registry(self):
        conn = self.connect()
        cases = (
            ("relays.kind", header.KINDS | {"BOOT"}),
            ("relays.needs", header.NEEDS),
            ("findings.severity", header.SEVERITIES),
            ("findings.status", {"open", "fixed", "fix-accepted", "withdrawn"}),
            ("finding_events.transition", {"raised", "fixes"} | header.RESOLUTIONS),
            ("recipients.role", {"to", "cc"}),
            ("edges.kind", {"parent", "ref"}),
            ("edges.target_type", {"relay", "git", "path"}),
            ("token_counts.source", {"host-log", "host-reported"}),
        )
        for column, wanted in cases:
            with self.subTest(column=column):
                self.assertEqual(check_values(conn, column), set(wanted))

    def test_kind_and_needs_rows(self):
        conn = self.connect()
        for handle, kind in enumerate(sorted(header.KINDS), start=1):
            verdict = sorted(header.VERDICTS[kind])[0] if kind in header.VERDICTS else None
            self.relay(conn, "k%d" % handle, handle, kind=kind, verdict=verdict)
        self.relay(conn, "boot", 100, kind="BOOT", sender="system")
        self.refused(conn, "UPDATE relays SET kind = 'NOTE' WHERE id = 'k1'")
        self.refused(conn, "UPDATE relays SET needs = NULL WHERE id = 'k1'")
        self.refused(conn, "UPDATE relays SET needs = 'maybe' WHERE id = 'k1'")

    def test_boot_only_from_system(self):
        conn = self.connect()
        self.relay(conn, "b1", 1, kind="BOOT", sender="system")
        with self.assertRaises(sqlite3.IntegrityError):
            self.relay(conn, "b2", 2, kind="BOOT", sender="a.planner")

    def test_verdict_tied_to_kind(self):
        conn = self.connect()
        self.relay(conn, "m1", 1, kind="MERGE-GATE", verdict="merge-ready", needs="act")
        self.relay(conn, "d1", 2, kind="DESIGN-REVIEW", verdict="approve", needs="act")
        for rid, handle, kind, verdict in (("x1", 3, "SITREP", "approve"),
                                           ("x2", 4, "MERGE-GATE", "approve"),
                                           ("x3", 5, "LIVE-VERIFY", "merge-ready")):
            with self.subTest(kind=kind, verdict=verdict):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.relay(conn, rid, handle, kind=kind, verdict=verdict)

    def test_same_millisecond_refused(self):
        conn = self.connect()
        self.relay(conn, "r1", 1, created=50)
        with self.assertRaises(sqlite3.IntegrityError):
            self.relay(conn, "r2", 2, created=50)
        self.relay(conn, "r3", 3, created=50, project="q")  # another project may share it

    def test_handle_immutable(self):
        conn = self.connect()
        self.relay(conn, "r1", 1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE relays SET handle = 2 WHERE id = 'r1'")
        conn.execute("UPDATE relays SET subject = 't' WHERE id = 'r1'")  # other columns change

    def test_digest_columns_are_hex(self):
        conn = self.connect()
        for bad in ("a" * 63, "A" * 64, "g" * 64):
            with self.subTest(bad=bad):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.relay(conn, "r-" + bad[:3], 1, body_sha=bad)
        self.refused(conn, "INSERT INTO refusals (code, at, draft_sha256) VALUES ('E-X', 1, 'z')")
        self.refused(conn, "INSERT INTO git_states (id, repository_id, head_commit, dirty_paths,"
                           " porcelain_sha256, measured_at) VALUES ('g', 'r', 'c', '[]', 'z', 1)")

    def test_foreign_keys(self):
        conn = self.connect()
        self.refused(conn, "INSERT INTO bodies (relay_id, body) VALUES ('nope', x'00')")
        self.relay(conn, "r1", 1)
        conn.execute("INSERT INTO bodies (relay_id, body) VALUES ('r1', x'00')")
        self.refused(conn, "INSERT INTO recipients (relay_id, seat_id, role) VALUES ('nope', 's', 'to')")
        self.refused(conn, "INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type,"
                           " spelling, target_relay_id) VALUES ('e', 'nope', 0, 'ref', 'relay', '#1', 'r1')")

    def test_recipient_and_edge_rows(self):
        conn = self.connect()
        self.relay(conn, "r1", 1)
        conn.execute("INSERT INTO recipients (relay_id, seat_id, role) VALUES ('r1', 's', 'to')")
        self.refused(conn, "INSERT INTO recipients (relay_id, seat_id, role) VALUES ('r1', 't', 'bcc')")
        edge = ("INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                " target_relay_id, target_repository_id, target_commit, target_path)"
                " VALUES (?, 'r1', ?, ?, ?, 's', ?, ?, ?, ?)")
        conn.execute(edge, ("e1", 0, "ref", "relay", "r1", None, None, None))
        conn.execute(edge, ("e2", 1, "ref", "git", None, "repo", "c" * 40, "a.md"))
        conn.execute(edge, ("e3", 2, "ref", "path", None, None, None, "~/x"))
        for args in (("e4", 3, "link", "relay", "r1", None, None, None),     # edge kind
                     ("e5", 4, "ref", "relay", None, None, None, None),       # relay without target
                     ("e6", 5, "ref", "git", None, "repo", None, "a.md"),     # git without commit
                     ("e7", 6, "ref", "path", None, None, None, None),        # path without path
                     ("e8", 7, "ref", "path", "r1", None, None, "~/x")):      # path with a relay
            with self.subTest(edge=args[0]):
                self.refused(conn, edge, args)

    def test_findings_rows(self):
        conn = self.connect()
        self.relay(conn, "r1", 1, kind="REVIEW", verdict="must-revise", needs="act")
        finding = ("INSERT INTO findings (id, handle, project_id, raised_in, raiser_seat_id,"
                   " severity, summary, status) VALUES (?, ?, 'p', 'r1', 's', ?, ?, ?)")
        conn.execute(finding, ("f1", 1, "blocking", "x" * 200, "withdrawn"))
        for args in (("f2", 2, "minor", "s", "open"), ("f3", 3, "advisory", "x" * 201, "open"),
                     ("f4", 4, "advisory", "s", "fix-refused")):
            with self.subTest(finding=args[0]):
                self.refused(conn, finding, args)
        event = ("INSERT INTO finding_events (finding_id, relay_id, transition, from_status,"
                 " to_status, at) VALUES ('f1', 'r1', ?, ?, ?, 1)")
        conn.execute(event, ("withdrawn", "open", "withdrawn"))
        for args in (("closed", "open", "withdrawn"), ("withdrawn", "shut", "withdrawn"),
                     ("withdrawn", "open", "gone")):
            with self.subTest(event=args):
                self.refused(conn, event, args)
        self.refused(conn, "INSERT INTO token_counts (seat_id, source, input, output,"
                           " cache_read, cache_write, copied_at) VALUES ('s', 'guess', 1, 1, 1, 1, 1)")


class TestVersioning(Store):
    def test_init_idempotent(self):
        conn = self.connect()
        before = self.objects(conn)
        schema.init_v3(conn)
        self.assertEqual(self.objects(conn), before)
        self.assertEqual(conn.execute("SELECT version, ddl_sha256 FROM schema_version").fetchall(),
                         [(schema.SCHEMA_V3, schema.ddl_digest())])

    def test_foreign_store_refused(self):
        conn = self.connect(init=False)
        conn.execute("CREATE TABLE relays (path TEXT)")  # a 2.9.7 ledger also has a relays table
        before = self.objects(conn)
        with self.assertRaises(errors.EngineError) as caught:
            schema.init_v3(conn)
        self.assertEqual(caught.exception.code, "E-SCHEMA-MISMATCH")
        self.assertIn("1 table", caught.exception.cause)
        self.assertEqual(self.objects(conn), before)  # nothing written

    def test_drifted_or_other_version_refused(self):
        for column, value, phrase in (("ddl_sha256", "b" * 64, "other table definitions"),
                                      ("version", "4", "version 4")):
            with self.subTest(column=column):
                conn = self.connect()
                conn.execute("UPDATE schema_version SET %s = ?" % column, (value,))
                conn.commit()
                before = self.objects(conn)
                with self.assertRaises(errors.EngineError) as caught:
                    schema.init_v3(conn)
                self.assertEqual(caught.exception.code, "E-SCHEMA-MISMATCH")
                self.assertIn(phrase, caught.exception.cause)
                self.assertEqual(self.objects(conn), before)

    def test_init_atomic(self):
        conn = self.connect(init=False)
        broken = schema._INDEXES + ("CREATE INDEX IF NOT EXISTS broken ON no_such_table (x)",)
        with mock.patch.object(schema, "_INDEXES", broken):
            with self.assertRaises(sqlite3.OperationalError):
                schema.init_v3(conn)
        self.assertEqual(self.objects(conn), [])
        schema.init_v3(conn)  # positive control: a clean run then creates everything
        self.assertEqual({name for kind, name, _ in self.objects(conn) if kind == "table"}, TABLES)

    def test_file_store_persists_version(self):
        with tempfile.TemporaryDirectory() as name:
            path = os.path.join(name, "store")
            with sqlite3.connect(path) as conn:
                schema.init_v3(conn)
            conn.close()
            reopened = sqlite3.connect(path)
            self.addCleanup(reopened.close)
            schema.init_v3(reopened)  # a matching store opens without change
            self.assertEqual(reopened.execute("SELECT version FROM schema_version").fetchone(),
                             (schema.SCHEMA_V3,))


class TestSqlVersion(unittest.TestCase):
    def test_old_sqlite_refused(self):
        with self.assertRaises(errors.EngineError) as caught:
            schema.require_sqlite((999, 0, 0))
        self.assertEqual(caught.exception.code, "E-SQLITE-VERSION")
        self.assertIn("999.0.0", caught.exception.cause)
        self.assertNotIn("3.37.0", caught.exception.remedy)  # the remedy names no fixed number
        schema.require_sqlite()
        for version, accepted in (((3, 37, 0), True), ((3, 36, 99), False), ((3, 36, 9), False)):
            with self.subTest(version=version):
                with mock.patch.object(schema.sqlite3, "sqlite_version_info", version):
                    if accepted:
                        schema.require_sqlite()
                    else:
                        with self.assertRaises(errors.EngineError):
                            schema.require_sqlite()

    def test_init_checks_the_version(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with mock.patch.object(schema.sqlite3, "sqlite_version_info", (3, 36, 0)):
            with self.assertRaises(errors.EngineError):
                schema.init_v3(conn)


if __name__ == "__main__":
    unittest.main()
