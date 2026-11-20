"""Catalogue lifecycle invariants and caller-owned transaction helpers."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import logging
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import restore_cancel, store_layout, submit


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
_INCUMBENT_LOG = logging.getLogger(__name__)
_INCUMBENT_STORE_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


class TestLifecycleTables(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store_layout.open_catalogue(Path(tmp.name))
        self.addCleanup(self.conn.close)
        self.conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, ?, ?, ?)',
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
        self.conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, ?, ?, ?)',
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

    def test_open_checkout_routes_lists_open_in_order(self):
        # O1 includes ended routes; O2 loses the final tie-break; O3 uses time.
        from dataclasses import astuple
        q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        self.conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, ?, ?, ?)',
                          (q, 'other', 'active', 1)).close()
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE').close()
        values = {}
        for n, (cid, project, path, created, ended, reason) in enumerate((
                ('e', P, '/0', 5, 6, 'matched'),
                ('a', q, '/b', 10, None, None),
                ('b', P, '/z', 20, None, None),
                ('c', P, '/a', 30, None, None),
                ('y', P, '/m', 40, None, None),
                ('x', P, '/m', 50, None, None))):
            row = (cid, project, 'repository-' + cid, '/git/' + cid, path,
                   100 + n, 200 + n, created, ended, reason, None)
            self.conn.execute('INSERT INTO checkout_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                              row).close()
            values[cid] = row
        self.conn.commit()
        reader = store_layout.open_catalogue_readonly(self.root)
        self.addCleanup(reader.close)
        self.assertEqual([astuple(row) for row in store_layout.open_checkout_routes(reader)],
                         [values[cid] for cid in ('c', 'x', 'y', 'b', 'a')])
        self.assertFalse(reader.in_transaction)

    def test_open_checkout_routes_empty(self):
        # An empty catalogue and one with only ended routes both stay empty.
        self.assertEqual(store_layout.open_checkout_routes(self.conn), [])
        self.insert('gone', 10, 15, 'matched')
        self.conn.commit()
        self.assertEqual(store_layout.open_checkout_routes(self.conn), [])

    def test_open_checkout_routes_keeps_caller_transaction(self):
        # O4 commits a caller's pending route and clears its transaction.
        self.conn.execute('BEGIN IMMEDIATE').close()
        pending = self.insert('pending', 10)
        self.assertEqual(store_layout.open_checkout_routes(self.conn),
                         [store_layout.CheckoutRoute(*pending)])
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(store_layout.open_checkout_routes(self.conn), [])
        self.conn.execute('BEGIN IMMEDIATE').close()
        self.insert('pending-error', 20)
        self.conn.execute('DROP TABLE checkout_routes').close()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.open_checkout_routes(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(store_layout.open_checkout_routes(self.conn), [])

    def test_open_checkout_routes_cleans_up_its_own(self):
        # O5 omits the rollback after an owned SELECT raises.
        self.assertEqual(store_layout.open_checkout_routes(self.conn), [])
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute('DROP TABLE checkout_routes').close()
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.open_checkout_routes(self.conn)
        self.assertFalse(self.conn.in_transaction)

    def test_owed_ended_routes_lists_owed_in_order(self):
        # R1/R2/R3 lose filters; R4/R5 lose the full order or its tie-break.
        from dataclasses import astuple
        q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        self.conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, ?, ?, ?)',
                          (q, 'other', 'active', 1)).close()
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE').close()
        values = {}
        for n, (cid, project, created, ended, reason, synced) in enumerate((
                ('o', P, 10, None, None, None),
                ('s', P, 20, 25, 'matched', 26),
                ('k', P, 50, 55, 'unmatched', None),
                ('j', P, 30, 35, 'matched', None),
                ('i', P, 30, 36, 'matched', None),
                ('q', q, 15, 16, 'matched', None))):
            row = (cid, project, 'repository-' + cid, '/git/' + cid, '/p/' + cid,
                   100 + n, 200 + n, created, ended, reason, synced)
            self.conn.execute('INSERT INTO checkout_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                              row).close()
            values[cid] = row
        self.conn.commit()
        reader = store_layout.open_catalogue_readonly(self.root)
        self.addCleanup(reader.close)
        self.assertEqual([astuple(row) for row in store_layout.owed_ended_routes(reader, P)],
                         [values[cid] for cid in ('i', 'j', 'k')])
        self.assertFalse(reader.in_transaction)

    def test_owed_ended_routes_empty(self):
        # R2 includes an ended route whose ledger end is already settled.
        self.assertEqual(store_layout.owed_ended_routes(self.conn, P), [])
        self.insert('done', 10, 15, 'matched')
        self.conn.execute("UPDATE checkout_routes SET ledger_synced_at=16 WHERE checkout_id='done'").close()
        self.conn.commit()
        self.assertEqual(store_layout.owed_ended_routes(self.conn, P), [])

    def test_owed_ended_routes_keeps_caller_transaction(self):
        # R6 commits the caller's pending route instead of leaving it owned.
        self.conn.execute('BEGIN IMMEDIATE').close()
        pending = self.insert('pending', 10, 15, 'matched')
        self.assertEqual(store_layout.owed_ended_routes(self.conn, P),
                         [store_layout.CheckoutRoute(*pending)])
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(store_layout.owed_ended_routes(self.conn, P), [])
        self.conn.execute('BEGIN IMMEDIATE').close()
        self.insert('pending-error', 20, 25, 'matched')
        self.conn.execute('DROP TABLE checkout_routes').close()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.owed_ended_routes(self.conn, P)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(store_layout.owed_ended_routes(self.conn, P), [])

    def test_owed_ended_routes_cleans_up_its_own(self):
        # R7 omits owned rollback; R8 swallows the unreadable catalogue.
        self.assertEqual(store_layout.owed_ended_routes(self.conn, P), [])
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute('DROP TABLE checkout_routes').close()
        self.conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            store_layout.owed_ended_routes(self.conn, P)
        self.assertFalse(self.conn.in_transaction)

    def test_checkout_reads_select_route_fields(self):
        # K1 drops a column; K2 swaps inode fields; K3 swaps end fields.
        import dataclasses
        trace = []
        self.conn.set_trace_callback(trace.append)
        self.addCleanup(self.conn.set_trace_callback, None)
        self.assertIsNone(store_layout.checkout_route(self.conn, 'none'))
        self.assertEqual(store_layout.checkout_routes(self.conn, dev=0, ino=0), [])
        statements = [text for text in trace if text.startswith('SELECT')]
        self.assertEqual(len(statements), 2)
        expected = [field.name for field in dataclasses.fields(store_layout.CheckoutRoute)]
        for name, statement in zip(('checkout_route', 'checkout_routes'), statements):
            columns = statement.split('SELECT', 1)[1].split('FROM', 1)[0]
            self.assertEqual([column.strip() for column in columns.split(',')], expected, name)

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


class TestIncumbentRecord(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'store'
        self.root.mkdir(mode=0o700)
        self.lock = self.root / 'api-service.lock'
        self.lock.touch(mode=0o600)

    def open_guard(self):
        guard = store_layout.IncumbentGuard.acquire(self.root)
        self.addCleanup(guard.close)
        return guard

    def idle(self, guard):
        proof = guard._disposition('enrollment', P, P, None, 'a' * 64)
        return guard._publish_idle(None, proof)

    def owner(self):
        return store_layout._IncumbentOwner('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
                                           os.getpid(), 'birth', 'boot', 'runtime', 'off')

    def test_live_contention_and_identity(self):
        guard = self.open_guard()
        alias = Path(self.root, '..', self.root.name)
        script = """
