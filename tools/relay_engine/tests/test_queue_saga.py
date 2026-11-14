"""Durable operator answers before any owning-ledger apply."""
from dataclasses import replace
import json
import sqlite3
import unittest

from relay_engine import header
from relay_engine.api_principals import OperatorPrincipal, ServicePrincipal
from relay_engine.operator_queue import ingest, kinds, operations, records, saga
from relay_engine.tests.test_queue_ingest import QueueFixture, Port, P


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
        ingest.question_ingest_pass(Port(self.f), [P], self.f.clock)
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
        # A byte cut drops the multibyte suffix instead of preserving two code points.
        item = self.question(subject='Q' * 381 + '界\U0001f642界')
        parsed = header.parse(saga.answer_draft(item, None, 'Yes', None))
        self.assertEqual(parsed.subject, 'Operator answer: ' + 'Q' * 381 + '界\U0001f642')
        self.assertEqual(len(parsed.subject), 400)
        # Cutting after whitespace must still produce a parseable SUBJECT.
        item = self.question(subject='Q' * 382 + ' ' + 'R' * 17)
        parsed = header.parse(saga.answer_draft(item, None, 'Yes', None))
        self.assertEqual(parsed.subject, 'Operator answer: ' + 'Q' * 382)

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


class ApplyPort:
    """Fresh callee connections, with actual catalogue writer boundaries."""
    def __init__(self, test):
        self.test = test
        self.f = test.f
        self.jobs = []
        self.active = None
        self.unavailable = set()
        self.sql = []

    def read_catalogue(self, fn):
        return fn(self.f.catalogue)

    def catalogue(self, fn):
        self.f.catalogue.commit()
        self.f.catalogue.execute('BEGIN IMMEDIATE').close()
        try:
            value = fn(self.f.catalogue)
            self.f.catalogue.commit()
            return value
        except BaseException:
            self.f.catalogue.rollback()
            raise

    def read_ledger(self, project, fn):
        if project in self.unavailable:
            raise records.JobUnavailable('ledger_unavailable')
        conn = sqlite3.connect(self.f.path, isolation_level=None, timeout=0.01)
        conn.execute('PRAGMA foreign_keys=ON').close()
        try:
            return fn(conn)
        finally:
            conn.close()

    def callee_ledger(self, project, fn):
        if project in self.unavailable:
            raise records.JobUnavailable('ledger_unavailable')
        conn = sqlite3.connect(self.f.path, isolation_level=None, timeout=0.01)
        conn.execute('PRAGMA foreign_keys=ON').close()
        conn.set_trace_callback(lambda statement: self.sql.append(statement))
        job = len(self.jobs)
        self.jobs.append((project, conn))
        self.active = job
        try:
            return fn(conn)
        finally:
            self.active = None
            conn.close()


