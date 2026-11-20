import os
from pathlib import Path
import runpy
import stat
from subprocess import CompletedProcess
from types import SimpleNamespace
import unittest

from relay_engine import operator_check_words as words
from relay_engine.operator_terminal import OperatorCheckRefused

ADT = runpy.run_path(str(Path(__file__).parents[2] / 'adt'))
HELPER_LINE = b'#!/usr/bin/python3 -IS\n'
def safe_metadata(path):
    kind = stat.S_IFREG if path == '/usr/bin/python3' else stat.S_IFDIR
    return SimpleNamespace(st_uid=0, st_mode=kind | 0o755)


class OperatorCheckProveTests(unittest.TestCase):
    def test_prove_probes_before_every_sign(self):
        trace = []
        probe_status = [1]

        def run(command, **kwargs):
            trace.append(command[-1] if command[-1] == 'probe' else command[-2])
            return CompletedProcess(command, probe_status[0] if command[-1] == 'probe' else 0, b'signature', b'')

        def prove():
            return ADT['prove'](b'input', 'Start %u', run=run, clock=lambda: 0,
                                lstat=safe_metadata, read_helper=lambda: HELPER_LINE)

        self.assertEqual(prove(), b'signature')
        probe_status[0] = 0
        with self.assertRaisesRegex(OperatorCheckRefused, 'passwordless'):
            prove()
        self.assertEqual(trace, ['probe', 'sign', 'probe'])

    def test_act_line_rendering(self):
        line = words.render_act_line('Start %u\n\x1b')
        self.assertEqual(line, 'Start %u\\x0a\\x1b')
        self.assertEqual(words.prompt_text(line), 'Password to confirm: Start %%u\\x0a\\x1b: ')
        self.assertEqual(len(words.render_act_line('a' * 161)), 160)

    def test_not_permitted_reported(self):
        self.assertEqual(words.classify_sudo_failure('jack is not in the sudoers file'), 'not_permitted')
        self.assertEqual(words.classify_sudo_failure('unrecognized error'), 'prove_failed')

    def test_prove_timeout(self):
        samples = iter((0, 120_000_000_000))
        trace = []

        def run(command, **kwargs):
            trace.append(command[-1])
            return CompletedProcess(command, 1, b'', b'')

        with self.assertRaisesRegex(OperatorCheckRefused, 'timeout'):
            ADT['prove'](b'input', 'Start selected', run=run, clock=lambda: next(samples),
                         lstat=safe_metadata, read_helper=lambda: HELPER_LINE)
        self.assertEqual(trace, ['probe'])

    def test_interpreter_check_refuses_before_running_a_process(self):
        def metadata(path):
            if path == '/usr/bin':
                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o775)
            return safe_metadata(path)

        def run(*args, **kwargs):
            self.fail('A process ran before the interpreter check')

        with self.assertRaisesRegex(OperatorCheckRefused, 'interpreter_unsafe.*[/]usr/bin'):
            ADT['prove'](b'input', 'Start selected', run=run, clock=lambda: 0, lstat=metadata,
                         read_helper=lambda: HELPER_LINE)

    def test_corrupt_helper_shebang_refuses_without_a_sudo_call(self):
        def run(*args, **kwargs):
            self.fail('A process ran with a malformed helper interpreter')

        with self.assertRaisesRegex(OperatorCheckRefused, 'interpreter_unsafe'):
            ADT['prove'](b'input', 'Start selected', run=run, clock=lambda: 0,
                         lstat=os.lstat,
                         read_helper=lambda: b'#!/bad\0name -IS\n')

    def test_unknown_purpose_tells_reinstall(self):
        def run(command, **kwargs):
            if command[-1] == 'probe':
                return CompletedProcess(command, 1, b'', b'')
            return CompletedProcess(command, 2, b'', b'unknown purpose\n')

        with self.assertRaisesRegex(OperatorCheckRefused, 'install-check'):
            ADT['prove'](b'input', 'Start selected', run=run, clock=lambda: 0,
                         lstat=safe_metadata, read_helper=lambda: HELPER_LINE)

    def test_refusal_rows_name_their_host(self):
        self.assertEqual({host for _, host, _ in words.SUDO_REFUSALS}, {'linux', 'none'})
        self.assertTrue(all(source for _, _, source in words.SUDO_REFUSALS))
