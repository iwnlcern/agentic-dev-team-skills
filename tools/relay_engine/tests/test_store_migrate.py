"""Manifest, quiet and copy boundaries on synthetic 2.9.7 roots."""
from contextlib import closing, contextmanager, ExitStack
import hashlib
import importlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit
import uuid

from relay_engine import store_layout, store_steps


NOW_MS = 1_790_000_000_000
PROJECT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
PRIOR_JOB = '019a7850-0000-7000-8000-000000000001'
PRIOR_STORE = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
ROOT_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
ELIGIBILITY_REFUSALS = (
    ValueError, store_layout.NameTaken, store_layout.ReservationChanged, store_layout.HoldConflict,
)


class InterruptedCopy(Exception):
    """A fixture interruption at a copy boundary."""


class QuietClock:
    def __init__(self, during_sleep=None):
        self.elapsed = 0.0
        self.sleeps = []
        self.during_sleep = during_sleep

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        if self.during_sleep is not None:
            self.during_sleep()


def source_snapshot(root):
    entries = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            entries[str(path.relative_to(root))] = ('link', os.readlink(path))
        elif path.is_file():
            stat = path.stat()
            entries[str(path.relative_to(root))] = (
                stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, path.read_bytes(),
            )
    return entries


class StoreMigrationCopyTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()

    def producer(self, first_api):
        module = 'relay_engine.store_migrate'
        try:
            return importlib.import_module(module)
        except ModuleNotFoundError as error:
            if error.name != module:
                raise
        self.fail(f'reached {first_api}: missing producer {module}')

    def fixture(self, label):
        area = self.scratch / label
        area.mkdir(mode=0o700)
        root = area / 'store'
        with closing(store_layout.open_catalogue(root)):
            pass
        legacy = area / 'legacy'
        legacy.mkdir(mode=0o700)
        engine = legacy / '.engine'
        engine.mkdir(mode=0o700)
        seed = area / 'seed.sqlite3'
        with closing(sqlite3.connect(seed)) as conn, conn:
            conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)').close()
            conn.executemany('INSERT INTO meta VALUES (?, ?)', (
                ('schema_version', '1'), ('canonical_root', str(legacy)),
                ('root_uuid', ROOT_ID), ('run_id', 'fixture'),
            )).close()
        (engine / 'ledger.db').write_bytes(seed.read_bytes())
        (engine / 'ledger.db-wal').write_bytes(b'')
        (engine / 'ledger.db-shm').write_bytes(b'')
        (legacy / 'relays').mkdir(mode=0o700)
        (legacy / 'relays' / 'notice.md').write_bytes(b'Fixture final notice\n')
        snapshot = source_snapshot(legacy)
        self.assertEqual(set(snapshot), {
            '.engine/ledger.db', '.engine/ledger.db-wal',
            '.engine/ledger.db-shm', 'relays/notice.md',
        })
        self.assertTrue(snapshot['.engine/ledger.db'][-1].startswith(b'SQLite format 3\x00'))
        self.assertEqual(snapshot['relays/notice.md'][-1], b'Fixture final notice\n')
        turns = store_steps.DirectTurns(root)
        self.assertTrue(self.store_id(turns))
        return root, turns, legacy

    def store_id(self, turns):
        def read(conns):
            with closing(conns['catalogue'].execute('SELECT store_id FROM catalogue_identity')) as rows:
                return rows.fetchone()[0]
        return turns.run(store_steps.Declared('read', {}), read)

    def start(self, migrate, root, turns, legacy, *, mode='new-project', name='fixture', roots=None):
        paths = (legacy,) if roots is None else roots
        return migrate.start(root, turns, migrate.MigrationManifest(mode, name, paths), NOW_MS)

    def quiet(self, migrate, root, turns, job_id, *, clock=None, status=None):
        clock = QuietClock() if clock is None else clock
        status = (lambda _root: False) if status is None else status
        with mock.patch.object(time, 'monotonic', clock.monotonic), mock.patch.object(
                time, 'sleep', clock.sleep):
            return migrate.quiet(root, turns, job_id, status)

    def job_path(self, root, job_id):
        self.assertEqual(uuid.UUID(job_id).version, 7)
        path = root / 'migrations' / job_id
        self.assertTrue((path / 'job.sqlite3').is_file())
        return path

    def checkpoint(self, root, job_id):
        with closing(store_steps.open_journal(self.job_path(root, job_id))) as journal:
            return journal.last_checkpoint()

    def seed_reservation(self, root, turns, *, case):
        job_id = None if case == 'base creation' else PRIOR_JOB
        if job_id is not None:
            store_id = PRIOR_STORE if case == 'orphaned' else self.store_id(turns)
            with closing(store_steps.open_journal(
                    store_steps.migration_dir(root, job_id), create=True)) as journal:
                journal.checkpoint('manifest', {
                    'job_id': job_id, 'store_id': store_id, 'project_id': PROJECT,
                    'project_name': 'reserved', 'mode': 'new-project', 'roots': [],
                })
                if case in ('undone', 'held', 'ledger', 'other name', 'held name'):
                    journal.checkpoint('undo', {'project_id': PROJECT})

        def write(conns):
            conn = conns['catalogue']
            store_layout.reserve_project(conn, PROJECT, 'reserved', NOW_MS, held_by_job=job_id)
            if case == 'active':
                store_layout.activate_project(conn, PROJECT)
            if case == 'held':
                store_layout.take_holds(conn, PRIOR_JOB, 'undo', (store_layout.project_scope(PROJECT),), NOW_MS)
        turns.run(store_steps.Declared('written', {}), write)
        if case == 'ledger':
            with closing(store_layout.open_ledger(root, PROJECT, create=True)) as conn:
                conn.execute(
                    'INSERT INTO projects(id,name,created_at) VALUES (?,?,?)',
                    (PROJECT, 'reserved', NOW_MS),
                ).close()
                conn.commit()

    def test_manifest_modes(self):
        root, turns, legacy = self.fixture('new-project')
        migrate = self.producer('start')
        job_id = self.start(migrate, root, turns, legacy)
        phase, manifest = self.checkpoint(root, job_id)
        self.assertEqual(phase, 'manifest')
        self.assertEqual(manifest['store_id'], self.store_id(turns))
        encoded = repr(manifest)
        for identity in (str(legacy), ROOT_ID, 'fixture'):
            self.assertIn(identity, encoded)

        for case in ('undone', 'orphaned', 'active', 'base creation', 'other name',
                     'in progress', 'held', 'ledger', 'held name'):
            with self.subTest(case=case):
                root, turns, legacy = self.fixture(case)
                self.seed_reservation(root, turns, case=case)
                mode = 'new-project' if case == 'held name' else 'takeover'
                name = 'unreserved' if case == 'other name' else 'reserved'
                if case in ('undone', 'orphaned'):
                    job_id = self.start(migrate, root, turns, legacy, mode=mode, name=name)
                    phase, manifest = self.checkpoint(root, job_id)
                    self.assertEqual(phase, 'manifest')
                    self.assertEqual(manifest['project_id'], PROJECT)
                    self.assertEqual(manifest['project_name'], 'reserved')
                else:
                    with self.assertRaises(ELIGIBILITY_REFUSALS):
                        self.start(migrate, root, turns, legacy, mode=mode, name=name)

        root, turns, legacy = self.fixture('duplicate')
        with self.assertRaises(ELIGIBILITY_REFUSALS):
            self.start(migrate, root, turns, legacy, roots=(legacy, legacy))
        root, turns, legacy = self.fixture('already moved')
        engine = legacy / '.engine'
        (engine / 'seats').mkdir(mode=0o700)
        (engine / 'drafts').mkdir(mode=0o700)
        original = engine.rename(legacy / '.engine.migrated-to-v3')
        engine.mkdir(mode=0o700)
        seats = engine / 'seats'
        seats.mkdir(mode=0o700)
        drafts = engine / 'drafts'
        drafts.mkdir(mode=0o700)
        # The original seats and drafts are empty in this synthetic root.
        self.assertEqual(list((original / 'seats').iterdir()), [])
        self.assertEqual(list((original / 'drafts').iterdir()), [])
        self.assertTrue((original / 'ledger.db').is_file())
        marker = engine / 'daemon.json'
        marker.write_bytes(
            b'{"state": "ready", "socket": "/nonexistent/adt-v3-moved/s", '
            b'"identity": {"kit": "3.0.0", "fp": "'
            + b'0' * 64
            + b'", "install": "/MOVED TO KIT 3.0.0 - this root is served by the global '
            b'v3 engine; finish or restart this session on kit 3.0.0"}, '
            b'"nonce": "00000000-0000-0000-0000-000000000000", "pid_start_time": "0"}\n'
        )
        self.addCleanup(seats.chmod, 0o700)
        self.addCleanup(engine.chmod, 0o700)
        marker.chmod(0o400)
        seats.chmod(0o500)
        engine.chmod(0o500)
        self.assertEqual(hashlib.sha256(marker.read_bytes()).hexdigest(),
                         '4a9c34859cf78db4a00dfcdbf152dc08650613784507b1b9eea6a9fc024632f0')
        self.assertEqual(tuple(p.stat().st_mode & 0o777 for p in (engine, seats, drafts, marker)),
                         (0o500, 0o500, 0o700, 0o400))
        self.assertFalse((engine / 'ledger.db').exists())
        with self.assertRaises(ELIGIBILITY_REFUSALS) as refusal:
            self.start(migrate, root, turns, legacy)
        self.assertIn('moved-layout marker', str(refusal.exception))

    @contextmanager
    def observe_opens(self, inspect_before, inspect_after=lambda _fd, _name, _dir_fd: None):
        os_open, io_open = os.open, io.open

        def open_fd(name, *args, **kwargs):
            inspect_before(name, kwargs.get('dir_fd'))
            fd = os_open(name, *args, **kwargs)
            try:
                inspect_after(fd, name, kwargs.get('dir_fd'))
            except BaseException:
                os.close(fd)
                raise
            return fd

        def open_file(name, *args, **kwargs):
            inspect_before(name, None)
            handle = io_open(name, *args, **kwargs)
            try:
                inspect_after(handle.fileno(), name, None)
            except BaseException:
                handle.close()
                raise
            return handle

        with mock.patch.object(os, 'open', open_fd), mock.patch.object(io, 'open', open_file), \
                mock.patch('builtins.open', open_file):
            yield

    def names_entry(self, name, dir_fd, entry):
        if isinstance(name, int):
            return False
        name = Path(os.fsdecode(name))
        if dir_fd is None or name.is_absolute():
            return name == entry
        return name == Path(entry.name) and os.path.samestat(os.fstat(dir_fd), entry.parent.stat())

    def test_not_quiet_refuses(self):
        root, turns, legacy = self.fixture('wal-changes')
        wal = legacy / '.engine' / 'ledger.db-wal'
        self.assertEqual(wal.read_bytes(), b'')
        migrate = self.producer('quiet')
        job_id = self.start(migrate, root, turns, legacy)
        before = self.checkpoint(root, job_id)
        self.assertEqual(before[0], 'manifest')
        seen_at = []
        status_paths = []
        open_wal = io.open

        def change_wal():
            with open_wal(wal, 'wb') as stream:
                stream.write(b'changed during quiet')

        clock = QuietClock(change_wal)

        def observe(name, dir_fd):
            if self.names_entry(name, dir_fd, wal):
                seen_at.append(clock.monotonic())

        def status(path):
            status_paths.append(Path(path))
            return False

        with self.observe_opens(observe), self.assertRaises(migrate.NotQuiet) as error:
            self.quiet(migrate, root, turns, job_id, clock=clock, status=status)
        self.assertIn('ledger.db-wal', str(error.exception))
        self.assertEqual(set(status_paths), {legacy})
        self.assertEqual(sorted(set(seen_at)), [0.0, 60.0])
        self.assertEqual(sum(clock.sleeps), 60.0)
        self.assertEqual(self.checkpoint(root, job_id), before)

    def copied_files(self, root, job_id, wanted):
        source = self.job_path(root, job_id) / 'source'
        (ledger,) = source.glob('*/ledger.db')
        return {name: ledger.with_name(name) for name in wanted}

    def test_copy_never_opens_in_place(self):
        root, turns, legacy = self.fixture('raw-copy')
        original = source_snapshot(legacy)
        migrate = self.producer('copy')
        connect = sqlite3.connect
        sqlite_sources = []

        def guarded(database, *args, **kwargs):
            value = os.fsdecode(database)
            candidate = Path(unquote(urlsplit(value).path) if value.startswith('file:') else value)
            if candidate.resolve().is_relative_to(legacy):
                sqlite_sources.append(candidate)
                self.fail(f'SQLite opened the legacy source: {candidate}')
            return connect(database, *args, **kwargs)

        with mock.patch.object(sqlite3, 'connect', guarded):
            job_id = self.start(migrate, root, turns, legacy)
            self.quiet(migrate, root, turns, job_id)
            self.assertEqual(self.checkpoint(root, job_id)[0], 'quiet')
            second = source_snapshot(legacy)
            migrate.copy(root, turns, job_id)
        self.assertEqual(sqlite_sources, [])
        self.assertEqual(source_snapshot(legacy), original)
        self.assertEqual(second, original)
        wanted = ('ledger.db', 'ledger.db-wal', 'ledger.db-shm')
        for name, copied in self.copied_files(root, job_id, wanted).items():
            dev, ino, size, _, content = second[f'.engine/{name}']
            stat = copied.stat()
            self.assertEqual((stat.st_size, hashlib.sha256(copied.read_bytes()).digest()),
                             (size, hashlib.sha256(content).digest()))
            self.assertFalse(copied.is_symlink())
            self.assertFalse(copied.parent.is_symlink())
            self.assertFalse(copied.parent.parent.is_symlink())
            self.assertNotEqual((stat.st_dev, stat.st_ino), (dev, ino))
        self.assertEqual(self.checkpoint(root, job_id)[0], 'copy')

    def test_copy_refuses_legacy_source_link(self):
        control = self.fixture('link-plain-control')
        self.assertFalse((control[2] / '.engine' / 'ledger.db').is_symlink())
        migrate = self.producer('copy')
        job_id = self.start(migrate, *control)
        self.quiet(migrate, *control[:2], job_id)
        migrate.copy(*control[:2], job_id)
        self.assertEqual(self.checkpoint(control[0], job_id)[0], 'copy')

        cases = (
            ('enumerated internal', 'relays/discovered.md', 'internal'),
            ('enumerated dangling', 'relays/discovered.md', 'missing'),
            ('consumed leaf', '.engine/ledger.db', 'external'),
            ('consumed directory', '.engine', 'directory'),
            ('substituted leaf', '.engine/ledger.db', 'swap'),
        )
        for label, relative, target_kind in cases:
            with self.subTest(case=label):
                self.assert_link_refused(migrate, label, relative, target_kind)

    def assert_link_refused(self, migrate, label, relative, target_kind):
        root, turns, legacy = self.fixture(label)
        job_id = self.start(migrate, root, turns, legacy)
        entry = legacy / relative
        outside = legacy.parent / 'outside'
        sentinel = outside
        if target_kind == 'directory':
            outside.mkdir()
            sentinel = outside / 'ledger.db'
        if target_kind == 'internal':
            sentinel = legacy / 'relays' / 'notice.md'
        if target_kind not in ('missing', 'internal'):
            sentinel.write_bytes(b'untouchable sentinel')
        self.quiet(migrate, root, turns, job_id)
        checkpoint = self.checkpoint(root, job_id)
        target = outside if target_kind == 'directory' else sentinel
        target_stats = [path.stat() for path in {target, sentinel} if path.exists()]
        moved = legacy.parent / 'before-substitution'

        def install_link():
            if entry.exists():
                entry.rename(moved)
            entry.symlink_to(target, target_is_directory=target_kind == 'directory')

        if target_kind != 'swap':
            install_link()
        before = source_snapshot(legacy)
        before_target = sentinel.read_bytes() if sentinel.exists() else None
        access = []
        substitutions = []

        def inspect_open(name, dir_fd):
            if target_kind == 'swap' and not substitutions and self.names_entry(name, dir_fd, entry):
                install_link()
                substitutions.append(entry)

        def inspect_target(fd, name, dir_fd):
            actual = os.fstat(fd)
            through_entry = self.names_entry(name, dir_fd, entry)
            if (through_entry or target_kind != 'internal') and any(
                    os.path.samestat(actual, stat) for stat in target_stats):
                access.append(actual.st_ino)

        scandir = os.scandir

        def inspect_walk(folder):
            # scandir can enter a directory directly, or consume a descriptor already checked above.
            if not isinstance(folder, int) and target_kind == 'directory':
                candidate = Path(folder)
                if candidate.is_relative_to(entry) or candidate.is_relative_to(target):
                    access.append(str(candidate))
            return scandir(folder)

        with self.observe_opens(inspect_open, inspect_target), mock.patch.object(
                os, 'scandir', inspect_walk), self.assertRaises(
                (OSError, ValueError, migrate.NotQuiet)) as error:
            migrate.copy(root, turns, job_id)
        self.assertIn(str(entry), str(error.exception))
        self.assertEqual(access, [])
        self.assertEqual(self.checkpoint(root, job_id), checkpoint)
        self.assertFalse((self.job_path(root, job_id) / 'source').exists())
        if target_kind == 'swap':
            self.assertEqual(substitutions, [entry])
            self.assertEqual(entry.readlink(), target)
            self.assertEqual(moved.read_bytes(), before[relative][-1])
            del before[relative]
            before[relative] = ('link', str(target))
        self.assertEqual(source_snapshot(legacy), before)
        self.assertEqual(sentinel.read_bytes() if sentinel.exists() else None, before_target)

    def test_store_id_binding_before_copy(self):
        root, turns, legacy = self.fixture('orphan-before-quiet')
        other = self.fixture('orphan-before-copy')
        migrate = self.producer('quiet')
        first = self.start(migrate, root, turns, legacy)
        second = self.start(migrate, *other)
        self.quiet(migrate, *other[:2], second)

        for (store, runner, source), job_id, phase in (
                ((root, turns, legacy), first, 'quiet'), (other, second, 'copy')):
            with self.subTest(phase=phase):
                before = self.checkpoint(store, job_id)
                before_source = source_snapshot(source)
                current = self.store_id(runner)

                def replace_identity(conns):
                    conns['catalogue'].execute(
                        'UPDATE catalogue_identity SET store_id=?', (PRIOR_STORE,),
                    ).close()
                runner.run(store_steps.Declared('written', {}), replace_identity)
                self.assertNotEqual(self.store_id(runner), current)

                def guard(name, dir_fd):
                    if isinstance(name, int):
                        return
                    candidate = Path(os.fsdecode(name))
                    if candidate.is_absolute() and candidate.is_relative_to(source):
                        self.fail(f'orphaned job accessed {candidate}')
                    if dir_fd is not None and candidate.name in {
                            '.engine', 'ledger.db', 'ledger.db-wal', 'ledger.db-shm', 'notice.md'}:
                        self.fail(f'orphaned job accessed {candidate}')

                def check_path(operation):
                    def checked(name, *args, **kwargs):
                        guard(name, kwargs.get('dir_fd'))
                        return operation(name, *args, **kwargs)
                    return checked

                status = mock.Mock(side_effect=AssertionError('orphaned job read status'))
                fresh = store_steps.DirectTurns(store)
                with self.observe_opens(guard), mock.patch.object(
                        os, 'stat', check_path(os.stat)), mock.patch.object(
                        os, 'lstat', check_path(os.lstat)), mock.patch.object(
                        os, 'scandir', check_path(os.scandir)), self.assertRaises(migrate.JobOrphaned):
                    if phase == 'quiet':
                        self.quiet(migrate, store, fresh, job_id, status=status)
                    else:
                        migrate.copy(store, fresh, job_id)
                status.assert_not_called()
                self.assertEqual(source_snapshot(source), before_source)
                self.assertEqual(self.checkpoint(store, job_id), before)
                self.assertFalse((self.job_path(store, job_id) / 'source').exists())

    def test_copy_checkpoint_resume(self):
        root, turns, legacy = self.fixture('interrupted')
        self.assertFalse((root / 'migrations').exists())
        migrate = self.producer('copy')
        # Three jobs use the same bytes and fingerprint from the same legacy root.
        jobs = [self.start(migrate, root, turns, legacy, name=name)
                for name in ('reference', 'before-publish', 'before-checkpoint')]
        manifests = {job_id: self.checkpoint(root, job_id)[1] for job_id in jobs}
        for job_id in jobs:
            self.quiet(migrate, root, turns, job_id)
        reference, before_publish, before_checkpoint = jobs
        migrate.copy(root, turns, reference)
        clean = self.completed_copy(root, reference, manifests[reference])

        rename, replace = os.rename, os.replace

        def interrupt_publication(native):
            def move(origin, destination, *args, **kwargs):
                if Path(os.fsdecode(destination)).name == 'source':
                    raise InterruptedCopy('before publication')
                return native(origin, destination, *args, **kwargs)
            return move

        with mock.patch.object(os, 'rename', interrupt_publication(rename)), mock.patch.object(
                os, 'replace', interrupt_publication(replace)), self.assertRaisesRegex(
                InterruptedCopy, 'before publication'):
            migrate.copy(root, turns, before_publish)
        self.assertFalse((self.job_path(root, before_publish) / 'source').exists())

        checkpoint = store_steps.Journal.checkpoint

        def interrupt_checkpoint(journal, phase, data):
            if phase == 'copy':
                raise InterruptedCopy('before durable copy checkpoint')
            return checkpoint(journal, phase, data)

        with mock.patch.object(store_steps.Journal, 'checkpoint', interrupt_checkpoint), \
                self.assertRaisesRegex(InterruptedCopy, 'before durable copy checkpoint'):
            migrate.copy(root, turns, before_checkpoint)

        relay = legacy / 'relays' / 'notice.md'
        original, stat = relay.read_bytes(), relay.stat()
        for job_id in (before_publish, before_checkpoint):
            with self.subTest(job=job_id):
                self.assertEqual(self.checkpoint(root, job_id)[0], 'quiet')
                relay.write_bytes(b'drift following interruption')
                try:
                    with self.assertRaises(migrate.NotQuiet):
                        migrate.copy(root, store_steps.DirectTurns(root), job_id)
                    self.assertEqual(self.checkpoint(root, job_id)[0], 'quiet')
                finally:
                    relay.write_bytes(original)
                    os.utime(relay, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                migrate.copy(root, store_steps.DirectTurns(root), job_id)
                self.assertEqual(self.completed_copy(root, job_id, manifests[job_id]), clean)

    def completed_copy(self, root, job_id, manifest):
        path = self.job_path(root, job_id)
        phase, data = self.checkpoint(root, job_id)
        identity = {manifest['project_name']: '<project-name>',
                    manifest['project_id']: '<project-id>'}

        def normalize(value):
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if isinstance(value, str):
                return identity.get(value, value.replace(job_id, '<job>'))
            return value

        self.assertEqual(phase, 'copy')
        files = {}
        for name, entry in source_snapshot(path / 'source').items():
            if name.endswith(('/ledger.db', '/ledger.db-wal', '/ledger.db-shm')):
                files[name] = entry[-1]
        self.assertEqual(len(files), 3)
        return normalize(data), files