class TestQueueApply(unittest.TestCase):
    view = TestQueueRecordAnswer.view

    def question(self, *, sender=None):
        project = (sender or self.f.sender).project_id
        q = self.f.admit(sender=sender)
        ingest.question_ingest_pass(Port(self.f), [project], self.f.clock)
        item_id = next(row[0] for row in self.f.items() if row[1] == q.relay_id)
        return self.view(item_id)

    def answer(self, item, *, key):
        return saga.record_answer(self.c, self.page, item.item_id, item.revision,
            {'text': 'Answer exactly'}, key, 25, frank_conformed=False,
            top_seat_address='v3.orchestrator-planner')

    def setUp(self):
        TestQueueRecordAnswer.setUp(self)
        self.port = ApplyPort(self)
        self.runtime = saga.QueueRuntime(self.make_context)

    def make_context(self, conn, project, sender, key):
        from relay_engine import submit
        from relay_engine.tests import test_submit as engine
        directory = engine.FakeDirectory()
        directory.resolve = lambda address, origin: replace(engine.SEATS[address], project_id=origin.project_id)
        state = engine.GIT_STATE if project == P else replace(engine.GIT_STATE, repository_id='repo-q')
        return submit.SubmitContext(sender, state, lambda: self.f.clock,
                                     directory, submission_key=key)

    def recorded(self, *, sender=None):
        item = self.question(sender=sender)
        self.answer(item, key='answer-'+item.item_id)
        self.c.commit()
        return item

    def job(self, item):
        return saga.load_apply_job(self.c, item.item_id)

    def apply(self, item):
        job = self.job(item)
        return self.port.callee_ledger(job.project_id, lambda conn: saga.apply_question(conn, job, self.make_context))

    def reconcile(self):
        return saga.reconcile_pass(self.port, self.runtime, lambda: 500)

    def relay_count(self):
        return records._one(self.f.ledger, "SELECT COUNT(*) FROM relays WHERE sender_special='operator'")[0]

    def outcome(self, item):
        return records._one(self.c, 'SELECT state,revision FROM queue_items WHERE item_id=?', (item.item_id,))

    def close_reopen(self):
        team = self.f.sender.team_instance_id
        row = operations._dicts(self.f.ledger, 'SELECT * FROM team_instances WHERE id=?', (team,))[0]
        self.f.ledger.execute('UPDATE team_instances SET closed_at=2 WHERE id=?', (team,)).close()
        row.update(id=team+'-successor', opened_at=3, closed_at=None)
        self.f.ledger.execute('INSERT INTO team_instances ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', tuple(row.values())).close()
        self.f.ledger.commit()

    def change_draft(self, item, data):
        self.c.execute('UPDATE queue_answers SET draft=? WHERE item_id=?', (data, item.item_id)).close()
        self.c.commit()

    def test_apply_admits_one_answer(self):
        item = self.recorded()
        result = self.apply(item)
        self.assertEqual(result.status, 'applied')
        self.assertEqual(records._one(self.f.ledger, 'SELECT parent_id,sender_special FROM relays WHERE id=?', (result.receipt,)), (item.source_relay_id, 'operator'))
        from relay_engine import queries
        self.assertTrue(queries.operator_questions(self.f.ledger, P, item.source_handle-1, 1)[0].answered)
        self.assertEqual(records._one(self.f.ledger, "SELECT COUNT(*) FROM seat_activity WHERE seat_id='operator'"), (0,))
        self.assertEqual(self.outcome(item), ('applying', 2))

    def test_crash_after_record(self):
        item = self.recorded()
        draft = self.job(item).draft
        self.assertFalse(self.runtime.ready)
        from relay_engine import submit
        from unittest import mock
        with mock.patch.object(submit, 'submit', wraps=submit.submit) as actual:
            self.reconcile()
        self.assertTrue(self.runtime.ready)
        self.assertEqual(self.outcome(item), ('resolved', 3))
        self.assertEqual(self.relay_count(), 1)
        self.assertEqual(actual.call_args.args[2], draft)

    def test_crash_after_apply(self):
        item = self.recorded()
        first = self.apply(item)
        self.assertEqual(self.outcome(item), ('applying', 2))
        self.reconcile()
        self.assertEqual(self.outcome(item), ('resolved', 3))
        self.assertEqual(self.relay_count(), 1)
        self.assertEqual(json.loads(records._one(self.c, 'SELECT outcome FROM queue_answers')[0])['receipt'], first.receipt)

    def test_refused_admission_is_final(self):
        from relay_engine import submit
        from unittest import mock
        item = self.recorded()
        self.change_draft(item, b'KIND: SITREP\n')
        self.reconcile()
        self.assertEqual(self.outcome(item)[0], 'refused')
        code = json.loads(records._one(self.c, "SELECT detail FROM queue_events WHERE event_kind='refused'")[0])['code']
        self.assertEqual(code, 'E-FIELD-REQUIRED')
        with mock.patch.object(submit, 'submit', side_effect=AssertionError('terminal job retried')):
            changes = self.c.total_changes
            self.reconcile()
            self.assertEqual(self.c.total_changes, changes)
        other = self.recorded(sender=self.f.other)
        blocker = sqlite3.connect(self.f.path, timeout=0.01)
        blocker.execute('BEGIN IMMEDIATE').close()
        try:
            self.reconcile()
            self.assertEqual(self.outcome(other)[0], 'applying')
            self.assertIn('locked', records._one(self.c, 'SELECT last_reason FROM queue_answers WHERE item_id=?', (other.item_id,))[0])
        finally:
            blocker.rollback();blocker.close()
        self.reconcile()
        self.assertEqual(self.outcome(other)[0], 'resolved')

    def test_answer_to_successor_refused(self):
        item = self.recorded()
        self.close_reopen()
        self.reconcile()
        self.assertEqual(self.outcome(item)[0], 'refused')
        self.assertEqual(self.relay_count(), 0)
        self.assertEqual(json.loads(records._one(self.c, "SELECT detail FROM queue_events WHERE event_kind='refused'")[0])['code'], 'E_QUEUE_STATE answer_unroutable')
        current = self.recorded(sender=self.f.other)
        self.reconcile()
        self.assertEqual(self.outcome(current)[0], 'resolved')
        self.assertEqual(records._one(self.f.ledger, "SELECT seat_id FROM recipients JOIN relays ON relay_id=relays.id WHERE sender_special='operator' AND role='to'"), (self.f.other.seat_id,))

    def test_admitted_answer_survives_close(self):
        item = self.recorded()
        first = self.apply(item)
        self.close_reopen()
        self.reconcile()
        self.assertEqual(self.outcome(item)[0], 'resolved')
        self.assertEqual(self.relay_count(), 1)
        self.assertEqual(json.loads(records._one(self.c, 'SELECT outcome FROM queue_answers')[0])['receipt'], first.receipt)

    def test_admission_with_other_bytes_is_faulted_once(self):
        from relay_engine import queries, submit
        from unittest import mock
        item = self.recorded()
        original = self.apply(item)
        self.assertEqual(self.apply(item).receipt, original.receipt)
        self.change_draft(item, self.job(item).draft+b'changed')
        self.reconcile()
        self.assertEqual(self.outcome(item), ('faulted', 3))
        self.assertEqual(records._one(self.c, 'SELECT phase,last_reason FROM queue_answers'), ('faulted', 'E-SUBMISSION-KEY-REUSED'))
        self.assertEqual(records._one(self.c, "SELECT COUNT(*) FROM queue_events WHERE event_kind='faulted'"), (1,))
        self.assertEqual(self.relay_count(), 1)
        changes = self.c.total_changes
        with mock.patch.object(queries, 'operator_admission', side_effect=AssertionError('fault retried')), mock.patch.object(submit, 'submit', side_effect=AssertionError('fault resubmitted')):
            self.reconcile();self.reconcile()
        self.assertEqual(self.c.total_changes, changes)
        other = self.recorded(sender=self.f.other)
        self.port.unavailable.add(P)
        self.reconcile()
        self.assertEqual(records._one(self.c, 'SELECT phase FROM queue_answers WHERE item_id=?', (other.item_id,)), ('recorded',))
        self.port.unavailable.clear();self.reconcile()
        self.assertEqual(self.outcome(other)[0], 'resolved')

    def test_undo_runs_past_faulted_item(self):
        item = self.recorded()
        notice = records.raise_item(self.c, records.InternalWriter(), records.RaiseRequest('notice', 'seat_registered', P, {'sentence': 'Registered'}, {}, 'seat', 'notice'), 100, enabled=frozenset({'pr3'}))
        self.assertNotIsInstance(notice, records.QueueRefusal)
        changes = self.c.total_changes
        with self.assertRaises(records.UndoBlocked) as caught:
            records.undo_step(self.c, P, 'blocked', now_ms=400)
        self.assertIn(item.item_id, [row.item_id for row in caught.exception.items])
        self.assertEqual(self.c.total_changes, changes)
        saga.confirm(self.c, item.item_id, saga.ApplyResult('faulted', None, None, 'E-SUBMISSION-KEY-REUSED', None), 450)
        records.undo_step(self.c, P, 'undo', now_ms=500)
        self.assertEqual(self.outcome(notice)[0], 'retired')

    def test_faulted_confirm_closes_flagged_condition(self):
        for flagged in (True, False):
            with self.subTest(flagged=flagged):
                item = self.recorded()
                if flagged:
                    records.clear_condition(self.c, records.InternalWriter(), item.item_id, 'question', None, P,
                        json.loads(item.condition_key), now_ms=400)
                result = saga.ApplyResult('faulted', None, None, 'E-SUBMISSION-KEY-REUSED', None)
                saga.confirm(self.c, item.item_id, result, 500)
                self.assertEqual(self.outcome(item)[0], 'faulted')
                self.assertEqual(records._one(self.c, "SELECT COUNT(*) FROM queue_events WHERE item_id=? AND event_kind='condition_closed'", (item.item_id,)), (int(flagged),))
                changes = self.c.total_changes;saga.confirm(self.c, item.item_id, result, 501)
                self.assertEqual(self.c.total_changes, changes)

    def test_reread_in_submit_job(self):
        from relay_engine import queries, submit
        from unittest import mock
        item = self.recorded()
        events = []
        read, write = queries.operator_questions, submit.submit
        def spy_read(conn, *args):
            events.append(('read', conn, self.port.active, len(self.port.sql)))
            return read(conn, *args)
        def spy_submit(conn, *args):
            events.append(('submit', conn, self.port.active, len(self.port.sql)))
            return write(conn, *args)
        with mock.patch.object(queries, 'operator_questions', spy_read), mock.patch.object(submit, 'submit', spy_submit):
            self.reconcile()
        self.assertEqual([e[0] for e in events], ['read', 'submit'])
        self.assertIs(events[0][1], events[1][1])
        self.assertIsNotNone(events[0][2])
        self.assertEqual(events[0][2], events[1][2])
        between = self.port.sql[events[0][3]:events[1][3]]
        self.assertFalse(any(sql.strip().upper().startswith(('COMMIT', 'ROLLBACK')) for sql in between))
        self.assertEqual(self.outcome(item)[0], 'resolved')

    def test_reread_moved_question_refused(self):
        self.f.admit('SITREP');low = self.f.backup('before-question.sqlite3')
        item = self.recorded()
        self.f.restore(low);self.f.admit('SITREP')
        result = self.apply(item)
        self.assertEqual((result.status, result.code), ('refused', 'E_QUEUE_STATE answer_unroutable'))
        self.assertEqual(self.relay_count(), 0)
        current = self.recorded()
        self.assertEqual(self.apply(current).status, 'applied')
        self.assertEqual(self.relay_count(), 1)

    def test_refusal_lost_before_confirm(self):
        for remove_cause in (False, True):
            with self.subTest(remove_cause=remove_cause):
                item = self.recorded()
                valid = self.job(item).draft
                self.change_draft(item, b'KIND: SITREP\n')
                self.assertEqual(self.apply(item).status, 'refused')
                self.assertEqual(records._one(self.c, 'SELECT phase FROM queue_answers WHERE item_id=?', (item.item_id,)), ('recorded',))
                if remove_cause:
                    self.change_draft(item, valid)
                self.reconcile()
                self.assertEqual(self.outcome(item)[0], 'resolved' if remove_cause else 'refused')
                self.assertEqual(self.relay_count(), int(remove_cause))

    def test_callee_owns_transaction(self):
        item = self.recorded();job = self.job(item)
        def call(conn):
            self.assertFalse(conn.in_transaction)
            result = saga.apply_question(conn, job, self.make_context)
            self.assertFalse(conn.in_transaction)
            return result
        self.assertEqual(self.port.callee_ledger(P, call).status, 'applied')
        def prohibited(conn):
            conn.execute('BEGIN IMMEDIATE').close()
            try:
                with self.assertRaises((AssertionError, ValueError)):
                    saga.apply_question(conn, job, self.make_context)
            finally:
                conn.rollback()
        self.port.callee_ledger(P, prohibited)
        self.assertEqual(self.relay_count(), 1)

    def test_confirm_closes_flagged_condition_once(self):
        item = self.recorded()
        records.clear_condition(self.c, records.InternalWriter(), item.item_id, 'question', None, P,
            json.loads(item.condition_key), now_ms=400)
        result = self.apply(item);saga.confirm(self.c, item.item_id, result, 500)
        self.assertEqual(self.outcome(item)[0], 'resolved')
        self.assertIsNotNone(records._one(self.c, 'SELECT closed_at_ms FROM queue_conditions WHERE item_id=?', (item.item_id,))[0])
        self.assertEqual(records._one(self.c, "SELECT COUNT(*) FROM queue_events WHERE item_id=? AND event_kind='condition_closed'", (item.item_id,)), (1,))
        changes = self.c.total_changes;saga.confirm(self.c, item.item_id, result, 501)
        self.assertEqual(self.c.total_changes, changes)

    def test_reconciler_ready_despite_one_unavailable_ledger(self):
        from relay_engine.tests.test_queue_ingest import Q
        a = self.recorded()
        b = self.recorded(sender=self.f.foreign)
        self.port.unavailable.add(Q)
        self.reconcile()
        self.assertTrue(self.runtime.ready)
        self.assertEqual(self.outcome(a)[0], 'resolved')
        self.assertEqual(self.outcome(b)[0], 'applying')
        self.assertEqual(records._one(self.c, 'SELECT last_reason FROM queue_answers WHERE item_id=?', (b.item_id,)), ('ledger_unavailable',))

    def test_confirm_records_internal_writer(self):
        item = self.recorded();self.reconcile()
        self.assertEqual(records._one(self.c, "SELECT principal_kind,principal_id,at_ms FROM queue_events WHERE item_id=? AND event_kind='applied'", (item.item_id,)), ('service', 'console_queue', 500))
        self.assertEqual(records._one(self.c, 'SELECT state,next_attempt_at_ms FROM queue_notifications WHERE item_id=? AND revision=3', (item.item_id,)), ('pending', 500))
