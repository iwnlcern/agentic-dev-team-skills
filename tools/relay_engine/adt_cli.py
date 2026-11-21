"""Explicit foreground service, relay submission and session sign-out commands."""
import argparse
import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import time
import tempfile
import uuid

from relay_engine import api_client, api_service, strings, store_layout, version


@dataclass(frozen=True)
class CommandGroup:
    name: str
    build: Callable[[argparse._SubParsersAction], None]
    engine_subcommands: bool = False


@dataclass(frozen=True)
class WindowSession:
    key_path: str
    session_id: str
    ready_path: str | None


WindowResolver = Callable[[], WindowSession | None]
NotifierFactory = Callable[[str | None, api_service.ApiService], None]


def _bridge_port(value):
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError('expected a port from 0 to 65535')
    return port


def _parser(groups):
    parser = argparse.ArgumentParser(prog='adt')
    commands = parser.add_subparsers(dest='command', required=True)
    engine = commands.add_parser('engine').add_subparsers(dest='engine_command', required=True)
    serve = engine.add_parser('serve')
    serve.add_argument('--store-root', required=True)
    serve.add_argument('--project-id', required=True)
    serve.add_argument('--bridge-port', type=_bridge_port)
    serve.add_argument('--terminal-notifier', metavar='PATH')
    submit = commands.add_parser('submit')
    submit.add_argument('draft')
    submit.add_argument('--submission-key')
    sign_out = commands.add_parser('sign-out')
    sign_out.add_argument('--operation-key')
    for command in (submit, sign_out):
        command.add_argument('--session-id')
        command.add_argument('--key')
        command.add_argument('--ready')
    for group in groups:
        group.build(engine if group.engine_subcommands else commands)
    return parser


def _resolve(args, resolver):
    record = None
    flags = (args.key, args.session_id)
    environment = (os.environ.get('ADT_KEY_FILE'), os.environ.get('ADT_SESSION_ID'))
    pair = None
    for tier in (flags, environment):
        if any(value is not None for value in tier):
            if not all(value is not None for value in tier):
                raise api_client.LocalRefusal('mixed_credentials')
            pair = tier
            break
    ready = args.ready if args.ready is not None else os.environ.get('ADT_READY_FILE')
    if resolver is not None and (pair is None or ready is None):
        record = resolver()
    if pair is None:
        if record is None:
            raise api_client.LocalRefusal('no_session')
        pair = (record.key_path, record.session_id)
        if not all(value is not None for value in pair):
            raise api_client.LocalRefusal('mixed_credentials')
    if ready is None and record is not None:
        ready = record.ready_path
    return pair[0], pair[1], ready


def _refusal(value):
    if isinstance(value, api_client.ApiErrorValue):
        result = {'code': value.code, 'class': value.cls, 'cause': dict(value.cause), 'remedy': value.remedy}
        strings.emit('stderr', 'adt-refusal', result=strings.machine_result(result))
        return 75 if value.code == 'E_API_UNAVAILABLE' else 1
    strings.emit('stdout', 'command-result', result=strings.machine_result(value))
    return 0


def _show_path(path):
    absolute = os.path.abspath(path)
    if strings._is_show_absolute(absolute):
        return absolute
    raw = os.fsencode(absolute)
    return strings.rejected_value(hashlib.sha256(raw).hexdigest()[:12], len(raw))


def _serve_refusal(error, root, project):
    path = _show_path(root)
    if isinstance(error, store_layout.LayoutFault):
        if error.cause == 'missing':
            if os.path.isdir(root):
                strings.emit('stderr', 'serve-no-catalogue', path=path)
            else:
                strings.emit('stderr', 'serve-no-folder', path=path)
        else:
            strings.emit('stderr', 'serve-store-unreadable', path=path, cause=error.cause)
    elif isinstance(error, version.FingerprintError):
        strings.emit('stderr', 'serve-install-mismatch', path=path, cause=str(error))
    elif error.reason == 'store_locked':
        strings.emit('stderr', 'serve-store-locked', path=path)
    elif error.reason == 'ledger_unavailable':
        strings.emit('stderr', 'serve-project-unavailable', path=path, project=project)
    elif error.reason == 'runtime_dir_unsafe':
        strings.emit('stderr', 'serve-runtime-unsafe', path=path,
                     tmp=_show_path(tempfile.gettempdir()))
    else:
        strings.emit('stderr', 'serve-install-incomplete', path=path, reason=error.reason)
    return 1


