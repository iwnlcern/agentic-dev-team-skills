"""Manifest, quiet and copy boundaries on synthetic 2.9.7 roots."""
from collections.abc import Sequence
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

from relay_engine import jcs, store_import, store_layout, store_steps


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

    def test_committed_copy_reader(self):
        import json
        import shutil
        import stat

        from relay_engine import store_import
        from relay_engine.tests.test_store_import import LEGACY_DDL

        migrate = self.producer('copy')
        rendered_path = 'fixture/r1.md'
        original_mtime = 1793525400000000000
        headers = {'FROM': 'fixture.planner', 'TO': '', 'CC': ''}
        document = b'# synthetic\n\nPHASE: SITREP\nFROM: fixture.planner\nTO: \nCC: \n\nfixture\n'

        def inventory(directory):
            entries = {}
            for entry in (directory, *sorted(directory.rglob('*'))):
                metadata = entry.lstat()
                data = entry.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
                entries[entry.relative_to(directory).as_posix()] = (
                    metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mode,
                    metadata.st_mtime_ns, data,
                )
            return entries

        def make(label, *, literal=rendered_path, write_render=True, mtime=original_mtime,
                 rendered=document):
            root, turns, legacy = self.fixture(label)
            seed = legacy.parent / 'legacy-seed.sqlite3'
            with closing(sqlite3.connect(seed)) as conn, conn:
                conn.executescript(LEGACY_DDL).close()
                conn.executemany('INSERT INTO meta VALUES (?, ?)', (
                    ('schema_version', '1'), ('canonical_root', str(legacy)),
                    ('root_uuid', ROOT_ID), ('run_id', 'fixture'),
                )).close()
                conn.execute(
                    'INSERT INTO relays (seq,stamp,rendered_path,phase,role,dispatch_id,'
                    'run_id,from_seat,to_seats,cc_seats,subject,headers_json,body,'
                    'body_sha256,content_hash,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (1, '20261101-013000', literal, 'SITREP', 'Pair Planner', 'fixture-1',
                     'fixture', 'fixture.planner', '[]', '[]', 'Synthetic source',
                     json.dumps(headers), document, hashlib.sha256(document).hexdigest(),
                     '0' * 64, 'hand'),
                ).close()
            engine = legacy / '.engine'
            (engine / 'ledger.db').write_bytes(seed.read_bytes())
            self.assertTrue((engine / 'ledger.db-wal').is_file())
            self.assertTrue((engine / 'ledger.db-shm').is_file())
            render = legacy / rendered_path
            if write_render:
                self.assertEqual(literal, rendered_path)
                render.parent.mkdir()
                render.write_bytes(rendered)
                os.utime(render, ns=(mtime, mtime))
                self.assertEqual(render.stat().st_mtime_ns, mtime)
            return root, turns, legacy, render, self.start(migrate, root, turns, legacy)

        def job_paths(root, job_id):
            job = self.job_path(root, job_id)
            return job, job / 'source' / '0'

        @contextmanager
        def private_sqlite(legacy, job):
            connect = sqlite3.connect
            duplicates = []
            ordinary = []

            def guarded(database, *args, **kwargs):
                value = os.fsdecode(database)
                path = Path(unquote(urlsplit(value).path) if value.startswith('file:') else value)
                resolved = path.resolve()
                self.assertFalse(resolved.is_relative_to(legacy), resolved)
                if resolved.name == 'ledger.db':
                    self.assertFalse(resolved.is_relative_to(job), resolved)
                    duplicates.append(resolved.parent)
                else:
                    ordinary.append(resolved)
                return connect(database, *args, **kwargs)

            with mock.patch.object(sqlite3, 'connect', guarded):
                yield duplicates, ordinary
            self.assertTrue(all(not path.exists() for path in duplicates))

        def root_read_guard(legacy):
            original_dirs = {(item.stat().st_dev, item.stat().st_ino)
                             for item in (legacy, *legacy.rglob('*')) if item.is_dir()}

            def guard(name, descriptor):
                if isinstance(name, int):
                    return
                candidate = Path(os.fsdecode(name))
                if candidate.is_absolute():
                    self.assertFalse(candidate.is_relative_to(legacy), candidate)
                elif descriptor is not None:
                    metadata = os.fstat(descriptor)
                    self.assertNotIn((metadata.st_dev, metadata.st_ino), original_dirs, candidate)
            return self.observe_opens(guard)

        def checked_read(root, turns, legacy, job_id):
            job, _ = job_paths(root, job_id)
            with private_sqlite(legacy, job) as (duplicates, ordinary), root_read_guard(legacy):
                roots = migrate.read_copied_sources(root, turns, job_id)
            self.assertTrue(duplicates)
            self.assertTrue(ordinary)
            (source,) = roots
            self.assertEqual((source.root_uuid, source.run_id), (ROOT_ID, 'fixture'))
            return roots

        def quiet_copy(root, turns, legacy, job_id):
            self.quiet(migrate, root, turns, job_id)
            job, _ = job_paths(root, job_id)
            with private_sqlite(legacy, job):
                migrate.copy(root, turns, job_id)
            self.assertEqual(self.checkpoint(root, job_id)[0], 'copy')

        def assert_plan(roots):
            plan = store_import.plan_relays(roots)
            self.assertEqual(plan.listed, (
                store_import.Listed('offset resolved by file mtime', rendered_path, False),
            ))
            self.assertEqual(plan.refused, 0)
            (relay,) = plan.relays
            self.assertEqual(relay.header_bytes + relay.body, document)
            self.assertEqual(relay.created_at, original_mtime // 1_000_000)
            self.assertEqual(roots[0].relays[0]['rendered_bytes'], document)
            return plan

        def assert_refused(roots, literal):
            plan = store_import.plan_relays(roots)
            self.assertEqual(plan.relays, ())
            self.assertEqual(plan.refused, 1)
            self.assertIn(store_import.Listed('source bytes differ', literal), plan.listed)

        def invalid(root, turns, legacy, job_id, reason):
            job, _ = job_paths(root, job_id)
            phase = self.checkpoint(root, job_id)
            retained = inventory(job / 'source')
            before_source = inventory(legacy)
            with (private_sqlite(legacy, job), root_read_guard(legacy),
                  self.assertRaises(ValueError) as failure):
                migrate.read_copied_sources(root, turns, job_id)
            self.assertEqual(failure.exception.reason, reason)
            self.assertIn(job_id, str(failure.exception))
            self.assertIn('0', str(failure.exception))
            self.assertEqual(self.checkpoint(root, job_id), phase)
            self.assertEqual(inventory(job / 'source'), retained)
            self.assertEqual(inventory(legacy), before_source)

        root, turns, legacy, render, job_id = make('reader-repeated-hour')
        original = inventory(legacy)
        quiet_copy(root, turns, legacy, job_id)
        self.assertEqual(inventory(legacy), original)
        self.assertTrue(callable(getattr(migrate, 'read_copied_sources', None)),
                        'missing store_migrate.read_copied_sources')

        with self.subTest(case='repeated-hour-replay'):
            job, copy_root = job_paths(root, job_id)
            self.assertTrue((copy_root / 'ledger.db').is_file())
            self.assertTrue((copy_root / 'ledger.db-wal').is_file())
            self.assertTrue((copy_root / 'ledger.db-shm').is_file())
            checkpoint = self.checkpoint(root, job_id)
            first = assert_plan(checked_read(root, turns, legacy, job_id))
            copied = copy_root / 'rendered' / rendered_path
            os.utime(copied, ns=(original_mtime + 3_600_000_000_000,) * 2)
            retained = inventory(job / 'source')
            second = assert_plan(checked_read(root, store_steps.DirectTurns(root), legacy, job_id))
            self.assertEqual(first, second)
            self.assertEqual(inventory(legacy), original)
            self.assertEqual(inventory(job / 'source'), retained)
            self.assertEqual(self.checkpoint(root, job_id), checkpoint)
            print('copy-reader case: repeated-hour-replay')

        with self.subTest(case='integer-second-boundary'):
            r, t, l, _, j = make('reader-second-boundary', mtime=original_mtime + 999_999_999)
            quiet_copy(r, t, l, j)
            assert_plan(checked_read(r, t, l, j))
            print('copy-reader case: integer-second-boundary')

        for label, literal in (('missing-at-quiet', rendered_path),
                               ('unsafe-ledger-spelling', '../outside.md')):
            with self.subTest(case=label):
                r, t, l, _, j = make(label, literal=literal, write_render=False)
                before = inventory(l)

                def no_unsafe_open(name, _dir_fd):
                    if not isinstance(name, int):
                        self.assertNotIn('outside.md', os.fsdecode(name))

                with self.observe_opens(no_unsafe_open):
                    quiet_copy(r, t, l, j)
                roots = checked_read(r, t, l, j)
                self.assertEqual(len(roots[0].relays), 1)
                (row,) = roots[0].relays
                self.assertEqual((row['seq'], row['rendered_path']), (1, literal))
                self.assertIsNone(row['rendered_bytes'])
                self.assertIsNone(row['file_mtime'])
                self.assertTrue(row['file_error'])
                assert_refused(roots, literal)
                self.assertEqual(inventory(l), before)
                print(f'copy-reader case: {label}')

        for label in ('selected-path-drift', 'selected-mtime-drift-during-quiet',
                      'selected-mtime-drift-after-quiet'):
            with self.subTest(case=label):
                r, t, l, path, j = make(label)
                if label == 'selected-mtime-drift-during-quiet':
                    self.quiet(migrate, r, t, j, clock=QuietClock(
                        lambda: os.utime(path, ns=(original_mtime + 1000,) * 2)))
                else:
                    self.quiet(migrate, r, t, j)
                    if label == 'selected-path-drift':
                        path.unlink()
                    else:
                        os.utime(path, ns=(original_mtime + 1000,) * 2)
                before = inventory(l)
                checkpoint = self.checkpoint(r, j)
                with self.assertRaises(migrate.NotQuiet) as failure:
                    migrate.copy(r, t, j)
                self.assertIn(rendered_path, str(failure.exception))
                self.assertEqual(self.checkpoint(r, j), checkpoint)
                self.assertFalse((self.job_path(r, j) / 'source').exists())
                self.assertEqual(inventory(l), before)
                print(f'copy-reader case: {label}')

        for label in ('committed-render-byte-damage', 'committed-render-absent',
                      'committed-receipt-damage', 'committed-receipt-missing-row'):
            with self.subTest(case=label):
                selected = label != 'committed-receipt-missing-row'
                r, t, l, _, j = make(label, write_render=selected)
                quiet_copy(r, t, l, j)
                job, copy_root = job_paths(r, j)
                copied = copy_root / 'rendered' / rendered_path
                receipt = copy_root / 'rendered-receipt.json'
                if label == 'committed-render-byte-damage':
                    data = copied.read_bytes()
                    copied.write_bytes(bytes((data[0] ^ 1,)) + data[1:])
                elif label == 'committed-render-absent':
                    copied.unlink()
                elif label == 'committed-receipt-damage':
                    receipt.write_bytes(receipt.read_bytes() + b'!')
                else:
                    record = json.loads(receipt.read_bytes())
                    self.assertEqual(len(record['defects']), 1)
                    record['defects'] = []
                    raw = json.dumps(record, sort_keys=True, separators=(',', ':')).encode() + b'\n'
                    receipt.write_bytes(raw)
                    _, data = self.checkpoint(r, j)
                    data['rendered_receipts'][0] = hashlib.sha256(raw).hexdigest()
                    with closing(store_steps.open_journal(job)) as journal:
                        journal.checkpoint('copy', data)
                invalid(r, t, l, j, 'copy-input-invalid')
                print(f'copy-reader case: {label}')

        with self.subTest(case='legacy-copy-checkpoint'):
            r, t, l, _, j = make('legacy-copy-checkpoint')
            self.quiet(migrate, r, t, j)
            _, data = self.checkpoint(r, j)
            data.pop('fingerprint_mtimes', None)
            job, copy_root = job_paths(r, j)
            copy_root.mkdir(parents=True)
            for name in ('ledger.db', 'ledger.db-wal', 'ledger.db-shm'):
                shutil.copyfile(l / '.engine' / name, copy_root / name)
            (copy_root / 'manifest.json').write_bytes(
                json.dumps(data['fingerprints'][0], sort_keys=True, separators=(',', ':')).encode() + b'\n')
            with closing(store_steps.open_journal(job)) as journal:
                journal.checkpoint('copy', data)
            invalid(r, t, l, j, 'copy-format-incompatible')
            print('copy-reader case: legacy-copy-checkpoint')

        with self.subTest(case='committed-ledger-document-mismatch'):
            r, t, l, _, j = make('committed-ledger-document-mismatch', rendered=document + b'changed')
            quiet_copy(r, t, l, j)
            assert_refused(checked_read(r, t, l, j), rendered_path)
            print('copy-reader case: committed-ledger-document-mismatch')

    def test_import_staging_rows(self):
        import json

        from relay_engine import store_import
        from relay_engine.tests.test_store_import import LEGACY_DDL

        migrate = self.producer('copy')
        root, turns, legacy_a = self.fixture('prepared-a')
        _, _, legacy_b = self.fixture('prepared-b')
        other_root_id = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
        parent_path, reply_path = 'fixture/parent.md', 'fixture/reply.md'
        stamp = '20260801-120000'
        parent = ('parent', parent_path, 'fixture.planner', 'fixture.implementer', b'parent payload\n')
        reply = ('reply', reply_path, 'fixture.implementer', 'fixture.planner', b'reply payload\n')
        expected = {}

        for legacy, source_id, rows, origin in (
                (legacy_a, ROOT_ID, (parent, reply), 'hand'),
                (legacy_b, other_root_id, (parent,), 'adopted')):
            seed = legacy.parent / 'staging-seed.sqlite3'
            with closing(sqlite3.connect(seed)) as db, db:
                db.executescript(LEGACY_DDL).close()
                db.executemany('INSERT INTO meta VALUES (?, ?)', (
                    ('schema_version', '1'), ('canonical_root', str(legacy)),
                    ('root_uuid', source_id), ('run_id', 'fixture'),
                )).close()
                db.execute("INSERT INTO runs VALUES ('fixture', 'operator')").close()
                for number, (name, path, sender, to, body) in enumerate(rows, 1):
                    headers = {'PHASE': 'SITREP', 'FROM': sender, 'TO': to, 'CC': 'operator'}
                    if name == 'reply':
                        headers['IN_REPLY_TO'] = parent_path
                    prefix = (f'# {name}\n\n' + ''.join(f'{k}: {v}\n' for k, v in headers.items()) + '\n').encode()
                    document = prefix + body
                    digest = hashlib.sha256(document).hexdigest()
                    target = legacy / path
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(document)
                    db.execute(
                        'INSERT INTO relays (seq,stamp,rendered_path,phase,role,dispatch_id,'
                        'run_id,from_seat,to_seats,cc_seats,subject,headers_json,body,'
                        'body_sha256,content_hash,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (number, stamp, path, 'SITREP',
                         'Pair Planner' if name == 'parent' else 'Pair Implementer',
                         name, 'fixture', sender, json.dumps([to]), '["operator"]',
                         name, json.dumps(headers), document, digest, digest, origin),
                    ).close()
                    expected[source_id, number] = (name, path, origin, headers, prefix, body, digest)
            (legacy / '.engine' / 'ledger.db').write_bytes(seed.read_bytes())

        source_before = {legacy: source_snapshot(legacy) for legacy in (legacy_a, legacy_b)}
        job_id = self.start(migrate, root, turns, legacy_a, roots=(legacy_a, legacy_b))
        self.quiet(migrate, root, turns, job_id)
        migrate.copy(root, turns, job_id)
        phase, copied = self.checkpoint(root, job_id)
        self.assertEqual(phase, 'copy')
        self.assertEqual(len(copied['rendered_receipts']), 2)
        sources = migrate.read_copied_sources(root, turns, job_id)
        self.assertEqual([source.root_uuid for source in sources], [ROOT_ID, other_root_id])
        mapped = store_import.plan_relays(sources)
        self.assertEqual([item.subject for item in mapped.relays], ['parent', 'reply'])
        self.assertEqual([len(item.provenance) for item in mapped.relays], [2, 1])
        self.assertEqual(mapped.relays[1].parent, 0)
        stage = self.job_path(root, job_id) / 'staging' / 'ledger.sqlite3'
        self.assertFalse(stage.parent.exists())

        self.assertTrue(callable(getattr(migrate, 'import_staging', None)),
                        'reached committed copy: missing store_migrate.import_staging')
        report = migrate.import_staging(root, turns, job_id)
        self.assertIsInstance(report, store_import.Report)
        self.assertEqual((report.store_id, report.project_id), (copied['store_id'], copied['project_id']))
        self.assertEqual(self.checkpoint(root, job_id)[0], 'import')
        self.assertTrue(stage.is_file())
        self.assertFalse(Path(str(stage) + '-wal').exists())
        self.assertFalse(Path(str(stage) + '-shm').exists())
        prepared = self.checkpoint(root, job_id)[1]['prepared']
        self.assertEqual((prepared['size'], prepared['sha256']),
                         (stage.stat().st_size, hashlib.sha256(stage.read_bytes()).hexdigest()))

        def rows(db, query):
            with closing(db.execute(query)) as cursor:
                return cursor.fetchall()

        with closing(sqlite3.connect(stage.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            stored = rows(db, 'SELECT id,subject,parent_id,needs,header_bytes,body FROM relays '
                              'JOIN bodies ON bodies.relay_id=relays.id ORDER BY handle')
            parent_id, reply_id = (row[0] for row in stored)
            first = expected[ROOT_ID, 1]
            second = expected[ROOT_ID, 2]
            self.assertEqual(stored, [
                (parent_id, 'parent', None, 'none', len(first[4]), first[5]),
                (reply_id, 'reply', parent_id, 'none', len(second[4]), second[5]),
            ])
            self.assertEqual(rows(db, 'SELECT from_relay_id,target_relay_id,spelling FROM edges'),
                             [(reply_id, parent_id, parent_path)])
            provenance = rows(db, 'SELECT relay_id,root_uuid,seq,rendered_path,origin,headers_json,'
                                  'source_sha256,delivery_marked_at FROM imported_relays ORDER BY root_uuid,seq')
            relay_ids = {'parent': parent_id, 'reply': reply_id}
            marked = copied['marked_at_ms']
            self.assertEqual([(rid, source, seq, path, origin, json.loads(headers), sha, marker)
                              for rid, source, seq, path, origin, headers, sha, marker in provenance],
                             [(relay_ids[name], source, seq, path, origin, headers, sha, marked)
                              for (source, seq), (name, path, origin, headers, _, _, sha)
                              in sorted(expected.items())])
            self.assertEqual(rows(db, 'SELECT r.subject,c.role,s.role,c.special,c.delivered_at,c.opened_at '
                                      'FROM recipients c JOIN relays r ON r.id=c.relay_id '
                                      'LEFT JOIN seats s ON s.id=c.seat_id ORDER BY r.handle,c.role'), [
                ('parent', 'cc', None, 'operator', None, marked),
                ('parent', 'to', 'implementer', None, marked, marked),
                ('reply', 'cc', None, 'operator', None, marked),
                ('reply', 'to', 'planner', None, marked, marked),
            ])
            for table in ('snapshots', 'verification_requests', 'slips', 'slip_events', 'seat_activity'):
                self.assertEqual(rows(db, f'SELECT count(*) FROM {table}'), [(0,)])
            with closing(sqlite3.connect(':memory:')) as control:
                db.backup(control)
                offered = ("SELECT relay_id FROM recipients WHERE role='to' "
                           "AND delivered_at IS NULL AND opened_at IS NULL ORDER BY relay_id")
                self.assertEqual(rows(control, offered), [])
                control.execute('UPDATE recipients SET delivered_at=NULL, opened_at=NULL').close()
                self.assertCountEqual(rows(control, offered), [(parent_id,), (reply_id,)])

        def private_catalogue(conns):
            db = conns['catalogue']
            self.assertEqual(store_layout.route_state(db, copied['project_id']), 'absent')
            self.assertEqual(rows(db, 'SELECT count(*) FROM maintenance_holds'), [(0,)])

        turns.run(store_steps.Declared('read', {}), private_catalogue)
        self.assertEqual({legacy: source_snapshot(legacy) for legacy in (legacy_a, legacy_b)}, source_before)

    def _copied_import_fixture(self, label, rows):
        import json

        from relay_engine.tests.test_store_import import LEGACY_DDL

        migrate = self.producer('copy')
        root, turns, legacy = self.fixture(label)
        seed = legacy.parent / 'import-controls.sqlite3'
        with closing(sqlite3.connect(seed)) as db, db:
            db.executescript(LEGACY_DDL).close()
            db.executemany('INSERT INTO meta VALUES (?, ?)', (
                ('schema_version', '1'), ('canonical_root', str(legacy)),
                ('root_uuid', ROOT_ID), ('run_id', 'fixture'),
            )).close()
            db.execute("INSERT INTO runs VALUES ('fixture', 'operator')").close()
            for seq, (name, sender, to, cc, references) in enumerate(rows, 1):
                path = f'relays/{name}.md'
                headers = {'PHASE': 'SITREP', 'FROM': sender, 'TO': to}
                if cc:
                    headers['CC'] = ', '.join(cc)
                if references:
                    headers['IN_REPLY_TO'] = ', '.join(f'relays/{ref}.md' for ref in references)
                fields = ''.join(f'{key}: {value}\n' for key, value in headers.items())
                document = f'# {name}\n\n{fields}\n{name} payload\n'.encode()
                digest = hashlib.sha256(document).hexdigest()
                (legacy / path).write_bytes(document)
                db.execute(
                    'INSERT INTO relays (seq,stamp,rendered_path,phase,role,dispatch_id,'
                    'run_id,from_seat,to_seats,cc_seats,subject,headers_json,body,'
                    'body_sha256,content_hash,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (seq, '20260801-120000', path, 'SITREP',
                     'Pair Planner' if sender == 'fixture.planner' else 'Pair Implementer',
                     name, 'fixture', sender, json.dumps([to]), json.dumps(cc), name,
                     json.dumps(headers), document, digest, digest, 'hand'),
                ).close()
        (legacy / '.engine' / 'ledger.db').write_bytes(seed.read_bytes())

        job_id = self.start(migrate, root, turns, legacy)
        self.quiet(migrate, root, turns, job_id)
        migrate.copy(root, turns, job_id)
        self.assertEqual(self.checkpoint(root, job_id)[0], 'copy')
        return migrate, root, turns, legacy, job_id

    def _assert_copy_retained_for_import(self, root, job_id, copied):
        attempt = {key: copied[key] for key in (
            'job_id', 'store_id', 'project_id', 'project_name', 'marked_at_ms',
            'rendered_receipts',
        )}
        attempt['decisions'] = copied.get('decisions', {})
        self.assertEqual(self.checkpoint(root, job_id),
                         ('copy', {**copied, 'import_attempt': attempt}))
        self.assertFalse((self.job_path(root, job_id) / 'staging').exists())
        return attempt

    def test_import_answers_rows(self):
        from relay_engine import imports

        planner, implementer = 'fixture.planner', 'fixture.implementer'
        rows = (
            ('parent', planner, implementer, ('operator',), ()),
            ('answered', planner, 'operator', (implementer,), ()),
            ('unrelated', planner, implementer, ('operator',), ()),
            ('first', implementer, planner, ('operator',), ('parent', 'answered')),
            ('second', implementer, planner, ('operator',), ('parent', 'answered')),
        )
        migrate, root, turns, _, job_id = self._copied_import_fixture('answers', rows)
        _, copied = self.checkpoint(root, job_id)
        marked = copied['marked_at_ms']
        ledger = self.job_path(root, job_id) / 'staging' / 'ledger.sqlite3'
        write_relay = imports.import_relay_step
        ids = {}

        def observe(conn, project_id, relay, *, marked_at_ms):
            if relay.subject in ('first', 'second'):
                self.assertEqual(relay.parent_id, ids['parent'])
                self.assertEqual(relay.answers, (ids['answered'],))
            relay_id = write_relay(conn, project_id, relay, marked_at_ms=marked_at_ms)
            ids[relay.subject] = relay_id
            return relay_id

        with mock.patch.object(imports, 'import_relay_step', side_effect=observe):
            report = migrate.import_staging(root, turns, job_id)
        self.assertEqual(report.unresolved(), ['team needs finished-or-running choice: fixture'])
        self.assertEqual(report.answers_listed(), [])
        self.assertEqual(self.checkpoint(root, job_id)[0], 'import')
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            with closing(db.execute('SELECT id,subject,parent_id FROM relays ORDER BY handle')) as cursor:
                self.assertEqual(cursor.fetchall(), [
                    (ids[name], name, ids['parent'] if name in ('first', 'second') else None)
                    for name, *_ in rows
                ])
            with closing(db.execute(
                    'SELECT r.subject,c.role,s.role,c.special,c.delivered_at,c.opened_at,c.answered_by '
                    'FROM recipients c JOIN relays r ON r.id=c.relay_id '
                    'LEFT JOIN seats s ON s.id=c.seat_id '
                    "WHERE r.subject IN ('parent','answered','unrelated') ORDER BY r.handle,c.role"
            )) as cursor:
                self.assertEqual(cursor.fetchall(), [
                    ('parent', 'cc', None, 'operator', None, marked, None),
                    ('parent', 'to', 'implementer', None, marked, marked, None),
                    ('answered', 'cc', 'implementer', None, None, marked, ids['first']),
                    ('answered', 'to', None, 'operator', marked, marked, None),
                    ('unrelated', 'cc', None, 'operator', None, marked, None),
                    ('unrelated', 'to', 'implementer', None, marked, marked, None),
                ])

    def test_import_step_refusal_discards_staging(self):
        from dataclasses import replace

        from relay_engine import identity_import, imports

        planner, implementer = 'fixture.planner', 'fixture.implementer'
        rows = (
            ('parent', planner, implementer, ('operator',), ()),
            ('denied', planner, 'operator', (), ()),
            ('reply', implementer, planner, ('operator',), ('parent', 'denied')),
        )
        write_relay = imports.import_relay_step
        write_identities = identity_import.import_identities_step

        for case in ('answer', 'identity', 'unrelated'):
            with self.subTest(case=case):
                migrate, root, turns, legacy, job_id = self._copied_import_fixture(case, rows)
                _, copied = self.checkpoint(root, job_id)
                job = self.job_path(root, job_id)
                original = source_snapshot(legacy)
                committed = source_snapshot(job / 'source')
                ids = {}

                def unfiltered(conn, project_id, relay, *, marked_at_ms):
                    if relay.subject == 'reply':
                        self.assertEqual(relay.answers, ())
                        relay = replace(relay, answers=(ids['denied'],))
                    relay_id = write_relay(conn, project_id, relay, marked_at_ms=marked_at_ms)
                    ids[relay.subject] = relay_id
                    return relay_id

                def wrong_top(conn, **fields):
                    self.assertIsNotNone(fields['top_seat_id'])
                    self.assertNotEqual(fields['top_seat_id'], fields['project_id'])
                    fields['top_seat_id'] = fields['project_id']
                    return write_identities(conn, **fields)

                def unexpected(conn, project_id, relay, *, marked_at_ms):
                    write_relay(conn, project_id, relay, marked_at_ms=marked_at_ms)
                    raise RuntimeError('unexpected fixture failure after a real relay step')

                if case == 'answer':
                    with mock.patch.object(imports, 'import_relay_step', side_effect=unfiltered):
                        report = migrate.import_staging(root, turns, job_id)
                    self.assertEqual(report.answers_listed(), [(2, 'relays/denied.md', 'not-received')])
                    self.assertIn(
                        'import refused: the import answers must name earlier relays of this project '
                        'that the sender holds and has opened, each once and none its parent',
                        report.unresolved(),
                    )
                elif case == 'identity':
                    with mock.patch.object(identity_import, 'import_identities_step', side_effect=wrong_top):
                        report = migrate.import_staging(root, turns, job_id)
                    self.assertIn(f"top_seat_mismatch: {copied['project_id']}", report.unresolved())
                else:
                    with mock.patch.object(imports, 'import_relay_step', side_effect=unexpected):
                        with self.assertRaisesRegex(RuntimeError, '^unexpected fixture failure after a real relay step$'):
                            migrate.import_staging(root, turns, job_id)

                attempt = self._assert_copy_retained_for_import(root, job_id, copied)
                self.assertEqual(source_snapshot(legacy), original)
                self.assertEqual(source_snapshot(job / 'source'), committed)
                recovered = migrate.import_staging(root, turns, job_id)
                phase, prepared = self.checkpoint(root, job_id)
                self.assertEqual(phase, 'import')
                self.assertEqual(prepared['import_attempt'], attempt)
                self.assertEqual((recovered.store_id, recovered.project_id),
                                 (attempt['store_id'], attempt['project_id']))
                self.assertEqual(source_snapshot(legacy), original)
                self.assertEqual(source_snapshot(job / 'source'), committed)

    def test_import_copied_receipt_mismatch_stops(self):
        row = ('parent', 'fixture.planner', 'fixture.implementer', ('operator',), ())
        migrate, root, turns, legacy, job_id = self._copied_import_fixture('receipt-mismatch', (row,))
        checkpoint = self.checkpoint(root, job_id)
        job = self.job_path(root, job_id)
        receipt = job / 'source' / '0' / 'rendered-receipt.json'
        original = source_snapshot(legacy)
        receipt.write_bytes(receipt.read_bytes() + b' ')
        damaged = source_snapshot(job / 'source')
        with self.assertRaises(migrate.CopiedSourceError) as failure:
            migrate.import_staging(root, turns, job_id)
        error = failure.exception
        self.assertEqual((error.reason, error.job_id, error.root_index, error.path),
                         ('copy-input-invalid', job_id, 0, receipt.name))
        self.assertIn(f'copy receipt digest differs from checkpoint: {receipt}', str(error))
        self.assertEqual(self.checkpoint(root, job_id), checkpoint)
        self.assertFalse((job / 'staging').exists())
        self.assertEqual(source_snapshot(legacy), original)
        self.assertEqual(source_snapshot(job / 'source'), damaged)

    def test_import_batch_restart_uses_fixed_inputs(self):
        import json

        from relay_engine import imports

        planner, implementer = 'fixture.planner', 'fixture.implementer'
        rows = (
            ('first', planner, implementer, ('operator',), ()),
            ('second', implementer, planner, ('operator',), ('first',)),
        )
        migrate, root, turns, legacy, job_id = self._copied_import_fixture('restart-batches', rows)
        _, copied = self.checkpoint(root, job_id)
        job = self.job_path(root, job_id)
        ledger = job / 'staging' / 'ledger.sqlite3'
        original = source_snapshot(legacy)
        committed = source_snapshot(job / 'source')
        marked = copied['marked_at_ms']
        write_relay = imports.import_relay_step

        def read_rows(db, query):
            with closing(db.execute(query)) as cursor:
                return cursor.fetchall()

        def provenance(db):
            return read_rows(db, 'SELECT r.subject,p.root_uuid,p.seq,p.rendered_path,p.source_sha256,'
                             'p.delivery_marked_at FROM imported_relays p '
                             'JOIN relays r ON r.id=p.relay_id ORDER BY p.seq')

        def semantic_digest(path):
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                relays = read_rows(db, 'SELECT r.handle,r.subject,p.subject,r.project_id,r.created_at,'
                                   'r.body_sha256,r.header_bytes,r.sender_role,hex(b.body) FROM relays r '
                                   'LEFT JOIN relays p ON p.id=r.parent_id '
                                   'JOIN bodies b ON b.relay_id=r.id ORDER BY r.handle')
                recipients = read_rows(
                    db, 'SELECT r.subject,c.role,s.role,c.special,c.delivered_at,c.opened_at '
                    'FROM recipients c JOIN relays r ON r.id=c.relay_id '
                    'LEFT JOIN seats s ON s.id=c.seat_id ORDER BY r.handle,c.role',
                )
                source_rows = provenance(db)
            self.assertEqual([(row[1], row[2], row[3]) for row in relays],
                             [('first', None, copied['project_id']),
                              ('second', 'first', copied['project_id'])])
            self.assertEqual([(row[0], row[1], row[2], row[3], row[5]) for row in source_rows], [
                ('first', ROOT_ID, 1, 'relays/first.md', marked),
                ('second', ROOT_ID, 2, 'relays/second.md', marked),
            ])
            self.assertEqual({opened for _, _, _, _, _, opened in recipients}, {marked})
            self.assertEqual({delivered for _, role, _, _, delivered, _ in recipients if role == 'to'},
                             {marked})
            value = json.dumps((relays, recipients, source_rows), separators=(',', ':')).encode()
            return hashlib.sha256(value).hexdigest()

        def stop_in_second_batch(conn, project_id, relay, *, marked_at_ms):
            if relay.subject == 'second':
                # A separate connection sees only data already committed by the writer.
                with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as db:
                    self.assertEqual(read_rows(db, 'SELECT subject FROM relays'), [('first',)])
                    self.assertEqual([(row[0], row[2], row[5]) for row in provenance(db)],
                                     [('first', 1, marked)])
                raise RuntimeError('stop after the first committed batch')
            return write_relay(conn, project_id, relay, marked_at_ms=marked_at_ms)

        expected = []

        def capture_before_prepare(path):
            self.assertEqual(path, ledger)
            expected.append(semantic_digest(path))
            raise RuntimeError('stop after both committed batches and before preparation')

        with mock.patch.object(migrate, '_IMPORT_BATCH_SIZE', 1):
            with mock.patch.object(imports, 'import_relay_step', side_effect=stop_in_second_batch):
                with self.assertRaisesRegex(RuntimeError, '^stop after the first committed batch$'):
                    migrate.import_staging(root, turns, job_id)
            attempt = self._assert_copy_retained_for_import(root, job_id, copied)
            self.assertEqual(source_snapshot(job / 'source'), committed)

            with mock.patch.object(store_steps, 'prepare_database', side_effect=capture_before_prepare):
                with self.assertRaisesRegex(RuntimeError, '^stop after both committed batches'):
                    migrate.import_staging(root, turns, job_id)
            self.assertEqual(self._assert_copy_retained_for_import(root, job_id, copied), attempt)
            self.assertEqual(len(expected), 1)

            earlier_ms = marked - 86_400_000
            with mock.patch.object(time, 'time', return_value=earlier_ms / 1_000), mock.patch.object(
                    time, 'time_ns', return_value=earlier_ms * 1_000_000):
                self.assertLess(time.time_ns() // 1_000_000, marked)
                report = migrate.import_staging(root, turns, job_id)

        self.assertEqual(report.unresolved(), ['team needs finished-or-running choice: fixture'])
        self.assertEqual((report.store_id, report.project_id), (attempt['store_id'], attempt['project_id']))
        self.assertEqual(semantic_digest(ledger), expected[0])
        receipt = {'size': ledger.stat().st_size, 'sha256': hashlib.sha256(ledger.read_bytes()).hexdigest()}
        self.assertEqual(self.checkpoint(root, job_id),
                         ('import', {**copied, 'import_attempt': attempt, 'prepared': receipt}))
        self.assertEqual(set(ledger.parent.iterdir()), {ledger})
        self.assertEqual(source_snapshot(legacy), original)
        self.assertEqual(source_snapshot(job / 'source'), committed)

    def _check_fixture(self, label):
        return self._copied_import_fixture(label, (
            ('first', 'fixture.planner', 'fixture.implementer', ('operator',), ()),
            ('second', 'fixture.implementer', 'fixture.planner', ('operator',), ('first',)),
        ))

    def _check_projection(self, job):
        ledger = job / 'staging' / 'ledger.sqlite3'
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            instances = conn.execute(
                'SELECT name, closed_at FROM team_instances ORDER BY name').fetchall()
            relays = conn.execute(
                'SELECT r.handle, r.subject, p.handle, b.body, r.header_bytes '
                'FROM relays r JOIN bodies b ON b.relay_id=r.id '
                'LEFT JOIN relays p ON p.id=r.parent_id ORDER BY r.handle').fetchall()
            provenance = conn.execute(
                'SELECT r.handle, i.root_uuid, i.seq, i.delivery_marked_at '
                'FROM imported_relays i JOIN relays r ON r.id=i.relay_id '
                'ORDER BY r.handle, i.root_uuid, i.seq').fetchall()
            parties = conn.execute(
                'SELECT r.handle, p.role, s.role, p.special, p.delivered_at, p.opened_at, a.handle '
                'FROM recipients p JOIN relays r ON r.id=p.relay_id '
                'LEFT JOIN seats s ON s.id=p.seat_id LEFT JOIN relays a ON a.id=p.answered_by '
                'ORDER BY 1, 2, 3, 4').fetchall()
        return instances, relays, provenance, parties

    def _semantic_digest(self, ledger: Path, planned: Sequence[store_import.PlannedRelay], *,
                         check_meta: bool = True) -> dict[str, list]:
        excluded = {('relays', 'id'), ('edges', 'id')}

        def quoted(identifier):
            return '"' + identifier.replace('"', '""') + '"'

        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = [row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
                if not row['name'].startswith('sqlite_')]
            relay_rows = db.execute('SELECT id, project_id, handle FROM relays').fetchall()
            self.assertEqual(sorted(row['handle'] for row in relay_rows),
                             list(range(1, len(planned) + 1)))
            relay_identities = {row['id']: (row['project_id'], row['handle']) for row in relay_rows}
            relay_ids = {identity: relay_id for relay_id, identity in relay_identities.items()}
            result = {}
            for table in tables:
                # The one-column relay references in this schema all point to relays.id.
                relay_references = set()
                for key in db.execute(f'PRAGMA foreign_key_list({quoted(table)})'):
                    if key['table'] == 'relays':
                        self.assertEqual(key['to'], 'id')
                        relay_references.add(key['from'])
                rows = []
                for row in db.execute(f'SELECT * FROM {quoted(table)}'):
                    columns = []
                    for column in sorted(row.keys()):
                        if (table, column) in excluded:
                            continue
                        value = row[column]
                        if column in relay_references and value is not None:
                            self.assertIn(value, relay_identities, f'{table}.{column}')
                            value = relay_identities[value]
                        elif (table, column) == ('relays', 'meta_sha256'):
                            project, handle = row['project_id'], row['handle']
                            relay = planned[handle - 1]
                            form = {
                                'import': '2.9.7',
                                'header_sha256': relay.header_sha256,
                                'parent': None if relay.parent is None else
                                relay_ids[project, relay.parent + 1],
                            }
                            if relay.answers:
                                form['answers'] = [relay_ids[project, pos + 1] for pos in relay.answers]
                            expected = hashlib.sha256(jcs.jcs_encode(form)).hexdigest()
                            if check_meta:
                                self.assertEqual(value, expected,
                                                 f'relays.meta_sha256 for {project}/{handle}')
                            value = ('meta', value == expected)
                        columns.append((column, value))
                    rows.append(tuple(columns))
                result[table] = sorted(rows, key=repr)
            return result

    def test_decide_records_and_rebuilds(self):
        migrate, root, turns, legacy, job_id = self._check_fixture('decide-rebuild')
        job = self.job_path(root, job_id)
        ledger = job / 'staging' / 'ledger.sqlite3'
        planned = store_import.plan_relays(migrate.read_copied_sources(root, turns, job_id)).relays
        migrate.import_staging(root, turns, job_id)
        marked = self.checkpoint(root, job_id)[1]['marked_at_ms']
        real_build = migrate._write_import_batches
        attempts = []

        def observe(conn, attempt, relays, identities):
            phase, durable = self.checkpoint(root, job_id)
            self.assertEqual(phase, 'copy')
            self.assertEqual(durable['decisions'], attempt['decisions'])
            self.assertEqual(durable['marked_at_ms'], marked)
            attempts.append(durable['decisions'])
            return real_build(conn, attempt, relays, identities)

        with mock.patch.object(migrate, '_write_import_batches', side_effect=observe):
            self.assertFalse(migrate.decide(root, turns, job_id, {'fixture': 'finished'}).unresolved())
            finished = self._check_projection(job)
            finished_semantic = self._semantic_digest(ledger, planned)
            self.assertFalse(migrate.decide(root, turns, job_id, {'fixture': 'finished'}).unresolved())
            self.assertEqual(self._check_projection(job), finished)
            self.assertEqual(self._semantic_digest(ledger, planned), finished_semantic)
            self.assertFalse(migrate.decide(root, turns, job_id, {'fixture': 'running'}).unresolved())
            running = self._check_projection(job)
        self.assertEqual(attempts, [{'fixture': 'finished'}, {'fixture': 'finished'}, {'fixture': 'running'}])
        self.assertEqual(running[1:], finished[1:])
        self.assertIsNotNone(dict(finished[0])['fixture'])
        self.assertIsNone(dict(running[0])['fixture'])
        self.assertEqual({row[-1] for row in running[2]}, {marked})

    def test_decide_refuses_drift(self):
        migrate, root, turns, legacy, job_id = self._check_fixture('decide-drift')
        job = self.job_path(root, job_id)
        migrate.import_staging(root, turns, job_id)
        migrate.decide(root, turns, job_id, {'fixture': 'finished'})
        before = source_snapshot(job)
        changed = legacy / 'relays' / 'second.md'
        changed.write_bytes(changed.read_bytes() + b'changed')
        with self.assertRaisesRegex(migrate.NotQuiet, 'relays/second[.]md'):
            migrate.decide(root, turns, job_id, {'fixture': 'running'})
        self.assertEqual(source_snapshot(job), before)

    def test_decide_refuses_link_drift(self):
        for relative in ('relays/second.md', 'relays'):
            with self.subTest(relative=relative):
                label = 'decide-link-' + relative.replace('/', '-')
                migrate, root, turns, legacy, job_id = self._check_fixture(label)
                job = self.job_path(root, job_id)
                migrate.import_staging(root, turns, job_id)
                self.assertFalse(migrate.decide(root, turns, job_id, {'fixture': 'finished'}).unresolved())
                before = source_snapshot(job)
                changed = legacy / relative
                retained = legacy.parent / 'retained'
                changed.rename(retained)
                changed.symlink_to(retained, target_is_directory=retained.is_dir())

                with self.assertRaises(migrate.NotQuiet) as refused:
                    migrate.decide(root, turns, job_id, {'fixture': 'running'})
                self.assertEqual(str(refused.exception), f'legacy source link: {changed}')
                self.assertIsInstance(refused.exception.__cause__, migrate.LegacySourceLink)
                self.assertEqual(source_snapshot(job), before)

    def test_validation_needs_resolved_items(self):
        migrate, root, turns, _, job_id = self._check_fixture('decide-validate')
        job = self.job_path(root, job_id)
        migrate.import_staging(root, turns, job_id)
        report = migrate.decide(root, turns, job_id, {})
        self.assertEqual(report.unresolved(), ['team needs finished-or-running choice: fixture'])
        self.assertTrue((job / 'staging' / 'ledger.sqlite3').is_file())
        self.assertTrue(any((job / 'report').iterdir()))
        report = migrate.decide(root, turns, job_id, {'fixture': 'running'})
        self.assertFalse(report.unresolved())
        for table in ('project_routes', 'maintenance_holds'):
            self.assertEqual(turns.run(store_steps.Declared('read', {}), lambda conns:
                conns['catalogue'].execute(f'SELECT count(*) FROM {table}').fetchone()), (0,))

    def test_imported_approve_issues_nothing(self):
        from relay_engine import authority, imports, slips
        from relay_engine.tests.test_slips import IMPL, ORCH, PLANNER, SlipStore, draft

        fixture = SlipStore()
        # Reuse Authority's real admission context and canonical test rows.
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        conn = fixture.conn
        raw_header = b'PHASE: PLAN-REVIEW\nVERDICT: approve\n'
        historic = imports.ImportedRelay(
            fixture.clock - 2, 'PLAN-REVIEW', 'approve', 'legacy approval',
            IMPL.seat_id, IMPL.role, IMPL.team_instance_id, (PLANNER.seat_id,), (),
            None, None, raw_header + b'\nLegacy approval.\n',
            len(raw_header), hashlib.sha256(raw_header).hexdigest(),
        )
        conn.execute('BEGIN IMMEDIATE').close()
        historical_id = imports.import_relay_step(conn, IMPL.project_id, historic,
                                                  marked_at_ms=fixture.clock - 1)
        conn.execute(
            'INSERT INTO imported_relays '
            '(relay_id,root_uuid,seq,stamp,zone,utc_offset_s,rendered_path,origin,'
            'headers_json,source_sha256,original_phase,foreign_to_json,delivery_marked_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (historical_id, '01000000-0000-4000-8000-000000000000', 1,
             '20261118-190000', 'UTC', 0, 'legacy/approval.md', 'engine', '{}',
             hashlib.sha256(raw_header).hexdigest(), 'PLAN-REVIEW', None,
             fixture.clock - 1),
        ).close()
        conn.commit()
        self.assertEqual(conn.execute('SELECT count(*) FROM imported_relays WHERE relay_id=?',
                                      (historical_id,)).fetchone(), (1,))
        self.assertEqual(conn.execute('SELECT needs FROM relays WHERE id=?',
                                      (historical_id,)).fetchone(), ('none',))
        self.assertEqual(conn.execute('SELECT count(*) FROM snapshots').fetchone(), (0,))
        self.assertEqual(conn.execute('SELECT count(*) FROM verification_requests').fetchone(), (0,))
        parent_line = b'PARENT: ' + historical_id.encode('utf-8') + b'\n'
        plan = fixture.submit(draft(kind='PLAN', needs='review', to=IMPL.address,
                                    cc=ORCH.address, parent=historical_id, bundle='b1',
                                    refs=('file:' + str(fixture.plan),)).replace(
                                        parent_line, parent_line + b'BUNDLE: b1\n', 1))
        self.assertIsNotNone(conn.execute('SELECT bundle_id FROM relays WHERE id=?', (plan.relay_id,)).fetchone()[0])
        refused = fixture.issue(plan, ORCH)
        self.assertIsNone(fixture.slip(refused))
        conn.execute('BEGIN IMMEDIATE').close()
        try:
            answer = authority.act_entry(conn, IMPL.seat_id, slips.Effect('commit', 'repo-1'),
                                         fixture.clock)
            self.assertEqual((answer.outcome, answer.code), ('deny', 'authority.lineage-incomplete'))
        finally:
            conn.rollback()
        approval = fixture.submit(draft(kind='PLAN-REVIEW', verdict='approve',
                                        parent=plan.relay_id, to=PLANNER.address,
                                        cc=fixture.issuers,
                                        refs=('file:' + str(fixture.plan),)), IMPL)
        fixture.verify(approval)
        fixture.issued(fixture.issue(approval, ORCH))

    def test_phase_crash_semantic_digest(self):
        migrate, root, turns, _, job_id = self._check_fixture('phase-restart')
        job = self.job_path(root, job_id)
        ledger = job / 'staging' / 'ledger.sqlite3'
        planned = store_import.plan_relays(migrate.read_copied_sources(root, turns, job_id)).relays
        self.maxDiff = None
        migrate.import_staging(root, turns, job_id)
        decisions = {'fixture': 'running'}
        self.assertFalse(migrate.decide(root, turns, job_id, decisions).unresolved())
        expected = self._semantic_digest(ledger, planned)
        marker = self.checkpoint(root, job_id)[1]['marked_at_ms']
        real_checkpoint = store_steps.Journal.checkpoint

        def generated_ids():
            with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                return tuple({row[0] for row in db.execute(f'SELECT id FROM {table}')}
                             for table in ('relays', 'edges'))

        expected_ids = generated_ids()
        rebuilt_with_new_ids = False
        for boundary in ('copy', 'import', 'check'):
            with self.subTest(boundary=boundary):
                hit = []

                def interrupt(journal, phase, data):
                    real_checkpoint(journal, phase, data)
                    if phase == boundary:
                        hit.append(phase)
                        raise InterruptedCopy(phase)

                with mock.patch.object(store_steps.Journal, 'checkpoint', interrupt):
                    with self.assertRaisesRegex(InterruptedCopy, boundary):
                        migrate.decide(root, turns, job_id, decisions)
                self.assertEqual(hit, [boundary])
                durable_phase, durable = self.checkpoint(root, job_id)
                self.assertEqual(durable_phase, boundary)
                with mock.patch.object(migrate, '_write_import_batches',
                                       wraps=migrate._write_import_batches) as writer:
                    if boundary in ('import', 'check'):
                        self.assertFalse(migrate.import_staging(root, turns, job_id).unresolved())
                        writer.assert_not_called()
                        self.assertEqual(hashlib.sha256(ledger.read_bytes()).hexdigest(),
                                         durable['prepared']['sha256'])
                        self.assertEqual(self._semantic_digest(ledger, planned), expected)
                        self.assertEqual(self.checkpoint(root, job_id), (boundary, durable))
                    if boundary != 'check':
                        self.assertFalse(migrate.decide(root, turns, job_id, decisions).unresolved())
                        writer.assert_called_once()
                        self.assertEqual(writer.call_args.args[1], durable['import_attempt'])
                self.assertEqual(self._semantic_digest(ledger, planned), expected)
                phase, resumed = self.checkpoint(root, job_id)
                self.assertEqual(phase, 'check')
                self.assertEqual(resumed['marked_at_ms'], marker)
                if boundary != 'check':
                    rebuilt_with_new_ids |= all(after != before for after, before in
                                               zip(generated_ids(), expected_ids, strict=True))
        self.assertTrue(rebuilt_with_new_ids)

        zero_hash = '0' * 64
        controls = (
            ('created_at', 'UPDATE relays SET created_at = (SELECT max(created_at) FROM relays) + 1 '
                           'WHERE handle = (SELECT max(handle) FROM relays)', ()),
            ('source_sha256', 'UPDATE imported_relays SET source_sha256 = ? WHERE rowid = '
                              '(SELECT min(rowid) FROM imported_relays)', (zero_hash,)),
            ('edge_spelling', "UPDATE edges SET spelling = spelling || 'x'", ()),
            ('top_seat', 'UPDATE projects SET top_seat_id = ? WHERE id = ?', ()),
            ('delivered_at', 'UPDATE recipients SET delivered_at = delivered_at + 1 WHERE rowid = '
                             "(SELECT min(rowid) FROM recipients WHERE role = 'to')", ()),
            ('meta_without_ids', 'UPDATE relays SET meta_sha256 = ? WHERE handle = 1', (zero_hash,)),
            ('meta_with_parent', 'UPDATE relays SET meta_sha256 = ? WHERE handle = 2', (zero_hash,)),
        )
        for name, statement, params in controls:
            with self.subTest(control=name), tempfile.TemporaryDirectory(dir=self.scratch) as directory:
                control_job = Path(directory)
                control_ledger = control_job / 'staging' / 'ledger.sqlite3'
                control_ledger.parent.mkdir()
                with (closing(sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as original,
                      closing(sqlite3.connect(control_ledger)) as changed):
                    original.backup(changed)
                    changed.execute('PRAGMA foreign_keys = ON').close()
                    if name == 'edge_spelling':
                        self.assertEqual(changed.execute('SELECT count(*) FROM edges').fetchone(), (1,))
                    if name == 'top_seat':
                        project, top = changed.execute('SELECT id, top_seat_id FROM projects').fetchone()
                        seats = dict(changed.execute(
                            'SELECT s.role, s.id FROM seats s JOIN team_instances t '
                            'ON t.id = s.team_instance_id WHERE t.project_id = ?', (project,)))
                        self.assertEqual(top, seats['planner'])
                        self.assertNotEqual(top, seats['implementer'])
                        params = seats['implementer'], project
                    with changed:
                        changed.execute(statement, params).close()
                        self.assertEqual(changed.execute('SELECT changes()').fetchone(), (1,))
                        if name == 'top_seat':
                            self.assertEqual(changed.execute(
                                'SELECT s.role FROM projects p JOIN seats s ON s.id = p.top_seat_id '
                                'JOIN team_instances t ON t.id = s.team_instance_id '
                                'WHERE p.id = ? AND t.project_id = p.id', (project,)).fetchone(),
                                ('implementer',))
                actual = self._semantic_digest(control_ledger, planned,
                                               check_meta=not name.startswith('meta_'))
                self.assertNotEqual(actual, expected)

    def test_store_id_binding(self):
        migrate, root, turns, _, job_id = self._check_fixture('phases-store-id')
        job = self.job_path(root, job_id)
        before = source_snapshot(job)
        with closing(sqlite3.connect(store_layout.catalogue_path(root))) as conn, conn:
            conn.execute('UPDATE catalogue_identity SET store_id=? WHERE id=1', (PRIOR_STORE,)).close()
        for phase, args in ((migrate.import_staging, ()), (migrate.decide, ({'fixture': 'running'},))):
            with self.subTest(phase=phase.__name__), mock.patch.object(
                    migrate, '_fingerprint', side_effect=AssertionError('orphan fingerprinted')):
                with self.assertRaises(migrate.JobOrphaned):
                    phase(root, turns, job_id, *args)
                self.assertEqual(source_snapshot(job), before)

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
