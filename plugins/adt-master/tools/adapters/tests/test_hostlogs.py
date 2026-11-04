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
F1 = "01a00000-0000-7000-8000-000000000011"
F2 = "01a00000-0000-7000-8000-000000000012"
F3 = "01a00000-0000-7000-8000-000000000013"
P3 = "01a00000-0000-7000-8000-000000000014"


def births(**by_stem):
    """An injected birth-time source: file stem -> birth time (absent stems have none, as on Linux)."""
    return lambda path: by_stem.get(pathlib.Path(path).stem.replace("-", "_"))


class RecordingDict(dict):
    """A decoded line that records every key read, plain access included, under its full path."""

    def __init__(self, data, path, log):
        super().__init__(data)
        self._path, self._log = path, log

    def _wrap(self, key, value):
        if isinstance(value, dict):
            return RecordingDict(value, self._path + (key,), self._log)
        if isinstance(value, list):
            return [self._wrap(key, v) for v in value]
        return value

    def __getitem__(self, key):
        self._log.append(self._path + (key,))
        return self._wrap(key, super().__getitem__(key))

    def get(self, key, default=None):
        self._log.append(self._path + (key,))
        return self._wrap(key, super().get(key, default))

    def __iter__(self):
        self._log.append(self._path + ("*",))
        return super().__iter__()

    def keys(self):
        self._log.append(self._path + ("*",))
        return super().keys()

    def values(self):
        self._log.append(self._path + ("*",))
        return [self._wrap(k, v) for k, v in super().items()]

    def items(self):
        self._log.append(self._path + ("*",))
        return [(k, self._wrap(k, v)) for k, v in super().items()]


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
        # the fork's first usage line carries the parent's timestamp (as measured), so first lines tie;
        # the injected birth times say the parent's file came first, so the parent owns the shared requests
        parent, fork = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl")
        birth = births(sess_b_parent=100.0, sess_a_fork=200.0)
        rows = self.rows(hostlogs.read_claude_code([fork, parent], self.owners, birth=birth))
        owners = {r.turn_id: r.host_session_id for r in rows}
        self.assertEqual(sorted(r.turn_id for r in rows), ["req_1", "req_2", "req_3"])  # each request counted once
        self.assertEqual(owners, {"req_1": "sess-b-parent", "req_2": "sess-b-parent", "req_3": "sess-a-fork"})

    def test_owner_without_birth_times_is_smallest_session_id(self):
        # no birth source (Linux): first lines tie, so the smaller session id owns the shared requests
        parent, fork = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl")
        rows = self.rows(hostlogs.read_claude_code([parent, fork], self.owners, birth=births()))
        self.assertEqual({r.turn_id: r.host_session_id for r in rows},
                         {"req_1": "sess-a-fork", "req_2": "sess-a-fork", "req_3": "sess-a-fork"})

    def test_earliest_first_usage_line_beats_birth_time(self):
        # p12 shape: a fork copies a later window of its parent, then the parent's file is re-created
        # (restore or copy), so its birth time is later; the parent's own first usage line is still earlier
        cc = self.tmp / "cc"
        cc.mkdir()

        def line(sess, ts, rid):
            return json.dumps({"type": "assistant", "sessionId": sess, "timestamp": ts, "requestId": rid,
                               "message": {"id": "m" + rid, "content": SENTINEL,
                                           "usage": {"input_tokens": 1, "output_tokens": 1}}},
                              separators=(",", ":")) + "\n"

        parent, fork = cc / "b-parent.jsonl", cc / "a-fork.jsonl"
        parent.write_text(line("b-parent", "2026-09-23T11:00:00Z", "r0") + line("b-parent", "2026-10-23T23:20:00Z", "r1")
                          + line("b-parent", "2026-10-23T23:21:00Z", "r2"))
        fork.write_text(line("a-fork", "2026-10-23T23:20:00Z", "r1") + line("a-fork", "2026-10-23T23:21:00Z", "r2")
                        + line("a-fork", "2026-11-04T10:00:00Z", "r3"))
        birth = births(a_fork=100.0, b_parent=200.0)
        rows = self.rows(hostlogs.read_claude_code([parent, fork], self.owners, birth=birth))
        self.assertEqual({r.turn_id: r.host_session_id for r in rows},
                         {"r0": "b-parent", "r1": "b-parent", "r2": "b-parent", "r3": "a-fork"})

    def test_partial_last_line_is_skipped_not_fatal(self):
        (partial,) = self.staged("partial.jsonl")
        rows = self.rows(hostlogs.read_claude_code([partial], self.owners))
        self.assertEqual([r.turn_id for r in rows], ["req_p1"])

    def test_non_string_session_id_falls_back_to_file_stem(self):
        path = self.tmp / "cc" / "stem-session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "assistant", "sessionId": 7, "timestamp": "2026-11-01T10:00:00Z",
                                    "requestId": "req_t", "message": {"usage": {"input_tokens": 1}}},
                                   separators=(",", ":")) + "\n")
        (row,) = self.rows(hostlogs.read_claude_code([path], self.owners))
        self.assertEqual(row.host_session_id, "stem-session")

    def test_deeply_nested_line_is_skipped_not_fatal(self):
        path = self.tmp / "cc" / "deep.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        deep = '{"type":"assistant","x":' + "[" * 200000 + "]" * 200000 + "}\n"
        ok = json.dumps({"type": "assistant", "sessionId": "deep", "timestamp": "2026-11-01T10:00:00Z",
                         "requestId": "req_d", "message": {"usage": {"input_tokens": 1}}}, separators=(",", ":")) + "\n"
        path.write_text(deep + ok)
        self.assertEqual([r.turn_id for r in self.rows(hostlogs.read_claude_code([path], self.owners))], ["req_d"])

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

    def test_codex_record_from_another_thread_is_skipped(self):
        # DD § 4.6 own-thread records only: a copy of thread A's record inside F1's rollout is not F1's
        items = hostlogs.read_codex([codex(F1, "10-09-00")])
        self.assertEqual([(r.host_session_id, r.turn_id) for r in self.rows(items)], [(F1, "resp_o1")])

    def test_codex_record_without_thread_keys_to_first_session_meta(self):
        (row,) = self.rows(hostlogs.read_codex([codex(F2, "10-10-00")]))
        self.assertEqual((row.host_session_id, row.turn_id), (F2, "resp_n1"))

    def test_codex_forked_helper_keeps_its_parent(self):
        (row,) = self.rows(hostlogs.read_codex([codex(F3, "10-11-00")]))
        self.assertEqual((row.host_session_id, row.parent_host_session_id), (F3, P3))


