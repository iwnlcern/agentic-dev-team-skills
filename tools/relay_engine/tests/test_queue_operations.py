"""Operator mutations and immutable event-position paging of the machine queue."""
import json
import re
import sqlite3
import unittest
from unittest import mock

from relay_engine import store_layout
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.operator_queue import kinds, records, operations
from relay_engine.tests import test_queue_records as fixtures, test_queue_saga as saga_fixtures
from relay_engine.tests.test_catalogue import P, Q


class TestQueueOperations(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestQueueRecords()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.conn
        self.operator = OperatorPrincipal('page')

    def notice(self, key='key', **changes):
        return self.fixture.raise_it(request=self.fixture.request(key=key, **changes))

    def listing(self, *, scope='machine', state=None, kind=None, size=100, token=None):
        return operations.list_items(self.conn, scope, state, kind, size, token)

    def get(self, item_id, size=100, token=None):
        return operations.get_item(self.conn, item_id, size, token)

    def history_item(self):
        item = self.notice()
        for index in range(249):
            records.append_event(self.conn, item.item_id, 1, 'pending', 'opened',
                                 self.operator, 101 + index)
        return item

    def test_open_dismisses_with_opened_event(self):
        item = self.notice()
        result = operations.open_notice(self.conn, self.operator, item.item_id, 1, 'open', 200)
        self.assertEqual((result['revision'], result['state']), (2, 'dismissed'))
        self.assertEqual(self.conn.execute('SELECT event_kind FROM queue_events ORDER BY position').fetchall(),
                         [('raised',), ('opened',)])
        question = self.fixture.raise_it(self.fixture.internal, self.fixture.request('question', None))
        result = operations.open_notice(self.conn, self.operator, question.item_id, 1, 'question', 200)
        self.assertEqual((result.code, result.cause_kind), ('E_QUEUE_INVALID', 'not_a_notice'))

    def test_reads_change_nothing(self):
        item = self.history_item()
        before, changes = self.fixture.snapshot(), self.conn.total_changes
        transaction = self.conn.in_transaction
        for _ in range(3):
            self.listing()
            first = self.get(item.item_id)
            self.get(item.item_id, token=first['continuation'])
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(self.conn.in_transaction, transaction)

    def test_stale_revision(self):
        item = self.notice()
        self.conn.execute('UPDATE queue_items SET revision=2 WHERE item_id=?', (item.item_id,)).close()
        before = self.fixture.snapshot()
        result = operations.dismiss(self.conn, self.operator, item.item_id, 1, 'stale', 200)
        self.assertEqual((result.code, result.cause_kind), ('E_QUEUE_STATE', 'revision_mismatch'))
        self.assertEqual(result.payload, {'current_revision': 2, 'current_state': 'pending'})
        self.assertEqual(self.fixture.snapshot(), before)
        self.conn.execute("UPDATE queue_items SET state='faulted' WHERE item_id=?", (item.item_id,)).close()
        result = operations.dismiss(self.conn, self.operator, item.item_id, 1, 'terminal', 200)
        self.assertEqual(result.cause_kind, 'item_not_pending')

    def test_dismiss_all_bound_and_events(self):
        old = self.notice('old')
        question = self.fixture.raise_it(self.fixture.internal, self.fixture.request('question', None))
        bound = records.next_position(self.conn) - 1
        new = self.notice('new')
        result = operations.dismiss_all(self.conn, self.operator, 'machine', bound, 'all', 200)
        self.assertEqual(result['item_ids'], [old.item_id])
        self.assertEqual(result['count'], 1)
        self.assertFalse(result['more_remaining'])
        self.assertEqual(self.conn.execute('SELECT item_id,state FROM queue_items WHERE item_id IN (?,?) ORDER BY item_id',
                                          (question.item_id, new.item_id)).fetchall(),
                         sorted([(question.item_id, 'pending'), (new.item_id, 'pending')]))
        self.assertEqual(self.conn.execute("SELECT event_kind FROM queue_events WHERE event_kind <> 'raised'").fetchall(),
                         [('dismissed_all',)])

    def test_dismiss_all_continuation_keys(self):
        for index in range(1500):
            self.notice(str(index))
        bound = records.next_position(self.conn) - 1
        first = operations.dismiss_all(self.conn, self.operator, 'machine', bound, 'first', 200)
        self.assertEqual((first['count'], first['more_remaining']), (1000, True))
        before = self.fixture.snapshot()
        replay = operations.dismiss_all(self.conn, self.operator, 'machine', bound, 'first', 200)
        self.assertEqual(replay['receipt'], first['receipt'])
        self.assertEqual(replay['item_ids'], first['item_ids'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.fixture.snapshot(), before)
        second = operations.dismiss_all(self.conn, self.operator, 'machine', bound, 'second', 200)
        self.assertEqual((second['count'], second['more_remaining']), (500, False))
        self.assertFalse(set(first['item_ids']) & set(second['item_ids']))

    def test_list_pages_snapshot_at_h(self):
        one, two = self.notice('one'), self.notice('two')
        first = self.listing(size=1, state=['pending'])
        self.assertEqual(first['items'][0]['item_id'], one.item_id)
        operations.dismiss(self.conn, self.operator, two.item_id, 1, 'dismiss', 200)
        later = self.notice('later')
        second = self.listing(size=1, state=['pending'], token=first['continuation'])
        self.assertEqual([(r['item_id'], r['state'], r['revision']) for r in second['items']],
                         [(two.item_id, 'pending', 1)])
        self.assertEqual(second['queue_event_position'], first['queue_event_position'])
        self.assertIsNone(second['continuation'])
        current = self.listing()
        self.assertIn(later.item_id, {r['item_id'] for r in current['items']})
        self.assertEqual(next(r['state'] for r in current['items'] if r['item_id'] == two.item_id), 'dismissed')

    def test_continuation_refusals(self):
        one, two = self.notice('one'), self.notice('two')
        token = self.listing(size=1)['continuation']
        for args in ({'scope': P}, {'state': ['pending']}, {'kind': ['notice']}):
            result = self.listing(size=1, token=token, **args)
            self.assertEqual(result.cause_kind, 'stale_continuation')
        original_id = records.store_id(self.conn)
        self.conn.execute("UPDATE catalogue_identity SET store_id='another' WHERE id=1").close()
        result = self.listing(size=1, token=token)
        self.assertEqual(result.cause_kind, 'stale_continuation')
        self.conn.execute('UPDATE catalogue_identity SET store_id=? WHERE id=1', (original_id,)).close()
        history = self.get(one.item_id, size=1)
        # Give this item a second event to force an actual history continuation.
        records.append_event(self.conn, one.item_id, 1, 'pending', 'opened', self.operator, 200)
        history = self.get(one.item_id, size=1)
        result = self.get(two.item_id, size=1, token=history['continuation'])
        self.assertEqual(result.cause_kind, 'stale_continuation')
        self.assertEqual(self.listing(token='bad!').cause_kind, 'stale_continuation')

    def test_history_pages_complete(self):
        item = self.history_item()
        positions, token, pages = [], None, 0
        while True:
            page = self.get(item.item_id, token=token)
            positions.extend(row['position'] for row in page['events'])
            pages += 1
            token = page['continuation']
            if token is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(positions, list(range(1, 251)))

    def test_ingest_skips_read(self):
        self.assertEqual(operations.ingest_skips(self.conn, limit=2), ())
        for at in (10, 30, 20):
            self.conn.execute('INSERT INTO queue_ingest_skips VALUES (?,?,?,?)',
                              ('store_items', str(at), 'reason-' + str(at), at)).close()
        changes, transaction = self.conn.total_changes, self.conn.in_transaction
        expected = tuple(operations.IngestSkip('store_items', str(at), 'reason-' + str(at), at)
                         for at in (30, 20, 10))
        self.assertEqual(operations.ingest_skips(self.conn, limit=2), expected[:2])
        self.assertEqual(operations.ingest_skips(self.conn, limit=10), expected)
        self.assertEqual((self.conn.total_changes, self.conn.in_transaction), (changes, transaction))
        for limit in (0, True, -1, '2'):
            with self.assertRaises(ValueError):
                operations.ingest_skips(self.conn, limit=limit)
        self.conn.commit()
        reader = store_layout.open_catalogue_readonly(self.fixture.root)
        try:
            self.assertEqual(operations.ingest_skips(reader, limit=10), expected)
        finally:
            reader.close()

    def test_get_item_reads_answer_fields_and_job(self):
        question = self.fixture.raise_it(self.fixture.internal, self.fixture.request('question', None),
                                        fields={'report_path': 'kept'})
        self.conn.execute('INSERT INTO queue_answers '
                          '(item_id,answer_id,answer,answered_at_ms,apply_key,draft,call_args,project_id,phase) '
                          'VALUES (?,?,?,?,?,?,?,?,?)',
                          (question.item_id, 'answer', 'Exact answer.', 123, 'secret-key', b'secret-draft',
                           '{"secret":"args"}', P, 'recorded')).close()
        self.conn.execute("UPDATE queue_items SET state='applying',revision=2 WHERE item_id=?",
                          (question.item_id,)).close()
        records.append_event(self.conn, question.item_id, 2, 'applying', 'answered', self.operator, 123)
        item = self.get(question.item_id)['item']
        self.assertEqual(item['fields'], {'report_path': 'kept'})
        self.assertEqual(item['answer'], {'answer_id': 'answer', 'answer': 'Exact answer.', 'answered_at_ms': 123})
        self.assertEqual(item['job'], {'phase': 'recorded', 'last_reason': None})
        self.conn.execute("UPDATE queue_answers SET last_reason='Busy' WHERE item_id=?", (question.item_id,)).close()
        result = self.get(question.item_id)
        self.assertEqual(result['item']['job']['last_reason'], 'Busy')
        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(v) for v in value.values()), set())
            if isinstance(value, list):
                return set().union(*(keys(v) for v in value), set())
            return set()
        self.assertFalse(keys(result) & {'apply_key', 'draft', 'call_args'})
        plain = self.get(self.notice('plain').item_id)['item']
        self.assertIsNone(plain['fields'])
        self.assertIsNone(plain['answer'])
        self.assertIsNone(plain['job'])

    def test_operator_close_subkinds(self):
        request = self.fixture.request(subkind='notifier_unhealthy', project=None, key='notifier')
        for index, action in enumerate(('open', 'dismiss', 'all', 'control')):
            item = self.fixture.raise_it(self.fixture.internal, replace_request(request, operation_key='raise-' + str(index)))
            patch = ({'notifier_unhealthy'} if action != 'control' else kinds.OPERATOR_CLOSED_SUBKINDS)
            with mock.patch.object(kinds, 'OPERATOR_CLOSED_SUBKINDS', frozenset(patch)):
                if action in ('open', 'control'):
                    operations.open_notice(self.conn, self.operator, item.item_id, 1, action, 200)
                elif action == 'dismiss':
                    operations.dismiss(self.conn, self.operator, item.item_id, 1, action, 200)
                else:
                    operations.dismiss_all(self.conn, self.operator, 'machine', records.next_position(self.conn)-1, action, 200)
            events = self.conn.execute('SELECT event_kind FROM queue_events WHERE item_id=? ORDER BY position',
                                       (item.item_id,)).fetchall()
            if action != 'control':
                own = 'opened' if action == 'open' else 'dismissed' if action == 'dismiss' else 'dismissed_all'
                self.assertEqual(events[-2:], [(own,), ('condition_closed',)])
                self.assertIsNotNone(self.conn.execute('SELECT closed_at_ms FROM queue_conditions WHERE item_id=?', (item.item_id,)).fetchone()[0])
            else:
                repeat = self.fixture.raise_it(self.fixture.internal, replace_request(request, operation_key='repeat'))
                self.assertEqual(repeat.item_id, item.item_id)
                self.assertEqual(events, [('raised',), ('opened',)])

    def test_continuation_spelling(self):
        expected = [self.notice(str(index)).item_id for index in range(1250)]
        ids, token = [], None
        while True:
            result = self.listing(size=100, token=token)
            ids.extend(item['item_id'] for item in result['items'])
            token = result['continuation']
            if token is None:
                break
            self.assertRegex(token, r'^[A-Za-z0-9_-]+$')
        self.assertEqual(ids, expected)
        item = expected[0]
        for index in range(249):
            records.append_event(self.conn, item, 1, 'pending', 'opened', self.operator, index)
        positions, token = [], None
        while True:
            result = self.get(item, token=token)
            positions.extend(event['position'] for event in result['events'])
            token = result['continuation']
            if token is None:
                break
            self.assertRegex(token, r'^[A-Za-z0-9_-]+$')
        self.assertEqual(positions, [1, *range(1251, 1500)])

    def test_machine_and_project_scopes(self):
        own = self.notice('p')
        other = self.notice('q', project=Q)
        machine = self.fixture.raise_it(self.fixture.internal,
                   self.fixture.request(subkind='notifier_unhealthy', project=None, key='notifier'))
        self.assertEqual([r['item_id'] for r in self.listing(scope=P)['items']], [own.item_id])
        result = operations.dismiss_all(self.conn, self.operator, P, records.next_position(self.conn)-1, 'p', 200)
        self.assertEqual(result['item_ids'], [own.item_id])
        for item in (other, machine):
            self.assertEqual(self.conn.execute('SELECT state FROM queue_items WHERE item_id=?', (item.item_id,)).fetchone(), ('pending',))

    def test_dismiss_all_no_project_scope(self):
        from relay_engine.api_principals import ServicePrincipal

        doorbell = self.notice()
        backup = self.fixture.raise_it(ServicePrincipal('approval_seal'), self.fixture.backup_request())
        bound = records.next_position(self.conn)
        result = operations.dismiss_all(self.conn, self.operator, P, bound, 'project', 200)
        self.assertEqual(result['item_ids'], [doorbell.item_id])
        self.assertEqual(self.get(backup.item_id)['item']['state'], 'pending')
        result = operations.dismiss_all(self.conn, self.operator, 'machine', bound, 'machine', 201)
        self.assertEqual(result['item_ids'], [backup.item_id])
        self.assertEqual(self.get(backup.item_id)['item']['state'], 'dismissed')

    def test_operator_auth_and_receipt_conflict(self):
        item = self.notice()
        before = self.fixture.snapshot()
        result = operations.dismiss(self.conn, self.fixture.engine, item.item_id, 1, 'key', 200)
        self.assertEqual((result.code, result.cause_kind), ('E_API_AUTH', 'not_operator'))
        self.assertEqual(self.fixture.snapshot(), before)
        first = operations.dismiss(self.conn, self.operator, item.item_id, 1, 'key', 200)
        second = operations.dismiss(self.conn, self.operator, item.item_id, 1, 'key', 200)
        self.assertEqual(second['receipt'], first['receipt'])
        self.assertTrue(second['replayed'])
        result = operations.dismiss(self.conn, self.operator, item.item_id, 2, 'key', 200)
        self.assertEqual((result.code, result.cause_kind), ('E_API_CONFLICT', 'operation_key_reused'))


