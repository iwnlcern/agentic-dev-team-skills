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

    def test_preimage_before_change(self):
        directory_modes = {
            '.': 0o750,
            'seats': 0o750,
            'seats/fixture-seat': 0o500,
            'drafts': 0o710,
            'drafts/fixture-seat': 0o700,
            'drafts/fixture-seat/nested': 0o550,
        }
        file_modes = {
            'daemon.json': 0o640,
            'daemon.log': 0o600,
            'seats/fixture-seat/key': 0o400,
            'drafts/fixture-seat/top.md': 0o640,
            'drafts/fixture-seat/nested/inner.md': 0o440,
        }

        def inventory(root):
            entries = {}
            for path in (root, *root.rglob('*')):
                mode = path.lstat().st_mode
                relative = path.relative_to(root).as_posix()
                if stat.S_ISDIR(mode):
                    entries[relative] = ('directory', None, stat.S_IMODE(mode))
                else:
                    self.assertTrue(stat.S_ISREG(mode), relative)
                    entries[relative] = ('file', path.read_bytes(), stat.S_IMODE(mode))
            return entries

        def restore_owner_access(root):
            root.chmod(stat.S_IMODE(root.stat().st_mode) | stat.S_IRWXU)
            for path in root.iterdir():
                if path.is_dir() and not path.is_symlink():
                    restore_owner_access(path)

        inventories = {}
        for label in ('original', 'sibling'):
            root = self.root / label
            root.mkdir()
            self.addCleanup(restore_owner_access, root)
            engine = root / '.engine'
            files = {
                'daemon.json': b'{"state":"stopped","fixture":"' + label.encode() + b'"}\n',
                'daemon.log': f'{label} legacy log\n'.encode(),
                'seats/fixture-seat/key': f'{label} legacy key\n'.encode(),
                'drafts/fixture-seat/top.md': f'{label} top draft\n'.encode(),
                'drafts/fixture-seat/nested/inner.md': f'{label} nested draft\n'.encode(),
            }
            for relative in directory_modes:
                (engine / relative).mkdir()
            for relative, data in files.items():
                path = engine / relative
                path.write_bytes(data)
                mode = file_modes[relative]
                path.chmod(mode | 0o200 if label == 'sibling' else mode)
            for relative, mode in reversed(directory_modes.items()):
                (engine / relative).chmod(mode | 0o200 if label == 'sibling' else mode)

            control = root / 'control.txt'
            control.write_bytes(f'{label} unchanged control\n'.encode())
            control.chmod(0o640)
            expected = {
                relative: ('directory', None, mode | 0o200 if label == 'sibling' else mode)
                for relative, mode in directory_modes.items()
            }
            expected.update({
                relative: ('file', data, file_modes[relative] | 0o200 if label == 'sibling'
                           else file_modes[relative])
                for relative, data in files.items()
            })
            self.assertEqual(inventory(engine), expected)
            self.assertEqual(inventory(control), {
                '.': ('file', f'{label} unchanged control\n'.encode(), 0o640),
            })
            inventories[engine] = expected

        self.assertTrue((self.job_path / 'job.sqlite3').is_file())
        self.assertEqual(self.journal.pending(), [])
        self.assertFalse((self.job_path / 'originals' / '1').exists())
        for engine, expected in inventories.items():
            self.assertEqual(inventory(engine), expected)
        self.assertTrue(callable(getattr(store_steps, 'capture_preimage', None)),
                        'missing store_steps.capture_preimage')

        import json

        def journal_rows():
            connection = sqlite3.connect((self.job_path / 'job.sqlite3').as_uri() + '?mode=ro',
                                         uri=True)
            try:
                return tuple(connection.iterdump())
            finally:
                connection.close()

        original = self.root / 'original'
        sibling = self.root / 'sibling'
        originals = self.job_path / 'originals'
        destination = originals / '1'
        rejected = originals / '2'
        expected_original = inventories[original / '.engine']
        expected_manifest = []
        for relative, (kind, data, mode) in sorted(expected_original.items()):
            entry = dict(path=relative, kind=kind, mode=mode)
            if kind == 'file':
                entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            expected_manifest.append(entry)

        controls = {root / 'control.txt': inventory(root / 'control.txt')
                    for root in (original, sibling)}
        before_journal = journal_rows()
        real_sync_file, real_sync_dir = store_steps._sync_file, store_steps._sync_dir
        completed = []

        def record_file(stream):
            real_sync_file(stream)
            completed.append(('file', Path(stream.name), destination.is_dir()))

        def record_dir(path):
            real_sync_dir(path)
            completed.append(('directory', Path(path), destination.is_dir()))

        def check_inputs():
            for engine, expected in inventories.items():
                self.assertEqual(inventory(engine), expected)
            for control, expected in controls.items():
                self.assertEqual(inventory(control), expected)
            self.assertEqual(journal_rows(), before_journal)
            self.assertEqual(self.journal.pending(), [])

        def restore_capture_access():
            if destination.is_dir():
                restore_owner_access(destination)

        self.addCleanup(restore_capture_access)
        with (mock.patch.object(store_steps, '_sync_file', record_file),
              mock.patch.object(store_steps, '_sync_dir', record_dir)):
            manifest = store_steps.capture_preimage(original, destination)

        self.assertIsInstance(manifest, store_steps.PreimageManifest)
        self.assertEqual(list(manifest.entries), expected_manifest)
        persisted = destination / 'manifest.json'
        expected_json = json.loads(persisted.read_text(encoding='utf-8'))
        self.assertEqual(expected_json, {'entries': expected_manifest})
        self.assertEqual(inventory(destination / 'payload'), expected_original)
        self.assertEqual({entry.name for entry in destination.iterdir()}, {'manifest.json', 'payload'})
        self.assertEqual(list(originals.iterdir()), [destination])
        check_inputs()

        self.assertEqual(completed[-1], ('directory', originals, True))
        staging = completed[-2][1]
        self.assertEqual(staging.parent, originals)
        self.assertRegex(staging.name, r'^\.preimage-')
        self.assertEqual(completed[-2], ('directory', staging, False))
        payload = staging / 'payload'
        expected_files = {payload / relative for relative, (kind, _, _) in expected_original.items()
                          if kind == 'file'}
        expected_dirs = {payload / relative for relative, (kind, _, _) in expected_original.items()
                         if kind == 'directory'}
        files = [path for kind, path, visible in completed if kind == 'file']
        self.assertCountEqual(files, expected_files | {staging / persisted.name})
        directories = [path for kind, path, visible in completed
                       if kind == 'directory' and path in expected_dirs]
        self.assertCountEqual(directories, expected_dirs)
        for position, (_, path, visible) in enumerate(completed[:-1]):
            self.assertFalse(visible, path)
            if path in expected_dirs:
                self.assertFalse(any(later.is_relative_to(path) and later != path
                                     for _, later, _ in completed[position + 1:]), path)
        before_success = inventory(destination)

        injected = OSError('injected first preimage payload sync failure')
        failed_sync = []

        def fail_first_payload(stream):
            path = Path(stream.name)
            if 'payload' in path.relative_to(originals).parts and not failed_sync:
                failed_sync.append(path)
                raise injected
            record_file(stream)

        with (mock.patch.object(store_steps, '_sync_file', fail_first_payload),
              mock.patch.object(store_steps, '_sync_dir', record_dir),
              self.assertRaises(OSError) as failure):
            store_steps.capture_preimage(sibling, rejected)

        self.assertIs(failure.exception, injected)
        self.assertEqual(len(failed_sync), 1)
        self.assertFalse(failed_sync[0].exists())
        self.assertFalse(rejected.exists())
        self.assertEqual(list(originals.iterdir()), [destination])
        self.assertEqual(inventory(destination), before_success)
        check_inputs()
        print('preimage syncs:',
              [(kind, str(path.relative_to(self.job_path)), visible)
               for kind, path, visible in completed],
              '| injected payload:', failed_sync[0].name,
              '| preserved roots, controls, capture and journal')

    def test_marker_matches_pinned_shape(self):
        import json

        copied_files = {
            'seats/fixture-seat/key': b'synthetic seat key\n',
            'drafts/fixture-seat/top.md': b'synthetic top draft\n',
            'drafts/fixture-seat/nested/inner.md': b'synthetic nested draft\n',
        }
        legacy_files = {
            **copied_files,
            'daemon.json': b'{"state": "stopped", "pid": 123}\n',
            'daemon.log': b'legacy daemon log\n',
        }
        marker_bytes = (json.dumps({
            'state': 'ready',
            'socket': '/nonexistent/adt-v3-moved/s',
            'identity': {
                'kit': '3.0.0',
                'fp': '0' * 64,
                'install': '/MOVED TO KIT 3.0.0 - this root is served by the global v3 engine; '
                           'finish or restart this session on kit 3.0.0',
            },
            'nonce': '00000000-0000-0000-0000-000000000000',
            'pid_start_time': '0',
        }) + '\n').encode()
        marker_sha = '4a9c34859cf78db4a00dfcdbf152dc08650613784507b1b9eea6a9fc024632f0'
        replacement_dirs = {
            '.': 0o500,
            'seats': 0o500,
            'seats/fixture-seat': 0o500,
            'drafts': 0o700,
            'drafts/fixture-seat': 0o700,
            'drafts/fixture-seat/nested': 0o500,
        }

        def inventory(root):
            paths = [root, *root.rglob('*')]
            return {
                path.relative_to(root).as_posix(): (
                    stat.S_IMODE(path.stat().st_mode),
                    path.read_bytes() if path.is_file() else None,
                )
                for path in paths
            }

        def restore_owner_access(path):
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IRWXU)
            for entry in path.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    restore_owner_access(entry)

        roots = []
        for _ in range(2):
            root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            self.addCleanup(restore_owner_access, root)
            roots.append(root)
            engine = root / '.engine'
            for relative in replacement_dirs:
                path = engine / relative
                path.mkdir()
                path.chmod(0o750)
            for relative, content in legacy_files.items():
                path = engine / relative
                path.write_bytes(content)
                path.chmod(0o640)
            control = root / 'control.txt'
            control.write_bytes(b'unchanged sibling\n')
            control.chmod(0o640)

        subject, oracle = roots
        original = inventory(subject / '.engine')
        control = inventory(subject / 'control.txt')
        self.assertEqual(inventory(oracle / '.engine'), original)
        self.assertEqual(inventory(oracle / 'control.txt'), control)
        (oracle / '.engine').rename(oracle / '.engine.migrated-to-v3')
        for relative in replacement_dirs:
            (oracle / '.engine' / relative).mkdir()
        expected_files = {**copied_files, 'daemon.json': marker_bytes}
        for relative, content in expected_files.items():
            path = oracle / '.engine' / relative
            path.write_bytes(content)
            path.chmod(0o400)
        for relative, mode in reversed(replacement_dirs.items()):
            (oracle / '.engine' / relative).chmod(mode)
        expected = {relative: (mode, None) for relative, mode in replacement_dirs.items()}
        expected.update({relative: (0o400, content) for relative, content in expected_files.items()})
        self.assertEqual(inventory(oracle / '.engine'), expected)
        self.assertEqual(inventory(oracle / '.engine.migrated-to-v3'), original)
        self.assertEqual(hashlib.sha256(marker_bytes).hexdigest(), marker_sha)

        self.assertTrue(hasattr(store_steps, 'place_marker'), 'missing store_steps.place_marker')
        store_steps.place_marker(subject)
        self.assertEqual(inventory(subject / '.engine.migrated-to-v3'), original)
        self.assertEqual(inventory(subject / 'control.txt'), control)
        self.assertEqual(inventory(subject / '.engine'), expected)
        actual_marker = (subject / '.engine' / 'daemon.json').read_bytes()
        self.assertTrue(actual_marker.endswith(b'\n'))
        self.assertFalse(actual_marker.endswith(b'\n\n'))
        self.assertNotIn('pid', json.loads(actual_marker))
        self.assertEqual(hashlib.sha256(actual_marker).hexdigest(), marker_sha)

    def _marker_replay_observations(self):
        def inventory(root):
            rows = {}
            for path in [root, *sorted(root.rglob('*'))]:
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    kind, payload = 'file', path.read_bytes()
                elif stat.S_ISDIR(metadata.st_mode):
                    kind, payload = 'directory', None
                elif stat.S_ISLNK(metadata.st_mode):
                    kind, payload = 'symlink', str(path.readlink())
                else:
                    self.fail(f'unexpected fixture entry: {path}')
                rows[path.relative_to(root).as_posix()] = (
                    kind, stat.S_IMODE(metadata.st_mode), payload,
                )
            return rows

        def inode(path):
            metadata = path.lstat()
            return metadata.st_dev, metadata.st_ino

        marker_modes = {
            '.': 0o500, 'seats': 0o500, 'seats/fixture-seat': 0o500,
            'drafts': 0o700, 'drafts/fixture-seat': 0o700,
            'drafts/fixture-seat/nested': 0o500,
        }
        marker_digest = '4a9c34859cf78db4a00dfcdbf152dc08650613784507b1b9eea6a9fc024632f0'
        originals = self.job_path.resolve() / 'originals'
        legacy_inventories = {}
        preimage_inventories = {}
        sibling_inventories = {}
        copied_files = ('seats/fixture-seat/key', 'drafts/fixture-seat/nested/note.md')

        for index, (name, directory_mode, file_mode) in enumerate(
                (('A', 0o750, 0o640), ('B', 0o700, 0o600))):
            root = self.root.resolve() / name
            root.mkdir()
            engine = root / '.engine'
            for relative in marker_modes:
                directory = engine / relative
                directory.mkdir()
                directory.chmod(directory_mode)
            for relative in (*copied_files, 'daemon.json', 'daemon.log'):
                file = engine / relative
                file.write_bytes(f'{name}: {relative}\n'.encode())
                file.chmod(file_mode)
            sibling = root / 'control.txt'
            sibling.write_bytes(f'independent control {name}\n'.encode())
            sibling.chmod(file_mode)
            legacy_inventories[name] = inventory(engine)
            sibling_inventories[name] = inventory(sibling)
            preimage = originals / str(index)
            synced = []
            sync_dir = store_steps._sync_dir

            def observe_sync(path):
                sync_dir(path)
                synced.append((Path(path), preimage.is_dir()))

            with mock.patch.object(store_steps, '_sync_dir', side_effect=observe_sync):
                store_steps.capture_preimage(root, preimage)
            self.assertEqual(synced[-1], (originals, True))
            self.assertEqual(inventory(preimage / 'payload'), legacy_inventories[name])
            preimage_inventories[name] = inventory(preimage)

        def observe_effect(name):
            root = self.root.resolve() / name
            engine = root / '.engine'
            parked = root / '.engine.migrated-to-v3'
            original_inode = inode(engine)
            moves = []
            rename = Path.rename

            def observe_rename(source, destination):
                identity = inode(source)
                result = rename(source, destination)
                moves.append((source, Path(destination), identity, inode(destination),
                              engine.exists(), parked.exists()))
                return result

            with mock.patch.object(Path, 'rename', observe_rename):
                store_steps.place_marker(root)
            self.assertEqual(len(moves), 2)
            first, second = moves
            self.assertEqual(first, (engine, parked, original_inode, original_inode, False, True))
            self.assertEqual(second[0].parent, root)
            self.assertEqual(second[1:], (engine, inode(engine), inode(engine), True, True))
            self.assertEqual(inventory(parked), legacy_inventories[name])
            installed = inventory(engine)
            expected = {relative: ('directory', mode, None) for relative, mode in marker_modes.items()}
            for relative in copied_files:
                expected[relative] = ('file', 0o400, legacy_inventories[name][relative][2])
            marker = installed['daemon.json']
            self.assertEqual(hashlib.sha256(marker[2]).hexdigest(), marker_digest)
            expected['daemon.json'] = ('file', 0o400, marker[2])
            self.assertEqual(installed, expected)
            return installed

        return (inventory, inode, observe_effect, marker_modes, copied_files, marker_digest,
                legacy_inventories, preimage_inventories, sibling_inventories)

    def test_marker_replay_fixture_observations(self):
        (inventory, _, observe_effect, _, _, _, legacy_inventories,
         preimage_inventories, sibling_inventories) = self._marker_replay_observations()
        originals = self.job_path.resolve() / 'originals'
        installed_a = observe_effect('A')
        b = self.root.resolve() / 'B'
        self.assertEqual(inventory(b / '.engine'), legacy_inventories['B'])
        self.assertFalse((b / '.engine.migrated-to-v3').exists())
        self.journal.close()
        reopened = store_steps.open_journal(self.job_path)
        try:
            # The observation control isolates the filesystem from the journal encoding.
            self.assertEqual(reopened.pending(), [])
        finally:
            reopened.close()
        installed_b = observe_effect('B')

        foreign = b / '.marker-foreign'
        foreign.mkdir()
        (foreign / 'control').write_bytes(b'foreign stage\n')
        foreign_inventory = inventory(foreign)
        parked_b = b / '.engine.migrated-to-v3'
        (parked_b / 'daemon.log').write_bytes(b'independent drift\n')
        self.assertNotEqual(inventory(parked_b), legacy_inventories['B'])
        self.assertEqual(inventory(foreign), foreign_inventory)
        self.assertEqual(inventory(b / '.engine'), installed_b)
        self.assertEqual(inventory(self.root / 'A' / '.engine'), installed_a)
        for index, name in enumerate(('A', 'B')):
            self.assertEqual(inventory(originals / str(index)), preimage_inventories[name])
            self.assertEqual(inventory(self.root / name / 'control.txt'), sibling_inventories[name])

    def _forward_marker_fixture(self):
        import json

        inventory, inode, _, _, _, _, legacy, preimages, siblings = self._marker_replay_observations()
        roots = {name: self.root.resolve() / name for name in ('A', 'B')}
        job = self.job_path.resolve()
        targets = {}
        manifests = {}
        for index, (name, root_uuid) in enumerate((('A', P), ('B', Q))):
            targets[name] = store_steps.MarkerTarget('cccccccc-cccc-4ccc-8ccc-cccccccccccc', JOB,
                                                     job, index, root_uuid, roots[name])
            manifest = json.loads((job / 'originals' / str(index) / 'manifest.json').read_text())
            manifests[name] = store_steps.PreimageManifest(tuple(manifest['entries']))

        def snapshot():
            return {path: {key: (value, inode(path / key)) for key, value in inventory(path).items()}
                    for path in (*roots.values(), job / 'originals')}

        return roots, targets, manifests, snapshot, legacy, preimages, siblings

    def _marker_rows(self):
        from contextlib import closing

        path = self.job_path.resolve() / 'job.sqlite3'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            return (
                conn.execute('SELECT id,step,target,before_state,after_state FROM intents ORDER BY id').fetchall(),
                conn.execute('SELECT intent_id,observed FROM receipts ORDER BY intent_id').fetchall(),
            )

    def _mark(self, journal, targets, manifests, name):
        return store_steps.advance_marker(journal, targets[name], manifests[name])

    def _pending_forward(self, journal, target, preimage, step, *, effected=False):
        while True:
            intent = store_steps.write_marker_intent(journal, target, preimage)
            if intent.step == 'marker-' + step:
                if effected:
                    root = Path(target.root_path)
                    with store_steps._open_marker_directory(root) as fd:
                        store_steps._marker_effect(root, fd, intent)
                return intent
            store_steps.resume_marker(journal, intent, target)

    def _pending_place(self, journal, target, preimage, *, placed=False):
        return self._pending_forward(journal, target, preimage, 'place', effected=placed)

    def _adoption(self):
        snapshot_source = store_steps.snapshot_source
        roots, targets, manifests, snapshot, legacy, _, _ = self._forward_marker_fixture()
        unmark = self.root / 'unmark'
        unmark.mkdir()
        return snapshot_source, roots, targets, manifests, snapshot, legacy, unmark

    @staticmethod
    def _journal_files(folder):
        return {p.relative_to(folder): (p.lstat().st_mode, p.read_bytes())
                for p in folder.rglob('*') if p.is_file()}

    def test_snapshot_source_copies_committed_wal_rows(self):
        snapshot_source, _, targets, manifests, _, _, unmark = self._adoption()
        self.journal.close()
        target = targets['A']
        values = (target.store_id, target.job_id, str(target.job_dir),
                  target.root_index, target.root_uuid, str(target.root_path))
        script = """import os, sys, ast
from pathlib import Path
from relay_engine import store_steps as s
target = s.MarkerTarget(*ast.literal_eval(sys.argv[1]))
j = s.open_journal(Path(target.job_dir))
j._conn.execute('PRAGMA wal_autocheckpoint=0').close()
p = s.PreimageManifest(tuple(ast.literal_eval(sys.argv[2])))
s.advance_marker(j, target, p)
os._exit(0)
"""
        subprocess.run([sys.executable, '-c', script, repr(values), repr(manifests['A'].entries)],
                       stdin=subprocess.DEVNULL, check=True)
        wal = self.job_path / 'job.sqlite3-wal'
        self.assertGreater(wal.stat().st_size, 0)
        (self.job_path / 'job.sqlite3-shm').unlink()
        before = self._journal_files(self.job_path)
        captured = snapshot_source(self.job_path, unmark)
        source = store_steps.open_source(captured, unmark)
        journal = store_steps.open_journal(unmark, create=True)
        try:
            self.assertEqual(store_steps.adopted_progress(source, journal, target).phase, 'marked')
        finally:
            journal.close()
            store_steps.close_source(source)
        self.assertEqual(self._journal_files(self.job_path), before)

    def test_snapshot_source_custody(self):
        snapshot_source, _, _, _, _, _, unmark = self._adoption()
        import os
        self.journal.checkpoint('wal-witness', True)
        wal = (self.job_path / 'job.sqlite3-wal').resolve()
        copy = shutil.copyfile

        def stop_before_wal(src, dst, *args, **kwargs):
            if Path(src) == wal:
                raise RuntimeError('between input members')
            return copy(src, dst, *args, **kwargs)

        with mock.patch.object(shutil, 'copyfile', side_effect=stop_before_wal):
            with self.assertRaisesRegex(RuntimeError, 'between input members'):
                snapshot_source(self.job_path, unmark)
        self.assertTrue((unmark / 'source.partial').exists())
        self.assertFalse((unmark / 'source-inputs').exists())
        db = (self.job_path / 'job.sqlite3').resolve()

        def drift(src, dst, *args, **kwargs):
            out = copy(src, dst, *args, **kwargs)
            if Path(src) == db:
                current = db.stat()
                os.utime(db, ns=(current.st_atime_ns, current.st_mtime_ns + 10000000))
            return out

        with mock.patch.object(shutil, 'copyfile', side_effect=drift):
            with self.assertRaises(store_steps.StepStateUnknown):
                snapshot_source(self.job_path, unmark)
        self.assertFalse((unmark / 'source-inputs').exists())
        captured = snapshot_source(self.job_path, unmark)
        published = unmark / 'source-inputs' / db.name
        self.assertEqual(stat.S_IMODE(published.stat().st_mode), 0o400)
        self.assertFalse((unmark / 'source.partial').exists())
        with self.assertRaises(store_steps.StepStateUnknown):
            snapshot_source(self.job_path, self.job_path / 'not-a-separate-job')
        published.chmod(0o600)
        original = published.read_bytes()
        # Byte 63 is the low byte of SQLite's application-controlled user_version.
        # The copy is still a database; only the published-input pin should catch it.
        published.write_bytes(original[:63] + bytes([original[63] ^ 1]) + original[64:])
        published.chmod(0o400)
        with self.assertRaises(store_steps.StepStateUnknown):
            store_steps.open_source(captured, unmark)
        published.chmod(0o600)
        published.write_bytes(original)
        published.chmod(0o400)
        self.journal.checkpoint('drift', False)
        with self.assertRaises(store_steps.StepStateUnknown):
            store_steps.open_source(captured, unmark)

    def _adopt_marked(self, names=('A', 'B')):
        snapshot_source, roots, targets, manifests, snapshot, legacy, unmark = self._adoption()
        for name in names:
            self._mark(self.journal, targets, manifests, name)
        captured = snapshot_source(self.job_path, unmark)
        source = store_steps.open_source(captured, unmark)
        self.addCleanup(store_steps.close_source, source)
        journal = store_steps.open_journal(unmark, create=True)
        self.addCleanup(journal.close)
        return roots, targets, snapshot, legacy, unmark, source, journal

    def test_adopted_inverse_leaves_source_unchanged(self):
        roots, targets, snapshot, _, unmark, source, journal = self._adopt_marked()
        before = self._journal_files(self.job_path)
        published = self._journal_files(unmark / 'source-inputs')
        for target in targets.values():
            store_steps.advance_adopted_inverse(source, journal, target)
            self.assertEqual(store_steps.adopted_root_check(source, journal, target), 'preimage')
        self.assertEqual(self._journal_files(self.job_path), before)
        self.assertEqual(self._journal_files(unmark / 'source-inputs'), published)
        self.assertEqual(journal.pending(), [])
        count = store_layout._row(journal._conn, 'SELECT count(*) FROM receipts')[0]
        self.assertEqual(count, len(roots) * 4)
        store_steps.close_source(source)
        self.assertFalse(list(unmark.glob('source-work-*')))

    def test_adopted_inverse_crash_points(self):
        from contextlib import closing
        order = ('inverse-unplace', 'inverse-restore', 'dispose-tomb', 'dispose-clear')
        effect, receipt = store_steps._marker_effect, store_steps.Journal.receipt
        for step in order:
            for moment in ('intent', 'effect', 'receipt'):
                with self.subTest(step=step, moment=moment):
                    root = self.root / f'{step}-{moment}'
                    root.mkdir()
                    source_dir = store_steps.migration_dir(root, JOB)
                    with (closing(store_steps.open_journal(source_dir, create=True)) as origin,
                          mock.patch.object(self, 'root', root),
                          mock.patch.object(self, 'job_path', source_dir),
                          mock.patch.object(self, 'journal', origin)):
                        roots, targets, snapshot, _, _, source, journal = self._adopt_marked(('A',))
                    target = targets['A']

                    def cut_effect(root, fd, intent):
                        if intent.step == step:
                            if moment == 'effect':
                                effect(root, fd, intent)
                            raise RuntimeError(step)
                        return effect(root, fd, intent)

                    def cut_receipt(j, ident, observed):
                        receipt(j, ident, observed)
                        row = store_layout._row(j._conn, 'SELECT step FROM intents WHERE id=?', (ident,))
                        if j is journal and row == (step,):
                            raise RuntimeError(step)

                    patch = (mock.patch.object(store_steps.Journal, 'receipt', cut_receipt)
                             if moment == 'receipt' else
                             mock.patch.object(store_steps, '_marker_effect', side_effect=cut_effect))
                    with patch, self.assertRaisesRegex(RuntimeError, step):
                        store_steps.advance_adopted_inverse(source, journal, target)
                    self.assertEqual(store_steps.adopted_root_check(source, journal, target),
                                     'preimage' if step == 'dispose-clear' and moment == 'receipt'
                                     else 'at-step')

                    if moment == 'effect':
                        after = snapshot()[roots['A']]
                        with (mock.patch.object(store_steps.Journal, 'receipt', cut_receipt),
                              mock.patch.object(store_steps, '_marker_effect', wraps=effect) as moved,
                              self.assertRaisesRegex(RuntimeError, step)):
                            store_steps.advance_adopted_inverse(source, journal, target)
                        moved.assert_not_called()
                        self.assertEqual(snapshot()[roots['A']], after)
                    store_steps.advance_adopted_inverse(source, journal, target)
                    self.assertEqual(store_steps.adopted_root_check(source, journal, target), 'preimage')
                    self.assertFalse(journal.pending())
                    count = store_layout._row(journal._conn, 'SELECT count(*) FROM receipts')[0]
                    self.assertEqual(count, len(order))
                    store_steps.close_source(source)

    def test_adopted_root_check_answers(self):
        roots, targets, _, _, _, source, journal = self._adopt_marked()
        a = targets['A']
        self.assertEqual(store_steps.adopted_root_check(source, journal, a), 'marked')
        progress = store_steps.marker_progress(self.journal, a)
        self.assertEqual(progress.phase, 'marked')
        engine = roots['A'] / '.engine'
        old = roots['A'] / 'old-marker'
        engine.rename(old)
        shutil.copytree(old, engine)
        self.assertEqual(store_steps.adopted_root_check(source, journal, a), 'other')
        engine.rename(roots['A'] / 'copied-marker')
        old.rename(engine)
        history = store_steps._read_marker_history(self.journal, a)
        sibling = roots['A'] / store_steps._marker_slots(
            store_steps._marker_binding(a), history.progress.attempt, history.nonce)['sibling']
        sibling.mkdir()
        self.assertEqual(store_steps.adopted_root_check(source, journal, a), 'other')
        sibling.rmdir()
        store_steps.advance_adopted_inverse(source, journal, a)
        self.assertEqual(store_steps.adopted_root_check(source, journal, a), 'preimage')
        (engine / 'daemon.log').write_bytes(b'drift')
        self.assertEqual(store_steps.adopted_root_check(source, journal, a), 'other')

    def test_adopted_inverse_source_shapes(self):
        snapshot_source, _, targets, manifests, snapshot, _, unmark = self._adoption()
        self._mark(self.journal, targets, manifests, 'A')
        self._pending_forward(self.journal, targets['B'], manifests['B'], 'aside')
        inverse = store_steps.write_inverse_intent(self.journal, targets['A'], None)
        store_steps.resume_inverse(self.journal, inverse, targets['A'])
        captured = snapshot_source(self.job_path, unmark)
        source = store_steps.open_source(captured, unmark)
        self.addCleanup(store_steps.close_source, source)
        journal = store_steps.open_journal(unmark, create=True)
        self.addCleanup(journal.close)
        before = snapshot()
        for target in targets.values():
            with self.assertRaises(store_steps.StepStateUnknown):
                store_steps.advance_adopted_inverse(source, journal, target)
        self.assertEqual(journal.pending(), [])
        self.assertEqual(snapshot(), before)

    def test_source_marker_phase(self):
        snapshot_source, _, targets, manifests, _, _, unmark = self._adoption()
        self._mark(self.journal, targets, manifests, 'A')
        for pending in (False, True):
            if pending:
                self._pending_forward(self.journal, targets['B'], manifests['B'], 'aside')
                store_steps.write_inverse_intent(self.journal, targets['A'], None)
            captured = snapshot_source(self.job_path, unmark)
            source = store_steps.open_source(captured, unmark)
            try:
                self.assertEqual(store_steps.source_marker_phase(source, targets['A']),
                                 'unfinished' if pending else 'marked')
                self.assertEqual(store_steps.source_marker_phase(source, targets['B']),
                                 'unfinished' if pending else 'none')
            finally:
                store_steps.close_source(source)

    def test_adopted_rows_bind_source_target(self):
        from dataclasses import replace
        _, targets, _, _, _, source, journal = self._adopt_marked()
        a = targets['A']
        intent = store_steps.write_inverse_intent(self.journal, a, None)
        journal.intent(intent.step, {**intent.target, 'job_id': Q}, intent.before, intent.after)
        with self.assertRaises(store_steps.StepStateUnknown):
            store_steps.adopted_progress(source, journal, a)
        with self.assertRaises(store_steps.StepStateUnknown):
            store_steps.source_marker_phase(source, replace(a, root_uuid=Q))

    def test_resume_marker(self):
        from contextlib import closing

        roots, targets, manifests, snapshot, _, _, _ = self._forward_marker_fixture()
        self.assertEqual(self._mark(self.journal, targets, manifests, 'A'), 'marker')
        before = snapshot()
        pending = self._pending_place(self.journal, targets['B'], manifests['B'])
        self.journal.close()
        with closing(store_steps.open_journal(self.job_path)) as journal:
            self.assertEqual(journal.pending(), [pending])
            root = roots['B']
            self.assertFalse((root / '.engine').exists())
            old_inode = (root / '.engine.migrated-to-v3').stat().st_ino
            self.assertEqual(store_steps.resume_marker(journal, pending, targets['B']), 'marker')
            self.assertEqual(journal.pending(), [])
            self.assertEqual(store_steps.marker_progress(journal, targets['B']).phase, 'marked')
            self.assertEqual((root / '.engine.migrated-to-v3').stat().st_ino, old_inode)
            self.assertEqual((root / '.engine/daemon.json').read_bytes(), store_steps.MARKER_DAEMON_JSON)
        after = snapshot()
        self.assertEqual(after[roots['A']], before[roots['A']])
        originals = self.job_path.resolve() / 'originals'
        self.assertEqual(after[originals], before[originals])
        self.assertEqual(after[roots['B']]['control.txt'], before[roots['B']]['control.txt'])

    def _guard_marker_mutations(self, stack, journal):
        import os

        for owner, name in (
                (store_steps, 'place_marker'), (os, 'rename'), (os, 'mkdir'), (os, 'unlink'),
                (os, 'rmdir'), (os, 'fchmod'), (store_steps, 'capture_preimage'),
                (shutil, 'rmtree'), (os, 'replace'), (journal, 'receipt'), (Path, 'unlink'), (Path, 'rmdir')):
            stack.enter_context(mock.patch.object(
                owner, name, side_effect=AssertionError(f'unexpected marker mutation: {name}')))

    def test_write_marker_intent_columns_and_duplicates(self):
        import json

        roots, targets, manifests, snapshot, _, _, _ = self._forward_marker_fixture()
        journal = self.journal
        before_tree = snapshot()
        nonce = 'f' * 32
        with mock.patch.object(store_steps.secrets, 'token_hex', return_value=nonce):
            intent = store_steps.write_marker_intent(journal, targets['B'], manifests['B'])
        self.assertEqual(snapshot(), before_tree)
        root = roots['B']
        job = self.job_path.resolve()
        preimage = job / 'originals/1/manifest.json'
        identity = lambda p: dict(dev=p.stat().st_dev, ino=p.stat().st_ino)
        suffix = f'{JOB}-1-1-{nonce}'
        encoded = dict(store_id=targets['B'].store_id, job_id=JOB, job_dir=str(job),
                       root_index=1, root_uuid=Q, root_path=str(root), version=2,
                       preimage_path=str(preimage.parent),
                       preimage_manifest_sha256=hashlib.sha256(preimage.read_bytes()).hexdigest(),
                       root_identity=identity(root), original_identity=identity(root / '.engine'),
                       attempt=1, nonce=nonce, build_identity=None, dispose_slot=None,
                       slots={'engine': '.engine', 'parked': '.engine.migrated-to-v3',
                              'build': f'.marker-build-{suffix}', 'stage': f'.marker-stage-{JOB}-1',
                              'sibling': f'.marker-inverse-{JOB}-1',
                              'discard': f'.marker-discard-{suffix}',
                              'inverse_discard': f'.marker-discard-{JOB}-1-inverse'})
        before = dict.fromkeys(encoded['slots'])
        before['engine'] = {'kind': 'preimage', 'identity': identity(root / '.engine')}
        after = {**before, 'build': {'kind': 'new-empty', 'identity': None}}
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
        self.assertEqual(intent, store_steps.Intent(1, 'marker-open', encoded, before, after))
        self.assertEqual(self._marker_rows(),
                         ([(1, 'marker-open', canonical(encoded), canonical(before), canonical(after))], []))
        self.assertEqual(journal.pending(), [intent])
        with self.assertRaisesRegex(ValueError, 'already pending'):
            store_steps.write_marker_intent(journal, targets['B'], manifests['B'])
        store_steps.resume_marker(journal, intent, targets['B'])
        open_identity = identity(root / encoded['slots']['build'])
        self.assertEqual(self._marker_rows()[1], [(1, canonical({'build_identity': open_identity}))])

        marker = {'kind': 'marker', 'identity': open_identity}
        empty = {'kind': 'empty', 'identity': open_identity}
        filled = {**before, 'build': marker}
        sealed = {**before, 'stage': marker}
        aside = {**sealed, 'engine': None, 'parked': before['engine']}
        placed = {**aside, 'stage': None, 'engine': marker}
        for number, (name, incoming, outgoing) in enumerate((
                ('fill', {**before, 'build': empty}, filled),
                ('seal', filled, sealed),
                ('aside', sealed, aside),
                ('place', aside, placed),
        ), 2):
            step = store_steps.write_marker_intent(journal, targets['B'], None)
            target = {**encoded, 'build_identity': open_identity}
            self.assertEqual(step, store_steps.Intent(number, 'marker-' + name, target, incoming, outgoing))
            self.assertEqual(self._marker_rows()[0][-1],
                             (number, 'marker-' + name, canonical(target), canonical(incoming), canonical(outgoing)))
            self.assertEqual(journal.pending(), [step])
            with self.assertRaisesRegex(ValueError, 'already pending'):
                store_steps.write_marker_intent(journal, targets['B'], None)
            store_steps.resume_marker(journal, step, targets['B'])
            self.assertEqual(self._marker_rows()[1][-1], (number, canonical(outgoing)))
        self.assertEqual(journal.pending(), [])
        self.assertEqual(snapshot()[roots['A']], before_tree[roots['A']])
        with self.assertRaisesRegex(ValueError, 'out of order'):
            store_steps.write_marker_intent(journal, targets['B'], None)

    def test_resume_marker_receipt_only(self):
        from contextlib import closing, ExitStack

        for step in ('open', 'fill', 'seal', 'aside', 'place'):
            with self.subTest(step=step), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                area = Path(scratch).resolve()
                job = store_steps.migration_dir(area, JOB)
                with mock.patch.multiple(self, root=area, job_path=job):
                    with closing(store_steps.open_journal(job, create=True)) as initial:
                        roots, targets, manifests, snapshot, _, _, _ = self._forward_marker_fixture()
                        self._mark(initial, targets, manifests, 'A')
                        pending = self._pending_forward(initial, targets['B'], manifests['B'], step, effected=True)
                    foreign = roots['B'] / '.marker-foreign'
                    foreign.mkdir()
                    (foreign / 'control').write_bytes(b'foreign stage stays in place\n')
                    prior = snapshot()
                    before_rows = self._marker_rows()
                    with closing(store_steps.open_journal(self.job_path)) as journal:
                        receipt, sync, observe = journal.receipt, store_steps._sync_dir, store_steps._marker_check
                        events = []

                        def check(*args):
                            result = observe(*args)
                            self.assertTrue(result[0])
                            events.append('observe')
                            return result

                        def sync_parent(path):
                            self.assertEqual(path, roots['B'])
                            self.assertEqual(self._marker_rows(), before_rows)
                            sync(path)
                            events.append('sync')

                        def receipt_after_sync(ident, state):
                            self.assertEqual(events, ['observe', 'sync', 'observe'])
                            expected = ({'build_identity': store_steps._marker_identity(
                                            (roots['B'] / pending.target['slots']['build']).stat())}
                                        if step == 'open' else pending.after)
                            self.assertEqual((ident, state), (pending.id, expected))
                            receipt(ident, state)
                            events.append('receipt')

                        with ExitStack() as stack:
                            stack.enter_context(mock.patch.object(store_steps, '_sync_marker_tree'))
                            self._guard_marker_mutations(stack, journal)
                            stack.enter_context(mock.patch.object(journal, 'receipt', receipt_after_sync))
                            stack.enter_context(mock.patch.object(store_steps, '_marker_check', check))
                            stack.enter_context(mock.patch.object(store_steps, '_sync_dir', sync_parent))
                            self.assertEqual(store_steps.resume_marker(journal, pending, targets['B']), 'receipt')
                        self.assertEqual(events, ['observe', 'sync', 'observe', 'receipt'])
                        self.assertEqual(journal.pending(), [])
                        self.assertEqual(snapshot(), prior)
                        self.assertEqual(self._marker_rows()[0], before_rows[0])

    def test_resume_marker_refuses_unproven_state(self):
        from contextlib import closing, ExitStack
        from dataclasses import replace

        cases = ('wrong-row', 'wrong-job', 'wrong-root', 'foreign-stage', 'parked-only',
                 'marker-drift', 'original-drift', 'preimage-payload-drift', 'preimage-manifest-drift',
                 'unreadable-stage')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                area = Path(scratch).resolve()
                job = store_steps.migration_dir(area, JOB)
                with mock.patch.multiple(self, root=area, job_path=job):
                    with closing(store_steps.open_journal(job, create=True)) as journal:
                        roots, targets, manifests, snapshot, _, _, _ = self._forward_marker_fixture()
                        self._mark(journal, targets, manifests, 'A')
                        bound = targets['B']
                        pending = self._pending_place(journal, bound, manifests['B'],
                                                      placed=case in ('marker-drift', 'original-drift'))
                        intent = pending
                        root = roots['B']
                        engine = root / '.engine'
                        parked = root / '.engine.migrated-to-v3'
                        stage = root / intent.target['slots']['stage']
                        if case == 'wrong-row':
                            intent = replace(intent, before={})
                        elif case == 'wrong-job':
                            bound = replace(bound, job_id='eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee')
                        elif case == 'wrong-root':
                            bound = replace(bound, root_path=roots['A'])
                        elif case == 'foreign-stage':
                            stage.rename(root / '.marker-unrelated')
                        elif case == 'parked-only':
                            stage.rename(root / 'outside-stage-slot')
                            stage = None
                        elif case == 'marker-drift':
                            marker = engine / 'daemon.json'
                            marker.chmod(0o600)
                            marker.write_bytes(b'changed marker\n')
                            marker.chmod(0o400)
                        elif case == 'original-drift':
                            (parked / 'daemon.log').write_bytes(b'changed original\n')
                        elif case == 'preimage-payload-drift':
                            (job / 'originals/1/payload/daemon.log').write_bytes(b'changed payload\n')
                        elif case == 'preimage-manifest-drift':
                            manifest = job / 'originals/1/manifest.json'
                            manifest.write_bytes(manifest.read_bytes() + b' ')
                        before, rows = snapshot(), self._marker_rows()
                    with closing(store_steps.open_journal(job)) as journal, ExitStack() as stack:
                        self._guard_marker_mutations(stack, journal)
                        stack.enter_context(mock.patch.object(
                            store_steps, '_sync_dir', side_effect=AssertionError('unexpected sync')))
                        if case == 'unreadable-stage':
                            observe = store_steps._marker_tree

                            def tree(fd, name):
                                if name == pending.target['slots']['stage']:
                                    raise PermissionError('simulated unreadable stage')
                                return observe(fd, name)

                            stack.enter_context(mock.patch.object(store_steps, '_marker_tree', tree))
                        with self.assertRaises(store_steps.StepStateUnknown) as failure:
                            store_steps.resume_marker(journal, intent, bound)
                        error = failure.exception
                        if case in ('wrong-row', 'unreadable-stage', 'marker-drift', 'original-drift',
                                    'foreign-stage', 'parked-only'):
                            self.assertEqual((error.before, error.after), (intent.before, intent.after))
                        if case == 'unreadable-stage':
                            self.assertIn('simulated unreadable stage',
                                          error.observed['before']['stage']['observation_error'])
                        elif case in ('foreign-stage', 'parked-only'):
                            self.assertIsNone(error.observed['before']['stage'])
                        elif case in ('marker-drift', 'original-drift'):
                            self.assertIn('inventory', error.observed['after'][
                                'engine' if case == 'marker-drift' else 'parked'])
                        elif case in ('preimage-payload-drift', 'preimage-manifest-drift', 'wrong-job'):
                            self.assertIn('binding_error', error.observed)
                        else:
                            self.assertIsNotNone(error.observed)
                        self.assertEqual(journal.pending(), [pending])
                    self.assertEqual(snapshot(), before)
                    self.assertEqual(self._marker_rows(), rows)

    def test_restore_preimage_from_complete_marked_root(self):
        import json
        from contextlib import closing

        roots, targets, manifests, snapshot, _, _, _ = self._forward_marker_fixture()
        original = snapshot()
        for name in ('A', 'B'):
            self._mark(self.journal, targets, manifests, name)
        self.journal.close()
        root = roots['B']
        (root / 'control.txt').write_bytes(b'independent B inverse sibling\n')
        marked = snapshot()
        forward_intents, forward_receipts = self._marker_rows()
        with closing(store_steps.open_journal(self.job_path)) as journal:
            self.assertEqual(journal.pending(), [])
            intent = store_steps.write_inverse_intent(journal, targets['B'], manifests['B'])
            self.assertEqual(intent.step, 'inverse-unplace')
            store_steps.resume_inverse(journal, intent, targets['B'])
            self.assertEqual(store_steps.advance_inverse(journal, targets['B']), 'marker')
            self.assertEqual(journal.pending(), [])
        restored = snapshot()
        expected = {p: value[0] for p, value in original[root].items() if Path(p).is_relative_to('.engine')}
        actual = {p: value[0] for p, value in restored[root].items() if Path(p).is_relative_to('.engine')}
        self.assertEqual(actual, expected)
        self.assertFalse((root / '.engine.migrated-to-v3').exists())
        outside = {p: value for p, value in marked[root].items()
                   if not Path(p).is_relative_to('.engine') and not Path(p).is_relative_to(
                       '.engine.migrated-to-v3')}
        self.assertEqual({p: value for p, value in restored[root].items()
                          if not Path(p).is_relative_to('.engine')}, outside)
        originals = self.job_path.resolve() / 'originals'
        self.assertEqual(restored[originals], marked[originals])
        self.assertEqual(restored[roots['A']], marked[roots['A']])
        intents, receipts = self._marker_rows()
        self.assertEqual(intents[:len(forward_intents)], forward_intents)
        self.assertEqual(receipts[:len(forward_receipts)], forward_receipts)
        suffix = intents[len(forward_intents):]
        placement = forward_intents[-1]
        target, placed = json.loads(placement[2]), json.loads(placement[4])
        marker, original = placed['engine'], placed['parked']
        unplaced = {**placed, 'engine': None, 'sibling': marker}
        restored = {**unplaced, 'engine': original, 'parked': None}
        tomb = {**restored, 'sibling': None, 'inverse_discard': marker}
        clearing = {**tomb, 'inverse_discard': {**marker, 'kind': 'marker-subset'}}
        cleared = {**tomb, 'inverse_discard': None}
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
        disposing = {**target, 'dispose_slot': 'sibling'}
        expected = [
            (placement[0] + i, label, canonical(encoded), canonical(before), canonical(after))
            for i, (label, encoded, before, after) in enumerate((
                ('inverse-unplace', target, placed, unplaced),
                ('inverse-restore', target, unplaced, restored),
                ('dispose-tomb', disposing, restored, tomb),
                ('dispose-clear', disposing, clearing, cleared),
            ), 1)
        ]
        self.assertEqual(suffix, expected)
        self.assertEqual(receipts[len(forward_receipts):], [(row[0], row[4]) for row in expected])

    def test_resume_marker_sync_failure_keeps_intent(self):
        from contextlib import closing, ExitStack

        for step in ('open', 'fill', 'seal', 'aside', 'place'):
            with self.subTest(step=step), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                area = Path(scratch).resolve()
                job = store_steps.migration_dir(area, JOB)
                with mock.patch.multiple(self, root=area, job_path=job):
                    with closing(store_steps.open_journal(job, create=True)) as initial:
                        roots, targets, manifests, snapshot, _, _, _ = self._forward_marker_fixture()
                        self._mark(initial, targets, manifests, 'A')
                        pending = self._pending_forward(initial, targets['B'], manifests['B'], step, effected=True)
                    intact, rows = snapshot(), self._marker_rows()
                    injected = OSError('injected common-parent sync failure')
                    with closing(store_steps.open_journal(self.job_path)) as journal, ExitStack() as stack:
                        stack.enter_context(mock.patch.object(store_steps, '_sync_marker_tree'))
                        self._guard_marker_mutations(stack, journal)
                        sync = stack.enter_context(mock.patch.object(store_steps, '_sync_dir', side_effect=injected))
                        with self.assertRaises(OSError) as failure:
                            store_steps.resume_marker(journal, pending, targets['B'])
                        self.assertIs(failure.exception, injected)
                        sync.assert_called_once_with(roots['B'])
                        self.assertEqual(journal.pending(), [pending])
                    self.assertEqual(snapshot(), intact)
                    self.assertEqual(self._marker_rows(), rows)
                    with closing(store_steps.open_journal(self.job_path)) as journal:
                        self.assertEqual(journal.pending(), [pending])

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


