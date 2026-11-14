"""v3 tables: STRICT, versioned, atomic, checked against the header registry (DD-v3-engine § 4, § 8, § 9)."""

from dataclasses import replace
import hashlib
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import errors
from relay_engine import header
from relay_engine import schema
from relay_engine import refs
from relay_engine.tests import test_submit
from relay_engine.tests.test_submit import Store as SubmitStore


TABLES = {"relays", "bodies", "recipients", "edges", "git_states", "findings",
          "finding_events", "bundles", "refusals", "seat_activity",
          "seat_sessions", "token_counts", "snapshots", "schema_version", "projects", "repositories",
          "repository_checkouts", "team_instances", "seats", "seat_occupants", "project_settings", "settings_history", "mutation_receipts",
          "lifecycle_outbox", "lifecycle_outbox_events",
          "evidence_objects", "evidence_object_bundles", "evidence_bundles",
          "evidence_captures", "evidence_uploads", "evidence_retention",
          "evidence_derive", "evidence_prune_marks", "evidence_pruned"}

INDEXES = {
    "snapshots": {("relay_id",), ("artifact", "handle"), ("content_sha256",)},
    "relays": {("parent_id",), ("bundle_id",), ("project_id", "handle"),
               ("sender_seat_id", "kind"), ("project_id", "id")},
    "recipients": {("seat_id",), ("relay_id",)},
    "edges": {("from_relay_id",), ("target_relay_id",),
              ("target_repository_id", "target_path"), ("target_path",)},
    "findings": {("bundle_id", "status"), ("raised_in",), ("resolved_in",)},
    "finding_events": {("finding_id",)},
    "refusals": {("project_id", "code")},
    "seat_sessions": {("seat_id",)},
    "token_counts": {("bundle_id",)},
}

SHA = "a" * 64

TABLES_QUERY = (
    "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")


def check_values(conn, column):
    """The value set of `table.column IN (...)` read back from the stored CHECK text."""
    table, name = column.split(".")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    found = re.findall(r"(?<![A-Za-z_])%s IN \(([^)]*)\)" % name, sql)
    assert len(found) == 1, (column, found)
    return set(re.findall(r"'([^']*)'", found[0]))


def platform_rows(conn):
    """The platform rows the engine's foreign keys need: projects p and q, an open pair team
    in each, seats s-a (planner, with occupant o1) and s-b in p, and repository repo-1."""
    conn.executescript("""
        INSERT INTO projects (id, name, created_at) VALUES ('p', 'p', 1), ('q', 'q', 1);
        INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)
            VALUES ('t1', 'p', 'v3-engine', 'pair', 1, 'operator'),
                   ('t9', 'q', 'v3-engine', 'pair', 1, 'operator');
        INSERT INTO seats (id, team_instance_id, role, state)
            VALUES ('s-a', 't1', 'planner', 'occupied'), ('s-b', 't1', 'implementer', 'vacant');
        INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)
            VALUES ('o1', 's-a', 'k1', 'v', 1);
        INSERT INTO repositories (id, project_id, name) VALUES ('repo-1', 'p', 'bivpak');
    """)


class Store(unittest.TestCase):
    def connect(self, init=True):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        if init:
            schema.init_v3(conn)
            platform_rows(conn)
        return conn

    def objects(self, conn):
        return sorted(conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall())

    def relay(self, conn, rid, handle, *, kind="SITREP", verdict=None, needs="none",
              sender="s-a", special=None, created=None, body_sha=SHA, project="p", meta_sha=SHA):
        conn.execute(
            "INSERT INTO relays (id, handle, project_id, kind, verdict, needs, subject,"
            " sender_seat_id, sender_special, created_at, body_sha256, meta_sha256, body_bytes,"
            " header_bytes) VALUES (?, ?, ?, ?, ?, ?, 's', ?, ?, ?, ?, ?, 0, 10)",
            (rid, handle, project, kind, verdict, needs,
             None if sender in schema.SPECIALS else sender,
             sender if sender in schema.SPECIALS else special,
             handle if created is None else created, body_sha, meta_sha))

    def refused(self, conn, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, params)