from relay_engine.store_layout import IncumbentGuard, LayoutFault
import sys
try:
    IncumbentGuard.acquire(sys.argv[1])
except LayoutFault as fault:
    print(fault.cause)
"""
        result = subprocess.run([sys.executable, '-c', script, str(alias)],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(result.stdout.strip(), 'store-locked')
        linked = self.root / 'lock-alias'
        os.link(self.lock, linked)
        with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
            guard.read()
        linked.unlink()
        self.root.rename(self.root.with_name('original'))
        self.root.mkdir(mode=0o700)
        self.lock.touch(mode=0o600)
        with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
            guard.read()
        self.lock.unlink()
        self.root.rmdir()
        self.root.with_name('original').rename(self.root)
        self.lock.rename(self.root / 'old.lock')
        self.lock.touch(mode=0o600)
        with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
            guard.read()
        with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
            store_layout.IncumbentGuard.acquire(self.root, expected_lock=guard.lock_identity)

    def test_record_validation_before_database_open(self):
        guard = self.open_guard()
        with patch.object(store_layout.sqlite3, 'connect') as opens:
            with self.assertRaisesRegex(store_layout.LayoutFault, 'enrollment-required'):
                guard.read()
            frame = self.idle(guard)
            self.assertEqual(guard.read(), frame)
            path = self.root / '.adt-store-incumbent.json'
            path.chmod(0o644)
            with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
                guard.read()
            path.chmod(0o600)
            for payload in (b'{"version":1,"version":1}', b'x' * (16 * 1024 + 1)):
                path.write_bytes(payload)
                with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
                    guard.read()
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(store_layout._canonical_frame(store_layout.asdict(frame)))
            replacement.chmod(0o600)
            replacement.replace(path)
            with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-invalid'):
                guard.read()
            opens.assert_not_called()

    def test_durable_transition_failure(self):
        guard = self.open_guard()
        idle = self.idle(guard)
        owner = self.owner()
        with patch.object(store_layout.os, 'replace', side_effect=OSError('replace')):
            with self.assertRaises(OSError):
                guard.arm(idle, owner)
        self.assertEqual(guard.read(), idle)
        with self.assertRaisesRegex(store_layout.LayoutFault, 'idle-unverified'):
            guard.arm(idle, owner)

        guard._verify_idle(idle, guard._disposition('enrollment', P, P, None, 'a' * 64))
        original_sync = os.fsync
        def fail_directory(fd):
            if fd == guard._root_fd:
                raise OSError('parent sync')
            original_sync(fd)
        with patch.object(store_layout.os, 'fsync', side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, 'parent sync'):
                guard.arm(idle, owner)
        identity = guard.lock_identity
        guard.close()
        with store_layout.IncumbentGuard.acquire(self.root, expected_lock=identity) as fresh:
            armed = fresh.read()
            self.assertEqual(armed.owner, owner)
            with self.assertRaisesRegex(store_layout.LayoutFault, 'retirement-pending'):
                fresh.arm(armed, owner)
            _INCUMBENT_LOG.info('arm fsync failure: lock=%s visible=%s next_open=refused',
                                identity, armed.phase)

        # A fresh process holds the original lock and fails only after publishing armed bytes.
        readback_root = self.root.with_name('arm-readback')
        readback_root.mkdir(mode=0o700)
        (readback_root / 'api-service.lock').touch(mode=0o600)
        with store_layout.IncumbentGuard.acquire(readback_root) as guard:
            proof = guard._retain('enrollment', P, P, None, b'arm test fixture')
            guard._publish_idle(None, proof.summary())
            readback_lock = guard.lock_identity
        script = """
