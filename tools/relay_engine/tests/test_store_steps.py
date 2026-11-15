"""Turns and durable moves: discriminate partial commits, stale files and replay."""
import hashlib
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from relay_engine import store_layout, store_steps


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
JOB = '019a7850-0000-7000-8000-000000000001'


class RollbackFault:
    """A real connection with only its ROLLBACK made to fail."""
    def __init__(self, conn, fail=False):
        self.conn, self.fail, self.closed = conn, fail, False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if self.fail and sql == 'ROLLBACK':
            raise sqlite3.OperationalError('injected rollback fault')
        return self.conn.execute(sql, *args)

    def close(self):
        self.conn.close()
        self.closed = True


class TestTurns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        c = store_layout.open_catalogue(self.root)
        c.close()
        self.turns = store_steps.DirectTurns(self.root)

    def seed(self, project=P):
        c = store_layout.open_ledger(self.root, project, create=True)
        try:
            c.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (project, 'before')).close()
            c.execute('COMMIT').close()
        finally:
            c.close()

    def catalogue_rows(self):
        c = store_layout.open_catalogue(self.root)
        try:
            return c.execute('SELECT project_id,name FROM project_routes ORDER BY project_id').fetchall()
        finally:
            c.close()

    def assert_closed(self, conns):
        for c in conns.values():
            with self.assertRaises(sqlite3.ProgrammingError):
                c.execute('SELECT 1')

    def test_turn_commits_only_written(self):
        # Read-set changes roll back, a body failure cannot commit either file.
        self.seed()
        seen = {}
        def body(conns):
            seen.update(conns)
            self.assertTrue(all(c.in_transaction for c in conns.values()))
            store_layout.reserve_project(conns['catalogue'], P, 'committed', 1)
            conns[P].execute("UPDATE projects SET name='discarded'").close()
            return 'value'
        declared = store_steps.Declared('written', {P: 'read'})
        self.assertEqual(self.turns.run(declared, body), 'value')
        self.assert_closed(seen)
        self.assertEqual(self.catalogue_rows(), [(P, 'committed')])
        c = store_layout.open_ledger(self.root, P)
        try:
            self.assertEqual(c.execute('SELECT name FROM projects').fetchall(), [('before',)])
        finally:
            c.close()
        sentinel = RuntimeError('body sentinel')
        def failed(conns):
            seen.update(conns)
            store_layout.reserve_project(conns['catalogue'], Q, 'rolled-back', 2)
            conns[P].execute("UPDATE projects SET name='also discarded'").close()
            raise sentinel
        with self.assertRaises(RuntimeError) as caught:
            self.turns.run(declared, failed)
        self.assertIs(caught.exception, sentinel)
        self.assertEqual(self.catalogue_rows(), [(P, 'committed')])
        self.assert_closed(seen)
        for ledger_mode in ('written', 'created'):
            empty_root = self.root / ledger_mode
            with self.subTest(mode=ledger_mode):
                with self.assertRaises(ValueError):
                    store_steps.DirectTurns(empty_root).run(
                        store_steps.Declared('written', {Q: ledger_mode}), body)
                self.assertFalse(empty_root.exists())
        with self.assertRaises(ValueError):
            self.turns.run(store_steps.Declared(None, {P: 'written', Q: 'written'}), body)
        self.assertFalse(store_layout.ledger_path(self.root, Q).exists())
        # E4: successful commit must still return its value beside a failed read rollback.
        real_catalogue, real_ledger = store_layout.open_catalogue, store_layout.open_ledger
        for raises in (False, True):
            opened = []
            sentinel = RuntimeError('body beside rollback fault')
            def cat(root):
                c = RollbackFault(real_catalogue(root)); opened.append(c); return c
            def ledger(root, project, *, create=False):
                c = RollbackFault(real_ledger(root, project, create=create), True)
                opened.append(c); return c
            def effect(conns):
                store_layout.reserve_project(conns['catalogue'], Q, 'e4', 3)
                if raises:
                    raise sentinel
                return 'body value'
            caught, result = None, None
            with mock.patch.object(store_layout, 'open_catalogue', side_effect=cat), mock.patch.object(store_layout, 'open_ledger', side_effect=ledger):
                try:
                    result = self.turns.run(declared, effect)
                except BaseException as error:
                    caught = error
            if not raises:
                self.assertIsNone(caught)
                self.assertEqual(result, 'body value')
                self.assertEqual(self.catalogue_rows(), [(P, 'committed'), (Q, 'e4')])
            else:
                self.assertIs(caught, sentinel)
                notes = getattr(caught, '__notes__', [])
                self.assertEqual(len(notes), 1)
                self.assertIn(P, notes[0])
                self.assertEqual(self.catalogue_rows(), [(P, 'committed')])
            self.assertTrue(all(c.closed for c in opened))
            if not raises:
                c = real_catalogue(self.root)
                try:
                    c.execute('BEGIN IMMEDIATE').close()
                    c.execute('DELETE FROM project_routes WHERE project_id=?', (Q,)).close()
                    c.execute('COMMIT').close()
                finally:
                    c.close()

    def test_turn_creates_declared_ledger(self):
        # S2-M10 must FAIL at captured-exception assertion for the schema-only file.
        declared = store_steps.Declared('read', {P: 'created'})
        def body(conns):
            conns[P].execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (P, 'created')).close()
            conns['catalogue'].execute("UPDATE catalogue_identity SET created_at=999").close()
            return 17
        c = store_layout.open_catalogue(self.root)
        before = c.execute('SELECT created_at FROM catalogue_identity').fetchone()
        c.close()
        path = store_layout.ledger_path(self.root, P)
        c = store_layout.open_ledger(self.root, P, create=True)
        c.close()
        caught, result = None, None
        try:
            result = self.turns.run(declared, body)
        except Exception as error:
            caught = error
        self.assertIsNone(caught)
        self.assertEqual(result, 17)
        c = store_layout.open_ledger(self.root, P)
        self.assertEqual(c.execute('SELECT id,name FROM projects').fetchall(), [(P, 'created')])
        c.close()
        path.unlink()
        self.assertEqual(self.turns.run(declared, body), 17)
        c = store_layout.open_catalogue(self.root)
        self.assertEqual(c.execute('SELECT created_at FROM catalogue_identity').fetchone(), before)
        c.close()
        path.unlink()
        def failed(conns):
            body(conns)
            raise RuntimeError('created body crash')
        with self.assertRaisesRegex(RuntimeError, 'created body crash'):
            self.turns.run(declared, failed)
        self.assertTrue(path.exists())
        c = sqlite3.connect(path)
        self.assertEqual(c.execute('SELECT id FROM projects').fetchall(), [])
        c.close()
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.turns.run(store_steps.Declared(None, {P: 'written'}), lambda _: None)
        self.assertEqual(caught.exception.cause, 'no-project-row')
        self.assertEqual(self.turns.run(declared, body), 17)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.turns.run(store_steps.Declared(None, {Q: 'written'}), lambda _: None)
        self.assertEqual(caught.exception.cause, 'missing')
        self.assertFalse(store_layout.ledger_path(self.root, Q).exists())

    def test_turn_fence_precondition(self):
        self.seed()
        self.turns.run(store_steps.Declared('written', {P: 'read'}), lambda _: None, fence_project=P)
        with self.assertRaises(store_layout.RouteUnavailable):
            self.turns.run(store_steps.Declared(None, {P: 'read'}), lambda _: None)
        self.turns.fenced_projects.discard(P)
        self.assertEqual(self.turns.run(store_steps.Declared(None, {P: 'read'}), lambda _: 'open'), 'open')


