"""Snapshot-safe P9 exemptions across ingest lag and ledger put-backs."""
from dataclasses import replace
import sqlite3
import unittest
from unittest import mock

from relay_engine import queries, submit
from relay_engine.operator_queue import ingest, liveness, operations, records
from relay_engine.tests.test_queue_ingest import QueueFixture, Port, P
from relay_engine.tests import test_submit as engine


class TestQueueLiveness(unittest.TestCase):
    def setUp(self):
        self.f = QueueFixture(self)
        self.c = self.f.catalogue
        self.f.ledger.execute('PRAGMA journal_mode=WAL').close()
        self.work()

    def work(self):
        directory = engine.FakeDirectory()
        directory.resolve = lambda address, origin: replace(engine.SEATS[address], project_id=P)
        data = engine.draft(kind='SITREP', to=self.f.sender.address, needs='act', bundle='liveness-work')
        context = submit.SubmitContext(self.f.other, engine.GIT_STATE, lambda: self.f.clock,
                                       directory, submission_key='liveness-work')
        self.work_relay = submit.submit(self.f.ledger, context, data)

    def run_pass(self):
        return ingest.question_ingest_pass(Port(self.f), [P], self.f.clock)

    def item(self, question):
        return next(row for row in self.f.items() if row[1] == question.relay_id)

    def withdraw(self, question):
        row = self.item(question)
        result = operations.withdraw(self.c, self.f.ledger, self.f.binding,
            row[0], row[3], 'No longer waiting', 'withdraw-'+row[0], self.f.clock)
        self.assertEqual(result['state'], 'withdrawn')
        self.c.commit()

    def flags(self, *, ledger=None, catalogue=None):
        self.c.commit()
        return liveness.p9_flags(self.c if catalogue is None else catalogue,
            self.f.ledger if ledger is None else ledger, P, self.f.clock+100000,
            unopened_ms=1, idle_ms=1)

    def assert_flagged(self, flags):
        self.assertIsInstance(flags, list)
        self.assertIn(self.f.sender.seat_id, [row.seat_id for row in flags])

    def assert_exempt(self, flags):
        self.assertIsInstance(flags, list)
        self.assertNotIn(self.f.sender.seat_id, [row.seat_id for row in flags])

    def test_waiting_seat_not_flagged(self):
        q = self.f.admit();self.run_pass()
        self.assert_exempt(self.flags())
        self.withdraw(q)
        self.assertTrue(queries.operator_questions(self.f.ledger, P, q.handle-1, 1)[0].answered is False)
        self.assert_flagged(self.flags())

    def test_ingest_lag_exempts_sender(self):
        self.assert_flagged(self.flags())
        self.f.admit()
        self.assertEqual(self.f.items(), [])
        self.assert_exempt(self.flags())

    def test_question_after_scan_invisible(self):
        reader = sqlite3.connect(self.f.path)
        self.addCleanup(reader.close)
        original = queries.operator_questions
        admitted = []
        def after_scan(conn, project, after_handle, limit):
            rows = original(conn, project, after_handle, limit)
            if not rows and not admitted:
                admitted.append(self.f.admit())
            return rows
        with mock.patch.object(queries, 'operator_questions', after_scan):
            first = self.flags(ledger=reader)
        self.assertEqual(len(admitted), 1)
        self.assert_flagged(first)
        self.assertFalse(reader.in_transaction)
        self.assert_exempt(self.flags(ledger=reader))

    def test_p9_treats_unmatched_h_as_zero(self):
        low = self.f.backup('below-h.sqlite3')
        self.f.admit('SITREP');old = self.f.admit(sender=self.f.other);self.run_pass()
        h = records.read_cursor(self.c, P, 'question')
        self.f.restore(low);self.f.clock += 100
        new = self.f.admit()
        self.assertLess(new.handle, h.position)
        self.assertNotEqual(new.relay_id, old.relay_id)
        self.assert_exempt(self.flags())

    def test_p9_flag_returns_after_one_pass(self):
        q = self.f.admit();self.run_pass();self.withdraw(q)
        kept = self.f.backup('keeps-withdrawn.sqlite3')
        later = self.f.admit(sender=self.f.other);self.run_pass()
        self.assertEqual(records.read_cursor(self.c, P, 'question').anchor_id, later.relay_id)
        self.f.restore(kept)
        self.assert_exempt(self.flags())
        self.run_pass()
        self.assert_flagged(self.flags())
        self.assertEqual(records.read_cursor(self.c, P, 'question').anchor_id, q.relay_id)

    def test_catalogue_fault_falls_back(self):
        q = self.f.admit();self.run_pass();self.withdraw(q)
        self.assert_flagged(self.flags())
        unavailable = sqlite3.connect(':memory:');unavailable.close()
        with mock.patch.object(queries, 'unopened_flags', wraps=queries.unopened_flags) as call:
            flags = self.flags(catalogue=unavailable)
        self.assertIsNone(call.call_args.kwargs['operator_waiting'])
        self.assert_exempt(flags)

    def test_ledger_fault_returns_unavailable(self):
        self.assert_flagged(self.flags())
        reads = []
        original = queries.operator_questions
        def scanned(*args, **kwargs):
            reads.append(True)
            return original(*args, **kwargs)
        with mock.patch.object(queries, 'operator_questions', scanned), mock.patch.object(queries, 'unopened_flags', side_effect=sqlite3.OperationalError('ledger read failed after scan')):
            result = self.flags()
        self.assertTrue(reads)
        self.assertIsInstance(result, liveness.Unavailable)
        self.assertNotIsInstance(result, list)
        self.assertFalse(self.f.ledger.in_transaction)

    def test_evaluation_writes_nothing(self):
        self.f.admit();self.run_pass();self.c.commit()
        before = self.c.total_changes, self.f.ledger.total_changes
        self.flags()
        self.assertEqual((self.c.total_changes, self.f.ledger.total_changes), before)
        self.assertFalse(self.c.in_transaction)
        self.assertFalse(self.f.ledger.in_transaction)
