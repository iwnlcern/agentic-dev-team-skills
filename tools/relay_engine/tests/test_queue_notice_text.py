"""Lifecycle text builders: exact contracts, whole pins and real queue admission."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from relay_engine import lifecycle_outbox as outbox, store_layout
from relay_engine.api_principals import ServicePrincipal
from relay_engine.jcs import jcs_encode
from relay_engine.operator_queue import kinds, records
from relay_engine.tests.test_catalogue import P

SETTLEMENTS = {
 ('settlement_finished','start'): 'After a sudden stop, the program finished an approval change in <project> when it restarted; whoever asked for it was told its outcome was not yet known, or had not yet been answered.',
 ('settlement_undone','start'): 'After a sudden stop, the program undid an unfinished approval change in <project> when it restarted; it is in effect only if it was asked for again and recorded since.',
 ('settlement_undone','running'): 'An approval change in <project> that was not completed has been undone; it is in effect only if it was asked for again and recorded since.',
 ('settlement_undone','stop'): 'When the program stopped, it undid an approval change in <project> that was not completed; it is in effect only if it was asked for again and recorded since.',
 ('settlement_undone','resolve'): "On your answer that this project's store was not put back from a backup, the program discarded an unfinished approval change in <project>; it is in effect only if it was asked for again and recorded since.",
}
EVENTS = {'slip-issued':'permission given','slip-revoked':'permission withdrawn',
 'branch-bound':'branch tied to a bundle','claim-recorded':'approval claim recorded',
 'verification-approved':'review approved','verification-objected':'review objection',
 'waiver':'waiver','reviewer-assigned':'reviewer assigned',
 'verification-interrupted':'review interrupted','reviewer-withheld':'reviewer held'}
SERVICES = {
 'engine_stopped':'v3 was stopped for every project at <time>.',
 'engine_started':'v3 was started again at <time>.',
 'project_stopped':"<project> was stopped at <time>. A stopped project's agents cannot send relays, get approvals, or commit or merge where the engine's permission is needed.",
 'project_started':'<project> was started again at <time>.',
 'engine_was_down':'v3 was not running from <time> to <time>; nothing was lost. Run adt engine status if it stops again.',
 'engine_updated':'v3 was updated from <from> to <to> at <time>.',
 'engine_update_failed':'v3 could not update from <from> to <to>, and went back to <from> at <time>.',
 'engine_restart_requested':"This project's top seat asked v3 to restart at <time>."}
SEATS = {
 'seat_registered':"Seat <seat> was registered in <project>; its agent can now sign in with the seat's key.",
 'seat_replaced':"Seat <seat> in <project> was given a new key; its old key stopped working at once.",
 'seat_stood_down':"Seat <seat> in <project> was stood down; no key signs in to it now."}


SEAL_CAUSES = {
    'folder_unusable': ('none', "the approval-seal folder or this project's files in it are missing or cannot be read or written"),
    'counter_unusable': ('chain', "the approval counter is damaged or does not match this project's ledger"),
    'high_water_unusable': ('chain', "the record of what has been written out is damaged or does not match this project's ledger"),
    'pointer_unset': ('none', "setting up this project's approval counter did not finish"),
    'ledger_chain_mismatch': ('chain', "this project's ledger does not match its approval record, as after a restore from a backup"),
    'pointer_damaged': ('none', "the record of which approval history this project is on is damaged or was replaced from outside v3"),
}
POINTER_SUFFIX = (
    "If v3 cannot read which approval history a project is on, run adopt-restored from the item that names it; "
    "as after any restore through Store's restore command, every go-ahead and approval in the store is then "
    "cancelled and every project held until you end the hold."
)
SEAL_RING = (
    "Approvals cannot be recorded; anything you cancel now stays in force until you cancel it again after a fix. "
    "To bar its agents' approvals now: adt engine stop --project <project>"
)
BACKUP_SENTENCE = (
    "The approval-seal folder on this machine may be included in backups. Approvals record as usual. "
    "If an old backup of this folder alone is put back, approvals may stop being recorded until it is fixed. "
    "Each start tries again to exclude it."
)


def settlement(**changes):
    values=dict(position=1,row_id='row',subkind='settlement_finished',committed_at_ms=10,
                chain_id='chain',runtime_instance_id='run',transaction_ordinal=1,
                events=(outbox.SettledEvent(1,'slip-issued','subject-a'),),occasion='start')
    values.update(changes)
    return outbox.SettlementRow(**values)


def seat(**changes):
    values=dict(position=1,row_id='seat-row',subkind='seat_registered',committed_at_ms=10,
                actor_kind='occupant',actor_occupant_id='actor',team_instance_id='team',
                seat_id='seat',seat_address='build.planner',old_occupant_id=None,new_occupant_id='new')
    values.update(changes)
    return outbox.SeatRow(**values)


def unseat(**changes):
    values=dict(position=1,row_id='unseat-row',subkind='seat_unseated',committed_at_ms=10,
                seat_id='seat',seat_address='build.planner',generation=1,surface='page',
                carrying_seat_id=None,carrying_seat_address=None,instruction_relay_id=None)
    values.update(changes)
    return outbox.UnseatRow(**values)


def merge(**changes):
    values=dict(position=1,row_id='merge-row',subkind='merge_target_changed',committed_at_ms=10,
                slip_id='slip',seat_id='seat',seat_address='build.planner',pr=123,
                merge_commit='a'*40,allowed_target='v3',landed_target='main')
    values.update(changes)
    return outbox.MergeAlarmRow(**values)


class TestNoticeText(unittest.TestCase):
    def setUp(self):
        # Missing builder is the intended first red, rather than a test import error.
        self.assertIsNotNone(importlib.util.find_spec('relay_engine.operator_queue.notice_text'),
                             'lifecycle text builders are absent')
        from relay_engine.operator_queue import notice_text
        self.n = notice_text
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.conn=store_layout.open_catalogue(Path(temp.name)/'store'); self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO project_routes (project_id,name,state,created_at) VALUES (?, 'p', 'active', 0)",(P,)).close()
        self.counter=0

    def raise_text(self, subkind, texts, pins=None, project=P):
        self.counter+=1
        req=records.RaiseRequest('notice',subkind,project,texts,pins or {},
                                 {'row_id':str(self.counter)},str(self.counter))
        return records.raise_item(self.conn,records.InternalWriter(),req,100,enabled=kinds.ENABLED_TAGS)

    def test_notice_subkinds_twenty_eight(self):
        self.assertEqual(len(kinds.NOTICE_SUBKINDS),28)
        self.assertEqual(len(kinds.NO_PROJECT_SUBKINDS),14)
        sql=self.conn.execute("SELECT sql FROM sqlite_master WHERE name='queue_items'").fetchone()[0]
        import re
        block=sql.split('subkind IN (',1)[1].split(')))',1)[0]
        self.assertEqual(set(re.findall("'([^']+)'",block)),kinds.NOTICE_SUBKINDS)
        block=sql.split('project_id TEXT CHECK',1)[1].split('raiser TEXT',1)[0]
        self.assertEqual(set(re.findall("subkind IS '([^']+)'",block)),kinds.NO_PROJECT_SUBKINDS)
        self.assertTrue({'restore_command_started','project_held_5d','project_held_new_folder','report_awaiting_confirmation'}.isdisjoint(kinds.NOTICE_SUBKINDS))

    def test_service_sentences_exact(self):
        self.assertEqual(self.n.SERVICE_SENTENCES,SERVICES)
        for key,size in zip(SERVICES,(43,31,154,38,100,45,75,54)):
            self.assertEqual(len(SERVICES[key].encode()),size)
            self.assertEqual(self.n.SERVICE_SENTENCES[key].count('<time>'),2 if key=='engine_was_down' else 1)

    def test_matrix_settlement_rows(self):
        for subkind in ('settlement_finished','settlement_undone','engine_stopped','engine_started','project_stopped','project_started','engine_was_down','merge_target_changed','seat_unseated'):
            self.assertIsNotNone(kinds.raiser_row('notice',subkind,'console_queue',enabled=frozenset({'pr3'})))
            self.assertIsNone(kinds.raiser_row('notice',subkind,'console_queue',enabled=frozenset({'pr1','pr2'})))
            for principal in ('approval_seal','authority_service','supervision','engine_liveness','xproject_transfer'):
                self.assertIsNone(kinds.raiser_row('notice',subkind,principal,enabled=frozenset({'pr3'})))
            project = None if subkind in kinds.NO_PROJECT_SUBKINDS else P
            self.assertIsInstance(self.raise_text(subkind, {'sentence': 'x'}, project=project), records.RaiseResult)
            for principal in ('console_queue','approval_seal','authority_service','engine_liveness','xproject_transfer'):
                before = self.conn.total_changes
                result = records.raise_item(self.conn,ServicePrincipal(principal),
                    records.RaiseRequest('notice',subkind,project,{'sentence':'x'},{},'key','direct'),1,enabled=kinds.ENABLED_TAGS)
                self.assertEqual(result.cause_kind,'kind_not_raisable')
                self.assertEqual(self.conn.total_changes,before)
        for subkind in ('approval_seal_unusable','approval_seal_backup_included'):
            row = kinds.raiser_row('notice', subkind, 'approval_seal', enabled=kinds.ENABLED_TAGS)
            self.assertIsNotNone(row)
            self.assertEqual(row.enabled_in, 'pr3')

    def test_settlement_sentences_exact(self):
        self.assertEqual(self.n.SETTLEMENT_SENTENCES,SETTLEMENTS)
        for (subkind,occasion),sentence in SETTLEMENTS.items():
            self.assertEqual(self.n.settlement_texts(settlement(subkind=subkind,occasion=occasion))['sentence'],sentence)
            self.assertLessEqual(len(sentence.replace('<project>','').encode()),300)
        for occasion in ('running','stop','resolve'):
            with self.assertRaises(KeyError):self.n.settlement_texts(settlement(occasion=occasion))

    def test_subject_lists_events_in_order(self):
        row=settlement(events=tuple(outbox.SettledEvent(i,k,s) for i,k,s in
            [(1,'slip-issued','a'),(2,'branch-bound','b'),(3,'claim-recorded','c')]))
        subject=self.n.settlement_texts(row)['subject']
        # Distinct subject ids avoid substring collisions with the words.
        subject=self.n.settlement_texts(replace(row,events=tuple(replace(e,subject='subject-'+str(e.number)) for e in row.events)))['subject']
        self.assertLess(subject.index('permission given'),subject.index('subject-1'))
        self.assertLess(subject.index('subject-1'),subject.index('branch tied to a bundle'))
        self.assertLess(subject.index('subject-2'),subject.index('approval claim recorded'))
        self.assertIn('subject-3',subject)

    def test_undone_reviewer_phrase(self):
        row=settlement(subkind='settlement_undone',events=(outbox.SettledEvent(1,'reviewer-assigned','s'),))
        phrase='undoing it left the earlier reviewer, if any, assigned at that time'
        self.assertIn(phrase,self.n.settlement_texts(row)['subject'])
        self.assertNotIn(phrase,self.n.settlement_texts(replace(row,subkind='settlement_finished'))['subject'])

    def test_undone_slip_phrase(self):
        row=settlement(subkind='settlement_undone',events=(outbox.SettledEvent(1,'slip-issued','s'),outbox.SettledEvent(2,'reviewer-assigned','r')))
        phrase="undoing it left the seat's earlier permission for that bundle and repository, if any, in force at that time"
        text=self.n.settlement_texts(row)['subject']
        self.assertIn(phrase,text); self.assertIn('undoing it left the earlier reviewer',text)
        self.assertNotIn(phrase,self.n.settlement_texts(replace(row,subkind='settlement_finished'))['subject'])

    def test_settlement_event_phrases(self):
        self.assertEqual(self.n.EVENT_PHRASES,EVENTS)
        for kind,phrase in EVENTS.items():
            for subkind in ('settlement_finished','settlement_undone'):
                text=self.n.settlement_texts(settlement(subkind=subkind,events=(outbox.SettledEvent(1,kind,'subject-id'),)))['subject']
                self.assertIn(phrase,text)
                if kind!='waiver':self.assertNotIn(kind,text)
        removed={key:value for key,value in self.n.EVENT_PHRASES.items() if key not in ('verification-interrupted','reviewer-withheld')}
        with mock.patch.dict(self.n.EVENT_PHRASES,removed,clear=True):
            for key in ('verification-interrupted','reviewer-withheld'):
                text=self.n.settlement_texts(settlement(events=(outbox.SettledEvent(1,key,'s'),)))['subject']
                self.assertIn(key,text)

    def test_unknown_event_kind_shown_literally(self):
        self.assertIn('slip-renewed',self.n.settlement_texts(settlement(events=(outbox.SettledEvent(1,'slip-renewed','s'),)))['subject'])

    def test_subject_cut_keeps_every_event_in_pins(self):
        events=tuple(outbox.SettledEvent(i,'claim-recorded','subject-'+str(i)+'x'*40) for i in range(1,301))
        row=settlement(events=events);subject=self.n.settlement_texts(row)['subject']
        self.assertLessEqual(len(subject.encode()),4096)
        import re
        count=int(re.search(r'and (\d+) more, listed when you open it$',subject).group(1))
        shown=sum('subject-'+str(i)+'x'*40 in subject for i in range(1,301))
        self.assertEqual(count,300-shown);self.assertGreater(shown,0)
        self.assertEqual(len(self.n.settlement_pins(row)['events']),300)
        self.assertNotIn('and ',self.n.settlement_texts(replace(row,events=events[:3]))['subject'])

    def test_caused_by(self):
        for occasion in ('resolve','start','running','stop'):
            self.assertEqual(self.n.settlement_texts(settlement(subkind='settlement_undone',occasion=occasion))['caused_by'], 'your answer' if occasion=='resolve' else 'the program')

    def test_texts_name_no_project(self):
        for sentence in (*SETTLEMENTS.values(),*self.n.SEAT_SENTENCES.values()):
            self.assertEqual(sentence.count('<project>'),1);self.assertNotIn(P,sentence)
        for key,text in self.n.SERVICE_SENTENCES.items():
            self.assertEqual(text.count('<project>'),int(key in ('project_started','project_stopped')))
        builders = [self.n.seal_texts(cause, chain) for cause, (chain, _) in SEAL_CAUSES.items()]
        backup = self.n.backup_texts()
        self.assertEqual(backup, dict(sentence=BACKUP_SENTENCE, subject='the approval-seal folder',
                                      caused_by='the approval seal'))
        self.assertEqual(self.n.backup_pins(), {'folder': 'approval_seal'})
        for texts in builders:
            self.assertEqual(texts['sentence'].count('<project>'), 1)
        for texts in [*builders, backup]:
            for value in texts.values():
                self.assertNotIn(P, value)
                self.assertNotIn('billing-service', value)
        self.assertNotIn('<project>', backup['sentence'])

    def test_seat_sentences_exact(self):
        self.assertEqual(self.n.SEAT_SENTENCES,SEATS)
        for key,size in zip(SEATS,(72,67,52)):
            self.assertEqual(len(SEATS[key].replace('<seat>','').replace('<project>','').encode()),size)
        for actor,cause in [('occupant',"the project's top seat"),('operator','you'),('program','the program')]:
            text=self.n.seat_texts(seat(actor_kind=actor));self.assertEqual(text['sentence'],SEATS['seat_registered'].replace('<seat>','build.planner'));self.assertEqual(text['caused_by'],cause)
        self.assertNotIn('old_occupant_id',self.n.seat_pins(seat()))
        self.assertNotEqual(self.n.seat_texts(seat(subkind='seat_stood_down'))['sentence'],SEATS['seat_registered'])

    def test_unseat_sentences_exact(self):
        # Catches any changed operator wording, even at the same byte length.
        expected = {
            'terminal': '<seat> was unseated from the terminal at <time>. Its window can no longer send relays, commit or merge as that agent until it signs in again.',
            'page': "<seat> was unseated from v3's page at <time>. Its window can no longer send relays, commit or merge as that agent until it signs in again.",
            'instructed': '<seat> was unseated at <time> by the top-level planner on your instruction <relay id>. Its window can no longer send relays, commit or merge as that agent until it signs in again.',
        }
        self.assertEqual(self.n.UNSEAT_SENTENCES,expected)
        for form,size in [('terminal',129),('page',126),('instructed',157)]:
            text=self.n.UNSEAT_SENTENCES[form]
            self.assertEqual(len(text.replace('<seat>','').replace('<time>','').replace('<relay id>','').encode()),size)
            row=unseat(surface=form if form!='instructed' else None,instruction_relay_id='relay' if form=='instructed' else None)
            got=self.n.unseat_texts(row)['sentence'];self.assertIn('build.planner',got);self.assertIn('<time>',got)
            self.assertEqual(got,expected[form].replace('<seat>','build.planner').replace('<relay id>','relay'))
            self.assertNotIn('<seat>',got)
            if form=='instructed':self.assertIn('relay',got)
        self.assertNotEqual(self.n.unseat_texts(unseat(surface='terminal')),self.n.unseat_texts(unseat()))

    def test_merge_alarm_text_exact(self):
        expected='PR #123 was merged onto main, but its go-ahead allowed only v3. If this merge should not stand, revert its merge commit from your own terminal: '+'a'*40
        self.assertEqual(self.n.merge_alarm_texts(merge())['sentence'],expected)
        self.assertEqual(len(expected.encode()),184)
        self.assertEqual(len(self.n.merge_alarm_texts(merge(merge_commit='a'*64))['sentence'].encode()),208)
        self.assertEqual(self.n.merge_alarm_texts(merge())['caused_by'],'the program')
        self.assertIn('<project>',self.n.merge_alarm_texts(merge(landed_target='<project>'))['sentence'])

    def test_restart_texts(self):
        row=outbox.RestartRow(1,'r','engine_restart_requested',10,'actor','seat','build.planner')
        self.assertEqual(self.n.restart_texts(row),{'sentence':SERVICES['engine_restart_requested']})
        pins=self.n.restart_pins(row);self.assertEqual(pins['at'],10);self.assertEqual(pins['row_id'],'r');self.assertEqual(pins['seat_id'],'seat')

    def test_builder_texts_shape(self):
        rows=[(settlement(),self.n.settlement_texts,self.n.settlement_pins),
              (seat(),self.n.seat_texts,self.n.seat_pins),
              (merge(),self.n.merge_alarm_texts,self.n.merge_alarm_pins)]
        rows += [(unseat(surface=f),self.n.unseat_texts,self.n.unseat_pins) for f in ('page','terminal')]
        rows += [(unseat(surface=None,instruction_relay_id='instruction'),self.n.unseat_texts,self.n.unseat_pins),
                 (outbox.RestartRow(1,'restart','engine_restart_requested',10,'actor','seat','build.planner'),self.n.restart_texts,self.n.restart_pins)]
        for row,build,pins in rows:
            texts=build(row);self.assertIn('sentence',texts)
            self.assertLessEqual(set(texts),{'sentence','subject','caused_by','ring_text'})
            for value in texts.values():self.assertTrue(1<=len(value.encode())<=4096);self.assertNotIn('\0',value)
            self.assertIsInstance(self.raise_text(row.subkind,texts,pins(row)),records.RaiseResult)
            before=self.conn.total_changes
            result=self.raise_text(row.subkind,dict(texts,title='bad'),pins(row))
            self.assertEqual(result.cause_kind,'subject_unresolved');self.assertEqual(self.conn.total_changes,before)

        seal = ServicePrincipal('approval_seal')
        subjects = [
            ('approval_seal_unusable', P, {'chain_id': chain, 'cause': cause},
             self.n.seal_texts(cause, chain))
            for cause, (chain, _) in SEAL_CAUSES.items()
        ]
        subjects.append(('approval_seal_backup_included', None, 'backup_exclusion', self.n.backup_texts()))
        for subkind, project, key, texts in subjects:
            self.counter += 1
            pins = key if project is not None else self.n.backup_pins()
            self.assertLessEqual(set(texts), {'sentence', 'subject', 'caused_by', 'ring_text'})
            for value in texts.values():
                self.assertTrue(1 <= len(value.encode()) <= 4096)
                self.assertNotIn('\0', value)
            request = records.RaiseRequest('notice', subkind, project, texts, pins, key, str(self.counter))
            self.assertIsInstance(records.raise_item(self.conn, seal, request, 100,
                                                     enabled=kinds.ENABLED_TAGS), records.RaiseResult)

    def test_seal_sentences_exact(self):
        self.assertEqual(self.n.CAUSE_PHRASES, {cause: phrase for cause, (_, phrase) in SEAL_CAUSES.items()})
        for cause, (chain, phrase) in SEAL_CAUSES.items():
            with self.subTest(cause=cause):
                expected = (f"Approvals in <project> cannot be recorded: {phrase}. "
                            "Work already approved carries on; new permissions, withdrawals, reviews, "
                            "waivers, reviewer changes and the first commit on a new branch wait until it is fixed.")
                size = len(expected.replace('<project>', '').encode())
                self.assertTrue(252 <= size <= 293)
                if cause == 'pointer_damaged':
                    expected += ' ' + POINTER_SUFFIX
                    self.assertEqual(len(expected.replace('<project>', '').encode()), 559)
                texts = self.n.seal_texts(cause, chain)
                self.assertEqual(texts['sentence'], expected)
                self.assertEqual(texts['ring_text'], SEAL_RING)
                self.assertEqual(len(texts['ring_text'].replace('<project>', '').encode()), 168)
                self.assertEqual(texts['caused_by'], 'the approval seal')
                if chain != 'none':
                    self.assertIn(chain, texts['subject'])

    def test_fit_value(self):
        for value,want in [('a'*512,'a'*512),('a'*513,'a'*509+'…'),('a'*508+'é'+'a'*3,'a'*508+'…'),('a\0b','a�b')]:
            self.assertEqual(self.n.fit_value(value),want)

    def test_fit_integers(self):
        for n in (2**53-1,-(2**53-1),2**53,-2**53,10**18):
            want=n if abs(n)<2**53 else str(n)
            self.assertEqual(self.n.fit_integers(n),want)
            value={'x':[n,{'n':n}],'b':True}
            got=self.n.fit_integers(value);self.assertEqual(got,{'x':[want,{'n':want}],'b':True});jcs_encode(got)
            self.assertIs(got['b'],True);self.assertEqual(value['x'][0],n)
        with self.assertRaises(ValueError):jcs_encode({'x':[2**53]})

    def test_settlement_free_values_fit(self):
        row=settlement(subkind='settlement_undone',events=(outbox.SettledEvent(1,'claim-recorded','a\0b'),outbox.SettledEvent(2,'x\0','s')))
        texts=self.n.settlement_texts(row);pins=self.n.settlement_pins(row)
        self.assertIn('a�b',texts['subject']);self.assertIn('x�',texts['subject']);self.assertIn('a\\u0000b',json.dumps(pins))
        self.assertIsInstance(self.raise_text(row.subkind,texts,pins),records.RaiseResult)
        with mock.patch.object(self.n,'fit_value',lambda v:v):
            self.assertEqual(self.raise_text(row.subkind,self.n.settlement_texts(row),pins).cause_kind,'subject_unresolved')
