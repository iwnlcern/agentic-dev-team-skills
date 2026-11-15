"""Approved fixed words and their exact display argument placement."""
import unittest

from relay_engine.operator_queue import kinds, notifier


EXPECTED = {('worker_stalled', 'queue_question_ingest'): "v3 has stopped turning agents' questions into "
                                              'items, so new questions are not shown. Run adt '
                                              'engine status.',
 ('worker_stalled', 'queue_lifecycle_ingest'): 'v3 has stopped reading notices from projects, so '
                                               'new notices are not shown. Run adt engine status.',
 ('worker_stalled', 'queue_reconcile'): 'v3 has stopped finishing your answers, so an answer you '
                                        'gave may not have reached its agent. Run adt engine '
                                        'status.',
 ('worker_stalled', 'engine_liveness'): 'v3 has stopped checking whether agents get the relays '
                                        'sent to them, so an agent that does not is not shown. Run '
                                        'adt engine status.',
 ('bridge_refused', '*'): 'v3 refused a request to act as you that did not come from its own page '
                          'address; nothing was changed. If you did not just use a v3 page at '
                          'another address, another site may have tried.',
 ('notifier_unhealthy', 'notifier'): 'v3 could not show its pop-ups five times in a row, so new '
                                     'questions and notices may not pop up. They are still listed '
                                     "on v3's page. Run adt engine status.",
 ('notifier_unhealthy', 'notify_worker'): 'v3 has stopped showing pop-ups, so new questions and '
                                          "notices do not pop up. They are still listed on v3's "
                                          'page. Run adt engine status.'}
EXPECTED[('backup_missed', 'daily')] = 'v3 has made no complete daily backup of its store yesterday or today, so putting the store back from a backup could lose more than a day of work. Run adt engine status.'
EXPECTED[('worker_stalled', 'store_backup_daily')] = 'v3 has stopped making its daily backup, so your store may go without one. Run adt engine status.'
LENGTHS = {('worker_stalled', 'queue_question_ingest'): 107,
 ('worker_stalled', 'queue_lifecycle_ingest'): 98,
 ('worker_stalled', 'queue_reconcile'): 115,
 ('worker_stalled', 'engine_liveness'): 130,
 ('bridge_refused', '*'): 183,
 ('notifier_unhealthy', 'notifier'): 154,
 ('notifier_unhealthy', 'notify_worker'): 134}
LENGTHS[('backup_missed', 'daily')] = 168
LENGTHS[('worker_stalled', 'store_backup_daily')] = 96
ALARM_TITLE = 'v3'
ALARM_TEXT = 'v3 notifications have stopped. Run adt engine status.'


class TestQueueTexts(unittest.TestCase):
    def test_fixed_texts_byte_for_byte(self):
        for key, text in EXPECTED.items():
            self.assertIn(key, kinds.FIXED_TEXTS)
            self.assertEqual(kinds.FIXED_TEXTS[key].encode('utf-8'), text.encode('utf-8'))
            self.assertEqual(len(kinds.FIXED_TEXTS[key].encode('utf-8')), LENGTHS[key])
            self.assertEqual(kinds.fixed_text(key[0], key[1]), text)
        self.assertEqual(set(kinds.FIXED_TEXTS), set(EXPECTED))
        self.assertEqual(len(kinds.FIXED_TEXTS), 9)
        self.assertIsNone(kinds.fixed_text('doorbell_off', 'anything'))
        self.assertEqual(kinds.fixed_text('bridge_refused', 'another-key'), EXPECTED[('bridge_refused', '*')])
        self.assertEqual(kinds.ALARM_TITLE, ALARM_TITLE)
        self.assertEqual(kinds.ALARM_TEXT.encode('utf-8'), ALARM_TEXT.encode('utf-8'))
        self.assertEqual(len(kinds.ALARM_TEXT.encode('utf-8')), 53)

    def test_alarm_words_in_argv(self):
        title, text = kinds.ALARM_TITLE, kinds.ALARM_TEXT
        self.assertEqual((title, text), (ALARM_TITLE, ALARM_TEXT))
        self.assertEqual(notifier.alarm_argv(title, text, 'osascript'),
                         ['osascript', '-e', 'on run argv', '-e',
                          'display notification (item 2 of argv) with title (item 1 of argv)',
                          '-e', 'end run', '--', ALARM_TITLE, ALARM_TEXT])
        self.assertEqual(notifier.alarm_argv(title, text, 'notify-send'),
                         ['notify-send', '--', ALARM_TITLE, ALARM_TEXT])
        self.assertEqual(notifier.alarm_argv(title, text, 'terminal-notifier'),
                         ['terminal-notifier', '-title', '\\' + ALARM_TITLE, '-message', '\\' + ALARM_TEXT])


    def test_answer_faulted_text(self):
        expected = "v3 stopped an answer you gave: a different version had already been sent to this agent, which only a fault in v3 can cause. Open the question on v3's page to check it."
        self.assertEqual(dict(kinds.ANSWER_FAULTED_TEXTS), {'question': expected})
        self.assertEqual(len(kinds.ANSWER_FAULTED_TEXTS['question'].encode('utf-8')), 167)
        with self.assertRaises(TypeError):
            kinds.ANSWER_FAULTED_TEXTS['question'] = 'Changed'
        for program in ('osascript', 'terminal-notifier', 'notify-send'):
            argv = notifier.notification_argv(notifier.Notification('i', 1, 'p: team.planner', expected), program)
            self.assertEqual(argv[-1], ('\\' if program == 'terminal-notifier' else '') + expected)
            self.assertNotIn(' … [cut:', argv[-1])


if __name__ == '__main__':
    unittest.main()