def _private_store_channels():
    channels = []
    try:
        for name, access in (('ADT_PRIVATE_IDLE_FD', os.O_RDONLY),
                             ('ADT_PRIVATE_RETIREMENT_FD', os.O_WRONLY)):
            value = os.environ.pop(name, None)
            if value is None:
                channels.append(None)
                continue
            if not value.isascii() or not value.isdecimal() or len(value) > 9:
                raise ValueError('invalid private channel')
            fd = int(value)
            if fd < 3 or fd in channels or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != access:
                raise ValueError('invalid private channel')
            channels.append(fd)
            os.set_inheritable(fd, False)
    except (ValueError, OSError) as error:
        for fd in channels:
            if fd is not None:
                os.close(fd)
        raise store_layout.LayoutFault('idle-receipt-invalid') from error
    return channels


def main(argv: Sequence[str], *, groups: Sequence[CommandGroup] = (),
         resolver: WindowResolver | None = None, queue_notifier: NotifierFactory | None = None,
         stop_report=None) -> int:
    retry = {}
    try:
        args = _parser(groups).parse_args(list(argv))
        if hasattr(args, 'handler'):
            return args.handler(args)
        if args.command == 'engine':
            try:
                idle_fd, retire_fd = _private_store_channels()
            except store_layout.LayoutFault as error:
                return _serve_refusal(error, args.store_root, args.project_id)
            startup_socket = os.environ.pop('ADT_PRIVATE_START_SOCKET', None)
            use_notifier = args.bridge_port is not None
            setup = None
            if use_notifier and queue_notifier is not None:
                setup = lambda service: queue_notifier(args.terminal_notifier, service)
            service = api_service.ApiService(args.store_root, args.project_id,
                                             clock=lambda: time.time_ns() // 1000000,
                                             bridge_port=args.bridge_port, stop_report=stop_report,
                                             idle_fd=idle_fd, retirement_fd=retire_fd,
                                             startup_socket=startup_socket,
                                             notifier_setup=setup,
                                             notifier_mode='configured' if use_notifier else 'off')
            service.arm_foreground()
            try:
                try:
                    path = service.start()
                except (api_service.ServiceStartRefused, store_layout.LayoutFault, version.FingerprintError) as error:
                    return _serve_refusal(error, args.store_root, args.project_id)
                if path is not None and not service.is_stopping():
                    strings.emit('stdout', 'adt-ready', path=path)
                    service.serve_forever()
                return 0
            finally:
                service.stop()
        key_path, session_id, ready_path = _resolve(args, resolver)
        key = api_client.read_key_file(key_path)
        credentials = {'key_id': key.key_id, 'key_secret': key.key_secret, 'session_id': session_id}
        if args.command == 'submit':
            try:
                with open(args.draft, 'rb') as stream:
                    data = stream.read(131073)
            except OSError:
                raise api_client.LocalRefusal('draft_unreadable') from None
            if len(data) > 131072:
                raise api_client.LocalRefusal('draft_too_large')
            submission_key = (args.submission_key if args.submission_key is not None
                              else 'sha256:' + hashlib.sha256(data).hexdigest())
            retry = {'submission_key': submission_key}
        else:
            operation_key = args.operation_key if args.operation_key is not None else str(uuid.uuid4())
            retry = {'operation_key': operation_key}
        ready = api_client.read_ready_file(ready_path)
        client = api_client.ApiClient.open(ready)
        try:
            if args.command == 'submit':
                resumed = client.call('session.resume', credentials)
                if isinstance(resumed, api_client.ApiErrorValue):
                    return _refusal(resumed)
                result = client.call('relay.submit', {'draft_base64': base64.b64encode(data).decode('ascii'),
                                                      'submission_key': submission_key})
            else:
                end_args = dict(credentials, operation_key=operation_key)
                result = client.call('session.end', end_args)
                if (isinstance(result, api_client.ApiErrorValue)
                        and result.cause.get('kind') == 'not_signed_in'):
                    resumed = client.call('session.resume', credentials)
                    if isinstance(resumed, api_client.ApiErrorValue):
                        return _refusal(resumed)
                    result = client.call('session.end', end_args)
            return _refusal(result)
        finally:
            client.close()
    except api_client.LocalRefusal as error:
        result = {'local': error.kind}
        if error.kind == 'no_session':
            result['remedy'] = 'run adt sign-in'
        strings.emit('stderr', 'adt-local-refusal', result=strings.machine_result(result))
        return 1
    except api_client.EndpointUnavailable as error:
        strings.emit('stderr', 'adt-unknown-outcome',
                     result=strings.machine_result({'outcome': error.outcome, **retry}))
        return 75
    except SystemExit as error:
        return int(error.code)
    except Exception as error:
        strings.report_diagnostic(error)
        strings.emit('stderr', 'unexpected-error')
        return 1
