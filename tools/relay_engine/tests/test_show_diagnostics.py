"""Root discovery distinguishes missing roots from stopped writers."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import cli, client, errors
from relay_engine.tests.test_identity_matrix import RunningDaemon, cid_did, DRAFT
from relay_engine.tests.test_root_lint_corrections import cwd

RELAY = Path(__file__).resolve().parents[2] / 'relay'
COMMANDS = (
    ('daemon', 'stop'), ('submit', 'draft', '--key', 'key'), ('status',),
    ('seat', 'register', 'p.planner', '--role', 'Planner'),
    ('seat', 'replace', 'p.planner', '--role', 'Planner'),
    ('seat', 'declare', 'p.planner', '--role', 'Planner', '--key', 'key'),
    ('seat', 'stand-down', 'p.planner'), ('seat', 'show', 'p.planner'),
    ('roster',), ('commission', 'dispatch', '--child-run', 'child'),
    ('adopt-commission', 'record'), ('export-ruling', 'ruling', '--for-child', 'child'),
    ('adopt-ruling', 'bundle'), ('authority', 'check', 'relay', '--seat', 'p.implementer', '--action', 'implement'),
    ('show', 'missing'), ('render',), ('verify',), ('reconcile',),
    ('migrate', 'check'), ('migrate', 'cutover', 'receipt'), ('migrate', 'rollback', 'archive'),
)


class TestShowDiagnostics(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = self.base / 'repo'
        self.root = self.repo / 'nested/root'
        self.root.mkdir(parents=True)
        (self.repo / '.engine').mkdir()
        self.empty = self.base / 'empty'
        self.empty.mkdir()
        self.cid, did = cid_did()
        self.running = RunningDaemon(str(self.root), did, socket_name=str(self.base / 's')).start()
        self.addCleanup(self.running.stop)
        registration = client.request(str(self.root), 'seat.register', {
            'address': 'identity-matrix.pair.planner', 'role_word': 'Planner'}, cid=self.cid)
        self.rel = registration['boot_relay']
        self.key = registration['key_path']
        self.body = (self.root / self.rel).read_bytes()

    def run_cli(self, directory, *args):
        result = subprocess.run([sys.executable, str(RELAY), *args], cwd=directory,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode, result.stdout, result.stderr.decode()

    def hint(self, target):
        return (f'hint: {target} is inside the relay root {self.root}; pass {self.rel}, '
                f'with --root {self.root} or from a directory inside the root')

    def test_show_cases_a_to_g(self):
        repository_target = str((self.root / self.rel).relative_to(self.repo))
        for label, directory, target, explicit, code, hint in (
                ('A', self.repo, repository_target, False, 'E-NO-ROOT', True),
                ('B', self.repo, repository_target, True, 'E-PATH-ESCAPE', True),
                ('C', self.repo, self.rel, True, None, False),
                ('D', self.repo, str(self.root / self.rel), True, 'E-PATH-ESCAPE', True),
                ('E', self.repo, 'missing.md', True, 'E-PATH-ESCAPE', False),
                ('F', self.root, self.rel, False, None, False),
                ('G', self.empty, 'missing.md', False, 'E-NO-ROOT', False)):
            with self.subTest(case=label):
                args = ['show', target, '--body'] + (['--root', str(self.root)] if explicit else [])
                rc, out, err = self.run_cli(directory, *args)
                if code is None:
                    self.assertEqual((rc, out, err), (0, self.body, ''))
                else:
                    self.assertEqual(rc, 1)
                    self.assertEqual(out, b'')
                    self.assertTrue(err.startswith(code + '\n'), err)
                    if label == 'A':
                        self.assertIn(f'{self.repo} holds .engine/ but no relay record', err)
                    if code == 'E-PATH-ESCAPE':
                        self.assertIn(f'no filed relay is {target} under the root {self.root}', err)
                    self.assertEqual('hint:' in err, hint)
                    if hint:
                        self.assertIn(self.hint(self.root / self.rel), err)

    def test_hint_uses_the_client_working_directory(self):
        # CLI is a distinct process; the daemon remains in the suite's cwd.
        client_directory = self.base / 'client'
        client_directory.mkdir()
        alias = client_directory / 'visible-root'
        alias.symlink_to(self.root, target_is_directory=True)
        rc, _, err = self.run_cli(client_directory, 'show', 'visible-root/' + self.rel,
                                  '--root', str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn(self.hint(self.root / self.rel), err)

    def test_every_discovering_command_without_root(self):
        self.assertEqual(len(COMMANDS), 21)
        for directory in (self.empty, self.repo):
            for command in COMMANDS:
                with self.subTest(directory=directory, command=command):
                    rc, _, err = self.run_cli(directory, *command)
                    self.assertEqual(rc, 1)
                    self.assertTrue(err.startswith('E-NO-ROOT\n'), err)
                    self.assertNotIn('E-DAEMON-DOWN', err)
                    self.assertNotIn('unexpected error', err)
                    expected = (f'no relay root at or above {directory}' if directory == self.empty
                                else f'{directory} holds .engine/ but no relay record')
                    self.assertIn(expected, err)

    def test_root_given_answers_equal_base(self):
        self.running.stop()
        draft = self.root / '.engine/drafts/stopped.md'
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(DRAFT)
        for command in (('status',), ('submit', '.engine/drafts/stopped.md', '--key', self.key),
                        ('roster',), ('seat', 'show', 'identity-matrix.pair.planner')):
            with self.subTest(command=command):
                rc, _, err = self.run_cli(self.empty, *command, '--root', str(self.root))
                self.assertEqual(rc, 1)
                self.assertTrue(err.startswith('E-DAEMON-DOWN\n'), err)
        for root, expected in ((self.empty, 'E-ROOT-NOT-FOUND'), (self.repo, 'E-DAEMON-DOWN')):
            rc, _, err = self.run_cli(self.empty, 'status', '--root', str(root))
            self.assertEqual(rc, 1)
            self.assertTrue(err.startswith(expected + '\n'), err)

    def test_unsafe_values_render_as_rejected_and_omit_the_hint(self):
        for char in ('\u2028', '\x01'):
            with self.subTest(char=repr(char), field='directory'):
                directory = self.base / ('unsafe' + char)
                directory.mkdir()
                rc, _, err = self.run_cli(directory, 'show', 'missing')
                self.assertEqual(rc, 1)
                self.assertTrue(err.startswith('E-NO-ROOT\n'), err)
                self.assertIn('unrecognized-input (sha256:', err)
                self.assertNotIn('hint:', err)
                self.assertNotIn(char, err)
            for field in ('target', 'root'):
                with self.subTest(char=repr(char), field=field):
                    root = self.root if field == 'target' else self.base / ('root' + char)
                    (root / '.engine').mkdir(parents=True, exist_ok=True)
                    target = root / ('target' + (char if field == 'target' else ''))
                    target.write_text('existing file')
                    refusal = errors.error_for('E-PATH-ESCAPE', variant='relay-not-found',
                                               target=str(target), root=str(root))
                    self.assertIn('unrecognized-input (sha256:', refusal.cause)
                    err = io.StringIO()
                    with cwd(self.empty), patch.object(client, 'request', side_effect=client.RemoteError(refusal.as_dict())), contextlib.redirect_stderr(err):
                        rc = cli.main(['show', str(target), '--root', str(root)])
                    self.assertEqual(rc, 1)
                    self.assertNotIn('hint:', err.getvalue())
                    self.assertNotIn(char, err.getvalue())

    def test_hint_never_raises(self):
        target = self.root / 'loop'
        target.symlink_to('loop')
        rc, out, err = self.run_cli(self.empty, 'show', str(target), '--root', str(self.root))
        self.assertEqual((rc, out), (1, b''))
        self.assertTrue(err.startswith('E-PATH-ESCAPE\n'), err)
        self.assertNotIn('hint:', err)
        self.assertNotIn('unexpected error', err)
        resolve = Path.resolve
        def loop_error(path, *args, **kwargs):
            if path == target:
                raise RuntimeError('Symlink loop')
            return resolve(path, *args, **kwargs)
        err = io.StringIO()
        with cwd(self.empty), patch.object(Path, 'resolve', loop_error), contextlib.redirect_stderr(err):
            rc = cli.main(['show', str(target), '--root', str(self.root)])
        self.assertEqual(rc, 1)
        self.assertTrue(err.getvalue().startswith('E-PATH-ESCAPE\n'), err.getvalue())
        self.assertNotIn('hint:', err.getvalue())
        self.assertNotIn('unexpected error', err.getvalue())

    def test_hint_shows_non_ascii_paths_verbatim(self):
        root = self.base / '文档' / '根'
        root.mkdir(parents=True)
        _, did = cid_did()
        running = RunningDaemon(str(root), did, socket_name=str(self.base / 'unicode-s')).start()
        self.addCleanup(running.stop)
        target = root / '消息.md'
        target.write_text('not a filed relay')
        rc, out, err = self.run_cli(self.empty, 'show', str(target), '--root', str(root))
        self.assertEqual((rc, out), (1, b''))
        self.assertTrue(err.startswith('E-PATH-ESCAPE\n'), err)
        self.assertIn(f'hint: {target} is inside the relay root {root}; pass 消息.md, '
                      f'with --root {root} or from a directory inside the root', err)

    def test_no_hint_when_the_target_is_root_relative(self):
        (self.root / 'unfiled.md').write_text('not a filed relay')
        rc, out, err = self.run_cli(self.root, 'show', 'unfiled.md')
        self.assertEqual((rc, out), (1, b''))
        self.assertTrue(err.startswith('E-PATH-ESCAPE\n'), err)
        self.assertIn('no filed relay is unfiled.md', err)
        self.assertNotIn('hint:', err)

    def test_hint_guard_covers_path_resolution_only(self):
        # A rendering failure must reach main's unexpected-error handler.
        from relay_engine import strings
        emit = strings.emit
        def fail_hint(stream, key, *args, **kwargs):
            if key == 'show-root-hint':
                raise RuntimeError('hint rendering failed')
            return emit(stream, key, *args, **kwargs)
        err = io.StringIO()
        with cwd(self.repo), patch.object(strings, 'emit', side_effect=fail_hint), contextlib.redirect_stderr(err):
            rc = cli.main(['show', str(self.root / self.rel), '--root', str(self.root)])
        self.assertEqual(rc, 1)
        self.assertIn('E-PATH-ESCAPE', err.getvalue())
        self.assertIn('unexpected error', err.getvalue())
        self.assertLess(err.getvalue().index('E-PATH-ESCAPE'),
                        err.getvalue().index('unexpected error'))

    def test_root_resolve_runtime_error_omits_the_hint(self):
        target = self.root / 'unfiled.md'
        target.write_text('not a filed relay')
        resolve = Path.resolve
        root = Path(str(self.root))

        def loop_error(path, *args, **kwargs):
            if path == root:
                raise RuntimeError('Symlink loop')
            return resolve(path, *args, **kwargs)

        err = io.StringIO()
        with cwd(self.empty), patch.object(Path, 'resolve', loop_error):
            with contextlib.redirect_stderr(err):
                rc = cli.main(['show', str(target), '--root', str(self.root)])
        self.assertEqual(rc, 1)
        self.assertTrue(err.getvalue().startswith('E-PATH-ESCAPE\n'))
        self.assertNotIn('hint:', err.getvalue())
        self.assertNotIn('unexpected error', err.getvalue())


class TestItem11Diagnostics(unittest.TestCase):
    """Item 11 (DD-v3-engine § 10): typed diagnostics through the real CLI."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'root'
        self.root.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.cid, did = cid_did()
        self.running = RunningDaemon(str(self.root), did, socket_name=str(self.base / 's')).start()
        self.addCleanup(self.running.stop)

    def run_cli(self, *args, env=None):
        result = subprocess.run([sys.executable, str(RELAY), *args], cwd=self.base,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=60)
        return result.returncode, result.stdout.decode(), result.stderr.decode()

    def code(self, stderr):
        return stderr.splitlines()[0] if stderr else ''

    def assert_ready(self):
        returncode, stdout, stderr = self.run_cli('status', '--root', str(self.root))
        self.assertEqual((returncode, stderr), (0, ''))
        self.assertEqual(json.loads(stdout)['daemon']['state'], 'ready')

    def declare_from(self, path):
        return self.run_cli('seat', 'declare', '--from-file', str(path), '--key', 'k',
                            '--root', str(self.root))

    def test_from_file_missing(self):
        missing = self.base / 'missing.txt'
        returncode, _, stderr = self.declare_from(missing)
        self.assertEqual((returncode, self.code(stderr)), (1, 'E-FILE-MISSING'))
        self.assertIn(str(missing), stderr)
        self.assertIn('no such file', stderr)
        # positive control: a readable file gets past the read to a later rule
        present = self.base / 'present.txt'
        present.write_text('p.planner Planner\n')
        returncode, _, stderr = self.declare_from(present)
        self.assertNotEqual(self.code(stderr), 'E-FILE-MISSING')

    def test_from_file_directory(self):
        returncode, _, stderr = self.declare_from(self.outside)
        self.assertEqual((returncode, self.code(stderr)), (1, 'E-FILE-MISSING'))
        self.assertIn(str(self.outside), stderr)
        self.assertIn('directory', stderr)

    def test_root_outside(self):
        returncode, _, stderr = self.run_cli('status', '--root', str(self.outside))
        self.assertEqual((returncode, self.code(stderr)), (1, 'E-ROOT-NOT-FOUND'))
        self.assertIn(str(self.outside), stderr)
        self.assertNotIn('unexpected error', stderr)
        self.assert_ready()  # positive control: the served root answers

    def test_corrupt_carrier(self):
        carrier = self.base / 'corrupt.bundle'
        carrier.write_bytes(b'not a record')
        returncode, _, stderr = self.run_cli('adopt-ruling', str(carrier), '--root', str(self.root))
        self.assertEqual((returncode, self.code(stderr)), (1, 'E-CARRIER-INVALID'))
        self.assertNotIn('E-DAEMON-DOWN', stderr)
        self.assert_ready()

    def test_wrong_field_carrier(self):
        from relay_engine.jcs import frame_record
        carrier = self.base / 'wrong.bundle'
        carrier.write_bytes(frame_record({'v': 1, 'x': 1}))
        returncode, _, stderr = self.run_cli('adopt-ruling', str(carrier), '--root', str(self.root))
        self.assertEqual((returncode, self.code(stderr)), (1, 'E-CARRIER-FIELD'))
        self.assert_ready()