def native_cut(operation, step, point, *, attempt=None, dispose_slot=None):
    """End an actual child at a committed intent/receipt or a named effect."""
    import os
    from contextlib import ExitStack

    pid = os.fork()
    if pid == 0:
        try:
            active = None

            def selected():
                return (active is not None and active.step == step
                        and (attempt is None or active.target.get('attempt') == attempt)
                        and (dispose_slot is None or active.target.get('dispose_slot') == dispose_slot))

            def hit(here):
                if selected() and here == point:
                    os._exit(77)

            intent_call = store_steps.Journal.intent
            receipt_call = store_steps.Journal.receipt

            def intent(journal, label, target, before, after):
                nonlocal active
                ident = intent_call(journal, label, target, before, after)
                active = store_steps.Intent(ident, label, target, before, after)
                hit('intent')
                return ident

            def receipt(journal, ident, observed):
                nonlocal active
                result = receipt_call(journal, ident, observed)
                if active is not None and active.id == ident:
                    hit('receipt')
                    active = None
                return result

            def monitor(name, call):
                def wrapped(*args, **kwargs):
                    result = call(*args, **kwargs)
                    hit(name)
                    if (name == 'rmdir' and selected() and active.target.get('slots') and
                            args[0] in (active.target['slots']['discard'],
                                        active.target['slots']['inverse_discard'])):
                        hit('discard-removed')
                    return result
                return wrapped

            def catalogue_commit(call):
                def wrapped(conn, sql):
                    result = call(conn, sql)
                    if sql == 'COMMIT' and any(
                            db == 'main' and Path(path).name == 'machine.sqlite3'
                            for _, db, path in conn.execute('PRAGMA database_list')):
                        hit('catalogue')
                    return result
                return wrapped

            original_open = store_steps.open_journal

            def open_journal(*args, **kwargs):
                nonlocal active
                journal = original_open(*args, **kwargs)
                candidates = journal.pending()
                if candidates:
                    active = candidates[-1]
                return journal

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(store_steps, 'open_journal', open_journal))
                stack.enter_context(mock.patch.object(store_steps.Journal, 'intent', intent))
                stack.enter_context(mock.patch.object(store_steps.Journal, 'receipt', receipt))
                stack.enter_context(mock.patch.object(store_layout, '_pragma', catalogue_commit(store_layout._pragma)))
                for name in ('mkdir', 'rename', 'chmod', 'fchmod', 'unlink', 'rmdir'):
                    stack.enter_context(mock.patch.object(os, name, monitor('chmod' if name == 'fchmod' else name, getattr(os, name))))
                stack.enter_context(mock.patch.object(store_steps, '_sync_file',
                                                      monitor('file-sync', store_steps._sync_file)))
                operation()
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(78)
        os._exit(79)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 77:
        raise AssertionError(f'native cut not reached: {step}/{point}, wait status {status}')


