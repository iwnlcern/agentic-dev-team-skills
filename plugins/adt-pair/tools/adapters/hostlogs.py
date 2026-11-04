"""Host-log token readers (DD-v3-adapter § 4.6, O9).

Reads token counts and identifiers from Claude Code session logs and Codex rollouts; never message content.
Each line is checked for its type marker before it is decoded, so user and tool lines are never decoded,
and every field is read through `_dig`, named paths only.

Column meanings (measured, plan PL-v3-adapter-a M1): `input` is uncached input on both hosts — Claude Code
reports it that way, Codex includes cache reads in `input_tokens`, so Codex `input = input_tokens −
cached_input_tokens`; `output` includes reasoning on both hosts, so `reasoning_in_output` is true and
`reasoning_output` is the host's split figure where it reports one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

_decode = json.loads


def _dig(obj, *path):
    """The value at a named key path, or None; the only way this module reads a decoded line."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


@dataclass(frozen=True)
class TokenRow:
    host_kind: str
    host_session_id: str
    turn_id: str
    parent_host_session_id: str | None
    host_time_utc: str
    input: int
    output: int
    reasoning_output: int | None
    reasoning_in_output: bool
    cache_read: int
    cache_write: int
    source: str = "host-log"


@dataclass(frozen=True)
class Missing:
    path: str
    reason: str


class OwnerMap:
    """Which session owns each Claude Code `requestId` (forks copy earlier records under a new session id).

    The first owner written keeps a request in every later run; within one run the owner is the session
    that started first (the order key given to `claim`), ties broken by the smaller session id.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._written = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}
        self._pending: dict[str, tuple[str, str]] = {}

    def owner(self, request_id: str) -> str | None:
        if request_id in self._written:
            return self._written[request_id]
        pending = self._pending.get(request_id)
        return pending[1] if pending else None

    def claim(self, request_id: str, session_id: str, session_first_line_utc: str) -> str:
        """Offer `session_id` as owner; `session_first_line_utc` is the session's start order key."""
        if request_id in self._written:
            return self._written[request_id]
        candidate = (session_first_line_utc or "~", session_id)
        current = self._pending.get(request_id)
        if current is None or candidate < current:
            self._pending[request_id] = candidate
        return self._pending[request_id][1]

    def save(self) -> None:
        for rid, (_, session) in self._pending.items():
            self._written.setdefault(rid, session)
        self._pending.clear()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".owners.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._written, handle, sort_keys=True)
        os.replace(tmp, self.path)


def _lines(path: Path, markers: tuple[bytes, ...]):
    """Decoded lines that carry one of `markers`; other lines are never decoded; a partial line is skipped."""
    with open(path, "rb") as handle:
        for raw in handle:
            if not any(marker in raw for marker in markers):
                continue
            try:
                yield _decode(raw)
            except ValueError:
                continue


def _start_order(path: Path, first_ts: str | None) -> str:
    """When a session file began: its birth time where the filesystem records one, else its first timestamp.

    A fork copies earlier records with their original timestamps, first line included (measured), so only
    the file's creation separates a fork from its parent.
    """
    try:
        birth = os.stat(path).st_birthtime
    except (AttributeError, OSError):
        birth = None
    return "%020.6f" % birth if birth is not None else "~" + (first_ts or "")


def _claude_session(path: Path) -> tuple[str | None, str | None]:
    """(host_session_id override, parent session) for a helper file `<session>/subagents/agent-<id>.jsonl`."""
    if path.parent.name == "subagents":
        parent = path.parent.parent.name
        return f"{parent}/{path.stem}", parent
    return None, None


