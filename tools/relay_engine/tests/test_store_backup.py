"""Backups preserve saga order, pin evidence and refuse unsafe snapshots."""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import store_layout, store_steps, store_evidence
try:
    from relay_engine import store_backup
except ImportError:
    store_backup = None

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
R = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
JOB = '019a7850-0000-7000-8000-000000000001'
NOW = 1700000000000
DAY = 86400000


class TestBackups(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(store_backup, 'Task 9 backup module is absent')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        store_layout.open_catalogue(self.root).close()
        self.turns = store_steps.DirectTurns(self.root)

    def reserve(self, project=P, owner=None):
        self.turns.run(store_steps.Declared('written', {}), lambda c:
            store_layout.reserve_project(c['catalogue'], project, project, NOW, held_by_job=owner))

    def create(self, project=P):
        def body(c):
            c[project].execute('INSERT OR IGNORE INTO projects(id,name,created_at) VALUES (?,?,?)',
                               (project, project, NOW)).close()
        self.turns.run(store_steps.Declared('read', {project: 'created'}), body)

    def activate(self, project=P):
        self.turns.run(store_steps.Declared('written', {}), lambda c:
                       store_layout.activate_project(c['catalogue'], project))

    def read(self, manifest, relative, sql):
        conn = sqlite3.connect((manifest.path / relative).as_uri() + '?mode=ro', uri=True)
        try:
            cursor = conn.execute(sql)
            try:
                return cursor.fetchall()
            finally:
                cursor.close()
        finally:
            conn.close()

    def no_backup(self):
        self.assertFalse((self.root / 'backups').exists())

    def test_saga_intent_without_effect(self):
        # Break: active-only route copy or copying the later effect without intent.
        self.reserve()
        result = store_backup.backup(self.turns, self.root, 'daily', NOW)
        self.assertEqual(self.read(result, 'machine.sqlite3', 'SELECT project_id,state FROM project_routes'),
                         [(P, 'creating')])
        self.assertFalse((result.path / 'projects' / P).exists())
        self.assertEqual(set(result.files), {'machine.sqlite3'})
        metadata = json.loads((result.path / 'manifest.json').read_text())
        self.assertEqual(metadata['reason'], 'daily')
        self.assertEqual(metadata['now_ms'], NOW)
        self.assertEqual(metadata['store_id'], self.turns.run(store_steps.Declared('read', {}),
            lambda c: c['catalogue'].execute('SELECT store_id FROM catalogue_identity').fetchone()[0]))
        self.assertTrue(metadata['kit_build'])
        row = metadata['files']['machine.sqlite3']
        self.assertEqual(row['size'], (result.path / 'machine.sqlite3').stat().st_size)
        self.assertEqual(row['sha256'], hashlib.sha256((result.path / 'machine.sqlite3').read_bytes()).hexdigest())
        self.assertEqual(row['schema_version'], '1')
        self.assertEqual(len(row['ddl_sha256']), 64)

    def test_auxiliary_connections(self):
        # Break: copying from a locked writer, missing query_only or leaked sources.
        self.reserve(); self.create()
        real_cat = store_layout.open_catalogue_readonly
        real_ledger = store_layout.open_ledger_readonly
        opened = []
        def checked(fn, *args):
            conn = fn(*args)
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone(), (1,))
            self.assertFalse(conn.in_transaction)
            opened.append(conn)
            return conn
        before = {str(p.relative_to(self.root)) for p in self.root.rglob('*')}
        with mock.patch.object(store_layout, 'open_catalogue_readonly', side_effect=lambda r: checked(real_cat, r)), mock.patch.object(store_layout, 'open_ledger_readonly', side_effect=lambda r,p: checked(real_ledger, r,p)):
            result = store_backup.backup(self.turns, self.root, 'daily', NOW)
        self.assertEqual(len(opened), 2)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
        after = {str(p.relative_to(self.root)) for p in self.root.rglob('*')
                 if 'backups' not in p.relative_to(self.root).parts}
        self.assertEqual(after, before)
        self.assertEqual(self.read(result, f'projects/{P}/ledger.sqlite3', 'SELECT id FROM projects'), [(P,)])

    def test_evidence_pinned(self):
        # Break: copying after release, omitting objects or losing encoded bytes.
        self.reserve(); self.create()
        payload = b'evidence pinned through unlink' * 100
        capture = store_evidence.Capture('host-capture', 'host-observed', None, 'operator', None)
        sha = self.turns.run(store_steps.Declared(None, {P: 'written'}), lambda c:
            store_evidence.LedgerEvidence(c[P], self.root, P, capture).put(payload))
        source = next((self.root / 'projects' / P / 'evidence' / 'objects').rglob(sha + '.*'))
        encoded = source.read_bytes()
        result = store_backup.backup(self.turns, self.root, 'daily', NOW)
        linked = result.path / source.relative_to(self.root)
        self.assertEqual(linked.stat().st_ino, source.stat().st_ino)
        source.unlink()
        self.assertEqual(linked.read_bytes(), encoded)
        self.assertGreater(result.held_seconds, 0)
        self.assertEqual(json.loads((result.path/'manifest.json').read_text())['held_seconds'], result.held_seconds)

    def test_enumeration_by_presence(self):
        # Break: active-only enumeration, refusing active owners, accepting owned creating payload.
        self.reserve(); self.create()
        self.reserve(Q, JOB); self.create(Q); self.activate(Q)
        self.reserve(R, JOB)  # completed undo: no file, inert reservation
        caught, result = None, None
        try:
            result = store_backup.backup(self.turns, self.root, 'daily', NOW)
        except Exception as exc:
            caught = exc
        self.assertIsNone(caught)
        self.assertEqual({x for x in result.files if x.endswith('ledger.sqlite3')},
                         {f'projects/{P}/ledger.sqlite3', f'projects/{Q}/ledger.sqlite3'})
        self.create(R)
        with self.assertRaises(store_backup.BackupRefused) as caught:
            store_backup.backup(self.turns, self.root, 'daily', NOW + 1)
        self.assertEqual(caught.exception.detail, R)
        self.assertEqual(len(self._published()), 1)
        self.assertEqual(self._partial_entries(), [])
        self._absent_creating_case()
        self._active_missing_case()

    def test_present_ledgers_stat_failure(self):
        # Break: suppressing unreadable ledgers or leaking their raw stat error.
        self.reserve(); self.create(); self.activate()
        self.reserve(Q); self.create(Q)
        store_backup.backup(self.turns, self.root, 'daily', NOW)
        partial = self.root / 'backups' / '.partial'
        marker = partial / 'retained'
        marker.write_bytes(b'untouched on refusal')
        before = self._tree_bytes(self.root / 'backups')
        published = self._published()
        real_stat = os.stat
        for project in (P, Q):
            with self.subTest(project=project):
                target = store_layout.ledger_path(self.root, project)
                injected = PermissionError('injected unreadable ledger')
                def checked_stat(path, *args, **kwargs):
                    if path == target:
                        raise injected
                    return real_stat(path, *args, **kwargs)
                with mock.patch.object(os, 'stat', side_effect=checked_stat):
                    caught, result = self._caught_backup('daily', NOW + 1)
                self.assertIsInstance(caught, store_backup.BackupRefused)
                self.assertIs(caught.__cause__, injected)
                self.assertEqual((caught.reason, caught.detail), ('copy-failed', project))
                self.assertIsNone(result)
                self.assertEqual(self._published(), published)
                self.assertEqual(self._tree_bytes(self.root / 'backups'), before)
        target = store_layout.ledger_path(self.root, Q)
        def absent_stat(path, *args, **kwargs):
            if path == target:
                raise FileNotFoundError(2, 'injected absent creating ledger')
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(os, 'stat', side_effect=absent_stat):
            result = store_backup.backup(self.turns, self.root, 'daily', NOW + 2)
        self.assertIn(f'projects/{P}/ledger.sqlite3', result.files)
        self.assertNotIn(f'projects/{Q}/ledger.sqlite3', result.files)
        self.assertEqual(self.read(result, 'machine.sqlite3',
            'SELECT state FROM project_routes WHERE project_id="' + Q + '"'), [('creating',)])

    def test_backup_refuses_creation_pending(self):
        # Break: mapping in take (unreachable), classifying active corruption as pending.
        self.reserve()
        store_layout.open_ledger(self.root, P, create=True).close()
        with self.assertRaises(store_backup.BackupRefused) as caught:
            store_backup.backup(self.turns, self.root, 'daily', NOW)
        self.assertEqual((caught.exception.reason, caught.exception.detail), ('creation-pending', P))
        self.no_backup()
        self.create()
        result = store_backup.backup(self.turns, self.root, 'daily', NOW)
        self.assertIn(f'projects/{P}/ledger.sqlite3', result.files)
        self.activate()
        self.turns.run(store_steps.Declared(None, {P:'written'}),
                       lambda c: c[P].execute('DELETE FROM projects').close())
        with self.assertRaises(store_layout.LayoutFault) as caught:
            store_backup.backup(self.turns, self.root, 'daily', NOW + 1)
        self.assertEqual(caught.exception.cause, 'no-project-row')
        # Original exception identity must survive the caller's no-match probes.
        original = store_layout.LayoutFault('no-project-row')
        class ActiveFault:
            def run(inner, declaration, body, **kwargs):
                if declaration.ledgers:
                    raise original
                return self.turns.run(declaration, body, **kwargs)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            store_backup.backup(ActiveFault(), self.root, 'daily', NOW + 2)
        self.assertIs(caught.exception, original)

    def test_backup_rereads_a_changed_present_set(self):
        # Break: using only the listing snapshot or retrying without a bound.
        outer = self
        class Interleave:
            def __init__(inner, projects):
                inner.projects = iter(projects)
                inner.refusals = []
            def run(inner, declaration, body, **kwargs):
                try:
                    result = outer.turns.run(declaration, body, **kwargs)
                except store_backup.BackupRefused as exc:
                    inner.refusals.append(exc.reason)
                    raise
                if declaration.catalogue == 'read' and not declaration.ledgers:
                    project = next(inner.projects, None)
                    if project:
                        outer.reserve(project); outer.create(project); outer.activate(project)
                return result
        # An existing ledger makes the actual backup declaration distinguishable.
        self.reserve(); self.create(); self.activate()
        runner = Interleave([Q])
        result = store_backup.backup(runner, self.root, 'daily', NOW)
        self.assertEqual(runner.refusals, ['present-set-changed'])
        self.assertIn(f'projects/{Q}/ledger.sqlite3', result.files)
        self.assertEqual(self.read(result, 'machine.sqlite3', 'SELECT project_id FROM project_routes ORDER BY project_id'), [(P,), (Q,)])
        # Independent store: every attempt changes, no copy directory is created.
        self.root = Path(self.tmp.name) / 'churning'
        store_layout.open_catalogue(self.root).close()
        self.turns = store_steps.DirectTurns(self.root)
        self.reserve(); self.create(); self.activate()
        runner = Interleave([Q,R])
        with self.assertRaises(store_backup.BackupRefused) as caught:
            store_backup.backup(runner, self.root, 'daily', NOW)
        self.assertEqual(caught.exception.reason, 'present-set-changed')
        self.assertEqual(runner.refusals, ['present-set-changed', 'present-set-changed'])
        self.no_backup()

    def test_holds_refuse_backup(self):
        # S2-M5: removing take's hold check fails the typed assertion, never errors.
        self.turns.run(store_steps.Declared('written', {}), lambda c:
            store_layout.take_holds(c['catalogue'], JOB, 'cutover', [store_layout.project_scope(P)], NOW))
        caught = None
        try:
            self.turns.run(store_steps.Declared('read', {}), lambda c:
                store_backup.take(c, self.root, 'daily', NOW))
        except Exception as exc:
            caught = exc
        self.assertIsInstance(caught, store_backup.BackupRefused)
        self.assertIn(JOB, str(caught.detail))
        self.no_backup()

    def test_then_hold_after_manifest(self):
        # Break: hold before snapshot, premature commit, or before hook too late.
        scopes = [store_layout.project_scope(P)]
        real = store_layout.take_holds
        def after_manifest(conn, *args):
            self.assertEqual(store_backup.last_complete(self.root, 'before-move'), NOW)
            self.assertEqual(conn.execute('SELECT count(*) FROM maintenance_holds').fetchone(), (0,))
            return real(conn, *args)
        with mock.patch.object(store_layout, 'take_holds', side_effect=after_manifest):
            result = store_backup.backup(self.turns, self.root, 'before-move', NOW,
                                         then_hold=(JOB,'cutover',scopes))
        self.assertEqual(self.read(result,'machine.sqlite3','SELECT * FROM maintenance_holds'), [])
        self.assertEqual(self.turns.run(store_steps.Declared('read',{}),lambda c:
                        store_layout.holds_of(c['catalogue'],JOB)), frozenset(scopes))
        self.turns.run(store_steps.Declared('written',{}),lambda c: store_layout.release_holds(c['catalogue'],JOB))
        class Crash(BaseException):
            pass
        with mock.patch.object(store_layout,'take_holds',side_effect=Crash), self.assertRaises(Crash):
            store_backup.backup(self.turns,self.root,'before-move',NOW+1,then_hold=(JOB,'cutover',scopes))
        self.assertEqual(store_backup.last_complete(self.root,'before-move'),NOW+1)
        self.assertEqual(self.turns.run(store_steps.Declared('read',{}),lambda c:
                         store_layout.holds_of(c['catalogue'],JOB)), frozenset())
        def refuse(conns):
            raise store_layout.NameTaken('reserved',P)
        count = len(list((self.root/'backups').iterdir()))
        with self.assertRaises(store_layout.NameTaken):
            store_backup.backup(self.turns,self.root,'before-move',NOW+2,
                                then_hold=(JOB,'cutover',scopes),before=refuse)
        self.assertEqual(len(list((self.root/'backups').iterdir())),count)
        self._ordinary_hold_failure_case()

    def test_prune(self):
        # Break: pruning without a newer complete backup or using one policy for all reasons.
        daily = [store_backup.backup(self.turns,self.root,'daily',NOW+i*DAY) for i in range(10)]
        old = store_backup.backup(self.turns,self.root,'before-move',NOW-31*DAY)
        boundary = store_backup.backup(self.turns,self.root,'before-update',NOW-30*DAY)
        recent = store_backup.backup(self.turns,self.root,'before-move',NOW-DAY)
        future = self.root/'backups'/'future-incomplete'
        future.mkdir(); (future/'partial').write_bytes(b'keep')
        removed = store_backup.prune(self.root,store_backup.BackupSettings(),NOW)
        self.assertEqual(set(removed), {x.path.name for x in daily[:3]} | {old.path.name})
        for item in daily[3:] + [boundary,recent]:
            self.assertTrue((item.path/'manifest.json').is_file())
        self.assertTrue((future/'partial').exists())
        self.assertEqual(store_backup.last_complete(self.root,'daily'),NOW+9*DAY)

    def test_failure_is_visible(self):
        # Break: publishing a partial copy or hiding the original copy failure.
        first = store_backup.backup(self.turns,self.root,'daily',NOW)
        failure = OSError('injected full destination')
        with mock.patch.object(store_layout,'open_catalogue_readonly',side_effect=failure):
            with self.assertRaises(store_backup.BackupRefused) as caught:
                store_backup.backup(self.turns,self.root,'daily',NOW+1)
        self.assertIs(caught.exception.__cause__,failure)
        self.assertEqual(store_backup.last_complete(self.root,'daily'),NOW)
        self.assertEqual(list((self.root/'backups').glob('*/manifest.json')), [first.path/'manifest.json'])
        for boundary in ('file', 'directory'):
            with self.subTest(boundary=boundary):
                self._manifest_sync_case(boundary)
        for removal_fails in (False, True):
            with self.subTest(removal_fails=removal_fails):
                self._copy_cleanup_case(removal_fails)
        for boundary in ('rename', 'backups', 'partial'):
            with self.subTest(publication=boundary):
                self._publication_failure_case(boundary)

    def _new_store(self, name):
        self.root = Path(self.tmp.name) / name
        store_layout.open_catalogue(self.root).close()
        self.turns = store_steps.DirectTurns(self.root)

    def _absent_creating_case(self):
        # Preservation: absent base-creation payload must remain admitted.
        self._new_store('base-pending')
        self.reserve()
        caught, result = None, None
        try:
            result = store_backup.backup(self.turns, self.root, 'daily', NOW)
        except Exception as exc:
            caught = exc
        self.assertIsNone(caught)
        self.assertEqual(set(result.files), {'machine.sqlite3'})
        self.assertEqual(self.read(result, 'machine.sqlite3',
            'SELECT state,held_by_job FROM project_routes'), [('creating', None)])

    def _active_missing_case(self):
        # I2: presence-only enumeration falsely completes a catalogue-only copy.
        self._new_store('active-missing')
        self.reserve(); self.create(); self.activate()
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        store_layout.ledger_path(self.root, P).unlink()
        with self.assertRaises(store_backup.BackupRefused) as caught:
            store_backup.backup(self.turns, self.root, 'daily', NOW + 1)
        self.assertEqual((caught.exception.reason, caught.exception.detail),
                         ('active-ledger-missing', P))
        self.assertEqual(self._published(), [first.path])
        self.assertEqual(self._partial_entries(), [])
        self.assertEqual(store_backup.last_complete(self.root, 'daily'), NOW)

    def _manifest_sync_case(self, boundary):
        # I1: a final-name manifest must not be accepted after publication fails.
        self._new_store('manifest-' + boundary)
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        failure = OSError('injected manifest ' + boundary + ' sync failure')
        real_file, real_dir = store_evidence._sync_file, store_evidence._sync_dir
        def file_sync(file):
            name = Path(file.name).name
            if boundary == 'file' and name.startswith('manifest') and name != 'manifest.sha256':
                file.flush()
                raise failure
            return real_file(file)
        def directory_sync(path):
            path = Path(path)
            if boundary == 'directory' and path != first.path and (path/'manifest.json').is_file():
                raise failure
            return real_dir(path)
        with mock.patch.object(store_evidence, '_sync_file', side_effect=file_sync), mock.patch.object(store_evidence, '_sync_dir', side_effect=directory_sync):
            with self.assertRaises(store_backup.BackupRefused) as caught:
                store_backup.backup(self.turns, self.root, 'daily', NOW + 1)
        self.assertEqual(caught.exception.reason, 'copy-failed')
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(store_backup.last_complete(self.root, 'daily'), NOW)
        self.assertEqual([row[3] for row in store_backup._complete(self.root)], [first.path])

    def _copy_cleanup_case(self, removal_fails):
        # Break: retaining full copied databases, or replacing the copy cause.
        self._new_store('cleanup-' + str(removal_fails))
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        stamp = datetime.fromtimestamp((NOW + 1) / 1000, timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        target = first.path.parent / '.partial' / (stamp + '-daily')
        failure = OSError('injected copy failure after payload')
        cleanup = OSError('injected directory removal failure')
        real_copy, real_remove = store_backup._copy, store_backup.shutil.rmtree
        def fail_copy(source, destination):
            real_copy(source, destination)
            raise failure
        def remove(path, *args, **kwargs):
            if removal_fails and Path(path).name == target.name:
                raise cleanup
            return real_remove(path, *args, **kwargs)
        with mock.patch.object(store_backup, '_copy', side_effect=fail_copy), mock.patch.object(store_backup.shutil, 'rmtree', side_effect=remove):
            with self.assertRaises(store_backup.BackupRefused) as caught:
                store_backup.backup(self.turns, self.root, 'daily', NOW + 1)
        self.assertEqual(caught.exception.reason, 'copy-failed')
        self.assertIs(caught.exception.__cause__, failure)
        if removal_fails:
            self.assertIn(str(cleanup), '\n'.join(getattr(failure, '__notes__', ())))
            self.assertTrue(target.is_dir())
        else:
            self.assertEqual(self._published(), [first.path])
            self.assertEqual(self._partial_entries(), [])
        self.assertEqual(store_backup.last_complete(self.root, 'daily'), NOW)
        self.assertEqual([row[3] for row in store_backup._complete(self.root)], [first.path])

    def _abandoned(self, first, name='20000101T000000.000000Z-daily'):
        path = first.path.parent / '.partial' / name
        path.parent.mkdir(exist_ok=True)
        path.mkdir()
        (path/'machine.sqlite3').write_bytes((first.path/'machine.sqlite3').read_bytes())
        (path/'manifest.tmp').write_bytes(b'killed before publication')
        return path

    def _tree_bytes(self, path):
        return {p.relative_to(path).as_posix(): p.read_bytes()
                for p in path.rglob('*') if p.is_file()}

    def _published(self):
        return sorted(p for p in (self.root/'backups').iterdir() if p.name != '.partial')

    def _partial_entries(self):
        partial = self.root/'backups'/'.partial'
        return sorted(partial.iterdir()) if partial.exists() else []

    def _caught_backup(self, *args, **kwargs):
        caught, result = None, None
        try:
            result = store_backup.backup(self.turns, self.root, *args, **kwargs)
        except Exception as exc:
            caught = exc
        return caught, result

    def _ordinary_hold_failure_case(self):
        # Break: wrapping a post-publication hold fault or deleting its backup.
        self._new_store('ordinary-hold')
        failure = OSError('injected ordinary hold failure')
        real_hold = store_layout.take_holds
        def fail_after_insert(*args, **kwargs):
            real_hold(*args, **kwargs)
            raise failure
        with mock.patch.object(store_layout, 'take_holds', side_effect=fail_after_insert):
            caught, _ = self._caught_backup('before-move', NOW,
                then_hold=(JOB, 'cutover', [store_layout.project_scope(P)]))
        self.assertIs(caught, failure)
        self.assertEqual(len(store_backup._complete(self.root)), 1)
        self.assertEqual(store_backup.last_complete(self.root, 'before-move'), NOW)
        self.assertEqual(self.turns.run(store_steps.Declared('read', {}), lambda c:
            store_layout.holds_of(c['catalogue'], JOB)), frozenset())

    def _publication_failure_case(self, boundary):
        # Break: retaining failed staging or deleting a published backup.
        self._new_store('publication-' + boundary)
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        stamp = datetime.fromtimestamp((NOW + 1)/1000, timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        published = first.path.parent/(stamp + '-daily')
        staging = first.path.parent/'.partial'/published.name
        failure = OSError('injected publication ' + boundary)
        real_rename, real_sync = os.rename, store_evidence._sync_dir
        fired = False
        def rename(src, dst, *args, **kwargs):
            if boundary == 'rename' and Path(src) == staging:
                raise failure
            return real_rename(src, dst, *args, **kwargs)
        def sync(path):
            nonlocal fired
            folder = first.path.parent if boundary == 'backups' else first.path.parent/'.partial'
            if boundary != 'rename' and not fired and Path(path) == folder and published.is_dir():
                fired = True
                raise failure
            return real_sync(path)
        with mock.patch.object(os, 'rename', side_effect=rename), mock.patch.object(store_evidence, '_sync_dir', side_effect=sync):
            caught, _ = self._caught_backup('daily', NOW + 1)
        self.assertIsInstance(caught, store_backup.BackupRefused)
        self.assertEqual(caught.reason, 'copy-failed')
        self.assertIs(caught.__cause__, failure)
        if boundary == 'rename':
            self.assertEqual(self._partial_entries(), [])
            self.assertEqual(self._published(), [first.path])
        else:
            self.assertIn(published, [row[3] for row in store_backup._complete(self.root)])
            self.assertEqual(store_backup.last_complete(self.root, 'daily'), NOW + 1)

    def test_abandoned_remains_removed(self):
        # S2-M13: sweeping the published root destroys damaged complete backups.
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        abandoned = self._abandoned(first)
        with_manifest = self._abandoned(first, 'staged-with-manifest')
        (with_manifest/'manifest.json').write_bytes((first.path/'manifest.json').read_bytes())
        stray = abandoned.parent/'stray-file'; stray.write_bytes(b'stray')
        outside = self.root/'link-target'
        outside.mkdir(); (outside/'keep').write_bytes(b'outside target')
        partial_link = abandoned.parent/'stray-link'
        partial_link.symlink_to(outside, target_is_directory=True)
        damaged = first.path.parent/'20000101T000000.000001Z-before-move'
        damaged.mkdir(); (damaged/'manifest.json').write_bytes(b'{damaged complete manifest')
        unrelated = first.path.parent/'operator-notes'
        unrelated.mkdir(); (unrelated/'note').write_bytes(b'keep')
        link = first.path.parent/'20000101T000000.000002Z-daily'
        link.symlink_to(outside, target_is_directory=True)
        saved_damaged, saved_unrelated = self._tree_bytes(damaged), self._tree_bytes(unrelated)
        saved_link, saved_target = link.readlink(), self._tree_bytes(outside)
        result = store_backup.backup(self.turns, self.root, 'daily', NOW + 1)
        self.assertTrue(damaged.is_dir())
        self.assertEqual(self._tree_bytes(damaged), saved_damaged)
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(self._tree_bytes(unrelated), saved_unrelated)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), saved_link)
        for path in (abandoned, with_manifest, stray, partial_link):
            self.assertFalse(os.path.lexists(path))
        self.assertEqual(self._tree_bytes(outside), saved_target)
        self.assertEqual(result.remains, ())
        self.assertEqual([row[3] for row in store_backup._complete(self.root)], [first.path, result.path])

    def test_unremovable_leftover_named(self):
        # Break: any per-entry filesystem error refusing or hiding a leftover.
        functions = [(os, name) for name in ('lstat', 'stat', 'scandir', 'listdir', 'unlink', 'remove', 'rmdir')]
        functions.append((shutil, 'rmtree'))
        originals = [(module, name, getattr(module, name)) for module, name in functions]
        def run_case(index):
            self._new_store('recording-' + str(index))
            first = store_backup.backup(self.turns, self.root, 'daily', NOW)
            abandoned = self._abandoned(first)
            recorded = []
            def wrap(name, real):
                def call(path, *args, **kwargs):
                    if isinstance(path, (str, bytes, os.PathLike)):
                        absolute = Path(os.path.abspath(os.fsdecode(path)))
                        if absolute == abandoned or abandoned in absolute.parents:
                            position = len(recorded)
                            recorded.append((name, str(absolute.relative_to(abandoned))))
                            if position == index:
                                raise PermissionError('injected entry call ' + str(index))
                    return real(path, *args, **kwargs)
                return call
            with ExitStack() as stack:
                for module, name, real in originals:
                    stack.enter_context(mock.patch.object(module, name, side_effect=wrap(name, real)))
                caught, result = self._caught_backup('daily', NOW + 1)
            return first, abandoned, recorded, caught, result
        first, abandoned, recorded, caught, result = run_case(None)
        self.assertFalse(abandoned.exists())
        self.assertTrue(recorded)
        self.assertIsNone(caught)
        print('ABANDONED_CALLS ' + json.dumps(recorded))
        for index, call in enumerate(recorded):
            with self.subTest(index=index, call=call):
                first, abandoned, actual, caught, result = run_case(index)
                self.assertIsNone(caught)
                self.assertTrue(abandoned.is_dir())
                self.assertEqual(result.remains, (abandoned.name,))
                self.assertEqual([row[3] for row in store_backup._complete(self.root)], [first.path, result.path])

    def test_partial_made_when_absent(self):
        # Break: missing staging bootstrap or following a staging-root symlink.
        store_backup.backup(self.turns, self.root, 'daily', NOW)
        partial = self.root/'backups'/'.partial'
        self.assertTrue(partial.is_dir())
        self.assertFalse(partial.is_symlink())
        self.assertEqual(list(partial.iterdir()), [])
        self.assertEqual(stat.S_IMODE(partial.stat().st_mode), 0o700)
        self._partial_symlink_case()

    def _partial_symlink_case(self):
        self._new_store('partial-symlink')
        outside = self.root/'outside'; outside.mkdir()
        (outside/'keep').write_bytes(b'keep outside')
        backups = self.root/'backups'; backups.mkdir()
        (backups/'.partial').symlink_to(outside, target_is_directory=True)
        saved = self._tree_bytes(outside)
        caught, _ = self._caught_backup('daily', NOW)
        self.assertIsInstance(caught, store_backup.BackupRefused)
        self.assertEqual(caught.reason, 'copy-failed')
        self.assertEqual(self._tree_bytes(outside), saved)
        self.assertEqual(self._published(), [])

    def test_name_taken_never_replaced(self):
        # S2-M15: a bare rename replaces a pre-existing empty destination.
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        saved = self._tree_bytes(first.path)
        caught, _ = self._caught_backup('daily', NOW)
        self.assertIsInstance(caught, store_backup.BackupRefused)
        self.assertEqual(caught.reason, 'copy-failed')
        self.assertEqual(self._tree_bytes(first.path), saved)
        self.assertEqual(self._partial_entries(), [])
        self.assertEqual([row[3] for row in store_backup._complete(self.root)], [first.path])
        stamp = datetime.fromtimestamp((NOW + 1)/1000, timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        empty = first.path.parent/(stamp + '-daily'); empty.mkdir()
        caught, _ = self._caught_backup('daily', NOW + 1)
        self.assertIsInstance(caught, store_backup.BackupRefused)
        self.assertEqual(caught.reason, 'copy-failed')
        self.assertTrue(empty.is_dir())
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(self._partial_entries(), [])

    def test_partial_read_by_no_reader(self):
        # Break: recursively treating staged manifests as published backups.
        first = store_backup.backup(self.turns, self.root, 'daily', NOW)
        partial = first.path.parent/'.partial'; partial.mkdir(exist_ok=True)
        staged = partial/'20000101T000000.000000Z-daily'
        shutil.copytree(first.path, staged)
        document = json.loads((staged/'manifest.json').read_text())
        document['now_ms'] = NOW - 31*DAY
        encoded = (json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n').encode()
        (staged/'manifest.json').write_bytes(encoded)
        (staged/'manifest.sha256').write_text(hashlib.sha256(encoded).hexdigest() + '\n')
        saved = self._tree_bytes(staged)
        self.assertEqual([row[3] for row in store_backup._complete(self.root)], [first.path])
        self.assertEqual(store_backup.last_complete(self.root, 'daily'), NOW)
        self.assertEqual(store_backup.prune(self.root, store_backup.BackupSettings(), NOW), [])
        self.assertTrue(staged.is_dir())
        self.assertEqual(self._tree_bytes(staged), saved)
