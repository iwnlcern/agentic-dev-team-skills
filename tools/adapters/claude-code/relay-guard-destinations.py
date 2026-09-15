#!/usr/bin/env python3
"""
Print the write destinations of a shell command, one per line, resolved against argv[1]
(the cwd).

Built on tools/adapters/shell_lex.py: quoted or escaped text is never an operator,
comments are dropped, newline separates commands. Used by bash-relay-guard.sh
(DD-v295-b2 component 14; plan-3 P6).
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "adt_shell_lex", Path(__file__).resolve().parent.parent / "shell_lex.py"
)
shell_lex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shell_lex)

SEPARATORS = (";", "&&", "||", "|", "&", "\n")
REDIRECT_OPS = (">", ">>", ">|")
DIGITS = re.compile(r"^\d+$")


Uncertain = shell_lex.Uncertain
strip_heredoc_bodies = shell_lex.strip_heredoc_bodies


def _redirection(toks, i, out):
    """
    If toks[i] opens a redirection, record its destination in `out` and return the index
    after it; otherwise None.
    """
    text, quoted, op = toks[i]
    if (
        not quoted
        and not op
        and DIGITS.match(text)
        and i + 1 < len(toks)
        and toks[i + 1][2]
        and (toks[i + 1][0] in REDIRECT_OPS or toks[i + 1][0].startswith("<"))
    ):
        return i + 1
    if op and (text in REDIRECT_OPS or text.startswith("<")):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt and not nxt[2]:
            if (
                text in REDIRECT_OPS
                and not nxt[0].startswith("&")
                and nxt[0] != "/dev/null"
            ):
                out.append(nxt[0])
            return i + 2
        if nxt and nxt[2] and nxt[0] == "&":
            return i + 3
        return i + 1
    return None


def destinations(command, cwd):
    toks = shell_lex.lex(strip_heredoc_bodies(command))
    if toks is None:
        raise Uncertain("unbalanced quote")
    out, i, at_command_start = [], 0, True
    while i < len(toks):
        text, quoted, op = toks[i]
        if op and text in SEPARATORS:
            at_command_start = True
            i += 1
            continue
        after = _redirection(toks, i, out)
        if after is not None:
            i = after
            continue
        if at_command_start and not quoted and not op and text in ("tee", "cp", "mv"):
            j, args = i + 1, []
            while j < len(toks) and not (toks[j][2] and toks[j][0] in SEPARATORS):
                after = _redirection(toks, j, out)
                if after is not None:
                    j = after
                    continue
                t, q, o = toks[j]
                if o:
                    j += 1
                    continue
                if q or not t.startswith("-"):
                    args.append(t)
                j += 1
            if text == "tee":
                out.extend(args)
            elif len(args) >= 2:
                out.append(args[-1])
            at_command_start = False
            i = j
            continue
        at_command_start = False
        i += 1
    return [
        os.path.normpath(d if os.path.isabs(d) else os.path.join(cwd, d)) for d in out
    ]


if __name__ == "__main__":
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    try:
        print("\n".join(destinations(sys.stdin.read(), cwd)))
    except Uncertain as why:
        print(f"uncertain: {why}", file=sys.stderr)
        sys.exit(3)
