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
