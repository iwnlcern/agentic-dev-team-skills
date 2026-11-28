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

class OperatorTerminalPurposeTests(unittest.TestCase):
    def test_purposes_match_the_helper(self):
        import ast
        from pathlib import Path
        from relay_engine.operator_terminal import PURPOSES

        path = Path(__file__).resolve().parents[2] / 'adt-operator-check'
        tree = ast.parse(path.read_bytes())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'PURPOSES'
                                  for target in node.targets))
        helper = ast.literal_eval(assignment.value)
        self.assertEqual(PURPOSES, helper)
        with self.assertRaises(AssertionError):
            self.assertEqual(tuple(value for value in PURPOSES if value != 'seat_unseat'), helper)


class OperatorTerminalClientTests(unittest.TestCase):
    @staticmethod
    def challenge():
        return {'challenge_id': 'a' * 32, 'value': '00' * 32, 'store_id': 'store'}

    def test_confirm_with_service_order(self):
        import base64
        from types import SimpleNamespace

        from relay_engine.api_client import ApiErrorValue

        confirm = ADT['confirm_with_service']
        error = ApiErrorValue('E_API_INTERNAL', 'internal', {'kind': 'handler_fault'}, 'report_nonsecret_failure')
        for result in ({'purpose': 'engine_start', 'result_base64': 'eyJkb25lIjp0cnVlfQ=='}, error):
            trace = []
            challenge = self.challenge()

            def call(op, args):
                trace.append((op, args))
                return challenge if op == 'operator.terminal-challenge' else result

            def prove(message, line):
                trace.append(('prove', (message, line)))
                return b'signed'

            args = {'z': 1, 'a': 2}
            line = 'Confirm selection'
            answer = confirm(SimpleNamespace(call=call), 'engine_start', args, act_line=line,
                             prove=prove, show=lambda shown: trace.append(('show', shown)))
            message = terminal.build_message('engine_start', args, store_id='store', value=bytes(32))
            self.assertEqual(trace, [
                ('operator.terminal-challenge', {}),
                ('show', line),
                ('prove', (message, line)),
                ('operator.terminal-act', dict(challenge_id=challenge['challenge_id'], purpose='engine_start',
                 args_base64=base64.b64encode(b'{"a":2,"z":1}').decode('ascii'), signature_base64='c2lnbmVk')),
            ])
            if result is error:
                self.assertIs(answer, error)
            else:
                self.assertEqual(answer, {'done': True})

    def test_confirm_with_service_not_sent(self):
        from types import SimpleNamespace

        from relay_engine.api_client import ApiErrorValue, EndpointUnavailable, LocalRefusal

        confirm = ADT['confirm_with_service']
        refusal = ApiErrorValue('E_TERMINAL_LIMIT', 'unavailable', {'kind': 'challenge_limit'},
                                'retry_after_challenge_expiry')
        cases = [
            (refusal, None, 'E_TERMINAL_LIMIT challenge_limit'),
            (LocalRefusal('ready_file_mismatch'), None, 'ready_file_mismatch'),
            (EndpointUnavailable('not_performed'), None, 'not_performed'),
        ]
        from relay_engine.operator_check_words import refusal_line

        for reason in ('no_prove', 'passwordless', 'not_permitted', 'interpreter_unsafe',
                       'not_installed', 'prove_failed', 'timeout'):
            error = terminal.OperatorCheckRefused(reason)
            cases.append((self.challenge(), error, refusal_line(error)))
        cases.append((self.challenge(), KeyboardInterrupt(), None))
        for reply, prove_error, line in cases:
            with self.subTest(reply=reply, prove_error=prove_error):
                trace = []

                def call(op, args):
                    trace.append(op)
                    if isinstance(reply, BaseException):
                        raise reply
                    return reply

                def prove(*_):
                    if prove_error is None:
                        self.fail('An unavailable challenge must not ask for proof')
                    raise prove_error

                with self.assertRaises(terminal.ActNotSent) as caught:
                    confirm(SimpleNamespace(call=call), 'engine_start', {}, act_line='Confirm',
                            prove=prove, show=lambda _: None)
                self.assertEqual(caught.exception.line, line)
                self.assertEqual(trace, ['operator.terminal-challenge'])

    def test_confirm_with_service_answer_lost(self):
        import base64
        from types import SimpleNamespace

        from relay_engine.api_client import EndpointUnavailable, LocalRefusal

        confirm = ADT['confirm_with_service']
        cases = [(LocalRefusal('ready_file_mismatch'), 'ready_file_mismatch'),
                 (EndpointUnavailable('not_performed'), 'not_performed')]
        unreadable = "the service's answer could not be read"
        for raw in (b'not JSON', b'null', b'[]', b'1', b'"one"', b'true'):
            cases.append(({'result_base64': base64.b64encode(raw).decode('ascii')}, unreadable))
        cases.append(({'result_base64': '!'}, unreadable))
        for reply, line in cases:
            with self.subTest(reply=reply):
                trace = []

                def call(op, args):
                    trace.append(op)
                    if op == 'operator.terminal-challenge':
                        return self.challenge()
                    if isinstance(reply, BaseException):
                        raise reply
                    return reply

                with self.assertRaises(terminal.ActAnswerLost) as caught:
                    confirm(SimpleNamespace(call=call), 'engine_start', {}, act_line='Confirm',
                            prove=lambda *_: b'signed', show=lambda _: None)
                self.assertEqual(caught.exception.line, line)
                self.assertEqual(trace, ['operator.terminal-challenge', 'operator.terminal-act'])

    def test_engine_up_refuses_changed_passwordless_policy(self):
        import stat
        from subprocess import CompletedProcess
        from types import SimpleNamespace

        from relay_engine.operator_check_words import refusal_line

        original = ADT['prove']
        trace, calls = [], []

        def runner(command, **kwargs):
            trace.append(command[-1])
            output = (FIXTURES / 'engine-start.sig').read_bytes() if 'sign' in command else b''
            return CompletedProcess(command, 0, output, b'')

        def prove(message, line, **kwargs):
            return original(message, line, run=runner, clock=lambda: 0,
                            read_helper=lambda: b'#!/usr/bin/python3 -IS\n',
                            lstat=lambda path: SimpleNamespace(
                                st_uid=0, st_mode=(stat.S_IFREG if path == '/usr/bin/python3'
                                                   else stat.S_IFDIR) | 0o755))

        def call(op, args):
            calls.append(op)
            return self.challenge()

        confirm = ADT['confirm_with_service']
        with (patch.dict(confirm.__globals__, {'prove': prove}),
              patch.object(terminal, 'require_operator') as require,
              patch.object(terminal, '_principal_after_verify') as verify,
              self.assertRaises(terminal.ActNotSent) as caught):
            confirm(SimpleNamespace(call=call), 'engine_start', {}, act_line='Confirm', show=lambda _: None)
        require.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(trace, ['probe'])
        self.assertEqual(calls, ['operator.terminal-challenge'])
        self.assertEqual(caught.exception.line, refusal_line(terminal.OperatorCheckRefused('passwordless')))

    def test_engine_up_round_trip_through_the_service(self):
        from hashlib import sha256

        from relay_engine import api_platform, api_service, api_terminal, jcs
        from relay_engine.api_client import ApiClient, read_ready_file
        from relay_engine.operator_queue.component import queue_component
        from relay_engine.tests.test_api_service import ApiServiceTests, P

        confirm = ADT['confirm_with_service']
        fixture = ApiServiceTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        acts = api_terminal.TerminalActs()
        received, verified = [], []

        def handle(principal, args):
            received.append((principal, args))
            return {'done': True}

        acts.register('engine_start', handle, max_result_bytes=256)
        service = api_service.ApiService(str(fixture.root), P, clock=lambda: 10000,
                                         admission=fixture.admitted.receipt, components=(
                                             api_platform.platform_component(), queue_component(),
                                             api_terminal.terminal_component(acts)))
        self.addCleanup(service.stop)
        ready, _ = fixture.start(service)
        info = read_ready_file(str(ready))
        client = ApiClient.open(info)
        self.addCleanup(client.close)
        args = {'root': '/selected', 'again': True}

        def verify(message, signature, **kwargs):
            verified.append((message, signature))

        with (patch.object(terminal, '_verify_signature', side_effect=verify),
              patch.object(terminal, 'require_operator', side_effect=AssertionError('duplicate confirmation'))
              as require):
            result = confirm(client, 'engine_start', args, act_line='Confirm',
                             prove=lambda *_: b'signed', show=lambda _: None)
        self.assertEqual(result, {'done': True})
        self.assertEqual(received, [(OperatorPrincipal('terminal'), args)])
        self.assertEqual(len(verified), 1)
        message, signature = verified[0]
        self.assertEqual(signature, b'signed')
        self.assertEqual(message.split(b'\0')[1:4], [
            b'engine_start', sha256(jcs.jcs_encode(args)).hexdigest().encode('ascii'),
            info.store_id.encode('ascii'),
        ])
        require.assert_not_called()

    def test_refusal_line_keeps_owner_start_text(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from types import SimpleNamespace

        from relay_engine import owner_start
        from relay_engine.operator_check_words import refusal_line

        error = terminal.OperatorCheckRefused('not_installed')
        expected = 'The operator check is missing. Run adt operator install-check.'
        self.assertEqual(refusal_line(error), expected)
        args = SimpleNamespace(store_root='root', project_id='p', initialize=False, project_name='p', bridge_port=0)
        err = StringIO()
        with patch.object(owner_start, 'run_owner', side_effect=error), redirect_stderr(err):
            self.assertEqual(ADT['_run_selected_owner'](args), 1)
        self.assertEqual(err.getvalue(), f'adt engine run: {expected}\n')

    def test_command_checks_budget_first(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from relay_engine import jcs

        confirm = ADT['confirm_with_service']
        args = {'x': 'x' * 10233}
        self.assertEqual(len(jcs.jcs_encode(args)), 10241)
        call, prove = Mock(), Mock()
        with self.assertRaises(terminal.ActNotSent) as caught:
            confirm(SimpleNamespace(call=call), 'engine_start', args, act_line='Confirm', prove=prove,
                    show=lambda _: self.fail('Unexpected confirmation prompt'))
        self.assertIn('10240', caught.exception.line)
        call.assert_not_called()
        prove.assert_not_called()