class TestReservedNames(Store):
    @staticmethod
    def catalogue(conn):
        # Independent of the production filter and Store.objects' old wildcard.
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name").fetchall()

    @staticmethod
    def opening_error(conn):
        try:
            schema.init_v3(conn)
        except errors.EngineError as error:
            return error
        return None

    def test_lookalike_objects_refuse(self):
        # Breaks: _definitions hides user objects after an unescaped sqlite_ prefix.
        cases = (
            ("table", "sqliteX", "CREATE TABLE sqliteX (value TEXT)"),
            ("index", "sqlite1", "CREATE INDEX sqlite1 ON relays (subject)"),
            ("view", "sqlitev", "CREATE VIEW sqlitev AS SELECT id FROM relays"),
            ("trigger", "sqlitet", "CREATE TRIGGER sqlitet AFTER INSERT ON relays "
             "BEGIN SELECT 1; END"),
            ("table", "SQLiteX", "CREATE TABLE SQLiteX (value TEXT)"),
        )
        for kind, name, statement in cases:
            with self.subTest(kind=kind, name=name):
                conn = self.connect(init=False)
                schema.init_v3(conn)
                conn.execute(statement)
                conn.commit()
                before = self.catalogue(conn)
                self.assertIn((kind, name), {(row[0], row[1]) for row in before})
                error = self.opening_error(conn)
                self.assertIsNotNone(error, "a lookalike user object must refuse")
                self.assertEqual(error.code, "E-SCHEMA-MISMATCH")
                self.assertIn("an extra %s %s" % (kind, name), error.cause)
                self.assertEqual(self.catalogue(conn), before)
                self.assertFalse(conn.in_transaction)

    def test_lookalike_only_file_refuses(self):
        # Breaks: init_v3 treats a lookalike-only file as empty, or counts 0 tables.
        conn = self.connect(init=False)
        conn.execute("CREATE TABLE sqliteX (value TEXT)")
        conn.commit()
        before = self.catalogue(conn)
        error = self.opening_error(conn)
        self.assertIsNotNone(error, "a nonempty lookalike-only file must refuse")
        self.assertEqual(error.code, "E-SCHEMA-MISMATCH")
        self.assertIn("1 table and no v3 version record", error.cause)
        self.assertEqual(self.catalogue(conn), before)
        self.assertEqual(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall(), [("sqliteX",)])
        self.assertFalse(conn.in_transaction)

    def test_sqlite_owned_objects_still_ignored(self):
        # Breaks: a literal backslash without ESCAPE exposes sqlite_stat1 as drift.
        for analysed in (False, True):
            with self.subTest(analysed=analysed):
                conn = self.connect(init=False)
                schema.init_v3(conn)
                if analysed:
                    conn.execute("ANALYZE")
                    conn.commit()
                    self.assertIsNotNone(conn.execute(
                        "SELECT name FROM sqlite_master WHERE name='sqlite_stat1'"
                    ).fetchone(), "ANALYZE must supply the SQLite-owned control")
                before = self.catalogue(conn)
                version = conn.execute("SELECT * FROM schema_version").fetchall()
                error = self.opening_error(conn)
                self.assertIsNone(error, str(error))
                self.assertEqual(self.catalogue(conn), before)
                self.assertEqual(conn.execute("SELECT * FROM schema_version").fetchall(),
                                 version)
                self.assertFalse(conn.in_transaction)

    def test_sqlite_owned_only_file_initializes(self):
        # Breaks: init_v3's emptiness check counts sqlite_sequence as a user object.
        conn = self.connect(init=False)
        conn.execute("CREATE TABLE counted (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        conn.execute("INSERT INTO counted (value) VALUES ('one')")
        conn.execute("DROP TABLE counted")
        conn.commit()
        self.assertEqual([row[1] for row in self.catalogue(conn)], ["sqlite_sequence"])
        error = self.opening_error(conn)
        self.assertIsNone(error, "a file holding only SQLite's own objects must initialize")
        self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchall(),
                         [(schema.SCHEMA_V3,)])
        names = {row[1] for row in self.catalogue(conn) if row[0] == "table"}
        for sql in schema._TABLES:
            table = re.search(r"CREATE TABLE IF NOT EXISTS (\w+) ", sql).group(1)
            self.assertIn(table, names)
        self.assertFalse(conn.in_transaction)

    def test_autoincrement_only_file_counts_one_table(self):
        # Breaks: the table count includes sqlite_sequence or the index.
        conn = self.connect(init=False)
        conn.execute("CREATE TABLE counted (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        conn.execute("CREATE INDEX counted_value ON counted (value)")
        conn.execute("INSERT INTO counted (value) VALUES ('one')")
        conn.commit()
        before = self.catalogue(conn)
        self.assertEqual({row[1] for row in before},
                         {"counted", "counted_value", "sqlite_sequence"})
        error = self.opening_error(conn)
        self.assertIsNotNone(error, "a file with one user table and no version record must refuse")
        self.assertEqual(error.code, "E-SCHEMA-MISMATCH")
        self.assertIn("1 table and no v3 version record", error.cause)
        self.assertEqual(self.catalogue(conn), before)
        self.assertFalse(conn.in_transaction)


class TestShape(Store):
    def test_tables_are_strict(self):
        conn = self.connect()
        rows = conn.execute(TABLES_QUERY).fetchall()
        self.assertEqual({name for name, _ in rows}, TABLES)
        for name, sql in rows:
            with self.subTest(table=name):
                self.assertTrue(sql.rstrip().endswith("STRICT"), sql)
                strict = conn.execute(
                    "SELECT strict FROM pragma_table_list WHERE name = ?",
                    (name,)).fetchone()
                self.assertEqual(strict, (1,))

    def test_tables_query_keeps_sqlite_lookalikes(self):
        conn = self.connect()
        conn.execute("CREATE TABLE sqliteX (a INTEGER) STRICT")
        self.assertIn("sqliteX", {name for name, _ in conn.execute(TABLES_QUERY)})

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
               " VALUES ('p', 's-a', 'E-HEADER-GRAMMAR', ?, 1, ?)")
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
        self.assertEqual(len(named), 29)


