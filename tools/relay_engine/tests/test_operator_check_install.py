import base64
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from relay_engine import operator_check_install as install


class OperatorCheckInstallTests(unittest.TestCase):
    def test_install_files_exact(self):
        files = install.install_plan('linux', b'#!/usr/bin/python3 -IS\nprint(0)\n', '/usr/local/bin/python3')
        self.assertEqual(len(files), 7)
        helper = next(member for member in files if member.path == '/opt/adt-operator/operator-check')
        self.assertEqual(helper.mode, 0o755)
        self.assertEqual(helper.content, b'#!/usr/local/bin/python3 -IS\nprint(0)\n')

    def test_installer_transport_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            files = install.install_plan('linux', b'#!/usr/bin/python3 -IS\n# "quoted"\n', sys.executable)
            payload = install.invocation_payload('linux', sys.executable, 'operator', files,
                                                 root=directory, owner_uid=os.geteuid())
            result = subprocess.run([sys.executable, '-IS', '-c', install.INSTALLER_SOURCE, 'decode'],
                                    input=payload, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)['outcome'], 'decoded')
            self.assertEqual(list(Path(directory).iterdir()), [])
            decoded = json.loads(payload)
            helper = next(member for member in decoded['files'] if member['path'].endswith('/operator-check'))
            self.assertIn(b'# "quoted"', base64.b64decode(helper['content_b64']))

    def test_installer_envelope_types_refused(self):
        result = subprocess.run([sys.executable, '-IS', '-c', install.INSTALLER_SOURCE, 'decode'],
                                input=b'{"v":true}', capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(json.loads(result.stdout)['outcome'], 'decoded')

    def fixture(self, directory, *, permitted=True):
        root = Path(directory)
        for member in ('opt', 'etc/sudoers.d', 'etc/pam.d', 'usr/bin', 'usr/sbin'):
            (root / member).mkdir(parents=True)
        (root / 'etc/sudoers').write_text('@includedir /etc/sudoers.d\n')
        programs = {
            'usr/bin/sudo': ('print("/opt/adt-operator/operator-check")\n' if permitted else 'raise SystemExit(1)\n'),
            'usr/bin/ssh-keygen': '''import pathlib, sys
key = pathlib.Path(sys.argv[-1])
assert not key.exists() and not key.with_suffix(".pub").exists()
key.write_text("test private")
key.with_suffix(".pub").write_text("test public")
''',
            'usr/sbin/visudo': 'import pathlib, sys\nassert pathlib.Path(sys.argv[-1]).is_file()\n',
        }
        for name, program in programs.items():
            path = root / name
            path.write_text(f'#!{sys.executable}\n' + program)
            path.chmod(0o755)
        files = install.install_plan('linux', b'#!/usr/bin/python3 -IS\nprint(0)\n', sys.executable)
        payload = install.invocation_payload('linux', sys.executable, 'operator', files,
                                             root=directory, owner_uid=os.geteuid())

        def invoke(action):
            return subprocess.run([sys.executable, '-IS', '-c', install.INSTALLER_SOURCE, action],
                                  input=payload, capture_output=True, check=False)

        return root, files, invoke

    def test_install_replaces_staging_and_uninstall_preserves_the_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root, files, invoke = self.fixture(directory)
            initial = sorted(path.relative_to(root) for path in root.rglob('*'))
            result = invoke('install')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for member in files:
                path = root / member.path.lstrip('/')
                self.assertEqual(path.stat().st_mode & 0o777, member.mode)
                if member.content is not None:
                    self.assertEqual(path.read_bytes(), member.content)
            stage = root / 'opt/adt-operator/key.adt-tmp'
            stage.mkdir(mode=0o700)
            (stage / 'operator-check').write_text('leftover')
            (stage / 'operator-check.pub').write_text('leftover')
            result = invoke('install')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(stage.exists())
            result = invoke('uninstall')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sorted(path.relative_to(root) for path in root.rglob('*')), initial)

    def test_denied_permission_with_no_helper_removes_only_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, invoke = self.fixture(directory, permitted=False)
            check = root / 'opt/adt-operator'
            check.mkdir()
            marker = check / 'operator-check.pub'
            marker.write_text('previous')
            result = invoke('install')
            report = json.loads(result.stdout)
            self.assertEqual(report['outcome'], 'not_permitted')
            self.assertEqual(marker.read_text(), 'previous')
            self.assertEqual(sorted(item.name for item in check.iterdir()), ['operator-check.pub'])

    def test_unknown_child_refused_before_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, invoke = self.fixture(directory)
            result = invoke('install')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            check = root / 'opt/adt-operator'
            (check / 'unknown').touch()
            initial = sorted(path.relative_to(root) for path in root.rglob('*'))
            for action in ('install', 'uninstall'):
                result = invoke(action)
                self.assertEqual(json.loads(result.stdout)['outcome'], 'operator_check_unsafe_path')
                self.assertEqual(sorted(path.relative_to(root) for path in root.rglob('*')), initial)

    def test_concurrent_installer_and_uninstaller_leave_an_in_progress_install_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, invoke = self.fixture(directory)
            descriptor = os.open(root / 'opt', os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for action in ('install', 'uninstall'):
                    result = invoke(action)
                    self.assertEqual(json.loads(result.stdout)['outcome'], 'operator_check_unsafe_path')
                    self.assertFalse((root / 'opt/adt-operator').exists())
            finally:
                os.close(descriptor)

    def test_reinstall_not_permitted_keeps_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root, files, invoke = self.fixture(directory)
            installed = invoke('install')
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            (root / 'usr/bin/sudo').write_text(f'#!{sys.executable}\nraise SystemExit(1)\n')
            baselines = {
                item.path: (root / item.path.lstrip('/')).read_bytes()
                for item in files if item.kind != 'dir'
            }
            refused = json.loads(invoke('install').stdout)
            self.assertEqual(refused['outcome'], 'not_permitted')
            self.assertEqual(set(refused['remaining']), {item.path for item in files})
            for name, content in baselines.items():
                self.assertEqual((root / name.lstrip('/')).read_bytes(), content)

    def test_install_interrupted_at_each_replacement_reruns(self):
        for cut in range(1, 6):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as directory:
                root, files, invoke = self.fixture(directory)
                payload = install.invocation_payload('linux', sys.executable, 'operator', files,
                                                     root=directory, owner_uid=os.geteuid())
                instrumented = install.INSTALLER_SOURCE.replace(
                    '\ntry:\n    result = main()',
                    f'''
original_replace = os.replace
replacements = 0
def stop_after_replace(source, target):
    global replacements
    original_replace(source, target)
    replacements += 1
    if replacements == {cut}:
        os._exit(65)
os.replace = stop_after_replace
try:
    result = main()''',
                )
                result = subprocess.run([sys.executable, '-IS', '-c', instrumented, 'install'],
                                        input=payload, capture_output=True, check=False)
                self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
                result = invoke('install')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for item in files:
                    self.assertTrue((root / item.path.lstrip('/')).exists(), item.path)