class TestDurableSteps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_path = store_steps.migration_dir(self.root, JOB)
        self.journal = store_steps.open_journal(self.job_path, create=True)
        self.addCleanup(self.journal.close)

    def fixture(self, name='source.sqlite3'):
        # A child exits without close so committed session rows live only in WAL.
        path = self.root / name
        code = """import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE sessions(id INTEGER PRIMARY KEY, value TEXT)');c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.executemany('INSERT INTO sessions VALUES (?,?)', [(i,'session-'+str(i)) for i in range(20)]);c.commit();os._exit(0)
"""
        subprocess.run([sys.executable, '-c', code, str(path)], check=True, stdin=subprocess.DEVNULL)
        self.assertGreater(Path(str(path) + '-wal').stat().st_size, 0)
        Path(str(path) + '-shm').unlink()
        return path

    def read_rows(self, path):
        c = sqlite3.connect(path)
        try:
            return c.execute('SELECT * FROM sessions ORDER BY id').fetchall()
        finally:
            c.close()

    def prepared(self, name='source.sqlite3'):
        path = self.fixture(name)
        return path, store_steps.prepare_database(path)

    def test_journal_locations_distinct(self):
        recovery = store_steps.recovery_dir(self.root, JOB)
        self.assertNotEqual(self.job_path, recovery)
        for path in (self.job_path, recovery):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.journal.checkpoint('only-migration', {'value': 3})
        journal = store_steps.open_journal(recovery, create=True)
        try:
            self.assertIsNone(journal.last_checkpoint())
        finally:
            journal.close()
        with self.assertRaises(ValueError):
            store_steps.migration_dir(self.root, '../escape')
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_journal_intent_then_receipt(self):
        self.journal.checkpoint('manifest', {'a': 1})
        self.journal.checkpoint('copy', {'b': 2})
        self.assertEqual(self.journal.last_checkpoint(), ('copy', {'b': 2}))
        ident = self.journal.intent('step', {'path': 'target'}, {'v': 1}, {'v': 2})
        pending = self.journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0].id, pending[0].before, pending[0].after), (ident, {'v': 1}, {'v': 2}))
        self.journal.close()
        journal = store_steps.open_journal(self.job_path)
        try:
            self.assertEqual(journal.pending(), pending)
            journal.receipt(ident, {'v': 2})
            self.assertEqual(journal.pending(), [])
            self.assertEqual(journal.last_checkpoint(), ('copy', {'b': 2}))
        finally:
            journal.close()

    def test_resume_before_after_other(self):
        for state in ('before', 'after', 'other'):
            with self.subTest(state=state):
                src, prepared = self.prepared(state + '.sqlite3')
                dst = self.root / (state + '-dest.sqlite3')
                with mock.patch.object(store_steps.os, 'rename', side_effect=RuntimeError('crash')):
                    with self.assertRaisesRegex(RuntimeError, 'crash'):
                        store_steps.move_database(self.journal, prepared, src, dst)
                intent = self.journal.pending()[-1]
                if state == 'after':
                    src.rename(dst)
                elif state == 'other':
                    src.unlink()
                if state == 'other':
                    with self.assertRaises(store_steps.StepStateUnknown) as caught:
                        store_steps.resume_move(self.journal, intent)
                    self.assertIsNotNone(caught.exception.before)
                    self.assertIsNotNone(caught.exception.observed)
                else:
                    with mock.patch.object(store_steps.os, 'rename', wraps=store_steps.os.rename) as rename:
                        self.assertEqual(store_steps.resume_move(self.journal, intent), 'rename' if state == 'before' else 'receipt')
                        self.assertEqual(rename.call_count, 1 if state == 'before' else 0)
                    self.assertNotIn(intent, self.journal.pending())
                    self.assertEqual(len(self.read_rows(dst)), 20)

    def test_prepare_checkpoints_wal(self):
        src = self.fixture()
        control = self.root / 'unprepared.sqlite3'
        shutil.copyfile(src, control)
        self.assertEqual(self.read_rows(control), [])
        prepared = store_steps.prepare_database(src)
        self.assertEqual(prepared.size, src.stat().st_size)
        self.assertEqual(prepared.sha256, hashlib.sha256(src.read_bytes()).hexdigest())
        self.assertFalse(Path(str(src) + '-shm').exists())
        dst = self.root / 'moved.sqlite3'
        store_steps.move_database(self.journal, prepared, src, dst)
        self.assertFalse(src.exists())
        self.assertEqual(self.read_rows(dst), [(i, 'session-' + str(i)) for i in range(20)])
        self.assertEqual(self.journal.pending(), [])

    def test_prepare_refuses_side_files(self):
        src = self.root / 'live.sqlite3'
        c = sqlite3.connect(src)
        try:
            c.execute('PRAGMA journal_mode=WAL').close()
            c.execute('CREATE TABLE t(value)').close()
            c.commit()
            with self.assertRaises(store_steps.FenceBreach):
                store_steps.prepare_database(src)
        finally:
            c.close()
        Path(str(src) + '-shm').write_bytes(b'left behind')
        with self.assertRaises(store_steps.FenceBreach):
            store_steps.prepare_database(src)

    def test_move_crash_points(self):
        for point in ('prepared', 'intent', 'renamed'):
            with self.subTest(point=point):
                src, prepared = self.prepared(point + '.sqlite3')
                dst = self.root / (point + '-dest.sqlite3')
                if point == 'prepared':
                    store_steps.move_database(self.journal, prepared, src, dst)
                elif point == 'intent':
                    with mock.patch.object(store_steps.os, 'rename', side_effect=RuntimeError('crash')):
                        with self.assertRaises(RuntimeError):
                            store_steps.move_database(self.journal, prepared, src, dst)
                    self.assertEqual(store_steps.resume_move(self.journal, self.journal.pending()[-1]), 'rename')
                else:
                    with mock.patch.object(self.journal, 'receipt', side_effect=RuntimeError('crash')):
                        with self.assertRaises(RuntimeError):
                            store_steps.move_database(self.journal, prepared, src, dst)
                    self.assertEqual(store_steps.resume_move(self.journal, self.journal.pending()[-1]), 'receipt')
                self.assertFalse(src.exists())
                self.assertEqual(self.read_rows(dst), [(i, 'session-' + str(i)) for i in range(20)])
                self.assertEqual(self.journal.pending(), [])

    def test_reappearing_side_file_is_breach(self):
        src, prepared = self.prepared()
        dst = self.root / 'dest.sqlite3'
        with mock.patch.object(store_steps.os, 'rename', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                store_steps.move_database(self.journal, prepared, src, dst)
        Path(str(src) + '-wal').write_bytes(b'')
        with self.assertRaises(store_steps.FenceBreach):
            store_steps.resume_move(self.journal, self.journal.pending()[0])
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())
        self.assertEqual(len(self.journal.pending()), 1)

    def test_digest_only_change_is_breach(self):
        # S2-M2 must FAIL at the captured type assertion, never ERROR or rename.
        src, prepared = self.prepared()
        dst = self.root / 'dest.sqlite3'
        data = bytearray(src.read_bytes());data[-1] ^= 1;src.write_bytes(data)
        self.assertEqual(src.stat().st_size, prepared.size)
        caught = None
        try:
            store_steps.move_database(self.journal, prepared, src, dst)
        except Exception as error:
            caught = error
        self.assertIsInstance(caught, store_steps.FenceBreach)
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())
        intent = self.journal.pending()[0]
        with self.assertRaises(store_steps.FenceBreach):
            store_steps.resume_move(self.journal, intent)
        self.assertEqual(self.journal.pending(), [intent])