from relay_engine import store_layout
from unittest.mock import patch
import os, sys
with store_layout.IncumbentGuard.acquire(sys.argv[1]) as guard:
    disposition = guard._retain('enrollment', sys.argv[2], sys.argv[2], None, b'arm test fixture')
    idle = guard.verify_idle(guard.read(), disposition)
    owner = store_layout._IncumbentOwner(sys.argv[2], os.getpid(), 'birth', 'boot', 'runtime', 'off')
    read = guard._read_frame
    def fail_read(*, sync=False):
        frame, data = read(sync=sync)
        if frame.phase == 'armed':
            raise OSError('arm readback')
        return frame, data
    with patch.object(guard, '_read_frame', side_effect=fail_read):
        try:
            guard.arm(idle, owner)
        except OSError as error:
            assert str(error) == 'arm readback'
            os._exit(17)
        raise AssertionError('arm unexpectedly verified')
"""
        result = subprocess.run([sys.executable, '-c', script, str(readback_root), P],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 17, result.stderr)
        with store_layout.IncumbentGuard.acquire(readback_root, expected_lock=readback_lock) as fresh:
            with patch.object(store_layout.sqlite3, 'connect') as opens:
                armed = fresh.read()
                with self.assertRaisesRegex(store_layout.LayoutFault, 'retirement-pending'):
                    fresh.arm(armed, owner)
                opens.assert_not_called()
            _INCUMBENT_LOG.info('arm readback failure: lock=%s old_exit=%s old_pid=%s '
                                'visible=armed ordinary_opens=0', readback_lock,
                                result.returncode, armed.owner.pid)



def _incumbent_birth(pid):
    if sys.platform == 'darwin':
        from adapters.window import process_start
        return process_start(pid)
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def _incumbent_boot():
    if sys.platform == 'darwin':
        return subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                                       text=True).strip()
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _incumbent_process(root, pipe, notifier, action):
    owner = store_layout._IncumbentOwner(str(uuid4()), os.getpid(), _incumbent_birth(os.getpid()),
                                         _incumbent_boot(), 'fixture-runtime', notifier)
    with store_layout.IncumbentGuard.acquire(root) as guard:
        part = pipe.recv()
        idle = guard.verify_idle(guard.read(), part)
        armed = guard.arm(idle, owner)
        if action == 'recover':
            operation = _submit_fixture_operation(root)
            witness = guard._checkpoint(armed, operation)
            pipe.send((armed, witness))
            pipe.recv()
        else:
            retirement = guard._retirement_proof(armed, b'fixture: handles settled')
            sync = os.fsync
            read = guard._read_frame
            def fail_sync(fd):
                if fd == guard._root_fd:
                    raise OSError('post-replace directory sync')
                sync(fd)
            def fail_read(*, sync=False):
                frame, data = read(sync=sync)
                if frame.phase == 'idle':
                    raise OSError('post-replace readback')
                return frame, data
            fault = (patch.object(os, 'fsync', side_effect=fail_sync) if action == 'sync'
                     else patch.object(guard, '_read_frame', side_effect=fail_read))
            with fault:
                try:
                    guard.retire(armed, retirement)
                except OSError as exc:
                    pipe.send((armed, retirement, str(exc)))
                else:
                    raise AssertionError('retirement unexpectedly succeeded')
            pipe.recv()
        os._exit(17)


def _incumbent_fresh(root, lock, expected, disposition, pipe):
    with store_layout.IncumbentGuard.acquire(root, expected_lock=lock) as guard:
        with patch.object(store_layout.sqlite3, 'connect') as opens:
            failures = []
            for proof in (None, disposition):
                with patch.object(os, 'fsync', side_effect=OSError('repeat sync failure')):
                    try:
                        guard.verify_idle(expected, proof)
                    except (store_layout.LayoutFault, OSError) as exc:
                        failures.append(str(exc))
                try:
                    guard.arm(expected, store_layout._IncumbentOwner(
                        str(uuid4()), os.getpid(), _incumbent_birth(os.getpid()),
                        _incumbent_boot(), 'fixture-runtime', 'off'))
                except store_layout.LayoutFault as exc:
                    failures.append(exc.cause)
            verified = guard.verify_idle(expected, disposition)
            fresh = guard.arm(verified, store_layout._IncumbentOwner(
                str(uuid4()), os.getpid(), _incumbent_birth(os.getpid()),
                _incumbent_boot(), 'fixture-runtime', 'off'))
            refused_opens = opens.call_count
        with closing(store_layout.open_ledger(root, P)) as ledger:
            project = store_layout._row(ledger, 'SELECT id FROM projects')
        pipe.send((guard.lock_identity, failures, refused_opens, fresh, project))


def _pending_notification(root):
    with closing(sqlite3.connect(store_layout.catalogue_path(root).as_uri() + '?immutable=1',
                                 uri=True)) as catalogue:
        return store_layout._row(catalogue, 'SELECT item_id,revision,state,attempts,'
                                'next_attempt_at_ms FROM queue_notifications')


def _materialize_incumbent_store(root):
    with closing(store_layout.open_catalogue(root)) as catalogue:
        catalogue.execute('UPDATE catalogue_identity SET store_id=?', (_INCUMBENT_STORE_ID,)).close()
        catalogue.execute("INSERT INTO project_routes (project_id,name,state,created_at) "
                          "VALUES (?, 'example', 'active', 1)", (P,)).close()
        catalogue.execute(
            "INSERT INTO queue_items (item_id,kind,subkind,project_id,raiser,revision,state,"
            "texts,pins,condition_key,raised_position,raised_at_ms) VALUES "
            "('fixture-pending','notice','project_stopped',?,'fixture',1,'pending','{}','{}',"
            "'fixture',1,1000)", (P,)).close()
        catalogue.execute(
            "INSERT INTO queue_notifications (item_id,revision,state,next_attempt_at_ms) "
            "VALUES ('fixture-pending',1,'pending',1000)").close()
        catalogue.commit()
    with closing(store_layout.open_ledger(root, P, create=True)) as ledger:
        ledger.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'example', 1)",
                       (P,)).close()
        ledger.commit()
    store_layout.catalogue_path(root).chmod(0o600)
    store_layout.ledger_path(root, P).chmod(0o600)


def _submit_fixture_operation(root):
    from relay_engine.tests.test_submit import FakeDirectory, draft
    sender = submit.SeatRef('operator', 'operator', 'operator', None, P)
    data = draft(to='operator')
    key = 'incumbent-operation'
    ctx = submit.SubmitContext(sender, None, lambda: 1700000000000,
                               FakeDirectory(), submission_key=key)
    with closing(store_layout.open_ledger(root, P)) as ledger:
        admitted = submit.submit(ledger, ctx, data)
    return store_layout._IncumbentOperation(key, data, admitted)


class TestIncumbentOwnerTransitions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'store'

    def start_process(self, target, *args):
        ctx = multiprocessing.get_context('spawn')
        pipe, child_pipe = ctx.Pipe()
        if target is _incumbent_process:
            process = ctx.Process(target=target, args=(self.root, child_pipe, *args))
        else:
            process = ctx.Process(target=target, args=(self.root, *args, child_pipe))
        process.start()
        child_pipe.close()
        self.addCleanup(pipe.close)
        self.addCleanup(self.stop_process, process)
        return process, pipe

    @staticmethod
    def stop_process(process):
        if process.is_alive():
            process.kill()
        process.join(10)
        process.close()

    def receive(self, pipe):
        self.assertTrue(pipe.poll(20), 'fixture child must report its observation')
        return pipe.recv()

    def setup_armed_birth(self):
        guard, birth = store_layout.IncumbentGuard._create_new(self.root, _INCUMBENT_STORE_ID, P)
        self.addCleanup(guard.close)
        idle, enrollment = guard.enroll_new(birth)
        self.assertFalse(store_layout.catalogue_path(self.root).exists())
        owner = store_layout._IncumbentOwner(str(uuid4()), os.getpid(),
                                           _incumbent_birth(os.getpid()), _incumbent_boot(),
                                           'fixture-runtime', 'off')
        armed = guard.arm(idle, owner)
        self.assertEqual(guard.read(), armed)
        _materialize_incumbent_store(self.root)
        return guard, armed

    def retire_birth(self):
        guard, armed = self.setup_armed_birth()
        self.assertEqual(guard._protected_identity(armed), _INCUMBENT_STORE_ID)
        retirement = guard._retirement_proof(armed, b'fixture: handles settled')
        idle = guard.retire(armed, retirement)
        identity = guard.lock_identity
        guard.close()
        return idle, retirement, identity

    def test_explicit_birth_and_clean_rearm(self):
        idle, proof, identity = self.retire_birth()
        with self.assertRaises(FileExistsError):
            store_layout.IncumbentGuard._create_new(self.root, _INCUMBENT_STORE_ID, P)
        with store_layout.IncumbentGuard.acquire(self.root, expected_lock=identity) as guard:
            with self.assertRaisesRegex(store_layout.LayoutFault, 'idle-unverified'):
                guard.arm(idle, None)
            self.assertEqual(guard.verify_idle(idle, proof), idle)
            owner = store_layout._IncumbentOwner(str(uuid4()), os.getpid(),
                                               _incumbent_birth(os.getpid()), _incumbent_boot(),
                                               'fixture-runtime', 'off')
            successor = guard.arm(idle, owner)
            self.assertNotEqual(successor.owner.epoch, proof.prior_epoch)
            with self.assertRaisesRegex(store_layout.LayoutFault, 'idle-unverified'):
                guard.arm(idle, owner)

    def test_offline_enrollment_and_hold_refusal(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'api-service.lock').touch(mode=0o600)
        _materialize_incumbent_store(self.root)
        with store_layout.IncumbentGuard.acquire(self.root) as guard:
            with self.assertRaisesRegex(store_layout.LayoutFault, 'enrollment-required'):
                guard.read()
            with self.assertRaisesRegex(store_layout.LayoutFault, 'enrollment-unproved'):
                guard.enroll_existing(None)
            approved = guard._offline_checkpoint(_INCUMBENT_STORE_ID, P, b'fixture: known provenance and quiescence')
            with closing(store_layout.open_catalogue(self.root)) as catalogue:
                catalogue.execute('BEGIN IMMEDIATE').close()
                store_layout.record_stop(catalogue, 'store', None, 'operator')
                catalogue.commit()
            with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-hold'):
                guard.enroll_existing(guard._offline_checkpoint(
                    _INCUMBENT_STORE_ID, P, b'fixture: known provenance and quiescence'))
            with closing(store_layout.open_catalogue(self.root)) as catalogue:
                catalogue.execute('BEGIN IMMEDIATE').close()
                store_layout.record_start(catalogue, 'store', None, 'operator')
                catalogue.commit()
            job = submit.new_uuid7(1700000000000)
            with closing(store_layout.open_catalogue(self.root)) as catalogue:
                store_layout.take_holds(catalogue, job, 'cutover', [store_layout.project_scope(P)], 4)
                catalogue.commit()
            with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-hold'):
                guard.enroll_existing(approved)
            with closing(store_layout.open_catalogue(self.root)) as catalogue:
                self.assertEqual(store_layout.release_holds(catalogue, job), 1)
                catalogue.commit()
            with closing(store_layout.open_ledger(self.root, P)) as ledger:
                restore_cancel.cancel_all(ledger, P, job, 4)
            with self.assertRaisesRegex(store_layout.LayoutFault, 'incumbent-hold'):
                guard.enroll_existing(approved)
            with closing(store_layout.open_ledger(self.root, P)) as ledger:
                self.assertIsNotNone(restore_cancel.held(ledger, P))
                ledger.execute('UPDATE restore_cancels SET released_at=5, released_by=?',
                               ('operator',)).close()
                ledger.commit()
            idle, _ = guard.enroll_existing(approved)
            self.assertEqual(idle.phase, 'idle')
            _INCUMBENT_LOG.info('legacy enrollment: lock=%s stop, maintenance, restore held; '
                                'idle after separate fixture release', guard.lock_identity)

    def recover_fixture(self, notifier):
        _, proof, lock = self.retire_birth()
        process, pipe = self.start_process(_incumbent_process, notifier, 'recover')
        pipe.send(proof)
        armed, witness = self.receive(pipe)
        child = store_layout._IncumbentChild.observe(
            process, armed, _incumbent_birth(process.pid), _incumbent_boot())
        with self.assertRaisesRegex(store_layout.LayoutFault, 'store-locked'):
            store_layout.IncumbentGuard.acquire(self.root, expected_lock=lock)
        pipe.send('exit')
        process.join(10)
        self.assertEqual(process.exitcode, 17)
        _INCUMBENT_LOG.info('original owner: pid=%s birth=%s boot=%s epoch=%s exit=%s '
                            'lock=%s held-before-exit=true notifier=%s',
                            armed.owner.pid, armed.owner.birth, armed.owner.boot, armed.owner.epoch,
                            process.exitcode, lock, armed.owner.notifier)
        return armed, witness, child, lock

    def test_abnormal_notifier_off_with_keyed_readback(self):
        armed, witness, child, lock = self.recover_fixture('off')
        with store_layout.IncumbentGuard.acquire(self.root, expected_lock=lock) as guard:
            for value in (None, replace(witness, operation=None),
                          replace(witness, operation=replace(witness.operation, key='unknown')),
                          replace(witness, operation=replace(witness.operation, draft=b'wrong'))):
                with self.assertRaises(store_layout.LayoutFault):
                    guard.recover(armed, child, value)
                self.assertEqual(guard.read(), armed)
            sidecar = Path(str(store_layout.ledger_path(self.root, P)) + '-wal')
            sidecar.touch()
            with self.assertRaisesRegex(store_layout.LayoutFault, 'journal-unresolved'):
                guard.recover(armed, child, witness)
            self.assertEqual(guard.read(), armed)
            sidecar.unlink()
            idle, proof = guard.recover(armed, child, witness)
            self.assertEqual(proof.prior_epoch, armed.owner.epoch)
            self.assertEqual(guard.read(), idle)
            successor = guard.arm(idle, replace(armed.owner, epoch=str(uuid4()), pid=os.getpid()))
            self.assertNotEqual(successor.owner.epoch, armed.owner.epoch)
            _INCUMBENT_LOG.info('protected recovery: lock=%s members=%s key=%s receipt=%s '
                                'idle_proof=%s new_epoch=%s', lock, witness.members,
                                witness.operation.key, witness.operation.admitted,
                                proof.summary().digest, successor.owner.epoch)

    def test_unresolved_prior_epoch_refuses_without_replay(self):
        for mode in ('unknown', 'configured'):
            with self.subTest(notifier=mode):
                self.root = self.root.with_name(mode)
                armed, witness, child, lock = self.recover_fixture(mode)
                expected = ('fixture-pending', 1, 'pending', 0, 1000)
                with store_layout.IncumbentGuard.acquire(self.root, expected_lock=lock) as guard:
                    self.assertEqual(_pending_notification(self.root), expected)
                    with patch.object(store_layout.sqlite3, 'connect') as opens:
                        with self.assertRaisesRegex(store_layout.LayoutFault, 'notifier-unresolved'):
                            guard.recover(armed, child, witness)
                        self.assertEqual(guard.read(), armed)
                        opens.assert_not_called()
                    observed = _pending_notification(self.root)
                    self.assertEqual(observed, expected)
                    _INCUMBENT_LOG.info('unresolved notifier: lock=%s mode=%s pending=%s '
                                        'phase=armed ordinary_opens=0', lock, mode, observed)

    def test_interrupted_idle_rechecked_after_holder_exit(self):
        for fault in ('sync', 'readback'):
            with self.subTest(fault=fault):
                self.root = self.root.with_name(fault)
                _, prior, lock = self.retire_birth()
                process, pipe = self.start_process(_incumbent_process, 'off', fault)
                pipe.send(prior)
                armed, proof, error = self.receive(pipe)
                self.assertIn(fault, error)
                with self.assertRaisesRegex(store_layout.LayoutFault, 'store-locked'):
                    store_layout.IncumbentGuard.acquire(self.root, expected_lock=lock)
                pipe.send('exit')
                process.join(10)
                self.assertEqual(process.exitcode, 17)
                with store_layout.IncumbentGuard.acquire(self.root, expected_lock=lock) as guard:
                    idle = guard.read()
                    self.assertEqual(idle.phase, 'idle')
                successor, reply = self.start_process(_incumbent_fresh, lock, idle, proof)
                held, failures, opens, fresh, project = self.receive(reply)
                successor.join(10)
                self.assertEqual(successor.exitcode, 0)
                self.assertEqual(held, lock)
                self.assertEqual(failures, ['idle-unverified', 'idle-unverified',
                                            'repeat sync failure', 'idle-unverified'])
                self.assertEqual(opens, 0)
                self.assertEqual(project, (P,))
                self.assertNotEqual(fresh.owner.epoch, armed.owner.epoch)
                self.assertNotEqual(fresh.owner.pid, armed.owner.pid)
                _INCUMBENT_LOG.info('interrupted idle: fault=%s lock=%s original_pid=%s '
                                    'original_exit=%s fresh_pid=%s checks=%s ordinary_opens=%s '
                                    'visible=idle verified_then_armed=%s', fault, held,
                                    armed.owner.pid, process.exitcode, fresh.owner.pid, failures,
                                    opens, fresh.owner.epoch)
