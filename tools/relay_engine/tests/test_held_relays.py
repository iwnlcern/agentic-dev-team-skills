"""Store's held-relay records and local transition rules."""
import ast
from contextlib import closing
from pathlib import Path
import sqlite3

from relay_engine import catalogue, schema
import relay_engine.held_relays as held_relays
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


class TestStep(test_schema.Store):
    def setUp(self):
        self.conn = self.connect()
        self.relay(self.conn, 'relay', 1, body_sha=SHA)

    def admit(self, receiver='q', decision='held', facts=None, now=1):
        if facts is None:
            facts = held_relays.ReceiverFacts('active', False, 'present')
        return held_relays.admit(self.conn, 'relay', receiver, SHA, (
            held_relays.Recipient(receiver + '-seat', 'team.implementer@' + receiver, 'to'),
            held_relays.Recipient(receiver + '-cc', 'team.planner@' + receiver, 'cc'),
        ), decision, facts, now)

    def select(self, sql):
        with closing(self.conn.execute(sql)) as cursor:
            return cursor.fetchall()

    def test_step_cases(self):
        cases = (
            ('not_active', True, 'not_read', 'denied', 'returned', 'destination_not_active'),
            ('active', True, 'not_read', 'denied', 'denied', None),
            ('active', True, 'not_read', 'held', 'held', None),
            ('active', True, 'unavailable', 'allowed', 'allowed', 'receiver_stopped'),
            ('active', False, 'unavailable', 'allowed', 'allowed', 'receiver_unavailable'),
            ('active', False, 'not_read', 'allowed', 'allowed', 'receiver_unavailable'),
            ('active', False, 'missing', 'allowed', 'returned', 'recipient_not_found'),
            ('active', False, 'present', 'allowed', 'delivered', None),
        )
        for i, (route, stopped, seats, decision, state, reason) in enumerate(cases):
            receiver = f'q{i}'
            self.admit(receiver)
            with self.subTest(receiver=receiver, route=route, decision=decision, seats=seats):
                res = held_relays.step(self.conn, 'relay', receiver, decision,
                                       held_relays.ReceiverFacts(route, stopped, seats), 2)
                self.assertEqual(res, held_relays.State(state, reason))
                with closing(self.conn.execute(
                    'SELECT state,reason FROM xproject_transfers WHERE receiver_project_id=?',
                    (receiver,),
                )) as cursor:
                    self.assertEqual(cursor.fetchone(), (state, reason))
        self.assertEqual(self.select('SELECT relay_id,receiver_project_id,body_sha256,delivered_at '
                                     'FROM xproject_deliveries'), [('relay', 'q7', SHA, 2)])

    def test_step_writes_nothing_unchanged(self):
        self.admit()
        facts = held_relays.ReceiverFacts('active', False, 'unavailable')
        writes = self.conn.total_changes
        self.assertEqual(held_relays.step(self.conn, 'relay', 'q', 'held', facts, 2),
                         held_relays.State('held', None))
        self.assertEqual(self.conn.total_changes, writes)
        held_relays.step(self.conn, 'relay', 'q', 'allowed', facts, 3)
        self.assertEqual(self.select('SELECT from_state,to_state,reason,at FROM xproject_events '
                                     'ORDER BY event_id')[-1],
                         ('held', 'allowed', 'receiver_unavailable', 3))
        self.assertEqual(len(self.select('SELECT event_id FROM xproject_events')), 2)

    def test_admit_inserts_and_steps(self):
        self.assertEqual(self.admit(decision='allowed'), held_relays.State('delivered', None))
        self.assertEqual(self.select('SELECT receiver_project_id,seat_id,role,opened_at '
                                     'FROM xproject_recipients ORDER BY role'),
                         [('q', 'q-cc', 'cc', None), ('q', 'q-seat', 'to', None)])
        self.assertEqual(self.select('SELECT state,body_sha256 FROM xproject_transfers'),
                         [('delivered', SHA)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.admit()

    def test_unknown_decision_refused(self):
        writes = self.conn.total_changes
        with self.assertRaises(ValueError):
            self.admit(decision='maybe')
        self.assertEqual(self.conn.total_changes, writes)
        self.admit()
        writes = self.conn.total_changes
        with self.assertRaises(ValueError):
            held_relays.step(self.conn, 'relay', 'q', 'maybe',
                             held_relays.ReceiverFacts('active', False, 'present'), 2)
        self.assertEqual(self.conn.total_changes, writes)

    def test_terminal_not_stepped(self):
        self.admit(decision='allowed')
        writes = self.conn.total_changes
        self.assertEqual(held_relays.step(
            self.conn, 'relay', 'q', 'held',
            held_relays.ReceiverFacts('not_active', False, 'not_read'), 2),
            held_relays.State('delivered', None))
        self.assertEqual(self.conn.total_changes, writes)

    def test_mark_opened_once(self):
        self.admit(decision='allowed')
        self.assertIs(held_relays.mark_opened(self.conn, 'relay', 'q', 'q-seat', 10), True)
        self.assertIs(held_relays.mark_opened(self.conn, 'relay', 'q', 'q-seat', 11), False)
        self.assertEqual(self.select("SELECT opened_at FROM xproject_recipients WHERE role='to'"), [(10,)])
        with self.assertRaises(ValueError):
            held_relays.mark_opened(self.conn, 'relay', 'q', 'other-seat', 10)

    def test_no_time_out(self):
        self.admit()
        facts = held_relays.ReceiverFacts('active', False, 'not_read')
        for now in (10, 1000, 10**10):
            self.assertEqual(held_relays.step(self.conn, 'relay', 'q', 'held', facts, now),
                             held_relays.State('held', None))

    def test_imports_no_authority(self):
        banned = {'xproject', 'authority', 'slips', 'claims', 'restore_cancel', 'operator_queue'}
        for node in ast.walk(ast.parse(Path(held_relays.__file__).read_text())):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = {part for alias in node.names for part in alias.name.split('.')}
                if isinstance(node, ast.ImportFrom):
                    names.update((node.module or '').split('.'))
                self.assertFalse(names & banned)

    def test_status_and_non_terminal(self):
        self.admit('q2', decision='allowed', facts=held_relays.ReceiverFacts('active', True, 'not_read'), now=2)
        self.admit('q1', now=3)
        self.assertEqual(held_relays.status(self.conn, 'relay'), (
            held_relays.TransferStatus('relay', 'q1', SHA, 'held', None, 3),
            held_relays.TransferStatus('relay', 'q2', SHA, 'allowed', 'receiver_stopped', 2),
        ))
        self.assertEqual(held_relays.status(self.conn, 'unknown'), ())
        self.admit('q3', decision='allowed')
        self.admit('q4', decision='denied')
        self.admit('q5', facts=held_relays.ReceiverFacts('not_active', False, 'not_read'))
        self.assertEqual(held_relays.non_terminal(self.conn),
                         (held_relays.TransferKey('relay', 'q1'), held_relays.TransferKey('relay', 'q2')))
        self.assertEqual(held_relays.non_terminal(self.conn, 'q2'),
                         (held_relays.TransferKey('relay', 'q2'),))