def replace_request(request, **changes):
    from dataclasses import replace
    return replace(request, **changes)


class TestQueueWithdrawal(unittest.TestCase):
    def setUp(self):
        from relay_engine.tests.test_queue_ingest import QueueFixture, Port
        from relay_engine.operator_queue import ingest
        self.f = QueueFixture(self)
        self.conn = self.f.catalogue
        self.q = self.f.admit()
        ingest.question_ingest_pass(Port(self.f), [P], self.f.clock)
        self.item = self.f.items()[0][0]

    def withdraw(self, binding=None, revision=1, key='withdraw', reason='Changed my mind'):
        return operations.withdraw(self.conn,self.f.ledger,binding or self.f.binding,
                                   self.item,revision,reason,key,200)

    def test_withdraw_own_plain_question(self):
        # Breaks asking-seat/kind guards, event detail, receipt scope, or commits.
        before=self.f.snapshot();changes=self.conn.total_changes
        result=self.withdraw(self.f.other_binding)
        self.assertEqual((result.code,result.cause_kind),('E_API_AUTH','not_asking_seat'))
        self.assertEqual(self.f.snapshot(),before);self.assertEqual(self.conn.total_changes,changes)
        first=self.withdraw()
        self.assertEqual(first['state'],'withdrawn');self.assertTrue(self.conn.in_transaction)
        event=self.conn.execute("SELECT detail FROM queue_events WHERE event_kind='withdrawn'").fetchone()
        self.assertEqual(json.loads(event[0]),{'reason_text':'Changed my mind'})
        changes=self.conn.total_changes
        replay=self.withdraw()
        self.assertEqual(replay['receipt'],first['receipt']);self.assertTrue(replay['replayed'])
        self.assertEqual(self.conn.total_changes,changes)
        self.assertEqual(self.withdraw(reason='Different').cause_kind,'operation_key_reused')
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT state FROM queue_items WHERE item_id=?',(self.item,)).fetchone(),('pending',))
        self.conn.execute("UPDATE queue_items SET kind='merge_go_ahead',answer_to=NULL,source_handle=NULL WHERE item_id=?",(self.item,)).close()
        before=self.f.snapshot();result=self.withdraw(key='kind')
        self.assertEqual((result.code,result.cause_kind),('E_API_AUTH','not_asking_seat'))
        self.assertEqual(self.f.snapshot(),before)

    def test_withdraw_after_answer_refuses(self):
        # F9's explicit applying/recorded seed; no Task 6 function is called.
        self.f.seed_answer(self.item,'applying')
        before=self.f.snapshot();changes=self.conn.total_changes
        result=self.withdraw(revision=2)
        self.assertEqual(result.cause_kind,'item_not_pending')
        self.assertEqual(self.f.snapshot(),before);self.assertEqual(self.conn.total_changes,changes)

    def test_withdraw_with_ended_binding_refuses(self):
        self.f.ledger.execute('UPDATE seat_sessions SET ended_at=3 WHERE id=?',(self.f.binding.session_id,)).close()
        self.f.ledger.commit()
        before=self.f.snapshot();changes=self.conn.total_changes
        result=self.withdraw()
        self.assertEqual((result.code,result.cause_kind),('E_API_AUTH','session_not_current'))
        self.assertEqual(self.f.snapshot(),before);self.assertEqual(self.conn.total_changes,changes)


