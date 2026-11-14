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

    def test_rows_never_replaced(self):
        # Break: REPLACE silently rewrites history behind the reader's cursor.
        from relay_engine import lifecycle_outbox
        insert_header(self.conn)
        insert_header(self.conn, position=2, **settlement())
        self.conn.execute("INSERT INTO lifecycle_outbox_events VALUES ('p', 2, 1, 1, 'kind', 'subject')")
        row_id = self.conn.execute("SELECT row_id FROM lifecycle_outbox WHERE position=1").fetchone()[0]
        header = ("INTO lifecycle_outbox "
                  "(project_id, position, row_id, subkind, committed_at_ms, actor_kind, seat_id, new_occupant_id) "
                  "VALUES ('p', ?, ?, 'seat_registered', 99, 'operator', 's-a', 'o1')")
        first = ('INSERT OR REPLACE ' + header, (1, uuid.uuid4().hex))
        cases = [(*first, 'row'),
                 ('INSERT OR REPLACE ' + header, (3, row_id), 'row'),
                 ("REPLACE INTO lifecycle_outbox_events VALUES ('p', 2, 1, 1, 'another', 'subject')", (), 'event'),
                 ('INSERT OR IGNORE ' + header, (1, uuid.uuid4().hex), 'row'),
                 ('INSERT ' + header + ' ON CONFLICT (project_id, position) DO UPDATE SET committed_at_ms = 99',
                  (1, uuid.uuid4().hex), 'row')]
        before = [self.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1, 2, 3').fetchall() for table in TABLES]
        for sql, parameters, kind in cases:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'a lifecycle outbox ' + kind + ' is never replaced'):
                self.conn.execute(sql, parameters)
            self.assertEqual([self.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1, 2, 3').fetchall()
                              for table in TABLES], before)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(lifecycle_outbox.append_seat_row(
            self.conn, project_id='p', subkind='seat_registered', seat_id='s-a',
            old_occupant_id=None, new_occupant_id='o1', actor_kind='operator',
            actor_occupant_id=None, committed_at_ms=10), 3)
        self.conn.execute('COMMIT')
        insert_header(self.conn, position=4, **settlement())
        self.conn.execute("INSERT INTO lifecycle_outbox_events VALUES ('p', 4, 1, 2, 'new', 'event')")
        self.assertEqual(self.conn.execute("SELECT event_index, number FROM lifecycle_outbox_events WHERE position=4").fetchall(), [(1, 2)])
        triggers = tuple(sql for sql in schema._TRIGGERS
                         if not any('IF NOT EXISTS ' + name + ' ' in sql
                                    for name in ('lifecycle_outbox_new', 'lifecycle_outbox_events_new')))
        with mock.patch.object(schema, '_TRIGGERS', triggers):
            unguarded = ledger_db(self)
        insert_header(unguarded)
        old_id = unguarded.execute('SELECT row_id FROM lifecycle_outbox').fetchone()[0]
        unguarded.execute(*first)
        self.assertNotEqual(unguarded.execute('SELECT row_id FROM lifecycle_outbox').fetchone()[0], old_id)
        self.assertEqual(unguarded.execute('SELECT row_id FROM lifecycle_outbox').fetchone()[0], first[1][1])

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
        invalid += [dict(subkind="seat_registered", seat_id=None),
                    dict(subkind="seat_replaced", old_occupant_id="o1", new_occupant_id="o2", seat_id=None),
                    dict(subkind="seat_stood_down", new_occupant_id=None, seat_id=None)]
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


def _column_domain(sql, column):
    """Read the column's domain, excluding cross-column IN constraints."""
    import re
    pattern = (r'^\s*' + re.escape(column) + r' TEXT NOT NULL CHECK \('
               + re.escape(column) + r' IN \(([^)]*)\)\)')
    found = re.findall(pattern, sql, re.MULTILINE)
    assert len(found) == 1, (column, found)
    return set(re.findall(r"'([^']*)'", found[0]))