class TestChecks(Store):
    def test_snapshots_table_shape(self):
        conn = self.connect()
        self.assertIn("snapshots", {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertEqual({r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}, {"id", "handle", "relay_id", "edge_id", "artifact", "content_sha256", "bytes_len", "has_bytes", "locked_by", "locked_at"})
        self.assertEqual(conn.execute("SELECT strict FROM pragma_table_list WHERE name='snapshots'").fetchone(), (1,))
        self.relay(conn, "r1", 1)
        self.relay(conn, "r2", 2)
        conn.execute("INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                     " target_path, digest_at_submit) VALUES ('e1', 'r1', 1, 'ref', 'path', 's',"
                     " '/p', ?)", (SHA,))
        conn.execute("INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                     " target_path, digest_at_submit) VALUES ('e2', 'r1', 2, 'ref', 'path', 's',"
                     " '/q', ?)", (SHA,))
        row = ("INSERT INTO snapshots (id, handle, relay_id, edge_id, artifact, content_sha256,"
               " bytes_len, has_bytes, locked_by, locked_at) VALUES (?, ?, 'r1', ?, '/p', ?, ?, ?,"
               " ?, ?)")
        conn.execute(row, ("s1", 1, "e1", SHA, 3, 1, None, None))
        conn.execute("UPDATE snapshots SET locked_by = 'r2', locked_at = 5 WHERE id = 's1'")
        for name, values in (("handle taken", ("s2", 1, "e2", SHA, 3, 1, None, None)),
                             ("edge taken", ("s2", 2, "e1", SHA, 3, 1, None, None)),
                             ("handle zero", ("s2", 0, "e2", SHA, 3, 1, None, None)),
                             ("bad digest", ("s2", 2, "e2", "b" * 40, 3, 1, None, None)),
                             ("negative length", ("s2", 2, "e2", SHA, -1, 1, None, None)),
                             ("has_bytes 2", ("s2", 2, "e2", SHA, 3, 2, None, None)),
                             ("lock without time", ("s2", 2, "e2", SHA, 3, 1, "r2", None)),
                             ("time without lock", ("s2", 2, "e2", SHA, 3, 1, None, 5)),
                             ("unknown lock relay", ("s2", 2, "e2", SHA, 3, 1, "nope", 5))):
            with self.subTest(name):
                self.refused(conn, row, values)
        conn.execute(row, ("s2", 2, "e2", SHA, 3, 0, "r2", 5))  # positive control


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
            self.relay(conn, "b2", 2, kind="BOOT", sender="s-a")

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
                           " porcelain_sha256, measured_at) VALUES ('g', 'r', NULL, 0, 'z', 1)")

    def test_submit_columns(self):  # Task 4 rulings
        conn = self.connect()
        for bad in (None, "a" * 63, "A" * 64):
            with self.subTest(meta=bad):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.relay(conn, "m-%s" % bad, 1, meta_sha=bad)
        state = ("INSERT INTO git_states (id, repository_id, head_commit, dirty_paths,"
                 " porcelain_sha256, measured_at) VALUES (?, 'repo-1', ?, ?, ?, 1)")
        conn.execute(state, ("g1", None, 0, SHA))          # an unborn branch has no head
        conn.execute(state, ("g2", "c" * 40, 3, SHA))
        conn.execute(state, ("g3", "c" * 64, 0, SHA))
        for name, head, dirty in (("g4", "c" * 39, 0), ("g5", "C" * 40, 0), ("g6", "c" * 40, -1),
                                  ("g7", "c" * 40, "[]")):
            with self.subTest(state=name):
                self.refused(conn, state, (name, head, dirty, SHA))
        self.relay(conn, "r1", 1)
        edge = ("INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                " target_relay_id, target_repository_name, target_repository_id, target_commit,"
                " target_path, digest_at_submit) VALUES (?1, 'r1', ?2, 'ref', ?3, 's', NULL,"
                " (SELECT name FROM repositories WHERE id = ?4), ?4, ?5, '/p', ?6)")
        conn.execute(edge, ("e1", 1, "git", "repo-1", "c" * 40, "b" * 40))   # a Git blob id
        conn.execute(edge, ("e2", 2, "git", "repo-1", "c" * 40, "b" * 64))
        conn.execute(edge, ("e3", 3, "path", None, None, SHA))
        self.refused(conn, edge, ("e4", 4, "path", None, None, "b" * 40))  # a file: sha256 only
        self.refused(conn, edge, ("e5", 5, "git", "repo-1", "c" * 40, "B" * 40))

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
        conn.execute("INSERT INTO recipients (relay_id, seat_id, role) VALUES ('r1', 's-a', 'to')")
        self.refused(conn, "INSERT INTO recipients (relay_id, seat_id, role) VALUES ('r1', 's-b', 'bcc')")
        edge = ("INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                " target_relay_id, target_repository_name, target_repository_id, target_commit,"
                " target_path) VALUES (?1, 'r1', ?2, ?3, ?4, 's', ?5,"
                " (SELECT name FROM repositories WHERE id = ?6), ?6, ?7, ?8)")
        conn.execute(edge, ("e1", 0, "ref", "relay", "r1", None, None, None))
        conn.execute(edge, ("e2", 1, "ref", "git", None, "repo-1", "c" * 40, "a.md"))
        conn.execute(edge, ("e3", 2, "ref", "path", None, None, None, "~/x"))
        for args in (("e4", 3, "link", "relay", "r1", None, None, None),     # edge kind
                     ("e5", 4, "ref", "relay", None, None, None, None),       # relay without target
                     ("e6", 5, "ref", "git", None, "repo-1", None, "a.md"),   # git without commit
                     ("e7", 6, "ref", "path", None, None, None, None),        # path without path
                     ("e8", 7, "ref", "path", "r1", None, None, "~/x")):      # path with a relay
            with self.subTest(edge=args[0]):
                self.refused(conn, edge, args)

    def test_findings_rows(self):
        conn = self.connect()
        self.relay(conn, "r1", 1, kind="REVIEW", verdict="must-revise", needs="act")
        finding = ("INSERT INTO findings (id, handle, project_id, raised_in, raiser_seat_id,"
                   " severity, summary, status) VALUES (?, ?, 'p', 'r1', 's-a', ?, ?, ?)")
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

    def test_changed_definitions_refused(self):  # A13
        changes = (
            ("CREATE TABLE extra (x INTEGER) STRICT", "an extra table extra"),
            ("ALTER TABLE bodies ADD COLUMN note TEXT", "a changed table bodies"),
            ("DROP INDEX findings_bundle_status", "no index findings_bundle_status"),
            ("DROP TRIGGER relays_handle_fixed", "no trigger relays_handle_fixed"),
        )
        for statement, phrase in changes:
            with self.subTest(statement=statement):
                conn = self.connect()
                conn.execute(statement)
                conn.commit()
                before = self.objects(conn)
                with self.assertRaises(errors.EngineError) as caught:
                    schema.init_v3(conn)
                self.assertEqual(caught.exception.code, "E-SCHEMA-MISMATCH")
                self.assertIn(phrase, caught.exception.cause)
                self.assertEqual(self.objects(conn), before)  # nothing written
        conn = self.connect()
        schema.init_v3(conn)  # positive control: an unchanged store opens

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



# J3 with amendment J-A1, transcribed from the frozen copy
# designs/DD-v3-platform-20261104-jl-a1.md, sha256
# 6e2022761164a7b3d8f2df3653a35fe58bdae11d4d42930dbc979735ad2e5dcd (never read at test time:
# the sprint tree is untracked and absent from worktrees).
PLATFORM_COLUMNS = {
    "projects": ["id", "name", "created_at", "top_seat_id"],
    "repositories": ["id", "project_id", "name"],
    "repository_checkouts": ["repository_id", "path", "last_seen_at", "checkout_id", "route_created_at", "ended_at"],
    "team_instances": ["id", "project_id", "name", "level", "parent_instance_id", "opened_at",
                       "opened_by", "closed_at"],
    "seats": ["id", "team_instance_id", "role", "state"],
    "seat_occupants": ["id", "seat_id", "key_id", "key_verifier", "issued_at", "ended_at"],
    "seat_sessions": ["id", "seat_id", "occupant_id", "host", "host_session_id", "generation",
                      "signed_in_at", "last_seen_at", "ended_at"],
    "mutation_receipts": ["principal_kind", "principal_id", "operation", "operation_key",
                          "request_digest", "result", "committed_at"],
}
# Exclusive arcs (v3-engine/SITREP-pair-planner-20261104-074637.md Q1): column prefix, specials.
ARCS = {"relays": ("sender_", {"operator", "system"}), "recipients": ("", {"operator"}),
        "refusals": ("", {"operator", "system"}), "bundles": ("opened_by_", {"operator", "system"})}


class TestPlatform(Store):
    def test_platform_tables_match_spec(self):
        conn = self.connect()
        for table, columns in PLATFORM_COLUMNS.items():
            with self.subTest(table=table):
                self.assertEqual([row[1] for row in conn.execute(
                    "SELECT * FROM pragma_table_info(?)", (table,))], columns)

    def test_seat_sessions_ja1(self):
        conn = self.connect()
        conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                     " VALUES ('o2', 's-b', 'k2', ?, 1)", (SHA,))
        row = ("INSERT INTO seat_sessions (id, seat_id, occupant_id, host, host_session_id,"
               " generation, signed_in_at, last_seen_at, ended_at) VALUES (?, ?, ?, ?, 'h', ?, 1, 1,"
               " ?)")
        conn.execute(row, ("x1", "s-a", "o1", "claude-code", 1, None))
        for name, values in (("second open session", ("x2", "s-a", "o1", "codex", 2, None)),
                             ("generation reused", ("x2", "s-a", "o1", "codex", 1, 5)),
                             ("generation zero", ("x2", "s-a", "o1", "codex", 0, 5)),
                             ("unknown host", ("x2", "s-a", "o1", "vim", 2, 5)),
                             ("another seat's occupant", ("x2", "s-a", "o2", "codex", 2, 5)),
                             ("generation as text", ("x2", "s-a", "o1", "codex", "two", 5))):
            with self.subTest(name):
                self.refused(conn, row, values)
        conn.execute("UPDATE seat_sessions SET ended_at = 2 WHERE id = 'x1'")
        for host, generation in (("codex", 2), ("frank", 3)):  # positive control
            conn.execute("UPDATE seat_sessions SET ended_at = 9 WHERE ended_at IS NULL")
            conn.execute(row, ("x%d" % generation, "s-a", "o1", host, generation, None))
        conn.execute(row, ("y1", "s-b", "o2", "other", 1, None))  # generations are per seat

    def test_platform_invariants(self):
        conn = self.connect()
        cases = (
            ("project name taken", "INSERT INTO projects (id, name, created_at) VALUES ('p3', 'p',"
             " 1)"),
            ("repository name taken in its project",
             "INSERT INTO repositories (id, project_id, name) VALUES ('r2', 'p', 'bivpak')"),
            ("open team name taken", "INSERT INTO team_instances (id, project_id, name, level,"
             " opened_at, opened_by) VALUES ('t2', 'p', 'v3-engine', 'pair', 1, 'operator')"),
            ("unknown level", "INSERT INTO team_instances (id, project_id, name, level, opened_at,"
             " opened_by) VALUES ('t2', 'p', 'new', 'squad', 1, 'operator')"),
            ("parent in another project", "INSERT INTO team_instances (id, project_id, name,"
             " level, parent_instance_id, opened_at, opened_by) VALUES ('t2', 'q', 'new', 'pair',"
             " 't1', 1, 'operator')"),
            ("seat role taken", "INSERT INTO seats (id, team_instance_id, role, state) VALUES"
             " ('s-z', 't1', 'planner', 'vacant')"),
            ("unknown seat state", "INSERT INTO seats (id, team_instance_id, role, state) VALUES"
             " ('s-z', 't1', 'reviewer', 'gone')"),
            ("second open occupant", "INSERT INTO seat_occupants (id, seat_id, key_id,"
             " key_verifier, issued_at) VALUES ('o9', 's-a', 'k9', 'v', 1)"),
            ("checkout of an unknown repository", "INSERT INTO repository_checkouts"
             " (repository_id, path, last_seen_at, checkout_id, route_created_at)"
             " VALUES ('nope', '/x', 1, 'checkout-unknown', 1)"),
        )
        for name, sql in cases:
            with self.subTest(name):
                self.refused(conn, sql)
        # positive controls: same names elsewhere, a closed team's name reused, a sub-team
        conn.execute("INSERT INTO repositories (id, project_id, name) VALUES ('r2', 'q', 'bivpak')")
        conn.execute("UPDATE team_instances SET closed_at = 5 WHERE id = 't1'")
        conn.execute("INSERT INTO team_instances (id, project_id, name, level, parent_instance_id,"
                     " opened_at, opened_by) VALUES ('t2', 'p', 'v3-engine', 'pair', 't1', 6,"
                     " 'operator')")
        conn.execute("INSERT INTO seats (id, team_instance_id, role, state) VALUES"
                     " ('s-z', 't2', 'planner', 'vacant')")
        conn.execute("UPDATE seat_occupants SET ended_at = 2 WHERE id = 'o1'")
        conn.execute("INSERT INTO seat_occupants (id, seat_id, key_id, key_verifier, issued_at)"
                     " VALUES ('o9', 's-a', 'k9', 'v', 3)")

    def arc_insert(self, conn, table, seat, special):
        prefix = ARCS[table][0]
        if table == "relays":
            self.relay(conn, "r-arc", 9, sender=seat, special=special)
        elif table == "recipients":
            self.relay(conn, "r-to", 8)
            conn.execute("INSERT INTO recipients (relay_id, seat_id, special, role) VALUES"
                         " ('r-to', ?, ?, 'to')", (seat, special))
        elif table == "refusals":
            conn.execute("INSERT INTO refusals (project_id, seat_id, special, code, at,"
                         " draft_sha256) VALUES ('p', ?, ?, 'E-HEADER-GRAMMAR', 1, ?)",
                         (seat, special, SHA))
        else:
            conn.execute("INSERT INTO bundles (id, project_id, name, %sseat_id, %sspecial) VALUES"
                         " ('b-arc', 'p', 'arc', ?, ?)" % (prefix, prefix), (seat, special))

    def test_party_arcs(self):
        for table, (_, allowed) in ARCS.items():
            for seat, special, admitted in (
                    [("s-a", None, True), ("s-a", "operator", False), (None, None, False),
                     ("nope", None, False)]
                    + [(None, name, name in allowed) for name in ("operator", "system", "x")]):
                with self.subTest(table=table, seat=seat, special=special):
                    conn = self.connect()
                    if admitted:
                        self.arc_insert(conn, table, seat, special)
                    else:
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.arc_insert(conn, table, seat, special)

    def test_seat_only_columns(self):
        conn = self.connect()
        for sql in ("INSERT INTO seat_activity (seat_id, last_request_at) VALUES (?, 1)",
                    "INSERT INTO token_counts (seat_id, source, input, output, cache_read,"
                    " cache_write, copied_at) VALUES (?, 'host-log', 0, 0, 0, 0, 1)"):
            with self.subTest(sql=sql):
                for value in ("nope", "operator", None):
                    self.refused(conn, sql, (value,))
                conn.execute(sql, ("s-a",))

    def test_engine_foreign_keys(self):
        conn = self.connect()
        self.relay(conn, "r1", 1)
        for name, sql in (
                ("relay in an unknown project", "INSERT INTO relays (id, handle, project_id,"
                 " kind, needs, subject, sender_seat_id, created_at, body_sha256, meta_sha256,"
                 " body_bytes, header_bytes) VALUES ('r9', 9, 'zz', 'SITREP', 'none', 's', 's-a',"
                 " 9, '%s', '%s', 0, 1)" % (SHA, SHA)),
                ("relay in an unknown team", "UPDATE relays SET team_instance_id = 'nope'"),
                ("finding raised by an unknown seat", "INSERT INTO findings (id, handle,"
                 " project_id, raised_in, raiser_seat_id, severity, summary, status) VALUES"
                 " ('f', 1, 'p', 'r1', 'nope', 'advisory', 's', 'open')"),
                ("git state of an unknown repository", "INSERT INTO git_states (id,"
                 " repository_id, dirty_paths, porcelain_sha256, measured_at) VALUES ('g',"
                 " 'bivpak', 0, '%s', 1)" % SHA),
                ("edge to an unknown repository", "INSERT INTO edges (id, from_relay_id, ordinal,"
                 " kind, target_type, spelling, target_repository_name, target_repository_id,"
                 " target_commit, target_path) VALUES ('e', 'r1', 1, 'ref', 'git', 's', 'bivpak',"
                 " 'nope', '%s', 'a')" % ("c" * 40)),
                ("git edge without its repository id", "INSERT INTO edges (id, from_relay_id,"
                 " ordinal, kind, target_type, spelling, target_repository_name, target_commit,"
                 " target_path) VALUES ('e', 'r1', 1, 'ref', 'git', 's', 'bivpak', '%s', 'a')"
                 % ("c" * 40))):
            with self.subTest(name):
                self.refused(conn, sql)
        conn.execute("INSERT INTO git_states (id, repository_id, dirty_paths, porcelain_sha256,"
                     " measured_at) VALUES ('g', 'repo-1', 0, ?, 1)", (SHA,))
        conn.execute("INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                     " target_repository_name, target_repository_id, target_commit, target_path)"
                     " VALUES ('e', 'r1', 1, 'ref', 'git', 's', 'bivpak', 'repo-1', ?, 'a')",
                     ("c" * 40,))