class SharedTests(HostLogTestCase):
    def test_helper_rows_carry_parent(self):
        (helper,) = self.staged("sess-b-parent/subagents/agent-x1.jsonl")
        (cc,) = self.rows(hostlogs.read_claude_code([helper], self.owners))
        self.assertEqual((cc.host_session_id, cc.parent_host_session_id), ("sess-b-parent/agent-x1", "sess-b-parent"))
        (cx,) = self.rows(hostlogs.read_codex([codex(H, "10-05-00")]))
        self.assertEqual((cx.host_session_id, cx.parent_host_session_id), (H, A))

    def test_never_reads_content(self):
        # every parsed line is a RecordingDict, so any key read is seen, through _dig or plain access
        paths = []
        real_decode = hostlogs._decode

        def recording_decode(raw):
            obj = real_decode(raw)
            return RecordingDict(obj, (), paths) if isinstance(obj, dict) else obj

        cc = self.staged("sess-b-parent.jsonl", "sess-a-fork.jsonl", "partial.jsonl", "sess-b-parent/subagents/agent-x1.jsonl")
        cx = sorted((FIX / "codex").iterdir())
        with mock.patch.object(hostlogs, "_decode", recording_decode):
            items = hostlogs.read_claude_code(cc, self.owners) + hostlogs.read_codex(cx)
        self.assertNotIn(SENTINEL, json.dumps([vars(i) for i in items]))
        body_keys = {"content", "text", "thinking", "tool_use", "tool_result", "toolUseResult", "base_instructions"}
        touched = {p for p in paths
                   if body_keys & set(p) or (p and p[0] == "message" and p[-1] in ("input", "output", "*"))
                   or p in (("*",), ("payload", "*"))}
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
