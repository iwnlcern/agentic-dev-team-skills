"""Host-log token readers (DD-v3-adapter § 4.6; plan PL-v3-adapter-a Task 5).

Fixtures are synthetic; every content field holds SENTINEL, which must never reach a row or a read path.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import hostlogs  # noqa: E402

FIX = HERE / "fixtures" / "hostlogs"
SENTINEL = "SENTINEL-CONTENT-7f3a"
A = "01a00000-0000-7000-8000-00000000000a"
H = "01a00000-0000-7000-8000-00000000000b"
N = "01a00000-0000-7000-8000-00000000000c"
FK = "01a00000-0000-7000-8000-00000000000d"
X = "01a00000-0000-7000-8000-00000000000e"
NAME_ONLY = "01a00000-0000-7000-8000-00000000000f"


def codex(tid, stamp):
    return FIX / "codex" / f"rollout-2026-11-01T{stamp}-{tid}.jsonl"


class HostLogTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.owners = hostlogs.OwnerMap(self.tmp / "owners.json")

    def tearDown(self):
        self._tmp.cleanup()

    def staged(self, *names):
        """Copy Claude Code fixtures in the given order, so each file's birth time follows that order."""
        out = []
        for name in names:
            dst = self.tmp / "cc" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIX / "claude-code" / name, dst)
            out.append(dst)
            time.sleep(0.02)
        return out

    def rows(self, items):
        return [r for r in items if isinstance(r, hostlogs.TokenRow)]


class ClaudeCodeTests(HostLogTestCase):
    def test_claude_rows_dedupe_by_request_id(self):
        (parent,) = self.staged("sess-b-parent.jsonl")
        rows = self.rows(hostlogs.read_claude_code([parent], self.owners))
        self.assertEqual(sorted(r.turn_id for r in rows), ["req_1", "req_2"])
        one = next(r for r in rows if r.turn_id == "req_1")
        self.assertEqual((one.input, one.cache_read, one.output), (10, 900, 40))

    def test_fork_in_later_run_adds_nothing(self):
        parent, fork = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl")
        first = self.rows(hostlogs.read_claude_code([parent], self.owners))
        self.owners.save()
        later = hostlogs.OwnerMap(self.tmp / "owners.json")
        second = self.rows(hostlogs.read_claude_code([fork], later))
        self.assertEqual({r.turn_id for r in first}, {"req_1", "req_2"})
        self.assertEqual([(r.host_session_id, r.turn_id) for r in second], [("sess-a-fork", "req_3")])

    def test_in_run_owner_is_session_with_earliest_first_line(self):
        # the fork's session id sorts first and its first line carries the same timestamp (as measured);
        # the parent's file was created first, so the parent owns the shared requests
        parent, fork = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl")
        rows = self.rows(hostlogs.read_claude_code([fork, parent], self.owners))
        owners = {r.turn_id: r.host_session_id for r in rows}
        self.assertEqual(sorted(r.turn_id for r in rows), ["req_1", "req_2", "req_3"])  # each request counted once
        if hasattr(parent.stat(), "st_birthtime"):
            self.assertEqual(owners, {"req_1": "sess-b-parent", "req_2": "sess-b-parent", "req_3": "sess-a-fork"})
        # without birth times the fallback (first timestamp, then session id) cannot tell a fork from its
        # parent; counts stay right and only the copied history's attribution can move (documented)

    def test_partial_last_line_is_skipped_not_fatal(self):
        (partial,) = self.staged("partial.jsonl")
        rows = self.rows(hostlogs.read_claude_code([partial], self.owners))
        self.assertEqual([r.turn_id for r in rows], ["req_p1"])

    def test_claude_column_mapping(self):
        (parent,) = self.staged("sess-b-parent.jsonl")
        row = next(r for r in self.rows(hostlogs.read_claude_code([parent], self.owners)) if r.turn_id == "req_1")
        self.assertEqual(
            (row.input, row.cache_read, row.cache_write, row.output, row.reasoning_output, row.reasoning_in_output),
            (10, 900, 50, 40, 15, True),
        )
        plain = next(r for r in self.rows(hostlogs.read_claude_code([parent], self.owners)) if r.turn_id == "req_2")
        self.assertIsNone(plain.reasoning_output)


