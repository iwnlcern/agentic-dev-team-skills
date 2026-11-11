"""Noncommitting checkout jobs and their registration retention boundary."""
from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from relay_engine import checkout_store as jobs, schema, store_layout

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class CheckoutCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cat = store_layout.open_catalogue(self.root)
        self.addCleanup(self.cat.close)
        self.cat.execute('INSERT INTO project_routes VALUES (?, ?, ?, ?)',
                         (P, 'active', 'active', 1)).close()
        self.cat.execute('INSERT INTO project_routes VALUES (?, ?, ?, ?)',
                         (Q, 'creating', 'creating', 1)).close()
        self.cat.commit()
        self.ledger = sqlite3.connect(':memory:')
        self.addCleanup(self.ledger.close)
        self.ledger.execute('PRAGMA foreign_keys=ON').close()
        schema.init_v3(self.ledger)
        self.ledger.execute('INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)',
                            (P, 'active', 1)).close()
        self.ledger.commit()
        self.cat.execute('BEGIN IMMEDIATE').close()
        self.ledger.execute('BEGIN IMMEDIATE').close()

    def rows(self, conn, sql, parameters=()):
        cursor = conn.execute(sql, parameters)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def routes(self):
        return self.rows(self.cat, 'SELECT * FROM checkout_routes ORDER BY checkout_id')

    def ledger_rows(self):
        return self.rows(self.ledger, 'SELECT * FROM repository_checkouts ORDER BY path')

    def ensure(self):
        return jobs.ensure_repository_step(self.ledger, P, 'repository', now_ms=10)

    def new(self, repository_id='repo', checkout_id='own', *, dev=1, ino=2):
        return jobs.NewRoute(checkout_id, P, repository_id, '/git', '/work', dev, ino)

    def register(self, route=None, *, floor=0, now=10):
        route = route or self.new()
        result = jobs.register_route_step(self.cat, route, ledger_order=floor, now_ms=now)
        self.assertTrue(self.cat.in_transaction)
        return result

    def read(self, dev=1, ino=2):
        return store_layout.checkout_routes(self.cat, dev=dev, ino=ino)