class TestItem11Fold(TestItem11Diagnostics):
    """PR-4 fold D3-D7: every unusable --root or file argument gets its typed code."""

    def assert_typed(self, result, code, *phrases):
        returncode, _, stderr = result
        self.assertEqual((returncode, self.code(stderr)), (1, code), stderr)
        self.assertNotIn('unexpected error', stderr)
        for phrase in phrases:
            self.assertIn(phrase, stderr)

    def locked(self, path):
        path.mkdir()
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o700)
        return path

    def test_root_failures(self):  # D3
        regular = self.base / 'file.txt'
        regular.write_text('x\n')
        daemon_json = self.root / '.engine' / 'daemon.json'
        self.assertTrue(daemon_json.is_file())
        os.symlink('loop', self.base / 'loop')
        cases = [(regular, 'not a directory'), (daemon_json, 'not a directory'),
                 (self.base / 'loop', 'too many levels of symbolic links'),
                 (self.base / ('x' * 300), 'file name too long')]
        if os.geteuid() != 0:
            cases.append((self.locked(self.base / 'locked') / 'inner', 'permission denied'))
        for path, reason in cases:
            with self.subTest(reason=reason):
                self.assert_typed(self.run_cli('status', '--root', str(path)),
                                  'E-ROOT-NOT-FOUND', reason)
        self.assert_typed(self.run_cli('status', '--root', str(self.outside)),
                          'E-ROOT-NOT-FOUND', str(self.outside),
                          'no directory at or above it holds .engine/')
        self.assert_ready()

    def carriers(self):
        os.mkfifo(self.base / 'fifo')
        target = self.base / 'real.bundle'
        target.write_bytes(b'x')
        os.symlink(target, self.base / 'link.bundle')
        return ((self.base / 'missing.bundle', 'no such file'),
                (self.outside, 'a directory'),
                (self.base / 'fifo', 'a FIFO'),
                (self.base / 'link.bundle', 'a symlink'))

    def test_carrier_file_arguments(self):  # D4, D5
        cases = self.carriers()
        for path, reason in cases:
            for command in ('adopt-ruling', 'adopt-commission'):
                with self.subTest(command=command, reason=reason):
                    self.assert_typed(self.run_cli(command, str(path), '--root', str(self.root)),
                                      'E-FILE-MISSING', str(path), reason)
        fresh = self.base / 'fresh'
        fresh.mkdir()
        for path, reason in cases:
            with self.subTest(command='daemon start', reason=reason):
                self.assert_typed(self.run_cli('daemon', 'start', '--root', str(fresh),
                                               '--commissioned-by', str(path)),
                                  'E-FILE-MISSING', str(path), reason)
        self.assertFalse((fresh / '.engine').exists())  # refused before any launch
        self.assert_ready()

    def test_from_file_reasons(self):  # D5, D7
        os.mkfifo(self.base / 'fifo')
        latin = self.base / 'latin.txt'
        latin.write_bytes(b'p.planner Planner caf\xe9\n')
        cases = [(self.base / 'fifo', 'a FIFO'), (latin, 'not UTF-8 text')]
        if os.geteuid() != 0:
            denied = self.base / 'denied.txt'
            denied.write_text('p.planner Planner\n')
            os.chmod(denied, 0)
            self.addCleanup(os.chmod, denied, 0o600)
            cases.append((denied, 'permission denied'))
        for path, reason in cases:
            with self.subTest(reason=reason):
                self.assert_typed(self.declare_from(path), 'E-FILE-MISSING', str(path), reason)
        # a relative argument is shown as the absolute path it names (M6)
        self.assert_typed(self.run_cli('seat', 'declare', '--from-file', 'gone.txt', '--key', 'k',
                                       '--root', str(self.root)),
                          'E-FILE-MISSING', str(self.base / 'gone.txt'), 'no such file')

    def test_from_file_utf8_under_any_locale(self):  # D6, gate G-E1
        """--from-file is UTF-8 whatever the locale.  Two files, because which one catches a
        reader that decodes with the locale's encoding depends on that encoding: under
        ISO-8859-1 (the mac leg) such a reader accepts the Latin-1 file; under ASCII (the linux
        leg, which lacks en_US.ISO8859-1) it refuses the UTF-8 file."""
        latin = self.base / 'latin.txt'
        latin.write_bytes(b'p.planner Planner caf\xe9\n')
        utf8 = self.base / 'utf8.txt'
        utf8.write_bytes(b'p.planner Planner caf\xc3\xa9\n')
        env = dict(os.environ, LC_ALL='en_US.ISO8859-1', LANG='en_US.ISO8859-1',
                   PYTHONUTF8='0')
        env.pop('PYTHONIOENCODING', None)
        probe = subprocess.run([sys.executable, '-c',
                                'import locale; print(locale.getpreferredencoding(False))'],
                               env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        encoding = probe.stdout.strip()
        self.assertNotEqual(encoding.upper().replace('-', ''), 'UTF8',
                            'the control needs a locale whose preferred encoding is not UTF-8;'
                            ' got %r' % encoding)
        self.assert_typed(self.run_cli('seat', 'declare', '--from-file', str(latin), '--key', 'k',
                                       '--root', str(self.root), env=env),
                          'E-FILE-MISSING', 'not UTF-8 text')
        # the UTF-8 file gets past the read to a later rule
        returncode, _, stderr = self.run_cli('seat', 'declare', '--from-file', str(utf8),
                                             '--key', 'k', '--root', str(self.root), env=env)
        self.assertNotEqual(self.code(stderr), 'E-FILE-MISSING', stderr)
        self.assertNotIn('unexpected error', stderr)
