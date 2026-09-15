"""
Quote-aware shell lexer shared by relay-monitor.py and the relay guard (DD-v295-b2,
plan-3 P5/P6).

lex() returns (text, quoted, operator) triples. Quoted or escaped text is never an
operator; an unquoted '#' at the start of a word discards the rest of the line; newline
is an operator (a command separator).
"""

from __future__ import annotations

SEPARATORS = (";", "&&", "\n")


class Uncertain(Exception):
    """
    The command cannot be parsed with confidence. The CLI exits 3 and the guard raises
    its generic advisory when the text names a relay root.
    """


def strip_heredoc_bodies(command):
    """
    Drop every here-document body (and its terminator line) before lexing: body text is
    data, never a command.

    Mirrors the lexer's quote and escape rules so a quoted `<<` is not an operator;
    `<<`/`<<-` register a pending body whose lines are consumed after the current line
    up to the delimiter (leading tabs stripped for `<<-`); `<<<` is a here-string and is
    left alone. Anything this scanner cannot decode with confidence (an ANSI-C escape in
    a delimiter, an unbalanced delimiter quote, a terminator line that never appears)
    raises Uncertain instead of guessing.
    """
    out, i, q, pending, n = [], 0, None, [], len(command)
    while i < n:
        ch = command[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < n:
                out.append(command[i : i + 2])
                i += 2
                continue
            if ch == q:
                q = None
            out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch == "#" and (
            i == 0 or command[i - 1].isspace() or command[i - 1] in ";|&()<>"
        ):
            j = command.find("\n", i)
            j = n if j < 0 else j
            out.append(command[i:j])
            i = j
            continue
        if command.startswith(
            "<<<", i
        ):  # here-string: an operator with a word operand, no body
            out.append("<<<")
            i += 3
            continue
        if command.startswith("<<", i):
            k = i + 2
            strip_tabs = k < n and command[k] == "-"
            k += 1 if strip_tabs else 0
            out.append(command[i:k])
            i = k
            while i < n and command[i] in " \t":
                out.append(command[i])
                i += 1
            delim, start = [], i
            while i < n and not command[i].isspace() and command[i] not in ";|&<>()":
                c = command[i]
                if c == "$" and i + 1 < n and command[i + 1] in ("'", '"'):
                    close = command.find(
                        command[i + 1],
                        i + 2,
                        # $"..." quotes like "..."; $'...' like '...' unless it holds an
                        # escape
                    )
                    if command[i + 1] == "'" and (
                        close < 0 or "\\" in command[i + 2 : close]
                    ):
                        raise Uncertain("ANSI-C escape in a heredoc delimiter")
                    i += 1
                    continue
                if c in ("'", '"'):
                    j = i + 1
                    while (
                        j < n and command[j] != c
                        # shell quote removal: inside double quotes a backslash escapes
                        # only
                        # $ ` " \\ newline
                    ):
                        if (
                            c == '"'
                            and command[j] == "\\"
                            and j + 1 < n
                            and command[j + 1] in '$`"\\\n'
                        ):
                            if (
                                command[j + 1] != "\n"
                                # backslash-newline is a line continuation: both
                                # characters
                                # vanish
                            ):
                                delim.append(command[j + 1])
                            j += 2
                            continue
                        delim.append(command[j])
                        j += 1
                    if j >= n:
                        raise Uncertain("unbalanced quote in a heredoc delimiter")
                    i = j + 1
                elif c == "\\" and i + 1 < n:
                    if (
                        command[i + 1] != "\n"
                    ):  # bare backslash-newline is a line continuation too
                        delim.append(command[i + 1])
                    i += 2
                else:
                    delim.append(c)
                    i += 1
            out.append(command[start:i])
            pending.append(("".join(delim), strip_tabs))
            continue
        if ch == "\n":
            out.append(ch)
            i += 1
            for (
                delim,
                strip_tabs,
            ) in pending:  # bodies follow the line, in operator order
                start, found = i, False
                while i < n:
                    j = command.find("\n", i)
                    end = n if j < 0 else j
                    line = command[i:end]
                    i = end + 1 if j >= 0 else n
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        found = True
                        break
                if not found:
                    raise Uncertain(
                        "heredoc terminator not found"
                        # never guess: a misread delimiter must surface as an advisory,
                        # not
                        # as a stripped body
                    )
            pending = []
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def lex(command: str):
    tokens, cur, quoted, q, i = [], [], False, None, 0

    def flush():
        nonlocal cur, quoted
        if cur or quoted:
            tokens.append(("".join(cur), quoted, False))
            cur, quoted = [], False

    while i < len(command):
        ch = command[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and i + 1 < len(command):
                cur.append(command[i + 1])
                i += 1
            else:
                cur.append(ch)
        elif ch in ("'", '"'):
            q = ch
            quoted = quoted or (True if not cur else len(cur))
        elif ch == "\\" and i + 1 < len(command):
            quoted = quoted or (True if not cur else len(cur))
            cur.append(command[i + 1])
            i += 1
        elif ch == "#" and not cur and not quoted:
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        elif ch == "\n":
            flush()
            tokens.append(("\n", False, True))
        elif ch.isspace():
            flush()
        elif ch in ";|&":
            flush()
            op = ch
            if ch in "|&" and i + 1 < len(command) and command[i + 1] == ch:
                op = ch * 2
                i += 1
            tokens.append((op, False, True))
        elif ch in "<>":
            flush()
            op = ch
            if i + 1 < len(command) and command[i + 1] in "<>|":
                op += command[i + 1]
                i += 1
            tokens.append((op, False, True))
        elif ch in "()":
            flush()
            tokens.append((ch, False, True))
        else:
            cur.append(ch)
        i += 1
    if q:
        return None
    flush()
    return tokens


def unquoted_prefix(text: str, quoted) -> int:
    return 0 if quoted is True else (len(text) if quoted is False else quoted)


def split_commands(tokens):
    commands, cur = [], []
    for tok in tokens:
        if tok[2] and tok[0] in SEPARATORS:
            commands.append(cur)
            cur = []
        else:
            cur.append(tok)
    commands.append(cur)
    return [c for c in commands if c]
