"""Host choice and window identity for the relay doorbell (DD-v3-adapter § 4.1, P12).

A *window* is one running host program hosting one agent conversation.
On Codex it is the thread (`CODEX_THREAD_ID`), because one app-server hosts many threads.
On Claude Code it is the agent process: the nearest ancestor with a host record
`~/.claude/sessions/<pid>.json` whose `pid` and `procStart` match that process.
The record is an undocumented host internal (P12); `procStart` is the `ps` lstart format in UTC (K2),
and a record can carry trailing bytes after its one JSON object (K1).
When no ancestor has a record, a structural walk applies: the first non-shell ancestor above the
nearest run of shells, never chosen by host process name.
"""

from __future__ import annotations

import calendar
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

ProcessTable = Callable[[int], "tuple[int, str, str] | None"]

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh"})
MAX_DEPTH = 64


@dataclass(frozen=True)
class Window:
    host: str
    key: str
    pid: int | None
    proc_start_utc: str | None
    thread_id: str | None
    source: Literal["host-record", "host-record-unreadable", "structural", "thread"]


def resolve_host(environ: Mapping[str, str]) -> Literal["claude-code", "codex"]:
    """`ADT_HOST`, else `CODEX_THREAD_ID` present means Codex, else Claude Code (S2).

    A Codex environment can inherit `CLAUDE_CODE_SESSION_ID` from the shell that started the
    app-server, so that variable never decides the host.
    """
    flag = environ.get("ADT_HOST")
    if flag in ("codex", "claude-code"):
        return flag
    if environ.get("CODEX_THREAD_ID"):
        return "codex"
    return "claude-code"


def lstart_epoch(lstart: str) -> int:
    """Seconds since the epoch for a `ps` lstart string read as UTC (`Wed Nov  4 10:02:36 2026`)."""
    return calendar.timegm(time.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y"))


def _same_instant(a: str, b: str) -> bool:
    try:
        return lstart_epoch(a) == lstart_epoch(b)
    except (ValueError, TypeError, AttributeError):
        return False


def read_host_record(path: Path) -> dict | Literal["unreadable"] | None:
    """The first JSON object in a host record; `None` when absent, `"unreadable"` when present but not readable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return "unreadable"
    return obj if isinstance(obj, dict) else "unreadable"


def ps_process_table(pid: int) -> tuple[int, str, str] | None:
    """(ppid, command name, lstart in UTC) for one pid, from `ps` with `TZ=UTC` and the C locale."""
    env = dict(os.environ, TZ="UTC", LC_ALL="C")
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
            capture_output=True, text=True, env=env, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = out.split()
    if len(fields) < 7:
        return None
    try:
        ppid = int(fields[0])
    except ValueError:
        return None
    return ppid, " ".join(out.strip().split(None, 6)[6:]), " ".join(fields[1:6])


def _is_shell(name: str) -> bool:
    return os.path.basename(name).lstrip("-") in SHELLS


def _ancestry(pid: int, process_table: ProcessTable) -> list[tuple[int, str, str]]:
    """[(pid, command name, lstart UTC), …] from `pid` outward, stopping before pid 1."""
    chain, seen = [], set()
    while pid > 1 and pid not in seen and len(chain) < MAX_DEPTH:
        seen.add(pid)
        row = process_table(pid)
        if row is None:
            break
        ppid, name, lstart = row
        chain.append((pid, name, lstart))
        pid = ppid
    return chain


def _claude_window(pid: int, lstart: str, source: str) -> Window:
    try:
        key = "cc-%d-%d" % (pid, lstart_epoch(lstart))
    except (ValueError, TypeError, AttributeError):
        key = "cc-%d-unknown" % pid
    return Window("claude-code", key, pid, " ".join(lstart.split()) if lstart else None, None, source)


def window_identity(
    environ: Mapping[str, str],
    pid: int,
    *,
    sessions_dir: Path | None = None,
    process_table: ProcessTable = ps_process_table,
) -> Window | None:
    """The window `pid` runs in, or `None` when it cannot be determined."""
    if resolve_host(environ) == "codex":
        thread = environ.get("CODEX_THREAD_ID")
        if not thread:
            return None
        return Window("codex", "cx-" + thread, None, None, thread, "thread")
    sessions = Path(sessions_dir) if sessions_dir is not None else Path.home() / ".claude" / "sessions"
    chain = _ancestry(pid, process_table)
    for proc, _name, lstart in chain:
        record = read_host_record(sessions / f"{proc}.json")
        if record is None:
            continue
        if record == "unreadable":
            # K1: a record that exists marks a host process even when it cannot be read; walking on
            # would reach an outer agent process (a nested `claude`) or the structural fallback.
            return _claude_window(proc, lstart, "host-record-unreadable")
        if record.get("pid") == proc and _same_instant(str(record.get("procStart", "")), lstart):
            return _claude_window(proc, lstart, "host-record")
        # a stale record for a reused pid: not this process
    first_shell = next((i for i, (_, name, _) in enumerate(chain) if _is_shell(name)), None)
    if first_shell is None:
        return None
    for proc, name, lstart in chain[first_shell:]:
        if not _is_shell(name):
            return _claude_window(proc, lstart, "structural")
    return None