class CodexTests(HostLogTestCase):
    def test_codex_bak_and_missing_record(self):
        bak = pathlib.Path(str(codex(A, "10-00-00")) + ".bak")
        items = hostlogs.read_codex([bak, codex(N, "10-06-00")])
        self.assertEqual(self.rows(items), [])
        missing = [i for i in items if isinstance(i, hostlogs.Missing)]
        self.assertEqual([(pathlib.Path(m.path).name, m.reason) for m in missing], [(codex(N, "10-06-00").name, "no token_usage_record")])

    def test_codex_column_mapping(self):
        (row,) = self.rows(hostlogs.read_codex([codex(A, "10-00-00")]))
        self.assertEqual(
            (row.input, row.cache_read, row.cache_write, row.output, row.reasoning_output, row.reasoning_in_output),
            (200, 800, 0, 60, 20, True),
        )
        self.assertEqual(row.input + row.cache_read, 1000)  # negative control: the record's input_tokens
        self.assertEqual((row.host_session_id, row.turn_id), (A, "resp_1"))

    def test_codex_forked_rollout_keys_to_own_thread(self):
        (row,) = self.rows(hostlogs.read_codex([codex(FK, "10-07-00")]))
        self.assertEqual(row.host_session_id, FK)

    def test_codex_renamed_rollout_keys_to_the_records_thread(self):
        (row,) = self.rows(hostlogs.read_codex([codex(NAME_ONLY, "10-08-00")]))
        self.assertEqual(row.host_session_id, X)  # the thread its records carry, never the file name


class SharedTests(HostLogTestCase):
    def test_helper_rows_carry_parent(self):
        (helper,) = self.staged("sess-b-parent/subagents/agent-x1.jsonl")
        (cc,) = self.rows(hostlogs.read_claude_code([helper], self.owners))
        self.assertEqual((cc.host_session_id, cc.parent_host_session_id), ("sess-b-parent/agent-x1", "sess-b-parent"))
        (cx,) = self.rows(hostlogs.read_codex([codex(H, "10-05-00")]))
        self.assertEqual((cx.host_session_id, cx.parent_host_session_id), (H, A))

    def test_never_reads_content(self):
        paths = []
        real_dig = hostlogs._dig

        def recording_dig(obj, *path):
            paths.append(path)
            return real_dig(obj, *path)

        cc = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl", "partial.jsonl", "sess-b-parent/subagents/agent-x1.jsonl")
        cx = sorted((FIX / "codex").iterdir())
        with mock.patch.object(hostlogs, "_dig", recording_dig):
            items = hostlogs.read_claude_code(cc, self.owners) + hostlogs.read_codex(cx)
        self.assertNotIn(SENTINEL, json.dumps([vars(i) for i in items]))
        body_keys = {"content", "text", "thinking", "tool_use", "tool_result", "toolUseResult", "base_instructions"}
        touched = {p for p in paths if body_keys & set(p) or (p and p[0] == "message" and p[-1] in ("input", "output"))}
        self.assertEqual(touched, set())
        self.assertTrue(paths)  # the instrument saw the reads

    def test_rows_match_engine_key_shape(self):
        cc = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl")
        rows = self.rows(hostlogs.read_claude_code(cc, self.owners) + hostlogs.read_codex(sorted((FIX / "codex").iterdir())))
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(r.host_session_id and r.turn_id, r)
            for n in (r.input, r.output, r.cache_read, r.cache_write):
                self.assertIsInstance(n, int)
                self.assertGreaterEqual(n, 0)
            self.assertEqual(r.source, "host-log")

    def test_cli_prints_one_object_per_line(self):
        out = subprocess.run(
            [sys.executable, str(HERE.parent / "hostlogs.py"), "read", "--host", "codex", str(codex(A, "10-00-00"))],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual([json.loads(line)["turn_id"] for line in out], ["resp_1"])


if __name__ == "__main__":
    unittest.main()
