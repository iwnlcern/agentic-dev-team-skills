"""File-backed notifier delivery, durable failure health, and inert argv values."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from relay_engine import api_principals, store_layout
from relay_engine.api_principals import ServicePrincipal
import relay_engine.operator_queue.notifier as notifier
from relay_engine.operator_queue import kinds, records
from relay_engine.tests.test_catalogue import P, Q
from relay_engine.tests.test_queue_records import ALL, FileJobPort


MARK = " … [cut: the whole message is on v3's page]"
PROGRAMS = ('osascript', 'terminal-notifier', 'notify-send')


class TrackedPort(FileJobPort):
    active = False

    def run(self, *args, **kwargs):
        self.active = True
        try:
            return super().run(*args, **kwargs)
        finally:
            self.active = False


class TestQueueNotifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        self.conn = store_layout.open_catalogue(self.root)
        self.addCleanup(lambda: self.conn.close())
        for project, name in ((P, 'billing-service'), (Q, 'q')):
            self.conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?,?,?,?)',
                              (project, name, 'active', 0)).close()
        self.conn.commit()
        self.port = TrackedPort(self.root)
        self.sequence = 0

    def raise_it(self, kind='question', subkind=None, project=P, actor=None, texts=None):
        self.sequence += 1
        principal = records.InternalWriter()
        key = 'key-' + str(self.sequence)
        if subkind == 'worker_stalled':
            with mock.patch.object(api_principals, 'SERVICE_NAMES',
                                   (*api_principals.SERVICE_NAMES, 'supervision')):
                principal = ServicePrincipal('supervision')
            key = 'queue_reconcile'
        elif subkind == 'notifier_unhealthy':
            key = 'notifier'
        elif kind == 'merge_go_ahead':
            principal = ServicePrincipal('authority_service')
        elif subkind == 'doorbell_off':
            principal = ServicePrincipal('engine_liveness')
        texts = texts or ({'subject': 'Question ' + str(self.sequence), 'body': 'Body'}
                          if kind == 'question' else {'sentence': 'Notice.'})
        request = records.RaiseRequest(kind, subkind, project, texts, {}, key,
                                       'op-' + str(self.sequence), actor)
        result = records.raise_item(self.conn, principal, request, 100 + self.sequence,
                                    enabled=ALL)
        self.assertIsInstance(result, records.RaiseResult)
        self.conn.commit()
        return result, principal, request

    def run_pass(self, deliver, now=1000, **kwargs):
        return notifier.notify_pass(self.port, deliver, now, notifier='notify-send', **kwargs)

    def row(self, item):
        return self.conn.execute('SELECT state,attempts,next_attempt_at_ms,last_error_class '
                                 'FROM queue_notifications WHERE item_id=?',
                                 (item.item_id,)).fetchone()

    def health(self):
        return self.conn.execute('SELECT consecutive_failures FROM queue_notifier_health '
                                 'WHERE id=1').fetchone()[0]

    def unhealthy(self):
        return self.conn.execute("SELECT item_id,state,condition_key FROM queue_items "
                                 "WHERE subkind='notifier_unhealthy' ORDER BY raised_position").fetchall()

    def delivery_failure(self, argv):
        raise OSError('display unavailable')

    def reopen(self):
        self.conn.close()
        self.conn = store_layout.open_catalogue(self.root)

    def test_backup_missed_notice(self):
        with mock.patch.object(api_principals, 'SERVICE_NAMES',
                               (*api_principals.SERVICE_NAMES, 'supervision')):
            supervision = ServicePrincipal('supervision')
        sentence = ('v3 has made no complete daily backup of its store yesterday or today, '
                    'so putting the store back from a backup could lose more than a day of work. '
                    'Run adt engine status.')
        request = records.RaiseRequest('notice', 'backup_missed', None,
                                       {'sentence': 'Other words.'}, {}, 'daily', 'backup-1')
        first = records.raise_item(self.conn, supervision, request, 100, enabled=ALL)
        self.assertIsInstance(first, records.RaiseResult)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM queue_items').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM queue_conditions '
                                           'WHERE closed_at_ms IS NULL').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM queue_notifications').fetchone()[0], 1)
        stored = self.conn.execute('SELECT texts,condition_key,pins,actor_address FROM queue_items '
                                   'WHERE item_id=?', (first.item_id,)).fetchone()
        self.assertEqual((json.loads(stored[0]), stored[1], json.loads(stored[2]), stored[3]),
                         ({'sentence': sentence}, '"daily"', {}, None))
        self.assertEqual(notifier.due_notifications(self.conn, 1000, 10),
                         [notifier.Notification(first.item_id, 1, 'v3: supervision', sentence)])
        before = tuple(self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                       for table in ('queue_events', 'queue_notifications'))
        second = records.raise_item(self.conn, supervision,
                                    replace(request, operation_key='backup-2'), 101, enabled=ALL)
        self.assertIsInstance(second, records.RaiseResult)
        self.assertEqual(second.item_id, first.item_id)
        self.assertEqual(tuple(self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                               for table in ('queue_events', 'queue_notifications')), before)
        self.assertEqual([item.item_id for item in records.open_condition_items(
            self.conn, supervision, None)], [first.item_id])
        cleared = records.clear_condition(self.conn, supervision, first.item_id,
                                           'notice', 'backup_missed', None, 'daily', 102)
        self.assertEqual(cleared, records.ClearResult('cleared'))
        self.assertEqual(self.conn.execute('SELECT state FROM queue_items WHERE item_id=?',
                                           (first.item_id,)).fetchone()[0], 'cleared')
        self.assertIsNotNone(self.conn.execute('SELECT closed_at_ms FROM queue_conditions '
                                               'WHERE item_id=?', (first.item_id,)).fetchone()[0])
        self.assertEqual(records.open_condition_items(self.conn, supervision, None), [])
        controls = (
            (records.InternalWriter(), replace(request, operation_key='backup-internal'), 'kind_not_raisable'),
            (supervision, replace(request, project_id=P, operation_key='backup-project'), 'subject_unresolved'),
            (supervision, replace(request, condition_key='weekly', operation_key='backup-weekly'), 'subject_unresolved'),
        )
        for principal, control, reason in controls:
            with self.subTest(operation_key=control.operation_key):
                changes = self.conn.total_changes
                result = records.raise_item(self.conn, principal, control, 103, enabled=ALL)
                self.assertEqual(result, records.QueueRefusal('E_QUEUE_INVALID', reason))
                self.assertEqual(self.conn.total_changes, changes)

    def test_backup_stall_key(self):
        with mock.patch.object(api_principals, 'SERVICE_NAMES',
                               (*api_principals.SERVICE_NAMES, 'supervision')):
            supervision = ServicePrincipal('supervision')
        sentence = ('v3 has stopped making its daily backup, so your store may go without one. '
                    'Run adt engine status.')
        request = records.RaiseRequest('notice', 'worker_stalled', None,
                                       {'sentence': 'Other words.'}, {}, 'store_backup_daily', 'stall-1')
        result = records.raise_item(self.conn, supervision, request, 100, enabled=ALL)
        self.assertIsInstance(result, records.RaiseResult)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM queue_items').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM queue_notifications').fetchone()[0], 1)
        stored = self.conn.execute('SELECT texts FROM queue_items WHERE item_id=?',
                                   (result.item_id,)).fetchone()[0]
        self.assertEqual(json.loads(stored), {'sentence': sentence})
        changes = self.conn.total_changes
        refused = records.raise_item(self.conn, supervision,
                                      replace(request, condition_key='queue_notify',
                                              operation_key='stall-notify'), 101, enabled=ALL)
        self.assertEqual(refused, records.QueueRefusal('E_QUEUE_INVALID', 'subject_unresolved'))
        self.assertEqual(self.conn.total_changes, changes)

    def test_unhealthy_raised_at_exactly_five(self):
        from relay_engine.api_principals import OperatorPrincipal
        from relay_engine.operator_queue import operations
        self.raise_it()
        def receipt_count():
            return records._one(self.conn, "SELECT COUNT(*) FROM queue_receipts "
                "WHERE principal_kind='service' AND principal_id='console_queue' "
                "AND operation='queue.raise' AND operation_key LIKE 'notifier-failure:%'")[0]
        for n in range(5):
            self.run_pass(self.delivery_failure, 1000 + n * 1000000)
        first = self.unhealthy()[0][0]
        revision = records._one(self.conn, 'SELECT revision FROM queue_items WHERE item_id=?', (first,))[0]
        dismissed = operations.dismiss(self.conn, OperatorPrincipal('page'), first, revision,
                                       'dismiss-unhealthy', 4002000)
        self.assertNotIsInstance(dismissed, records.QueueRefusal)
        self.conn.commit()
        before = (receipt_count(), records._one(self.conn, 'SELECT COUNT(*) FROM queue_events')[0])
        later = []
        for n in (5, 6):
            self.run_pass(self.delivery_failure, 1000 + n * 1000000)
            later.append((receipt_count(), records._one(self.conn, 'SELECT COUNT(*) FROM queue_events')[0]))
        self.assertEqual(receipt_count(), 1)
        self.assertEqual(later, [before, before])
        self.assertEqual(self.unhealthy(), [(first, 'dismissed', '"notifier"')])
        self.assertEqual(self.health(), 7)
        self.run_pass(lambda argv: None, 7001000)
        self.assertEqual(self.health(), 0)
        self.assertEqual(records.open_condition_items(self.conn, records.InternalWriter(), None), [])
        self.assertIsNotNone(records._one(self.conn,
            'SELECT closed_at_ms FROM queue_conditions WHERE item_id=?', (first,))[0])
        self.raise_it()
        for n in range(5):
            self.run_pass(self.delivery_failure, 8001000 + n * 1000000)
        rows = self.unhealthy()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], (first, 'dismissed', '"notifier"'))
        self.assertNotEqual(rows[1][0], first)
        self.assertEqual(rows[1][1:], ('pending', '"notifier"'))
        self.assertEqual(receipt_count(), 2)

    def test_unhealthy_sentence_from_table(self):
        self.raise_it()
        with mock.patch.object(records, 'raise_item', wraps=records.raise_item) as raising:
            for n in range(5):
                self.run_pass(self.delivery_failure, 1000 + n * 1000000)
        self.assertEqual(raising.call_count, 1)
        request = raising.call_args.args[2]
        self.assertEqual(request.texts, {'sentence': kinds.fixed_text('notifier_unhealthy', 'notifier')})
        self.assertFalse(hasattr(notifier, '_UNHEALTHY_TEXT'))

    def test_argv_stops_option_parsing(self):
        note = notifier.Notification('item', 1, '-e', '-e do shell script "x"')
        argv = notifier.notification_argv(note, 'osascript')
        self.assertEqual(argv, ['osascript', '-e', 'on run argv', '-e',
                         'display notification (item 2 of argv) with title (item 1 of argv)',
                         '-e', 'end run', '--', note.title, note.text])
        def guarded(values):
            self.assertEqual(values[-3:], ['--', note.title, note.text])
        guarded(argv)
        with self.assertRaises(AssertionError):
            guarded([v for v in argv if v != '--'])

    def test_terminal_notifier_escapes_values(self):
        values = ('-execute x', '-help', '-version', '--diagnose', '-action', '-reply',
                  '-ignoreDnD', '-message', '[x', '(x', '{x', '"x', '\\x', 'plain')
        for title in values:
            for text in values:
                note = notifier.Notification('item', 1, title, text)
                expected = ['terminal-notifier', '-title', '\\' + title, '-message', '\\' + text]
                for argv in (notifier.notification_argv(note, 'terminal-notifier'),
                             notifier.alarm_argv(title, text, 'terminal-notifier')):
                    self.assertEqual(argv, expected)
                    self.assertEqual([v for v in argv if v.startswith('-')], ['-title', '-message'])
                with self.assertRaises(AssertionError):
                    self.assertEqual(['terminal-notifier', '-title', title, '-message', text], expected)

    def test_notify_send_argv(self):
        self.assertEqual(notifier.alarm_argv('-title', '-text', 'notify-send'),
                         ['notify-send', '--', '-title', '-text'])
        # Byte 155 is in the middle of a multi-byte character.
        for text, shown in [('a' * 154 + 'é' * 73, 'a' * 154 + MARK),
                            ('a' * 250, 'a' * 155 + MARK),
                            ('a' * 154 + ' ' + 'b' * 95, 'a' * 154 + MARK),
                            ('é' * 100, 'é' * 100)]:
            for program in PROGRAMS:
                argv = notifier.notification_argv(notifier.Notification('i', 1, 'title', text), program)
                self.assertEqual(argv[-1], ('\\' if program == 'terminal-notifier' else '') + shown)
                self.assertLessEqual(len(shown.encode()), 200)
            if len(text.encode()) > 200:
                self.assertTrue(shown.endswith(MARK))
        with self.assertRaises(AssertionError):
            self.assertEqual('a' * 154 + ' ' + MARK, 'a' * 154 + MARK)
        with self.assertRaises(AssertionError):
            self.assertEqual('é' * 100 + MARK, 'é' * 100)

    def test_argv_names_program_only(self):
        for program in PROGRAMS:
            for argv in (notifier.notification_argv(notifier.Notification('i', 1, 't', 'x'), program),
                         notifier.alarm_argv('t', 'x', program)):
                self.assertEqual(argv[0], program)
                self.assertNotIn('/', argv[0])

    def test_alarm_argv(self):
        for program in (*PROGRAMS, 'none'):
            for title, text in [('v3', 'Alarm.'), ('-e', 'quote " and \\')]:
                self.assertEqual(notifier.alarm_argv(title, text, program),
                    notifier.notification_argv(notifier.Notification('i', 1, title, text), program))

    def test_no_project_title(self):
        self.assertEqual(notifier.notification_title(None, 'supervision'), 'v3: supervision')
        self.assertEqual(notifier.notification_title('alpha', 'alpha.planner'), 'alpha: alpha.planner')

    def test_title_names_the_seat(self):
        addressed, _, _ = self.raise_it(actor='billing.implementer')
        unaddressed, _, _ = self.raise_it()
        stalled, _, _ = self.raise_it('notice', 'worker_stalled', None)
        notes = {n.item_id: n for n in notifier.due_notifications(self.conn, 1000, 20)}
        self.assertEqual(notes[addressed.item_id].title, 'billing-service: billing.implementer')
        self.assertEqual(notes[unaddressed.item_id].title, 'billing-service: console_queue')
        self.assertEqual(notes[stalled.item_id].title, 'v3: supervision')
        self.assertEqual(notifier.notification_title(None, 'operator_bridge'), 'v3: operator_bridge')
        project = 'customer-onboarding-and-identity'
        actor = 'customer-onboarding-identity-verification-and-fraud-review-team1.orchestrator-reviewer'
        self.conn.execute('UPDATE project_routes SET name=? WHERE project_id=?', (project, P)).close()
        long, _, _ = self.raise_it(actor=actor)
        note = next(n for n in notifier.due_notifications(self.conn, 1000, 20) if n.item_id == long.item_id)
        self.assertEqual(note.title, project + ': ' + actor)
        self.assertEqual(len(note.title.encode()), 120)
        for program in PROGRAMS:
            argv = notifier.notification_argv(note, program)
            self.assertEqual(argv[-2 if program != 'terminal-notifier' else 2],
                             ('\\' if program == 'terminal-notifier' else '') + note.title)
        self.conn.execute('UPDATE queue_items SET actor_address=? WHERE item_id=?',
                          ('another.planner', addressed.item_id)).close()
        changed = next(n for n in notifier.due_notifications(self.conn, 1000, 20)
                       if n.item_id == addressed.item_id)
        self.assertEqual(changed.title, project + ': another.planner')

    def test_delivery_failures_make_no_alarm(self):
        item, _, _ = self.raise_it()
        expected = notifier.notification_argv(notifier.due_notifications(self.conn, 1000, 1)[0], 'notify-send')
        calls = []
        def failing(argv):
            calls.append(argv)
            self.delivery_failure(argv)
        for n in range(5):
            self.run_pass(failing, 1000 + n * 1000000)
        self.assertEqual(calls, [expected] * 5)
        self.assertEqual(self.row(item)[:2], ('pending', 5))
        self.assertEqual(len(self.unhealthy()), 1)
        self.assertEqual(self.unhealthy()[0][2], '"notifier"')
        self.assertIsNone(self.conn.execute('SELECT 1 FROM queue_notifications WHERE item_id=?',
                                           (self.unhealthy()[0][0],)).fetchone())

    def test_failures_raise_unhealthy_once(self):
        items = [self.raise_it()[0] for _ in range(6)]
        self.run_pass(self.delivery_failure)
        self.assertEqual(self.health(), 5)
        self.run_pass(self.delivery_failure, 2000000)
        self.assertEqual(len(self.unhealthy()), 1)
        self.assertTrue(all(self.row(item)[0] == 'pending' for item in items))
        self.run_pass(lambda argv: None, 4000000, limit=1)
        self.assertEqual(self.health(), 0)
        self.assertEqual(self.unhealthy()[0][1], 'cleared')
        self.assertEqual(records.open_condition_items(self.conn, records.InternalWriter(), None), [])

    def test_failure_count_survives_restart(self):
        self.raise_it()
        for n in range(4):
            self.run_pass(self.delivery_failure, 1000 + n * 1000000)
        self.assertEqual(self.health(), 4)
        self.assertEqual(self.unhealthy(), [])
        self.reopen()
        self.run_pass(self.delivery_failure, 4001000)
        self.assertEqual(self.health(), 5)
        self.assertEqual(len(self.unhealthy()), 1)
        self.run_pass(lambda argv: None, 5001000)
        self.assertEqual(self.health(), 0)
        self.assertEqual(self.unhealthy()[0][1], 'cleared')
        # Run the same delivery path with only a restart-local health counter.
        control = TestQueueNotifier('test_failure_count_survives_restart')
        control.setUp()
        self.addCleanup(control.doCleanups)
        control.raise_it()
        local_count = [0]
        record_failure = notifier.notify_failed
        def memory_only(conn, note, error_class, now_ms):
            records._write(conn, 'INSERT OR REPLACE INTO queue_notifier_health VALUES (1,?,?)',
                           (local_count[0], now_ms))
            record_failure(conn, note, error_class, now_ms)
            local_count[0] += 1
        with mock.patch.object(notifier, 'notify_failed', side_effect=memory_only):
            for n in range(4):
                control.run_pass(control.delivery_failure, 1000 + n * 1000000)
            self.assertEqual(control.health(), 4)
            control.reopen()
            local_count[0] = 0
            control.run_pass(control.delivery_failure, 4001000)
        self.assertEqual(control.health(), 1)
        self.assertEqual(control.unhealthy(), [])
        with self.assertRaises(AssertionError):
            self.assertEqual(len(control.unhealthy()), 1)

    def test_five_deliveries_per_pass(self):
        items = [self.raise_it()[0] for _ in range(12)]
        due = notifier.due_notifications(self.conn, 1000, 12)
        self.assertEqual([n.item_id for n in due], [i.item_id for i in items])
        calls = []
        for count in (5, 10, 12):
            self.run_pass(calls.append, limit=99)
            self.assertEqual(len(calls), count)

    def test_delivery_is_per_entry(self):
        item, principal, request = self.raise_it()
        calls = []
        self.run_pass(calls.append)
        records.raise_item(self.conn, principal, request, 2000, enabled=ALL)
        records.raise_item(self.conn, principal, replace(request, operation_key='replay'), 2000, enabled=ALL)
        records._rows(self.conn, 'SELECT * FROM queue_items')
        records._rows(self.conn, 'SELECT * FROM queue_events')
        self.conn.commit()
        self.run_pass(calls.append, 3000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.row(item)[0], 'delivered')

    def test_ring_set_delivery(self):
        self.raise_it('notice', 'worker_stalled', None)
        unhealthy, _, _ = self.raise_it('notice', 'notifier_unhealthy', None)
        calls = []
        self.run_pass(calls.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2], 'v3: supervision')
        self.assertIsNone(self.row(unhealthy))

    def test_failed_entry_retries_after_due(self):
        item, _, _ = self.raise_it()
        self.run_pass(self.delivery_failure)
        self.assertEqual(self.row(item), ('pending', 1, 31000, 'OSError'))
        self.reopen()
        calls = []
        self.run_pass(calls.append, 30999)
        self.assertEqual(calls, [])
        self.run_pass(calls.append, 31000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.row(item)[0], 'delivered')
        self.run_pass(calls.append, 999999)
        self.assertEqual(len(calls), 1)

    def test_deliver_runs_outside_jobs(self):
        self.raise_it()
        calls = []
        def deliver(argv):
            self.assertFalse(self.port.active)
            # An independent writer can start while the delivery is outside the job.
            self.conn.execute('BEGIN IMMEDIATE').close()
            self.conn.rollback()
            calls.append(argv)
        self.run_pass(deliver)
        self.assertEqual(len(calls), 1)

    def test_crash_between_show_and_record(self):
        item, _, _ = self.raise_it()
        calls = []
        with mock.patch.object(self.port, 'catalogue', side_effect=RuntimeError('crash after show')):
            with self.assertRaisesRegex(RuntimeError, 'crash after show'):
                self.run_pass(calls.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.row(item)[0], 'pending')
        self.reopen()
        self.run_pass(calls.append)
        self.run_pass(calls.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.row(item)[0], 'delivered')

    def test_in_flight_notification_after_undo(self):
        for failing in (False, True):
            item, _, _ = self.raise_it('notice', 'doorbell_off')
            other, _, _ = self.raise_it('notice', 'doorbell_off', Q)
            self.conn.execute('INSERT OR REPLACE INTO queue_notifier_health VALUES (1,4,0)').close()
            self.conn.commit()
            before = self.row(item)
            calls = []
            def deliver(argv):
                if not calls:
                    records.undo_step(self.conn, P, 'undo-' + str(failing), now_ms=900)
                    self.conn.commit()
                calls.append(argv)
                if failing and argv[-2].startswith('billing-service:'):
                    self.delivery_failure(argv)
            self.run_pass(deliver)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.row(item), ('retired', before[1], before[2], before[3]))
            self.assertEqual(self.row(other)[0], 'delivered')
            self.run_pass(calls.append)
            self.assertEqual(len(calls), 2)
            if failing:
                # Q's success resets health, but P's late fifth failure raised its occurrence.
                self.assertEqual(len(self.unhealthy()), 1)
                self.assertEqual(self.unhealthy()[0][1], 'cleared')
        # Remove both pending-only guards from the actual failure recording path.
        control = TestQueueNotifier('test_in_flight_notification_after_undo')
        control.setUp()
        self.addCleanup(control.doCleanups)
        item, _, _ = control.raise_it('notice', 'doorbell_off')
        before = control.row(item)
        one, write = records._one, records._write
        def without_guard(fn):
            def run(conn, sql, params=()):
                if 'queue_notifications' in sql:
                    sql = sql.replace(" AND state='pending'", '')
                return fn(conn, sql, params)
            return run
        def late_failure(argv):
            records.undo_step(control.conn, P, 'undo-control', now_ms=900)
            control.conn.commit()
            control.delivery_failure(argv)
        with mock.patch.object(records, '_one', side_effect=without_guard(one)), \
             mock.patch.object(records, '_write', side_effect=without_guard(write)):
            control.run_pass(late_failure)
        self.assertEqual(control.row(item), ('pending', 1, 31000, 'OSError'))
        with self.assertRaises(AssertionError):
            self.assertEqual(control.row(item), ('retired', before[1], before[2], before[3]))

    def test_none_notifier_records_nothing(self):
        item, _, _ = self.raise_it()
        before = self.row(item)
        deliver = mock.Mock()
        notifier.notify_pass(self.port, deliver, 1000, notifier='none')
        deliver.assert_not_called()
        self.assertEqual(self.row(item), before)
        self.assertIsNone(self.conn.execute('SELECT 1 FROM queue_notifier_health').fetchone())

    def test_popup_text_choice(self):
        question, _, _ = self.raise_it(texts={'subject': 'Subject', 'body': 'Body'})
        ring, _, _ = self.raise_it('notice', 'doorbell_off')
        self.conn.execute('UPDATE queue_items SET texts=? WHERE item_id=?',
                          (json.dumps({'sentence': 'S', 'ring_text': 'R'}), ring.item_id)).close()
        popup, _, _ = self.raise_it('merge_go_ahead')
        self.conn.execute('UPDATE queue_items SET popup_text=? WHERE item_id=?', ('T', popup.item_id)).close()
        worker, _, _ = self.raise_it('notice', 'worker_stalled', None)
        self.conn.commit()
        sentence = json.loads(self.conn.execute('SELECT texts FROM queue_items WHERE item_id=?',
                                               (worker.item_id,)).fetchone()[0])['sentence']
        expected = {question.item_id: 'Subject', ring.item_id: 'R', popup.item_id: 'T', worker.item_id: sentence}
        def check():
            self.assertEqual({n.item_id: n.text for n in notifier.due_notifications(self.conn, 1000, 10)}, expected)
        check()
        with mock.patch.object(notifier, '_popup_text', side_effect=lambda kind, texts, popup: texts.get('sentence', '')):
            with self.assertRaises(AssertionError):
                check()


if __name__ == '__main__':
    unittest.main()