class TestCheckoutSteps(CheckoutCase):
    def test_ensure_repository_inserts_once(self):
        # Catches duplicate repository insertion and a per-repository order floor.
        first = self.ensure()
        second = self.ensure()
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.repository_id, second.repository_id)
        self.assertEqual((first.ledger_order, second.ledger_order), (0, 0))
        self.assertEqual(self.rows(self.ledger, 'SELECT project_id, name FROM repositories'),
                         [(P, 'repository')])
        self.ledger.execute('INSERT INTO repository_checkouts VALUES (?, ?, ?, ?, ?, ?)',
                            (first.repository_id, '/other', 1, 'retained', 1099, 2)).close()
        self.assertEqual(self.ensure().ledger_order, 1099)
        self.assertTrue(self.ledger.in_transaction)
        self.ledger.rollback()
        self.assertEqual(self.rows(self.ledger, 'SELECT id FROM repositories'), [])

    def test_register_orders_above_both_floors(self):
        # Catches either missing catalogue or retained-ledger floor.
        self.assertEqual(self.register(self.new(checkout_id='old'), now=1000), jobs.Registered(1000))
        self.cat.execute("UPDATE checkout_routes SET ended_at=1001, end_reason='matched'").close()
        self.assertEqual(self.register(self.new(checkout_id='next'), floor=1099, now=1001),
                         jobs.Registered(1100))
        self.assertEqual(self.register(self.new(checkout_id='later', dev=3), floor=0, now=5000),
                         jobs.Registered(5000))
        self.cat.rollback()
        self.assertEqual(self.routes(), [])

    def test_register_refusals_write_nothing(self):
        # Catches refusal after mutation, incorrect same-inode matching or ending an owner.
        own = self.new()
        cases = [(replace(own, project_id='unknown'), 'project_not_found'),
                 (replace(own, project_id=Q), 'project_not_active')]
        for route, reason in cases:
            before = self.routes()
            with self.subTest(reason=reason), self.assertRaises(jobs.CheckoutRefused) as caught:
                self.register(route)
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(self.routes(), before)
        self.register(own)
        before = self.routes()
        self.assertEqual(self.register(own), jobs.AlreadyRegistered())
        self.assertEqual(self.routes(), before)
        for route, reason in [(replace(own, repository_id='elsewhere'), 'checkout_registered_elsewhere'),
                              (replace(own, checkout_id='new-id'), 'checkout_route_unmatched')]:
            with self.subTest(reason=reason), self.assertRaises(jobs.CheckoutRefused) as caught:
                self.register(route)
            self.assertEqual(caught.exception.reason, reason)
            if reason == 'checkout_registered_elsewhere':
                self.assertEqual(caught.exception.detail['project_id'], P)
                self.assertEqual(caught.exception.detail['repository_id'], 'repo')
            self.assertEqual(self.routes(), before)

    def test_unregister_sets_reason(self):
        # Catches id-only unregistration and failure to re-owe the ledger end.
        for index, supplied in enumerate(['own', 'other', None]):
            route = self.new(checkout_id='own', dev=index + 1)
            if index:
                route = replace(route, checkout_id='own-' + str(index))
            self.register(route)
            self.cat.execute('UPDATE checkout_routes SET ledger_synced_at=11 WHERE checkout_id=?',
                             (route.checkout_id,)).close()
            result = jobs.unregister_route_step(self.cat, dev=route.dev, ino=2,
                                                checkout_id=supplied, now_ms=20)
            want = 'matched' if index == 0 else 'unmatched'
            self.assertEqual(result, jobs.Ended(route.checkout_id, want))
            self.assertEqual(self.rows(self.cat, 'SELECT ended_at, end_reason, ledger_synced_at '
                                       'FROM checkout_routes WHERE checkout_id=?', (route.checkout_id,)),
                             [(20, want, None)])
            before = self.routes()
            self.assertEqual(jobs.unregister_route_step(self.cat, dev=route.dev, ino=2,
                                                        checkout_id=supplied, now_ms=21),
                             jobs.NotRegistered())
            self.assertEqual(self.routes(), before)

    def test_ledger_write_order(self):
        # Catches an old replay replacing a new owner or reviving its own ended row.
        repo = self.ensure().repository_id
        self.register(self.new(repo))
        old = self.read()[0]
        self.assertEqual(jobs.write_ledger_row_step(self.ledger, old, now_ms=11), 'written')
        self.assertEqual(self.ledger_rows(), [(repo, '/work', 11, 'own', 10, None)])
        self.assertEqual(jobs.write_ledger_row_step(self.ledger, old, now_ms=12), 'written')
        new = replace(old, checkout_id='new', created_at=20)
        self.assertEqual(jobs.write_ledger_row_step(self.ledger, new, now_ms=21), 'written')
        before = self.ledger_rows()
        self.assertEqual(jobs.write_ledger_row_step(self.ledger, old, now_ms=22), 'superseded')
        self.assertEqual(self.ledger_rows(), before)
        self.assertEqual(jobs.end_ledger_row_step(self.ledger, new, now_ms=30), 'ended')
        before = self.ledger_rows()
        self.assertEqual(jobs.write_ledger_row_step(self.ledger, new, now_ms=31), 'superseded')
        self.assertEqual(self.ledger_rows(), before)
        self.assertTrue(self.ledger.in_transaction)

    def test_ledger_end_compares_owner(self):
        # Catches ending a newer owner on the same path, or changing a settled end.
        repo = self.ensure().repository_id
        self.register(self.new(repo))
        old = self.read()[0]
        jobs.write_ledger_row_step(self.ledger, old, now_ms=11)
        self.assertEqual(jobs.end_ledger_row_step(self.ledger, old, now_ms=12), 'ended')
        new = replace(old, checkout_id='new', created_at=20)
        jobs.write_ledger_row_step(self.ledger, new, now_ms=21)
        before = self.ledger_rows()
        self.assertEqual(jobs.end_ledger_row_step(self.ledger, old, now_ms=22), 'unchanged')
        self.assertEqual(self.ledger_rows(), before)
        self.assertEqual(jobs.end_ledger_row_step(self.ledger, new, now_ms=23), 'ended')
        before = self.ledger_rows()
        self.assertEqual(jobs.end_ledger_row_step(self.ledger, new, now_ms=24), 'unchanged')
        self.assertEqual(self.ledger_rows(), before)

    def test_acknowledge_guards_state(self):
        # M4: an obsolete write acknowledgement must not settle a newly owed end.
        repo = self.ensure().repository_id
        self.register(self.new(repo))
        route = self.read()[0]
        jobs.write_ledger_row_step(self.ledger, route, now_ms=11)
        jobs.unregister_route_step(self.cat, dev=1, ino=2, checkout_id='own', now_ms=12)
        self.assertFalse(jobs.acknowledge_step(self.cat, 'own', 'write', now_ms=13))
        ended = self.read()[0]
        self.assertIsNone(ended.ledger_synced_at)
        self.assertEqual(self.ledger_rows()[0][-1], None)
        self.assertEqual(jobs.end_ledger_row_step(self.ledger, ended, now_ms=14), 'ended')
        self.assertTrue(jobs.acknowledge_step(self.cat, 'own', 'end', now_ms=15))
        self.assertEqual(self.read()[0].ledger_synced_at, 15)
        self.assertEqual(self.ledger_rows()[0][-1], 14)
        self.assertFalse(jobs.acknowledge_step(self.cat, 'own', 'end', now_ms=16))
        self.assertEqual(self.read()[0].ledger_synced_at, 15)

    def test_unregister_ends_revived_route_unmatched(self):
        # M5: the restored route has no kept id, yet its inode must be ended.
        self.register()
        self.cat.commit()
        copy = self.root / 'backup.sqlite3'
        backup = sqlite3.connect(copy)
        try:
            self.cat.backup(backup)
        finally:
            backup.close()
        self.cat.execute('BEGIN IMMEDIATE').close()
        jobs.unregister_route_step(self.cat, dev=1, ino=2, checkout_id='own', now_ms=20)
        self.cat.commit()
        self.cat.close()
        path = store_layout.catalogue_path(self.root)
        self.assertFalse(Path(str(path) + '-wal').exists())
        self.assertFalse(Path(str(copy) + '-wal').exists())
        os.replace(copy, path)
        self.cat = store_layout.open_catalogue(self.root)
        self.addCleanup(self.cat.close)
        self.assertIsNone(self.read()[0].ended_at)
        self.cat.execute('BEGIN IMMEDIATE').close()
        self.assertEqual(jobs.unregister_route_step(self.cat, dev=1, ino=2,
                                                    checkout_id=None, now_ms=30),
                         jobs.Ended('own', 'unmatched'))
        self.assertEqual([(r.ended_at, r.end_reason) for r in self.read()], [(30, 'unmatched')])
        self.assertEqual([r for r in self.read() if r.ended_at is None], [])

    def test_register_with_existing_checkout_id_writes_nothing(self):
        # Catches silent conflict replacement for an id used on another inode.
        self.register()
        before = self.routes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.register(self.new(dev=9))
        self.assertEqual(self.routes(), before)

    def test_acknowledge_unknown_checkout(self):
        before = self.routes()
        self.assertFalse(jobs.acknowledge_step(self.cat, 'unknown', 'write', now_ms=10))
        self.assertEqual(self.routes(), before)
        self.assertTrue(self.cat.in_transaction)


