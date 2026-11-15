"""Closed queue policy and offered-choice parsing before any runtime wiring."""
from dataclasses import astuple
import itertools
import unittest
from unittest import mock

from relay_engine.operator_queue import kinds


WORKERS = ('queue_reconcile', 'queue_question_ingest', 'queue_lifecycle_ingest', 'engine_liveness', 'store_backup_daily')
EXPECTED_MATRIX = (
    ('question', None, 'internal', 'console_queue', 'pr1', None),
    ('cross_project_permission', None, 'queue.raise', 'xproject_transfer', 'pr2', None),
    ('merge_go_ahead', None, 'queue.raise', 'authority_service', 'pr2', None),
    ('verification_held', None, 'queue.raise', 'authority_service', 'pr2', None),
    ('notice', 'doorbell_off', 'queue.raise', 'engine_liveness', 'pr5', None),
    ('notice', 'seat_registered', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'seat_replaced', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'seat_stood_down', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'engine_restart_requested', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'engine_updated', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'engine_update_failed', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'notifier_unhealthy', 'internal', 'console_queue', 'pr1', frozenset({'notifier'})),
    ('notice', 'notifier_unhealthy', 'queue.raise', 'supervision', 'pr1', frozenset({'notify_worker'})),
    ('notice', 'worker_stalled', 'queue.raise', 'supervision', 'pr1', frozenset(WORKERS)),
    ('notice', 'operator_act_carried', 'internal', 'console_queue', 'pr3c', None),
    ('notice', 'answer_faulted', 'internal', 'console_queue', 'pr1', None),
    ('notice', 'backup_missed', 'queue.raise', 'supervision', 'pr1', frozenset({'daily'})),
    ('notice', 'settlement_finished', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'settlement_undone', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'engine_stopped', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'engine_started', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'project_stopped', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'project_started', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'engine_was_down', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'merge_target_changed', 'internal', 'console_queue', 'pr3', None),
    ('notice', 'seat_unseated', 'internal', 'console_queue', 'pr3', None),

)
ALL_TAGS = frozenset({'pr1','pr2','pr3','pr3c','pr4','pr5','pr6'})
NOTICE_NAMES = frozenset({'seat_registered','seat_replaced','seat_stood_down','seat_unseated',
    'engine_restart_requested','engine_updated','engine_update_failed','doorbell_off',
    'notifier_unhealthy','operator_act_carried','worker_stalled','bridge_refused',
    'panel_key_enrolled','answer_faulted','instruction_carried','seat_record_missing','seat_ambiguous','backup_missed',
    'settlement_finished','settlement_undone','engine_stopped','engine_started',
    'project_stopped','project_started','engine_was_down','merge_target_changed',
    'approval_seal_unusable','approval_seal_backup_included'})