class TestMutationReceipts(Store):
    """Slice A receipts stay scoped to an occupant and operation, without cascades."""

    def test_mutation_receipts_constraints(self):
        conn = self.connect()
        self.assertIn("mutation_receipts", {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})
        sql = ("INSERT INTO mutation_receipts (principal_kind,principal_id,operation,"
               "operation_key,request_digest,result,committed_at) VALUES (?,?,?,?,?,?,?)")
        valid = ("occupant", "o1", "session.sign-in", "same-key", SHA, '{"ok":true}', 2)
        conn.execute(sql, valid)
        for name, index, value in (("wrong principal kind", 0, "operator"),
                                    ("unknown occupant", 1, "missing"),
                                    ("non-hex digest", 4, "g" * 64),
                                    ("short digest", 4, "a" * 63),
                                    ("uppercase digest", 4, "A" * 64),
                                    ("duplicate scope", 5, '{"ok":false}')):
            with self.subTest(name=name):
                values = list(valid)
                if name != "duplicate scope":
                    values[3] = "refused-" + name
                values[index] = value
                self.refused(conn, sql, values)
        for index in range(7):
            with self.subTest(null_column=index):
                values = list(valid)
                values[3] = "null-column-%d" % index
                values[index] = None
                self.refused(conn, sql, values)
        # Same key on another operation or occupant is a separate receipt.
        values = list(valid)
        values[2] = "session.end"
        conn.execute(sql, values)
        conn.execute("INSERT INTO seat_occupants (id,seat_id,key_id,key_verifier,issued_at)"
                     " VALUES ('o2','s-b','k2',?,1)", (SHA,))
        values = list(valid)
        values[1] = "o2"
        conn.execute(sql, values)
        self.assertEqual(conn.execute("SELECT count(*) FROM mutation_receipts").fetchone(), (3,))
        self.assertEqual(conn.execute(
            "SELECT result,committed_at FROM mutation_receipts WHERE principal_id='o1'"
            " AND operation='session.sign-in'").fetchone(), ('{"ok":true}', 2))

    def test_mutation_receipts_has_no_trigger_or_cascade(self):
        conn = self.connect()
        self.assertIn("mutation_receipts", {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertEqual(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mutation_receipts'"
        ).fetchall(), [])
        keys = conn.execute("PRAGMA foreign_key_list(mutation_receipts)").fetchall()
        self.assertEqual([(row[2], row[3], row[4], row[5], row[6]) for row in keys],
                         [("seat_occupants", "principal_id", "id", "NO ACTION", "NO ACTION")])
        indexes = conn.execute("PRAGMA index_list(mutation_receipts)").fetchall()
        unique = [row[1] for row in indexes if row[2]]
        self.assertEqual(len(unique), 1)
        self.assertEqual([row[2] for row in conn.execute(
            "SELECT * FROM pragma_index_info(?) ORDER BY seqno", (unique[0],))],
            ["principal_kind", "principal_id", "operation", "operation_key"])


class TestAdmissionReplaySchema(Store):
    """Breaks: unpaired receipt metadata, seat-only uniqueness and silent DDL drift."""

    def require_replay_columns(self, conn):
        names = {row[1] for row in conn.execute("PRAGMA table_info(relays)")}
        self.assertIn("submission_key", names, "replay key must be persisted atomically")
        self.assertIn("draft_sha256", names, "the full original input digest must be persisted")

    def test_replay_schema_pairing(self):
        conn = self.connect()
        self.require_replay_columns(conn)
        self.relay(conn, "seat", 1)
        self.relay(conn, "system", 2, sender="system")
        self.assertEqual(conn.execute(
            "SELECT submission_key, draft_sha256 FROM relays WHERE id='system'").fetchone(),
            (None, None))
        for key, digest in (("key", None), (None, SHA), ("", SHA), ("key", "bad"),
                            ("key", "A" * 64), ("key", b"x" * 64)):
            with self.subTest(key=key, digest=digest):
                self.refused(conn, "UPDATE relays SET submission_key=?, draft_sha256=?"
                                   " WHERE id='seat'", (key, digest))
        conn.execute("UPDATE relays SET submission_key='valid', draft_sha256=? WHERE id='seat'",
                     (SHA,))
        self.assertEqual(conn.execute(
            "SELECT submission_key, draft_sha256 FROM relays WHERE id='seat'").fetchone(),
            ("valid", SHA))

    def test_replay_party_unique_index(self):
        conn = self.connect()
        self.require_replay_columns(conn)
        for rid, handle, sender in (("a", 1, "s-a"), ("b", 2, "s-b"),
                                    ("o1", 3, "operator"), ("o2", 4, "operator"),
                                    ("a2", 5, "s-a")):
            self.relay(conn, rid, handle, sender=sender)
        for rid in ("a", "b", "o1"):
            conn.execute("UPDATE relays SET submission_key='same', draft_sha256=? WHERE id=?",
                         (SHA, rid))
        for rid in ("o2", "a2"):
            self.refused(conn, "UPDATE relays SET submission_key='same', draft_sha256=? WHERE id=?",
                         (SHA, rid))
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM relays WHERE submission_key='same'").fetchone(), (3,))

    def test_replay_ddl_digest_refuses_old_store(self):
        # Build a real pre-replay store with its own genuine DDL digest.
        old_tables = tuple(re.sub(
            r"    submission_key TEXT[\s\S]*?(?=    UNIQUE \(project_id, handle\))", "", sql)
            if "CREATE TABLE IF NOT EXISTS relays " in sql else sql
            for sql in schema._TABLES)
        old_indexes = tuple(sql for sql in schema._INDEXES
                            if "relays_submission_party" not in sql)
        conn = self.connect(init=False)
        with mock.patch.object(schema, "_TABLES", old_tables), mock.patch.object(
                schema, "_INDEXES", old_indexes):
            schema.init_v3(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(relays)")}
        self.assertNotIn("submission_key", columns)
        self.assertNotIn("draft_sha256", columns)
        before = self.objects(conn)
        digest = conn.execute("SELECT ddl_sha256 FROM schema_version").fetchone()
        with self.assertRaises(errors.EngineError) as caught:
            schema.init_v3(conn)
        self.assertEqual(caught.exception.code, "E-SCHEMA-MISMATCH")
        self.assertEqual(self.objects(conn), before)
        self.assertEqual(conn.execute("SELECT ddl_sha256 FROM schema_version").fetchone(), digest)
        control = self.connect()
        objects = self.objects(control)
        schema.init_v3(control)
        self.assertEqual(self.objects(control), objects)



class TestTableOwners(Store):
    """Breaks: incomplete or permissive production ownership declarations."""

    def test_table_owners_complete(self):
        self.assertTrue(hasattr(schema, "TABLE_OWNERS"),
                        "the hook needs the complete production table-owner registry")
        conn = self.connect()
        actual = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(set(schema.TABLE_OWNERS), actual)
        platform = {"projects", "repositories", "repository_checkouts", "team_instances",
                    "seats", "seat_occupants", "seat_sessions", "mutation_receipts"}
        store = {"evidence_objects", "evidence_object_bundles", "evidence_bundles",
                 "evidence_captures", "evidence_uploads", "evidence_retention",
                 "evidence_derive", "evidence_prune_marks", "evidence_pruned"}
        self.assertEqual(len(actual), 34)
        self.assertEqual({t for t, owner in schema.TABLE_OWNERS.items() if owner == "platform"}, platform)
        self.assertEqual({t for t, owner in schema.TABLE_OWNERS.items() if owner == "store"}, store)
        console = {"lifecycle_outbox", "lifecycle_outbox_events"}
        self.assertEqual({t for t, owner in schema.TABLE_OWNERS.items() if owner == "console"}, console)
        self.assertEqual({t for t, owner in schema.TABLE_OWNERS.items() if owner == "engine"}, actual - platform - console - store)
        fixture = SubmitStore()
        fixture.setUp()
        try:
            for table in sorted(actual | {"missing_table"}):
                with self.subTest(table=table):
                    with self.assertRaises(ValueError):
                        replace(fixture.ctx(), after_admit_writes=frozenset({table}))
        finally:
            fixture.doCleanups()


class TestSnapshotSchema(Store):
    def test_snapshots_ddl_refuses_bf2deda_store(self):
        self.assertIn("snapshots", schema.TABLE_OWNERS)
        tables = tuple((t for t in schema._TABLES if "CREATE TABLE IF NOT EXISTS snapshots " not in t))
        indexes = tuple((t for t in schema._INDEXES if " ON snapshots " not in t))
        conn = self.connect(init=False)
        with mock.patch.object(schema, "_TABLES", tables), mock.patch.object(schema, "_INDEXES",
            indexes):
            schema.init_v3(conn)
        before = self.objects(conn)
        digest = conn.execute("SELECT ddl_sha256 FROM schema_version").fetchone()
        with self.assertRaises(errors.EngineError) as caught:
            schema.init_v3(conn)
        self.assertEqual(caught.exception.code, "E-SCHEMA-MISMATCH")
        self.assertEqual(self.objects(conn), before)
        self.assertEqual(conn.execute("SELECT ddl_sha256 FROM schema_version").fetchone(), digest)
        fresh = self.connect()
        objects = self.objects(fresh)
        schema.init_v3(fresh)
        self.assertEqual(self.objects(fresh), objects)

    def test_read_file_keeps_small(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "artifact")
            with open(p, "wb") as f:
                f.write(b"abc\x00def")
            digest = hashlib.sha256(b"abc\x00def").hexdigest()
            for keep in (7, 8):
                self.assertEqual(refs.read_file(p, keep), (digest, 7, b"abc\x00def"))
            self.assertEqual(refs.read_file(p, 6), (digest, 7, None))
            self.assertEqual(refs.file_digest(p), digest)

    def test_snapshot_bytes_check(self):
        fixture = test_submit.Snapshots()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ref = fixture.artifact("bytes-check", b"x")
        fixture.request(ref)
        row = list(fixture.conn.execute("SELECT * FROM snapshots").fetchone())
        names = [col[1] for col in fixture.conn.execute("PRAGMA table_info(snapshots)")]
        fixture.conn.execute("DELETE FROM snapshots")
        fixture.conn.commit()
        row[names.index("has_bytes")] = 1
        row[names.index("bytes_len")] = 1048577
        sql = "INSERT INTO snapshots VALUES (" + ",".join(("?" for _ in row)) + ")"
        caught = None
        try:
            fixture.conn.execute(sql, row)
        except Exception as exc:
            caught = exc
        self.assertIsInstance(caught, sqlite3.IntegrityError)
        self.assertIn("CHECK", str(caught))
        row[names.index("bytes_len")] = 1048576
        caught = None
        try:
            fixture.conn.execute(sql, row)
        except Exception as exc:
            caught = exc
        self.assertIsNone(caught)
        self.assertEqual(fixture.conn.execute("SELECT bytes_len, has_bytes FROM snapshots").fetchall(), [
            (1048576, 1)])

if __name__ == "__main__":
    unittest.main()