class TestNativeMarkerCuts(unittest.TestCase):
    _marker_replay_observations = TestDurableSteps._marker_replay_observations

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def fixture(self, label):
        from contextlib import closing

        parent, self.root = self.root, self.root / label
        self.root.mkdir()
        self.job_path = store_steps.migration_dir(self.root, JOB)
        with closing(store_steps.open_journal(self.job_path, create=True)):
            pass
        inventory, _, _, _, _, _, originals, _, _ = self._marker_replay_observations()
        roots = self.root / 'A', self.root / 'B'
        self.root = parent
        target = store_steps.MarkerTarget('cccccccc-cccc-4ccc-8ccc-cccccccccccc', JOB,
                                          self.job_path, 0, P, roots[0])
        data, _ = store_steps._marker_preimage(self.job_path / 'originals' / '0')
        preimage = store_steps.PreimageManifest(tuple(data))
        return roots, target, preimage, inventory, originals

    def _journal(self, target):
        from contextlib import closing
        return closing(store_steps.open_journal(Path(target.job_dir)))

    def forward(self, target, preimage):
        with self._journal(target) as journal:
            advance = getattr(store_steps, 'advance_marker', None)
            if advance is None:
                pending = journal.pending()
                intent = pending[0] if pending else store_steps.write_marker_intent(journal, target, preimage)
                return store_steps.resume_marker(journal, intent, target)
            return advance(journal, target, preimage)

    def inverse(self, target):
        with self._journal(target) as journal:
            return store_steps.advance_inverse(journal, target)

    def _verify(self, fixture, *, inverse=False):
        roots, target, _, inventory, originals = fixture
        root, other = roots
        self.assertEqual(inventory(other / '.engine'), originals['B'])
        self.assertEqual((root / 'control.txt').read_bytes(), b'independent control A\n')
        self.assertEqual(inventory(Path(target.job_dir) / 'originals' / '0' / 'payload'), originals['A'])
        with self._journal(target) as journal:
            self.assertEqual(journal.pending(), [])
        if inverse:
            self.assertEqual(inventory(root / '.engine'), originals['A'])
            self.assertFalse((root / '.engine.migrated-to-v3').exists())
        else:
            self.assertEqual(inventory(root / '.engine.migrated-to-v3'), originals['A'])
            installed = inventory(root / '.engine') if (root / '.engine').is_dir() else {}
            expected_modes = {'.': 0o500, 'seats': 0o500, 'seats/fixture-seat': 0o500,
                              'drafts': 0o700, 'drafts/fixture-seat': 0o700,
                              'drafts/fixture-seat/nested': 0o500}
            expected = {key: ('directory', mode, None) for key, mode in expected_modes.items()}
            for name in ('seats/fixture-seat/key', 'drafts/fixture-seat/nested/note.md'):
                expected[name] = ('file', 0o400, originals['A'][name][2])
            expected['daemon.json'] = ('file', 0o400, store_steps.MARKER_DAEMON_JSON)
            self.assertEqual(installed, expected)
        self.assertFalse(any(p.name.startswith(('.marker-build-', '.marker-stage-', '.marker-inverse-',
                                                '.marker-discard-')) for p in root.iterdir()
                             if p.name not in getattr(self, 'foreign', ())))

    def _foreign_control(self, fixture):
        from contextlib import contextmanager
        import os

        (root, _), _, _, inventory, _ = fixture
        unrelated = root / '.marker-unrelated'
        unrelated.mkdir()
        (unrelated / 'daemon.json').write_bytes(store_steps.MARKER_DAEMON_JSON)
        before = inventory(unrelated)
        os_stat = os.stat

        def reject_foreign(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path).name == unrelated.name:
                self.fail('marker recovery inspected an unrelated sibling')
            return os_stat(path, *args, **kwargs)

        @contextmanager
        def control():
            with mock.patch.object(os, 'stat', reject_foreign):
                yield
            self.assertEqual(inventory(unrelated), before)

        return control()

    def test_marker_cut_at_each_point(self):
        for name, effect in (('open', 'mkdir'), ('fill', 'file-sync'), ('seal', 'rename'),
                             ('aside', 'rename'), ('place', 'rename')):
            for point in ('intent', effect, 'receipt'):
                with self.subTest(step=name, point=point):
                    case = self.fixture(name + point)
                    _, target, preimage, *_ = case
                    with self._foreign_control(case):
                        native_cut(lambda: self.forward(target, preimage), 'marker-' + name, point)
                        self.forward(target, None)
                    self._verify(case)

    def test_inverse_cut_at_each_point(self):
        for name, effects in (('inverse-unplace', ('rename',)), ('inverse-restore', ('rename',)),
                              ('dispose-tomb', ('rename',)), ('dispose-clear', ('chmod', 'unlink', 'discard-removed'))):
            for point in ('intent', *effects, 'receipt'):
                with self.subTest(step=name, point=point):
                    case = self.fixture(name + point)
                    _, target, preimage, *_ = case
                    with self._foreign_control(case):
                        self.forward(target, preimage)
                        native_cut(lambda: self.inverse(target), name, point)
                        self.inverse(target)
                    self._verify(case, inverse=True)

    def test_build_name_binds_ownership(self):
        case = self.fixture('build-owner')
        roots, target, preimage, inventory, _ = case
        stem = f'.marker-build-{JOB}-0-1'
        foreign = [roots[0] / (stem + '-' + 'a' * 32), roots[0] / stem]
        self.foreign = [path.name for path in foreign]
        for path in foreign:
            path.mkdir(mode=0o700)
        before = [path.stat().st_ino for path in foreign]
        def cut_open():
            try:
                native_cut(lambda: self.forward(target, preimage), 'marker-open', 'mkdir', attempt=1)
            except AssertionError:
                self.fail('the owned build did not open beside the foreign bare-prefix directory')

        cut_open()
        with self._journal(target) as journal:
            pending, = journal.pending()
            build = roots[0] / pending.target['slots']['build']
            identity = store_steps._marker_identity(build.stat())
        self.forward(target, None)
        with self._journal(target) as journal:
            received = store_layout._row(journal._conn, 'SELECT observed FROM receipts WHERE intent_id=?',
                                         (pending.id,))
        import json
        self.assertEqual(json.loads(received[0])['build_identity'], identity)
        self.assertEqual([path.stat().st_ino for path in foreign], before)
        self._verify(case)

    def test_reserved_inverse_discard_stops_before_any_effect(self):
        for marked in (False, True):
            with self.subTest(marked=marked):
                roots, target, preimage, inventory, _ = self.fixture('reserved-discard-' + str(marked))
                if marked:
                    self.forward(target, preimage)
                root = roots[0]
                reserved = root / f'.marker-discard-{JOB}-0-inverse'
                reserved.mkdir()
                prior = inventory(root)
                run = (lambda: self.inverse(target)) if marked else (lambda: self.forward(target, preimage))
                with self.assertRaises(store_steps.StepStateUnknown) as stopped:
                    run()
                self.assertIn('identity', stopped.exception.observed['inverse_discard'])
                self.assertEqual(inventory(root), prior)
                with self._journal(target) as journal:
                    self.assertEqual(journal.pending(), [])

    def test_partial_fill_retries_as_next_attempt(self):
        case = self.fixture('partial')
        _, target, preimage, *_ = case
        run = lambda: self.forward(target, preimage)
        native_cut(run, 'marker-fill', 'file-sync', attempt=1)
        for step, where in (('marker-fill', 'receipt'), ('dispose-tomb', 'rename'),
                            ('dispose-clear', 'unlink'), ('dispose-clear', 'receipt')):
            native_cut(run, step, where, attempt=1)
        native_cut(run, 'marker-fill', 'file-sync', attempt=2)
        run()
        self._verify(case)
        with self._journal(target) as journal:
            rows = journal._conn.execute(
                'SELECT i.step,json_extract(i.target,\'$.attempt\'),r.observed '
                'FROM intents i JOIN receipts r ON r.intent_id=i.id ORDER BY i.id').fetchall()
        self.assertEqual([item[1] for item in rows if item[0] == 'marker-place'], [3])
        self.assertEqual([item[1] for item in rows if item[0] == 'marker-fill'
                          and item[2] == '{"abandoned":true}'], [1, 2])
        self.assertEqual([item[1] for item in rows if item[0] == 'dispose-clear'], [1, 2])

    def test_progress_between_receipts(self):
        case = self.fixture('progress')
        _, target, preimage, *_ = case
        forward = lambda: self.forward(target, preimage)
        for step, next_step in (('marker-open', 'marker-fill'), ('marker-fill', 'marker-seal'),
                                ('marker-seal', 'marker-aside'), ('marker-aside', 'marker-place'),
                                ('marker-place', None)):
            native_cut(forward, step, 'receipt')
            with self._journal(target) as journal:
                state = store_steps.marker_progress(journal, target)
                self.assertIsNone(state.pending)
                self.assertEqual(state.next_step, next_step)
                self.assertEqual(state.phase, 'forward' if next_step else 'marked')
        self._verify(case)
        for step, next_step in (('inverse-unplace', 'inverse-restore'), ('inverse-restore', 'dispose-tomb'),
                                ('dispose-tomb', 'dispose-clear'), ('dispose-clear', None)):
            native_cut(lambda: self.inverse(target), step, 'receipt')
            with self._journal(target) as journal:
                state = store_steps.marker_progress(journal, target)
                self.assertIsNone(state.pending)
                self.assertEqual(state.next_step, next_step)
                self.assertEqual(state.phase, 'inverse' if next_step else 'restored')
        self._verify(case, inverse=True)

    def test_marker_cut_with_changed_byte_stops(self):
        for label, point, slot in (('marker-aside', 'rename', 'parked'),
                                    ('marker-place', 'intent', 'stage'),
                                    ('inverse-unplace', 'rename', 'sibling')):
            with self.subTest(slot=slot):
                roots, target, preimage, inventory, _ = self.fixture('changed-' + slot)
                inverse = slot == 'sibling'
                if inverse:
                    self.forward(target, preimage)
                run = (lambda: self.inverse(target)) if inverse else (lambda: self.forward(target, preimage))
                native_cut(run, label, point)
                with self._journal(target) as journal:
                    pending, = journal.pending()
                root = roots[0]
                file = root / pending.target['slots'][slot] / 'daemon.json'
                original_mode = stat.S_IMODE(file.stat().st_mode)
                file.chmod(0o600)
                payload = file.read_bytes()
                file.write_bytes(b'!' + payload[1:])
                file.chmod(original_mode)
                prior = [inventory(p) for p in roots]
                with self.assertRaises(store_steps.StepStateUnknown):
                    run()
                with self._journal(target) as journal:
                    self.assertEqual(journal.pending(), [pending])
                self.assertEqual([inventory(p) for p in roots], prior)

    def test_slot_identity_binds_ownership(self):
        for label, point, slot in (('marker-place', 'intent', 'stage'),
                                    ('marker-aside', 'rename', 'parked'),
                                    ('inverse-restore', 'intent', 'sibling'),
                                    ('dispose-clear', 'intent', 'inverse_discard')):
            with self.subTest(slot=slot):
                roots, target, preimage, inventory, _ = self.fixture('identity-' + slot)
                inverse = slot in ('sibling', 'inverse_discard')
                if inverse:
                    self.forward(target, preimage)
                run = (lambda: self.inverse(target)) if inverse else (lambda: self.forward(target, preimage))
                native_cut(run, label, point)
                with self._journal(target) as journal:
                    original_intent, = journal.pending()
                path = roots[0] / original_intent.target['slots'][slot]
                moved = path.with_name('.untouched-original')
                path.rename(moved)
                shutil.copytree(moved, path)
                prior = inventory(roots[0])
                with self.assertRaises(store_steps.StepStateUnknown):
                    run()
                self.assertEqual(inventory(roots[0]), prior)
                with self._journal(target) as journal:
                    self.assertEqual(journal.pending(), [original_intent])

    def test_foreign_sibling_not_adopted(self):
        roots, target, preimage, inventory, _ = self.fixture('foreign')
        native_cut(lambda: self.forward(target, preimage), 'marker-place', 'intent')
        with self._journal(target) as journal:
            marker_intent, = journal.pending()
        root = roots[0]
        stage = root / marker_intent.target['slots']['stage']
        foreign = root / '.marker-unrelated'
        shutil.copytree(stage, foreign)
        complete = inventory(foreign)
        self.forward(target, None)
        self.assertEqual(inventory(foreign), complete)
        self.inverse(target)
        self.assertEqual(inventory(foreign), complete)
