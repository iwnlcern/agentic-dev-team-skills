"""Store's held-relay records and local transition rules."""
from contextlib import closing
import sqlite3

from relay_engine import catalogue, schema
from relay_engine.tests import test_schema


SHA = 'a' * 64
LEDGER = {'xproject_transfers', 'xproject_recipients', 'xproject_deliveries', 'xproject_events'}
CATALOGUE = {'xproject_inbox', 'xproject_asks'}


class TestHeldTables(test_schema.Store):
    def setUp(self):
        self.conn = self.connect()
        self.relay(self.conn, 'relay', 1, body_sha=SHA)

    def transfer(self, receiver='q', state='held', reason=None, digest=SHA):
        self.conn.execute(
            'INSERT INTO xproject_transfers '
            '(relay_id,receiver_project_id,body_sha256,state,reason,created_at,changed_at) '
            'VALUES (?,?,?,?,?,1,1)', ('relay', receiver, digest, state, reason)).close()

    def test_held_tables_strict_and_owned(self):
        cat = sqlite3.connect(':memory:')
        self.addCleanup(cat.close)
        catalogue.init_catalogue(cat)
        for conn, names, owners in ((self.conn, LEDGER, schema.TABLE_OWNERS),
                                    (cat, CATALOGUE, catalogue.TABLE_OWNERS)):
            with closing(conn.execute('PRAGMA table_list')) as cursor:
                strict = {row[1]: row[5] for row in cursor}
            for name in sorted(names):
                with self.subTest(table=name):
                    self.assertIn(name, strict)
                    self.assertEqual(strict[name], 1)
                    self.assertEqual(owners[name], 'store')

    def test_transfer_checks(self):
        for state, reason, digest in (('bad', None, SHA), ('held', 'receiver_stopped', SHA),
                                      ('allowed', 'recipient_not_found', SHA),
                                      ('returned', 'receiver_unavailable', SHA),
                                      ('held', None, None)):
            with self.subTest(state=state, reason=reason, digest=digest):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.transfer(state=state, reason=reason, digest=digest)
        self.transfer()

    def test_terminal_rows_frozen(self):
        for state in ('delivered', 'denied', 'returned'):
            self.transfer(receiver=state, state=state)
            with self.subTest(state=state):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'terminal transfer'):
                    self.conn.execute('UPDATE xproject_transfers SET changed_at=2 '
                                      'WHERE receiver_project_id=?', (state,)).close()
        self.transfer()
        with closing(self.conn.execute('UPDATE xproject_transfers SET changed_at=2 '
                                       "WHERE receiver_project_id='q'")) as cursor:
            self.assertEqual(cursor.rowcount, 1)

    def test_deliveries_insert_only(self):
        self.transfer()
        self.conn.execute('INSERT INTO xproject_deliveries VALUES (?,?,?,1)',
                          ('relay', 'q', SHA)).close()
        for stmt in ('UPDATE xproject_deliveries SET delivered_at=2',
                     'DELETE FROM xproject_deliveries'):
            with self.subTest(sql=stmt), self.assertRaisesRegex(sqlite3.IntegrityError, 'insert-only'):
                self.conn.execute(stmt).close()

    def test_events_append_only(self):
        self.transfer()
        self.conn.execute('INSERT INTO xproject_events '
                          '(relay_id,receiver_project_id,from_state,to_state,reason,at) '
                          "VALUES ('relay','q',NULL,'held',NULL,1)").close()
        for stmt in ('UPDATE xproject_events SET at=2', 'DELETE FROM xproject_events'):
            with self.subTest(sql=stmt), self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                self.conn.execute(stmt).close()

    def test_recipients_reference_transfer(self):
        stmt = ("INSERT INTO xproject_recipients (relay_id,receiver_project_id,seat_id,address,role) "
                "VALUES ('relay','q','q-seat','team.implementer@q','to')")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            self.conn.execute(stmt).close()
        self.transfer()
        self.conn.execute(stmt).close()
