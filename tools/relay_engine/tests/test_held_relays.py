"""Store's held-relay records and local transition rules."""
import ast
from contextlib import closing
from pathlib import Path
import sqlite3

from relay_engine import catalogue, schema
import relay_engine.held_relays as held_relays
from relay_engine.tests import test_schema


SHA = 'a' * 64
LEDGER = {'xproject_transfers', 'xproject_recipients', 'xproject_deliveries', 'xproject_events', 'xproject_notices'}
CATALOGUE = {'xproject_inbox', 'xproject_asks'}


class TestHeldTables(test_schema.Store):
    def setUp(self):
        self.conn = self.connect()
        self.relay(self.conn, 'relay', 1, body_sha=SHA)

    def transfer(self, receiver='q', state='held', reason=None, digest=SHA):
        self.conn.execute(
            'INSERT INTO xproject_transfers '
            '(relay_id,receiver_project_id,body_sha256,state,reason,created_at,changed_at,admit_seq) '
            'VALUES (?,?,?,?,?,1,1,(SELECT COALESCE(MAX(admit_seq), 0) + 1 FROM xproject_transfers))', ('relay', receiver, digest, state, reason)).close()

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

    def admit(self, receiver='q', decision='held', facts=None, now=1, *, relay_id='relay'):
        if facts is None:
            facts = held_relays.ReceiverFacts('active', False, 'present')
        return held_relays.admit(self.conn, relay_id, receiver, SHA, (
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

    def test_admit_seq_orders_admissions(self):
        self.assertEqual(held_relays.horizon(self.conn), 0)
        for receiver, now in (('q', 20), ('r', 20), ('s', 10)):
            self.admit(receiver, now=now)
        self.assertEqual(self.select(
            'SELECT receiver_project_id,created_at,admit_seq FROM xproject_transfers '
            'ORDER BY admit_seq'), [('q', 20, 1), ('r', 20, 2), ('s', 10, 3)])
        self.assertEqual(held_relays.horizon(self.conn), 3)

    def test_admit_seq_counts_terminal_rows(self):
        self.admit()
        facts = held_relays.ReceiverFacts('active', False, 'present')
        self.assertEqual(held_relays.step(self.conn, 'relay', 'q', 'allowed', facts, 2),
                         held_relays.State('delivered', None))
        self.assertEqual(held_relays.horizon(self.conn), 1)
        self.admit('r')
        self.assertEqual(self.select(
            'SELECT state,admit_seq FROM xproject_transfers ORDER BY admit_seq'),
            [('delivered', 1), ('held', 2)])

    def test_non_terminal_page_fence(self):
        for i in range(2, 9):
            self.relay(self.conn, f'r{i}', i, body_sha=SHA)
        self.admit(now=20)
        self.admit(decision='allowed',
                   facts=held_relays.ReceiverFacts('active', True, 'not_read'),
                   now=10, relay_id='r2')
        self.admit(decision='allowed', relay_id='r3')
        self.admit(decision='denied', relay_id='r4')
        self.admit(facts=held_relays.ReceiverFacts('not_active', False, 'not_read'),
                   relay_id='r5')
        self.admit('r', relay_id='r6')
        through = held_relays.horizon(self.conn)
        self.admit(now=20, relay_id='r7')
        self.admit(now=5, relay_id='r8')

        page = held_relays.non_terminal_page
        expected = (held_relays.TransferRow('relay', 'q', 20, 1),
                    held_relays.TransferRow('r2', 'q', 10, 2))
        self.assertEqual(page(self.conn, 'q', None, 10, through=through), expected)
        self.assertEqual(page(self.conn, 'q', None, 1, through=through), expected[:1])
        self.assertEqual(page(self.conn, 'q', 1, 1, through=through), expected[1:])
        self.assertEqual(page(self.conn, 'q', 2, 10, through=through), ())

    def test_pending_receivers_fence(self):
        cat = self.connect(init=False)
        catalogue.init_catalogue(cat)
        with closing(cat.execute('SELECT count(*) FROM project_routes')) as cursor:
            self.assertEqual(cursor.fetchone(), (0,))
        self.relay(self.conn, 'r2', 2, body_sha=SHA)
        for receiver in ('z', 'no-route', 'a'):
            self.admit(receiver)
        self.admit('a', relay_id='r2')
        self.admit('terminal', decision='denied')
        through = held_relays.horizon(self.conn)
        self.admit('after-fence')

        page = held_relays.pending_receivers
        self.assertEqual(page(self.conn, None, 10, through=through), ('a', 'no-route', 'z'))
        self.assertEqual(page(self.conn, None, 1, through=through), ('a',))
        self.assertEqual(page(self.conn, 'a', 1, through=through), ('no-route',))
        self.assertEqual(page(self.conn, 'no-route', 10, through=through), ('z',))

    def test_selectors_refuse_bad_bounds(self):
        self.relay(self.conn, 'r2', 2, body_sha=SHA)
        self.admit()
        self.admit(relay_id='r2')
        closed = self.connect()
        closed.close()
        selectors = (
            ('non_terminal_page', ('q', None)),
            ('pending_receivers', (None,)),
        )
        for name, args in selectors:
            selector = getattr(held_relays, name)
            for label, conn in (('open', self.conn), ('closed', closed)):
                for limit, through in ((0, 2), (1, -1)):
                    with self.subTest(selector=name, connection=label, limit=limit, through=through):
                        try:
                            with self.assertRaises(ValueError):
                                selector(conn, *args, limit, through=through)
                        except sqlite3.ProgrammingError:
                            if label != 'closed':
                                raise
                            self.fail('bounds check ran after a read')

    def test_notice_receipt_append_only(self):
        self.relay(self.conn, 'notice-1', 2, body_sha=SHA)
        self.relay(self.conn, 'notice-2', 3, body_sha=SHA)
        self.admit()
        self.admit('r')
        events = self.select('SELECT event_id FROM xproject_events ORDER BY event_id')
        (first,), (second,) = events
        insert = ('INSERT INTO xproject_notices (event_id,notice_relay_id,filed_at) '
                  'VALUES (?,?,?)')
        self.conn.execute(insert, (first, 'notice-1', 4)).close()
        for row in ((first, 'notice-2', 5), (second, 'notice-1', 5)):
            with self.subTest(row=row), self.assertRaisesRegex(sqlite3.IntegrityError, 'UNIQUE'):
                self.conn.execute(insert, row).close()
        for stmt in ('UPDATE xproject_notices SET filed_at=9', 'DELETE FROM xproject_notices'):
            self.conn.execute('SAVEPOINT notice_receipt_probe').close()
            try:
                with self.subTest(sql=stmt), self.assertRaisesRegex(
                        sqlite3.IntegrityError, 'notice receipt is append-only'):
                    self.conn.execute(stmt).close()
            finally:
                self.conn.execute('ROLLBACK TO notice_receipt_probe').close()
                self.conn.execute('RELEASE notice_receipt_probe').close()
        self.assertEqual(self.select('SELECT event_id,notice_relay_id,filed_at FROM xproject_notices'),
                         [(first, 'notice-1', 4)])

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

    def test_inbox_plan_rewrites_changed_row(self):
        existing = held_relays.InboxRow('p', 'relay', 'q', 'q-a', 'to', 1)
        other = held_relays.InboxRow('other', 'relay', 'q', 'q-a', 'to', 1)
        cases = (('time only', 'to', 2, True), ('role only', 'cc', 1, True),
                 ('equal', 'to', 1, False))
        for case, role, delivered_at, changed in cases:
            with self.subTest(case=case):
                recipient = held_relays.DeliveredRecipient('relay', 'q', 'q-a', role, delivered_at)
                desired = held_relays.InboxRow('p', 'relay', 'q', 'q-a', role, delivered_at)
                plan = held_relays.plan_inbox('p', (recipient,), (existing, other))
                self.assertEqual(plan.deletes, (existing,) if changed else ())
                self.assertEqual(plan.inserts, (desired,) if changed else ())
                self.assertNotIn(other, plan.deletes)
                self.assertNotIn(other, plan.inserts)

    def test_inbox_seq_rises_strictly(self):
        for case in ('highest deleted', 'all deleted', 'unique key'):
            with self.subTest(case=case), closing(sqlite3.connect(':memory:')) as cat:
                catalogue.init_catalogue(cat)

                def insert(relay_id):
                    cat.execute(
                        'INSERT INTO xproject_inbox '
                        '(source_project_id, relay_id, receiver_project_id, seat_id, role, delivered_at) '
                        'VALUES (?, ?, ?, ?, ?, ?)',
                        ('p', relay_id, 'q', 'q-a', 'to', 1),
                    ).close()

                def sequence(relay_id):
                    with closing(cat.execute(
                        'SELECT inbox_seq FROM xproject_inbox WHERE relay_id=?', (relay_id,),
                    )) as cursor:
                        return cursor.fetchone()[0]

                for relay_id in ('first', 'second', 'highest'):
                    insert(relay_id)
                first, second, highest = map(sequence, ('first', 'second', 'highest'))
                self.assertGreater(second, first)
                self.assertGreater(highest, second)
                if case == 'highest deleted':
                    cat.execute("DELETE FROM xproject_inbox WHERE relay_id='highest'").close()
                    insert('next')
                    self.assertGreater(sequence('next'), highest)
                elif case == 'all deleted':
                    cat.execute('DELETE FROM xproject_inbox').close()
                    insert('next')
                    self.assertGreater(sequence('next'), highest)
                else:
                    with self.assertRaises(sqlite3.IntegrityError):
                        insert('first')

    def test_inbox_row_fixed(self):
        cat = sqlite3.connect(':memory:')
        self.addCleanup(cat.close)
        catalogue.init_catalogue(cat)
        columns = ('source_project_id', 'relay_id', 'receiver_project_id', 'seat_id', 'role', 'delivered_at')
        original = ('p', 'relay', 'q', 'q-a', 'to', 1)
        cat.execute(
            'INSERT INTO xproject_inbox (' + ', '.join(columns) + ') VALUES (?, ?, ?, ?, ?, ?)',
            original,
        ).close()
        for column in (*columns, 'inbox_seq'):
            with self.subTest(column=column):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'inbox row is insert-or-delete only'):
                    cat.execute(f'UPDATE xproject_inbox SET {column}={column}').close()
                with closing(cat.execute(
                    'SELECT source_project_id, relay_id, receiver_project_id, seat_id, role, delivered_at, '
                    'inbox_seq FROM xproject_inbox'
                )) as cursor:
                    self.assertEqual(cursor.fetchall(), [(*original, 1)])

    def test_inbox_listing_index(self):
        cat = sqlite3.connect(':memory:')
        self.addCleanup(cat.close)
        catalogue.init_catalogue(cat)
        with closing(cat.execute("PRAGMA index_info('xproject_inbox_listing')")) as cursor:
            self.assertEqual([row[2] for row in cursor], ['receiver_project_id', 'seat_id', 'inbox_seq'])

    def test_inbox_restore_redelivery(self):
        cat = sqlite3.connect(':memory:')
        self.addCleanup(cat.close)
        catalogue.init_catalogue(cat)
        t1 = ('p', 'relay', 'q', 'q-a', 'to', 1)
        t2 = ('p', 'relay', 'q', 'q-a', 'to', 2)
        other = ('other', 'relay', 'q', 'q-a', 'to', 1)
        with cat:
            cat.executemany('INSERT INTO xproject_inbox '
                            '(source_project_id, relay_id, receiver_project_id, seat_id, role, delivered_at) '
                            'VALUES (?,?,?,?,?,?)', (t1, other)).close()
        def sequences():
            with closing(cat.execute('SELECT source_project_id, inbox_seq FROM xproject_inbox')) as cursor:
                return dict(cursor.fetchall())

        with cat:
            cat.execute('BEGIN IMMEDIATE').close()
            before_seq = sequences()
            with closing(cat.execute(
                'SELECT source_project_id, relay_id, receiver_project_id, seat_id, role, delivered_at '
                'FROM xproject_inbox ORDER BY source_project_id'
            )) as cursor:
                before = cursor.fetchall()
            plan = held_relays.plan_inbox(
                'p', (held_relays.DeliveredRecipient(*t2[1:]),),
                tuple(held_relays.InboxRow(*row) for row in before),
            )
            self.assertEqual(plan.deletes, (held_relays.InboxRow(*t1),))
            self.assertEqual(plan.inserts, (held_relays.InboxRow(*t2),))
            held_relays.apply_inbox(cat, 'p', plan)
            self.assertTrue(cat.in_transaction)
        with closing(cat.execute(
            'SELECT source_project_id, relay_id, receiver_project_id, seat_id, role, delivered_at '
            'FROM xproject_inbox ORDER BY source_project_id'
        )) as cursor:
            after = cursor.fetchall()
        self.assertEqual(after, [other, t2])
        self.assertEqual(len(after), len(before))
        after_seq = sequences()
        self.assertGreater(after_seq['p'], before_seq['p'])
        self.assertEqual(after_seq['other'], before_seq['other'])

    def test_apply_rows(self):
        cat = sqlite3.connect(':memory:')
        self.addCleanup(cat.close)
        catalogue.init_catalogue(cat)
        cat.executemany('INSERT INTO xproject_inbox '
                            '(source_project_id, relay_id, receiver_project_id, seat_id, role, delivered_at) '
                            'VALUES (?,?,?,?,?,?)', (
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
