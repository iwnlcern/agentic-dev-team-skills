#!/usr/bin/env python3
"""Print the write destinations of a shell command, one per line, resolved against argv[1] (the cwd).

Built on tools/adapters/shell_lex.py: quoted or escaped text is never an operator, comments are dropped,
newline separates commands. Used by bash-relay-guard.sh (DD-v295-b2 component 14; plan-3 P6).
"""
import importlib.util, os, re, sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("adt_shell_lex", Path(__file__).resolve().parent.parent / "shell_lex.py")
shell_lex = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(shell_lex)

SEPARATORS = (";", "&&", "||", "|", "&", "\n")
REDIRECT_OPS = (">", ">>", ">|")
DIGITS = re.compile(r"^\d+$")


class Uncertain(Exception):
    """The command cannot be parsed with confidence. The CLI exits 3 and the guard raises its generic advisory when the text names a relay root."""


def strip_heredoc_bodies(command):
    """Drop every here-document body (and its terminator line) before lexing: body text is data, never a command.

    Mirrors the lexer's quote and escape rules so a quoted `<<` is not an operator; `<<`/`<<-` register a pending
    body whose lines are consumed after the current line up to the delimiter (leading tabs stripped for `<<-`);
    `<<<` is a here-string and is left alone. Anything this scanner cannot decode with confidence (an ANSI-C escape
    in a delimiter, an unbalanced delimiter quote, a terminator line that never appears) raises Uncertain instead of guessing.
    """
    out, i, q, pending, n = [], 0, None, [], len(command)
    while i < n:
        ch = command[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < n:
                out.append(command[i:i + 2]); i += 2; continue
            if ch == q:
                q = None
            out.append(ch); i += 1; continue
        if ch in ("'", '"'):
            q = ch; out.append(ch); i += 1; continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i:i + 2]); i += 2; continue
        if ch == "#" and (i == 0 or command[i - 1].isspace() or command[i - 1] in ";|&()<>"):
            j = command.find("\n", i); j = n if j < 0 else j
            out.append(command[i:j]); i = j; continue
        if command.startswith("<<<", i):                                         # here-string: an operator with a word operand, no body
            out.append("<<<"); i += 3; continue
        if command.startswith("<<", i):
            k = i + 2; strip_tabs = k < n and command[k] == "-"
            k += 1 if strip_tabs else 0
            out.append(command[i:k]); i = k
            while i < n and command[i] in " \t":
                out.append(command[i]); i += 1
            delim, start = [], i
            while i < n and not command[i].isspace() and command[i] not in ";|&<>()":
                c = command[i]
                if c == "$" and i + 1 < n and command[i + 1] in ("'", '"'):
                    close = command.find(command[i + 1], i + 2)                     # $"..." quotes like "..."; $'...' like '...' unless it holds an escape
                    if command[i + 1] == "'" and (close < 0 or "\\" in command[i + 2:close]):
                        raise Uncertain("ANSI-C escape in a heredoc delimiter")
                    i += 1; continue
                if c in ("'", '"'):
                    j = i + 1
                    while j < n and command[j] != c:                               # shell quote removal: inside double quotes a backslash escapes only $ ` " \\ newline
                        if c == '"' and command[j] == "\\" and j + 1 < n and command[j + 1] in '$`"\\\n':
                            if command[j + 1] != "\n":                             # backslash-newline is a line continuation: both characters vanish
                                delim.append(command[j + 1])
                            j += 2; continue
                        delim.append(command[j]); j += 1
                    if j >= n:
                        raise Uncertain("unbalanced quote in a heredoc delimiter")
                    i = j + 1
                elif c == "\\" and i + 1 < n:
                    if command[i + 1] != "\n":                                     # bare backslash-newline is a line continuation too
                        delim.append(command[i + 1])
                    i += 2
                else:
                    delim.append(c); i += 1
            out.append(command[start:i]); pending.append(("".join(delim), strip_tabs)); continue
        if ch == "\n":
            out.append(ch); i += 1
            for delim, strip_tabs in pending:                                    # bodies follow the line, in operator order
                start, found = i, False
                while i < n:
                    j = command.find("\n", i); end = n if j < 0 else j
                    line = command[i:end]; i = end + 1 if j >= 0 else n
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        found = True; break
                if not found:
                    raise Uncertain("heredoc terminator not found")                 # never guess: a misread delimiter must surface as an advisory, not as a stripped body
            pending = []; continue
        out.append(ch); i += 1
    return "".join(out)


def _redirection(toks, i, out):
    """If toks[i] opens a redirection, record its destination in `out` and return the index after it; otherwise None."""
    text, quoted, op = toks[i]
    if not quoted and not op and DIGITS.match(text) and i + 1 < len(toks) and toks[i + 1][2] and (toks[i + 1][0] in REDIRECT_OPS or toks[i + 1][0].startswith("<")):
        return i + 1
    if op and (text in REDIRECT_OPS or text.startswith("<")):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt and not nxt[2]:
            if text in REDIRECT_OPS and not nxt[0].startswith("&") and nxt[0] != "/dev/null":
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
            at_command_start = True; i += 1; continue
        after = _redirection(toks, i, out)
        if after is not None:
            i = after; continue
        if at_command_start and not quoted and not op and text in ("tee", "cp", "mv"):
            j, args = i + 1, []
            while j < len(toks) and not (toks[j][2] and toks[j][0] in SEPARATORS):
                after = _redirection(toks, j, out)
                if after is not None:
                    j = after; continue
                t, q, o = toks[j]
                if o:
                    j += 1; continue
                if q or not t.startswith("-"):
                    args.append(t)
                j += 1
            if text == "tee":
                out.extend(args)
            elif len(args) >= 2:
                out.append(args[-1])
            at_command_start = False; i = j; continue
        at_command_start = False; i += 1
    return [os.path.normpath(d if os.path.isabs(d) else os.path.join(cwd, d)) for d in out]


if __name__ == "__main__":
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    try:
        print("\n".join(destinations(sys.stdin.read(), cwd)))
    except Uncertain as why:
        print(f"uncertain: {why}", file=sys.stderr); sys.exit(3)
