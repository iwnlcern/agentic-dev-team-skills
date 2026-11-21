from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from relay_engine.api_principals import OperatorPrincipal
from relay_engine import operator_terminal as terminal

ADT = runpy.run_path(str(Path(__file__).parents[2] / 'adt'))
FIXTURES = Path(__file__).parent / 'fixtures' / 'sshsig'


class OperatorTerminalTests(unittest.TestCase):
    def test_confirmation_binds_its_act(self):
        message = terminal.build_message('engine_start', {'root': '/selected'}, store_id='store', value=bytes(32))
        signature = (FIXTURES / 'engine-start.sig').read_bytes()
        kwargs = {'key_path': str(FIXTURES / 'engine-start.pub'), 'stat': lambda path: type(
            'Metadata', (), {'st_uid': 0, 'st_mode': (0o100644 if path.endswith('.pub') else 0o40755)})()}
        self.assertEqual(terminal._principal_after_verify(message, signature, **kwargs), OperatorPrincipal('terminal'))
        for purpose, root, store, value in (
            ('engine_stop', '/selected', 'store', bytes(32)),
            ('engine_start', '/different', 'store', bytes(32)),
            ('engine_start', '/selected', 'different', bytes(32)),
            ('engine_start', '/selected', 'store', b'1' * 32),
        ):
            with self.subTest(purpose=purpose, root=root, store=store), self.assertRaisesRegex(
                terminal.OperatorCheckRefused, 'bad_signature'
            ):
                wrong = terminal.build_message(purpose, {'root': root}, store_id=store, value=value)
                terminal._principal_after_verify(wrong, signature, **kwargs)

    def test_engine_down_confirms_in_process(self):
        require = ADT['confirm_locally'].__globals__
        with patch.dict(require, {'prove': lambda message, act_line, **kwargs: b'proof'}), patch.object(
            terminal, '_principal_after_verify', return_value=OperatorPrincipal('terminal')
        ) as verify:
            result = ADT['confirm_locally']('engine_start', {'root': '/selected'}, store_id='store')
        self.assertEqual(result, OperatorPrincipal('terminal'))
        self.assertEqual(verify.call_args.args[1], b'proof')

    def test_sshsig_parse(self):
        signature = (FIXTURES / 'engine-start.sig').read_bytes()
        self.assertEqual(terminal.parse_sshsig(signature).namespace, 'adt-terminal-check-v1')
        for malformed in (b'x' * 64, signature + b'x', signature[:-20]):
            with self.assertRaisesRegex(terminal.OperatorCheckRefused, 'bad_signature'):
                terminal.parse_sshsig(malformed)

    def test_key_path_ownership_is_required_before_signature(self):
        fixture = FIXTURES / 'engine-start.pub'
        signature = (FIXTURES / 'engine-start.sig').read_bytes()
        message = terminal.build_message('engine_start', {'root': '/selected'}, store_id='store', value=bytes(32))

        def unsafe_parent(path):
            if path == str(fixture):
                return type('Metadata', (), {'st_uid': 0, 'st_mode': 0o100644})()
            return type('Metadata', (), {'st_uid': 0, 'st_mode': 0o40775})()

        with self.assertRaisesRegex(terminal.OperatorCheckRefused, 'key_unsafe'):
            terminal._principal_after_verify(message, signature, key_path=str(fixture), stat=unsafe_parent)

    def test_missing_prover_refuses_without_requesting_confirmation(self):
        with self.assertRaisesRegex(terminal.OperatorCheckRefused, 'no_prove'):
            terminal.require_operator('engine_start', {}, store_id='store', prove=None,
                                      show=lambda line: self.fail('Unexpected confirmation prompt'),
                                      act_line='Start')

    def test_engine_down_refuses_changed_passwordless_policy(self):
        import stat
        from subprocess import CompletedProcess
        from types import SimpleNamespace

        bindings = ADT['confirm_locally'].__globals__
        original = bindings['prove']
        trace = []

        def runner(command, **kwargs):
            trace.append(command[-1])
            return CompletedProcess(command, 0, b'', b'')

        def prove(message, act_line, **kwargs):
            return original(
                message, act_line, run=runner, clock=lambda: 0,
                read_helper=lambda: b'#!/usr/bin/python3 -IS\n',
                lstat=lambda path: SimpleNamespace(
                    st_uid=0, st_mode=(stat.S_IFREG if path == '/usr/bin/python3' else stat.S_IFDIR) | 0o755),
            )

        with patch.dict(bindings, {'prove': prove}), patch.object(terminal, '_principal_after_verify') as verify:
            with self.assertRaisesRegex(terminal.OperatorCheckRefused, 'passwordless'):
                ADT['confirm_locally']('engine_start', {'root': '/selected'}, store_id='store')
        self.assertEqual(trace, ['probe'])
        verify.assert_not_called()
