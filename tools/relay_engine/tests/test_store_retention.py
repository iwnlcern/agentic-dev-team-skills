"""Bounded retention walks and counters on disposable project ledgers."""
import importlib
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from relay_engine import queries, schema, store_layout
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


DAY = 86400000
NOW = 40 * DAY


class TestRetentionPasses(unittest.TestCase):
    require = TestRetention.require
    bundle = TestRetention.bundle
    store = TestRetention.store
    seed = TestRetention.seed

    def setUp(self):
        TestRetention.setUp(self)
        for name in ('mark', 'sweep', 'unlink_swept', 'list_orphan_candidates', 'orphans'):
            self.require(name)
        self.e = self.evidence

    def disk(self):
        self.conn.close()
        self.conn = store_layout.open_ledger(self.root, PROJECT, create=True)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'passes',1)", (PROJECT,))
        self.conn.commit()

    def old_put(self, data, bundle=None, at=1):
        with mock.patch.object(self.e.time, 'time_ns', return_value=at * 1000000):
            sha = self.store(bundle).put(data)
        self.conn.commit()
        return sha

    def job(self, name, now=NOW, **kwargs):
        self.conn.execute('BEGIN IMMEDIATE')
        args = (self.conn, self.root, PROJECT)
        report = getattr(self.e, name)(*args, **kwargs) if name == 'unlink_swept' else getattr(
            self.e, name)(*args, now, **kwargs)
        self.assertTrue(self.conn.in_transaction, name + ' committed its caller transaction')
        self.conn.commit()
        return report

    def derived(self, now=NOW):
        report = self.job('derive', now)
        while not report.finished:
            report = self.job('derive', now)
        return report.run_id

    def path(self, sha):
        codec = self.conn.execute('SELECT codec FROM evidence_objects WHERE sha256=?', (sha,)).fetchone()[0]
        return store_layout.evidence_dir(self.root, PROJECT) / 'objects' / sha[:2] / (sha + '.' + codec)

    def point(self, sha, count=1):
        self.conn.execute('INSERT OR IGNORE INTO relays '
                          '(id,handle,project_id,kind,needs,subject,sender_special,created_at,'
                          'body_sha256,meta_sha256,body_bytes,header_bytes) '
                          "VALUES ('pointer',1,?,'SITREP','none','pointer','operator',1,?,?,0,0)",
                          (PROJECT, '0' * 64, '0' * 64))
        start = self.conn.execute('SELECT COALESCE(MAX(handle),0)+1 FROM snapshots').fetchone()[0]
        for n in range(start, start + count):
            self.conn.execute('INSERT INTO edges '
                              '(id,from_relay_id,ordinal,kind,target_type,spelling,target_path) '
                              "VALUES (?,'pointer',?,'ref','path','fixture','fixture')", ('edge' + str(n), n))
            self.conn.execute('INSERT INTO snapshots '
                              '(id,handle,relay_id,edge_id,artifact,content_sha256,bytes_len,has_bytes) '
                              "VALUES (?,?,'pointer',?,'fixture',?,0,1)",
                              ('snapshot' + str(n), n, 'edge' + str(n), sha))
        self.conn.commit()

    def test_mark_uses_derived_values(self):
        self.bundle('closed', NOW - 15 * DAY)
        self.bundle('open')
        closed = self.old_put(b'lost decrement', 'closed')
        opened = self.old_put(b'lost increment', 'open')
        self.derived()
        self.conn.execute('UPDATE evidence_objects SET open_bundle_count=1 WHERE sha256=?', (closed,))
        self.conn.execute('UPDATE evidence_objects SET open_bundle_count=0 WHERE sha256=?', (opened,))
        self.conn.commit()
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_prune_marks').fetchall(), [(closed,)])
        counts = dict(self.conn.execute('SELECT sha256,open_bundle_count FROM evidence_objects'))
        self.assertEqual((counts[closed], counts[opened]), (0, 1))

    def test_pointed_never_marked(self):
        self.seed(1200)
        self.conn.execute("UPDATE bundles SET closed_at=? WHERE id='bulk'", (NOW - 15 * DAY,))
        self.conn.commit()
        pointed = format(1, '064x')
        self.point(pointed, 10000)
        self.bundle('recent', NOW - 13 * DAY)
        recent = self.old_put(b'recent close', 'recent')
        self.derived()
        with mock.patch.object(queries, 'evidence_pointed', wraps=queries.evidence_pointed) as reader:
            reports = [self.job('mark') for _ in range(3)]
        self.assertEqual([r.visited for r in reports], [500, 500, 201])
        self.assertTrue(all(len(call.args[2]) <= 500 for call in reader.call_args_list))
        marked = {row[0] for row in self.conn.execute('SELECT sha256 FROM evidence_prune_marks')}
        self.assertNotIn(pointed, marked)
        self.assertNotIn(recent, marked)
        self.assertEqual(len(marked), 1199)
        self.assertEqual(self.conn.execute('SELECT locked_at FROM snapshots LIMIT 1').fetchone(), (None,))

    def test_bundleless_clock(self):
        sha = self.old_put(b'bundleless clock')
        protected = self.old_put(b'bundleless pointed')
        self.point(protected)
        self.derived(13 * DAY)
        self.job('mark', 13 * DAY)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_prune_marks').fetchone(), (0,))
        self.derived(15 * DAY)
        self.job('mark', 15 * DAY)
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_prune_marks').fetchall(), [(sha,)])

    def test_sweep_rechecks(self):
        pointed = self.old_put(b'new pointer')
        changed = self.old_put(b'changed sequence')
        stable = self.old_put(b'stable sweep')
        self.derived()
        self.job('mark')
        self.point(pointed)
        self.conn.execute('UPDATE evidence_objects SET state_seq=state_seq+100 WHERE sha256=?', (changed,))
        self.conn.commit()
        self.job('sweep', NOW + DAY + 1)
        self.assertEqual({r[0] for r in self.conn.execute('SELECT sha256 FROM evidence_objects')},
                         {pointed, changed})
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_pruned').fetchall(), [(stable,)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_prune_marks').fetchone(), (0,))

    def test_unlink_guarded(self):
        data = b'writer outside queue'
        sha = self.old_put(data)
        other = self.old_put(b'crash orphan')
        path, orphan = self.path(sha), self.path(other)
        self.derived()
        self.job('mark')
        self.job('sweep', NOW + DAY + 1)
        self.assertTrue(path.is_file())
        self.assertTrue(orphan.is_file())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_objects').fetchone(), (0,))
        # An independently queued writer resurrects the same digest before unlink.
        self.old_put(data, at=NOW + DAY + 1)
        self.job('unlink_swept')
        self.assertTrue(path.is_file())
        self.assertEqual(self.store(None).get(sha), data)
        self.assertFalse(orphan.exists())
        # Also exercise orphan convergence rather than requiring a sweep replay.
        self.conn.execute('BEGIN IMMEDIATE')
        lost = self.store(None).put(b'rolled back orphan convergence')
        loose = self.path(lost)
        self.conn.rollback()
        os.utime(loose, ((NOW - 2 * DAY) / 1000,) * 2)
        candidates = self.e.list_orphan_candidates(self.root, PROJECT, NOW)
        self.job('orphans', candidates=candidates)
        self.assertFalse(loose.exists())
        self.assertTrue(path.exists())

    def test_orphan_pass_keeps_current_object_file(self):
        # M17: removing the current-row check deletes an old, still-owned file.
        data = b'current object, old file'
        sha = self.old_put(data)
        current = self.path(sha)
        other_codec = 'zlib' if current.suffix == '.raw' else 'raw'
        orphan = current.with_suffix('.' + other_codec)
        orphan.write_bytes(b'unowned codec file')
        for path in (current, orphan):
            os.utime(path, ((NOW - 2 * DAY) / 1000,) * 2)
        candidates = self.e.list_orphan_candidates(self.root, PROJECT, NOW)
        self.job('orphans', candidates=candidates)
        self.assertTrue(current.is_file())
        self.assertEqual(self.store(None).get(sha), data)
        self.assertFalse(orphan.exists())

    def test_sweep_waits_a_day_after_mark(self):
        # M18: removing the mark's 24-hour wait prunes at DAY - 1.
        sha = self.old_put(b'wait after mark')
        self.derived()
        self.job('mark')
        self.job('sweep', NOW + DAY - 1)
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_objects').fetchall(), [(sha,)])
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_prune_marks').fetchall(), [(sha,)])
        self.job('sweep', NOW + DAY + 1)
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_pruned').fetchall(), [(sha,)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_objects').fetchone(), (0,))

    def test_fresh_capture_is_not_marked(self):
        # M19: dropping the fresh-capture clause marks the under-a-day capture.
        self.bundle('old-close', NOW - 15 * DAY)
        sha = self.old_put(b'fresh capture', 'old-close', at=NOW - DAY + 1)
        self.derived()
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_prune_marks').fetchone(), (0,))
        self.conn.execute('UPDATE evidence_objects SET latest_capture_at=? WHERE sha256=?',
                          (NOW - DAY - 1, sha))
        self.conn.commit()
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_prune_marks').fetchall(), [(sha,)])

    def test_object_under_501_bundles(self):
        # M20: replacing rather than accumulating leaves the last closed count 0.
        self.bundle('first-open')
        data = b'one object, 501 bundles'
        sha = self.old_put(data, 'first-open')
        for n in range(500):
            name = 'closed-%03d' % n
            self.bundle(name, NOW - 15 * DAY)
            self.old_put(data, name)

        def finish(name):
            for _ in range(10):
                report = self.job(name)
                self.assertLessEqual(report.visited, 500)
                if report.finished:
                    return report
            self.fail(name + ' did not finish its 501-association walk')

        finish('follow_up')
        run = finish('derive').run_id
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_prune_marks').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT open_count FROM evidence_derive WHERE run_id=?',
                                          (run,)).fetchone(), (1,))
        self.conn.execute("UPDATE bundles SET closed_at=? WHERE id='first-open'", (NOW - 15 * DAY,))
        self.conn.commit()
        finish('follow_up')
        run = finish('derive').run_id
        self.assertEqual(self.conn.execute('SELECT open_count FROM evidence_derive WHERE run_id=?',
                                          (run,)).fetchone(), (0,))
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT sha256 FROM evidence_prune_marks').fetchall(), [(sha,)])

    def test_idle_upload_under_a_day_stays_open(self):
        # M21: removing abandonment's age clause changes this upload to abandoned.
        self.disk()
        capture = self.e.Capture('upload', 'seat-supplied', None, 'operator', None)
        upload = self.e.begin_upload(self.conn, self.root, PROJECT, capture, '0' * 64, 0).upload_id
        self.conn.execute('UPDATE evidence_uploads SET updated_at=? WHERE upload_id=?',
                          (NOW - DAY + 1, upload))
        self.conn.commit()
        path = store_layout.evidence_dir(self.root, PROJECT) / 'uploads' / (upload + '.part')
        os.utime(path, ((NOW - 2 * DAY) / 1000,) * 2)
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT state FROM evidence_uploads WHERE upload_id=?',
                                          (upload,)).fetchone(), ('open',))
        self.job('orphans', candidates=self.e.list_orphan_candidates(self.root, PROJECT, NOW))
        self.assertTrue(path.is_file())

    def test_evidence_directories_are_0700(self):
        # M22: mkdir's default mode under umask 022 leaves these directories 0755.
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        sha = self.old_put(b'private evidence directories')
        capture = self.e.Capture('upload', 'seat-supplied', None, 'operator', None)
        self.e.begin_upload(self.conn, self.root, PROJECT, capture, '0' * 64, 0)
        self.conn.commit()
        evidence = self.root / 'projects' / PROJECT / 'evidence'
        for path in (evidence, evidence / 'objects', evidence / 'objects' / sha[:2],
                     evidence / 'tmp', evidence / 'uploads'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))

    def test_unlink_pass_syncs_each_directory_once(self):
        # B2: syncing after every unlink flushes 20 times instead of twice.
        groups = {}
        n = 0
        while len(groups) < 2 or any(len(values) < 10 for values in groups.values()):
            data = ('two-directory sync %d' % n).encode()
            n += 1
            prefix = hashlib.sha256(data).hexdigest()[:2]
            if prefix not in groups and len(groups) < 2:
                groups[prefix] = []
            if prefix in groups and len(groups[prefix]) < 10:
                groups[prefix].append(self.old_put(data))
        self.derived()
        self.job('mark')
        self.job('sweep', NOW + DAY + 1)
        with mock.patch.object(self.e, '_sync_dir', wraps=self.e._sync_dir) as sync:
            self.job('unlink_swept')
        self.assertEqual(sync.call_count, 2)
        self.assertEqual({call.args[0] for call in sync.call_args_list},
                         {store_layout.evidence_dir(self.root, PROJECT) / 'objects' / prefix
                          for prefix in groups})

    def test_orphans_finished_counts_history(self):
        # B3: a short file page alone incorrectly reports finished on page one.
        self.conn.executemany('INSERT INTO evidence_captures '
                              '(object_id,sha256,source,captured_by,party_special,captured_at) '
                              "VALUES (999999,?,'upload','seat-supplied','operator',1)",
                              [('0' * 64,)] * 501)
        self.conn.commit()
        first = self.job('orphans', candidates=[])
        self.assertFalse(first.finished)
        second = self.job('orphans', candidates=[])
        self.assertTrue(second.finished)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_captures').fetchone(), (0,))

    def test_captured_by_none_after_prune(self):
        sha = self.old_put(b'pruned lookup')
        self.derived()
        self.job('mark')
        self.job('sweep', NOW + DAY + 1)
        self.job('unlink_swept')
        self.assertIsNone(self.store(None).captured_by(sha))
        with self.assertRaises(KeyError):
            self.store(None).get(sha)

    def test_stale_snapshot_reader_answers_no_record(self):
        self.disk()
        sha = self.old_put(b'unsigned reader race')
        path = self.path(sha)
        self.derived()
        self.job('mark')
        reader = sqlite3.connect(store_layout.ledger_path(self.root, PROJECT).as_uri() + '?mode=ro', uri=True)
        self.addCleanup(reader.close)
        store = self.e.LedgerEvidence(reader, self.root, PROJECT, self.e.Capture(
            'upload', 'seat-supplied', None, 'operator', None))
        original = Path.read_bytes
        raced = []

        def read(file):
            if file == path and not raced:
                raced.append(True)
                self.job('sweep', NOW + DAY + 1)
                self.job('unlink_swept')
            return original(file)

        with mock.patch.object(Path, 'read_bytes', read), self.assertRaises(KeyError):
            store.get(sha)
        self.assertEqual(raced, [True])
        self.assertFalse(reader.in_transaction)

    def test_orphan_pass_removes_stale_prepared_and_rolled_back_files(self):
        self.disk()
        self.conn.execute('BEGIN IMMEDIATE')
        sha = self.store(None).put(b'rolled back object')
        loose = self.path(sha)
        self.conn.rollback()
        data = b'abandoned prepared upload'
        capture = self.e.Capture('upload', 'seat-supplied', None, 'operator', None)
        upload = self.e.begin_upload(self.conn, self.root, PROJECT, capture,
                                     hashlib.sha256(data).hexdigest(), len(data)).upload_id
        self.conn.commit()
        self.e.append_chunk(self.conn, self.root, PROJECT, upload, 0, data)
        self.conn.commit()
        prepared = self.e.prepare_upload(self.root, PROJECT, upload)
        self.conn.execute("UPDATE evidence_uploads SET state='abandoned' WHERE upload_id=?", (upload,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.e.commit_upload(self.conn, self.root, PROJECT, prepared)
        self.conn.rollback()
        evidence = store_layout.evidence_dir(self.root, PROJECT)
        files = [loose, evidence / 'tmp' / prepared.encoded_name, evidence / 'uploads' / (upload + '.part')]
        for path in files:
            os.utime(path, ((NOW - DAY + 1) / 1000,) * 2)
        candidates = self.e.list_orphan_candidates(self.root, PROJECT, NOW)
        self.job('orphans', candidates=candidates)
        self.assertTrue(all(path.is_file() for path in files))
        for path in files:
            os.utime(path, ((NOW - DAY - 1) / 1000,) * 2)
        self.job('orphans', candidates=candidates)
        self.assertTrue(all(not path.exists() for path in files))
        opened = self.e.begin_upload(self.conn, self.root, PROJECT, capture, '0' * 64, 0).upload_id
        self.conn.commit()
        path = evidence / 'uploads' / (opened + '.part')
        os.utime(path, ((NOW - 2 * DAY) / 1000,) * 2)
        self.job('orphans', candidates=self.e.list_orphan_candidates(self.root, PROJECT, NOW))
        self.assertTrue(path.exists())
        self.conn.execute('UPDATE evidence_uploads SET updated_at=? WHERE upload_id=?',
                          (NOW - DAY - 1, opened))
        self.conn.commit()
        self.job('mark')
        self.assertEqual(self.conn.execute('SELECT state FROM evidence_uploads WHERE upload_id=?',
                                          (opened,)).fetchone(), ('abandoned',))
        self.job('orphans', candidates=self.e.list_orphan_candidates(self.root, PROJECT, NOW))
        self.assertFalse(path.exists())

    def test_orphan_pass_bounded(self):
        self.seed(10003)
        self.conn.executemany('INSERT INTO evidence_captures '
                              '(object_id,sha256,source,captured_by,party_special,captured_at) '
                              "VALUES (?,?,'upload','seat-supplied','operator',1)",
                              [(1, format(1, '064x'))] * 10003 + [(999999, '0' * 64)] * 501)
        self.conn.executemany('INSERT INTO evidence_object_bundles '
                              "(bundle_id,object_id,counted_open) VALUES ('bulk',?,0)",
                              [(999999 + n,) for n in range(501)])
        self.conn.executemany('INSERT INTO evidence_derive (run_id,object_id,open_count) VALUES (1,?,0)',
                              [(n,) for n in range(1, 1101)])
        self.conn.execute('UPDATE evidence_retention SET derive_run_id=2,derive_start_seq=2,derive_finished=1')
        self.conn.commit()
        directory = store_layout.evidence_dir(self.root, PROJECT) / 'tmp'
        directory.mkdir(parents=True, mode=0o700)
        for n in range(501):
            path = directory / ('f%04d' % n)
            path.write_bytes(b'x')
            os.utime(path, ((NOW - 2 * DAY) / 1000,) * 2)
        young = directory / 'z-young'
        young.write_bytes(b'keep')
        os.utime(young, ((NOW - 1) / 1000,) * 2)
        original_scan, original_list = os.scandir, os.listdir

        def scan(*args, **kwargs):
            self.assertFalse(self.conn.in_transaction, 'directory listing under the job lock')
            return original_scan(*args, **kwargs)

        def listing_directory(*args, **kwargs):
            self.assertFalse(self.conn.in_transaction, 'directory listing under the job lock')
            return original_list(*args, **kwargs)

        with mock.patch.object(os, 'scandir', side_effect=scan) as scanning, \
                mock.patch.object(os, 'listdir', side_effect=listing_directory) as listing:
            candidates = self.e.list_orphan_candidates(self.root, PROJECT, NOW)
            calls = scanning.call_count + listing.call_count
            self.assertGreater(calls, 0)
            first = self.job('orphans', candidates=candidates)
            self.assertEqual(scanning.call_count + listing.call_count, calls)
        self.assertEqual(first.visited, 500)
        self.assertEqual(len(list(directory.iterdir())), 2)
        self.assertEqual(self.conn.execute('SELECT orphan_capture_cursor,orphan_association_cursor '
                                          'FROM evidence_retention').fetchone(), (500, 500))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_captures WHERE object_id=999999').fetchone(), (501,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_object_bundles WHERE object_id>=999999').fetchone(), (501,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_derive WHERE run_id=1').fetchone(), (600,))
        self.job('orphans', candidates=candidates[500:])
        self.assertEqual(list(directory.iterdir()), [young])

    def test_passes_return_after_one_page(self):
        self.seed(1200)
        self.conn.execute("UPDATE bundles SET closed_at=? WHERE id='bulk'", (NOW - 15 * DAY,))
        self.conn.commit()
        self.derived()
        other_project = '00000000-0000-7000-8000-000000000002'
        other = store_layout.open_ledger(self.root, other_project, create=True)
        self.addCleanup(other.close)
        other.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'other',1)", (other_project,))
        other.commit()
        reports = []
        for n in range(3):
            reports.append(self.job('mark'))
            other.execute('UPDATE projects SET created_at=? WHERE id=?', (n + 2, other_project))
            other.commit()
            self.assertEqual(other.execute('SELECT created_at FROM projects').fetchone(), (n + 2,))
        self.assertEqual([r.visited for r in reports], [500, 500, 200])
        self.assertEqual(self.conn.execute('SELECT mark_cursor FROM evidence_retention').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_prune_marks').fetchone(), (1200,))

    def test_store_touches_only_its_entries(self):
        self.disk()
        foreign = self.root / 'foreign-file'
        foreign.write_bytes(b'foreign')
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        sentinel = Path(outside.name) / 'sentinel'
        sentinel.write_bytes(b'outside')
        sha = self.old_put(b'owned evidence')
        capture = self.e.Capture('upload', 'seat-supplied', None, 'operator', None)
        with mock.patch.object(self.e.time, 'time_ns', return_value=1000000):
            upload = self.e.begin_upload(self.conn, self.root, PROJECT, capture, sha, len(b'owned evidence')).upload_id
            self.conn.commit()
            self.e.append_chunk(self.conn, self.root, PROJECT, upload, 0, b'owned evidence')
            self.conn.commit()
            prepared = self.e.prepare_upload(self.root, PROJECT, upload)
            self.e.commit_upload(self.conn, self.root, PROJECT, prepared)
            self.conn.commit()
        self.assertTrue((self.root / 'projects' / PROJECT / 'evidence' / 'uploads' / (upload + '.part')).is_file())
        self.derived()
        self.job('mark')
        self.job('sweep', NOW + DAY + 1)
        self.job('unlink_swept')
        candidates = self.e.list_orphan_candidates(self.root, PROJECT, NOW)
        self.job('orphans', candidates=candidates)
        for bad in (str(sentinel), '../foreign-file', 'uploads/../../foreign-file'):
            with self.assertRaises(ValueError):
                self.job('orphans', candidates=[bad])
            self.conn.rollback()
        self.assertEqual(foreign.read_bytes(), b'foreign')
        self.assertEqual(sentinel.read_bytes(), b'outside')
        base = self.root / 'projects' / PROJECT
        for path in self.root.rglob('*'):
            if path.is_file() and path != foreign:
                relative = path.relative_to(base).as_posix()
                self.assertTrue(relative.startswith(('ledger.sqlite3', 'evidence/objects/',
                                                     'evidence/uploads/', 'evidence/tmp/')), relative)
        self.assertFalse((base / 'evidence' / 'staging').exists())


if __name__ == '__main__':
    unittest.main()
