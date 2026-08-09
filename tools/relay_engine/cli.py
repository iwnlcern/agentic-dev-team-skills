"""Relay-engine command entry points."""

import argparse
import os
import sys

from relay_engine import client, daemon, errors, strings


def _cmd_explain(args):
    if args.code not in errors.ERRORS:
        strings.emit("stderr", "usage-error")
        return 2
    explanation, remedy = errors.explain_for(args.code)
    strings.emit("stdout", "explain-heading", code=args.code)
    strings.emit("stdout", "explain-cause", cause=explanation)
    strings.emit("stdout", "explain-remedy", remedy=remedy)
    return 0


def _command_root(args, fresh=False):
    if fresh:
        return os.path.realpath(os.path.abspath(args.root or os.getcwd()))
    return client.discover_root(args.root)


def _cmd_daemon_start(args):
    root = _command_root(args, fresh=True)
    if not daemon.launch(root, socket_override=args.socket,
                         timeout=args.timeout, top_seat=args.seat,
                         top_role=args.role, top_dispatch=args.dispatch):
        strings.emit("stderr", "daemon-start-failed")
        return 1
    return 0


def _cmd_daemon_stop(args):
    client.request(_command_root(args), "daemon.stop", {},
                   timeout=args.timeout)
    return 0


def _cmd_submit(args):
    result = client.submit(
        _command_root(args), args.draft, args.key,
        admits_against=args.admits_against, timeout=args.timeout)
    strings.emit("stdout", "command-result",
                 result=strings.machine_result(result))
    return 0


def _cmd_status(args):
    result = client.request(_command_root(args), "status", {},
                            timeout=args.timeout)
    strings.emit("stdout", "command-result",
                 result=strings.machine_result(result))
    return 0


def _emit_result(result):
    strings.emit("stdout", "command-result",
                 result=strings.machine_result(result))
    return 0


def _cmd_seat_register(args):
    return _emit_result(client.seat_register(
        _command_root(args), args.address, args.role, args.dispatch,
        replace=args.seat_command == "replace", timeout=args.timeout))


def _cmd_seat_stand_down(args):
    return _emit_result(client.seat_stand_down(
        _command_root(args), args.address, timeout=args.timeout))


def _cmd_seat_show(args):
    return _emit_result(client.seat_show(
        _command_root(args), args.address, timeout=args.timeout))


def _cmd_roster(args):
    return _emit_result(client.request(
        _command_root(args), "roster", {}, timeout=args.timeout))


def _root_option(parser):
    parser.add_argument("--root")


def _timeout_option(parser):
    parser.add_argument("--timeout", type=float, default=10.0)


def build_parser():
    parser = argparse.ArgumentParser(prog="relay")
    commands = parser.add_subparsers(dest="command", required=True)

    explain = commands.add_parser("explain")
    explain.add_argument("code")
    explain.set_defaults(handler=_cmd_explain)

    daemon_command = commands.add_parser("daemon")
    daemon_commands = daemon_command.add_subparsers(
        dest="daemon_command", required=True)
    daemon_start = daemon_commands.add_parser("start")
    _root_option(daemon_start)
    _timeout_option(daemon_start)
    daemon_start.add_argument("--socket")
    daemon_start.add_argument("--seat")
    daemon_start.add_argument("--role", default="Orchestrator Planner")
    daemon_start.add_argument("--dispatch")
    daemon_start.set_defaults(handler=_cmd_daemon_start)
    daemon_stop = daemon_commands.add_parser("stop")
    _root_option(daemon_stop)
    _timeout_option(daemon_stop)
    daemon_stop.set_defaults(handler=_cmd_daemon_stop)

    submit = commands.add_parser("submit")
    submit.add_argument("draft")
    _root_option(submit)
    _timeout_option(submit)
    submit.add_argument("--key")
    submit.add_argument("--admits-against")
    submit.set_defaults(handler=_cmd_submit)

    status = commands.add_parser("status")
    _root_option(status)
    _timeout_option(status)
    status.set_defaults(handler=_cmd_status)

    seat = commands.add_parser("seat")
    seat_commands = seat.add_subparsers(dest="seat_command", required=True)
    for name in ("register", "replace"):
        command = seat_commands.add_parser(name)
        command.add_argument("address")
        command.add_argument("--role", required=True)
        command.add_argument("--dispatch")
        _root_option(command)
        _timeout_option(command)
        command.set_defaults(handler=_cmd_seat_register)
    stand_down = seat_commands.add_parser("stand-down")
    stand_down.add_argument("address")
    _root_option(stand_down)
    _timeout_option(stand_down)
    stand_down.set_defaults(handler=_cmd_seat_stand_down)
    show = seat_commands.add_parser("show")
    show.add_argument("address")
    _root_option(show)
    _timeout_option(show)
    show.set_defaults(handler=_cmd_seat_show)

    roster = commands.add_parser("roster")
    _root_option(roster)
    _timeout_option(roster)
    roster.set_defaults(handler=_cmd_roster)
    return parser


def _report_command_error(exc):
    strings.emit("stderr", "explain-heading", code=exc.code)
    strings.emit("stderr", "explain-cause", cause=exc.cause)
    strings.emit("stderr", "explain-remedy", remedy=exc.remedy)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = build_parser().parse_args(args)
        return parsed.handler(parsed)
    except (client.RemoteError, errors.EngineError) as exc:
        _report_command_error(exc)
        return 1
    except SystemExit as exc:
        return int(exc.code)
    except BaseException as exc:
        strings.report_diagnostic(exc)
        strings.emit("stderr", "unexpected-error")
        return 1
