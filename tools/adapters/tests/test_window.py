"""Window and host identity (DD-v3-adapter § 4.1, P12; plan PL-v3-adapter-a Task 1)."""
import inspect
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import window  # noqa: E402

UTC_START = "Wed Nov  4 10:02:36 2026"
LOCAL_START = "Wed Nov  4 02:02:36 2026"  # the same instant at UTC-8, as `ps` prints it without TZ=UTC
OTHER_START = "Tue Nov  3 09:00:00 2026"


class Table:
    """A fake process table: pid -> (ppid, command name, lstart in UTC); records every lookup."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, pid):
        self.calls.append(pid)
        return self.rows.get(pid)


class WindowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sessions = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, pid, proc_start=UTC_START, raw=None):
        path = self.sessions / f"{pid}.json"
        if raw is None:
            raw = json.dumps({"pid": pid, "procStart": proc_start, "sessionId": "s-%d" % pid})
        path.write_text(raw)

    def identity(self, table, pid, environ=None):
        return window.window_identity(environ or {}, pid, sessions_dir=self.sessions, process_table=table)


class HostTests(unittest.TestCase):
    def test_adt_host_wins(self):
        env = {"ADT_HOST": "codex", "CLAUDE_CODE_SESSION_ID": "c-1"}
        self.assertEqual(window.resolve_host(env), "codex")
        self.assertEqual(window.resolve_host({"ADT_HOST": "claude-code", "CODEX_THREAD_ID": "t-1"}), "claude-code")

    def test_codex_thread_beats_inherited_claude_var(self):
        env = {"CODEX_THREAD_ID": "t-1", "CLAUDE_CODE_SESSION_ID": "c-1"}
        self.assertEqual(window.resolve_host(env), "codex")
        self.assertEqual(window.resolve_host({"CLAUDE_CODE_SESSION_ID": "c-1"}), "claude-code")


class HostRecordTests(WindowTestCase):
    # chain: python(10) > zsh(11) > bash(12) > zsh(13) > 2.1.318(14) > bg-pty-host(15) > claude daemon(16) > launchd(1)
    CHAIN = {
        10: (11, "Python", UTC_START),
        11: (12, "zsh", UTC_START),
        12: (13, "bash", UTC_START),
        13: (14, "/bin/zsh", UTC_START),
        14: (15, "/Users/u/.local/share/claude/versions/2.1.318", UTC_START),
        15: (16, "claude", UTC_START),
        16: (1, "claude", UTC_START),
    }

    def test_host_record_picks_agent_process(self):
        self.record(14)
        w = self.identity(Table(self.CHAIN), 10)
        self.assertEqual((w.host, w.pid, w.source), ("claude-code", 14, "host-record"))
        self.assertEqual(w.key, "cc-14-%d" % window.lstart_epoch(UTC_START))

    def test_record_with_trailing_bytes_is_read(self):
        body = json.dumps({"pid": 14, "procStart": UTC_START, "sessionId": "s"})
        self.record(14, raw=body + "}")
        self.assertEqual(window.read_host_record(self.sessions / "14.json")["pid"], 14)
        w = self.identity(Table(self.CHAIN), 10)
        self.assertEqual((w.pid, w.source), (14, "host-record"))

    def test_unreadable_record_marks_the_window(self):
        self.record(14, raw="{not json")
        self.record(16)  # an outer process with a good record must not be reached
        self.assertEqual(window.read_host_record(self.sessions / "14.json"), "unreadable")
        w = self.identity(Table(self.CHAIN), 10)
        self.assertEqual((w.pid, w.source), (14, "host-record-unreadable"))

    def test_proc_start_compared_in_utc(self):
        self.record(14, proc_start=UTC_START)
        self.assertEqual(self.identity(Table(self.CHAIN), 10).source, "host-record")
        local_only = {pid: (pp, name, LOCAL_START) for pid, (pp, name, _) in self.CHAIN.items()}
        self.assertNotEqual(self.identity(Table(local_only), 10).source, "host-record")

    def test_reused_pid_record_is_ignored(self):
        self.record(14, proc_start=OTHER_START)
        w = self.identity(Table(self.CHAIN), 10)
        self.assertNotEqual(w.source, "host-record")
        self.record(14, proc_start=UTC_START)  # negative control
        self.assertEqual(self.identity(Table(self.CHAIN), 10).pid, 14)

    def test_absent_record_is_none(self):
        self.assertIsNone(window.read_host_record(self.sessions / "99.json"))


class StructuralTests(WindowTestCase):
    def test_structural_fallback_skips_per_call_shells(self):
        rows = {
            10: (11, "Python", UTC_START),  # agent-side command
            11: (12, "/bin/zsh", UTC_START),  # per-call shell
            12: (13, "claude", UTC_START),  # host
            13: (14, "-zsh", UTC_START),  # login shell
            14: (1, "login", UTC_START),
            20: (21, "Python", UTC_START),  # doorbell
            21: (12, "/bin/zsh", UTC_START),
            30: (31, "Python", UTC_START),  # agent command wrapped twice
            31: (32, "bash", UTC_START),
            32: (12, "/bin/zsh", UTC_START),
        }
        agent, doorbell, wrapped = (self.identity(Table(rows), pid) for pid in (10, 20, 30))
        self.assertEqual((agent.pid, agent.source), (12, "structural"))
        self.assertEqual(doorbell.key, agent.key)
        self.assertEqual(wrapped.key, agent.key)


class CodexTests(WindowTestCase):
    def test_codex_window_is_thread(self):
        table = Table({})
        w = self.identity(table, 10, {"CODEX_THREAD_ID": "01a1-thread", "CLAUDE_CODE_SESSION_ID": "c-1"})
        self.assertEqual((w.host, w.key, w.thread_id, w.source), ("codex", "cx-01a1-thread", "01a1-thread", "thread"))
        self.assertEqual(table.calls, [])


class ExitWatchTests(unittest.TestCase):
    """Fold F6: window liveness without a process spawn per poll."""

    def sleeper(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(timeout=10)))
        return p

    def start_of(self, pid):
        return window.process_start(pid)

    def test_exit_is_seen(self):
        p = self.sleeper()
        watch = window.ExitWatch(p.pid, self.start_of(p.pid))
        self.addCleanup(watch.close)
        self.assertFalse(watch.gone())
        p.kill()
        p.wait(timeout=10)
        end = time.monotonic() + 2.0
        while not watch.gone() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(watch.gone())
        self.assertTrue(watch.gone())  # stays gone once seen

    def test_reused_pid_is_gone_at_once(self):
        p = self.sleeper()
        watch = window.ExitWatch(p.pid, "Tue Nov  3 09:00:00 2026")
        self.addCleanup(watch.close)
        self.assertTrue(watch.gone())

    def test_polls_start_no_process(self):
        p = self.sleeper()
        watch = window.ExitWatch(p.pid, self.start_of(p.pid))
        self.addCleanup(watch.close)
        spawned = []
        real_run, real_popen = subprocess.run, subprocess.Popen
        with mock.patch("subprocess.run", lambda *a, **k: spawned.append(a) or real_run(*a, **k)), \
                mock.patch("subprocess.Popen", lambda *a, **k: spawned.append(a) or real_popen(*a, **k)):
            for _ in range(200):
                self.assertFalse(watch.gone())
        self.assertEqual(spawned, [])



class ProcessStartTests(unittest.TestCase):
    """Fold 2 G3: a process's start time from the kernel, without starting `ps`."""

    def test_start_time_from_the_kernel_starts_no_process(self):
        before = time.time()
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (p.kill(), p.wait(timeout=10)))
        after = time.time()
        spawned = []
        real_run, real_popen = subprocess.run, subprocess.Popen
        with mock.patch("subprocess.run", lambda *a, **k: spawned.append(a) or real_run(*a, **k)), \
                mock.patch("subprocess.Popen", lambda *a, **k: spawned.append(a) or real_popen(*a, **k)):
            start = window.process_start(p.pid)
        self.assertEqual(spawned, [])
        self.assertLessEqual(int(before) - 1, window.lstart_epoch(start))
        self.assertLessEqual(window.lstart_epoch(start), int(after) + 1)

    def test_process_row_reads_the_kernel_where_linux_offers_it(self):
        # G-A1 linux leg: the window walk reads (ppid, name, start) from /proc on Linux, so a host without
        # `ps` still finds its window; macOS keeps `ps` for the walk, whose command names differ from the kernel's
        spawned = []
        real_run = subprocess.run
        with mock.patch("subprocess.run", lambda *a, **k: spawned.append(a) or real_run(*a, **k)):
            row = window.process_row(os.getpid())
        self.assertIsNotNone(row)
        ppid, name, start = row
        self.assertEqual(ppid, os.getppid())
        self.assertTrue(name)
        self.assertEqual(window.lstart_epoch(start), window.lstart_epoch(window.process_start(os.getpid())))
        if os.path.isdir("/proc/self"):
            self.assertEqual(spawned, [])
            with open("/proc/self/comm", encoding="utf-8") as handle:
                self.assertEqual(name, handle.read().strip())
        else:
            self.assertEqual([a[0][0] for a in spawned], ["ps"])

    def test_process_row_of_a_gone_process_is_none(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=10)
        self.assertIsNone(window.process_row(p.pid))

    def test_window_identity_defaults_to_process_row(self):
        self.assertIs(inspect.signature(window.window_identity).parameters["process_table"].default, window.process_row)

    def test_start_time_of_a_gone_process_is_none(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=10)
        self.assertIsNone(window.process_start(p.pid))


if __name__ == "__main__":
    unittest.main()
