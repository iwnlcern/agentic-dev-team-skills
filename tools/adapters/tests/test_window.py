"""Window and host identity (DD-v3-adapter § 4.1, P12; plan PL-v3-adapter-a Task 1)."""
import json
import pathlib
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
