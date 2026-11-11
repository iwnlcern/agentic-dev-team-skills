"""Catalogue lifecycle invariants and caller-owned transaction helpers."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from relay_engine import store_layout


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


class TestLifecycleTables(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store_layout.open_catalogue(Path(tmp.name))
        self.addCleanup(self.conn.close)
        self.conn.execute('INSERT INTO project_routes VALUES (?, ?, ?, ?)',
                          (P, 'example', 'active', 1)).close()
        self.conn.commit()
        self.assertEqual(self.rows('PRAGMA foreign_keys'), [(1,)])
        self.conn.execute('BEGIN IMMEDIATE').close()

    def rows(self, sql, parameters=()):
        cursor = self.conn.execute(sql, parameters)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def execute(self, sql, parameters=()):
        self.conn.execute(sql, parameters).close()

    def test_store_wide_start_ends_the_store_stop(self):
        # Catches equality against NULL when finding the open store stop.
        self.assertEqual(store_layout.record_stop(self.conn, 'store', None, 'one'), 1)
        self.assertEqual(store_layout.record_start(self.conn, 'store', None, 'two'), 2)
        self.assertEqual(store_layout.open_stops(self.conn), [])
        self.assertEqual([(e.kind, e.scope, e.project_id)
                          for e in store_layout.stop_events_after(self.conn, 0)],
                         [('stop', 'store', None), ('start', 'store', None)])
        self.assertIsNone(store_layout.record_start(self.conn, 'store', None, 'three'))

    def test_one_open_store_stop(self):
        # Catches a unique index that permits duplicate NULL project ids.
        store_layout.record_stop(self.conn, 'store', None, 'one')
        with self.assertRaises(sqlite3.IntegrityError):
            store_layout.record_stop(self.conn, 'store', None, 'two')
        self.assertEqual([(s.scope, s.project_id, s.stopped_by)
                          for s in store_layout.open_stops(self.conn)],
                         [('store', None, 'one')])

    def test_end_cannot_change_other_columns(self):
        # Catches an end guard that also permits changing immutable columns.
        store_layout.record_stop(self.conn, 'project', P, 'one')
        for change in ["scope='store', project_id=NULL", "stopped_at='other'",
                       "stopped_by='other'"]:
            with self.subTest(change=change):
                self.execute('SAVEPOINT end_guard')
                try:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'engine_stops'):
                        self.execute("UPDATE engine_stops SET ended_at='x', ended_by='y', " + change)
                    self.assertEqual([(s.row_id, s.scope, s.project_id, s.ended_at)
                                      for s in store_layout.open_stops(self.conn)],
                                     [(1, 'project', P, None)])
                finally:
                    self.execute('ROLLBACK TO end_guard')
                    self.execute('RELEASE end_guard')

    def test_event_ids_never_reused(self):
        self.assertEqual(store_layout.record_stop(self.conn, 'project', P, 'one'), 1)
        self.assertEqual(store_layout.record_start(self.conn, 'project', P, 'two'), 2)
        self.assertEqual(store_layout.record_stop(self.conn, 'project', P, 'three'), 3)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'engine_stop_events'):
            self.execute('DELETE FROM engine_stop_events WHERE event_id = 3')
        triggers = self.rows("SELECT name FROM sqlite_master WHERE type='trigger' "
                             "AND tbl_name='engine_stop_events' AND sql LIKE '%BEFORE DELETE%'")
        self.assertEqual(len(triggers), 1)
        self.execute('DROP TRIGGER "' + triggers[0][0].replace('"', '""') + '"')
        self.execute('DELETE FROM engine_stop_events WHERE event_id = 3')
        self.assertEqual(store_layout.record_stop(self.conn, 'store', None, 'four'), 4)
        # A plain INTEGER PRIMARY KEY reuses the deleted maximum.
        self.execute('CREATE TABLE control_ids (id INTEGER PRIMARY KEY) STRICT')
        for _ in range(3):
            self.execute('INSERT INTO control_ids DEFAULT VALUES')
        self.execute('DELETE FROM control_ids WHERE id = 3')
        self.execute('INSERT INTO control_ids DEFAULT VALUES')
        self.assertEqual(self.rows('SELECT max(id) FROM control_ids'), [(3,)])
        self.assertTrue(self.conn.in_transaction)

    def test_one_open_stop_per_scope(self):
        store_layout.record_stop(self.conn, 'project', P, 'one')
        with self.assertRaises(sqlite3.IntegrityError):
            store_layout.record_stop(self.conn, 'project', P, 'duplicate')
        store_layout.record_stop(self.conn, 'store', None, 'store')
        self.assertEqual([(row.scope, row.project_id) for row in store_layout.open_stops(self.conn)],
                         [('project', P), ('store', None)])
        self.assertEqual(store_layout.record_start(self.conn, 'project', P, 'start'), 3)
        self.assertEqual(store_layout.record_stop(self.conn, 'project', P, 'again'), 4)
        self.assertEqual([row.row_id for row in store_layout.open_stops(self.conn)], [2, 3])

    def test_stop_rows_refuse_update_and_delete(self):
        store_layout.record_stop(self.conn, 'project', P, 'one')
        for sql, table in [
                ("UPDATE engine_stops SET stopped_by='other'", 'engine_stops'),
                ('DELETE FROM engine_stops', 'engine_stops'),
                ('DELETE FROM engine_stop_events', 'engine_stop_events'),
                ("UPDATE engine_stop_events SET kind='start'", 'engine_stop_events')]:
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, table):
                self.execute(sql)
        self.assertEqual(store_layout.record_start(self.conn, 'project', P, 'start'), 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'engine_stops'):
            self.execute("UPDATE engine_stops SET ended_at='again', ended_by='other'")
        self.assertEqual(store_layout.open_stops(self.conn), [])
        self.assertTrue(self.conn.in_transaction)

    def test_record_start_without_open_row(self):
        before = self.conn.total_changes
        self.assertIsNone(store_layout.record_start(self.conn, 'project', P, 'start'))
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.rows('SELECT * FROM engine_stops'), [])
        self.assertEqual(self.rows('SELECT * FROM engine_stop_events'), [])

    def test_stop_events_carry_scope(self):
        store_layout.record_stop(self.conn, 'project', P, 'one')
        store_layout.record_stop(self.conn, 'store', None, 'two')
        store_layout.record_start(self.conn, 'project', P, 'three')
        events = store_layout.stop_events_after(self.conn, 0)
        self.assertEqual([(e.event_id, e.kind, e.scope, e.project_id) for e in events],
                         [(1, 'stop', 'project', P), (2, 'stop', 'store', None),
                          (3, 'start', 'project', P)])
        self.assertEqual([e.by for e in events], ['one', 'two', 'three'])
        self.assertTrue(all(isinstance(e.at, str) for e in events))
        self.assertEqual([e.event_id for e in store_layout.stop_events_after(self.conn, 1)], [2, 3])
        # Reading the events table alone cannot supply the ended row's scope.
        cursor = self.conn.execute('SELECT * FROM engine_stop_events WHERE event_id=3')
        try:
            self.assertNotIn('scope', [column[0] for column in cursor.description])
        finally:
            cursor.close()

    def test_checkout_routes_one_open_per_inode(self):
        sql = 'INSERT INTO checkout_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
        def insert(name, ended=None, reason=None, dev=1):
            self.execute(sql, (name, P, 'repository', '/git', '/work', dev, 2, 10,
                               ended, reason, None))
        insert('first')
        with self.assertRaises(sqlite3.IntegrityError):
            insert('duplicate')
        self.execute("UPDATE checkout_routes SET ended_at=11, end_reason='matched' WHERE checkout_id='first'")
        insert('next')
        for ended, reason in [(None, 'matched'), (12, 'other')]:
            with self.subTest(ended=ended, reason=reason), self.assertRaises(sqlite3.IntegrityError):
                insert('invalid', ended, reason, dev=3)
        self.assertEqual(self.rows('SELECT checkout_id FROM checkout_routes ORDER BY created_at, checkout_id'),
                         [('first',), ('next',)])

    def test_record_stop_refuses_mismatched_scope(self):
        for scope, project in [('project', None), ('store', P), ('project', 'unknown')]:
            with self.subTest(scope=scope, project=project):
                with self.assertRaises(sqlite3.IntegrityError):
                    store_layout.record_stop(self.conn, scope, project, 'operator')
                self.assertEqual(self.rows('SELECT * FROM engine_stops'), [])
                self.assertEqual(self.rows('SELECT * FROM engine_stop_events'), [])

    def test_stop_events_after_the_last_is_empty(self):
        store_layout.record_stop(self.conn, 'project', P, 'stop')
        store_layout.record_start(self.conn, 'project', P, 'start')
        self.assertEqual(store_layout.stop_events_after(self.conn, 2), [])
        self.assertEqual(store_layout.stop_events_after(self.conn, 99), [])


class TestCheckoutRoutes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = store_layout.open_catalogue(self.root)
        self.addCleanup(self.conn.close)
        self.conn.execute('INSERT INTO project_routes VALUES (?, ?, ?, ?)',
                          (P, 'example', 'active', 1)).close()
        self.conn.commit()

    def insert(self, checkout_id, created, ended=None, reason=None, *, dev=1):
        values = (checkout_id, P, 'repository', '/git', '/work', dev, 2,
                  created, ended, reason, None)
        self.conn.execute('INSERT INTO checkout_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                          values).close()
        return values

    def test_checkout_routes_read(self):
        # Catches filtering away ended rows, wrong order/inode and lost columns.
        from dataclasses import astuple
        self.conn.execute('BEGIN IMMEDIATE').close()
        newest = self.insert('open', 30)
        oldest = self.insert('oldest', 10, 15, 'matched')
        middle = self.insert('middle', 20, 25, 'unmatched')
        self.insert('elsewhere', 40, dev=9)
        self.conn.commit()
        reader = store_layout.open_catalogue_readonly(self.root)
        self.addCleanup(reader.close)
        rows = store_layout.checkout_routes(reader, dev=1, ino=2)
        self.assertEqual([astuple(row) for row in rows], [oldest, middle, newest])
        self.assertFalse(reader.in_transaction)

    def test_checkout_routes_read_keeps_caller_transaction(self):
        # Catches a read that commits or rolls back its caller's pending row.
        self.conn.execute('BEGIN IMMEDIATE').close()
        self.insert('pending', 10)
        rows = store_layout.checkout_routes(self.conn, dev=1, ino=2)
        self.assertEqual([r.checkout_id for r in rows], ['pending'])
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT checkout_id FROM checkout_routes').fetchall(), [])
        # The caller also keeps ownership when the SELECT raises.
        self.conn.execute('BEGIN IMMEDIATE').close()
        self.insert('pending-error', 20)
        self.conn.execute('DROP TABLE checkout_routes').close()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.checkout_routes(self.conn, dev=1, ino=2)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT checkout_id FROM checkout_routes').fetchall(), [])

    def test_checkout_routes_read_cleans_up_its_own(self):
        # Catches leaking the owned read transaction on success or failure.
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(store_layout.checkout_routes(self.conn, dev=1, ino=2), [])
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute('DROP TABLE checkout_routes').close()
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.checkout_routes(self.conn, dev=1, ino=2)
        self.assertFalse(self.conn.in_transaction)

    def test_checkout_route_by_id(self):
        # D1: filtering ended routes; D2: additionally filtering by one inode.
        self.conn.execute('BEGIN IMMEDIATE').close()
        a = self.insert('A', 10, 15, 'matched')
        b = self.insert('B', 20)
        c = self.insert('C', 30, dev=9)
        self.conn.commit()
        reader = store_layout.open_catalogue_readonly(self.root)
        self.addCleanup(reader.close)
        self.assertEqual(store_layout.checkout_route(reader, 'A'), store_layout.CheckoutRoute(*a))
        self.assertEqual(store_layout.checkout_route(reader, 'B'), store_layout.CheckoutRoute(*b))
        self.assertIsNone(store_layout.checkout_route(reader, 'B').ended_at)
        self.assertEqual(store_layout.checkout_route(reader, 'C'), store_layout.CheckoutRoute(*c))
        self.assertIsNone(store_layout.checkout_route(reader, 'none'))
        self.assertFalse(reader.in_transaction)

    def test_checkout_route_keeps_caller_transaction(self):
        # D3: a read must not commit its caller's pending row.
        self.conn.execute('BEGIN IMMEDIATE').close()
        pending = self.insert('pending', 10)
        self.assertEqual(store_layout.checkout_route(self.conn, 'pending'),
                         store_layout.CheckoutRoute(*pending))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(store_layout.checkout_route(self.conn, 'pending'))
        self.conn.execute('BEGIN IMMEDIATE').close()
        self.insert('pending-error', 20)
        self.conn.execute('DROP TABLE checkout_routes').close()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.checkout_route(self.conn, 'pending-error')
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(store_layout.checkout_route(self.conn, 'pending-error'))

    def test_checkout_route_cleans_up_its_own(self):
        # D4: omitting ROLLBACK must fail after an owned SELECT raises.
        self.assertIsNone(store_layout.checkout_route(self.conn, 'none'))
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute('DROP TABLE checkout_routes').close()
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.checkout_route(self.conn, 'none')
        self.assertFalse(self.conn.in_transaction)

    def test_restore_revives_ended_route_inert(self):
        # Catalogue-half witness only: restoring the file revives its old route.
        import os
        self.conn.execute('BEGIN IMMEDIATE').close()
        self.insert('revived', 10)
        self.conn.commit()
        backup_path = self.root / 'backup.sqlite3'
        backup = sqlite3.connect(backup_path)
        try:
            self.conn.backup(backup)
        finally:
            backup.close()
        self.conn.execute("UPDATE checkout_routes SET ended_at=20, end_reason='matched'").close()
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT ended_at FROM checkout_routes').fetchall(), [(20,)])
        self.conn.close()
        path = store_layout.catalogue_path(self.root)
        self.assertFalse(Path(str(path) + '-wal').exists())
        self.assertFalse(Path(str(backup_path) + '-wal').exists())
        os.replace(backup_path, path)
        writer = store_layout.open_catalogue(self.root)
        self.addCleanup(writer.close)
        reader = store_layout.open_catalogue_readonly(self.root)
        self.addCleanup(reader.close)
        rows = store_layout.checkout_routes(reader, dev=1, ino=2)
        self.assertEqual([(r.checkout_id, r.ended_at, r.end_reason) for r in rows],
                         [('revived', None, None)])