class TestQueueKinds(unittest.TestCase):
    def test_matrix_is_closed(self):
        self.assertEqual(tuple(astuple(row) for row in kinds.RAISER_MATRIX), EXPECTED_MATRIX)
        expected = {(r[0],r[1],r[3]) for r in EXPECTED_MATRIX}
        for kind, subkind, principal in itertools.product(
                ('question','cross_project_permission','merge_go_ahead','verification_held','notice','bad'),
                (None,*NOTICE_NAMES,'bad'),
                ('console_queue','xproject_transfer','authority_service','engine_liveness',
                 'supervision','operator_bridge','act_lookup','seat','bad')):
            with self.subTest(kind=kind,subkind=subkind,principal=principal):
                row = kinds.raiser_row(kind,subkind,principal,enabled=ALL_TAGS)
                self.assertEqual(row is not None,(kind,subkind,principal) in expected)
        domains = {
            'KINDS': {'question','cross_project_permission','merge_go_ahead','verification_held','notice'},
            'NOTICE_SUBKINDS': NOTICE_NAMES,
            'STATES': {'pending','applying','resolved','superseded','refused','faulted','dismissed','withdrawn','cleared','retired'},
            'EVENT_KINDS': {'raised','answered','applied','superseded','refused','faulted','withdrawn','opened','dismissed','dismissed_all','cleared','clear_requested','condition_closed','retired'},
            'NOTIFICATION_STATES': {'pending','delivered','retired'},
            'RETIRE_REASONS': {'project_undone'},
            'NO_PROJECT_SUBKINDS': {'notifier_unhealthy','worker_stalled','bridge_refused','panel_key_enrolled','engine_updated','engine_update_failed','instruction_carried','seat_record_missing','seat_ambiguous','backup_missed','engine_stopped','engine_started','engine_was_down','approval_seal_backup_included'},
            'NO_CLEAR_SUBKINDS': {'bridge_refused','panel_key_enrolled','instruction_carried','seat_record_missing','seat_ambiguous','merge_target_changed'},
            'OPERATOR_CLOSED_SUBKINDS': {'instruction_carried'},
        }
        for name, expected_set in domains.items():
            self.assertIsInstance(getattr(kinds,name),frozenset)
            self.assertEqual(getattr(kinds,name), expected_set)
        self.assertEqual(kinds.STALL_WORKERS,WORKERS)

    def test_rows_enabled_by_pr(self):
        self.assertEqual(kinds.ENABLED_TAGS, frozenset({'pr1','pr3'}))
        for expected in EXPECTED_MATRIX:
            row = kinds.raiser_row(expected[0],expected[1],expected[3],enabled=frozenset({'pr1'}))
            self.assertEqual(row is not None,expected[4]=='pr1')
            row = kinds.raiser_row(expected[0],expected[1],expected[3],enabled=ALL_TAGS)
            self.assertEqual(astuple(row),expected)
            self.assertIsNone(kinds.raiser_row(expected[0],expected[1],expected[3],enabled=frozenset()))

    def test_ring_set(self):
        expected = frozenset({'question','cross_project_permission','merge_go_ahead','verification_held',
                             'notice/doorbell_off','notice/instruction_carried','notice/worker_stalled','notice/bridge_refused','notice/answer_faulted','notice/backup_missed','notice/merge_target_changed'})
        self.assertEqual(kinds.RING_SET,expected)
        for member in expected:
            parts=member.split('/')
            self.assertTrue(kinds.rings(parts[0],parts[1] if len(parts)>1 else None))
        for subkind in ('seat_registered','notifier_unhealthy','operator_act_carried'):
            self.assertFalse(kinds.rings('notice',subkind))
        with mock.patch.object(kinds,'RING_SET',expected|{'notice/seat_registered'}):
            self.assertTrue(kinds.rings('notice','seat_registered'))

    def test_row_keys(self):
        for i,row in enumerate(kinds.RAISER_MATRIX):
            self.assertEqual(row.keys, EXPECTED_MATRIX[i][5])
        self.assertEqual(kinds.RAISER_MATRIX[11].keys,{'notifier'})
        self.assertEqual(kinds.RAISER_MATRIX[12].keys,{'notify_worker'})
        self.assertEqual(kinds.RAISER_MATRIX[13].keys,set(WORKERS))

    def test_no_direct_raise_of_instructed_act(self):
        rows=[r for r in kinds.RAISER_MATRIX if r.subkind=='operator_act_carried']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].entry,'internal')
        for name in ('authority_service','engine_liveness','xproject_transfer','supervision',
                     'operator_bridge','act_lookup','approval_seal'):
            self.assertIsNone(kinds.raiser_row('notice','operator_act_carried',name,enabled=ALL_TAGS))
        self.assertEqual(kinds.raiser_row('notice','operator_act_carried','console_queue',enabled=ALL_TAGS),rows[0])

    def test_fixed_text_reads_the_table(self):
        fixture={('worker_stalled','queue_reconcile'):'a',('bridge_refused','*'):'b'}
        with mock.patch.object(kinds,'FIXED_TEXTS',fixture):
            self.assertEqual(kinds.fixed_text('worker_stalled','queue_reconcile'),'a')
            for key in ('run','other'):
                self.assertEqual(kinds.fixed_text('bridge_refused',key),'b')
            self.assertIsNone(kinds.fixed_text('worker_stalled','queue_notify'))
            self.assertIsNone(kinds.fixed_text('seat_registered','key'))
        with mock.patch.object(kinds,'FIXED_TEXTS',{}):
            self.assertIsNone(kinds.fixed_text('worker_stalled','queue_reconcile'))
            self.assertIsNone(kinds.fixed_text('bridge_refused','run'))
        with self.assertRaises(TypeError):
            kinds.PIN_TOKENS['worker_stalled']=('x','y')

    def test_answer_surface_table(self):
        self.assertEqual(kinds.SURFACES,{'page','frank_panel'})
        cells=[('question',None),('cross_project_permission','allow_always'),
               ('cross_project_permission','allow_once'),('cross_project_permission','deny'),
               ('merge_go_ahead','go_ahead'),('merge_go_ahead','go_ahead_without_review'),
               ('verification_held','waive'),('notice','open'),('notice','dismiss'),('notice','dismiss_all')]
        for terminal in (False,True):
            surfaces=frozenset({'page','frank_panel'}|({'terminal'} if terminal else set()))
            with mock.patch.object(kinds,'SURFACES',surfaces):
                for conformed in (False,True):
                    for kind,choice in cells:
                        expected=set(surfaces)
                        if kind=='merge_go_ahead' and not conformed:expected.discard('frank_panel')
                        self.assertEqual(kinds.answer_surfaces(kind,choice,frank_conformed=conformed),expected)
        self.assertEqual(kinds.fixed_choices('cross_project_permission'),('allow_always','allow_once','deny'))
        self.assertEqual(kinds.fixed_choices('merge_go_ahead'),('go_ahead','go_ahead_without_review'))
        self.assertEqual(kinds.fixed_choices('verification_held'),('waive',))
        self.assertEqual(kinds.fixed_choices('question'),())

    def test_choices_block_valid(self):
        self.assertEqual(kinds.parse_choices(b'Which?\nChoices:\n- on my way\n- not now\n'),
                         (('on my way','not now'),None))
        self.assertEqual(kinds.parse_choices('Choices:\n- café\n'.encode()),(('café',),None))
        self.assertEqual(kinds.parse_choices(b'Choices:\n- '+b'a'*120+b'\n'),(('a'*120,),None))

    def test_choices_block_edges(self):
        cases=[(b'Choices:\r\n- yes\r\n','crlf'),
               (b'Choices:\n- yes\n\n','trailing_blank_line'),
               (b'Choices:\n'+b'- x\n'*9,'too_many_labels'),
               (b'Choices:\n- '+b'x'*121+b'\n','label_too_long'),
               (b'Choices:\n- \xc3\n','label_not_utf8'),
               (b'Choices:\n- \n','empty_label')]
        for body,reason in cases:
            with self.subTest(reason=reason):self.assertEqual(kinds.parse_choices(body),((),reason))
        for body in (b'No choices',b'no block\xff',b''):
            self.assertEqual(kinds.parse_choices(body),((),None))

    def test_condition_key_is_canonical(self):
        self.assertEqual(kinds.condition_key('question',None,{'b':2,'a':1}),'{"a":1,"b":2}')
        self.assertEqual(kinds.condition_key('question',None,{'a':1,'b':2}),'{"a":1,"b":2}')
        self.assertEqual(kinds.condition_key('notice','notifier_unhealthy','notifier'),'"notifier"')

    def test_act_lookup_args_valid(self):
        digest='a'*64
        missing={'seat_id':'s-a','session_digest':digest}; ambiguous={'host':'codex','session_digest':digest}
        self.assertTrue(kinds.act_lookup_args_valid('seat_record_missing',missing,{'seat_id':'s-a'}))
        for host in ('codex','claude-code'):
            self.assertTrue(kinds.act_lookup_args_valid('seat_ambiguous',dict(ambiguous,host=host),{}))
        cases=[('seat_record_missing',dict(missing,extra=1),{'seat_id':'s-a'}),
               ('seat_record_missing',{'seat_id':'s-a'},{'seat_id':'s-a'}),
               ('seat_record_missing',{'session_digest':digest},{'seat_id':'s-a'}),
               ('seat_record_missing',missing,{'seat_id':'other'}),
               ('seat_record_missing',missing,{'seat_id':'s-a','extra':1}),
               ('seat_ambiguous',dict(ambiguous,extra=1),{}),
               ('seat_ambiguous',{'host':'codex'},{}),
               ('seat_ambiguous',{'session_digest':digest},{}),
               ('seat_ambiguous',dict(ambiguous,host='other'),{}),
               ('seat_ambiguous',ambiguous,{'seat_id':'s-a'}),
               ('worker_stalled',missing,{'seat_id':'s-a'})]
        for value in ('','a\x00b','x'*257):
            cases.append(('seat_record_missing',dict(missing,seat_id=value),{'seat_id':value}))
        for value in ('A'*64,'a'*63):
            cases.append(('seat_record_missing',dict(missing,session_digest=value),{'seat_id':'s-a'}))
            cases.append(('seat_ambiguous',dict(ambiguous,session_digest=value),{}))
        for args in cases:
            with self.subTest(args=args):self.assertFalse(kinds.act_lookup_args_valid(*args))
        # A value-only validator would accept this extra-member key.
        permissive=lambda key: key.get('seat_id')=='s-a' and key.get('session_digest')==digest
        self.assertTrue(permissive(dict(missing,extra=1)))
        self.assertFalse(kinds.act_lookup_args_valid('seat_record_missing',dict(missing,extra=1),{'seat_id':'s-a'}))


if __name__ == '__main__':
    unittest.main()
