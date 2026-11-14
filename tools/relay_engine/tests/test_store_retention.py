"""Bounded retention walks and counters on disposable project ledgers."""
import importlib
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import queries, schema
from relay_engine.tests.test_store_evidence import PROJECT


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.evidence = importlib.import_module('relay_engine.store_evidence')
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA foreign_keys=ON')
        schema.init_v3(self.conn)
        self.conn.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'retention',1)",
                          (PROJECT,))
        self.conn.commit()

    def require(self, name):
        method = getattr(self.evidence, name, None)
        self.assertTrue(callable(method), 'missing retention interface: ' + name)
        return method

    def bundle(self, name, closed=None):
        self.conn.execute('INSERT INTO bundles '
                          '(id,project_id,name,opened_by_special,closed_at) VALUES (?,?,?,\'operator\',?)',
                          (name, PROJECT, name, closed))
        return name

    def store(self, bundle):
        capture = self.evidence.Capture('upload', 'seat-supplied', None, 'operator', bundle)
        return self.evidence.LedgerEvidence(self.conn, self.root, PROJECT, capture)

    def seed(self, count):
        self.bundle('bulk')
        self.conn.executemany('INSERT INTO evidence_objects '
                              '(sha256,bytes_len,stored_len,codec,stored_at,latest_capture_at,'
                              'any_host_observed,open_bundle_count,state_seq) '
                              "VALUES (?,1,1,'raw',1,1,0,1,1)",
                              [(format(n, '064x'),) for n in range(1, count + 1)])
        self.conn.executemany('INSERT INTO evidence_object_bundles '
                              '(bundle_id,object_id,counted_open) VALUES (\'bulk\',?,1)',
                              [(n,) for n in range(1, count + 1)])
        self.conn.execute("INSERT INTO evidence_bundles (bundle_id,state_seen) VALUES ('bulk','open')")
        self.conn.execute('INSERT INTO evidence_retention (project_id,next_seq) VALUES (?,2)',
                          (PROJECT,))
        self.conn.commit()

    def page(self, method):
        report = method(self.conn, self.root, PROJECT, 1000)
        self.assertTrue(self.conn.in_transaction, 'retention must leave the transaction to its caller')
        self.conn.commit()
        return report

    def test_capture_updates_counters_in_constant_work(self):
        self.bundle('open')
        self.bundle('closed', 100)
        data = b'counter summary'
        sha = self.store('open').put(data)
        first_seq = self.conn.execute('SELECT state_seq FROM evidence_objects').fetchone()[0]
        self.assertEqual(self.conn.execute('SELECT open_bundle_count,latest_bundle_close_at '
                                          'FROM evidence_objects').fetchone(), (1, None))
        self.store('closed').put(data)
        second_seq = self.conn.execute('SELECT state_seq FROM evidence_objects').fetchone()[0]
        self.assertGreater(second_seq, first_seq)
        self.assertEqual(self.conn.execute('SELECT open_bundle_count,latest_bundle_close_at '
                                          'FROM evidence_objects').fetchone(), (1, 100))
        self.assertEqual(self.conn.execute('SELECT bundle_id,counted_open FROM '
                                          'evidence_object_bundles ORDER BY bundle_id').fetchall(),
                         [('closed', 0), ('open', 1)])
        self.conn.executemany('INSERT INTO evidence_captures '
                              '(object_id,sha256,source,captured_by,party_special,captured_at) '
                              "VALUES (1,?,'upload','seat-supplied','operator',1)", [(sha,)] * 10000)
        self.conn.commit()
        steps = []
        self.conn.set_progress_handler(lambda: (steps.append(1), 0)[1], 1)
        try:
            self.store('open').put(data)
        finally:
            self.conn.set_progress_handler(None, 0)
        # One summary/association update is <700 steps, independent of history.
        self.assertLessEqual(len(steps), 700)
        self.assertEqual(self.conn.execute('SELECT open_bundle_count FROM evidence_objects').fetchone(), (1,))
        self.assertGreater(self.conn.execute('SELECT state_seq FROM evidence_objects').fetchone()[0], second_seq)

    def test_follow_up_pages_and_is_idempotent(self):
        follow_up = self.require('follow_up')
        self.seed(501)
        self.conn.execute("UPDATE bundles SET closed_at=100 WHERE id='bulk'")
        self.conn.commit()
        first = self.page(follow_up)
        second = self.page(follow_up)
        self.assertEqual([first.visited, second.visited], [500, 1])
        self.assertEqual([first.changed, second.changed], [500, 1])
        self.assertEqual(self.conn.execute('SELECT min(open_bundle_count),max(open_bundle_count) '
                                          'FROM evidence_objects').fetchone(), (0, 0))
        self.assertEqual(self.conn.execute('SELECT min(latest_bundle_close_at),min(state_seq) '
                                          'FROM evidence_objects').fetchone(), (100, 2))
        # Replay a completed page by restoring only its durable continuation.
        self.conn.execute("UPDATE evidence_bundles SET follow_up_cursor=0 WHERE bundle_id='bulk'")
        repeat = self.page(follow_up)
        self.assertEqual((repeat.visited, repeat.changed, repeat.drift), (500, 0, 0))
        self.conn.execute('UPDATE evidence_object_bundles SET counted_open=1 WHERE association_id=1')
        self.conn.execute("UPDATE evidence_bundles SET follow_up_cursor=0 WHERE bundle_id='bulk'")
        drift = self.page(follow_up)
        self.assertEqual(drift.drift, 1)
        self.assertEqual(self.conn.execute('SELECT open_bundle_count FROM evidence_objects '
                                          'WHERE object_id=1').fetchone(), (0,))

    def test_derivation_pages_and_invalidates(self):
        derive = self.require('derive')
        follow_up = self.require('follow_up')
        self.seed(501)
        race_data = b'association race'
        race_sha = hashlib.sha256(race_data).hexdigest()
        self.conn.execute('UPDATE evidence_objects SET sha256=? WHERE object_id=1', (race_sha,))
        self.conn.commit()
        with mock.patch.object(queries, 'bundle_closed_at', wraps=queries.bundle_closed_at) as reader:
            first, second = self.page(derive), self.page(derive)
        self.assertEqual([first.visited, second.visited], [500, 1])
        self.assertFalse(first.finished)
        self.assertTrue(second.finished)
        self.assertEqual(first.run_id, second.run_id)
        self.assertTrue(all(len(call.args[2]) <= 500 for call in reader.call_args_list))
        # Run two races the next derivation: a close followed up after page one,
        # and a new association for an object already observed by that page.
        run = self.page(derive).run_id
        self.conn.execute("UPDATE bundles SET closed_at=100 WHERE id='bulk'")
        self.page(follow_up)
        self.bundle('later')
        # Use an actual existing object's bytes/digest for the association race.
        sha = self.store('later').put(race_data)
        object_id = self.conn.execute('SELECT object_id FROM evidence_objects WHERE sha256=?', (sha,)).fetchone()[0]
        self.conn.commit()
        end = self.page(derive)
        self.assertTrue(end.finished)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_derive WHERE run_id=? '
                                          'AND (object_id<=500 OR object_id=?)',
                                          (run, object_id)).fetchone(), (0,))
        self.page(follow_up)
        latest = self.page(derive)
        while not latest.finished:
            latest = self.page(derive)
        self.assertEqual(self.conn.execute('SELECT open_count,latest_close_at FROM evidence_derive '
                                          'WHERE run_id=? AND object_id=501',
                                          (latest.run_id,)).fetchone(), (0, 100))
        self.assertEqual(self.conn.execute('SELECT open_count FROM evidence_derive '
                                          'WHERE run_id=? AND object_id=?',
                                          (latest.run_id, object_id)).fetchone(), (1,))

    def test_walks_visit_bounded_rows(self):
        derive = self.require('derive')
        self.seed(10003)
        walking = [False]
        steps = []

        def trace(sql):
            walking[0] = sql.lstrip().upper().startswith('SELECT') and 'FROM evidence_object_bundles' in sql

        def progress():
            if walking[0]:
                steps.append(1)
            return 0

        self.conn.set_trace_callback(trace)
        self.conn.set_progress_handler(progress, 1)
        try:
            report = self.page(derive)
        finally:
            self.conn.set_trace_callback(None)
            self.conn.set_progress_handler(None, 0)
        self.assertEqual(report.visited, 500)
        # A 500-row primary-key page is <8000 VM steps; walking 10000 before
        # filtering or sorting is >50000. Count the association walk itself.
        self.assertLessEqual(len(steps), 8000)
        self.assertEqual(self.conn.execute('SELECT derive_cursor FROM evidence_retention').fetchone(), (500,))


if __name__ == '__main__':
    unittest.main()