class TestOutboxAPI(unittest.TestCase):
    def setUp(self):
        # Import per test: missing module must not suppress Task 1's DDL tests.
        from relay_engine import lifecycle_outbox
        self.api = lifecycle_outbox
        self.conn = ledger_db(self)
        self.other = connect_again(self, self.conn)

    def seat_args(self, **changes):
        values = dict(project_id='p', subkind='seat_registered', seat_id='s-a',
                      old_occupant_id=None, new_occupant_id='o1', actor_kind='operator',
                      actor_occupant_id=None, committed_at_ms=10)
        values.update(changes)
        return values

    def settlement_args(self, **changes):
        values = dict(project_id='p', outcome='finished', occasion='start', chain_id='chain',
                      runtime_instance_id='runtime', transaction_ordinal=0,
                      events=(self.api.SettledEvent(2, 'approved', 'first'),
                              self.api.SettledEvent(5, 'denied', 'second')), committed_at_ms=11)
        values.update(changes)
        return values

    def cases(self):
        return [
            ('append_seat_row', self.seat_args()),
            ('append_settlement_row', self.settlement_args()),
            ('append_restart_row', dict(project_id='p', seat_id='s-a',
                                        actor_occupant_id='o1', committed_at_ms=12)),
            ('append_instructed_act_row', dict(project_id='p', act_kind='resume',
                 carrying_seat_id='s-a', instruction_relay_id='relay', chain_id='chain',
                 event_number=3, committed_at_ms=13)),
            ('append_merge_alarm_row', dict(project_id='p', slip_id='slip', holder_seat_id='s-a',
                 pr=7, merge_commit='a' * 40, allowed_target='v3', landed_target='other',
                 committed_at_ms=14)),
            ('append_unseat_row', dict(project_id='p', seat_id='s-a', generation=2,
                 surface='terminal', carrying_seat_id=None, instruction_relay_id=None,
                 committed_at_ms=15)),
        ]

    def append(self, name='append_seat_row', values=None, conn=None):
        return getattr(self.api, name)(self.conn if conn is None else conn,
                                       **(self.seat_args() if values is None else values))

    def header(self, position):
        cursor = self.conn.execute('SELECT * FROM lifecycle_outbox WHERE project_id=? AND position=?',
                                   ('p', position))
        return dict(zip((d[0] for d in cursor.description), cursor.fetchone()))

    def count(self, conn=None, table='lifecycle_outbox'):
        return (self.conn if conn is None else conn).execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def commit_append(self, name='append_seat_row', values=None):
        self.conn.execute('BEGIN IMMEDIATE')
        position = self.append(name, values)
        self.conn.execute('COMMIT')
        return position

    def seat_row(self, position, **changes):
        values = dict(position=position, row_id=self.header(position)['row_id'],
                      subkind='seat_registered', committed_at_ms=10, actor_kind='operator',
                      actor_occupant_id=None, team_instance_id='t1', seat_id='s-a',
                      seat_address='v3-engine.planner', old_occupant_id=None, new_occupant_id='o1')
        values.update(changes)
        return self.api.SeatRow(**values)

    def assert_refused_header(self, values):
        before = self.count()
        with self.assertRaises(sqlite3.IntegrityError):
            insert_header(self.conn, position=before + 1, **values)
        self.assertEqual(self.count(), before)

    def test_append_rollback_leaves_no_row(self):
        # Break: an append commits the caller's work or allocates an external counter.
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.append(), 1)
        self.conn.execute('ROLLBACK')
        self.assertEqual(self.count(self.other), 0)
        self.assertEqual(self.commit_append(), 1)
        row = self.header(1)
        for key, value in self.seat_args().items():
            self.assertEqual(row[key], value)
        self.assertEqual(self.count(), 1)
        self.assertEqual(row['position'], 1)

    def test_append_outside_transaction_refuses(self):
        # Break: isolation_level=None silently autocommits an unguarded append.
        headers = [{}, settlement(), *self.r6_headers(), dict(
            subkind='seat_unseated', new_occupant_id=None, generation=2, surface='terminal')]
        for (name, values), header in zip(self.cases(), headers):
            with self.subTest(append=name):
                conn = ledger_db(self)
                other = connect_again(self, conn)
                with self.assertRaises(ValueError):
                    self.append(name, values, conn)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(self.count(other), 0)
                self.assertEqual(self.count(other, TABLES[1]), 0)
                # Control: the corresponding bare header immediately autocommits.
                insert_header(conn, **header)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(self.count(other), 1)

    def test_append_never_ends_transaction(self):
        # Break: an append makes the state change visible before its caller commits.
        for name, values in self.cases():
            with self.subTest(append=name):
                before = self.count(self.other)
                old = self.conn.execute("SELECT state FROM seats WHERE id='s-a'").fetchone()[0]
                new = 'vacant' if old == 'occupied' else 'occupied'
                self.conn.execute('BEGIN IMMEDIATE')
                self.conn.execute("UPDATE seats SET state=? WHERE id='s-a'", (new,))
                self.append(name, values)
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.count(self.other), before)
                self.assertEqual(self.other.execute("SELECT state FROM seats WHERE id='s-a'").fetchone()[0], old)
                self.conn.execute('COMMIT')
                self.assertEqual(self.count(self.other), before + 1)
                self.assertEqual(self.other.execute("SELECT state FROM seats WHERE id='s-a'").fetchone()[0], new)

    def test_positions_gap_free_across_rollback(self):
        # Break: a rolled-back settlement consumes a project position.
        self.assertEqual(self.commit_append(), 1)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.append('append_settlement_row', self.settlement_args()), 2)
        self.conn.execute('ROLLBACK')
        self.assertEqual(self.commit_append('append_settlement_row', self.settlement_args()), 2)
        self.assertEqual(self.conn.execute('SELECT position FROM lifecycle_outbox ORDER BY position').fetchall(), [(1,), (2,)])

    def read_only(self):
        filename = self.conn.execute('PRAGMA database_list').fetchone()[2]
        ro = sqlite3.connect(Path(filename).as_uri() + '?mode=ro', uri=True, isolation_level=None)
        self.addCleanup(ro.close)
        return ro

    def pure_read(self, conn, after, limit):
        before = conn.total_changes, conn.in_transaction
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            rows = self.api.read_after(conn, 'p', after, limit=limit)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual((conn.total_changes, conn.in_transaction), before)
        self.assertEqual(len(statements), 1)
        self.assertRegex(statements[0].lstrip().upper(), r'^(WITH|SELECT)\b')
        return rows

    def test_read_after_ordered_and_pure(self):
        # Break: a reader writes state, reads uncommitted data or pages out of order.
        variants = [('seat_registered', None, 'o1'), ('seat_replaced', 'o1', 'o2'),
                    ('seat_stood_down', 'o2', None), ('seat_registered', None, 'o2'),
                    ('seat_stood_down', 'o2', None)]
        self.conn.execute("UPDATE seat_occupants SET ended_at=2 WHERE id='o1'")
        self.conn.execute("INSERT INTO seat_occupants (id,seat_id,key_id,key_verifier,issued_at) VALUES ('o2','s-a','k2','v',2)")
        for subkind, old, new in variants:
            self.commit_append(values=self.seat_args(subkind=subkind, old_occupant_id=old, new_occupant_id=new))
        expected = tuple(self.seat_row(p, subkind=variants[p-1][0], old_occupant_id=variants[p-1][1],
                                       new_occupant_id=variants[p-1][2]) for p in (3, 4))
        for conn in (self.conn, self.read_only()):
            self.assertEqual(self.pure_read(conn, 2, 2), expected)
            self.assertEqual(self.pure_read(conn, 5, 10), ())
        self.conn.execute('BEGIN DEFERRED')
        self.assertEqual(self.pure_read(self.conn, 2, 2), expected)
        self.conn.execute('ROLLBACK')
        self.other.execute('BEGIN IMMEDIATE')
        self.append(conn=self.other)
        self.assertEqual(self.pure_read(self.conn, 5, 10), ())
        self.other.execute('COMMIT')
        self.assertEqual(self.pure_read(self.conn, 5, 10), (self.seat_row(6),))
        for after, limit in ((-1, 1), (0, 0), (True, 1), (0, True)):
            statements = []
            self.conn.set_trace_callback(statements.append)
            try:
                with self.assertRaises(ValueError):
                    self.api.read_after(self.conn, 'p', after, limit=limit)
            finally:
                self.conn.set_trace_callback(None)
            self.assertEqual(statements, [])

    def test_concurrent_appends_never_share_a_position(self):
        # Break: max(position) is allocated outside the caller's write lock/snapshot.
        self.other.execute('PRAGMA busy_timeout=0')
        self.conn.execute('BEGIN IMMEDIATE')
        self.append()
        with self.assertRaises(sqlite3.OperationalError) as busy:
            self.other.execute('BEGIN IMMEDIATE')
        self.assertEqual(busy.exception.sqlite_errorname, 'SQLITE_BUSY')
        self.conn.execute('COMMIT')
        self.other.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.append('append_settlement_row', self.settlement_args(), self.other), 2)
        self.other.execute('COMMIT')
        # A fresh ledger makes (b)'s sole committed row independent of (a).
        first = ledger_db(self)
        second = connect_again(self, first)
        second.execute('PRAGMA busy_timeout=0')
        second.execute('BEGIN DEFERRED')
        second.execute('SELECT * FROM lifecycle_outbox').fetchall()
        first.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.append(conn=first), 1)
        first.execute('COMMIT')
        with self.assertRaises(sqlite3.OperationalError) as stale:
            self.append('append_settlement_row', self.settlement_args(), second)
        self.assertEqual(stale.exception.sqlite_errorname, 'SQLITE_BUSY_SNAPSHOT')
        self.assertTrue(second.in_transaction)
        second.execute('ROLLBACK')
        cursor = first.execute('SELECT * FROM lifecycle_outbox')
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(zip((d[0] for d in cursor.description), rows[0]))
        self.assertEqual(row['position'], 1)
        for key, value in self.seat_args().items():
            self.assertEqual(row[key], value)

    def test_settlement_append_rollback_leaves_no_row(self):
        # Break: header/events escape rollback or events are reordered.
        args = self.settlement_args()
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.append('append_settlement_row', args), 1)
        self.conn.execute('ROLLBACK')
        for table in TABLES:
            self.assertEqual(self.count(self.other, table), 0)
        self.assertEqual(self.commit_append('append_settlement_row', args), 1)
        row = self.header(1)
        self.assertEqual((row['actor_kind'], row['seat_id'], row['actor_occupant_id'],
                          row['old_occupant_id'], row['new_occupant_id']), ('program', None, None, None, None))
        for field in ('chain_id', 'runtime_instance_id', 'transaction_ordinal', 'occasion', 'committed_at_ms'):
            self.assertEqual(row[field], args[field])
        self.assertEqual(self.conn.execute('SELECT event_index,number,kind,subject FROM lifecycle_outbox_events ORDER BY event_index').fetchall(),
                         [(1, 2, 'approved', 'first'), (2, 5, 'denied', 'second')])

    def assert_value_refusal(self, name, values):
        before = self.count(), self.count(self.conn, TABLES[1])
        changes = self.conn.total_changes
        self.conn.execute('BEGIN IMMEDIATE')
        with self.assertRaises(ValueError):
            self.append(name, values)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual((self.count(self.other), self.count(self.other, TABLES[1])), before)
        self.conn.execute('ROLLBACK')

    def test_settlement_append_refusals(self):
        # Break: a bad outcome/occasion/order is written before validation.
        invalid = [dict(events=()), dict(outcome='done'), dict(occasion='resolved')]
        invalid += [dict(occasion=o) for o in ('running', 'stop', 'resolve')]
        invalid += [dict(events=tuple(self.api.SettledEvent(n, 'k', 's') for n in ns))
                    for ns in ((3, 2), (2, 2))]
        for change in invalid:
            with self.subTest(change=change):
                self.assert_value_refusal('append_settlement_row', self.settlement_args(**change))
        self.assert_value_refusal('append_seat_row', self.seat_args(actor_kind='program'))
        self.commit_append()
        for occasion in ('start', 'running', 'stop', 'resolve'):
            self.commit_append('append_settlement_row', self.settlement_args(outcome='undone', occasion=occasion))
        self.commit_append('append_settlement_row', self.settlement_args())
        self.assertEqual(self.count(), 6)

    def test_settlement_event_refusal_rolls_back_whole(self):
        # Break: a failed second event leaves a committed header or first event.
        events = (self.api.SettledEvent(1, 'ok', 'first'), self.api.SettledEvent(2, '', 'bad'))
        self.conn.execute('BEGIN IMMEDIATE')
        with self.assertRaises(sqlite3.IntegrityError):
            self.append('append_settlement_row', self.settlement_args(events=events))
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(self.conn, TABLES[1]), 1)
        self.assertEqual(self.count(self.other), 0)
        self.conn.execute('ROLLBACK')
        for table in TABLES:
            self.assertEqual(self.count(self.other, table), 0)
        self.assertEqual(self.commit_append('append_settlement_row', self.settlement_args()), 1)

    def test_row_id_unique_and_fresh(self):
        # Break: callers choose ids, or uuid generation is reused between calls.
        self.commit_append()
        self.commit_append()
        ids = [r[0] for r in self.conn.execute('SELECT row_id FROM lifecycle_outbox ORDER BY position')]
        self.assertNotEqual(ids[0], ids[1])
        for value in ids:
            self.assertRegex(value, r'^[0-9a-f]{32}$')
        self.assert_refused_header(dict(row_id=ids[0]))
        with mock.patch('uuid.uuid4', return_value=uuid.UUID('b' * 32)):
            self.conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(self.append(), 3)
            with self.assertRaises(sqlite3.IntegrityError):
                self.append()
            self.assertTrue(self.conn.in_transaction)
            self.conn.execute('ROLLBACK')
        self.assertEqual(self.count(), 2)

    def test_read_after_returns_both_shapes(self):
        # Break: LIMIT counts joined events and returns an incomplete settlement.
        from dataclasses import FrozenInstanceError
        self.commit_append()
        events = tuple(self.api.SettledEvent(n, 'kind' + str(n), 'subject' + str(n)) for n in (2, 5, 9))
        self.commit_append('append_settlement_row', self.settlement_args(events=events))
        self.commit_append()
        expected = (self.seat_row(1), self.api.SettlementRow(
            position=2, row_id=self.header(2)['row_id'], subkind='settlement_finished', committed_at_ms=11,
            chain_id='chain', runtime_instance_id='runtime', transaction_ordinal=0, events=events, occasion='start'),
            self.seat_row(3))
        for conn in (self.conn, self.read_only()):
            self.assertEqual(self.pure_read(conn, 0, 3), expected)
            self.assertEqual(self.pure_read(conn, 0, 2), expected[:2])
            self.assertEqual(self.pure_read(conn, 1, 1), expected[1:2])
        for row in expected:
            with self.assertRaises(FrozenInstanceError):
                row.position = 99
        with self.assertRaises(FrozenInstanceError):
            events[0].number = 99
        for occasion in ('start', 'running', 'stop', 'resolve'):
            position = self.commit_append('append_settlement_row', self.settlement_args(outcome='undone', occasion=occasion))
            self.assertEqual(self.pure_read(self.conn, position - 1, 1)[0].occasion, occasion)

    def test_events_written_only_by_append(self):
        # Break: another producer can add an event after a header was published.
        import re
        from relay_engine.tests import test_strings_gate
        expression = re.compile(r'\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+lifecycle_outbox_events\b', re.IGNORECASE)
        sources = {Path(p).name: Path(p).read_text() for p in test_strings_gate.TestGate.production_paths()}
        writers = lambda texts: {name for name, source in texts.items() if expression.search(source)}
        self.assertEqual(writers(sources), {'lifecycle_outbox.py'})
        self.assertNotIn('schema.py', writers(sources))
        for source in ('insert\n INTO\tlifecycle_outbox_events VALUES (...)',
                       'INSERT OR IGNORE INTO lifecycle_outbox_events VALUES (...)',
                       'replace into lifecycle_outbox_events VALUES (...)'):
            with self.subTest(source=source):
                self.assertEqual(writers(dict(sources, extra=source)), {'lifecycle_outbox.py', 'extra'})

    def test_outbox_checks_refuse_alone(self):
        # Break: another refusal masks a missing range, length or foreign-key check.
        instructed, merge = self.r6_headers()[1:]
        cases = [({}, 'committed_at_ms', -1),
                 (settlement(), 'transaction_ordinal', -1),
                 (settlement(), 'chain_id', ''),
                 (settlement(), 'runtime_instance_id', ''),
                 (instructed, 'act_kind', ''), (instructed, 'instruction_relay_id', ''),
                 (instructed, 'event_number', 0),
                 (merge, 'slip_id', ''), (merge, 'allowed_target', ''), (merge, 'landed_target', ''),
                 (dict(subkind='seat_stood_down', old_occupant_id=None, new_occupant_id=None), 'seat_id', 's-x'),
                 (self.r6_headers()[0], 'actor_occupant_id', 'o-x'),
                 (dict(subkind='seat_unseated', new_occupant_id=None, generation=2,
                       surface=None, carrying_seat_id='s-b', instruction_relay_id='relay'), 'carrying_seat_id', 's-x')]
        for base, column, invalid in cases:
            with self.subTest(column=column):
                control = dict(base, row_id=uuid.uuid4().hex)
                before = self.conn.execute('SELECT * FROM lifecycle_outbox').fetchall()
                self.conn.execute('SAVEPOINT inserting_control')
                insert_header(self.conn, position=self.count() + 1, **control)
                self.assertEqual(self.count(), len(before) + 1)
                self.conn.execute('ROLLBACK TO inserting_control')
                self.conn.execute('RELEASE inserting_control')
                self.assert_refused_header(dict(control, **{column: invalid}))
                self.assertEqual(self.conn.execute('SELECT * FROM lifecycle_outbox').fetchall(), before)
        insert_header(self.conn, **settlement())
        sql = 'INSERT INTO lifecycle_outbox_events VALUES (?, ?, ?, ?, ?, ?)'
        for index, column, invalid in ((3, 'number', 0), (4, 'kind', ''), (5, 'subject', '')):
            with self.subTest(column=column):
                control = ('p', 1, 1, 1, 'kind', 'subject')
                before = self.conn.execute('SELECT * FROM lifecycle_outbox_events').fetchall()
                self.conn.execute('SAVEPOINT inserting_control')
                self.conn.execute(sql, control)
                self.assertEqual(self.count(self.conn, TABLES[1]), len(before) + 1)
                self.conn.execute('ROLLBACK TO inserting_control')
                self.conn.execute('RELEASE inserting_control')
                refused = list(control)
                refused[index] = invalid
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql, refused)
                self.assertEqual(self.conn.execute('SELECT * FROM lifecycle_outbox_events').fetchall(), before)

    def r6_headers(self):
        return [
            dict(subkind='engine_restart_requested', actor_kind='occupant', actor_occupant_id='o1', new_occupant_id=None),
            dict(subkind='operator_act_carried', act_kind='resume', instruction_relay_id='relay',
                 chain_id='chain', event_number=3, new_occupant_id=None),
            dict(subkind='merge_target_changed', actor_kind='program', new_occupant_id=None, slip_id='slip',
                 pr=7, merge_commit='a' * 40, allowed_target='v3', landed_target='other'),
        ]

    def test_r6_appends_and_shapes(self):
        # Break: restart/act/merge lose their actor or accept another shape's fields.
        from dataclasses import FrozenInstanceError
        for name, values in self.cases()[2:5]:
            self.commit_append(name, values)
        rows = self.pure_read(self.conn, 0, 3)
        expected = (
            self.api.RestartRow(1, self.header(1)['row_id'], 'engine_restart_requested', 12, 'o1', 's-a', 'v3-engine.planner'),
            self.api.InstructedActRow(2, self.header(2)['row_id'], 'operator_act_carried', 13, 'resume', 's-a',
                                      'v3-engine.planner', 'relay', 'chain', 3),
            self.api.MergeAlarmRow(3, self.header(3)['row_id'], 'merge_target_changed', 14, 'slip', 's-a',
                                   'v3-engine.planner', 7, 'a' * 40, 'v3', 'other'))
        self.assertEqual(rows, expected)
        self.assertEqual([self.header(p)['actor_kind'] for p in (1, 2, 3)], ['occupant', 'operator', 'program'])
        self.assertEqual(self.count(self.conn, TABLES[1]), 0)
        for row in rows:
            with self.assertRaises(FrozenInstanceError):
                row.position = 9
        headers = self.r6_headers()
        own = [('seat_id', 'actor_occupant_id'),
               ('seat_id', 'act_kind', 'instruction_relay_id', 'chain_id', 'event_number'),
               ('seat_id', 'slip_id', 'pr', 'merge_commit', 'allowed_target', 'landed_target')]
        for index, base in enumerate(headers):
            invalid = [dict(base, **{key: None}) for key in own[index]]
            invalid += [dict(base, actor_kind=k) for k in ('occupant', 'operator', 'program') if k != base.get('actor_kind', 'operator')]
            if index == 0:
                invalid.append(dict(base, actor_kind='operator', actor_occupant_id=None))
            elif index == 1:
                invalid.append(dict(base, actor_kind='occupant', actor_occupant_id='o1'))
            other_fields = {'chain_id': 'x', 'runtime_instance_id': 'r', 'transaction_ordinal': 0, 'occasion': 'start',
                            'act_kind': 'x', 'instruction_relay_id': 'r', 'event_number': 1, 'slip_id': 'x',
                            'pr': 1, 'merge_commit': 'a' * 40, 'allowed_target': 'a', 'landed_target': 'b'}
            invalid += [dict(base, **{key: value}) for key, value in other_fields.items() if key not in own[index]]
            for bad in invalid:
                with self.subTest(bad=bad):
                    self.assert_refused_header(bad)
        for change in (dict(landed_target='v3'), dict(merge_commit='A' * 40), dict(merge_commit='a' * 39), dict(pr=0)):
            self.assert_refused_header(dict(headers[2], **change))
        for base in ({}, settlement()):
            for key, value in (('act_kind', 'x'), ('instruction_relay_id', 'r'), ('event_number', 1),
                               ('slip_id', 's'), ('pr', 1), ('merge_commit', 'a' * 40),
                               ('allowed_target', 'a'), ('landed_target', 'b')):
                self.assert_refused_header(dict(base, **{key: value}))
        for base in headers + [dict(headers[2], merge_commit='a' * 64)]:
            insert_header(self.conn, position=self.count() + 1, **base)
        self.assertEqual(self.count(), 7)

    def test_r6_append_refusals(self):
        # Break: invalid r6 arguments reach SQL, or a non-settlement accepts events.
        for name, values in self.cases()[2:5]:
            for key, value in values.items():
                if isinstance(value, str):
                    self.assert_value_refusal(name, dict(values, **{key: ''}))
            if name == 'append_instructed_act_row':
                self.assert_value_refusal(name, dict(values, event_number=0))
            if name == 'append_merge_alarm_row':
                for change in (dict(pr=0), dict(merge_commit='A' * 40), dict(merge_commit='a' * 39),
                               dict(merge_commit='a' * 65), dict(merge_commit='z' * 64), dict(landed_target='v3')):
                    self.assert_value_refusal(name, dict(values, **change))
            position = self.commit_append(name, values)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'only a settlement row has events'):
                self.conn.execute('INSERT INTO lifecycle_outbox_events VALUES (?, ?, 1, 1, ?, ?)', ('p', position, 'k', 's'))
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count(self.conn, TABLES[1]), 0)

    def expected_address(self, seat_id):
        from relay_engine import addresses
        team, role = self.conn.execute('SELECT t.name,s.role FROM seats s JOIN team_instances t ON t.id=s.team_instance_id WHERE s.id=?', (seat_id,)).fetchone()
        return addresses.render(addresses.Address(team, role))

    def test_seat_address_read(self):
        # Break: seat ids or actor identities are rendered as the wrong agent.
        self.commit_append()
        for name, values in self.cases()[2:5]:
            self.commit_append(name, values)
        self.commit_append(values=self.seat_args(subkind='seat_stood_down', seat_id='s-b', new_occupant_id=None))
        rows = self.pure_read(self.conn, 0, 10)
        self.assertEqual([r.seat_address for r in rows], [self.expected_address(r.seat_id) for r in rows])
        self.assertNotEqual(rows[0].seat_address, rows[-1].seat_address)

    def test_new_appends_not_idempotent(self):
        # Break: producer-level deduplication hides snapshot/restore replay rows.
        for name, values in self.cases()[3:5]:
            positions = [self.commit_append(name, values) for _ in range(2)]
            first, second = (self.header(p) for p in positions)
            self.assertNotEqual(first['row_id'], second['row_id'])
            keys = ('slip_id',) if name == 'append_merge_alarm_row' else ('chain_id', 'event_number')
            for key in keys:
                self.assertEqual(first[key], second[key])
        self.assertEqual(self.count(), 4)

    def unseat_args(self, **changes):
        args = self.cases()[-1][1]
        args.update(changes)
        return args

    def test_unseat_append_and_shape(self):
        # Break: surface and instructed unseat become ambiguous or lose their generation.
        from dataclasses import FrozenInstanceError
        forms = [dict(surface='terminal'), dict(surface='page'),
                 dict(surface=None, carrying_seat_id='s-b', instruction_relay_id='relay')]
        for change in forms:
            self.commit_append('append_unseat_row', self.unseat_args(**change))
        rows = self.pure_read(self.conn, 0, 3)
        for position, (row, change) in enumerate(zip(rows, forms), 1):
            args = self.unseat_args(**change)
            self.assertEqual(row, self.api.UnseatRow(position, self.header(position)['row_id'],
                'seat_unseated', 15, 's-a', 'v3-engine.planner', 2, args['surface'], args['carrying_seat_id'],
                'v3-engine.implementer' if args['carrying_seat_id'] else None, args['instruction_relay_id']))
            self.assertEqual(self.header(position)['actor_kind'], 'operator')
            with self.assertRaises(FrozenInstanceError):
                row.position = 9
        self.assertEqual(self.count(self.conn, TABLES[1]), 0)
        base = dict(subkind='seat_unseated', new_occupant_id=None, generation=2, surface='terminal')
        invalid = [dict(carrying_seat_id='s-b', instruction_relay_id='relay'), dict(surface=None),
                   dict(surface=None, carrying_seat_id='s-b'), dict(generation=None), dict(generation=0),
                   dict(actor_kind='program'), dict(actor_kind='occupant', actor_occupant_id='o1'),
                   dict(surface='frank_panel')]
        for change in invalid:
            self.assert_refused_header(dict(base, **change))
        for header in ({}, self.r6_headers()[0], self.r6_headers()[2]):
            for change in (dict(generation=1), dict(surface='page'), dict(carrying_seat_id='s-b')):
                self.assert_refused_header(dict(header, **change))
        insert_header(self.conn, position=4, **self.r6_headers()[1])
        self.assertEqual(self.count(), 4)

    def test_unseat_append_refusals(self):
        # Break: unseat accepts bool generations or an incomplete instruction/surface choice.
        invalid = [dict(generation=0), dict(generation=True), dict(generation='1'),
                   dict(generation=1.5), dict(generation=None), dict(surface='frank_panel'),
                   dict(surface=None), dict(carrying_seat_id='s-b', instruction_relay_id='relay'),
                   dict(surface=None, carrying_seat_id='s-b'), dict(surface=None, instruction_relay_id='relay')]
        invalid += [{key: ''} for key in ('project_id', 'seat_id', 'surface')]
        invalid += [dict(surface=None, carrying_seat_id='', instruction_relay_id='r'),
                    dict(surface=None, carrying_seat_id='s-b', instruction_relay_id='')]
        invalid += [dict(surface='terminal', carrying_seat_id='s-b', instruction_relay_id=None),
                    dict(surface='terminal', carrying_seat_id=None, instruction_relay_id='relay')]
        for change in invalid:
            self.assert_value_refusal('append_unseat_row', self.unseat_args(**change))
        for change in (dict(surface='terminal'), dict(surface='page'),
                       dict(surface=None, carrying_seat_id='s-b', instruction_relay_id='relay')):
            position = self.commit_append('append_unseat_row', self.unseat_args(**change))
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'only a settlement row has events'):
                self.conn.execute('INSERT INTO lifecycle_outbox_events VALUES (?, ?, 1, 1, ?, ?)', ('p', position, 'k', 's'))
        self.assertEqual(self.count(), 3)

    def test_unseat_addresses_read(self):
        # Break: an instructed unseat reports its target as the carrying actor.
        self.commit_append('append_unseat_row', self.unseat_args(surface=None, carrying_seat_id='s-b', instruction_relay_id='relay'))
        self.commit_append('append_unseat_row', self.unseat_args(surface='page'))
        instructed, surface = self.pure_read(self.conn, 0, 2)
        self.assertEqual(instructed.seat_address, self.expected_address('s-a'))
        self.assertEqual(instructed.carrying_seat_address, self.expected_address('s-b'))
        self.assertNotEqual(instructed.seat_address, instructed.carrying_seat_address)
        self.assertIsNone(surface.carrying_seat_address)

    def test_module_issues_no_transaction_control(self):
        # Break: an alternate transaction-control spelling or import crosses the seam.
        import ast
        import re
        source = Path(self.api.__file__).read_text()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {'commit', 'rollback', 'executescript'})
            if isinstance(node, ast.With):
                self.fail('module has a with statement')
        self.assertLessEqual(imports, {'sqlite3', 'uuid', 'dataclasses', 'typing'})
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr):
                    value = node.body[0].value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        docstrings.add(id(value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                self.assertIsNone(re.search(r'\b(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|executescript)\b', node.value, re.IGNORECASE))

    def test_subkinds_match_the_table(self):
        # Break: constants diverge from stored SQL, or another IN list is mistaken for the domain.
        from relay_engine.tests.test_schema import check_values
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='lifecycle_outbox'").fetchone()[0]
        subkinds = set(self.api.SEAT_SUBKINDS) | set(self.api.SETTLEMENT_SUBKINDS) | {
            self.api.RESTART_SUBKIND, self.api.INSTRUCTED_ACT_SUBKIND,
            self.api.MERGE_ALARM_SUBKIND, self.api.UNSEAT_SUBKIND}
        self.assertEqual(subkinds, _column_domain(sql, 'subkind'))
        for column, values in (('actor_kind', self.api.ACTOR_KINDS), ('occasion', self.api.OCCASIONS),
                               ('surface', self.api.UNSEAT_SURFACES)):
            self.assertEqual(set(values), check_values(self.conn, 'lifecycle_outbox.' + column))
        self.assertEqual(set(self.api.SETTLEMENT_OUTCOMES.values()), set(self.api.SETTLEMENT_SUBKINDS))
        fixture = """CREATE TABLE fixture (
            subkind TEXT NOT NULL CHECK (subkind IN ('a', 'b')),
            x INTEGER,
            CHECK ((subkind IN ('a')) = (x = 1))
        )"""
        self.assertEqual(_column_domain(fixture, 'subkind'), {'a', 'b'})
