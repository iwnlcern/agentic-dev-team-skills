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


class TestProjection(test_schema.Store):
    def setUp(self):
        self.conn = self.connect()
        self.relay(self.conn, 'relay', 1, body_sha=SHA)

    def admit(self, receiver, decision='held', facts=None):
        return held_relays.admit(
            self.conn, 'relay', receiver, SHA,
            (held_relays.Recipient(receiver + '-a', 'team.implementer@' + receiver, 'to'),
             held_relays.Recipient(receiver + '-b', 'team.planner@' + receiver, 'cc')),
            decision, facts or held_relays.ReceiverFacts('active', False, 'present'), 1,
        )

    def held(self):
        self.admit('q')
        return held_relays.desired_asks(self.conn, {}, '')

    def previous(self, desired, *, state='pending', pins=None):
        key = desired[0].key
        item = held_relays.OpenAsk('old-item', key,
                                  desired[0].pins if pins is None else pins, state)
        ask = held_relays.AskRow(key, 0, item.item_id)
        return item, {key: ask}

    def test_desired_asks(self):
        self.admit('q')
        self.admit('r')
        self.admit('s', 'allowed')
        self.admit('t', 'denied')
        self.admit('u', 'allowed', held_relays.ReceiverFacts('active', True, 'not_read'))
        got = held_relays.desired_asks(self.conn, {'q': 7}, 'J1')
        self.assertEqual(got, tuple(held_relays.DesiredAsk(
            ('relay', receiver), SHA, rev,
            {'relay_id': 'relay', 'expected_body_sha256': SHA, 'expected_allow_revision': rev,
             'expected_restore_job': 'J1'},
        ) for receiver, rev in (('q', 7), ('r', 0))))

    def test_plan_clears_stale(self):
        desired = self.held()
        pins = dict(desired[0].pins, expected_allow_revision=2)
        stale, _ = self.previous(desired, pins=pins)
        obsolete = held_relays.OpenAsk('obsolete', ('relay', 'r'), pins, 'pending')
        self.assertEqual(held_relays.plan_clears(desired, (stale, obsolete)), (stale, obsolete))

    def test_raise_only_after_confirmed_clear(self):
        desired = self.held()
        stale, asks = self.previous(desired, pins=dict(desired[0].pins, expected_allow_revision=2))
        self.assertEqual(held_relays.plan_raises('p', desired, (stale,), {}, asks), ())
        clears = {stale.item_id: held_relays.ClearOutcome(stale.item_id, 'cleared')}
        (raise_,) = held_relays.plan_raises('p', desired, (stale,), clears, asks)
        self.assertEqual(raise_.incarnation, 1)
        self.assertNotEqual(raise_.operation_key, held_relays.ask_operation_key('p', 'relay', 'q', SHA, 0, 0))
        self.assertEqual(held_relays.plan_raises('p', (), (stale,), clears, asks), ())

    def test_deferred_clear_raises_nothing(self):
        desired = self.held()
        stale, asks = self.previous(
            desired, state='applying', pins=dict(desired[0].pins, expected_allow_revision=2),
        )
        self.assertEqual(held_relays.plan_clears(desired, (stale,)), (stale,))
        clears = {stale.item_id: held_relays.ClearOutcome(stale.item_id, 'clear_pending')}
        planned = held_relays.plan_raises('p', desired, (stale,), clears, asks)
        self.assertEqual(planned, ())
        self.assertEqual(held_relays.ask_rows_for(planned, {}, frozenset({stale.item_id})), ())
        self.assertEqual(asks[stale.key].incarnation, 0)

    def test_later_closed_outcome(self):
        desired = self.held()
        item, asks = self.previous(desired, state='applying',
                                   pins=dict(desired[0].pins, expected_allow_revision=2))
        clears = {item.item_id: held_relays.ClearOutcome(item.item_id, 'already_closed')}
        planned = held_relays.plan_raises('p', desired, (item,), clears, asks)
        self.assertEqual(len(planned), 1)
        outcome = {item.key: held_relays.RaiseOutcome('new-item', False, planned[0].operation_key)}
        self.assertEqual(held_relays.ask_rows_for(planned, outcome, frozenset({item.item_id})),
                         (held_relays.AskRow(item.key, 1, 'new-item'),))

    def test_existing_item_is_a_fault(self):
        desired = self.held()
        planned = held_relays.plan_raises('p', desired, (), {}, {})
        for item_id, op in (('old-item', planned[0].operation_key), ('new-item', 'another-operation')):
            with self.subTest(item_id=item_id):
                result = {desired[0].key: held_relays.RaiseOutcome(item_id, True, op)}
                with self.assertRaises(ValueError):
                    held_relays.ask_rows_for(planned, result, frozenset({'old-item'}))

    def keep(self, state, *, has_marker=True):
        desired = self.held()
        item, asks = self.previous(desired, state=state)
        self.assertEqual(held_relays.plan_clears(desired, (item,)), ())
        self.assertEqual(held_relays.plan_raises('p', desired, (item,), {}, asks if has_marker else {}), ())

    def test_pending_current_kept(self):
        self.keep('pending')

    def test_applying_current_kept(self):
        self.keep('applying')

    def test_plan_keeps_current(self):
        self.keep('pending', has_marker=False)

    def test_terminal_open_cleared_and_reasked(self):
        desired = self.held()
        for state in ('resolved', 'superseded', 'refused', 'faulted'):
            with self.subTest(state=state):
                item, asks = self.previous(desired, state=state)
                self.assertEqual(held_relays.plan_clears(desired, (item,)), (item,))
                clears = {item.item_id: held_relays.ClearOutcome(item.item_id, 'closed')}
                (raise_,) = held_relays.plan_raises('p', desired, (item,), clears, asks)
                self.assertEqual(raise_.incarnation, 1)

    def test_incarnation_after_closed(self):
        desired = self.held()
        item, asks = self.previous(desired)
        planned = held_relays.plan_raises('p', desired, (), {}, asks)
        result = {item.key: held_relays.RaiseOutcome('new-item', True, planned[0].operation_key)}
        self.assertEqual(held_relays.ask_rows_for(planned, result, frozenset()),
                         (held_relays.AskRow(item.key, 1, 'new-item'),))

    def test_restore_replay_witness(self):
        desired = self.held()
        (first,) = held_relays.plan_raises('p', desired, (), {}, {})
        seen = {first.key: held_relays.AskRow(first.key, first.incarnation, 'A')}
        (second,) = held_relays.plan_raises('p', desired, (), {}, seen)
        self.assertEqual((first.incarnation, second.incarnation), (0, 1))
        self.assertNotEqual(first.operation_key, second.operation_key)
        self.assertEqual(held_relays.plan_raises('p', desired, (), {}, seen), (second,))

    def test_restore_generation_reasks(self):
        before = self.held()
        item, asks = self.previous(before)
        after = held_relays.desired_asks(self.conn, {}, 'J1')
        self.assertEqual(held_relays.plan_clears(after, (item,)), (item,))
        clears = {item.item_id: held_relays.ClearOutcome(item.item_id, 'cleared')}
        (raise_,) = held_relays.plan_raises('p', after, (item,), clears, asks)
        self.assertEqual(raise_.incarnation, 1)
        self.assertEqual(raise_.pins['expected_restore_job'], 'J1')

    def test_operation_key_form(self):
        args = ('p', 'relay', 'q', SHA, 0, 0)
        key = held_relays.ask_operation_key(*args)
        self.assertRegex(key, r'^xpa2-[0-9a-f]{32}$')
        for index, value in enumerate(('new-source', 'new-relay', 'new-dest', 'b' * 64, 2, 1)):
            with self.subTest(index=index):
                self.assertNotEqual(key, held_relays.ask_operation_key(*args[:index], value, *args[index+1:]))

    def test_inbox_plan(self):
        self.admit('q', 'allowed')
        self.admit('r')
        delivered = held_relays.delivered_recipients(self.conn)
        self.assertEqual(delivered, (
            held_relays.DeliveredRecipient('relay', 'q', 'q-a', 'to', 1),
            held_relays.DeliveredRecipient('relay', 'q', 'q-b', 'cc', 1),
        ))
        existing = held_relays.InboxRow('p', 'relay', 'q', 'q-a', 'to', 1)
        stale = held_relays.InboxRow('p', 'relay', 'q', 'not-on-relay', 'to', 2)
        foreign = held_relays.InboxRow('other', 'relay', 'q', 'q-b', 'cc', 1)
        self.assertEqual(held_relays.plan_inbox('p', delivered, (existing, stale, foreign)),
                         held_relays.InboxPlan(
                             (held_relays.InboxRow('p', 'relay', 'q', 'q-b', 'cc', 1),), (stale,),
                         ))

    def test_apply_rows(self):
        cat = sqlite3.connect(':memory:')
        self.addCleanup(cat.close)
        catalogue.init_catalogue(cat)
        cat.executemany('INSERT INTO xproject_inbox VALUES (?,?,?,?,?,?)', (
            ('p', 'old', 'q', 'a', 'to', 1), ('elsewhere', 'old', 'q', 'a', 'to', 2),
        )).close()
        cat.commit()
        cat.execute('BEGIN IMMEDIATE').close()
        held_relays.apply_ask_rows(cat, 'p', (held_relays.AskRow(('new', 'q'), 2, 'answer-me'),))
        held_relays.apply_inbox(cat, 'p', held_relays.InboxPlan(
            (held_relays.InboxRow('p', 'new', 'q', 'b', 'cc', 3),),
            (held_relays.InboxRow('p', 'old', 'q', 'a', 'to', 1),),
        ))
        self.assertTrue(cat.in_transaction)
        with closing(cat.execute('SELECT * FROM xproject_asks')) as cursor:
            self.assertEqual(cursor.fetchall(), [('p', 'new', 'q', 2, 'answer-me')])
        with closing(cat.execute('SELECT source_project_id,relay_id,seat_id,role FROM xproject_inbox '
                                 'ORDER BY source_project_id')) as cursor:
            self.assertEqual(cursor.fetchall(), [('elsewhere', 'old', 'a', 'to'), ('p', 'new', 'b', 'cc')])
        cat.rollback()
        with closing(cat.execute('SELECT * FROM xproject_asks')) as cursor:
            self.assertEqual(cursor.fetchall(), [])
        with closing(cat.execute('SELECT relay_id FROM xproject_inbox ORDER BY source_project_id')) as cursor:
            self.assertEqual(cursor.fetchall(), [('old',), ('old',)])
