"""Relay-engine command entry points."""

import sys

from relay_engine import errors, strings


def _cmd_explain(code):
    if code not in errors.ERRORS:
        strings.emit("stderr", "usage-error")
        return 2
    item = errors.error_for(code)
    strings.emit("stdout", "explain-heading", code=item.code)
    strings.emit("stdout", "explain-cause", cause=item.cause)
    strings.emit("stdout", "explain-remedy", remedy=item.remedy)
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == "explain":
            return _cmd_explain(args[1])
        strings.emit("stderr", "usage-error")
        return 2
    except BaseException as exc:
        strings.report_diagnostic(exc)
        strings.emit("stderr", "unexpected-error")
        return 1
