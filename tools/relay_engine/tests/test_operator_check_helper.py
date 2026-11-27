import ast
import contextlib
import io
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).parents[2]
PATH = TOOLS / 'adt-operator-check'
HELPER = runpy.run_path(str(PATH))
PURPOSES = (
    'engine_stop', 'engine_start', 'project_stop', 'project_start',
    'restore', 'mirror_recovery', 'adopt_restored', 'abandonment',
    'new_folder_pass', 'answer_5d', 'report_confirmation',
    'authority.set_reapproval', 'authority.release_restore',
    'authority.assign_reviewer', 'authority.unbind_branch',
    'queue.answer', 'queue.open', 'queue.dismiss', 'queue.dismiss_all',
    'checkout_register', 'checkout_unregister', 'queue.go_ahead_without_review',
    'queue.waive_verification', 'slip.revoke', 'slip.reissue', 'seat_unseat',
)
FIELDS = (b'adt-terminal-check-v1', b'engine_start', b'a' * 64, b'store', b'0' * 64)


class OperatorCheckHelperTests(unittest.TestCase):
    def test_helper_first_line(self):
        self.assertEqual(PATH.read_bytes().splitlines()[0], b'#!/usr/bin/python3 -IS')

    def test_helper_purpose_list(self):
        self.assertEqual(HELPER['PURPOSES'], PURPOSES)

    def test_helper_parse(self):
        self.assertEqual(HELPER['parse_message'](b'\0'.join(FIELDS)).store_id, 'store')
        for index, bad_field in enumerate((b'other', b'unlisted', b'A' * 64, b'\x7f', b'f')):
            with self.subTest(index=index), self.assertRaises(ValueError):
                HELPER['parse_message'](b'\0'.join(FIELDS[:index] + (bad_field,) + FIELDS[index + 1:]))
        with self.assertRaises(ValueError):
            HELPER['parse_message'](b'\0'.join(FIELDS + (b'extra',)))

    def test_helper_prints_what_it_signs(self):
        message = b'\0'.join(FIELDS)
        stderr = io.StringIO()
        stdout = io.TextIOWrapper(io.BytesIO())
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            status = HELPER['main'](
                ['sign', message.hex()],
                signer=(sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'),
            )
        stdout.flush()
        self.assertEqual(status, 0)
        self.assertEqual(stdout.buffer.getvalue(), message)
        self.assertEqual(stderr.getvalue(), 'Signing: engine_start, arguments aaaaaaaaaaaaaaaa, store store\n')

    def test_helper_stdlib_39(self):
        imports = set()
        for node in ast.walk(ast.parse(PATH.read_text(), feature_version=(3, 9))):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertEqual(imports, {'re', 'subprocess', 'sys', 'typing'})
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / 'operator-check'
            _, body = PATH.read_bytes().split(b'\n', 1)
            launcher.write_bytes(f'#!{sys.executable} -ISB\n'.encode() + body)
            launcher.chmod(0o755)
            result = subprocess.run([launcher, 'probe'], capture_output=True, check=True)
            self.assertEqual((result.stdout, result.stderr), (b'', b''))
