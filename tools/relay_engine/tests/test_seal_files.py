"""Temporary-root witnesses for the seal folder's names and non-slot I/O."""
import gc
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
C = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
D = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


def run_captured(argv):
    from relay_engine.seal_types import RunResult
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            check=False, timeout=30)
    if result.returncode and any(word in result.stderr.lower() for word in
                                 (b'password', b'permission', b'not permitted', b'full disk access')):
        raise RuntimeError('STOP: tmutil requested permission; no answer or retry')
    return RunResult(result.returncode, result.stdout)


class SealFilesTests(unittest.TestCase):
    def setUp(self):
        from relay_engine import seal_files, seal_slots
        self.f, self.s = seal_files, seal_slots
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'seal'
        self.folder = self.f.SealFolder(self.root)

    def directory(self, project=P):
        fd = self.folder.project_dir(project)
        os.close(fd)
        return self.root / 'projects' / project

    def counter(self, chain=C, project=P, suffix='counter'):
        with self.folder.lock(project, exclusive=True):
            fd = self.folder.project_dir(project)
            try:
                item = self.s.create(fd, chain + '.' + suffix,
                    kind=self.s.ItemKind.COUNTER if suffix == 'counter' else self.s.ItemKind.HIGH_WATER,
                    binding=(project, chain), value=0, identity_from=None)
                os.close(item)
            finally:
                os.close(fd)

    def entry(self, chain=C, project=P, number=1):
        with self.folder.lock(project, exclusive=True):
            self.folder.write_entry(project, chain, number, b'entry')

    def mark(self, chain=C, project=P, data=b'first'):
        with self.folder.lock(project, exclusive=True):
            self.folder.write_mark(project, chain, data)

    def test_record_head_unlistable_is_unreadable(self):
        from relay_engine.seal_types import RecordUnreadable
        records = self.directory() / 'records'
        records.mkdir(exist_ok=True)
        (records / C).write_bytes(b'not a directory')
        with self.assertRaises(RecordUnreadable) as caught:
            self.folder.record_head(P, C)
        self.assertEqual(caught.exception.cause, 'not_regular')
        with patch.object(self.f.os, 'listdir', side_effect=PermissionError('fixture')):
            with self.assertRaises(RecordUnreadable) as caught:
                self.folder.record_head(P, C)
        self.assertEqual(caught.exception.cause, 'io')
        self.assertIsNone(self.folder.record_head(P, D))

    def snapshot(self):
        if not self.root.exists():
            return None
        return {str(p.relative_to(self.root)): (p.stat().st_ino, p.stat().st_size,
                p.read_bytes() if p.is_file() else None) for p in self.root.rglob('*')}


    def test_created_directories_synced(self):
        parent = Path(self.temp.name) / 'missing-parent'
        self.root = parent / 'seal'; self.folder.root = self.root
        events = []
        real_mkdir, real_sync = Path.mkdir, self.s._sync
        emitted = set()
        def mkdir(path, *args, **kwargs):
            existed = path.exists()
            result = real_mkdir(path, *args, **kwargs)
            if not existed and path not in emitted:
                emitted.add(path)
                events.append(('mkdir', Path(path)))
            return result
        def sync(fd, **kwargs):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                events.append(('sync', info.st_dev, info.st_ino, kwargs))
            return real_sync(fd, **kwargs)
        with patch.object(Path, 'mkdir', mkdir), patch.object(self.s, '_sync', side_effect=sync):
            os.close(self.folder.project_dir(P))
            self.folder.publish_record(P, C, 0, b'record')
            self.folder.create_key()
        created = [parent, self.root, self.root / 'projects',
                   self.root / 'projects' / P,
                   self.root / 'projects' / P / 'records',
                   self.root / 'projects' / P / 'records' / C,
                   self.root / 'keys']
        self.assertEqual([x[1] for x in events if x[0] == 'mkdir'], created)
        for directory in created:
            index = events.index(('mkdir', directory))
            info = directory.parent.stat()
            self.assertEqual(events[index + 1],
                             ('sync', info.st_dev, info.st_ino, {'changing_length': True}))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sum(x[0] == 'sync' for x in events), 7)
        # Existing levels cause no directory durability work.
        events.clear()
        with patch.object(self.s, '_sync', side_effect=sync):
            os.close(self.folder.project_dir(P))
        self.assertEqual(events, [])

    def test_folder_state_absent(self):
        self.assertEqual(self.folder.folder_state(P), 'absent')
        self.assertFalse(self.root.exists())
        directory = self.directory()
        for name in ('lock', '.creating-' + C + '.counter.left', 'CACHEDIR.TAG', '.DS_Store'):
            (directory / name).write_bytes(b'ignored')
        self.assertEqual(self.folder.folder_state(P), 'absent')

    def test_folder_state_other_project(self):
        self.counter(project=Q)
        self.assertEqual(self.folder.folder_state(P), 'absent')
        self.assertEqual(self.folder.folder_state(Q), 'present')

    def test_folder_state_final_name_present(self):
        self.counter()
        self.assertEqual(self.folder.folder_state(P), 'present')
        self.assertFalse((self.root / 'projects' / P / 'pointer').exists())

    def test_folder_state_writes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.folder.folder_state(P), 'absent')
        self.assertEqual(self.snapshot(), before)
        self.counter(project=Q)
        for project, expected in ((P, 'absent'), (Q, 'present')):
            before = self.snapshot()
            self.assertEqual(self.folder.folder_state(project), expected)
            self.assertEqual(self.snapshot(), before)
        (self.root / 'projects' / Q / (C + '.counter')).write_bytes(b'damaged')
        before = self.snapshot()
        with patch.object(self.f.os, 'open', side_effect=AssertionError('opened an item')):
            self.assertEqual(self.folder.folder_state(Q), 'present')
        self.assertEqual(self.snapshot(), before)

    def test_chains_empty_and_missing(self):
        self.assertEqual(self.folder.chains(P), ())
        self.assertFalse(self.root.exists())
        directory = self.directory()
        (directory / 'lock').touch()
        self.assertEqual(self.folder.chains(P), ())

    def test_chains_lists_every_chain(self):
        self.counter(); self.counter(suffix='high-water')
        self.entry(number=1); self.entry(number=2); self.counter(chain=D)
        self.assertEqual(self.folder.chains(P), tuple(sorted((C, D))))

    def test_chains_lists_each_kind_alone(self):
        for kind in ('counter', 'high-water', 'entry', 'broken'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                original = self.folder
                self.folder = self.f.SealFolder(Path(root) / 'seal')
                try:
                    if kind in ('counter', 'high-water'):
                        self.counter(suffix=kind)
                    elif kind == 'entry':
                        self.entry()
                    else:
                        self.mark()
                    self.assertEqual(self.folder.chains(P), (C,))
                finally:
                    self.folder = original

    def test_chains_lists_damaged_and_partial(self):
        self.counter(suffix='high-water')
        (self.root / 'projects' / P / (C + '.high-water')).write_bytes(bytes(256))
        self.entry(chain=D)
        other = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
        self.mark(chain=other)
        with patch.object(self.f.os, 'open', side_effect=AssertionError('opened an item')):
            self.assertEqual(self.folder.chains(P), tuple(sorted((C, D, other))))

    def test_chains_skips_creating_and_foreign(self):
        self.counter(project=Q)
        directory = self.directory()
        for name in ('.creating-' + C + '.counter.left', C + '.entry.01', C + '.unknown',
                     'CACHEDIR.TAG', '.DS_Store', 'pointer', 'lock'):
            (directory / name).write_bytes(b'ignored')
        before = self.snapshot()
        self.assertEqual(self.folder.chains(P), ())
        self.assertEqual(self.snapshot(), before)

    def test_chains_lists_records_directory(self):
        with self.folder.lock(P, exclusive=True):
            self.folder.publish_record(P, C, 0, b'record')
        before = self.snapshot()
        self.assertEqual(self.folder.chains(P), (C,))
        self.assertEqual(self.folder.folder_state(P), 'present')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.folder.open_record_file(P, C, 0), b'record')

    def test_key_file_usability(self):
        key = self.folder.create_key()
        self.assertEqual(key.seq, 1)
        directory = self.root / 'keys'
        raw = (directory / '1').read_bytes()
        self.assertEqual(key.key_id, hashlib.sha256(raw).hexdigest()[:32])
        self.assertNotIn(raw.hex(), repr(key))
        self.assertEqual(len(raw), 32)
        for name, data, mode in (('2', b'x' * 32, 0o640), ('3', b'x' * 31, 0o600),
                                 ('01', b'x' * 32, 0o600), ('1a', b'x' * 32, 0o600)):
            p = directory / name; p.write_bytes(data); p.chmod(mode)
        (directory / '4').symlink_to(directory / '1')
        self.assertEqual([k.seq for k in self.folder.usable_keys()], [1])
        real_fstat = os.fstat
        def other_owner(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=info.st_mode,
                                   st_size=info.st_size)
        with patch.object(self.f.os, 'fstat', side_effect=other_owner):
            self.assertEqual(self.folder.usable_keys(), [])
        self.assertEqual((directory / '2').stat().st_size, 32)  # Length alone is not enough.
        self.assertEqual(len(self.f.mac(key, b'context', b'data')), 64)
        retained = self.folder.usable_keys()[0]
        del key
        gc.collect()
        self.assertEqual(len(self.f.mac(retained, b'context', b'data')), 64)
        with tempfile.TemporaryDirectory() as root:
            raced = self.f.SealFolder(Path(root) / 'seal')
            def winner(src, dst, **kwargs):
                destination = Path(root) / 'seal' / 'keys' / dst
                destination.write_bytes(b'w' * 32)
                destination.chmod(0o600)
                raise FileExistsError('another creator won')
            with patch.object(self.f.os, 'link', side_effect=winner):
                found = raced.create_key()
            self.assertEqual(found.seq, 1)
            self.assertEqual(found.key_id, hashlib.sha256(b'w' * 32).hexdigest()[:32])
            self.assertEqual(sorted(p.name for p in (Path(root) / 'seal' / 'keys').iterdir()), ['1'])

    def test_mark_file_first_wins(self):
        self.mark(data=b'bad-checksum-first')
        path = self.root / 'projects' / P / (C + '.broken')
        original = (path.stat().st_ino, path.read_bytes())
        self.mark(data=b'another-cause')
        self.assertEqual((path.stat().st_ino, path.read_bytes()), original)
        self.assertTrue(self.folder.mark_exists(P, C))
        self.entry(number=1)
        entry = self.root / 'projects' / P / (C + '.entry.1')
        inode = entry.stat().st_ino
        with self.folder.lock(P, exclusive=True):
            self.folder.write_entry(P, C, 1, b'rewritten')
            self.assertEqual(self.folder.read_entries(P, C, after=0), ((1, b'rewritten'),))
        self.assertEqual(entry.stat().st_ino, inode)

    def test_apply_backup_exclusion_per_platform(self):
        from relay_engine.seal_types import RunResult
        self.directory()
        if sys.platform == 'darwin':
            cases = [(1, 0, b'', 'add_failed', 1), (0, 1, b'', 'check_failed', 2),
                     (0, 0, b'[Included] ' + os.fsencode(self.root), 'not_excluded', 2),
                     (0, 0, b'', 'not_excluded', 2),
                     (0, 0, b'[Excluded] ' + os.fsencode(self.root), 'excluded', 2)]
            for add, check, data, expected, count in cases:
                calls = []
                def run(argv):
                    calls.append(argv)
                    return RunResult(add if len(calls) == 1 else check,
                                     b'' if len(calls) == 1 else data)
                self.assertEqual(self.folder.apply_backup_exclusion(run), expected)
                self.assertEqual(calls, [['tmutil', 'addexclusion', str(self.root)],
                    ['tmutil', 'isexcluded', str(self.root)]][:count])
            self.assertEqual(self.folder.apply_backup_exclusion(run_captured), 'excluded')
        else:
            run = unittest.mock.Mock(side_effect=AssertionError('launched a process'))
            self.assertEqual(self.folder.apply_backup_exclusion(run), 'excluded')
            tag = self.root / 'CACHEDIR.TAG'
            self.assertTrue(tag.read_bytes().startswith(b'Signature: 8a477f597d28d172789f06886806bc55\n'))
            with patch.object(self.f.os, 'pwrite', side_effect=OSError('full')):
                self.assertEqual(self.folder.apply_backup_exclusion(run), 'add_failed')
            with patch.object(self.f.os, 'read', side_effect=OSError('unreadable')):
                self.assertEqual(self.folder.apply_backup_exclusion(run), 'check_failed')
            tag.write_bytes(b'other')
            self.assertEqual(self.folder.apply_backup_exclusion(run), 'not_excluded')
            run.assert_not_called()

    def test_seal_files_launches_nothing(self):
        from relay_engine.tests.test_strings_gate import gate_violations
        self.assertEqual(gate_violations([Path(self.f.__file__)]), [])
        bad = Path(self.temp.name) / 'seal_files.py'
        bad.write_text(Path(self.f.__file__).read_text() + '\nimport subprocess\n')
        self.assertTrue(gate_violations([bad]))

    def test_noncanonical_ids_refused_before_folder(self):
        calls = [lambda p: self.folder.project_dir(p),
                 lambda p: self.folder.lock(p, exclusive=True).__enter__(),
                 lambda p: self.folder.folder_state(p), lambda p: self.folder.chains(p),
                 lambda p: self.folder.write_entry(p, C, 1, b'x'),
                 lambda p: self.folder.read_entries(p, C),
                 lambda p: self.folder.write_mark(p, C, b'x'),
                 lambda p: self.folder.mark_exists(p, C),
                 lambda p: self.folder.publish_record(p, C, 0, b'x'),
                 lambda p: self.folder.open_record_file(p, C, 0),
                 lambda p: self.folder.record_head(p, C),
                 lambda c: self.folder.write_entry(P, c, 1, b'x'),
                 lambda c: self.folder.read_entries(P, c),
                 lambda c: self.folder.write_mark(P, c, b'x'),
                 lambda c: self.folder.mark_exists(P, c),
                 lambda c: self.folder.publish_record(P, c, 0, b'x'),
                 lambda c: self.folder.open_record_file(P, c, 0),
                 lambda c: self.folder.record_head(P, c)]
        with patch.object(self.f.os, 'open', side_effect=AssertionError('opened before validation')), \
                patch.object(self.f.os, 'listdir', side_effect=AssertionError('listed before validation')):
            for bad in (P.upper(), '{' + P + '}', ' ' + P + ' '):
                for call in calls:
                    with self.subTest(value=bad, call=call), self.assertRaises(ValueError):
                        call(bad)
        self.assertFalse(self.root.exists())