def read_claude_code(paths: Iterable[Path], owners: OwnerMap) -> list[TokenRow | Missing]:
    files = []
    for path in map(Path, paths):
        helper_id, parent = _claude_session(path)
        session, first_ts, turns = helper_id, None, {}
        try:
            for line in _lines(path, (b'"type":"assistant"',)):
                if _dig(line, "type") != "assistant":
                    continue
                usage = _dig(line, "message", "usage")
                turn = _dig(line, "requestId") or _dig(line, "message", "id")
                if not isinstance(usage, dict) or not isinstance(turn, str):
                    continue
                stamp = _dig(line, "timestamp")
                first_ts = first_ts or stamp
                session = session or _dig(line, "sessionId") or path.stem
                turns.setdefault(turn, (usage, stamp))
        except OSError as exc:
            files.append(Missing(str(path), f"unreadable: {type(exc).__name__}"))
            continue
        if session is None:
            files.append(Missing(str(path), "no assistant usage"))
            continue
        files.append((path, session, parent, _start_order(path, first_ts), turns))
    for entry in files:
        if not isinstance(entry, Missing):
            _, session, _, order, turns = entry
            for turn in turns:
                owners.claim(turn, session, order)
    out: list[TokenRow | Missing] = []
    emitted = set()
    for entry in files:
        if isinstance(entry, Missing):
            out.append(entry)
            continue
        _, session, parent, _, turns = entry
        for turn, (usage, stamp) in turns.items():
            if turn in emitted or owners.owner(turn) != session:
                continue
            emitted.add(turn)
            thinking = _dig(usage, "output_tokens_details", "thinking_tokens")
            out.append(TokenRow(
                host_kind="claude-code", host_session_id=session, turn_id=turn, parent_host_session_id=parent,
                host_time_utc=stamp if isinstance(stamp, str) else "",
                input=_count(_dig(usage, "input_tokens")), output=_count(_dig(usage, "output_tokens")),
                reasoning_output=_count(thinking) if thinking is not None else None, reasoning_in_output=True,
                cache_read=_count(_dig(usage, "cache_read_input_tokens")),
                cache_write=_count(_dig(usage, "cache_creation_input_tokens")),
            ))
    return out


def read_codex(paths: Iterable[Path]) -> list[TokenRow | Missing]:
    out: list[TokenRow | Missing] = []
    seen = set()
    for path in map(Path, paths):
        if path.name.endswith(".bak"):
            continue
        own, parent, records = None, None, 0
        try:
            for line in _lines(path, (b'"session_meta"', b'"token_usage_record"')):
                kind = _dig(line, "type")
                if kind == "session_meta":
                    if own is None:  # a forked rollout's second session_meta is not its own (measured)
                        own = _dig(line, "payload", "id")
                        parent = _dig(line, "payload", "source", "subagent", "thread_spawn", "parent_thread_id")
                    continue
                if kind != "token_usage_record":
                    continue
                usage = _dig(line, "payload", "usage")
                turn = _dig(line, "payload", "response_id")
                thread = _dig(line, "payload", "thread_id") or own
                if not isinstance(usage, dict) or not isinstance(turn, str) or not isinstance(thread, str):
                    continue
                records += 1
                if (thread, turn) in seen:
                    continue
                seen.add((thread, turn))
                total_in, cached = _count(_dig(usage, "input_tokens")), _count(_dig(usage, "cached_input_tokens"))
                reasoning = _dig(usage, "reasoning_output_tokens")
                stamp = _dig(line, "timestamp")
                out.append(TokenRow(
                    host_kind="codex", host_session_id=thread, turn_id=turn,
                    parent_host_session_id=parent if thread == own else None,
                    host_time_utc=stamp if isinstance(stamp, str) else "",
                    input=max(0, total_in - cached), output=_count(_dig(usage, "output_tokens")),
                    reasoning_output=_count(reasoning) if reasoning is not None else None, reasoning_in_output=True,
                    cache_read=cached, cache_write=_count(_dig(usage, "cache_write_input_tokens")),
                ))
        except OSError as exc:
            out.append(Missing(str(path), f"unreadable: {type(exc).__name__}"))
            continue
        if records == 0:
            out.append(Missing(str(path), "no token_usage_record"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hostlogs.py", description="Token counts from host logs (token fields only).")
    sub = parser.add_subparsers(dest="mode", required=True)
    read = sub.add_parser("read")
    read.add_argument("--host", choices=("claude-code", "codex"), required=True)
    read.add_argument("--owners", help="persistent requestId owner map (Claude Code)")
    read.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    if args.host == "codex":
        items = read_codex(args.paths)
    else:
        owners = OwnerMap(Path(args.owners) if args.owners else Path(tempfile.gettempdir()) / "hostlogs-owners-unsaved.json")
        items = read_claude_code(args.paths, owners)
        if args.owners:
            owners.save()
    for item in items:
        print(json.dumps({"kind": "row" if isinstance(item, TokenRow) else "missing", **asdict(item)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