class TestQueueHistorySnapshot(unittest.TestCase):
    def setUp(self):
        self.answers = saga_fixtures.TestQueueRecordAnswer()
        self.answers.setUp()
        self.addCleanup(self.answers.doCleanups)
        self.conn = self.answers.c
        self.item = self.answers.question()
        self.answers.answer(self.item, value={'text': 'Yes'})

    def test_get_history_coherent_across_confirm(self):
        # Current answer metadata would mix a confirmed job with H=2.
        from relay_engine.operator_queue import saga
        first = operations.get_item(self.conn, self.item.item_id, 1, None)
        self.assertEqual(first['queue_event_position'], 2)
        self.assertIsNotNone(first['continuation'])
        saga.confirm(self.conn, self.item.item_id,
                     saga.ApplyResult('applied', 'receipt', None, None, None), 500)
        page = operations.get_item(self.conn, self.item.item_id, 1, first['continuation'])
        self.assertEqual((page['item']['state'], page['item']['revision']), ('applying', 2))
        self.assertEqual(page['item']['answer']['answer'], '{"text":"Yes"}')
        self.assertEqual(page['item']['job']['phase'], 'recorded')
        self.assertIsNone(page['item']['outcome'])
        self.assertEqual(page['queue_event_position'], 2)

    def test_get_last_reason_fixed_by_first_page(self):
        # Omitting the carried reason would expose the later transient result.
        from relay_engine.operator_queue import saga
        saga.confirm(self.conn, self.item.item_id,
                     saga.ApplyResult('transient', None, None, None, 'busy'), 500)
        first = operations.get_item(self.conn, self.item.item_id, 1, None)
        self.assertEqual(first['item']['job']['last_reason'], 'busy')
        self.assertIsNotNone(first['continuation'])
        saga.confirm(self.conn, self.item.item_id,
                     saga.ApplyResult('transient', None, None, None, 'closed'), 501)
        page = operations.get_item(self.conn, self.item.item_id, 1, first['continuation'])
        self.assertEqual(page['item']['job']['last_reason'], 'busy')


if __name__ == '__main__':
    unittest.main()
