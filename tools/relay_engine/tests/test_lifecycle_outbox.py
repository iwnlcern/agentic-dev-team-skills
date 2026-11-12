"""Committed lifecycle records: shape, ownership, immutability and reader contracts."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

from relay_engine import errors, schema
from relay_engine.tests.test_schema import platform_rows
from relay_engine.tests.test_submit import Store as SubmitStore


TABLES = ("lifecycle_outbox", "lifecycle_outbox_events")


def ledger_db(testcase):
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    conn = sqlite3.connect(Path(directory.name) / "ledger.sqlite3", isolation_level=None)
    testcase.addCleanup(conn.close)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    schema.init_v3(conn)
    platform_rows(conn)
    return conn


def connect_again(testcase, conn):
    filename = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(filename, isolation_level=None)
    testcase.addCleanup(other.close)
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("PRAGMA foreign_keys=ON")
    return other


def insert_header(conn, **columns):
    values = dict(project_id="p", position=1, row_id=uuid.uuid4().hex,
                  subkind="seat_registered", committed_at_ms=10,
                  actor_kind="operator", seat_id="s-a", new_occupant_id="o1")
    values.update(columns)
    names = ", ".join(values)
    conn.execute("INSERT INTO lifecycle_outbox (" + names + ") VALUES ("
                 + ", ".join("?" for _ in values) + ")", tuple(values.values()))


def settlement(**columns):
    values = dict(subkind="settlement_finished", actor_kind="program", seat_id=None,
                  new_occupant_id=None, chain_id="chain", runtime_instance_id="runtime",
                  transaction_ordinal=0, occasion="start")
    values.update(columns)
    return values


class TestOutboxDDL(unittest.TestCase):
    def setUp(self):
        self.conn = ledger_db(self)
        self.conn.execute("UPDATE seat_occupants SET ended_at=2 WHERE id='o1'")
        self.conn.execute("INSERT INTO seat_occupants "
                          "(id, seat_id, key_id, key_verifier, issued_at, ended_at) VALUES "
                          "('o2', 's-a', 'k2', 'v', 2, NULL), ('other', 's-b', 'k3', 'v', 1, 2)")

    def rejected_header(self, values):
        before = self.conn.execute("SELECT * FROM lifecycle_outbox").fetchall()
        with self.assertRaises(sqlite3.IntegrityError):
            insert_header(self.conn, **values)
        self.assertEqual(self.conn.execute("SELECT * FROM lifecycle_outbox").fetchall(), before)

    def test_lifecycle_outbox_not_hook_writable(self):
        # Break: declaring Console tables as hook-owned admits unreviewed writes.
        fixture = SubmitStore()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for table in TABLES:
            with self.subTest(table=table):
                self.assertEqual(schema.TABLE_OWNERS.get(table), "console")
                with self.assertRaises(ValueError):
                    replace(fixture.ctx(), after_admit_writes=frozenset({table}))
                with mock.patch.dict(schema.TABLE_OWNERS, {table: "authority"}):
                    accepted = replace(fixture.ctx(), after_admit_writes=frozenset({table}))
                    self.assertEqual(accepted.after_admit_writes, frozenset({table}))

    def test_lifecycle_outbox_ddl_refuses_older_store(self):
        # Break: accepting the old digest silently leaves the required tables absent.
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        tables = tuple(sql for sql in schema._TABLES
                       if not any("CREATE TABLE IF NOT EXISTS " + t + " " in sql for t in TABLES))
        triggers = tuple(sql for sql in schema._TRIGGERS if "lifecycle_outbox" not in sql)
        with mock.patch.object(schema, "_TABLES", tables), mock.patch.object(schema, "_TRIGGERS", triggers):
            schema.init_v3(old)
        before = old.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        digest = old.execute("SELECT ddl_sha256 FROM schema_version").fetchone()
        with self.assertRaises(errors.EngineError) as caught:
            schema.init_v3(old)
        self.assertEqual(caught.exception.code, "E-SCHEMA-MISMATCH")
        self.assertEqual(old.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall(), before)
        self.assertEqual(old.execute("SELECT ddl_sha256 FROM schema_version").fetchone(), digest)
        fresh_before = self.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        schema.init_v3(self.conn)
        self.assertEqual(self.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall(), fresh_before)

    def test_rows_immutable(self):
        # Break: an unguarded column update or delete changes an already committed act.
        insert_header(self.conn)
        insert_header(self.conn, position=2, **settlement())
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(lifecycle_outbox)")]
        for position in (1, 2):
            before = self.conn.execute("SELECT * FROM lifecycle_outbox WHERE position=?", (position,)).fetchone()
            for column in columns:
                with self.subTest(position=position, column=column):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "a lifecycle outbox row never changes"):
                        self.conn.execute("UPDATE lifecycle_outbox SET " + column + "=" + column
                                          + " WHERE position=?", (position,))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "a lifecycle outbox row is never deleted"):
                self.conn.execute("DELETE FROM lifecycle_outbox WHERE position=?", (position,))
            self.assertEqual(self.conn.execute("SELECT * FROM lifecycle_outbox WHERE position=?", (position,)).fetchone(), before)
        insert_header(self.conn, position=3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lifecycle_outbox").fetchone()[0], 3)

    def test_row_shape_checks(self):
        # Break: weakening any seat shape or composite occupant FK admits a false history.
        invalid = [dict(old_occupant_id="o1"), dict(new_occupant_id=None),
                   dict(subkind="seat_replaced", old_occupant_id="o1"),
                   dict(subkind="seat_replaced", old_occupant_id=None),
                   dict(subkind="seat_replaced", old_occupant_id="o1", new_occupant_id=None),
                   dict(subkind="seat_stood_down"), dict(actor_kind="program"),
                   dict(actor_kind="occupant", actor_occupant_id=None),
                   dict(actor_occupant_id="o1"), dict(new_occupant_id="other"),
                   dict(subkind="seat_replaced", old_occupant_id="other"),
                   dict(subkind="not_a_subkind"), dict(position=0)]
        invalid += [dict(subkind=s, seat_id=None) for s in
                    ("seat_registered", "seat_replaced", "seat_stood_down")]
        for values in invalid:
            with self.subTest(values=values):
                self.rejected_header(values)
        insert_header(self.conn)
        self.rejected_header({"position": 1})
        insert_header(self.conn, position=2, subkind="seat_replaced", old_occupant_id="o1", new_occupant_id="o2")
        insert_header(self.conn, position=3, subkind="seat_stood_down", seat_id="s-b", new_occupant_id=None)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lifecycle_outbox").fetchone()[0], 3)

    def test_settlement_row_shape(self):
        # Break: incomplete settlement metadata, false occasion or malformed identity persists.
        invalid = [settlement(seat_id="s-a"), settlement(old_occupant_id="o1"),
                   settlement(new_occupant_id="o1"), settlement(actor_kind="operator"),
                   settlement(actor_kind="occupant", actor_occupant_id="o1")]
        invalid += [settlement(**{key: None}) for key in
                    ("chain_id", "runtime_instance_id", "transaction_ordinal", "occasion")]
        invalid += [settlement(occasion=o) for o in ("resolved", "unknown", "running", "stop", "resolve")]
        invalid += [settlement(row_id=x) for x in ("a" * 31, "a" * 33, "A" * 32, "z" * 32)]
        invalid += [{key: value} for key, value in
                    (("chain_id", "c"), ("runtime_instance_id", "r"),
                     ("transaction_ordinal", 0), ("occasion", "start"))]
        for values in invalid:
            with self.subTest(values=values):
                self.rejected_header(values)
        insert_header(self.conn, row_id="a" * 32, **settlement())
        self.rejected_header(settlement(position=2, row_id="a" * 32))
        for position, occasion in enumerate(("start", "running", "stop", "resolve"), 2):
            insert_header(self.conn, position=position,
                          **settlement(subkind="settlement_undone", occasion=occasion))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lifecycle_outbox").fetchone()[0], 5)

    def test_events_only_for_settlement_rows(self):
        # Break: allowing an event on a seat record invents a settlement.
        insert_header(self.conn)
        insert_header(self.conn, position=2, **settlement())
        sql = "INSERT INTO lifecycle_outbox_events VALUES ('p', ?, 1, 1, 'kind', 'subject')"
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only a settlement row has events"):
            self.conn.execute(sql, (1,))
        self.conn.execute(sql, (2,))
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.conn.execute(sql, (3,))
        self.assertIn("FOREIGN KEY", str(caught.exception))
        self.assertEqual(self.conn.execute("SELECT position FROM lifecycle_outbox_events").fetchall(), [(2,)])

    def test_events_triggers_fixed_and_kept(self):
        # Break: modifying one event invalidates the committed settlement's meaning.
        insert_header(self.conn, **settlement())
        self.conn.execute("INSERT INTO lifecycle_outbox_events VALUES ('p', 1, 1, 1, 'kind', 'subject')")
        before = self.conn.execute("SELECT * FROM lifecycle_outbox_events").fetchall()
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(lifecycle_outbox_events)")]
        for column in columns:
            with self.subTest(column=column):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "a lifecycle outbox event never changes"):
                    self.conn.execute("UPDATE lifecycle_outbox_events SET " + column + "=" + column)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "a lifecycle outbox event is never deleted"):
            self.conn.execute("DELETE FROM lifecycle_outbox_events")
        self.assertEqual(self.conn.execute("SELECT * FROM lifecycle_outbox_events").fetchall(), before)
        insert_header(self.conn, position=2, **settlement())
        self.conn.execute("INSERT INTO lifecycle_outbox_events VALUES ('p', 2, 1, 1, 'kind', 'subject')")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lifecycle_outbox_events").fetchone()[0], 2)
