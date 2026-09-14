"""Quote-aware shell lexer shared by relay-monitor.py and the relay guard (DD-v295-b2, plan-3 P5/P6).

lex() returns (text, quoted, operator) triples. Quoted or escaped text is never an operator; an unquoted
'#' at the start of a word discards the rest of the line; newline is an operator (a command separator).
"""
from __future__ import annotations

SEPARATORS = (";", "&&", "\n")


def lex(command: str):
    tokens, cur, quoted, q, i = [], [], False, None, 0

    def flush():
        nonlocal cur, quoted
        if cur or quoted:
            tokens.append(("".join(cur), quoted, False)); cur, quoted = [], False

    while i < len(command):
        ch = command[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and i + 1 < len(command):
                cur.append(command[i + 1]); i += 1
            else:
                cur.append(ch)
        elif ch in ("'", '"'):
            q = ch; quoted = quoted or (True if not cur else len(cur))
        elif ch == "\\" and i + 1 < len(command):
            quoted = quoted or (True if not cur else len(cur)); cur.append(command[i + 1]); i += 1
        elif ch == "#" and not cur and not quoted:
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        elif ch == "\n":
            flush(); tokens.append(("\n", False, True))
        elif ch.isspace():
            flush()
        elif ch in ";|&":
            flush(); op = ch
            if ch in "|&" and i + 1 < len(command) and command[i + 1] == ch:
                op = ch * 2; i += 1
            tokens.append((op, False, True))
        elif ch in "<>":
            flush(); op = ch
            if i + 1 < len(command) and command[i + 1] in "<>|":
                op += command[i + 1]; i += 1
            tokens.append((op, False, True))
        elif ch in "()":
            flush(); tokens.append((ch, False, True))
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
            commands.append(cur); cur = []
        else:
            cur.append(tok)
    commands.append(cur)
    return [c for c in commands if c]
