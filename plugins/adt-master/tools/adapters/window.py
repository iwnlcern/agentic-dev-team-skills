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
import errno
import json
import os
import select
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


def _within_a_second(a: str, b: str) -> bool:
    """Two lstart strings name the same start; the kernel and `ps` may round a start a second apart."""
    try:
        return abs(lstart_epoch(a) - lstart_epoch(b)) <= 1
    except (ValueError, TypeError, AttributeError):
        return True  # uncertainty reads alive


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


def ps_process_table(pid: int, timeout: float = 5.0) -> tuple[int, str, str] | None:
    """(ppid, command name, lstart in UTC) for one pid, from `ps` with `TZ=UTC` and the C locale."""
    env = dict(os.environ, TZ="UTC", LC_ALL="C")
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
            capture_output=True, text=True, env=env, timeout=max(0.01, timeout), check=False,
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


LSTART = "%a %b %d %H:%M:%S %Y"
_LIBPROC: list = []


def _kernel_start_epoch(pid: int) -> int | None:
    """Start time in whole seconds since the epoch, read from the kernel: `proc_pidinfo` on macOS, `/proc` on
    Linux; None when the kernel will not say (no such process, not permitted, another platform)."""
    if pid <= 0:
        return None
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                ticks = int(handle.read().rsplit(")", 1)[1].split()[19])
            with open("/proc/stat", encoding="utf-8") as handle:
                btime = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
            return btime + ticks // os.sysconf("SC_CLK_TCK")  # procps truncates the same way
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    try:
        import ctypes

        if not _LIBPROC:
            class _BsdInfo(ctypes.Structure):  # struct proc_bsdinfo, <sys/proc_info.h>
                _fields_ = [(n, ctypes.c_uint32) for n in (
                    "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "rfu")] + [
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [(n, ctypes.c_uint32) for n in (
                    "nfiles", "pgid", "pjobc", "tdev", "tpgid")] + [
                    ("nice", ctypes.c_int32), ("start_tvsec", ctypes.c_uint64), ("start_tvusec", ctypes.c_uint64)]

            _LIBPROC.extend([ctypes.CDLL("/usr/lib/libproc.dylib"), _BsdInfo])
        lib, info_type = _LIBPROC
        info = info_type()
        filled = lib.proc_pidinfo(pid, 3, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info))  # PROC_PIDTBSDINFO
        if filled != ctypes.sizeof(info) or info.pid != pid:
            return None
        return int(info.start_tvsec)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def process_start(pid: int, timeout: float = 5.0) -> str | None:
    """A process's start as a UTC `ps` lstart string, from the kernel without starting a process (fold 2 G3);
    `ps` (bounded by `timeout`) only where the kernel will not say."""
    epoch = _kernel_start_epoch(pid)
    if epoch is None:
        row = ps_process_table(pid, timeout)
        if row is None:
            return None
        try:
            epoch = lstart_epoch(row[2])
        except (ValueError, TypeError):
            return None
    return time.strftime(LSTART, time.gmtime(epoch))


def process_row(pid: int, timeout: float = 5.0) -> tuple[int, str, str] | None:
    """(ppid, command name, lstart in UTC) for one pid: from `/proc` on Linux, so a host without `ps` still finds
    its window (G-A1 linux leg); elsewhere from `ps`, whose macOS command names the kernel's 16-byte name would
    not match.  `/proc/<pid>/comm` is the name procps prints for `comm`, so both sources give the same row."""
    if not os.path.isdir("/proc/self"):
        return ps_process_table(pid, timeout)
    if pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            stat = handle.read()
        name = stat[stat.index("(") + 1:stat.rindex(")")]
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None
    epoch = _kernel_start_epoch(pid)
    if epoch is None:
        return None
    return ppid, name, time.strftime(LSTART, time.gmtime(epoch))


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
    process_table: ProcessTable = process_row,
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


class ExitWatch:
    """Notices that a window process has exited, without starting a process per poll (fold F6).

    One start-time read at construction (`process_start`) tells a reused pid from the window itself; after that the
    kernel reports the exit: a kqueue `NOTE_EXIT` registration (macOS, BSD) or a pidfd (Linux), each tied
    to the process, not to its number.  Where neither exists, a signal-0 probe reads the pid, which cannot
    see a reuse after construction.  Uncertainty reads alive, as the doorbell's liveness always has.
    """

    def __init__(self, pid: int, proc_start_utc: str | None, *, start_reader: Callable[[int], str | None] = process_start):
        self.pid = pid
        self._kq = None
        self._fd = None
        self._gone = False
        observed = start_reader(pid)
        if observed is not None and proc_start_utc and not _within_a_second(observed, proc_start_utc):
            self._gone = True  # the pid now names another process: the window is gone
            return
        try:
            if hasattr(select, "kqueue"):
                kq = select.kqueue()
                try:
                    kq.control([select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                                              fflags=select.KQ_NOTE_EXIT)], 0, 0)
                except BaseException:
                    kq.close()
                    raise
                self._kq = kq
            elif hasattr(os, "pidfd_open"):
                self._fd = os.pidfd_open(pid)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                self._gone = True

    def gone(self) -> bool:
        if self._gone:
            return True
        try:
            if self._kq is not None:
                self._gone = any(e.fflags & select.KQ_NOTE_EXIT for e in self._kq.control(None, 4, 0))
            elif self._fd is not None:
                self._gone = bool(select.select([self._fd], [], [], 0)[0])
            else:
                os.kill(self.pid, 0)
        except ProcessLookupError:
            self._gone = True
        except OSError:
            pass
        return self._gone

    def close(self) -> None:
        kq, fd, self._kq, self._fd = self._kq, self._fd, None, None
        if kq is not None:
            kq.close()
        if fd is not None:
            os.close(fd)
