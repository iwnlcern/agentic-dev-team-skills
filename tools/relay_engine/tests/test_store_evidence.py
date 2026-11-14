"""Evidence schema and object behavior, independent of the caller's admission."""
from dataclasses import replace
import hashlib
import importlib
import importlib.util
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib

from relay_engine import errors, schema, store_layout, submit
from relay_engine.tests import test_submit
from relay_engine.tests.test_submit import Store as SubmitStore


EVIDENCE_TABLES = frozenset({
    'evidence_objects', 'evidence_object_bundles', 'evidence_bundles',
    'evidence_captures', 'evidence_uploads', 'evidence_retention',
    'evidence_derive', 'evidence_prune_marks', 'evidence_pruned',
})


class TestEvidenceSchema(unittest.TestCase):
    def connect(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA foreign_keys=ON')
        schema.init_v3(conn)
        return conn

    def require_evidence(self, conn):
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(EVIDENCE_TABLES, names, 'missing evidence tables')

    def test_evidence_tables_are_strict_and_owned(self):
        # Breaks: omitting a table/owner, weakening STRICT, or adding object FKs
        # that prevent capture and association history from outliving a prune.
        conn = self.connect()
        self.require_evidence(conn)
        self.assertEqual({t for t, owner in schema.TABLE_OWNERS.items()
                          if owner == 'store'}, EVIDENCE_TABLES)
        strict = {r[1]: r[5] for r in conn.execute('PRAGMA table_list')}
        for table in sorted(EVIDENCE_TABLES):
            with self.subTest(table=table):
                self.assertEqual(strict[table], 1)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='evidence_objects'").fetchone()[0]
        self.assertRegex(sql, r'object_id INTEGER PRIMARY KEY AUTOINCREMENT')
        for table in ('evidence_captures', 'evidence_object_bundles'):
            with self.subTest(table=table):
                foreign = list(conn.execute('PRAGMA foreign_key_list(' + table + ')'))
                self.assertFalse(any(r[3] == 'object_id' for r in foreign))
                indexes = [r[1] for r in conn.execute('PRAGMA index_list(' + table + ')')]
                indexed = [tuple(r[2] for r in conn.execute('PRAGMA index_info(' + name + ')'))
                           for name in indexes]
                self.assertTrue(any(cols and cols[0] == 'object_id' for cols in indexed))
        for table in ('evidence_object_bundles', 'evidence_bundles'):
            foreign = list(conn.execute('PRAGMA foreign_key_list(' + table + ')'))
            self.assertTrue(any(r[2:5] == ('bundles', 'bundle_id', 'id') for r in foreign))
            self.assertTrue(all(r[5] == r[6] == 'NO ACTION' for r in foreign))

    def test_evidence_tables_not_hook_writable(self):
        # Breaks: the old owner-label denylist permits Store tables to a hook.
        self.require_evidence(self.connect())
        fixture = SubmitStore()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for table in sorted(EVIDENCE_TABLES):
            with self.subTest(table=table), self.assertRaises(ValueError):
                replace(fixture.ctx(), after_admit_writes=frozenset({table}))

    def test_old_store_refuses_after_evidence_ddl(self):
        # Breaks: opening the actual pre-evidence layout as current or writing
        # the new tables into it. The independent control opens today's layout.
        control = self.connect()
        self.require_evidence(control)
        old_tables = tuple(sql for sql in schema._TABLES if not re.search(
            r'CREATE TABLE(?: IF NOT EXISTS)? evidence_', sql))
        old_indexes = tuple(sql for sql in schema._INDEXES if not re.search(
            r' ON evidence_', sql))
        old = sqlite3.connect(':memory:')
        self.addCleanup(old.close)
        with mock.patch.object(schema, '_TABLES', old_tables), mock.patch.object(
                schema, '_INDEXES', old_indexes):
            schema.init_v3(old)
        before = old.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        digest = old.execute('SELECT ddl_sha256 FROM schema_version').fetchone()
        with self.assertRaises(errors.EngineError) as caught:
            schema.init_v3(old)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')
        self.assertEqual(old.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall(), before)
        self.assertEqual(old.execute('SELECT ddl_sha256 FROM schema_version').fetchone(), digest)
        self.assertFalse(old.in_transaction)
        schema.init_v3(control)
        self.assertFalse(control.in_transaction)


PROJECT = '00000000-0000-7000-8000-000000000001'


class TestEvidenceObjects(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('relay_engine.store_evidence'),
                             'missing evidence module')
        self.evidence = importlib.import_module('relay_engine.store_evidence')
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA foreign_keys=ON')
        schema.init_v3(self.conn)
        self.conn.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'objects', 1)",
                          (PROJECT,))
        self.conn.commit()
        self.store = self.make_store()

    def make_store(self, *, source='snapshot', captured_by='host-observed'):
        capture = self.evidence.Capture(source, captured_by, None, 'operator', None)
        return self.evidence.LedgerEvidence(self.conn, self.root, PROJECT, capture)

    def object_path(self, sha):
        codec = self.conn.execute('SELECT codec FROM evidence_objects WHERE sha256=?',
                                  (sha,)).fetchone()[0]
        return self.root / 'projects' / PROJECT / 'evidence' / 'objects' / sha[:2] / (sha + '.' + codec)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def fallback(self):
        patch = mock.patch.dict(sys.modules, {'compression': None, 'compression.zstd': None})
        patch.start()
        self.addCleanup(importlib.reload, self.evidence)
        self.addCleanup(patch.stop)
        importlib.reload(self.evidence)
        self.store = self.make_store()

    def test_put_inside_admission_commits_with_it(self):
        # Breaks: rows committed on a second connection, or put committing the
        # admission before its hook. Both histories roll back with the snapshot.
        fixture = SubmitStore()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.conn.execute('PRAGMA foreign_keys=OFF')
        for table, column in (('projects', 'id'), ('team_instances', 'project_id'),
                              ('repositories', 'project_id')):
            fixture.conn.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE ' + column + "='p'",
                                 (PROJECT,))
        fixture.conn.commit()
        fixture.conn.execute('PRAGMA foreign_keys=ON')
        self.assertEqual(fixture.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

        class Directory(test_submit.FakeDirectory):
            def resolve(self, address, sender):
                seat = super().resolve(address, sender)
                return replace(seat, project_id=PROJECT) if seat.project_id == 'p' else seat

        capture = self.evidence.Capture('snapshot', 'host-observed',
                                         test_submit.PLANNER.seat_id, None, None)
        store = self.evidence.LedgerEvidence(fixture.conn, self.root, PROJECT, capture)
        ctx = replace(fixture.ctx(), sender=replace(test_submit.PLANNER, project_id=PROJECT),
                      directory=Directory(), store=store)
        path = self.root / 'design.md'
        path.write_bytes(b'first design\n')
        ref = 'file:' + str(path)
        submit.submit(fixture.conn, ctx, test_submit.draft(
            kind='DESIGN', needs='review', to='v3-engine.implementer', refs=(ref,)))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(store.get(sha), path.read_bytes())
        self.assertEqual(fixture.conn.execute(
            'SELECT content_sha256,has_bytes FROM snapshots').fetchall(), [(sha, 1)])
        self.assertFalse(fixture.conn.in_transaction)
        before = [fixture.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                  for table in ('snapshots', 'evidence_objects', 'evidence_captures', 'relays')]

        def failing(*args):
            raise RuntimeError('stop after snapshot')

        path.write_bytes(b'second design\n')
        ctx = replace(ctx, submission_key='second-design', after_admit=failing)
        with self.assertRaises(errors.EngineError) as caught:
            submit.submit(fixture.conn, ctx, test_submit.draft(
                kind='DESIGN', needs='review', to='v3-engine.implementer', refs=(ref,)))
        self.assertEqual(caught.exception.code, 'E-ADMISSION-HOOK')
        self.assertEqual([fixture.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                          for table in ('snapshots', 'evidence_objects', 'evidence_captures', 'relays')], before)
        self.assertIsNone(store.captured_by(hashlib.sha256(path.read_bytes()).hexdigest()))
        self.assertFalse(fixture.conn.in_transaction)

    def test_rolled_back_put_leaves_orphan_file_only(self):
        self.conn.execute('BEGIN IMMEDIATE')
        sha = self.store.put(b'rollback bytes' * 100)
        path = self.object_path(sha)
        self.assertTrue(path.is_file())
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertTrue(path.is_file())
        for table in ('evidence_objects', 'evidence_captures', 'evidence_retention'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone(), (0,))
        with self.assertRaises(KeyError):
            self.store.get(sha)

    def test_put_twice_one_object_two_captures(self):
        data = b'captured twice' * 100
        sha = self.store.put(data)
        self.assertEqual(self.store.put(data), sha)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_objects').fetchone(), (1,))
        self.assertEqual(self.conn.execute('SELECT source,captured_by FROM evidence_captures').fetchall(),
                         [('snapshot', 'host-observed')] * 2)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.store.captured_by(sha), 'host-observed')
        seat = self.make_store(source='upload', captured_by='seat-supplied')
        other = b'uploaded separately' * 100
        key = seat.put(other)
        self.assertEqual(seat.captured_by(key), 'seat-supplied')
        self.make_store(source='host-capture').put(other)
        self.assertEqual(seat.captured_by(key), 'host-observed')
        self.assertEqual(self.conn.execute(
            'SELECT source,captured_by FROM evidence_captures WHERE sha256=? ORDER BY capture_id',
            (key,)).fetchall(), [('upload', 'seat-supplied'), ('host-capture', 'host-observed')])

    def test_captured_by_none_for_never_stored(self):
        self.assertIsNone(self.store.captured_by('0' * 64))

    def test_codec_round_trip(self):
        data = b'compressible evidence\n' * 1000
        native = self.evidence.CODEC_WRITE
        self.assertIn(native, ('zlib', 'zstd'))
        sha = self.store.put(data)
        self.assertEqual(self.object_path(sha).suffix, '.' + native)
        self.assertEqual(self.store.get(sha), data)
        self.conn.commit()
        self.fallback()
        self.assertEqual(self.evidence.CODEC_WRITE, 'zlib')
        key = self.store.put(data + b'fallback')
        self.assertEqual(self.object_path(key).suffix, '.zlib')
        self.assertEqual(self.store.get(key), data + b'fallback')
        raw = self.store.put(b'')
        self.assertEqual(self.object_path(raw).suffix, '.raw')
        self.assertEqual(self.store.get(raw), b'')
        path = self.object_path(key)
        unsupported = path.with_suffix('.zstd')
        path.replace(unsupported)
        self.conn.execute("UPDATE evidence_objects SET codec='zstd' WHERE sha256=?", (key,))
        with self.assertRaises(submit.EvidenceUnavailable) as caught:
            self.store.get(key)
        self.assertEqual(caught.exception.reason, 'unsupported-codec')

    def test_existing_row_writes_no_file(self):
        self.fallback()
        data = b'old codec evidence' * 1000
        sha = self.store.put(data)
        old = self.object_path(sha)
        old.replace(old.with_suffix('.zstd'))
        self.conn.execute("UPDATE evidence_objects SET codec='zstd' WHERE sha256=?", (sha,))
        before = self.files()
        with mock.patch.object(zlib, 'compress', side_effect=AssertionError('existing row encoded')):
            self.assertEqual(self.store.put(data), sha)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_captures').fetchone(), (2,))
        orphan_data = data + b'orphan'
        key = hashlib.sha256(orphan_data).hexdigest()
        orphan = old.parent.parent / key[:2] / (key + '.zstd')
        orphan.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        orphan.write_bytes(b'foreign codec orphan')
        self.assertEqual(self.store.put(orphan_data), key)
        self.assertEqual(self.object_path(key).suffix, '.zlib')
        self.assertEqual(orphan.read_bytes(), b'foreign codec orphan')
        self.assertEqual(self.store.get(key), orphan_data)

    def test_put_over_same_codec_orphan_names_its_own_file(self):
        self.fallback()
        data = b'fresh evidence replaces orphan' * 1000
        sha = hashlib.sha256(data).hexdigest()
        path = self.root / 'projects' / PROJECT / 'evidence' / 'objects' / sha[:2] / (sha + '.zlib')
        path.parent.mkdir(mode=0o700, parents=True)
        path.write_bytes(b'not encoded evidence')
        with mock.patch.object(zlib, 'decompress', side_effect=AssertionError('orphan decoded')):
            self.assertEqual(self.store.put(data), sha)
        self.assertEqual(self.object_path(sha), path)
        self.assertEqual(self.store.get(sha), data)
        self.assertNotEqual(path.read_bytes(), b'not encoded evidence')

    def test_corrupt_object_is_unavailable(self):
        data = b'corruption control' * 1000
        sha = self.store.put(data)
        path = self.object_path(sha)
        encoded = bytearray(path.read_bytes())
        encoded[len(encoded) // 2] ^= 1
        path.write_bytes(encoded)
        with self.assertRaises(submit.EvidenceUnavailable) as caught:
            self.store.get(sha)
        self.assertEqual(caught.exception.reason, 'corrupt')

    def test_get_absent_and_missing(self):
        with self.assertRaises(KeyError):
            self.store.get('0' * 64)
        sha = self.store.put(b'a known object')
        self.conn.commit()
        self.object_path(sha).unlink()
        with self.assertRaises(submit.EvidenceUnavailable) as caught:
            self.store.get(sha)
        self.assertEqual(caught.exception.reason, 'missing')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_objects').fetchone(), (1,))

    def test_captured_by_reads_one_row(self):
        sha = self.store.put(b'history budget')
        object_id = self.conn.execute('SELECT object_id FROM evidence_objects').fetchone()[0]
        self.conn.executemany('INSERT INTO evidence_captures (object_id,sha256,source,captured_by,'
                              'party_special,captured_at) VALUES (?, ?, ?, ?, ?, ?)',
                              [(object_id, sha, 'snapshot', 'host-observed', 'operator', n)
                               for n in range(10000)])
        self.conn.commit()
        statements, steps = [], []
        self.conn.set_trace_callback(statements.append)
        self.conn.set_progress_handler(lambda: (steps.append(1), 0)[1], 1)
        try:
            self.assertEqual(self.store.captured_by(sha), 'host-observed')
        finally:
            self.conn.set_trace_callback(None)
            self.conn.set_progress_handler(None, 0)
        self.assertEqual(len(statements), 1, statements)
        # A unique object lookup is <200 VM steps; a 10,000-capture walk is not.
        self.assertLessEqual(len(steps), 200)


class TestEvidenceUploads(unittest.TestCase):
    def setUp(self):
        self.evidence = importlib.import_module('relay_engine.store_evidence')
        for name in ('begin_upload', 'append_chunk', 'prepare_upload', 'commit_upload'):
            self.assertTrue(callable(getattr(self.evidence, name, None)),
                            'missing upload interface: ' + name)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.conn = store_layout.open_ledger(self.root, PROJECT, create=True)
        self.addCleanup(lambda: self.conn.close())
        self.conn.execute("INSERT INTO projects (id,name,created_at) VALUES (?, 'uploads', 1)",
                          (PROJECT,))
        self.conn.commit()
        self.capture = self.evidence.Capture('upload', 'seat-supplied', None, 'operator', None)
        self.store = self.evidence.LedgerEvidence(self.conn, self.root, PROJECT, self.capture)

    def begin(self, data, *, digest=None):
        state = self.evidence.begin_upload(self.conn, self.root, PROJECT, self.capture,
                                          digest or hashlib.sha256(data).hexdigest(), len(data))
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        return state.upload_id

    def append(self, upload, offset, data):
        return self.evidence.append_chunk(self.conn, self.root, PROJECT, upload, offset, data)

    def ready(self, data):
        upload = self.begin(data)
        for offset in range(0, len(data), 1024 * 1024):
            self.append(upload, offset, data[offset:offset + 1024 * 1024])
            self.conn.commit()
        return self.evidence.prepare_upload(self.root, PROJECT, upload)

    def finalize(self, prepared):
        return self.evidence.commit_upload(self.conn, self.root, PROJECT, prepared)

    def staging(self):
        return tuple((store_layout.evidence_dir(self.root, PROJECT) / 'uploads').iterdir())

    def test_upload_bounds_and_conflicts(self):
        with self.assertRaises(ValueError):
            self.evidence.begin_upload(self.conn, self.root, PROJECT, self.capture,
                                       '0' * 64, 256 * 1024 * 1024 + 1)
        upload = self.begin(b'abcdef')
        self.assertTrue((self.root / 'projects' / PROJECT / 'evidence' / 'uploads' /
                         (upload + '.part')).is_file(),
                        'design section 3 requires evidence/uploads/<upload_id>.part')
        for offset, data in ((0, b'x' * (1024 * 1024 + 1)), (1, b'a'), (0, b'abcdefg')):
            with self.subTest(offset=offset, size=len(data)), self.assertRaises(ValueError):
                self.append(upload, offset, data)
        first = self.append(upload, 0, b'abc')
        self.conn.commit()
        before = self.staging()[0].read_bytes()
        self.assertEqual(self.append(upload, 0, b'abc'), first)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.staging()[0].read_bytes(), before)
        with self.assertRaises(ValueError):
            self.append(upload, 0, b'xyz')
        self.append(upload, 3, b'def')
        self.conn.commit()
        prepared = self.evidence.prepare_upload(self.root, PROJECT, upload)
        result = self.finalize(prepared)
        self.conn.commit()
        self.assertEqual((result.sha256, result.bytes_len, result.captured_by, result.replayed),
                         (hashlib.sha256(b'abcdef').hexdigest(), 6, 'seat-supplied', False))
        replay = self.finalize(prepared)
        self.assertTrue(replay.replayed)
        self.assertEqual(replace(replay, replayed=False), result)
        bad = self.begin(b'bad', digest='0' * 64)
        self.append(bad, 0, b'bad')
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.evidence.prepare_upload(self.root, PROJECT, bad)

    def test_rolled_back_append_is_truncated(self):
        upload = self.begin(b'abcd')
        self.append(upload, 0, b'ab')
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT received_len FROM evidence_uploads').fetchone(), (0,))
        self.assertEqual(self.staging()[0].read_bytes(), b'ab')
        self.append(upload, 0, b'ab')
        self.conn.commit()
        self.append(upload, 2, b'cd')
        self.conn.commit()
        self.assertEqual(self.staging()[0].read_bytes(), b'abcd')
        prepared = self.evidence.prepare_upload(self.root, PROJECT, upload)
        self.finalize(prepared)
        self.conn.commit()
        self.assertEqual(self.store.get(prepared.sha256), b'abcd')

    def test_rolled_back_finalize_keeps_staging(self):
        data = b'finalize rollback' * 100
        prepared = self.ready(data)
        self.finalize(prepared)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT state FROM evidence_uploads').fetchone(), ('open',))
        self.assertEqual(self.staging()[0].read_bytes(), data)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_objects').fetchone(), (0,))
        retry = self.evidence.prepare_upload(self.root, PROJECT, prepared.upload_id)
        self.finalize(retry)
        self.conn.commit()
        self.assertEqual(self.store.get(prepared.sha256), data)

    def test_restart_completes_original_upload(self):
        # Simulate process loss at begin, chunk, prepare and finalize boundaries;
        # reopen the same ledger and repeat the original requests, never migrate.
        for boundary in ('begin', 'chunk', 'prepare', 'finalize'):
            with self.subTest(boundary=boundary):
                data = ('restart ' + boundary).encode() * 100
                upload = self.begin(data)
                if boundary != 'begin':
                    self.append(upload, 0, data)
                    self.conn.commit()
                if boundary in ('prepare', 'finalize'):
                    prepared = self.evidence.prepare_upload(self.root, PROJECT, upload)
                if boundary == 'finalize':
                    self.finalize(prepared)
                    self.conn.commit()
                self.conn.close()
                self.conn = store_layout.open_ledger(self.root, PROJECT)
                if boundary == 'begin':
                    self.append(upload, 0, data)
                    self.conn.commit()
                if boundary != 'finalize':
                    prepared = self.evidence.prepare_upload(self.root, PROJECT, upload)
                result = self.finalize(prepared)
                self.conn.commit()
                self.assertEqual(result.replayed, boundary == 'finalize')
                store = self.evidence.LedgerEvidence(self.conn, self.root, PROJECT, self.capture)
                self.assertEqual(store.get(result.sha256), data)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_uploads '
                                                  'WHERE upload_id=?', (upload,)).fetchone(), (1,))

    def test_finalize_refuses_stale_prepared(self):
        for changed in ('abandoned', 'digest', 'length'):
            with self.subTest(changed=changed):
                prepared = self.ready(('stale ' + changed).encode())
                if changed == 'abandoned':
                    sql, value = 'state=?', 'abandoned'
                elif changed == 'digest':
                    sql, value = 'declared_sha256=?', '0' * 64
                else:
                    sql, value = 'declared_len=?', prepared.bytes_len + 1
                self.conn.execute('UPDATE evidence_uploads SET ' + sql + ' WHERE upload_id=?',
                                  (value, prepared.upload_id))
                self.conn.commit()
                with self.assertRaises(ValueError):
                    self.finalize(prepared)
                self.conn.rollback()
                self.assertTrue((store_layout.evidence_dir(self.root, PROJECT) / 'tmp' /
                                 prepared.encoded_name).is_file())
        prepared = self.ready(b'encoded file length control')
        path = store_layout.evidence_dir(self.root, PROJECT) / 'tmp' / prepared.encoded_name
        path.write_bytes(b'wrong')
        with self.assertRaises(ValueError):
            self.finalize(prepared)
        self.conn.rollback()

    def measured_finalize(self, prepared):
        start = time.monotonic()
        self.conn.execute('BEGIN IMMEDIATE')
        with mock.patch.object(hashlib, 'sha256', side_effect=AssertionError('hash inside job')) as hasher, \
                mock.patch.object(zlib, 'decompress', side_effect=AssertionError('decode inside job')) as decoder:
            result = self.finalize(prepared)
            self.conn.commit()
        duration = time.monotonic() - start
        self.assertEqual((hasher.call_count, decoder.call_count), (0, 0))
        return result, duration

    def test_finalize_over_same_codec_orphan_without_reading(self):
        data = b'orphan replacement' * 1000
        prepared = self.ready(data)
        _, clean = self.measured_finalize(prepared)
        other = data + b'next'
        prepared = self.ready(other)
        path = store_layout.evidence_dir(self.root, PROJECT) / 'objects' / prepared.sha256[:2] / (
            prepared.sha256 + '.' + prepared.codec)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A real 64 MiB orphan; compression and hashing must never touch it.
        with path.open('wb') as file:
            for _ in range(64):
                file.write(b'x' * (1024 * 1024))
        _, orphan = self.measured_finalize(prepared)
        print('finalize lock wall seconds (no orphan,64MiB orphan):', clean, orphan)
        self.assertLess(orphan, 1.0)
        self.assertLess(abs(orphan - clean), 0.5)
        self.assertEqual(self.store.get(prepared.sha256), other)

    def test_overlapping_identical_commits_replay(self):
        first = self.ready(b'overlapping preparations' * 100)
        second = self.evidence.prepare_upload(self.root, PROJECT, first.upload_id)
        original = self.finalize(first)
        self.conn.commit()
        replay = self.finalize(second)
        self.assertEqual(replace(replay, replayed=False), original)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_captures').fetchone(), (1,))
        for bad in (replace(second, sha256='0' * 64), replace(second, bytes_len=1)):
            with self.assertRaises(ValueError):
                self.finalize(bad)

    def test_finalize_lock_time_flat(self):
        # Preparation and all 256 chunks precede the measured transaction.
        data = b'large finalize bytes' * (256 * 1024 * 1024 // 20)
        data += b'x' * (256 * 1024 * 1024 - len(data))
        self.assertEqual(len(data), 256 * 1024 * 1024)
        prepared = self.ready(data)
        result, duration = self.measured_finalize(prepared)
        print('256MiB finalize lock wall seconds:', duration)
        self.assertLess(duration, 1.0)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())


if __name__ == '__main__':
    unittest.main()