class TestRegistrationSequence(CheckoutCase):
    def test_unlisted_writer_reason_writes_nothing(self):
        # Test-only port double: production WriterUnavailable seam remains unqualified.
        class Refused(Exception):
            pass

        def facts():
            return (self.routes(), self.ledger_rows(),
                    self.rows(self.ledger, 'SELECT id, project_id, name FROM repositories ORDER BY id'))

        def job(conn, name, body, refuse):
            before = facts()
            if name == refuse:
                try:
                    raise Refused('reason_not_in_the_list')
                finally:
                    self.assertEqual(facts(), before)
                    if name == 'register':
                        self.assertEqual(len(before[2]), 1)
                        self.assertEqual(before[0:2], ([], []))
                    else:
                        self.assertEqual(len(before[0]), 1)
                        self.assertEqual(len(before[1]), 1)
                        self.assertIsNone(before[0][0][-1])
            conn.execute('BEGIN IMMEDIATE').close()
            try:
                result = body()
                self.assertTrue(conn.in_transaction)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

        def sequence(refuse=None):
            repo = job(self.ledger, 'ensure', self.ensure, refuse)
            new = self.new(repo.repository_id)
            job(self.cat, 'register', lambda: jobs.register_route_step(
                self.cat, new, ledger_order=repo.ledger_order, now_ms=20), refuse)
            route = self.read()[0]
            job(self.ledger, 'write', lambda: jobs.write_ledger_row_step(
                self.ledger, route, now_ms=21), refuse)
            job(self.cat, 'write-acknowledge', lambda: jobs.acknowledge_step(
                self.cat, 'own', 'write', now_ms=22), refuse)

        self.cat.commit()
        self.ledger.commit()
        # Distinct real databases keep the two refusal histories independent.
        for refused in ['register', 'write-acknowledge']:
            with self.subTest(refused=refused):
                if refused != 'register':
                    self.doCleanups()
                    self.setUp()
                    self.cat.commit()
                    self.ledger.commit()
                with self.assertRaisesRegex(Refused, 'reason_not_in_the_list'):
                    sequence(refused)
                sequence()
                routes, ledger_rows, repositories = facts()
                self.assertEqual(len(routes), 1)
                self.assertIsNone(routes[0][8])
                self.assertEqual(len(repositories), 1)
                self.assertEqual(len(ledger_rows), 1)
                self.assertEqual(ledger_rows[0][0], repositories[0][0])
                self.assertEqual(ledger_rows[0][3], routes[0][0])
                self.assertIsNotNone(routes[0][-1])
