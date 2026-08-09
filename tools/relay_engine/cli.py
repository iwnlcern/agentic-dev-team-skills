"""Relay-engine command entry points."""

import sys

from relay_engine import errors, strings


def _cmd_explain(code):
    if code not in errors.ERRORS:
        strings.emit("stderr", "usage-error")
        return 2
    explanation, remedy = errors.explain_for(code)
    strings.emit("stdout", "explain-heading", code=code)
    strings.emit("stdout", "explain-cause", cause=explanation)
    strings.emit("stdout", "explain-remedy", remedy=remedy)
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
