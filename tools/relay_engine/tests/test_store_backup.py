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


class TestRestore(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()
        self.reset('store')

    def reset(self, label):
        self.root = self.scratch / label
        store_layout.open_catalogue(self.root).close()
        self.turns = store_steps.DirectTurns(self.root)

    class Crash(Exception):
        pass

    def restore(self, backup, *, confirm=True, job=JOB):
        return store_backup.restore(self.root, getattr(backup, 'path', backup), job,
                                    serving_stopped=True, confirm=confirm, now_ms=NOW + 100)

    def reserve(self, project, holder=None):
        self.turns.run(store_steps.Declared('written', {}), lambda c: store_layout.reserve_project(
            c['catalogue'], project, project, NOW, held_by_job=holder))

    def make(self, project, *, active=True):
        from relay_engine import store_create

        self.reserve(project)
        if active:
            store_create.finish_creation(self.turns, project)
        else:
            with store_layout.open_ledger(self.root, project, create=True) as con:
                con.execute('INSERT INTO projects(id,name,created_at) VALUES(?,?,?)',
                            (project, project, NOW)).close()
            con.close()

    def snapshot(self, root=None):
        return store_backup.backup(self.turns, root or self.root, 'daily', NOW)

    def seal(self, backup, relative, operation):
        path = backup.path / relative
        operation(path)
        manifest = backup.path / 'manifest.json'
        data = json.loads(manifest.read_bytes())
        if relative not in data['files']:
            data['files'][relative] = dict(data['files']['machine.sqlite3'])
        data['files'][relative].update(size=path.stat().st_size,
                                       sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        raw = (json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n').encode()
        manifest.write_bytes(raw)
        (backup.path / 'manifest.sha256').write_text(hashlib.sha256(raw).hexdigest() + '\n')

    def ledger(self, project, sql, root=None):
        from contextlib import closing

        path = store_layout.ledger_path(root or self.root, project)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            return store_backup._rows(conn, sql)

    def pending(self):
        from contextlib import closing

        path = store_layout.catalogue_path(self.root)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as cat:
            return store_backup.pending_creations(cat)

    def digest(self, project, root=None):
        path = store_layout.ledger_path(root or self.root, project)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def fixture_migration(self):
        from relay_engine.tests import test_store_migrate as fixtures

        fixture = fixtures.StoreMigrationCopyTests()
        fixture.scratch = self.scratch
        return fixture, fixtures.NOW_MS

    def seeded(self):
        from contextlib import closing

        for project in (P, Q):
            self.make(project)
            with closing(store_layout.open_ledger(self.root, project)) as conn, conn:
                conn.execute("INSERT INTO team_instances(id,project_id,name,level,opened_at,opened_by)"
                             " VALUES('team',?,'pair','pair',?,'operator')", (project, NOW)).close()
                conn.execute("INSERT INTO seats VALUES('seat','team','implementer','occupied')").close()
                conn.execute("INSERT INTO repositories VALUES('repo',?,'repo')", (project,)).close()
                conn.execute("INSERT INTO bundles(id,project_id,name,opened_by_special)"
                             " VALUES('bundle',?,'bundle','operator')", (project,)).close()
                for handle in (1, 2):
                    item = str(handle)
                    conn.execute(
                        "INSERT INTO relays(id,handle,project_id,kind,needs,subject,sender_special,"
                        " created_at,body_sha256,meta_sha256,body_bytes,header_bytes)"
                        " VALUES(?,?,?,'SITREP','none','fixture','operator',?,?,?,0,0)",
                        (item, handle, project, NOW + handle, 'a' * 64, 'b' * 64)).close()
                    conn.execute(
                        "INSERT INTO slips(id,handle,project_id,kind,seat_id,issuer_special,bundle_id,"
                        " repository_id,issued_in,binds,state,issued_at,revision)"
                        " VALUES(?,?,?,'implement','seat','operator','bundle','repo',?,'{}','live',?,1)",
                        (item, handle, project, item, NOW + handle)).close()
        return self.snapshot()

    def check_held(self, project, slips=2):
        self.assertEqual(self.ledger(project, 'SELECT job_id FROM restore_cancels'), [(JOB,)])
        self.assertEqual(self.ledger(project, "SELECT state,revision FROM slips ORDER BY handle"),
                         [('revoked', 2)] * slips)
        self.assertEqual(self.ledger(project, "SELECT event FROM slip_events"), [('revoke',)] * slips)

    def test_restore_verifies_and_renews(self):
        for change in ('manifest', 'file', 'swap'):
            with self.subTest(change=change):
                self.reset(change)
                self.make(P)
                backup = self.snapshot()
                self.assertTrue(callable(store_backup.restore))
                if change != 'swap':
                    path = backup.path / ('manifest.json' if change == 'manifest' else 'machine.sqlite3')
                    with path.open('ab') as fh:
                        fh.write(b'changed')
                    with self.assertRaises(store_backup.RestoreRefused) as caught:
                        self.restore(backup)
                    self.assertEqual(caught.exception.reason, 'backup-mismatch')
                    continue
                real = store_steps._rename
                def rename(src, dst):
                    real(src, dst)
                    if src == store_layout.catalogue_path(self.root):
                        raise self.Crash()
                with mock.patch.object(store_steps, '_rename', side_effect=rename):
                    with self.assertRaises(self.Crash):
                        self.restore(backup)
                report = self.restore(backup)
                self.assertNotEqual(report.new_store_id, backup.store_id)
                self.assertEqual(store_layout.read_store_id(self.root), report.new_store_id)
                self.check_held(P, slips=0)

    def test_restore_completes_pending_creation(self):
        from relay_engine import store_create

        self.reserve(P)
        report = self.restore(self.snapshot())
        self.assertEqual(self.pending(), [P])
        self.assertEqual(report.being_created, (P,))
        for project in self.pending():
            store_create.finish_creation(self.turns, project)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.ledger(P, 'SELECT * FROM restore_cancels'), [])

    def test_before_move_restore_end_state(self):
        fixture, stamp = self.fixture_migration()
        for removed in (False, True):
            with self.subTest(removed=removed):
                migrate, root, turns, _, job = fixture._cutover_fixture(str(removed), paired=True)
                self.root = root
                _, data = fixture.checkpoint(root, job)
                marker = store_steps.advance_marker
                completed = []
                def first(journal, target, preimage):
                    if completed:
                        raise self.Crash()
                    value = marker(journal, target, preimage)
                    completed.append(target.root_path)
                    return value
                with mock.patch.object(store_steps, 'advance_marker', side_effect=first):
                    with self.assertRaises(self.Crash):
                        migrate.cutover(root, turns, job, stamp + 1)
                backup, = (root / 'backups').glob('*-before-move')
                store_layout.catalogue_path(root).unlink()
                shutil.rmtree(root / 'projects')
                if removed:
                    (root / 'migrations' / job / 'job.sqlite3').unlink()
                report = self.restore(backup)
                move, = report.moved_jobs
                self.assertEqual(move.job_id, job)
                self.assertEqual(move.state, 'unavailable' if removed else 'journal')
                self.assertEqual(tuple(map(Path, move.marked_roots)), () if removed else tuple(map(Path, completed)))
                if removed:
                    self.assertIn(f'The move state for job {job} is unavailable.', report.lines())
                    self.assertFalse(any('still marked' in line for line in report.lines()))
                else:
                    self.assertTrue(any(job in line and str(completed[0]) in line for line in report.lines()))
                self.assertEqual(fixture._cutover_route(store_steps.DirectTurns(root), data['project_id']),
                                 'not-found')
                if not removed:
                    with self.assertRaises(migrate.JobOrphaned):
                        migrate.cutover(root, store_steps.DirectTurns(root), job, stamp + 2)
                self.assertFalse(fixture._cutover_holds(store_steps.DirectTurns(root), job))
                self.assertEqual(store_backup.backup(store_steps.DirectTurns(root), root, 'daily',
                                                     stamp + 3).reason, 'daily')

    def empty_file(self, target, project):
        from contextlib import closing

        with closing(store_layout.open_ledger(self.root, project, create=True)):
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store_layout.ledger_path(self.root, project), target)

    def test_restore_base_creation_after_steps(self):
        from relay_engine import store_create

        for phase in ('reserved', 'empty-file', 'project-row'):
            with self.subTest(phase=phase):
                self.reset(phase)
                if phase == 'project-row':
                    self.make(P, active=False)
                    rows_before = self.ledger(P, 'SELECT * FROM projects')
                else:
                    self.reserve(P)
                backup = self.snapshot()
                if phase == 'empty-file':
                    self.seal(backup, f'projects/{P}/ledger.sqlite3',
                              lambda target: self.empty_file(target, P))
                from relay_engine import restore_cancel
                with mock.patch.object(restore_cancel, 'cancel_all', wraps=restore_cancel.cancel_all) as calls:
                    report = self.restore(backup)
                self.assertEqual(self.pending(), [P])
                self.assertEqual(report.held, (P,) if phase == 'project-row' else ())
                self.assertEqual(report.being_created, () if phase == 'project-row' else (P,))
                self.assertEqual(calls.call_count, int(phase == 'project-row'))
                store_create.finish_creation(self.turns, P)
                self.assertEqual(self.pending(), [])
                self.assertEqual(len(self.ledger(P, 'SELECT id FROM projects')), 1)
                if phase == 'project-row':
                    self.assertEqual(self.ledger(P, 'SELECT * FROM projects'), rows_before)
                    self.check_held(P, slips=0)
                else:
                    self.assertEqual(self.ledger(P, 'SELECT * FROM restore_cancels'), [])

    def test_restore_undone_reservation(self):
        from relay_engine import restore_cancel, store_create

        fixture, stamp = self.fixture_migration()
        migrate, self.root, self.turns, legacy, job, project, _ = fixture._undo_fixture('undo')
        fixture._undo_call(migrate, self.root, self.turns, job)
        with mock.patch.object(restore_cancel, 'cancel_all', wraps=restore_cancel.cancel_all) as calls:
            report = self.restore(self.snapshot())
        self.assertNotIn(project, report.held + report.being_created)
        self.assertFalse(any('being created' in line or 'cancels every' in line for line in report.lines()
                             if line != store_backup.RESTORE_LIMITS[0]))
        calls.assert_not_called()
        self.assertNotIn(project, self.pending())
        with self.assertRaises(store_create.CreationHeld):
            store_create.finish_creation(self.turns, project)
        self.assertFalse(store_layout.ledger_path(self.root, project).exists())
        self.assertEqual(fixture._cutover_route(self.turns, project), 'creating')
        new_job = migrate.start(self.root, self.turns,
                                migrate.MigrationManifest('takeover', 'fixture', (legacy,)), stamp + 10)
        phase, data = fixture.checkpoint(self.root, new_job)
        self.assertEqual((phase, data['project_id'], data['project_name']), ('manifest', project, 'fixture'))

    def test_restore_report_from_journals(self):
        self.make(P)
        self.reserve(Q)
        report = self.restore(self.snapshot(), confirm=False)
        lines = report.lines()
        held = store_backup.HELD_LINE.format(name=P)
        creating = store_backup.BEING_CREATED_LINE.format(name=Q)
        self.assertIn(held, lines)
        self.assertIn(creating, lines)
        self.assertLess(lines.index(held), lines.index(creating))
        self.assertEqual(lines[-7:], store_backup.RESTORE_LIMITS)
        self.assertEqual(lines.count(store_backup.RESTORE_LIMITS[3]), 1)
        self.assertEqual(lines[0], f'Project {P}: no later relay or receipt was found.')
        self.assertEqual(store_backup.RESTORE_LIMITS[3],
                         'During the hold no agent gets a go-ahead, and you end the hold at the terminal.')

    def test_restore_keeps_migrations(self):
        journal_dir = self.root / 'migrations' / JOB
        journal = store_steps.open_journal(journal_dir, create=True)
        journal.checkpoint('fixture', {'source': 'retained'})
        journal.close()
        path = journal_dir / 'job.sqlite3'
        before = path.read_bytes()
        self.restore(self.snapshot())
        self.assertEqual(path.read_bytes(), before)

    def test_restore_cancels_each_staged_ledger_before_swap(self):
        import re
        from relay_engine import restore_cancel

        uninterrupted = None
        for cut in (None, 0, 1, 2):
            with self.subTest(cut=cut):
                self.reset(f'order-{cut}')
                backup = self.seeded()
                original = {p: self.digest(p) for p in (P, Q)}
                calls = []
                cancel = restore_cancel.cancel_all
                prepare = store_steps.prepare_database
                def record(conn, project, job, now):
                    if cut == len(calls) and cut < 2 and not stop[0]:
                        stop[0] = True
                        raise self.Crash()
                    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
                    self.assertEqual(path, self.root / 'recovery' / JOB / 'store'
                                     / 'projects' / project / 'ledger.sqlite3')
                    self.assertFalse(conn.in_transaction)
                    self.assertEqual({p: self.digest(p) for p in (P, Q)}, original)
                    calls.append(project)
                    return cancel(conn, project, job, now)
                def preparing(path):
                    path = Path(path)
                    job = self.root / 'recovery' / JOB
                    if (path.name == 'machine.sqlite3' and path.parent.parent == job
                            and re.fullmatch(r'observe-work-[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}',
                                             path.parent.name)):
                        return prepare(path)
                    self.assertEqual(calls, [P, Q])
                    if cut == 2 and not stop[0]:
                        stop[0] = True
                        raise self.Crash()
                    return prepare(path)
                stop = [False]
                with mock.patch.object(restore_cancel, 'cancel_all', side_effect=record), mock.patch.object(
                        store_steps, 'prepare_database', side_effect=preparing):
                    if cut is not None:
                        with self.assertRaises(self.Crash):
                            self.restore(backup)
                    report = self.restore(backup)
                self.assertEqual(calls, [P, Q])
                for project in (P, Q):
                    self.check_held(project)
                if uninterrupted is None:
                    uninterrupted = report.lines()
                self.assertEqual(report.lines(), uninterrupted)

    def test_restore_cancel_replay_after_commit_before_receipt(self):
        from relay_engine import restore_cancel

        backup = self.seeded()
        staged = self.root / 'recovery' / JOB / 'store'
        cancel, receipt, prepare = restore_cancel.cancel_all, store_steps.Journal.receipt, store_steps.prepare_database
        calls, readers, before_prepare, preparations = [], [], [], []
        flags = {'cancel': False, 'prepare': False}
        def recording(conn, project, job, now):
            if not calls:
                path = store_layout.ledger_path(staged, project)
                # Keep a writable handle open so the final writable close checkpoints
                # even though the actual cancellation connection closes before its receipt.
                anchor = store_layout.open_ledger(staged, project)
                reader = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
                readers.append((reader, anchor))
                self.addCleanup(anchor.close)
                self.addCleanup(reader.close)
                reader.execute('BEGIN').close()
                reader.execute('SELECT count(*) FROM slips').fetchone()
                before_prepare.append(self.digest(project, staged))
            result = cancel(conn, project, job, now)
            calls.append((project, result))
            return result
        def after_commit(journal, ident, observed):
            if calls and not flags['cancel']:
                flags['cancel'] = True
                raise self.Crash()
            if (preparations and not flags['prepare'] and isinstance(observed, dict)
                    and observed.get('sha256') == preparations[-1].sha256):
                flags['prepare'] = True
                raise self.Crash()
            out = receipt(journal, ident, observed)
            if readers and calls[-1][0] == P:
                self.assertEqual(before_prepare[0], self.digest(P, staged))
                reader, anchor = readers.pop()
                reader.close()
                anchor.close()
            return out
        def preparing(path):
            result = prepare(path)
            if Path(path) == store_layout.ledger_path(staged, P):
                self.assertNotEqual(before_prepare[0], result.sha256)
                preparations.append(result)
            return result
        with mock.patch.object(restore_cancel, 'cancel_all', side_effect=recording), mock.patch.object(
                store_steps.Journal, 'receipt', after_commit), mock.patch.object(
                store_steps, 'prepare_database', side_effect=preparing):
            for _ in range(2):
                with self.assertRaises(self.Crash):
                    self.restore(backup)
            self.restore(backup)
        self.assertEqual([project for project, _ in calls], [P, P, Q])
        self.assertTrue(calls[1][1].already)
        self.assertEqual(calls[1][1].cut_handle, calls[0][1].cut_handle)
        for project in (P, Q):
            self.check_held(project)
        self.assertEqual(len(preparations), 2)
        self.assertEqual(preparations[0], preparations[1])
        from contextlib import closing
        control = self.scratch / 'stale-digest-control'
        control.mkdir()
        source, target = control / 'source.sqlite3', control / 'target.sqlite3'
        shutil.copyfile(store_layout.ledger_path(self.root, P), source)
        prepared = prepare(source)
        with closing(store_steps.open_journal(control, create=True)) as journal:
            with self.assertRaises(store_steps.FenceBreach):
                store_steps.move_database(journal, store_steps.Prepared(prepared.size, before_prepare[0]),
                                          source, target)
        self.assertTrue(source.is_file())
        self.assertFalse(target.exists())

    def test_restore_empty_authority_project_held(self):
        self.make(P)
        report = self.restore(self.snapshot())
        self.check_held(P, slips=0)
        self.assertEqual(report.held, (P,))
        self.assertIn(store_backup.HELD_LINE.format(name=P), report.lines())

    def test_restore_refused_confirmation_leaves_live(self):
        self.make(P)
        backup = self.snapshot()
        old = store_layout.read_store_id(self.root), self.digest(P)
        report = self.restore(backup, confirm=False)
        self.assertNotEqual(report.new_store_id, old[0])
        self.assertEqual((store_layout.read_store_id(self.root), self.digest(P)), old)
        self.assertEqual(self.ledger(P, 'SELECT * FROM restore_cancels'), [])

    def test_restore_classing_refusals(self):
        from contextlib import closing
        from relay_engine import restore_cancel

        for reason in ('active-without-row', 'relays-without-row', 'held', 'absent'):
            with self.subTest(reason=reason):
                self.reset(reason)
                if reason != 'absent':
                    self.make(P, active=reason == 'active-without-row')
                backup = self.snapshot()
                if reason == 'held':
                    def hold(path):
                        with closing(sqlite3.connect(path)) as cat, cat:
                            cat.execute('UPDATE project_routes SET held_by_job=? WHERE project_id=?',
                                        (JOB, P)).close()
                    self.seal(backup, 'machine.sqlite3', hold)
                def alter(path):
                    if reason == 'absent':
                        self.empty_file(path, P)
                    elif reason in ('active-without-row', 'relays-without-row'):
                        with closing(sqlite3.connect(path)) as conn, conn:
                            if reason == 'relays-without-row':
                                conn.execute("INSERT INTO relays(id,handle,project_id,kind,needs,subject,"
                                             " sender_special,created_at,body_sha256,meta_sha256,body_bytes,"
                                             " header_bytes) VALUES('r',1,?,'SITREP','none','test','operator',?,"
                                             " ?,?,0,0)", (P, NOW, 'a'*64, 'b'*64)).close()
                            conn.execute('DELETE FROM projects').close()
                self.seal(backup, f'projects/{P}/ledger.sqlite3', alter)
                self.assertTrue(callable(store_backup.restore))
                with mock.patch.object(restore_cancel, 'cancel_all', wraps=restore_cancel.cancel_all) as calls:
                    with self.assertRaises(store_backup.RestoreRefused) as caught:
                        self.restore(backup)
                self.assertEqual(caught.exception.reason, 'unclassed-ledger')
                self.assertEqual(caught.exception.detail[0], P)
                calls.assert_not_called()
                with closing(store_steps.open_journal(self.root / 'recovery' / JOB)) as journal:
                    self.assertNotIn('restore-classes', [x[0] for x in store_backup._rows(
                        journal._conn, 'SELECT phase FROM checkpoints')])

    def stop_write(self, method, scope, project, by):
        return self.turns.run(store_steps.Declared('written', {}), lambda c: method(
            c['catalogue'], scope, project, by))

    def stop_rows(self, root=None):
        from contextlib import closing
        cat = store_layout.catalogue_path(root or self.root)
        with closing(store_backup._offline_read(cat)) as conn:
            return tuple(store_backup._rows(conn, sql) for sql in (
                'SELECT * FROM engine_stops ORDER BY row_id',
                'SELECT * FROM engine_stop_events ORDER BY event_id',
                "SELECT position,epoch,anchor_id FROM queue_highwaters WHERE project_id IS NULL AND source='stop_events'",
            ))

    def history(self):
        self.make(P)
        self.make(Q)
        self.stop_write(store_layout.record_stop, 'project', P, 'old')
        self.stop_write(store_layout.record_start, 'project', P, 'old')
        self.stop_write(store_layout.record_stop, 'project', Q, 'old')
        backup = self.snapshot()
        self.stop_write(store_layout.record_start, 'project', Q, 'new')
        self.stop_write(store_layout.record_stop, 'project', P, 'new')
        self.stop_write(store_layout.record_stop, 'store', None, 'new')
        self.turns.run(store_steps.Declared('written', {}), lambda c: c['catalogue'].execute(
            "INSERT INTO queue_highwaters VALUES (NULL,'stop_events',6,3,NULL)").close())
        return backup

    def journal_path(self, job=JOB):
        return self.root / 'recovery' / job

    def checkpoint_data(self, phase, job=JOB):
        from contextlib import closing
        with closing(store_steps.open_journal(self.journal_path(job))) as journal:
            data, = store_backup._rows(journal._conn, 'SELECT data FROM checkpoints WHERE phase=?', (phase,))
            return json.loads(data[0])

    def stop_before(self, phase, backup, *, confirm=False):
        write = store_steps.Journal.checkpoint
        def cut(journal, step, data):
            if step == phase:
                raise self.Crash()
            return write(journal, step, data)
        with mock.patch.object(store_steps.Journal, 'checkpoint', cut):
            with self.assertRaises(self.Crash):
                self.restore(backup, confirm=confirm)

    def test_restore_carries_the_stopped_state(self):
        backup = self.history()
        rows = self.stop_rows()
        self.assertEqual(tuple(map(len, rows)), (4, 6, 1))
        self.restore(backup, confirm=False)
        staged_rows = self.stop_rows(self.journal_path() / 'store')
        self.restore(backup)
        open_count = sum(row[2] is None and row[5] is None for row in self.stop_rows()[0])
        self.assertEqual(open_count, 1)
        self.assertEqual(staged_rows, rows)
        self.assertEqual(self.stop_rows(), rows)
        self.assertGreater(self.stop_write(store_layout.record_start, 'store', None, 'next'), 6)

    def test_restore_rebuild_keeps_guards(self):
        from contextlib import closing
        from relay_engine import catalogue
        backup = self.history()
        live = self.stop_rows()
        self.make(R)
        self.stop_write(store_layout.record_stop, "project", R, "filtered")
        self.restore(backup)
        self.assertEqual(self.stop_rows(), live)
        with closing(store_layout.open_catalogue(self.root)) as conn:
            catalogue._require_matching(conn)
            self.assertEqual(store_backup._rows(conn, 'PRAGMA foreign_key_check'), [])
            for table in ('engine_stops', 'engine_stop_events'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, table + ' cannot be deleted'):
                    conn.execute('DELETE FROM ' + table).close()

        with self.subTest(validation='post-rebuild schema'):
            self.reset('post-rebuild-schema')
            backup = self.snapshot()
            self.stop_write(store_layout.record_stop, 'store', None, 'live-only')
            live = store_layout.catalogue_path(self.root)
            live_before = live.read_bytes()
            staged = self.journal_path() / 'store'
            staged_cat = store_layout.catalogue_path(staged)
            matching = catalogue._require_matching
            tampered = []

            def mismatch_after_rebuild(conn):
                database = store_backup._rows(conn, 'PRAGMA database_list')[0][2]
                if (database == str(staged_cat) and conn.in_transaction
                        and store_backup._rows(conn, "SELECT stopped_by FROM engine_stops") == [('live-only',)]):
                    conn.execute('DROP TRIGGER engine_stops_guard_delete').close()
                    tampered.append(database)
                return matching(conn)

            with mock.patch.object(catalogue, '_require_matching', mismatch_after_rebuild):
                with self.assertRaises(store_steps.FenceBreach) as stopped:
                    self.restore(backup)
            self.assertEqual(tampered, [str(staged_cat)])
            self.assertEqual(stopped.exception.__cause__.code, 'E-SCHEMA-MISMATCH')
            self.assertEqual(live.read_bytes(), live_before)
            with closing(store_steps.open_journal(self.journal_path())) as journal:
                self.assertEqual(store_backup._rows(journal._conn,
                    "SELECT r.intent_id FROM intents i LEFT JOIN receipts r ON i.id=r.intent_id"
                    " WHERE i.step='restore-carry'"), [(None,)])
            with closing(store_backup._offline_read(staged_cat)) as conn:
                matching(conn)
                self.assertEqual(store_backup._rows(conn, 'SELECT * FROM engine_stops'), [])

    def test_restore_stop_not_kept_line(self):
        backup = self.history()
        self.make(R)
        self.stop_write(store_layout.record_stop, 'project', R, 'new')
        failure = None
        try:
            report = self.restore(backup)
        except (sqlite3.Error, store_steps.FenceBreach) as exc:
            failure = exc
        self.assertIsNone(failure)
        self.assertEqual(report.stops_not_kept, (R,))
        rows, events, _ = self.stop_rows()
        self.assertNotIn(R, [row[2] for row in rows])
        self.assertLessEqual({row[1] for row in events}, {row[0] for row in rows})
        lines = report.lines()
        pos = lines.index(store_backup.STOP_NOT_KEPT_LINE.format(name=R))
        self.assertEqual(lines[pos + 1], store_backup.NOTICES_LINE)
        self.assertEqual(lines[pos + 2:], store_backup.RESTORE_LIMITS)

    def test_lost_store_restore_leaves_store_stopped(self):
        for opaque in (False, True):
            with self.subTest(opaque=opaque):
                self.reset('lost-'+str(opaque))
                backup = self.snapshot()
                main = store_layout.catalogue_path(self.root)
                main.unlink()
                if opaque:
                    main.write_bytes(b'old unreadable catalogue')
                    Path(str(main)+'-wal').write_bytes(b'opaque wal')
                receipt = store_steps.Journal.receipt
                def cut(journal, ident, observed):
                    if store_backup._rows(journal._conn, 'SELECT step FROM intents WHERE id=?',
                                          (ident,)) == [('restore-lost-stop',)]:
                        raise self.Crash()
                    receipt(journal, ident, observed)
                with mock.patch.object(store_steps.Journal, 'receipt', cut):
                    with self.assertRaises(self.Crash):
                        store_backup.restore(self.root, backup.path, JOB, serving_stopped=True,
                                             confirm=True, now_ms=NOW+100, stopped_by='named-restore')
                report = self.restore(backup)
                self.assertEqual(report.mode, 'lost')
                rows, events, _ = self.stop_rows()
                self.assertEqual([(r[1], r[4], r[5]) for r in rows], [('store', 'named-restore', None)])
                self.assertEqual(len(events), 1)
                if opaque:
                    lost = self.journal_path() / 'lost'
                    self.assertEqual((lost / main.name).read_bytes(), b'old unreadable catalogue')
                    self.assertEqual((lost / (main.name+'-wal')).read_bytes(), b'opaque wal')

    def test_restore_observation_writes_nothing_live(self):
        for absent in (False, True):
            with self.subTest(absent=absent):
                self.reset('observe-'+str(absent))
                backup = self.snapshot()
                main = store_layout.catalogue_path(self.root)
                if absent:
                    main.unlink()
                def members():
                    return {p.name: (p.stat().st_mode, p.read_bytes()) for p in main.parent.glob(main.name+'*')}
                before = members()
                self.stop_before('restore-staged', backup)
                self.assertEqual(members(), before)
                start = self.checkpoint_data('restore-start')
                self.assertEqual(start['mode'], 'lost' if absent else 'readable')
                self.assertEqual(start['job_id'], JOB)
                self.assertEqual([record['name'] for record in start['observed']], [] if absent else [main.name])
                if not absent:
                    copy = self.journal_path() / 'observed' / main.name
                    self.assertEqual(copy.read_bytes(), before[main.name][1])
                    self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o400)
                else:
                    with store_layout.open_catalogue(self.root) as conn:
                        self.assertTrue(main.is_file())
                    conn.close()

    def test_restore_observation_restart(self):
        backup = self.snapshot()
        folder = self.journal_path()
        partial = folder / 'observed.partial'
        partial.mkdir(parents=True)
        (partial / 'machine.sqlite3').write_bytes(b'incomplete copy')
        self.stop_before('restore-start', backup)
        main = store_layout.catalogue_path(self.root)
        observed = folder / 'observed' / main.name
        self.assertEqual(observed.read_bytes(), main.read_bytes())
        self.assertFalse(partial.exists())
        with observed.open('rb') as prior:
            self.restore(backup, confirm=False)
            self.assertNotEqual(observed.stat().st_ino, os.fstat(prior.fileno()).st_ino)
        pinned = observed.read_bytes()
        with main.open('ab') as file:
            file.write(b'outside drift')
        with self.assertRaises(store_steps.FenceBreach):
            self.restore(backup)
        self.assertEqual(observed.read_bytes(), pinned)

    def test_restore_live_fence(self):
        from contextlib import closing
        for control in ('prepared', 'shm', 'observed', 'payload'):
            with self.subTest(control=control):
                self.reset('fence-'+control)
                backup = self.snapshot()
                main = store_layout.catalogue_path(self.root)
                # Leave committed frames without an open writer for the restore.
                conn = sqlite3.connect(main, isolation_level=None)
                conn.execute('PRAGMA journal_mode=WAL').close()
                conn.execute('PRAGMA wal_autocheckpoint=0').close()
                before_write = main.read_bytes()
                conn.execute("INSERT INTO queue_highwaters VALUES(NULL,'stop_events',3,2,NULL)").close()
                outstanding = Path(str(main)+'-wal').read_bytes()
                conn.close()
                self.assertGreater(len(outstanding), 32)
                # Restore the pre-transaction main; closing the last connection checkpointed it.
                self.assertNotEqual(main.read_bytes(), before_write)
                main.write_bytes(before_write)
                Path(str(main)+'-wal').write_bytes(outstanding)
                permit = store_steps.Journal.receipt
                def cut(journal, ident, observed):
                    if store_backup._rows(journal._conn, "SELECT step FROM intents WHERE id=? AND target LIKE ?",
                                          (ident, '%/replaced/machine.sqlite3%')) == [('restore-prepare',)]:
                        raise self.Crash()
                    permit(journal, ident, observed)
                with mock.patch.object(store_steps.Journal, 'receipt', cut):
                    with self.assertRaises(self.Crash):
                        self.restore(backup)
                self.assertFalse(Path(str(main)+'-wal').exists())
                start = self.checkpoint_data('restore-start')
                self.assertEqual(start['observed'][0]['sha256'], hashlib.sha256(before_write).hexdigest())
                self.assertNotEqual(start['prepared']['sha256'], start['observed'][0]['sha256'])
                if control == 'shm':
                    Path(str(main)+'-shm').write_bytes(b'appearance')
                elif control == 'observed':
                    p = self.journal_path() / 'observed' / main.name
                    p.chmod(0o600)
                    p.write_bytes(p.read_bytes()+b'changed input')
                elif control == 'payload':
                    with closing(store_steps.open_journal(self.journal_path())) as journal, journal._conn:
                        val = self.checkpoint_data('restore-start')
                        val['payload']['routes'] = [{'project_id': P, 'name': P, 'state': 'active'}]
                        journal._conn.execute("UPDATE checkpoints SET data=? WHERE phase='restore-start'",
                                              (json.dumps(val),)).close()
                before = main.read_bytes()
                if control == 'prepared':
                    failure = None
                    try:
                        report = self.restore(backup)
                    except store_steps.FenceBreach as exc:
                        failure = exc
                    self.assertIsNone(failure)
                    self.assertEqual(report.mode, 'readable')
                    self.assertEqual(self.stop_rows()[2], [(3, 2, None)])
                else:
                    with self.assertRaises(store_steps.FenceBreach):
                        self.restore(backup)
                    self.assertEqual(main.read_bytes(), before)

    def test_restore_overlap_restore_side(self):
        from contextlib import closing
        saved_job = JOB[:-1]+'9'
        for mode in ('readable', 'lost'):
            with self.subTest(mode=mode):
                self.reset('overlap-'+mode)
                backup = self.snapshot()
                if mode == 'lost':
                    store_layout.catalogue_path(self.root).unlink()
                self.restore(backup, confirm=False)
                with self.assertRaises(store_backup.RestoreRefused) as rejected:
                    self.restore(backup, job=saved_job)
                self.assertEqual((rejected.exception.reason, rejected.exception.detail), ('job-open', JOB))
                self.assertEqual(self.restore(backup, confirm=False).mode, mode)
                start = self.checkpoint_data('restore-start')
                proof = hashlib.sha256(json.dumps(start, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                with closing(store_steps.open_journal(self.journal_path())) as journal:
                    journal.checkpoint('restore-abandoned', dict(job_id=JOB, start_sha256='bad'))
                self.assertIn(JOB, store_backup.open_restores(self.root))
                with closing(store_steps.open_journal(self.journal_path())) as journal:
                    journal.checkpoint('restore-abandoned', dict(job_id=saved_job, start_sha256=proof))
                self.assertIn(JOB, store_backup.open_restores(self.root))
                with closing(store_steps.open_journal(self.journal_path())) as journal:
                    journal.checkpoint('restore-abandoned', dict(job_id=JOB, start_sha256=proof))
                self.assertNotIn(JOB, store_backup.open_restores(self.root))
                with self.assertRaises(store_backup.RestoreRefused):
                    self.restore(backup)
                self.restore(backup, job=saved_job, confirm=False)

    def test_restore_admits_refused_undo(self):
        from contextlib import closing
        fixture, _ = self.fixture_migration()
        for case in ('native', 'queue', 'mismatch', 'pending', 'reopened'):
            with self.subTest(case=case):
                migrate, root, self.turns, legacy, job, project, _ = fixture._undo_fixture(
                    'restore-overlap-'+case)
                self.root = root
                if case == 'native':
                    fixture._undo_native_relay(self.turns, project)
                    reject = None
                else:
                    def reject(*args, **kwargs):
                        raise fixture._QueueBlocked(('blocked',))
                with self.assertRaises(migrate.UndoRefused):
                    fixture._undo_call(migrate, root, self.turns, job, reject)
                backup = self.snapshot()
                if case not in ('native', 'queue'):
                    with closing(store_steps.open_journal(fixture.job_path(root, job))) as journal, journal._conn:
                        rid, raw = store_backup._rows(journal._conn,
                            "SELECT id,target FROM intents WHERE step='undo-refuse'")[-1]
                        if case == 'mismatch':
                            target = json.loads(raw)
                            target['route_intent'] += 999
                            encoded = json.dumps(target)
                            journal._conn.execute('UPDATE intents SET target=? WHERE id=?', (encoded, rid)).close()
                            journal._conn.execute('UPDATE receipts SET observed=? WHERE intent_id=?',
                                                  (encoded, rid)).close()
                        elif case == 'pending':
                            journal._conn.execute('DELETE FROM receipts WHERE intent_id=?', (rid,)).close()
                        else:
                            route, = store_backup._rows(journal._conn,
                                "SELECT id FROM intents WHERE step='undo-route'")[0]
                            journal.receipt(route, {'reopened': True})
                if case in ('native', 'queue'):
                    self.assertEqual(self.restore(backup, confirm=False).mode, 'readable')
                else:
                    with self.assertRaises(store_backup.RestoreRefused) as rejected:
                        self.restore(backup)
                    self.assertEqual((rejected.exception.reason, rejected.exception.detail),
                                     ('job-open', job))

    def test_lost_rename_resumes_per_member(self):
        for edge in ('machine.sqlite3', 'machine.sqlite3-wal', 'replacement', 'occupied', 'newwal'):
            with self.subTest(edge=edge):
                self.reset('rename-'+edge)
                backup = self.snapshot()
                main = store_layout.catalogue_path(self.root)
                content = {main.name: b'bad main'}
                if edge != 'newwal':
                    content.update({main.name+'-wal': b'bad wal', main.name+'-shm': b'bad shm'})
                for name, data in content.items():
                    (self.root/name).write_bytes(data)
                ino = main.stat().st_ino
                if edge in content:
                    if edge == main.name:
                        rename = os.rename
                        def cut(src, dst):
                            rename(src, dst)
                            if Path(dst) == self.journal_path()/'lost'/edge:
                                raise self.Crash()
                        patch = mock.patch.object(os, 'rename', cut)
                    else:
                        receipt = store_steps.Journal.receipt
                        def after(journal, ident, value):
                            receipt(journal, ident, value)
                            if store_backup._rows(journal._conn, "SELECT step FROM intents WHERE id=? AND target LIKE ?",
                                                  (ident, '%'+edge+'%')) == [('restore-lost-move',)]:
                                raise self.Crash()
                        patch = mock.patch.object(store_steps.Journal, 'receipt', after)
                    with patch, self.assertRaises(self.Crash):
                        self.restore(backup)
                    self.assertEqual(self.restore(backup).mode, 'lost')
                    self.assertEqual({p.name:p.read_bytes() for p in (self.journal_path()/'lost').iterdir()},
                                     content)
                    self.assertEqual((self.journal_path()/'lost'/main.name).stat().st_ino, ino)
                    self.assertNotEqual(main.stat().st_ino, ino)
                else:
                    self.restore(backup, confirm=False)
                    if edge == 'replacement':
                        other = main.with_suffix('.replacement')
                        other.write_bytes(b'replacement')
                        os.replace(other, main)
                        changed = main
                    elif edge == 'occupied':
                        changed = self.journal_path()/'lost'/main.name
                        changed.parent.mkdir()
                        changed.write_bytes(b'unrelated')
                    else:
                        changed = Path(str(main)+'-wal')
                        changed.write_bytes(b'appeared')
                    before = changed.read_bytes()
                    with self.assertRaises(store_steps.FenceBreach):
                        self.restore(backup)
                    self.assertEqual(changed.read_bytes(), before)

    def test_restore_excluded_pinned(self):
        backup = self.snapshot()
        self.make(P)
        self.stop_write(store_layout.record_stop, 'project', P, 'new')
        self.assertEqual(self.restore(backup, confirm=False).stops_not_kept, (P,))
        self.assertEqual(self.checkpoint_data('restore-excluded'), [P])
        self.assertEqual(self.restore(backup).stops_not_kept, (P,))
        self.assertEqual(self.restore(backup).stops_not_kept, (P,))

    def test_restore_jobs_lock(self):
        import errno
        import fcntl
        backup = self.snapshot()
        lock = self.root/'jobs.lock'
        with lock.open('xb') as holder:
            lock.chmod(0o600)
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            jobs_lock = store_backup.jobs_lock
            with mock.patch.object(store_backup, 'jobs_lock', side_effect=lambda root: jobs_lock(root, wait_s=0.2)):
                with self.assertRaises(store_backup.RestoreRefused) as busy:
                    self.restore(backup)
            self.assertEqual((busy.exception.reason, busy.exception.detail), ('jobs-busy', None))
            self.assertFalse(self.journal_path().exists())
        flock = fcntl.flock
        def inspect(fd, op):
            self.assertFalse(os.get_inheritable(fd))
            raise OSError(errno.ENOLCK, 'no lock')
        with mock.patch.object(fcntl, 'flock', inspect):
            with self.assertRaises(store_backup.RestoreRefused) as failed:
                self.restore(backup)
        self.assertEqual((failed.exception.reason, failed.exception.detail), ('jobs-lock-error', 'ENOLCK'))
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertIs(fcntl.flock, flock)

    def test_unmark_offer_text(self):
        offer = "Project {name} is offline. The restored backup is from before {name} moved, but {name}'s old folders are still marked as moved. Restore {name}'s old folders so 2.9.7 can use them again? Work done in v3 after the backup is not in the restored backup."
        one = store_backup.MovedJob(JOB, 'sample', ('/fixture/old',), 'journal')
        unavailable = store_backup.MovedJob(JOB[:-1]+'2', 'unavailable', (), 'unavailable')
        missing = store_backup.MovedJob(JOB[:-1]+'3', 'unlisted', ('/fixture/other',), 'journal')
        loss = store_backup.RestoreLoss(P, 'sample', None, None, True)
        report = store_backup.RestoreReport('store', (loss,), [one, unavailable, missing], (P,), ())
        lines = report.lines()
        self.assertEqual(store_backup.UNMARK_OFFER_LINE, offer)
        self.assertEqual(lines[lines.index(loss.line())+1], offer.format(name='sample'))
        self.assertEqual(lines[lines.index(missing.line())+1], offer.format(name='unlisted'))
        self.assertEqual(lines.count(offer.format(name='sample')), 1)
        self.assertNotIn(offer.format(name='unavailable'), lines)
