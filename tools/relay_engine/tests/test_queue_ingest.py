"""Native question ingestion, anchored put-backs and caller-owned transactions."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import api_sessions, schema, store_layout, submit
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.api_protocol import SeatBinding
from relay_engine.operator_queue import ingest, kinds, operations, records
from relay_engine.tests import test_submit as engine
from relay_engine.tests.test_catalogue import P, Q


class QueueFixture:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalogue = store_layout.open_catalogue(self.root / 'store')
        test.addCleanup(self.catalogue.close)
        for project, name in ((P, 'p'), (Q, 'q')):
            self.catalogue.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?,?,?,0)',
                                   (project, name, 'active')).close()
        self.catalogue.commit()
        self.path = self.root / 'ledger.sqlite3'
        self.ledger = sqlite3.connect(self.path)
        schema.init_v3(self.ledger)
        engine.platform_rows(self.ledger)
        for table in ('projects', 'team_instances', 'repositories'):
            column = 'id' if table == 'projects' else 'project_id'
            for old, new in (('p', P), ('q', Q)):
                self.ledger.execute('UPDATE '+table+' SET '+column+'=? WHERE '+column+'=?', (new, old)).close()
        self.ledger.commit()
        self.ledger.execute('PRAGMA foreign_keys=ON').close()
        test.addCleanup(lambda: self.ledger.close())
        self.sender = replace(engine.PLANNER, project_id=P)
        self.other = replace(engine.OTHER, project_id=P)
        self.foreign = replace(engine.FOREIGN, project_id=Q)
        self.operator = OperatorPrincipal('page')
        self.clock = 1700000000000
        self.sequence = 0
        self.reads = []
        self.before_catalogue = None
        self.unavailable = set()
        self.binding = self.session(self.sender)
        self.other_binding = self.session(self.other)

    def session(self, sender):
        name = 'session-' + sender.seat_id
        self.ledger.execute('INSERT INTO seat_sessions '
            '(id,seat_id,occupant_id,host,host_session_id,generation,signed_in_at,last_seen_at) '
            'VALUES (?,?,?, ?,?,1,1,1)',
            (name, sender.seat_id, 'o-'+sender.seat_id, 'codex', name)).close()
        self.ledger.commit()
        return SeatBinding(sender.project_id, sender.team_instance_id, sender.seat_id,
                           'o-'+sender.seat_id, 'k-'+sender.seat_id, name, 1)

    def admit(self, kind='QUESTION', *, sender=None, parent=None, body='Question body\n', subject='Question', answers=()):
        sender = sender or self.sender
        self.sequence += 1
        directory = engine.FakeDirectory()
        # Only the operator recipient is used for questions; answers use the real fixture asker.
        directory.resolve = lambda address, origin: (replace(engine.SEATS['operator'], project_id=origin.project_id)
            if address == 'operator' else replace(engine.SEATS[address], project_id=origin.project_id))
        data = engine.draft(kind=kind, to='operator' if parent is None else self.sender.address,
                            needs='answer' if kind == 'QUESTION' else 'none', parent=parent,
                            bundle='fixture-'+str(self.sequence) if parent is None else None, body=body, subject=subject,
                            answers=answers)
        state = replace(engine.GIT_STATE, repository_id='repo-q') if sender.project_id == Q else engine.GIT_STATE
        context = submit.SubmitContext(sender, state, lambda: self.clock, directory,
                                       submission_key='fixture-'+str(self.sequence))
        return submit.submit(self.ledger, context, data)

    def answer(self, question):
        return self.admit('SITREP', sender=replace(engine.SEATS['operator'], project_id=P),
                          parent=question.relay_id, body='Operator answer\n')

    def rows(self, after=0, limit=101):
        self.reads.append((after, limit))
        return ingest.read_questions(self.ledger, P, after, limit)

    def items(self):
        cursor = self.catalogue.execute('SELECT item_id,source_relay_id,state,revision FROM queue_items ORDER BY raised_position')
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def snapshot(self):
        return {name: self.catalogue.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                for name in ('queue_items','queue_conditions','queue_events','queue_answers',
                             'queue_notifications','queue_receipts','queue_highwaters')}

    def read_ledger(self, project, fn):
        if project in self.unavailable:
            raise records.JobUnavailable('ledger_unavailable')
        if project == P:
            # Record the actual query arguments, without replacing its SQL or returned rows.
            original = ingest.read_questions
            def traced(conn, project_id, after_handle, limit):
                self.reads.append((after_handle, limit))
                return original(conn, project_id, after_handle, limit)
            with mock.patch.object(ingest, 'read_questions', traced):
                return fn(self.ledger)
        return fn(self.ledger)

    def backup(self, name):
        self.ledger.commit()
        self.ledger.close()
        path = self.root / name
        shutil.copyfile(self.path, path)
        self.ledger = sqlite3.connect(self.path)
        self.ledger.execute('PRAGMA foreign_keys=ON').close()
        return path

    def restore(self, path):
        self.ledger.close()
        shutil.copyfile(path, self.path)
        self.ledger = sqlite3.connect(self.path)
        self.ledger.execute('PRAGMA foreign_keys=ON').close()

    def seed_answer(self, item_id, state):
        # r7 F9: the only two later-made states seeded by Task 5.
        conn = self.catalogue
        conn.execute("UPDATE queue_items SET state='applying',revision=2 WHERE item_id=?", (item_id,)).close()
        records.append_event(conn,item_id,2,'applying','answered',self.operator,self.clock)
        phase = 'recorded'
        if state == 'resolved':
            conn.execute("UPDATE queue_items SET state='resolved',revision=3 WHERE item_id=?", (item_id,)).close()
            records.append_event(conn,item_id,3,'resolved','applied',records.InternalWriter(),self.clock)
            phase = 'confirmed'
        conn.execute('INSERT INTO queue_answers '
            '(item_id,answer_id,answer,answered_at_ms,apply_key,draft,project_id,phase) VALUES (?,?,?,?,?,?,?,?)',
            (item_id,'answer-'+item_id,'Answer',self.clock,'apply-'+item_id,b'draft',P,phase)).close()


class Port:
    def __init__(self, fixture):
        self.f = fixture

    def read_catalogue(self, fn):
        return fn(self.f.catalogue)

    def read_ledger(self, project, fn):
        return self.f.read_ledger(project, fn)

    def catalogue(self, fn):
        if self.f.before_catalogue:
            self.f.before_catalogue()
        conn = self.f.catalogue
        conn.commit()
        conn.execute('BEGIN IMMEDIATE').close()
        try:
            result = fn(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


class TestQueueIngest(unittest.TestCase):
    def setUp(self):
        self.f = QueueFixture(self)
        self.c = self.f.catalogue
        self.port = Port(self.f)

    def run_pass(self):
        return ingest.question_ingest_pass(self.port, [P, Q], self.f.clock)

    def item(self, question):
        return next(row for row in self.f.items() if row[1] == question.relay_id)

    def test_one_item_per_question(self):
        # Breaks lost source pins, sender routing, texts, or duplicate raises.
        q = self.f.admit(body='Exact body\n', subject='Exact subject')
        rows = self.f.rows()
        self.assertEqual(self.run_pass(), 1)
        row = self.c.execute('SELECT asking_seat_id,source_handle,source_relay_id,answer_to,actor_address,texts,pins '
                             'FROM queue_items').fetchone()
        self.assertEqual(row[:5], (self.f.sender.seat_id,q.handle,q.relay_id,self.f.sender.address,self.f.sender.address))
        self.assertEqual(json.loads(row[5]), {'subject':'Exact subject','body':'Exact body\n'})
        self.assertEqual(json.loads(row[6]), {'project_id':P,'relay_id':q.relay_id,'body_sha256':hashlib.sha256(b'Exact body\n').hexdigest()})
        before = self.f.snapshot()
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.f.snapshot(), before)
        self.assertFalse(self.f.ledger.in_transaction)
        self.assertEqual(rows[0].relay_id, q.relay_id)

    def test_unroutable_question(self):
        # Breaks dropped NULL-address questions or inverted dismiss eligibility.
        q = self.f.admit()
        self.f.ledger.execute('UPDATE team_instances SET closed_at=2 WHERE id=?', (self.f.sender.team_instance_id,)).close()
        self.f.ledger.commit()
        self.run_pass()
        item = self.item(q)
        self.assertEqual(self.c.execute('SELECT answer_to,actor_address FROM queue_items').fetchone(), (None,None))
        self.assertEqual(operations.dismiss(self.c,self.f.operator,item[0],1,'dismiss',3)['state'],'dismissed')
        self.assertEqual(self.c.execute('SELECT event_kind FROM queue_events ORDER BY position DESC LIMIT 1').fetchone(),('dismissed',))
        q2 = self.f.admit(sender=self.f.other)
        self.run_pass()
        item2 = self.item(q2)
        self.assertEqual(self.c.execute('SELECT answer_to,actor_address FROM queue_items WHERE item_id=?',(item2[0],)).fetchone(),(self.f.other.address,self.f.other.address))
        self.assertEqual(operations.dismiss(self.c,self.f.operator,item2[0],1,'control',4).cause_kind,'not_a_notice')

    def test_crash_between_relay_and_item(self):
        q = self.f.admit()
        self.assertEqual(self.f.items(), [])
        self.assertEqual(self.run_pass(),1)
        self.assertEqual(self.item(q)[2:4],('pending',1))
        before=self.f.snapshot();self.assertEqual(self.run_pass(),0);self.assertEqual(self.f.snapshot(),before)

    def test_other_kinds_and_projects_ignored(self):
        self.f.admit('SITREP');self.f.admit(sender=self.f.foreign)
        self.assertEqual(self.f.rows(),[])
        self.assertEqual(ingest.question_ingest_pass(self.port,[P],self.f.clock),0)
        self.assertEqual(self.f.items(),[])

    def test_question_ingest_empty_and_nul_body(self):
        # Rejecting empty/NUL question text loses admitted questions and stalls the cursor.
        empty = self.f.admit(body='')
        nul = self.f.admit(body='a\x00b')
        self.assertEqual(self.run_pass(), 2)
        rows = self.c.execute('SELECT source_relay_id,state,texts FROM queue_items '
                              'ORDER BY raised_position').fetchall()
        self.assertEqual([(row[0], row[1], json.loads(row[2])['body']) for row in rows],
                         [(empty.relay_id, 'pending', ''), (nul.relay_id, 'pending', 'a\x00b')])
        self.assertEqual(records.read_cursor(self.c, P, 'question'),
                         records.Cursor(nul.handle, 0, nul.relay_id))

    def test_choices_from_body(self):
        a=self.f.admit(body='Choose\nChoices:\n- Yes\n- No\n')
        b=self.f.admit(body='Choose\nChoices:\n- \n')
        self.run_pass()
        rows=self.c.execute('SELECT source_relay_id,choices,choices_reason FROM queue_items ORDER BY raised_position').fetchall()
        self.assertEqual(rows,[(a.relay_id,'["Yes","No"]',None),(b.relay_id,None,'empty_label')])

    def test_ingest_pass_skips_unavailable_ledger(self):
        # Breaking instead of continuing must not skip the available later project.
        q=self.f.admit();self.f.unavailable.add(Q)
        self.assertEqual(ingest.question_ingest_pass(self.port,[Q,P],self.f.clock),1)
        self.assertEqual(self.item(q)[2],'pending')
        self.assertEqual(records.read_cursor(self.c,Q,'question'),records.Cursor(0,0,None))

    def test_highwater_advances_with_items(self):
        q=self.f.admit();self.c.execute('BEGIN').close()
        self.assertEqual(ingest.ingest_questions(self.c,self.f.rows(),P,records.Cursor(0,0,None),check_rewind=True,now_ms=3),1)
        self.assertEqual(records.read_cursor(self.c,P,'question'),records.Cursor(q.handle,0,q.relay_id))
        self.assertTrue(self.c.in_transaction);self.c.rollback()
        self.assertEqual(self.f.items(),[]);self.assertEqual(records.read_cursor(self.c,P,'question'),records.Cursor(0,0,None))

    def test_question_ingest_fence_and_cursor(self):
        a=self.f.admit();b=self.f.admit(sender=self.f.foreign)
        def fence():
            self.c.execute("UPDATE project_routes SET state='creating' WHERE project_id=?",(P,)).close()
        self.f.before_catalogue=fence
        self.assertEqual(self.run_pass(),1);self.assertEqual([r[1] for r in self.f.items()],[b.relay_id])
        self.assertEqual(records.read_cursor(self.c,P,'question'),records.Cursor(0,0,None))
        self.f.before_catalogue=None
        rows=self.f.rows();old=records.read_cursor(self.c,P,'question')
        self.c.execute("UPDATE project_routes SET state='active' WHERE project_id=?",(P,)).close()
        self.c.execute('INSERT INTO queue_highwaters VALUES (?,?,0,1,NULL)',(P,'question')).close()
        before=self.f.snapshot();changes=self.c.total_changes
        self.assertIsInstance(ingest.ingest_questions(self.c,rows,P,old,check_rewind=True,now_ms=4),records.Stale)
        self.assertEqual(self.f.snapshot(),before);self.assertEqual(self.c.total_changes,changes)
        self.assertEqual(self.run_pass(),1);self.assertEqual(self.item(a)[2],'pending')

    def test_recovered_answered_question_stays_answered(self):
        a=self.f.admit();b=self.f.admit();self.f.answer(a)
        self.assertEqual(self.run_pass(),1);self.assertEqual([r[1] for r in self.f.items()],[b.relay_id])

    def test_cursor_advances_past_answered_rows(self):
        b=self.f.admit();a=self.f.admit();self.f.answer(a)
        self.assertEqual(self.run_pass(),1)
        self.assertEqual(records.read_cursor(self.c,P,'question'),records.Cursor(a.handle,0,a.relay_id))
        self.assertEqual([r.relay_id for r in self.f.rows(a.handle-1)],[a.relay_id])
        changes=self.c.total_changes;self.assertEqual(self.run_pass(),0);self.assertEqual(self.c.total_changes,changes)
        self.assertEqual([r[1] for r in self.f.items()],[b.relay_id])

    def test_answers_line_answers_without_item(self):
        # An ANSWERS target B must be skipped independently of the parent A.
        a = self.f.admit(subject='Question A')
        b = self.f.admit(subject='Question B')
        control = QueueFixture(self)
        self.f.ledger.backup(control.ledger)
        self.assertEqual(self.f.ledger.execute(
            "SELECT opened_at FROM recipients WHERE special='operator' ORDER BY relay_id"
        ).fetchall(), [(None,), (None,)])
        self.f.admit('SITREP', sender=replace(engine.SEATS['operator'], project_id=P),
                     parent=a.relay_id, answers=(b.relay_id,), body='Answer A and B\n')
        self.run_pass()
        self.assertIsNone(self.c.execute(
            'SELECT item_id FROM queue_items WHERE source_relay_id=?', (b.relay_id,)
        ).fetchone())
        self.assertIsNone(self.c.execute(
            'SELECT item_id FROM queue_items WHERE source_relay_id=?', (a.relay_id,)
        ).fetchone())
        higher = max((a, b), key=lambda question: question.handle)
        self.assertEqual(records.read_cursor(self.c, P, 'question'),
                         records.Cursor(higher.handle, 0, higher.relay_id))
        self.assertEqual(ingest.question_ingest_pass(Port(control), [P], control.clock), 2)
        self.assertEqual([(row[1], row[2]) for row in control.items()],
                         [(a.relay_id, 'pending'), (b.relay_id, 'pending')])

    def test_answers_after_raise_keeps_item_pending(self):
        # Outside answers do not revisit the items already raised by this cursor.
        c = self.f.admit(subject='Question C')
        d = self.f.admit(subject='Question D')
        self.assertEqual(self.run_pass(), 2)
        items = self.f.items()
        self.assertEqual([(row[1], row[2]) for row in items],
                         [(c.relay_id, 'pending'), (d.relay_id, 'pending')])
        before = self.f.snapshot()
        changes = self.c.total_changes
        self.f.admit('SITREP', sender=replace(engine.SEATS['operator'], project_id=P),
                     parent=d.relay_id, answers=(c.relay_id,), body='Answer D and C\n')
        self.f.reads.clear()
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.f.items(), items)
        self.assertEqual(self.f.snapshot(), before)
        self.assertEqual(self.c.total_changes, changes)
        self.assertEqual(self.f.reads, [(d.handle - 1, 101)])
        fresh = QueueFixture(self)
        self.f.ledger.backup(fresh.ledger)
        self.assertEqual(ingest.question_ingest_pass(Port(fresh), [P], fresh.clock), 0)
        self.assertEqual(fresh.items(), [])
        self.assertEqual(records.read_cursor(fresh.catalogue, P, 'question'),
                         records.Cursor(d.handle, 0, d.relay_id))

    def test_unrendered_answer_reopens_question(self):
        a=self.f.admit();self.assertEqual(self.run_pass(),1);self.assertEqual(self.run_pass(),0)
        self.assertEqual(self.item(a)[2],'pending')
        b=self.f.admit();self.f.answer(b);self.assertEqual(self.run_pass(),0)
        self.assertNotIn(b.relay_id,[r[1] for r in self.f.items()])

    def anchor(self):
        self.f.admit('SITREP')
        low=self.f.backup('low.sqlite3')
        self.f.admit('SITREP')
        a=self.f.admit()
        self.run_pass()
        self.f.clock+=100
        return low,a

    def test_putback_new_question_at_cursor_raised(self):
        low,a=self.anchor();self.f.restore(low);self.f.admit('SITREP');b=self.f.admit()
        self.assertEqual(b.handle,a.handle);self.assertNotEqual(b.relay_id,a.relay_id)
        self.assertEqual(self.run_pass(),1);self.assertEqual(self.item(b)[2],'pending')
        self.assertEqual(records.read_cursor(self.c,P,'question').anchor_id,b.relay_id)

    def test_putback_non_question_at_cursor(self):
        low,a=self.anchor();self.f.restore(low);b=self.f.admit();self.f.admit('SITREP')
        self.assertLess(b.handle,a.handle);self.assertEqual(self.run_pass(),1);self.assertEqual(self.item(b)[2],'pending')

    def test_putback_below_cursor_no_question(self):
        low,a=self.anchor();self.f.restore(low)
        self.assertEqual(self.run_pass(),0);self.assertEqual(records.read_cursor(self.c,P,'question'),records.Cursor(0,0,None))
        changes=self.c.total_changes;self.assertEqual(self.run_pass(),0);self.assertEqual(self.c.total_changes,changes)
        b=self.f.admit();self.assertLess(b.handle,a.handle);self.assertEqual(self.run_pass(),1)

    def test_putback_at_or_above_cursor_reads_on(self):
        low,a=self.anchor();at=self.f.backup('at.sqlite3');self.f.admit();self.f.restore(at)
        self.f.reads.clear();self.assertEqual(self.run_pass(),0)
        self.assertTrue(self.f.reads);self.assertNotIn(0,[after for after,_ in self.f.reads])
        b=self.f.admit();self.assertEqual(self.run_pass(),1);self.assertEqual(self.item(b)[2],'pending')

    def test_putback_answered_outside_queue_not_trusted(self):
        low,a=self.anchor();self.f.restore(low);d=self.f.admit();b=self.f.admit();self.f.answer(b)
        self.assertEqual(b.handle,a.handle);self.assertEqual(self.run_pass(),1)
        self.assertEqual(self.item(d)[2],'pending');self.assertNotIn(b.relay_id,[r[1] for r in self.f.items()])

    def test_putback_reread_raises_nothing_twice(self):
        a=self.f.admit();b=self.f.admit();c=self.f.admit();d=self.f.admit();self.f.answer(d)
        base=self.f.backup('all.sqlite3');self.run_pass()
        self.f.seed_answer(self.item(b)[0],'resolved')
        operations.withdraw(self.c,self.f.ledger,self.f.binding,self.item(c)[0],1,'Withdraw','withdraw',self.f.clock)
        self.c.commit()
        self.f.admit();self.run_pass();self.f.restore(base)
        before={r[0]:(r[2],r[3]) for r in self.f.items()}
        events=self.c.execute('SELECT * FROM queue_events').fetchall()
        self.assertEqual(self.run_pass(),0)
        self.assertEqual({r[0]:(r[2],r[3]) for r in self.f.items()},before)
        self.assertEqual(self.c.execute('SELECT * FROM queue_events').fetchall(),events)
        self.assertNotIn(d.relay_id,[r[1] for r in self.f.items()])
        self.assertEqual(self.c.execute('SELECT closed_at_ms,clear_requested FROM queue_conditions WHERE item_id=?',(self.item(b)[0],)).fetchone(),(None,0))

    def test_putback_removed_anchor_answer_reraises(self):
        a=self.f.admit();before_answer=self.f.backup('question.sqlite3');self.f.answer(a);self.run_pass()
        self.assertEqual(self.f.items(),[]);self.f.restore(before_answer)
        self.assertEqual(self.run_pass(),1);self.assertEqual(self.item(a)[2],'pending');self.assertEqual(self.run_pass(),0)

    def test_putback_repeated_id_without_item_reraises(self):
        self.f.admit('SITREP');low=self.f.backup('repeat.sqlite3')
        # Replay the same entropy series only for the two real admissions.
        with mock.patch.object(submit.secrets, 'randbits', side_effect=iter(range(100, 200))):
            a=self.f.admit()
        self.f.answer(a);self.run_pass();self.assertEqual(self.f.items(),[])
        self.f.restore(low)
        with mock.patch.object(submit.secrets, 'randbits', side_effect=iter(range(100, 200))):
            b=self.f.admit()
        self.assertEqual((b.relay_id,b.handle),(a.relay_id,a.handle))
        self.assertEqual(self.run_pass(),1);self.assertEqual(self.item(b)[2],'pending');self.assertEqual(self.run_pass(),0)


if __name__ == '__main__':
    unittest.main()


# PR 3a lifecycle cases use the same real ledger/catalogue and writer-job port.
from relay_engine import lifecycle_outbox as outbox
from relay_engine.operator_queue import notice_text, notifier


class TestLifecycleIngest(unittest.TestCase):
    def setUp(self):
        self.f = QueueFixture(self)
        self.c = self.f.catalogue
        self.port = Port(self.f)
        self.assertTrue(callable(getattr(ingest, 'ingest_lifecycle', None)),
                        'lifecycle catalogue ingest is absent')
        self.assertTrue(callable(getattr(ingest, 'lifecycle_ingest_pass', None)),
                        'three-stage lifecycle pass is absent')

    def append(self, form='seat', *, project=P, commit=True, **changes):
        ledger = self.f.ledger
        ledger.execute('BEGIN').close()
        common = dict(project_id=project, committed_at_ms=self.f.clock)
        sender = self.f.foreign if project == Q else self.f.sender
        if form == 'seat':
            args = dict(subkind='seat_registered', seat_id=sender.seat_id,
                        old_occupant_id=None, new_occupant_id='o-'+sender.seat_id,
                        actor_kind='occupant', actor_occupant_id='o-'+sender.seat_id)
            fn = outbox.append_seat_row
        elif form == 'settlement':
            args = dict(outcome='finished', occasion='start', chain_id='chain',
                        runtime_instance_id='runtime', transaction_ordinal=1,
                        events=(outbox.SettledEvent(1, 'slip-issued', 'slip'),))
            fn = outbox.append_settlement_row
        elif form == 'restart':
            args = dict(seat_id=sender.seat_id, actor_occupant_id='o-'+sender.seat_id)
            fn = outbox.append_restart_row
        elif form == 'merge':
            args = dict(holder_seat_id=sender.seat_id, slip_id='slip', pr=123,
                        merge_commit='a'*40, allowed_target='v3', landed_target='main')
            fn = outbox.append_merge_alarm_row
        elif form == 'instruction':
            args = dict(act_kind='slip-issued', carrying_seat_id=sender.seat_id,
                        instruction_relay_id='instruction', chain_id='chain', event_number=1)
            fn = outbox.append_instructed_act_row
        elif form == 'unseat':
            args = dict(seat_id=sender.seat_id, generation=1, surface='page',
                        carrying_seat_id=None, instruction_relay_id=None)
            fn = outbox.append_unseat_row
        else:
            raise AssertionError(form)
        args.update(changes)
        if form == 'seat':
            if args['subkind'] == 'seat_replaced' and args['old_occupant_id'] is None:
                args['old_occupant_id'] = 'old-'+sender.seat_id
            if args['subkind'] == 'seat_stood_down':
                args['new_occupant_id'] = None
            for occupant in (args['old_occupant_id'], args['new_occupant_id']):
                if occupant is not None:
                    ledger.execute('INSERT OR IGNORE INTO seat_occupants '
                        '(id,seat_id,key_id,key_verifier,issued_at,ended_at) VALUES (?,?,?, ?,1,2)',
                        (occupant, sender.seat_id, 'key-'+occupant, 'v')).close()
        try:
            fn(ledger, **common, **args)
            row = outbox.read_after(ledger, project, 0, limit=10000)[-1]
            if commit:
                ledger.commit()
            else:
                ledger.rollback()
            return row
        except BaseException:
            ledger.rollback()
            raise

    def run_pass(self, projects=(P, Q), limit=100):
        return ingest.lifecycle_ingest_pass(self.port, projects, self.f.clock, limit=limit)

    def cursor(self, project=P):
        return records.read_cursor(self.c, project, 'lifecycle')

    def items(self):
        return operations._dicts(self.c, 'SELECT * FROM queue_items ORDER BY raised_position')

    def item(self, row):
        for item in self.items():
            if json.loads(item['pins'])['row_id'] == row.row_id:
                return item
        self.fail('missing item for '+row.row_id)

    def notify_count(self):
        return self.c.execute('SELECT COUNT(*) FROM queue_notifications').fetchone()[0]

    def reset_project(self, backup):
        records.undo_step(self.c, P, 'undo', now_ms=self.f.clock)
        self.c.commit()
        self.f.restore(backup)
        self.c.execute("UPDATE project_routes SET state='active' WHERE project_id=?", (P,)).close()
        self.c.commit()

    def catalogue_copy(self, name):
        self.c.commit()
        path = self.f.root / name
        target = sqlite3.connect(path)
        try:
            self.c.backup(target)
        finally:
            target.close()
        return path

    def restore_catalogue(self, path):
        self.c.commit()
        source = sqlite3.connect(path)
        try:
            source.backup(self.c)
        finally:
            source.close()

    def test_one_notice_per_outbox_row(self):
        row = self.append(subkind='seat_replaced', old_occupant_id='old', new_occupant_id='new')
        self.assertEqual(self.items(), [])
        self.assertEqual(self.run_pass(), 1)
        item = self.item(row)
        self.assertEqual(item['subkind'], 'seat_replaced')
        self.assertEqual(item['actor_address'], self.f.sender.address)
        self.assertEqual(json.loads(item['pins'])['old_occupant_id'], 'old')
        self.assertEqual(json.loads(item['pins'])['new_occupant_id'], 'new')
        self.assertEqual(json.loads(item['condition_key']), dict(project_id=P, row_id=row.row_id))
        self.assertEqual(self.cursor(), records.Cursor(1, 0, row.row_id))
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(len(self.items()), 1)

    def committed_settlement(self, outcome, occasion):
        row = self.append('settlement', outcome=outcome, occasion=occasion)
        # Dropping the source connection models loss of process-local work after commit.
        saved = self.f.backup('after-commit.sqlite3')
        self.f.restore(saved)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(row)['subkind'], 'settlement_'+outcome)
        # Control: the old two-transaction writer stops after its ledger-only commit.
        control = QueueFixture(self)
        control.ledger.execute("UPDATE projects SET name='settled' WHERE id=?", (P,)).close()
        control.ledger.commit()
        # A stop here prevents the separate catalogue-raise transaction from running.
        self.assertEqual(ingest.lifecycle_ingest_pass(Port(control), [P], self.f.clock), 0)
        self.assertEqual(control.items(), [])

    def test_settlement_notice_survives_stop_after_commit(self):
        self.committed_settlement('finished', 'start')

    def test_running_void_notice_survives_stop_after_commit(self):
        self.committed_settlement('undone', 'running')

    def test_rolled_back_settlement_raises_nothing(self):
        self.append('settlement', commit=False)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.items(), [])
        row = self.append('settlement')
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(row)['state'], 'pending')

    def test_reread_over_dismissed_notice_raises_nothing(self):
        one = self.append('settlement')
        low = self.f.backup('one.sqlite3')
        self.append()
        self.run_pass()
        item = self.item(one)
        result = operations.dismiss(self.c, self.f.operator, item['item_id'], 1, 'dismiss', self.f.clock)
        self.assertEqual(result['state'], 'dismissed')
        self.c.commit()
        dismissed = self.catalogue_copy('dismissed.sqlite3')
        self.f.restore(low)
        fresh = self.append()
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual((self.item(one)['state'], self.item(one)['revision']), ('dismissed', 2))
        self.assertEqual(self.item(fresh)['state'], 'pending')
        self.assertEqual(self.notify_count(), 0)
        self.restore_catalogue(dismissed)
        self.c.execute('UPDATE queue_conditions SET closed_at_ms=? WHERE item_id=?',
                       (self.f.clock, item['item_id'])).close()
        self.c.commit()
        self.assertEqual(self.run_pass(), 2)
        self.assertEqual(sum(json.loads(i['pins'])['row_id'] == one.row_id for i in self.items()), 2)

    def test_settlement_notice_text(self):
        rows = [self.append('settlement', outcome='undone', occasion=occasion)
                for occasion in ('start', 'running', 'stop', 'resolve')]
        rows.append(self.append('settlement'))
        self.assertEqual(self.run_pass(), 5)
        for row in rows:
            item = self.item(row)
            self.assertEqual(json.loads(item['texts']), notice_text.settlement_texts(row))
            self.assertEqual(json.loads(item['pins']), notice_text.settlement_pins(row))
            self.assertIsNone(item['actor_address'])
        self.assertEqual(json.loads(self.item(rows[3])['texts'])['caused_by'], 'your answer')

    def test_settlement_notice_lifetime(self):
        rows = [self.append('settlement') for _ in range(3)] + [self.append()]
        self.assertEqual(self.run_pass(), 4)
        reopened = self.catalogue_copy('reopen.sqlite3')
        self.restore_catalogue(reopened)
        self.assertEqual(self.run_pass(), 0)
        self.assertTrue(all(i['state'] == 'pending' for i in self.items()))
        for row, action in zip(rows[:2], (operations.open_notice, operations.dismiss)):
            result = action(self.c, self.f.operator, self.item(row)['item_id'], 1,
                            'act-'+row.row_id, self.f.clock)
            self.assertEqual(result['state'], 'dismissed')
        high = self.c.execute('SELECT MAX(position) FROM queue_events').fetchone()[0]
        result = operations.dismiss_all(self.c, self.f.operator, 'machine', high, 'all', self.f.clock)
        self.assertEqual(result['count'], 2)
        self.assertTrue(all(i['state'] == 'dismissed' for i in self.items()))

    def test_ledger_rewind_raises_reused_position(self):
        self.append(); self.append()
        low = self.f.backup('two.sqlite3')
        self.append(); self.run_pass()
        before = self.catalogue_copy('three.sqlite3')
        self.f.restore(low)
        fresh = self.append('settlement')
        original = ingest.ingest_lifecycle
        def unchecked(conn, rows, project, cursor, **kwargs):
            return original(conn, [r for r in rows if r.position > cursor.position], project,
                            cursor, check_rewind=False, now_ms=kwargs['now_ms'])
        with mock.patch.object(ingest, 'ingest_lifecycle', unchecked):
            self.assertEqual(self.run_pass(), 0)
        self.assertFalse(any(json.loads(i['pins'])['row_id'] == fresh.row_id for i in self.items()))
        self.restore_catalogue(before)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.cursor().anchor_id, fresh.row_id)

    def test_catalogue_restore_reraises_lost_notices(self):
        one = self.append('settlement'); self.run_pass()
        operations.dismiss(self.c, self.f.operator, self.item(one)['item_id'], 1, 'dismiss', self.f.clock)
        saved = self.catalogue_copy('catalogue-one.sqlite3')
        two = self.append(); three = self.append('settlement'); self.run_pass()
        self.restore_catalogue(saved)
        external = records.Cursor(3, 0, three.row_id)
        original = ingest.ingest_lifecycle
        def external_without_rewind(c, rs, p, cur, **kw):
            new = [row for row in rs if row.position > cur.position]
            return original(c, new, p, cur, check_rewind=False, now_ms=kw['now_ms']) if new else 0
        with mock.patch.object(records, 'read_cursor', return_value=external), mock.patch.object(
                ingest, 'ingest_lifecycle', external_without_rewind):
            self.assertEqual(self.run_pass(), 0)
        self.restore_catalogue(saved)
        self.assertEqual(self.run_pass(), 2)
        self.assertEqual(self.item(one)['state'], 'dismissed')
        self.assertEqual(self.item(two)['state'], 'pending')
        self.assertEqual(self.run_pass(), 0)

    def test_undo_retires_settlement_notice(self):
        empty = self.f.backup('empty.sqlite3')
        row = self.append('settlement'); self.run_pass()
        saved = self.catalogue_copy('pending.sqlite3')
        original = records._rows
        def omit_settlement(conn, sql, params=()):
            rows = original(conn, sql, params)
            if sql.startswith('SELECT item_id,kind,state,project_id,condition_key,revision'):
                return [r for r in rows if r[0] != self.item(row)['item_id']]
            return rows
        with mock.patch.object(records, '_rows', omit_settlement):
            records.undo_step(self.c, P, 'bad-undo', now_ms=self.f.clock)
        self.assertEqual(self.item(row)['state'], 'pending')
        self.restore_catalogue(saved)
        self.reset_project(empty)
        self.assertEqual(self.item(row)['state'], 'retired')
        fresh = self.append('settlement')
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(fresh)['state'], 'pending')

    def test_fresh_ledger_raises_again(self):
        empty = self.f.backup('empty.sqlite3')
        old = self.append(); self.run_pass()
        saved = self.catalogue_copy('old.sqlite3')
        self.reset_project(empty)
        fresh = self.append()
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(fresh)['subkind'], 'seat_registered')
        self.restore_catalogue(saved)
        original = ingest.ingest_lifecycle
        def without_reset_or_rewind(c, rs, p, cur, **kw):
            new = [row for row in rs if row.position > cur.position]
            return original(c, new, p, cur, check_rewind=False, now_ms=kw['now_ms']) if new else 0
        with mock.patch.object(ingest, 'ingest_lifecycle', without_reset_or_rewind):
            self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.cursor().anchor_id, old.row_id)

    def test_old_lifecycle_batch_across_reactivation(self):
        empty = self.f.backup('empty.sqlite3')
        old = self.append(); old_rows = outbox.read_after(self.f.ledger, P, 0, limit=101)
        old_cursor = self.cursor()
        self.reset_project(empty)
        fresh = self.append()
        before = self.f.snapshot()
        result = self.port.catalogue(lambda c: ingest.ingest_lifecycle(
            c, old_rows, P, old_cursor, check_rewind=True, now_ms=self.f.clock))
        self.assertIsInstance(result, records.Stale)
        self.assertEqual(self.f.snapshot(), before)
        saved = self.catalogue_copy('before-wrong-epoch.sqlite3')
        current = self.cursor()
        result = self.port.catalogue(lambda c: ingest.ingest_lifecycle(
            c, old_rows, P, current, check_rewind=True, now_ms=self.f.clock))
        self.assertEqual(result, 1)  # Removing the epoch comparison admits the old batch.
        self.assertEqual(self.cursor().anchor_id, old.row_id)
        self.restore_catalogue(saved)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(fresh)['state'], 'pending')
        self.assertEqual(self.run_pass(), 0)

    def test_mixed_batch_in_position_order(self):
        rows = [self.append(), self.append('settlement'), self.append()]
        self.assertEqual(self.run_pass(), 3)
        self.assertEqual([json.loads(i['pins'])['row_id'] for i in self.items()], [r.row_id for r in rows])

    def test_lifecycle_notices_do_not_ring(self):
        for subkind in ('seat_registered', 'seat_replaced', 'seat_stood_down'):
            self.append(subkind=subkind)
        self.append('settlement'); self.append('settlement', outcome='undone', occasion='running')
        self.assertEqual(self.run_pass(), 5)
        self.assertEqual(self.notify_count(), 0)

    def test_reread_twice_moves_on(self):
        self.append(); other = self.append(project=Q)
        original = ingest.ingest_lifecycle
        def twice(conn, rows, project, cursor, **kw):
            return records.Reread() if project == P else original(conn, rows, project, cursor, **kw)
        with mock.patch.object(ingest, 'ingest_lifecycle', twice):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.item(other)['project_id'], Q)
        self.assertEqual(self.cursor(), records.Cursor(0, 0, None))

    def test_restart_notice_from_outbox(self):
        rows = [self.append('restart'), self.append('restart')]
        self.assertEqual(self.run_pass(), 2)
        self.assertNotEqual(self.item(rows[0])['condition_key'], self.item(rows[1])['condition_key'])
        for row in rows:
            item = self.item(row)
            self.assertEqual(item['actor_address'], self.f.sender.address)
            self.assertEqual(json.loads(item['pins']), notice_text.restart_pins(row))
            self.assertEqual(json.loads(item['texts']), notice_text.restart_texts(row))
        self.assertEqual(self.notify_count(), 0)

    def test_merge_alarm_rings(self):
        alarm = self.append('merge'); seat = self.append()
        self.assertEqual(self.run_pass(), 2)
        self.assertEqual(json.loads(self.item(alarm)['condition_key']), dict(project_id=P, slip_id='slip'))
        self.assertEqual(json.loads(self.item(alarm)['texts']), notice_text.merge_alarm_texts(alarm))
        self.assertEqual(self.item(alarm)['actor_address'], self.f.sender.address)
        self.assertEqual(self.notify_count(), 1)
        self.assertNotEqual(self.item(alarm)['item_id'], self.item(seat)['item_id'])

    def test_merge_replay_rings_nothing(self):
        empty = self.f.backup('empty.sqlite3')
        first = self.append('merge'); self.run_pass(); item = self.item(first)
        operations.dismiss(self.c, self.f.operator, item['item_id'], 1, 'dismiss', self.f.clock)
        self.append('merge')
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.notify_count(), 1)
        self.reset_project(empty)
        fresh = self.append('merge')
        self.assertEqual(self.run_pass(), 1)
        self.assertNotEqual(self.item(fresh)['item_id'], item['item_id'])
        self.assertEqual(self.notify_count(), 2)

    def test_merge_text_and_cut(self):
        rows = [self.append('merge', slip_id='slip-'+str(n), merge_commit='a'*n) for n in (40, 64)]
        self.run_pass()
        notes = notifier.due_notifications(self.c, self.f.clock, 10)
        self.assertEqual([len(n.text.encode()) for n in notes], [184, 208])
        delivered = [notifier.notification_argv(n, 'notify-send')[-1] for n in notes]
        mark = " … [cut: the whole message is on v3's page]"
        self.assertEqual(delivered[0], notes[0].text)
        self.assertEqual(delivered[1], notes[1].text[:155] + mark)
        self.assertTrue(delivered[1].endswith('a'*11+mark))
        for row, note in zip(rows, notes):
            whole = operations.get_item(self.c, self.item(row)['item_id'], 100, None)['item']
            whole['texts'] = json.loads(whole['texts'])
            self.assertEqual(whole['texts']['sentence'], note.text)

    def test_cursor_anchor_at_batch_end(self):
        self.append(); self.append(); self.append('merge')
        self.assertEqual(self.run_pass(limit=3), 3)
        saved = self.catalogue_copy('at-three.sqlite3')
        fourth = self.append(); fifth = self.append('merge')
        self.assertEqual(self.run_pass(limit=3), 1)
        self.assertEqual(self.cursor(), records.Cursor(5, 0, fifth.row_id))
        self.assertEqual(self.run_pass(limit=3), 0)
        self.assertEqual(self.notify_count(), 1)
        self.restore_catalogue(saved)
        original = ingest.ingest_lifecycle
        def item_anchor(conn, rows, project, cursor, **kw):
            if cursor.position and not any(json.loads(i['condition_key']).get('row_id') == cursor.anchor_id
                                          for i in self.items()):
                return records.Reread()
            return original(conn, rows, project, cursor, **kw)
        with mock.patch.object(ingest, 'ingest_lifecycle', item_anchor):
            self.assertEqual(self.run_pass(limit=3), 0)
            self.assertEqual(self.run_pass(limit=3), 0)
        self.assertFalse(any(json.loads(i['pins'])['row_id'] == fourth.row_id for i in self.items()))

    def hold_batch(self, form, **changes):
        one = self.append(); self.append(form, **changes); self.append()
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.cursor(), records.Cursor(1, 0, one.row_id))
        self.assertEqual(len(self.items()), 1)

    def test_instructed_act_waits_for_pr3c(self):
        self.hold_batch('instruction')
        control = QueueFixture(self)
        other = type(self)('test_mixed_batch_in_position_order')
        other.f, other.c, other.port = control, control.catalogue, Port(control)
        rows = [other.append() for _ in range(3)]
        self.assertEqual(other.run_pass(), 3)
        self.assertEqual(other.cursor(), records.Cursor(3, 0, rows[-1].row_id))

    def test_unseat_notice_and_instructed_hold(self):
        surface = self.append('unseat'); self.assertEqual(self.run_pass(), 1)
        item = self.item(surface)
        self.assertIsNone(item['fields'])
        self.assertEqual(item['actor_address'], self.f.sender.address)
        self.assertEqual(json.loads(item['texts']), notice_text.unseat_texts(surface))
        self.assertEqual(self.notify_count(), 0)
        control = QueueFixture(self)
        other = type(self)('test_mixed_batch_in_position_order')
        other.f, other.c, other.port = control, control.catalogue, Port(control)
        other.hold_batch('unseat', surface=None, carrying_seat_id=control.sender.seat_id,
                         instruction_relay_id='instruction')
        third = QueueFixture(self)
        ordinary = type(self)('test_mixed_batch_in_position_order')
        ordinary.f, ordinary.c, ordinary.port = third, third.catalogue, Port(third)
        ordinary.append(); page = ordinary.append('unseat'); last = ordinary.append()
        self.assertEqual(ordinary.run_pass(), 3)
        self.assertEqual(ordinary.item(page)['subkind'], 'seat_unseated')
        self.assertEqual(json.loads(ordinary.item(page)['texts']), notice_text.unseat_texts(page))
        self.assertEqual(ordinary.cursor(), records.Cursor(3, 0, last.row_id))

    def test_lifecycle_free_values_fit(self):
        alarm = self.append('merge', pr=10**18, merge_commit='a'*64,
                            allowed_target='v'*3000, landed_target='m'*3000)
        last = self.append()
        saved = self.catalogue_copy('before-fit.sqlite3')
        self.assertEqual(self.run_pass(), 2)
        item = self.item(alarm)
        text = json.loads(item['texts'])['sentence']
        self.assertIn('v'*509+'…', text); self.assertIn('m'*509+'…', text)
        self.assertLessEqual(len(text.encode()), 4096)
        self.assertEqual(json.loads(item['pins'])['allowed_target'], alarm.allowed_target)
        self.assertEqual(json.loads(item['pins'])['pr'], str(10**18))
        self.assertEqual(self.cursor().anchor_id, last.row_id)
        self.restore_catalogue(saved)
        original = notice_text.merge_alarm_texts
        def unfit(row):
            with mock.patch.object(notice_text, 'fit_value', lambda value: value):
                return original(row)
        with mock.patch.object(notice_text, 'merge_alarm_texts', unfit):
            with self.assertRaises(ingest.LifecycleRowRefused):
                self.port.catalogue(lambda c: ingest.ingest_lifecycle(c, (alarm, last), P,
                    self.cursor(), check_rewind=True, now_ms=self.f.clock))
        self.assertEqual(self.items(), [])

    def test_lifecycle_pin_integer_boundary(self):
        rows = [self.append('merge', pr=2**53-1, slip_id='in'),
                self.append('merge', pr=2**53, slip_id='out'),
                self.append('settlement', transaction_ordinal=2**53,
                            events=(outbox.SettledEvent(2**53, 'slip-issued', 'slip'),)),
                self.append('unseat', generation=2**53)]
        final = self.append()
        saved = self.catalogue_copy('before-integers.sqlite3')
        self.assertEqual(self.run_pass(), 5)
        values = [json.loads(operations.get_item(self.c, self.item(r)['item_id'], 100, None)['item']['pins']) for r in rows]
        self.assertEqual(values[0]['pr'], 2**53-1)
        self.assertEqual(values[1]['pr'], str(2**53))
        self.assertEqual(values[2]['transaction_ordinal'], str(2**53))
        self.assertEqual(values[2]['events'][0]['number'], str(2**53))
        self.assertEqual(values[3]['generation'], str(2**53))
        self.assertEqual(self.cursor().anchor_id, final.row_id)
        for row in rows:
            self.restore_catalogue(saved)
            with mock.patch.object(notice_text, 'fit_integers', lambda value: value):
                if row is rows[0]:
                    self.assertEqual(self.port.catalogue(lambda c: ingest.ingest_lifecycle(
                        c, (row,), P, self.cursor(), check_rewind=True, now_ms=self.f.clock)), 1)
                else:
                    with self.assertRaises(ingest.LifecycleRowRefused) as caught:
                        self.port.catalogue(lambda c: ingest.ingest_lifecycle(
                            c, (row, final), P, self.cursor(), check_rewind=True, now_ms=self.f.clock))
                    self.assertEqual(caught.exception.refusal.cause_kind, 'subject_unresolved')
                    self.assertEqual(self.items(), [])

    def refused(self, target):
        original = records.raise_item
        def refusal(conn, principal, request, *args, **kw):
            if request.pins['row_id'] == target.row_id:
                return records.QueueRefusal('E_QUEUE_INVALID', 'subject_unresolved')
            return original(conn, principal, request, *args, **kw)
        return mock.patch.object(records, 'raise_item', refusal)

    def test_refused_row_rolls_back_and_prefix_commits(self):
        one = self.append(); two = self.append(subkind='seat_replaced')
        other = self.append(project=Q)
        with self.refused(two):
            self.assertEqual(self.run_pass(), 2)
        self.assertEqual(self.cursor(), records.Cursor(1, 0, one.row_id))
        self.assertEqual({i['item_id'] for i in self.items()},
                         {self.item(one)['item_id'], self.item(other)['item_id']})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM queue_events').fetchone()[0], 2)
        self.assertEqual(self.notify_count(), 0)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(two)['state'], 'pending')

    def test_refused_row_after_rewind(self):
        one = self.append(); two = self.append(subkind='seat_replaced'); bad = self.append()
        self.append(project=Q)
        records.advance_cursor(self.c, P, 'lifecycle', self.cursor(), 100, anchor_id='lost')
        self.c.commit()
        saved = self.catalogue_copy('ahead.sqlite3')
        with self.refused(bad):
            self.assertEqual(self.run_pass(), 3)
        self.assertEqual(self.cursor(), records.Cursor(2, 0, two.row_id))
        self.assertEqual(self.item(one)['state'], 'pending')
        self.restore_catalogue(saved)
        with self.refused(one):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.cursor(), records.Cursor(0, 0, None))
        self.assertEqual(len(self.items()), 1)
