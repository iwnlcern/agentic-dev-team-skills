"""Durable operator answers before any owning-ledger apply."""
from dataclasses import replace
import json
import sqlite3
import unittest

from relay_engine import header
from relay_engine.api_principals import OperatorPrincipal, ServicePrincipal
from relay_engine.operator_queue import ingest, kinds, operations, records, saga
from relay_engine.tests.test_queue_ingest import QueueFixture, P


class TestQueueRecordAnswer(unittest.TestCase):
    def setUp(self):
        self.f = QueueFixture(self)
        self.c = self.f.catalogue
        self.page = OperatorPrincipal('page')
        self.panel = OperatorPrincipal('frank_panel')

    def question(self, *, subject='Question', unroutable=False, sender=None):
        q = self.f.admit(sender=sender, subject=subject, body='Question\nChoices:\n- Go exactly\n- Wait\n')
        if unroutable:
            self.f.ledger.execute('UPDATE team_instances SET closed_at=2 WHERE id=?',
                                  ((sender or self.f.sender).team_instance_id,)).close()
            self.f.ledger.commit()
        rows = self.f.rows()
        cursor = records.read_cursor(self.c, P, 'question')
        ingest.ingest_questions(self.c, rows, P, cursor, check_rewind=True, now_ms=self.f.clock)
        item_id = next(row[0] for row in self.f.items() if row[1] == q.relay_id)
        return self.view(item_id)

    def view(self, item_id):
        row = operations._dicts(self.c, 'SELECT * FROM queue_items WHERE item_id=?', (item_id,))[0]
        row['events_position'] = records._one(self.c, 'SELECT MAX(position) FROM queue_events WHERE item_id=?', (item_id,))[0]
        return records.ItemView(**row)

    def authority_item(self):
        result = records.raise_item(self.c, ServicePrincipal('authority_service'),
            records.RaiseRequest('merge_go_ahead', None, P, {'sentence': 'Allow merge'},
                {'proposal_id': 'proposal'}, {'proposal_id': 'proposal'}, 'raise-merge'),
            self.f.clock, enabled=frozenset({'pr1', 'pr2'}))
        self.assertNotIsInstance(result, records.QueueRefusal)
        return self.view(result.item_id)

    def answer(self, item, *, principal=None, revision=None, value=None, key='answer', now=25):
        return saga.record_answer(self.c, principal or self.page, item.item_id,
            item.revision if revision is None else revision,
            value if value is not None else {'choice': 'Go exactly', 'text': 'Exact text\nsecond line'},
            key, now, frank_conformed=False, top_seat_address='top.planner')

    def refused(self, item, cause, **kwargs):
        changes, before = self.c.total_changes, self.f.snapshot()
        result = self.answer(item, **kwargs)
        self.assertIsInstance(result, records.QueueRefusal)
        self.assertEqual(result.cause_kind, cause)
        self.assertEqual(self.c.total_changes, changes)
        self.assertEqual(self.f.snapshot(), before)
        return result

    def test_answer_draft_parses(self):
        item = self.question()
        # Routing follows answer_to, even if display metadata names somebody else.
        item = replace(item, actor_address='other.planner')
        data = saga.answer_draft(item, 'Go exactly', 'Exact text\nsecond line', 'top.planner')
        parsed = header.parse(data)
        self.assertEqual(parsed.kind, 'SITREP')
        self.assertEqual(parsed.to, (item.answer_to,))
        self.assertEqual(parsed.cc, ('top.planner',))
        self.assertEqual(parsed.parent, item.source_relay_id)
        self.assertEqual(parsed.needs, 'none')
        self.assertEqual(parsed.subject, 'Operator answer: Question')
        self.assertEqual(parsed.body, ('Item: '+item.item_id+'\nChoice: Go exactly\nText: Exact text\nsecond line').encode())
        same = header.parse(saga.answer_draft(item, None, 'Typed', item.answer_to))
        self.assertEqual(same.cc, ())
        self.assertNotIn(b'Choice:', same.body)
        self.assertTrue(same.body.endswith(b'Text: Typed'))

    def test_answer_subject_truncation(self):
        item = self.question(subject='Q'*398+'\u754c\U0001f642')
        parsed = header.parse(saga.answer_draft(item, None, 'Yes', None))
        self.assertEqual(len(parsed.subject), 400)
        self.assertEqual(parsed.subject, ('Operator answer: '+'Q'*398+'\u754c\U0001f642')[:400])
        self.assertEqual(parsed.cc, ())

    def test_answer_text_too_long(self):
        self.refused(self.question(), 'text_too_long', value={'text': '\u754c'*21846})

    def test_record_is_durable_before_apply(self):
        item = self.question()
        value = {'choice': 'Go exactly', 'text': '  exact\n\u754c\n'}
        result = self.answer(item, value=value, now=123)
        self.assertEqual((result['state'], result['revision']), ('applying', 2))
        self.c.commit()
        # A separate connection sees the recorded bytes and job before any apply runs.
        database = self.c.execute('PRAGMA database_list').fetchone()[2]
        with sqlite3.connect(database) as read:
            row = read.execute('SELECT answer_id,answer,answered_at_ms,apply_key,draft,phase,call_args FROM queue_answers WHERE item_id=?', (item.item_id,)).fetchone()
            store_id = read.execute('SELECT store_id FROM catalogue_identity WHERE id=1').fetchone()[0]
            state = read.execute('SELECT state,revision FROM queue_items WHERE item_id=?', (item.item_id,)).fetchone()
        read.close()
        self.assertEqual(state, ('applying', 2))
        self.assertEqual(row[0], result['answer_id'])
        self.assertEqual(json.loads(row[1]), value)
        self.assertEqual(row[2], 123)
        self.assertEqual(row[3], 'adt-queue-v1:'+store_id+':'+item.item_id+':'+row[0])
        self.assertLessEqual(len(row[3].encode()), 256)
        self.assertNotIn('\x00', row[3])
        self.assertEqual(row[4], saga.answer_draft(item, value['choice'], value['text'], 'top.planner'))
        self.assertEqual(row[5:], ('recorded', None))
        self.assertEqual(records._one(self.c, "SELECT revision,state,event_kind,principal_kind,principal_id,at_ms FROM queue_events WHERE event_kind='answered'"), (2, 'applying', 'answered', 'operator', 'operator', 123))
        self.assertIsNotNone(result['receipt'])

    def test_surface_rule(self):
        item = self.authority_item()
        self.refused(item, 'surface_not_allowed', principal=self.panel, value={'choice': 'go_ahead'})
        result = self.answer(item, value={'choice': 'go_ahead'})
        self.assertEqual(result['state'], 'applying')
        self.assertEqual(records._one(self.c, 'SELECT draft,call_args,phase FROM queue_answers'), (None, None, 'recorded'))

    def test_stale_revision_on_answer(self):
        item = self.question()
        self.answer(item)
        stale = self.refused(item, 'revision_mismatch', key='old-revision')
        self.assertEqual(stale.payload, {'current_revision': 2, 'current_state': 'applying'})
        self.refused(item, 'item_not_pending', revision=2, key='new-revision')
        fresh = self.question(subject='Another')
        self.assertEqual(self.answer(fresh, key='fresh')['state'], 'applying')

    def test_two_surfaces_race(self):
        item = self.question()
        first = self.answer(item, principal=self.panel, key='panel')
        second = self.refused(item, 'revision_mismatch', principal=self.page, key='page')
        self.assertEqual(first['state'], 'applying')
        self.assertEqual(second.payload['current_state'], 'applying')
        self.assertEqual(records._one(self.c, 'SELECT COUNT(*) FROM queue_answers'), (1,))
        self.assertEqual(records._one(self.c, "SELECT COUNT(*) FROM queue_events WHERE event_kind='answered'"), (1,))

    def test_answer_receipt_replay(self):
        item = self.question()
        first = self.answer(item)
        changes = self.c.total_changes
        again = self.answer(item, principal=self.panel)
        self.assertTrue(again['replayed'])
        self.assertEqual({k:v for k,v in first.items() if k!='replayed'}, {k:v for k,v in again.items() if k!='replayed'})
        self.assertEqual(self.c.total_changes, changes)
        refused = self.refused(item, 'operation_key_reused', value={'text': 'Different'})
        self.assertEqual(refused.code, 'E_API_CONFLICT')

    def test_fixed_choices_only(self):
        item = self.authority_item()
        self.refused(item, 'choice_not_allowed', value={'choice': 'Allow merge'})
        self.assertEqual(self.answer(item, value={'choice': 'go_ahead_without_review'})['state'], 'applying')

    def test_record_answer_unroutable(self):
        item = self.question(unroutable=True)
        self.assertIsNone(item.answer_to)
        result = self.refused(item, 'answer_unroutable')
        self.assertEqual(result.code, 'E_QUEUE_STATE')
        addressed = self.question(subject='Addressed', sender=self.f.other)
        self.assertEqual(addressed.answer_to, self.f.other.address)
        self.assertEqual(self.answer(addressed, key='addressed')['state'], 'applying')
