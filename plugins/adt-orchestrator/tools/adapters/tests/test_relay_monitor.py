# tools/adapters/tests/test_relay_monitor.py
import base64
import contextlib
import errno
import importlib.util
import io
import json
import os
import pathlib
import select
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "relay-monitor.py"
REPO = HERE.parent.parent.parent


def load_script():
    spec = importlib.util.spec_from_file_location("relay_monitor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["relay_monitor"] = module
    spec.loader.exec_module(module)
    return module


def ns(**kw):
    """Argument namespace for mode functions (no class-scope lookups)."""
    base = dict(
        host="auto",
        session=None,
        plugin_root=None,
        anchor=None,
        manual=False,
        root=None,
        deadline=5.0,
        since=None,
        notifier="none",
        after=None,
        limit=50,
        token=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


HEADER = (
    "| time | phase | role | dispatch | parent | from | t"
    "o | cc | status | file |\n|---|---|---|---|---|---|--"
    "-|---|---|---|\n"
)


def row(
    n, frm="a.planner", to="orch.orchestrator-planner", cc="—", file=None, status="—"
):
    return (
        f"| 20260912-{n:06d} | SITREP | Pair Planner | d{n} | "
        f"— | {frm} | {to} | {cc} | {status} | "
        f"{file or f'lane/r{n}.md'} "
        f"|\n"
    )


def write_index(path, rows_text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HEADER + rows_text, encoding="utf-8")
    os.replace(tmp, path)


class TmpEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(
            os.environ,
            {"TMPDIR": str(self.base), "ADT_PACE_WINDOW": "0.5", "ADT_PACE_LINES": "5"},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        for k in (
            "ADT_SEAT",
            "ADT_RELAY_ROOT",
            "ADT_RELAY_ANCHOR",
            "ADT_HOST",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "PLUGIN_ROOT",
            "CLAUDE_PROJECT_DIR",
            "CLAUDE_PLUGIN_ROOT",
        ):
            os.environ.pop(k, None)
        self.rm = load_script()
        self.root = self.base / "sprint" / ".relays" / "v1"
        (self.root / ".engine" / "seats" / "a.planner").mkdir(parents=True)
        (self.root / "lane").mkdir()
        self.index = self.root / "INDEX.md"


class SessionIdentityTests(TmpEnv):
    def test_invalid_storage_ids_are_rejected_before_filesystem_access(self):
        invalid = (
            "../../escaped-note",
            "",
            "../x",
            "a/b",
            "a\\b",
            "a..b",
            "a b",
            "$(x)",
            "é",
            "x" * 129,
            None,
            42,
        )
        for session in invalid:
            for constructor in (self.rm.NoteStore, self.rm.LeaderLock):
                with self.subTest(session=session, store=constructor.__name__):
                    with self.assertRaisesRegex(ValueError, "invalid-session-id"):
                        constructor(session)
        self.assertFalse((self.base / "adt-relay-monitor").exists())

    def test_normal_session_ids_store_inside_note_directory(self):
        for session in (
            "sess-1",
            "019c6e27-e55b-73d1-87d8-4e01f1f75043",
            "thread_A.1",
            "x" * 128,
        ):
            with self.subTest(session=session):
                store = self.rm.NoteStore(session)
                store.update(lambda note: note.update({"seat": "a.planner"}))
                self.assertEqual(
                    store.path.resolve().parent,
                    (self.base / "adt-relay-monitor").resolve(),
                )
                self.assertEqual(store.load()["seat"], "a.planner")

    def invalid_entry(self, mode, session, source):
        with tempfile.TemporaryDirectory(dir=self.base) as scratch:
            scratch = pathlib.Path(scratch)
            tmpdir = scratch / "one" / "two"
            tmpdir.mkdir(parents=True)
            before = set(scratch.rglob("*"))
            env = dict(os.environ, TMPDIR=str(tmpdir))
            cmd = [sys.executable, str(SCRIPT), mode, "--host", "codex"]
            payload = {
                "hook_event_name": "PostToolUse",
                "tool_input": {"command": "tools/relay submit d.md | head"},
                "tool_response": {"stdout": ""},
            }
            if source == "payload":
                payload["session_id"] = session
                env["CLAUDE_CODE_SESSION_ID"] = "valid-fallback"
            elif source == "flag":
                cmd.extend(["--session", session])
                env["CLAUDE_CODE_SESSION_ID"] = "valid-fallback"
            else:
                env[source] = session
            if mode == "bind" and source != "payload":
                cmd.extend(
                    [
                        "--manual",
                        "--root",
                        str(self.root),
                        "--anchor",
                        "lane/r1.md",
                    ]
                )
            try:
                result = subprocess.run(
                    cmd,
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=2,
                )
            except subprocess.TimeoutExpired:
                self.fail("invalid session entry did not refuse to start")
            self.assertEqual(set(scratch.rglob("*")), before)
            return result

    def test_invalid_hook_sessions_exit_zero_without_writes(self):
        for mode in ("bind", "drain", "session-start"):
            for session in ("../../escaped-note", ""):
                for source in (
                    "payload",
                    "flag",
                    "CLAUDE_CODE_SESSION_ID",
                    "CODEX_THREAD_ID",
                ):
                    with self.subTest(mode=mode, session=session, source=source):
                        result = self.invalid_entry(mode, session, source)
                        if mode == "bind" and source != "payload":
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertIn("invalid-session-id", result.stderr)
                        else:
                            self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_cli_sessions_report_reason_and_refuse(self):
        for mode in ("status", "follow", "replay"):
            for session in ("../../escaped-note", ""):
                for source in (
                    "flag",
                    "CLAUDE_CODE_SESSION_ID",
                    "CODEX_THREAD_ID",
                ):
                    with self.subTest(mode=mode, session=session, source=source):
                        result = self.invalid_entry(mode, session, source)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("invalid-session-id", result.stderr)


class NoteStoreTests(TmpEnv):
    def test_note_dir_is_private_and_under_tmpdir(self):
        d = self.rm.note_dir()
        self.assertEqual(d, self.base / "adt-relay-monitor")
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")

    def test_load_returns_defaults_when_absent(self):
        note = self.rm.NoteStore("sess-1").load()
        self.assertEqual(
            (note["binding_gen"], note["phase"], note["progress"], note["frame"]),
            (0, "not-started", {}, None),
        )

    def test_save_is_atomic_and_private(self):
        store = self.rm.NoteStore("sess-1")
        with store.state_lock():
            note = store.load()
            note["seat"] = "a.planner"
            store.save(note)
        self.assertEqual(oct(store.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(store.path.read_text())["seat"], "a.planner")
        self.assertEqual(
            [p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")], []
        )

    def test_update_is_read_modify_write_under_lock(self):
        store = self.rm.NoteStore("sess-1")
        store.update(lambda n: n.__setitem__("seat", "a.planner"))
        store.update(lambda n: n.__setitem__("root", "/r"))
        note = store.load()
        self.assertEqual((note["seat"], note["root"]), ("a.planner", "/r"))

    def test_observer_reports_one_event_per_acquisition(self):
        store = self.rm.NoteStore("sess-1")
        seen = []
        store.observer = lambda ev: seen.append(ev)
        with store.state_lock():
            pass
        self.assertEqual(seen, ["lock-attempt"])  # uncontended: one event
        holder = self.rm.NoteStore("sess-1")
        seen.clear()
        with holder.state_lock():
            with self.assertRaises(self.rm.LockTimeout):
                with store.state_lock(
                    timeout=0.15
                ):  # contended: several 10 ms retries, still one event
                    pass
        self.assertEqual(seen, ["lock-attempt"])

    def test_state_lock_is_bounded(self):
        store = self.rm.NoteStore("sess-1")
        other = self.rm.NoteStore("sess-1")
        with store.state_lock():
            t0 = time.monotonic()
            with self.assertRaises(self.rm.LockTimeout):
                with other.state_lock(timeout=0.2):
                    pass
            self.assertLess(time.monotonic() - t0, 1.0)

    def test_session_sources_in_order(self):
        rm = self.rm
        with mock.patch.dict(
            os.environ, {"CLAUDE_CODE_SESSION_ID": "c", "CODEX_THREAD_ID": "x"}
        ):
            self.assertEqual(rm.resolve_session(ns(session="flag")), "flag")
            self.assertEqual(rm.resolve_session(ns()), "c")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "x"}):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertEqual(rm.resolve_session(ns()), "x")
        self.assertIsNone(rm.resolve_session(ns()))


class LeaderLockTests(TmpEnv):
    def test_probe_out_of_range_pid_reads_dead(self):
        for pid in (2**31, 2**63, -(2**63)-1):
            with self.subTest(pid=pid):
                self.assertEqual(self.rm.process_probe(pid), "dead")

    def test_probe_eperm_reads_alive(self):
        probe_os = types.SimpleNamespace(
            kill=mock.Mock(side_effect=PermissionError(errno.EPERM, "denied"))
        )
        with mock.patch.object(self.rm, "os", probe_os):
            self.assertEqual(self.rm.process_probe(os.getpid()), "alive")

    def test_second_acquire_blocks_probe_reports_held_and_instance_is_readable(self):
        a = self.rm.LeaderLock("s")
        b = self.rm.LeaderLock("s")
        self.assertTrue(a.acquire(blocking=False))
        a.stamp("inst-a")
        self.assertFalse(b.acquire(blocking=False))
        self.assertEqual(b.holder_instance(), "inst-a")
        a.release()
        self.assertTrue(b.acquire(blocking=False))
        b.release()

    def test_leader_record_matches_live_process(self):
        rec = self.rm.leader_record()
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(self.rm.process_liveness(rec), "alive")
        self.assertEqual(
            self.rm.process_liveness({**rec, "start_time": "1970-01-01 00:00:00"}),
            "dead",
        )
        self.assertEqual(self.rm.process_liveness({**rec, "pid": 2**22 - 1}), "dead")
        for pid in (0, -1, True, "invalid"):
            with self.subTest(pid=pid):
                self.assertEqual(self.rm.process_liveness({"pid": pid}), "dead")

    def test_probe_classifies_by_errno(self):
        for error, expected in (
            (None, "alive"),
            (errno.EPERM, "alive"),
            (errno.ESRCH, "dead"),
            (errno.EINVAL, "unknown"),
        ):
            with self.subTest(error=error):
                side_effect = None if error is None else OSError(error, "probe failed")
                with mock.patch.object(self.rm.os, "kill", side_effect=side_effect):
                    self.assertEqual(self.rm.process_probe(os.getpid()), expected)

    def test_absent_process_reads_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=5)
        self.assertEqual(self.rm.process_liveness({"pid": child.pid}), "dead")

    def test_start_time_evidence(self):
        for recorded, observed, expected in (
            (None, "A", "alive"),
            ("A", None, "alive"),
            (None, None, "alive"),
            ("A", "A", "alive"),
            ("A", "B", "dead"),
        ):
            with self.subTest(recorded=recorded, observed=observed):
                with (
                    mock.patch.object(self.rm, "process_probe", return_value="alive"),
                    mock.patch.object(
                        self.rm, "process_start_time", return_value=observed
                    ),
                ):
                    self.assertEqual(
                        self.rm.process_liveness(
                            {"pid": os.getpid(), "start_time": recorded}
                        ),
                        expected,
                    )
        with (
            mock.patch.object(
                self.rm.os, "kill", side_effect=OSError(errno.EPERM, "refused")
            ),
            mock.patch.object(self.rm, "process_start_time", return_value="B"),
        ):
            self.assertEqual(
                self.rm.process_liveness({"pid": os.getpid(), "start_time": "A"}),
                "dead",
            )

    def test_unknown_propagates_through_liveness(self):
        with mock.patch.object(self.rm, "process_start_time", return_value=None):
            for error, expected in ((errno.EINVAL, "unknown"), (errno.ESRCH, "dead")):
                with self.subTest(error=error):
                    with mock.patch.object(
                        self.rm.os, "kill", side_effect=OSError(error, "probe failed")
                    ):
                        self.assertEqual(
                            self.rm.process_liveness(
                                {"pid": os.getpid(), "start_time": "A"}
                            ),
                            expected,
                        )


class SinkTests(TmpEnv):
    def test_fd_sink_is_nonblocking_and_waits_bounded(self):
        r, w = os.pipe()
        sink = self.rm.FdSink(w)
        total = 0
        while True:
            n = sink.write(b"x" * 65536)
            if n == 0:
                break
            total += n
        self.assertGreater(total, 0)
        t0 = time.monotonic()
        self.assertFalse(sink.wait_writable(0.1))
        self.assertLess(time.monotonic() - t0, 0.5)
        os.read(r, total)
        self.assertTrue(sink.wait_writable(0.5))
        os.close(r)
        os.close(w)


class HostTableTests(TmpEnv):
    def decide(self, flag="auto", env=None, payload=None):
        return self.rm.decide_host(ns(host=flag), payload, env or {})

    def test_row1_flag_and_env_override_win(self):
        self.assertEqual(
            self.decide(
                flag="codex",
                env={"CLAUDE_PLUGIN_ROOT": "/p"},
                payload={"prompt_id": "x"},
            ),
            "codex",
        )
        self.assertEqual(
            self.decide(env={"ADT_HOST": "claude-code", "PLUGIN_ROOT": "/x"}),
            "claude-code",
        )

    def test_row2_environment_evidence(self):
        fresh = {
            "cwd": "/w",
            "hook_event_name": "SessionStart",
            "model": "m",
            "permission_mode": "default",
            "session_id": "s",
            "source": "startup",
            "transcript_path": None,
        }
        self.assertEqual(self.decide(env={"PLUGIN_ROOT": "/x"}, payload=fresh), "codex")
        self.assertEqual(
            self.decide(env={"CLAUDE_PLUGIN_ROOT": "/plug"}, payload=fresh),
            "claude-code",
        )  # the measured Claude plugin-hook environment (hook-env capture, v2.0.26)
        self.assertEqual(
            self.decide(env={"CLAUDE_PROJECT_DIR": "/p"}, payload=fresh), "host-unknown"
        )  # documented but absent in the capture: not consulted
        self.assertEqual(
            self.decide(env={"CLAUDE_PLUGIN_ROOT": ""}, payload=fresh), "host-unknown"
        )  # empty is unset (the monitor process sees it empty)
        self.assertEqual(
            self.decide(
                env={
                    "PLUGIN_ROOT": "/x",
                    "CLAUDE_PLUGIN_ROOT": "/x",
                    "PLUGIN_DATA": "/d",
                    "CLAUDE_PLUGIN_DATA": "/d",
                },
                payload=fresh,
            ),
            "codex",
            # the exact pinned Codex plugin-route export set (discovery.rs:265-270)
            # decides
            # codex
        )
        self.assertEqual(
            self.decide(env={"CLAUDE_PLUGIN_ROOT": "/plug"}, payload={"turn_id": "t"}),
            "host-ambiguous",
            # Claude environment against Codex payload evidence: the reverse-direction
            # conflict
        )

    def test_row3_payload_evidence(self):
        self.assertEqual(
            self.decide(payload={"turn_id": "t", "session_id": "s"}), "codex"
        )
        self.assertEqual(self.decide(payload={"prompt_id": "p"}), "claude-code")
        self.assertEqual(
            self.decide(payload={"effort": {"level": "high"}}), "claude-code"
        )

    def test_ambiguity_and_unknown(self):
        self.assertEqual(
            self.decide(payload={"turn_id": "t", "prompt_id": "p"}), "host-ambiguous"
        )
        self.assertEqual(
            self.decide(env={"PLUGIN_ROOT": "/x"}, payload={"prompt_id": "p"}),
            "host-ambiguous",
        )
        fresh = {
            "cwd": "/w",
            "hook_event_name": "SessionStart",
            "model": "m",
            "permission_mode": "default",
            "session_id": "s",
            "source": "startup",
            "transcript_path": None,
        }
        self.assertEqual(self.decide(payload=fresh), "host-unknown")
        self.assertEqual(
            self.decide(
                env={"CLAUDECODE": "1"},
                payload={"hook_event_name": "SessionStart", "session_id": "s"},
            ),
            "host-unknown",
        )
        self.assertEqual(
            self.decide(env={"ADT_HOST": "codex", "CLAUDECODE": "1"}, payload=fresh),
            "codex",
        )


class IndexReaderTests(TmpEnv):
    def test_missing_and_malformed_header(self):
        self.assertEqual(self.rm.read_index(self.index).error, "index-missing")
        self.index.write_text("| a | b |\n|---|---|\n")
        self.assertEqual(self.rm.read_index(self.index).error, "index-malformed")

    def test_rows_parse_with_positions_and_unescaping(self):
        write_index(self.index, row(1, to=r"x\|y.planner") + row(2))
        snap = self.rm.read_index(self.index)
        self.assertIsNone(snap.error)
        self.assertEqual([r.position for r in snap.rows], [1, 2])
        self.assertEqual(snap.rows[0].cells["to"], "x|y.planner")
        self.assertIsNone(snap.rows[1].cells["cc"])
        self.assertEqual(self.rm.unescape_cell(r"a\\b"), r"a\b")

    def test_partial_trailing_row_is_ignored_and_flagged(self):
        self.index.write_text(HEADER + row(1) + "| 20260912-000002 | SITREP | Pair")
        snap = self.rm.read_index(self.index)
        self.assertEqual(len(snap.rows), 1)
        self.assertTrue(snap.partial_tail)
        self.assertIsNone(snap.error)

    def test_malformed_complete_row_and_duplicate_file_halt(self):
        write_index(self.index, row(1) + "| only | three | cells |\n" + row(3))
        snap = self.rm.read_index(self.index)
        self.assertEqual(snap.error, "index-malformed-row:2")
        self.assertEqual(len(snap.rows), 1)
        write_index(
            self.index,
            row(1, file="lane/same.md") + row(2, file="lane/same.md") + row(3),
        )
        snap = self.rm.read_index(self.index)
        self.assertEqual(snap.error, "duplicate-file-cell:2")
        self.assertEqual(len(snap.rows), 1)

    def test_line_grammar(self):
        write_index(self.index, row(1, to="B.Implementer, c.planner"))
        line = self.rm.row_to_line(
            self.rm.read_index(self.index).rows[0], "docs/x/.relays/v1"
        )
        self.assertTrue(line.endswith("\n"))
        obj = json.loads(line)
        self.assertEqual(list(obj), sorted(obj))
        self.assertEqual(obj["root"], "docs/x/.relays/v1")
        self.assertIsNone(obj["cc"])
        self.assertIsNone(obj["parent"])
        self.assertEqual(obj["file"], "lane/r1.md")
        self.assertNotIn(": ", line)

    def test_seat_matching_and_locate(self):
        write_index(
            self.index,
            row(
                1,
                frm="A.Planner",
                to="B.Implementer, c.planner",
                cc="ORCH.orchestrator-reviewer",
            )
            + row(2),
        )
        snap = self.rm.read_index(self.index)
        r = snap.rows[0]
        self.assertTrue(self.rm.names_seat(r, "c.planner"))
        self.assertTrue(self.rm.names_seat(r, "orch.orchestrator-reviewer"))
        self.assertFalse(self.rm.names_seat(r, "a.planner"))
        self.assertTrue(self.rm.is_own(r, "a.planner"))
        self.assertEqual(self.rm.locate(snap, self.rm.SENTINEL), -1)
        self.assertEqual(self.rm.locate(snap, {"file": "lane/r2.md", "position": 2}), 1)
        self.assertIsNone(self.rm.locate(snap, {"file": "lane/gone.md", "position": 9}))


class StallingSink:
    """
    Accepts `accept_first` bytes of the first write, then returns 0 until `released`.
    """

    def __init__(self, accept_first):
        self.buf = bytearray()
        self.accept_first = accept_first
        self.calls = 0
        self.released = False

    def write(self, data):
        self.calls += 1
        if self.calls == 1 and self.accept_first:
            self.buf += data[: self.accept_first]
            return self.accept_first
        if not self.released:
            return 0
        self.buf += data
        return len(data)

    def wait_writable(self, timeout):
        time.sleep(min(timeout, 0.01))
        return self.released

    def flush(self):
        pass


class GateSink:
    """
    Signals `entered` when a consumer is inside its locked write, then blocks until
    `release` is set.
    """

    def __init__(self):
        self.buf = bytearray()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, data):
        self.entered.set()
        self.release.wait(5.0)
        self.buf += data
        return len(data)

    def wait_writable(self, timeout):
        return True

    def flush(self):
        pass


def hold_owner_lock(test, path):
    """
    Start a child that holds an exclusive flock on `path` for the test's life; returns
    once it reports the hold.
    """
    code = (
        "import fcntl, sys, time; f = open(sys.argv[1], 'w');"
        " fcntl.flock(f, fcntl.LOCK_EX); print('held', flush="
        "True); time.sleep(60)"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    test.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
    test.assertEqual(p.stdout.readline().strip(), "held")
    return p


class DeliveryTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("sess")
        self.inst = "leader-1"

    def bind(self, anchor=None):
        cur = dict(self.rm.SENTINEL) if anchor is None else anchor

        def fn(n):
            n.update(
                {
                    "seat": "b.implementer",
                    "root": str(self.root),
                    "anchor": cur["file"],
                    "binding_gen": 1,
                    "binding_source": "hook",
                    "leader": {
                        "pid": os.getpid(),
                        "start_time": "x",
                        "instance": self.inst,
                    },
                }
            )
            n["progress"][str(self.root)] = dict(cur)
            self.rm.record_floor(n, str(self.root), "b.implementer", cur)

        self.store.update(fn)

    def deliver(self, sink=None, deadline=0.5, snap=None, **kw):
        sink = sink if sink is not None else self.rm.ByteSink()
        binding = kw.pop("binding", None) or self.rm.resolve_binding(
            self.store.load(), {}, None
        )
        snap = snap or self.rm.read_index(self.index)
        return self.rm.deliver_once(
            self.store,
            sink,
            binding,
            snap,
            end=time.monotonic() + deadline,
            leader_instance=self.inst,
            **kw,
        ), sink

    def files(self, sink):
        return [
            json.loads(line)["file"] for line in bytes(sink.buf).decode().splitlines()
        ]

    def test_env_override_wins_and_partial_is_ignored(self):
        self.bind()
        with mock.patch.dict(
            os.environ, {"ADT_SEAT": "z.planner", "ADT_RELAY_ROOT": "/other"}
        ):
            b = self.rm.resolve_binding(self.store.load(), os.environ, None)
            self.assertEqual((b.seat, b.root, b.source), ("z.planner", "/other", "env"))
        with mock.patch.dict(os.environ, {"ADT_SEAT": "z.planner"}):
            note = self.store.load()
            b = self.rm.resolve_binding(note, os.environ, None)
            self.assertEqual(b.source, "hook")
            self.assertEqual(note["reason"], "override-incomplete")

    def test_cc_only_65_then_129_once_across_rereads_restart_and_status_only(self):
        self.bind()
        write_index(
            self.index, "".join(row(i, cc="b.implementer") for i in range(1, 66))
        )
        r, s = self.deliver()
        self.assertEqual(r.emitted, 65)
        r, s = self.deliver()
        self.assertEqual(r.emitted, 0)
        write_index(
            self.index, "".join(row(i, cc="b.implementer") for i in range(1, 130))
        )
        r, s = self.deliver()
        self.assertEqual(r.emitted, 64)
        self.assertEqual(self.files(s), [f"lane/r{i}.md" for i in range(66, 130)])
        self.rm = load_script()
        self.store = self.rm.NoteStore("sess")  # restart: fresh module, same note
        r, s = self.deliver()
        self.assertEqual(r.emitted, 0)
        write_index(
            self.index,
            "".join(
                row(i, cc="b.implementer", status=("superseded" if i == 3 else "—"))
                for i in range(1, 130)
            ),
        )
        r, s = self.deliver()
        self.assertEqual(r.emitted, 0)

    def test_pending_row_survives_binding_refresh(self):
        self.bind()
        write_index(
            self.index,
            row(1, to="b.implementer") + row(2, frm="b.implementer", to="a.planner"),
        )
        self.store.update(
            lambda n: n.update({"anchor": "lane/r2.md", "binding_gen": 2})
        )
        r, s = self.deliver()
        self.assertEqual(self.files(s), ["lane/r1.md"])
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r2.md"
        )

    def test_two_consumers_same_snapshot_emit_once_with_barriers(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))
        snap = self.rm.read_index(self.index)
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        first_sink, second_sink, results = GateSink(), self.rm.ByteSink(), {}
        second_store = self.rm.NoteStore("sess")
        attempt = threading.Event()
        second_store.observer = lambda ev: attempt.set()
        t1 = threading.Thread(
            target=lambda: results.update(
                first=self.rm.deliver_once(
                    self.store,
                    first_sink,
                    binding,
                    snap,
                    end=time.monotonic() + 5.0,
                    leader_instance=self.inst,
                )
            )
        )
        t2 = threading.Thread(
            target=lambda: results.update(
                second=self.rm.deliver_once(
                    second_store,
                    second_sink,
                    binding,
                    snap,
                    end=time.monotonic() + 5.0,
                    leader_instance=self.inst,
                    start_cursor=dict(self.rm.SENTINEL),
                )
            )
        )
        self.addCleanup(
            lambda: (
                first_sink.release.set(),
                [t.join(5) for t in (t1, t2) if t.ident is not None],
            )
        )  # join only threads that started
        t1.start()
        self.assertTrue(
            first_sink.entered.wait(5.0)
        )  # first is inside its locked write
        t2.start()
        self.assertTrue(attempt.wait(5.0))
        time.sleep(0.05)  # second has begun its lock attempt and is blocked
        first_sink.release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual((results["first"].emitted, results["second"].emitted), (1, 0))
        self.assertEqual(results["second"].aborted, "progress-moved")

    def test_captured_binding_is_validated_not_fresh_note(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))
        snap = self.rm.read_index(self.index)
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        self.store.update(
            lambda n: n.update({"seat": "z.planner"})
        )  # same root, seat switched, gen unchanged
        r = self.rm.deliver_once(
            self.store,
            self.rm.ByteSink(),
            binding,
            snap,
            end=time.monotonic() + 0.5,
            leader_instance=self.inst,
        )
        self.assertEqual((r.emitted, r.aborted), (0, "binding-changed"))

    def rebase_setup(self, seat="b.implementer"):
        """
        Seat a binding with a cursor the index no longer holds; the next scan must
        rebase on anchor r1.
        """
        self.bind(anchor={"file": "lane/r1.md", "position": 1})
        write_index(
            self.index, "".join(row(i, to="b.implementer") for i in range(1, 4))
        )
        self.store.update(
            lambda n: (
                n.update({"seat": seat}),
                n["progress"].__setitem__(
                    str(self.root), {"file": "lane/gone.md", "position": 9}
                ),
            )
        )
        return self.rm.resolve_binding(self.store.load(), {}, None)

    def interleave_before_lock(self, nth, action):
        """
        Run action just before the nth state-lock acquisition of this test's store.
        """
        real, calls = self.rm.NoteStore.state_lock, {"n": 0}

        def hooked(store, *a, **kw):
            calls["n"] += 1
            if calls["n"] == nth:
                action()
            return real(store, *a, **kw)

        patcher = mock.patch.object(self.rm.NoteStore, "state_lock", hooked)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def test_rebase_is_compare_and_update_against_a_newer_binding(
        self,
    ):  # final review FR2
        binding = self.rebase_setup(seat="a.planner")

        def bind_b():  # seat b binds at r3 between the stale read and the rebase lock
            self.store.update(
                lambda n: (
                    n.update({"seat": "b.implementer", "binding_gen": 2}),
                    n["progress"].__setitem__(
                        str(self.root), {"file": "lane/r3.md", "position": 3}
                    ),
                )
            )

        patcher = self.interleave_before_lock(2, bind_b)
        r, _ = self.deliver(binding=binding)
        self.assertEqual((r.emitted, r.aborted), (0, "binding-changed"))
        n = self.store.load()
        self.assertEqual(
            n["progress"][str(self.root)], {"file": "lane/r3.md", "position": 3}
        )
        self.assertNotEqual(n.get("reason"), "rebased")
        patcher.stop()
        r2, sink = self.deliver(binding=self.rm.resolve_binding(n, {}, None))
        self.assertEqual((r2.emitted, r2.aborted), (0, None))
        self.assertEqual(
            self.files(sink), []
        )  # no addressed b row precedes b's floor r3

    def test_rebase_is_compare_and_update_against_competing_progress(
        self,
    ):  # final review FR2
        binding = self.rebase_setup()
        self.interleave_before_lock(
            2,
            lambda: self.store.update(
                lambda n: n["progress"].__setitem__(
                    str(self.root), {"file": "lane/r2.md", "position": 2}
                )
            ),
        )  # another consumer moved progress
        r, _ = self.deliver(binding=binding)
        self.assertEqual((r.emitted, r.aborted), (0, "progress-moved"))
        self.assertEqual(
            self.store.load()["progress"][str(self.root)],
            {"file": "lane/r2.md", "position": 2},
        )

    def test_ordinary_rebase_still_recovers_on_the_anchor(self):  # FR2 control
        binding = self.rebase_setup()
        r, sink = self.deliver(binding=binding)
        self.assertEqual((r.emitted, r.aborted), (2, None))
        self.assertEqual(self.files(sink), ["lane/r2.md", "lane/r3.md"])
        n = self.store.load()
        self.assertEqual(n["progress"][str(self.root)]["file"], "lane/r3.md")
        self.assertEqual(n["reason"], "rebased")

    def test_prefix_stall_then_resume_yields_one_framed_line(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))
        sink = StallingSink(accept_first=7)
        r, _ = self.deliver(sink=sink, deadline=0.2)
        self.assertEqual((r.emitted, r.aborted), (0, "output-stalled"))
        note = self.store.load()
        self.assertEqual(note["frame"]["accepted"], 7)
        self.assertEqual(note["frame"]["identity"]["leader"], self.inst)
        self.assertEqual(note["progress"][str(self.root)], self.rm.SENTINEL)
        sink.released = True
        r, _ = self.deliver(sink=sink, deadline=0.2)
        self.assertEqual(r.emitted, 1)
        lines = bytes(sink.buf).decode().split("\n")
        self.assertEqual(json.loads(lines[0])["file"], "lane/r1.md")
        self.assertEqual(lines[1], "")
        self.assertIsNone(self.store.load()["frame"])

    def test_partial_frame_invalidated_by_drain_or_bind_abandons(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer") + row(2, to="b.implementer"))
        sink = StallingSink(accept_first=5)
        self.deliver(sink=sink, deadline=0.2)
        self.store.update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/r1.md", "position": 1}
            )
        )  # drain committed r1 meanwhile
        sink.released = True
        with self.assertRaises(self.rm.TransportFailed):
            self.deliver(sink=sink, deadline=0.2)
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))
        sink = StallingSink(accept_first=5)
        self.deliver(sink=sink, deadline=0.2)
        self.store.update(
            lambda n: n.update({"root": "/elsewhere", "binding_gen": 9})
        )  # bind switched root
        sink.released = True
        with self.assertRaises(self.rm.TransportFailed):
            binding = self.rm.Binding("b.implementer", "/elsewhere", None, 9, "hook")
            self.rm.deliver_once(
                self.store,
                sink,
                binding,
                self.rm.read_index(self.index),
                end=time.monotonic() + 0.2,
                leader_instance=self.inst,
            )

    def test_foreign_frame_alive_dead_unknown(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))

        def plant(lock, leader="drain-other"):
            return self.store.update(
                lambda n: n.__setitem__(
                    "frame",
                    {
                        "identity": {
                            "root": str(self.root),
                            "seat": "b.implementer",
                            "binding_gen": 1,
                            "leader": leader,
                            "file": "lane/r1.md",
                            "owner": {"lock": str(lock)},
                        },
                        "data": "00",
                        "accepted": 1,
                    },
                )
            )

        live = self.store.owner_lock_path("drain-live")
        holder = hold_owner_lock(self, live)  # alive: a child holds the flock
        plant(live)
        with mock.patch.object(
            self.rm.subprocess, "run", side_effect=AssertionError("external probe")
        ):
            t0 = time.monotonic()
            r, s = self.deliver()
            probe = time.monotonic() - t0
        self.assertEqual((r.emitted, r.aborted), (0, "frame-busy"))
        self.assertLess(probe, 0.5)
        self.assertEqual(bytes(s.buf), b"")
        with self.rm.NoteStore("sess").state_lock(timeout=0.1):
            pass  # the state lock is free immediately after the probe
        holder.kill()
        holder.wait()  # dead: the flock is released by the kernel
        r, s = self.deliver()
        self.assertEqual((r.emitted, r.aborted), (1, None))
        self.assertIsNone(self.store.load()["frame"])
        self.assertFalse(live.exists())
        self.assertEqual(
            json.loads(bytes(s.buf))["file"], "lane/r1.md"
        )  # reclaimed: the full row from byte zero
        self.bind()
        unheld = self.store.owner_lock_path("drain-unheld")
        unheld.touch()
        plant(unheld)
        r, s = self.deliver()
        self.assertEqual(r.emitted, 1)
        self.assertFalse(unheld.exists())  # dead: a file nobody holds
        self.bind()
        plant(self.store.owner_lock_path("drain-missing"))  # unknown: no such file
        r, s = self.deliver()
        self.assertEqual(r.aborted, "frame-busy")
        self.assertIsNotNone(self.store.load()["frame"])
        directory = self.store.owner_lock_path("drain-dir")
        directory.mkdir()
        plant(directory)  # unknown: unreadable path
        r, s = self.deliver()
        self.assertEqual(r.aborted, "frame-busy")
        self.assertIsNotNone(self.store.load()["frame"])
        self.store.update(
            lambda n: n.__setitem__(
                "frame",
                {
                    "identity": {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "binding_gen": 1,
                        "leader": "other-follower",
                        "file": "lane/r1.md",
                    },
                    "data": "00",
                    "accepted": 1,
                },
            )
        )
        r, s = self.deliver()
        self.assertEqual(
            r.aborted, "frame-busy"
        )  # a follower frame without an owner is never stale; a new leader clears it

    def plant_owner_frame(self, lock, leader="drain-other"):
        frame = {
            "identity": {
                "root": str(self.root),
                "seat": "b.implementer",
                "binding_gen": 1,
                "leader": leader,
                "file": "lane/r1.md",
                "owner": {"lock": str(lock)},
            },
            "data": "00",
            "accepted": 1,
        }
        self.store.update(lambda n: n.__setitem__("frame", frame))
        return frame

    def test_owner_probe_is_repeatable_and_reclaim_saves_before_unlink(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))
        unheld = self.store.owner_lock_path("drain-unheld")
        unheld.touch()
        frame = self.plant_owner_frame(unheld)
        with self.store.state_lock():
            self.assertEqual(self.rm.owner_state(frame), "dead")
            self.assertTrue(unheld.exists())
            self.assertEqual(self.rm.owner_state(frame), "dead")
            self.assertTrue(
                unheld.exists()
            )  # observational: the evidence survives repeated probes
            saved = []
            real_save = self.store.save

            def save_then_check(note):
                real_save(note)
                saved.append(
                    unheld.exists()
                )  # the file must still exist at the moment of the durable save

            with mock.patch.object(self.store, "save", side_effect=save_then_check):
                self.rm.reclaim_frame(self.store, self.store.load(), frame)
        self.assertEqual(saved, [True])
        self.assertFalse(unheld.exists())
        self.assertIsNone(self.store.load()["frame"])  # unlink only after the save

    def test_failure_between_detection_and_durable_clear_is_recoverable(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))
        unheld = self.store.owner_lock_path("drain-unheld")
        unheld.touch()
        frame = self.plant_owner_frame(unheld)
        with mock.patch.object(
            self.rm.NoteStore,
            "save",
            side_effect=OSError("simulated crash before the durable clear"),
        ):
            with self.assertRaises(OSError):
                self.deliver()
        self.assertEqual(self.store.load()["frame"], frame)
        self.assertTrue(unheld.exists())  # detection consumed nothing
        r, s = self.deliver()
        self.assertEqual(r.emitted, 1)
        self.assertIsNone(self.store.load()["frame"])
        self.assertFalse(unheld.exists())  # the next invocation reclaims

    def test_owner_creation_is_invisible_to_the_sweep(self):
        opened, resume, result = threading.Event(), threading.Event(), {}

        def between():
            opened.set()
            resume.wait(5.0)

        t = threading.Thread(
            target=lambda: result.update(
                me=self.rm.stream_owner(
                    self.store, time.monotonic() + 5.0, _between=between
                )
            )
        )
        self.addCleanup(lambda: (resume.set(), t.join(5)))
        t.start()
        self.assertTrue(opened.wait(5.0))  # creator parked between open and flock
        sweeper = self.rm.NoteStore("sess")
        with self.assertRaises(self.rm.LockTimeout):
            with sweeper.state_lock(timeout=0.3):
                self.rm.sweep_owner_files(
                    sweeper
                )  # the actual sweep cannot run: the creator holds the state lock
        resume.set()
        t.join(5)
        me = result["me"]
        self.assertIsNotNone(me)
        path = pathlib.Path(me["lock"])
        self.assertTrue(path.exists())
        frame = {"identity": {"owner": {"lock": str(path)}}}
        with self.store.state_lock():
            self.assertEqual(self.rm.owner_state(frame), "alive")  # published and held
            self.assertEqual(self.rm.sweep_owner_files(self.store), 0)
            self.assertTrue(path.exists())  # a held file is never swept
        me["_lock"].release(unlink=False)
        with self.store.state_lock():
            self.assertEqual(self.rm.owner_state(frame), "dead")
            self.assertEqual(
                self.rm.sweep_owner_files(self.store), 1
            )  # released: reclaimable, then swept as unreferenced
        self.assertFalse(path.exists())

    def test_sweep_removes_only_unreferenced_acquirable_owner_files(self):
        self.bind()
        referenced = self.store.owner_lock_path("drain-referenced")
        referenced.touch()
        self.plant_owner_frame(referenced)
        orphan = self.store.owner_lock_path("drain-orphan")
        orphan.touch()
        live = self.store.owner_lock_path("drain-live")
        hold_owner_lock(self, live)
        with self.store.state_lock():
            self.assertEqual(self.rm.sweep_owner_files(self.store), 1)
        self.assertTrue(referenced.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(live.exists())
        self.assertIsNotNone(self.store.load()["frame"])

    def test_write_error_raises_transport_failed_and_commits_nothing(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))

        class Dead:
            def write(self, d):
                raise BrokenPipeError()

            def wait_writable(self, t):
                return True

            def flush(self):
                pass

        with self.assertRaises(self.rm.TransportFailed):
            self.deliver(sink=Dead())
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )

    def test_slow_positive_writes_respect_deadline_and_lock_is_bounded(self):
        self.bind()
        write_index(self.index, row(1, to="b.implementer"))

        class Trickle:
            def __init__(self):
                self.buf = bytearray()

            def write(self, d):
                time.sleep(0.05)
                self.buf += d[:1]
                return 1

            def wait_writable(self, t):
                return True

            def flush(self):
                pass

        t0 = time.monotonic()
        r, _ = self.deliver(sink=Trickle(), deadline=0.2)
        self.assertEqual(r.aborted, "output-stalled")
        self.assertLess(time.monotonic() - t0, 1.0)
        other = self.rm.NoteStore("sess")
        with other.state_lock():
            t0 = time.monotonic()
            r, _ = self.deliver(deadline=0.2)
            self.assertEqual(r.aborted, "lock-timeout")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.index.unlink()
            t0 = time.monotonic()
            r, _ = self.deliver(deadline=0.2)
            self.assertIn(r.aborted, ("lock-timeout",))
            self.assertLess(
                time.monotonic() - t0, 1.0
            )  # error-state update is bounded too
        write_index(self.index, row(1, to="b.implementer"))

        class ZeroOnce:
            def __init__(self):
                self.buf = bytearray()
                self.calls = 0

            def write(self, d):
                self.calls += 1
                return 0 if self.calls == 1 else (self.buf.extend(d) or len(d))

            def wait_writable(self, t):
                return True

            def flush(self):
                pass

        t0 = time.monotonic()
        r, _ = self.deliver(sink=ZeroOnce(), deadline=5.0)
        self.assertEqual(r.aborted, "output-stalled")
        self.assertLess(
            time.monotonic() - t0, 0.5
        )  # zero progress returns at once: no waiting under the lock

    def test_pacing_is_per_row_and_leaves_rows_pending(self):
        self.bind()
        write_index(
            self.index, "".join(row(i, to="b.implementer") for i in range(1, 13))
        )
        pacer = self.rm.Pacer(5, 60.0)
        r, s = self.deliver(pacer=pacer)
        self.assertEqual((r.emitted, r.aborted), (5, "paced"))
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r5.md"
        )
        r, s = self.deliver(pacer=self.rm.Pacer(100, 60.0))
        self.assertEqual(r.emitted, 7)

    def test_rebase_when_progress_row_absent_and_anchor_present(self):
        write_index(
            self.index, "".join(row(i, to="b.implementer") for i in range(1, 4))
        )
        self.bind(anchor={"file": "lane/r1.md", "position": 1})
        self.store.update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/gone.md", "position": 2}
            )
        )
        r, s = self.deliver()
        self.assertEqual(self.files(s), ["lane/r2.md", "lane/r3.md"])
        self.assertEqual(self.store.load()["reason"], "rebased")

    def test_header_only_index_then_first_row_is_delivered(self):
        self.bind()
        self.index.write_text(HEADER)
        r, _ = self.deliver()
        self.assertEqual(r.emitted, 0)
        write_index(self.index, row(1, to="b.implementer"))
        r, s = self.deliver()
        self.assertEqual(self.files(s), ["lane/r1.md"])

    def test_completed_empty_scan_establishes_following(
        self,
    ):  # final review FR1, in-process half
        self.bind(anchor={"file": "lane/r1.md", "position": 1})
        write_index(self.index, row(1, to="b.implementer"))
        self.store.update(lambda n: n.update({"phase": "waiting-for-binding"}))
        r, sink = self.deliver()
        self.assertEqual((r.emitted, r.aborted, r.halted), (0, None, None))
        self.assertEqual(self.store.load()["phase"], "following")
        self.store.update(
            lambda n: n.update(
                {"phase": "waiting-for-binding", "binding_degraded": "x"}
            )
        )
        self.deliver()
        self.assertEqual(
            self.store.load()["phase"], "following"
        )  # legacy observation does not block recovery
        self.store.update(lambda n: n.update({"binding_degraded": None}))
        self.index.unlink()
        r, _ = self.deliver()
        self.assertEqual(r.halted, "index-missing")
        self.assertEqual(self.store.load()["phase"], "unavailable")


class FollowProcessMixin:
    """
    Helpers for real-subprocess witnesses; mixed into FollowProcessTests (T3) and
    EnvRestartReplayTests (T6).
    """

    def spawn(self, seat="b.implementer", extra_env=None, anchor=None):
        env = dict(
            os.environ,
            ADT_SEAT=seat,
            ADT_RELAY_ROOT=str(self.root),
            **(extra_env or {}),
        )
        if anchor:
            env["ADT_RELAY_ANCHOR"] = anchor
        p = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "follow",
                "--host",
                "claude-code",
                "--session",
                "psess",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.addCleanup(
            lambda: (p.kill(), p.wait(), p.stdout.close(), p.stderr.close())
        )
        return p

    def wait_note(self, pred, timeout=8.0):
        store, end = self.rm.NoteStore("psess"), time.monotonic() + timeout
        while time.monotonic() < end:
            note = store.load()
            if pred(note):
                return note
            time.sleep(0.05)
        self.fail(
            "note condition not observed: "
            + json.dumps(store.load(), default=str)[:300]
        )

    def bound(self, note):
        return (
            note.get("phase") in ("following", "waiting-for-binding")
            and note.get("leader")
            and str(self.root) in note.get("progress", {})
        )

    def read_lines(self, proc, count, timeout=8.0):
        out, end = [], time.monotonic() + timeout
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        buf = b""
        while len(out) < count and time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    out.append(json.loads(line))
        return out


class FollowProcessTests(FollowProcessMixin, TmpEnv):
    """
    Real subprocess witnesses: crash injection, takeover on real streams, backlog
    pacing, note-refresh recovery.
    """

    def test_note_written_during_scan_wakes_without_index_change(self):
        self._assert_note_wake()

    def test_note_wake_survives_polling_process_probe(self):
        self._assert_note_wake(force_poll=True)

    def _assert_note_wake(self, force_poll=False):
        polls = []

        class PollingProcess(subprocess.Popen):
            def _try_wait(child, wait_flags):
                if wait_flags == os.WNOHANG and not getattr(child, "forced_poll", False):
                    child.forced_poll = True
                    polls.append(child.pid)
                    return 0, 0
                return super()._try_wait(wait_flags)

        def polling_probe(args, **kwargs):
            with PollingProcess(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=kwargs.get("text", False)) as child:
                stdout, stderr = child.communicate(timeout=kwargs.get("timeout"))
                return subprocess.CompletedProcess(args, child.returncode, stdout, stderr)

        probe_module = types.SimpleNamespace(**{
            **vars(subprocess), "run": polling_probe if force_poll else subprocess.run,
        })
        platform_module = types.SimpleNamespace(**{
            **vars(self.rm.platform), "system": lambda: "Darwin",
        }) if force_poll else self.rm.platform
        with (
            mock.patch.object(self.rm, "subprocess", probe_module),
            mock.patch.object(self.rm, "platform", platform_module),
        ):
            self._exercise_note_wake()
        if force_poll:
            self.assertGreaterEqual(len(polls), 1, "probe never entered polling wait")

    def _exercise_note_wake(self):
        write_index(self.index, row(1, to="b.implementer"))
        store = self.rm.NoteStore("scan-session")
        store.update(
            lambda note: note.update(
                {
                    "seat": "b.implementer",
                    "root": str(self.root),
                    "binding_source": "hook",
                    "binding_gen": 1,
                    "progress": {str(self.root): {"file": "lane/r1.md", "position": 1}},
                }
            )
        )
        index_before = self.rm.index_signature(self.index)
        lock = self.rm.LeaderLock("scan-session")
        self.addCleanup(lock.release)
        seen = []

        class StopLoop(Exception):
            pass

        def scan(*args, **kwargs):
            seen.append(store.load().get("reason"))
            if len(seen) == 2:
                raise StopLoop()
            old_mtime = store.mtime()
            store.update(lambda note: note.update({"reason": "mid-scan-edit"}))
            os.utime(store.path, ns=(old_mtime + 1000000, old_mtime + 1000000))
            return self.rm.DeliverResult()

        with (
            mock.patch.object(self.rm, "deliver_once", side_effect=scan),
            mock.patch.object(self.rm, "LeaderLock", return_value=lock),
            mock.patch.object(self.rm, "FdSink", return_value=self.rm.ByteSink()),
            mock.patch.object(self.rm, "time", types.SimpleNamespace(**{
                **vars(time), "sleep": mock.Mock(side_effect=StopLoop),
            })),
        ):
            with self.assertRaises(StopLoop):
                self.rm.follow(ns(session="scan-session", host="claude-code"))
        self.assertEqual(seen, [None, "mid-scan-edit"])
        self.assertEqual(self.rm.index_signature(self.index), index_before)

    def test_closed_stdout_records_transport_failure_and_releases_lock(self):
        write_index(self.index, row(1, to="b.implementer"))
        proc = self.spawn()
        before = self.wait_note(self.bound)
        progress = before["progress"]
        self.assertEqual(progress[str(self.root)]["file"], "lane/r1.md")
        proc.stdout.close()
        write_index(self.index, row(1, to="b.implementer") + row(2, to="b.implementer"))
        proc.wait(timeout=8)
        after = self.rm.NoteStore("psess").load()
        with self.subTest("exit"):
            self.assertEqual(proc.returncode, 1)
        with self.subTest("phase"):
            self.assertEqual(after["phase"], "unavailable")
        with self.subTest("reason"):
            self.assertEqual(after["reason"], "transport-failed")
        with self.subTest("leader"):
            self.assertIsNone(after["leader"])
        with self.subTest("progress"):
            self.assertEqual(after["progress"], progress)
        probe = self.rm.LeaderLock("psess")
        self.addCleanup(probe.release)
        self.assertTrue(probe.acquire(blocking=False))

    def test_crash_after_flush_before_persist_reemits_exactly_one_row(self):
        write_index(
            self.index,
            row(1, to="b.implementer")
            + row(2, to="b.implementer")
            + row(3, to="b.implementer"),
        )
        p = self.spawn(
            extra_env={"ADT_TEST_CRASH_AFTER_FLUSH": "1"}, anchor="lane/r1.md"
        )
        first = self.read_lines(p, 1)
        p.wait(5)
        self.assertEqual(p.returncode, 9)
        self.assertEqual(first[0]["file"], "lane/r2.md")
        p = self.spawn(anchor="lane/r1.md")
        got = self.read_lines(p, 2)
        self.assertEqual(
            [g["file"] for g in got], ["lane/r2.md", "lane/r3.md"]
        )  # r2 once more (the ruled bound), then r3

    def test_three_followers_one_leader_and_takeover_on_real_streams(self):
        write_index(self.index, row(1, to="b.implementer"))
        procs = [self.spawn(anchor="lane/r1.md") for _ in range(3)]
        note = self.wait_note(
            lambda n: self.bound(n) and len(n.get("standbys", [])) == 2
        )
        old_leader = note["leader"]["instance"]
        write_index(self.index, row(1, to="b.implementer") + row(2, to="b.implementer"))
        outputs = [self.read_lines(p, 1, timeout=4.0) for p in procs]
        leaders = [i for i, o in enumerate(outputs) if o]
        self.assertEqual(len(leaders), 1)
        procs[leaders[0]].kill()
        procs[leaders[0]].wait()
        self.wait_note(
            lambda n: n.get("leader")
            and n["leader"]["instance"] != old_leader
            and self.bound(n)
        )  # takeover observed before the next arrival
        write_index(
            self.index,
            row(1, to="b.implementer")
            + row(2, to="b.implementer")
            + row(3, to="b.implementer"),
        )
        outputs = [
            self.read_lines(p, 1, timeout=6.0) if i != leaders[0] else []
            for i, p in enumerate(procs)
        ]
        self.assertEqual(sum(1 for o in outputs if o), 1)
        self.assertEqual([o[0]["file"] for o in outputs if o], ["lane/r3.md"])

    def test_backlog_above_pace_is_delivered_paced_without_loss(self):
        self.index.write_text(HEADER)  # bind at a header-only index: sentinel cursor
        p = self.spawn(extra_env={"ADT_PACE_LINES": "5", "ADT_PACE_WINDOW": "1.0"})
        self.wait_note(
            lambda n: self.bound(n)
            and n["progress"][str(self.root)] == self.rm.SENTINEL
        )  # observed bound at the sentinel before rows exist
        write_index(
            self.index, "".join(row(i, to="b.implementer") for i in range(1, 13))
        )  # twelve rows arrive at once
        early = self.read_lines(p, 12, timeout=0.6)
        self.assertLessEqual(len(early), 5)
        rest = self.read_lines(p, 12 - len(early), timeout=6.0)
        self.assertEqual(
            [g["file"] for g in early + rest], [f"lane/r{i}.md" for i in range(1, 13)]
        )

    def test_env_bound_follower_at_index_tail_is_armed_without_new_rows(
        self,
    ):  # final review FR1
        write_index(self.index, row(1, to="b.implementer"))
        p = self.spawn(seat="b.implementer")
        note = self.wait_note(lambda n: self.bound(n) and n.get("phase") == "following")
        self.assertEqual(note["progress"][str(self.root)]["file"], "lane/r1.md")
        self.assertEqual(self.rm.readiness(self.rm.NoteStore("psess"))[0], "armed")
        write_index(self.index, row(1, to="b.implementer") + row(2, to="b.implementer"))
        self.assertEqual(
            self.read_lines(p, 1)[0]["file"], "lane/r2.md"
        )  # arrival control
        self.assertEqual(self.rm.readiness(self.rm.NoteStore("psess"))[0], "armed")
        p.kill()
        p.wait()
        q = self.spawn(seat="b.implementer")
        self.wait_note(
            lambda n: n.get("phase") == "following"
            and n.get("leader", {}).get("pid") == q.pid
        )  # resume at the tail: armed with no backlog
        self.assertEqual(self.rm.readiness(self.rm.NoteStore("psess"))[0], "armed")
        q.kill()
        q.wait()

    def test_unbound_or_indexless_follower_is_not_armed(self):  # FR1 negatives
        p = self.spawn(seat="b.implementer")  # no INDEX.md yet
        self.wait_note(
            lambda n: n.get("leader")
            and n.get("phase") in ("unavailable", "waiting-for-binding")
        )
        self.assertNotEqual(self.rm.readiness(self.rm.NoteStore("psess"))[0], "armed")
        p.kill()
        p.wait()

    def test_env_restart_with_new_seat_on_known_root_gets_its_own_floor(
        self,
    ):  # D8-R1 (floors half; the replay half is EnvRestartReplayTests in T6)
        self.index.write_text(HEADER)
        p = self.spawn(seat="b.implementer")
        first = self.wait_note(
            lambda n: self.bound(n) and len(n.get("floors", [])) == 1
        )
        old_floor = dict(first["floors"][0])
        write_index(self.index, row(1, to="b.implementer"))
        self.assertEqual(self.read_lines(p, 1)[0]["file"], "lane/r1.md")
        self.wait_note(
            lambda n: n["progress"][str(self.root)].get("file") == "lane/r1.md"
            # D9-R2: the checkpoint is persisted before the kill (output precedes
            # persistence by design)
        )
        p.kill()
        p.wait()
        q = self.spawn(seat="c.reviewer")  # same session, same root, new seat
        note = self.wait_note(
            lambda n: n.get("seat") == "c.reviewer" and len(n.get("floors", [])) == 2
        )
        self.assertEqual(note["floors"][0], old_floor)
        self.assertEqual(note["floors"][1]["seat"], "c.reviewer")
        self.assertEqual(note["floors"][1]["root"], str(self.root))
        self.assertEqual(
            note["floors"][1]["file"], "lane/r1.md"
        )  # the new pair's floor is the preserved progress, not a reset
        self.assertEqual(note["binding_source"], "env")
        self.assertEqual(note["progress"][str(self.root)]["file"], "lane/r1.md")
        write_index(self.index, row(1, to="b.implementer") + row(2, to="c.reviewer"))
        self.assertEqual(self.read_lines(q, 1)[0]["file"], "lane/r2.md")
        after = self.wait_note(
            lambda n: n["progress"][str(self.root)].get("file") == "lane/r2.md"
        )  # D9-R2: clean checkpointed transition; the crash window keeps its own test
        q.kill()
        q.wait()
        self.assertEqual(len(after["floors"]), 2)
        self.assertEqual(
            after["floors"][0], old_floor
        )  # the restart added exactly one floor and moved nothing

    def test_missing_anchor_recovers_on_note_refresh_without_index_change(self):
        write_index(self.index, row(1, to="b.implementer") + row(2, to="b.implementer"))
        store = self.rm.NoteStore("psess")
        store.update(
            lambda n: (
                n.update(
                    {
                        "seat": "b.implementer",
                        "root": str(self.root),
                        "anchor": "lane/gone.md",
                        "binding_gen": 1,
                        "binding_source": "hook",
                    }
                ),
                n["progress"].__setitem__(
                    str(self.root), {"file": "lane/gone.md", "position": 5}
                ),
            )
        )
        p = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "follow",
                "--host",
                "claude-code",
                "--session",
                "psess",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        self.addCleanup(
            lambda: (p.kill(), p.wait(), p.stdout.close(), p.stderr.close())
        )
        self.wait_note(lambda n: n.get("reason") == "anchor-not-found")
        store.update(
            lambda n: n.update({"anchor": "lane/r1.md", "binding_gen": 2})
        )  # a bind refresh, index bytes unchanged
        got = self.read_lines(p, 1, timeout=6.0)
        self.assertEqual(got[0]["file"], "lane/r2.md")


class BindTests(TmpEnv):
    def test_rebind_checks_observation_root_instead_of_bound_root(self):
        import shlex
        observed = self.other_root()
        self.store.update(lambda note: note.update(root=str(self.root)))
        for provenance in ("command", "inferred"):
            for row_in_observation in (False, True):
                with self.subTest(provenance=provenance, row_in_observation=row_in_observation):
                    write_index(self.index, "" if row_in_observation else row(1))
                    write_index(observed / "INDEX.md", row(1) if row_in_observation else "")
                    self.store.update(lambda note: note.update(bind_observation={"root": str(observed), "root_provenance": provenance, "receipts": ["lane/r1.md"]}))
                    line = self.rm.rebind_line(self.store.load(), "sess")
                    if row_in_observation:
                        prefix = "rebind, may need operator approval: " if provenance == "command" else "rebind (root inferred, check it, may need operator approval): "
                        self.assertTrue(line.startswith(prefix))
                        words = shlex.split(line.removeprefix(prefix))
                        self.assertEqual(words[words.index("--root") + 1], str(observed))
                        self.assertEqual(words[words.index("--anchor") + 1], "lane/r1.md")
                    else:
                        self.assertTrue(line.startswith("# rebind: incomplete"))
                        self.assertNotIn("bind --manual", line)
                    self.assertEqual(self.store.load()["root"], str(self.root))

    def test_lookup_rejects_nul_root_and_requires_exact_file_cell(self):
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root) + "\0", "lane/r1.md"), "unreadable")
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/r1.md"), "match")
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/./r1.md"), "no-match")

    def test_nonrow_receipt_under_command_provenance_gets_incomplete_hint(self):
        (self.root / "lane/missing.md").write_text("not filed")
        rc, output = self._hook_output(self._submit(anchor="lane/missing.md"))
        self.assertEqual(rc, 0)
        record = self.store.load()["bind_observation"]
        self.assertEqual((record["reason"], record["root_provenance"]), ("receipt-mismatch", "command"))
        hint = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(hint.startswith("# rebind: incomplete"))
        self.assertNotIn("bind --manual", hint)
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            status = self.rm.status_line(self.store, None)
        self.assertIn("# rebind: incomplete", status)
        self.assertNotIn("bind --manual", status)

    def test_complete_hint_appears_once_row_exists(self):
        import shlex
        self._hook_output(self._submit(anchor="lane/missing.md"))
        before = self.store.load()["bind_observation"]
        self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("# rebind: incomplete"))
        with self.index.open("a") as stream:
            stream.write(row(2, file="lane/missing.md"))
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            status = self.rm.status_line(self.store, None)
        hint = next(line for line in status.splitlines() if line.startswith("rebind, may need operator approval: "))
        words = shlex.split(hint.removeprefix("rebind, may need operator approval: "))
        self.assertEqual(words[-4:], ["--root", str(self.root), "--anchor", "lane/missing.md"])
        self.assertEqual(self.store.load()["bind_observation"], before)

    def test_command_observation_with_unreadable_index_gets_incomplete_hint(self):
        self._hook_output(self._submit(anchor="lane/missing.md"))
        with mock.patch.object(self.rm, "read_index", side_effect=PermissionError("fixture unreadable")):
            hint = self.rm.rebind_line(self.store.load(), "sess")
        self.assertTrue(hint.startswith("# rebind: incomplete"))
        self.assertNotIn("bind --manual", hint)

    def test_inferred_observation_follows_index_readability(self):
        self._hook_output(self._miss())
        record = self.store.load()["bind_observation"]
        self.assertEqual(record["root_provenance"], "inferred")
        self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("rebind (root inferred, check it, may need operator approval): "))
        with mock.patch.object(self.rm, "read_index", side_effect=PermissionError("fixture unreadable")):
            self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("# rebind: incomplete"))
        self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("rebind (root inferred, check it, may need operator approval): "))
        self.assertEqual(self.store.load()["bind_observation"], record)

    def test_refused_manual_bind_exits_one_with_reason(self):
        self._manual()
        before = self.store.path.read_bytes()
        (self.root / "lane/missing.md").write_text("not filed")
        result = subprocess.run([sys.executable, str(SCRIPT), "bind", "--manual", "--session", "sess", "--root", str(self.root), "--anchor", "lane/missing.md"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "relay-monitor: manual bind refused: receipt-mismatch\n")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_manual_bind_usage_and_session_errors_exit_two(self):
        before = set(self.base.rglob("*"))
        for operands, reason in ((["--root", str(self.root)], "manual bind needs"), (["--anchor", "lane/r1.md"], "manual bind needs"), (["--root", str(self.root), "--anchor", "lane/r1.md", "--session", "../bad"], "invalid-session-id")):
            with self.subTest(operands=operands):
                result = subprocess.run([sys.executable, str(SCRIPT), "bind", "--manual", "--session", "sess", *operands], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn(reason, result.stderr)
                self.assertEqual(set(self.base.rglob("*")), before)

    def test_successful_manual_bind_is_silent(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "bind", "--manual", "--session", "sess", "--root", str(self.root), "--anchor", "lane/r1.md"], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.store.load()["anchor"], "lane/r1.md")
        self.assertEqual(self.store.load()["seat"], "a.planner")

    def test_manual_bind_unexpected_error_is_not_swallowed(self):
        bootstrap = """import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fault_adapter', sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
def fail(*args, **kwargs):
    Path(sys.argv[2]).write_text('bind fault reached')
    raise RuntimeError('injected bind fault')
m.bind_from_receipt = fail
raise SystemExit(m.main(sys.argv[3:]))
"""
        for manual in (False, True):
            with self.subTest(manual=manual):
                marker = self.base / ("manual-fault" if manual else "hook-fault")
                self.assertFalse(marker.exists())
                args = ["bind", "--host", "codex"]
                if manual:
                    args += ["--manual", "--session", "sess", "--root", str(self.root), "--anchor", "lane/r1.md"]
                result = subprocess.run([sys.executable, "-c", bootstrap, str(SCRIPT), str(marker), *args], input=self._submit(), capture_output=True, text=True, timeout=10)
                self.assertEqual(marker.read_text(), "bind fault reached")
                self.assertEqual(result.stdout, "")
                if manual:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("RuntimeError: injected bind fault", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn("Traceback", result.stderr)

    def test_every_hint_form_is_inert_or_runnable_in_shells(self):
        import shlex
        marker = self.base / "must-not-exist"
        shells = (["sh", "-c"], ["bash", "-c"], ["zsh", "-f", "-i", "-c"])
        for provenance in ("none", "command", "inferred"):
            record = dict(root=str(self.root), root_provenance=provenance, receipts=["lane/r1.md"])
            if provenance == "none":
                record["receipts"] = [f"$(touch {marker})", f"`touch {marker}`", "a'; touch nope; '"]
            hint = self.rm.rebind_line({"bind_observation": record}, "sess")
            if provenance == "none":
                for shell in shells:
                    result = subprocess.run([*shell, hint], capture_output=True, text=True, timeout=10)
                    self.assertFalse(marker.exists())
                    self.assertEqual(result.stdout, "")
            else:
                prefix = "rebind, may need operator approval: " if provenance == "command" else "rebind (root inferred, check it, may need operator approval): "
                words = shlex.split(hint.removeprefix(prefix))
                self.assertEqual(words, ["python3", str(SCRIPT.resolve()), "bind", "--manual", "--session", "sess", "--root", str(self.root), "--anchor", "lane/r1.md"])
                for shell in shells:
                    # Shell parsing, without executing a real manual bind.
                    result = subprocess.run([*shell, "printf '%s\\n' " + hint.removeprefix(prefix)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), words)

    def _assert_incomplete_in_interactive_shells(self, hint):
        import shlex
        # Sentinel executables cover all fixed words, including an old separator
        # mutant and the dropped-value marker, without defining a hash command.
        sentinels = self.base / "hint-sentinels"
        sentinels.mkdir(exist_ok=True)
        names = ["rebind:", "incomplete", "incomplete,", "supply", "--root",
                 "and", "--anchor", "seen=", "command", "text="]
        names += [word for word in shlex.split(hint) if word.startswith("dropped=")]
        for name in names:
            executable = sentinels / name
            executable.write_text("#!/bin/sh\nprintf 'FIXED_WORD_EXECUTED\\n'\n")
            executable.chmod(0o755)
        modes = [(["zsh", "-f", "-i"], "unsetopt interactivecomments"),
                 (["zsh", "-f", "-i"], "setopt interactivecomments"),
                 (["bash", "--noprofile", "--norc", "-i"], "shopt -u interactive_comments"),
                 (["bash", "--noprofile", "--norc", "-i"], "shopt -s interactive_comments"),
                 (["sh"], "")]
        for shell, option in modes:
            with self.subTest(shell=shell, option=option, hint=hint):
                script = option + "\nPATH=" + shlex.quote(str(sentinels)) + ":$PATH\n"
                script += hint + "\nprintf 'AFTER_HINT\\n'\nexit\n"
                result = subprocess.run(shell, input=script, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.stdout, "AFTER_HINT\n", result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_hint_fixed_words_are_inert_with_comments_disabled(self):
        marker = self.base / "incomplete-hostile-marker"
        cases = [(["lane/r1.md"], []),
                 ([f"$(touch {marker})", f"`touch {marker}`", f"a'; touch {marker}; '"],
                  [f"$(touch {marker})", "a|b&c;d<e>f(g)\nnext"])]
        for receipts, operands in cases:
            hint = self.rm.rebind_line({"bind_observation": dict(
                receipts=receipts, command_roots=operands, root_provenance="none")}, "sess")
            self._assert_incomplete_in_interactive_shells(hint)
            self.assertFalse(marker.exists())
            self.assertNotIn("\n", hint)

    def test_incomplete_hint_truncation_keeps_complete_quoted_values(self):
        import shlex
        prefix = "# rebind: incomplete, supply --root and --anchor seen="
        # A quoted value exactly fills the bound; the next value forces a cut.
        value = "a b" + "x" * (1024 - len(prefix) - 2 - 3)
        exact = self.rm.rebind_line({"bind_observation": dict(receipts=[value])}, "sess")
        self.assertEqual(len(exact), 1024)
        self.assertEqual(shlex.split(exact)[-1], "seen=" + value)
        self._assert_incomplete_in_interactive_shells(exact)
        for receipts, operands in [([value, "last"], []),
                                    (["lane/r1.md", value], ["root with spaces"]),
                                    (["lane/r1.md"], [value, "last"])]:
            with self.subTest(receipts=receipts, operands=operands):
                hint = self.rm.rebind_line({"bind_observation": dict(
                    receipts=receipts, command_roots=operands)}, "sess")
                self.assertLessEqual(len(hint), 1024)
                try:
                    words = shlex.split(hint)
                except ValueError as error:
                    self.fail(f"truncation left an open quote: {error}")
                self.assertRegex(words[-1], r"^dropped=[1-9][0-9]*$")
                self._assert_incomplete_in_interactive_shells(hint)

    def test_runnable_approval_prefixes_preserve_hook_and_status_arguments(self):
        import shlex
        root = self.base / "root with ' quote: text"
        (root / ".engine").mkdir(parents=True)
        receipt = "lane/it's a: receipt.md"
        write_index(root / "INDEX.md", row(1, file=receipt))
        response = json.dumps(dict(path=receipt, render_state="rendered", duplicate=False))
        expected = ["python3", str(SCRIPT.resolve()), "bind", "--manual", "--session", "sess",
                    "--root", str(root), "--anchor", receipt]
        for provenance, prefix in [("command", "rebind, may need operator approval: "),
                                   ("inferred", "rebind (root inferred, check it, may need operator approval): ")]:
            with self.subTest(provenance=provenance):
                command = ("tools/relay submit d.md --root " + shlex.quote(str(root))
                           if provenance == "command" else "time tools/relay submit d.md")
                payload = json.loads(self.payload(command, response, call_id=provenance))
                payload["cwd"] = str(root)
                rc, output = self._hook_output(json.dumps(payload))
                self.assertEqual(rc, 0)
                self.assertEqual(self.store.load()["bind_observation"]["root_provenance"], provenance)
                context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
                with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
                    status = self.rm.status_line(self.store, None).splitlines()[-1]
                for line in (context, status):
                    self.assertTrue(line.startswith(prefix))
                    self.assertEqual(shlex.split(line.split(": ", 1)[1]), expected)
                    self.assertLessEqual(len(line), 1024)
                    self.assertTrue(all(32 <= ord(c) <= 126 for c in line))

    def test_runnable_approval_prefixes_count_toward_line_bound(self):
        import shlex
        for provenance, prefix in [("command", "rebind, may need operator approval: "),
                                   ("inferred", "rebind (root inferred, check it, may need operator approval): ")]:
            with self.subTest(provenance=provenance):
                record = dict(root=str(self.root), root_provenance=provenance, receipts=["lane/r1.md"])
                args = ["python3", str(SCRIPT.resolve()), "bind", "--manual", "--session", "s",
                        "--root", str(self.root), "--anchor", "lane/r1.md"]
                session = "s" + "x" * (1024 - len(prefix + shlex.join(args)))
                args[args.index("--session") + 1] = session
                line = self.rm.rebind_line({"bind_observation": record}, session)
                self.assertTrue(line.startswith(prefix))
                self.assertEqual(len(line), 1024)
                self.assertEqual(shlex.split(line.split(": ", 1)[1]), args)
                overflow = self.rm.rebind_line({"bind_observation": record}, session + "x")
                self.assertTrue(overflow.startswith("# rebind: incomplete, supply "))
                self.assertNotIn("bind --manual", overflow)
                self.assertLessEqual(len(overflow), 1024)

    def test_lookup_reports_match_no_match_and_unreadable(self):
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/r1.md"), "match")
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/missing.md"), "no-match")
        with mock.patch.object(self.rm, "read_index", side_effect=PermissionError("fixture unreadable")):
            self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/r1.md"), "unreadable")
        self.index.unlink()
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/r1.md"), "unreadable")
        self.index.write_text("malformed index\n")
        self.assertEqual(self.rm.receipt_row_lookup(str(self.root), "lane/r1.md"), "unreadable")

    def test_inference_allows_readable_nonmatching_second_candidate(self):
        other = self.other_root()
        write_index(other / "INDEX.md", "")
        with mock.patch.object(self.rm, "discover_root_like_cli", return_value=str(other)):
            result = self.rm.attribute_root(self.rm.SubmitParse(False), str(other), {"root": str(self.root)}, [{"path": "lane/r1.md"}], None, ([], False))
        self.assertEqual(result, (str(self.root), "inferred"))

    def test_inference_withheld_when_second_candidate_unreadable_in_either_order(self):
        other = self.other_root()
        real_read = self.rm.read_index
        ordered = []
        def observe_order(path):
            ordered.append(str(path.parent))
            return real_read(path)
        with mock.patch.object(self.rm, "discover_root_like_cli", return_value=str(self.root)), mock.patch.object(self.rm, "read_index", side_effect=observe_order):
            self.rm.attribute_root(self.rm.SubmitParse(False), str(self.root), {"root": str(other)}, [{"path": "lane/r1.md"}], None, ([], False))
        self.assertEqual(len(ordered), 2)
        self.assertEqual(set(ordered), {str(self.root), str(other)})
        for bad_position in (0, 1):
            for malformed in (False, True):
                with self.subTest(bad_position=bad_position, malformed=malformed):
                    bad = ordered[bad_position]
                    good = ordered[1 - bad_position]
                    write_index(pathlib.Path(good) / "INDEX.md", row(1))
                    pathlib.Path(bad, "INDEX.md").write_text("malformed\n")
                    reads = []
                    def read(path):
                        reads.append(str(path.parent))
                        if str(path.parent) == bad and not malformed:
                            raise PermissionError("fixture unreadable")
                        return real_read(path)
                    with mock.patch.object(self.rm, "discover_root_like_cli", return_value=str(self.root)), mock.patch.object(self.rm, "read_index", side_effect=read):
                        result = self.rm.attribute_root(self.rm.SubmitParse(False), str(self.root), {"root": str(other)}, [{"path": "lane/r1.md"}], None, ([], False))
                    self.assertEqual(result, (None, "none"))
                    self.assertEqual(reads, ordered[:bad_position + 1])

    def _advance_stamps(self):
        # A stray save must change persisted bytes even within one real second.
        import itertools
        ticks = itertools.count()
        patcher = mock.patch.object(self.rm, "now_stamp", side_effect=lambda: f"test-stamp-{next(ticks):08d}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fd_duplication_before_submit_verb_binds(self):
        command = f"tools/relay 2>&1 submit d.md --root {self.root} --key {self.key}"
        self.assertTrue(self.rm.parse_submit_command(command).ok)
        rc, out = self._hook_output(self.payload(command, self.receipt))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(self.store.load()["anchor"], "lane/r1.md")
        self.assertEqual(self.store.load()["binding_gen"], 1)

    def test_hook_context_escapes_non_ascii_and_control_receipts(self):
        for char in ("\v", "\f", "\x85", "\u2028", "\x1b", "é"):
            with self.subTest(char=repr(char)):
                receipt = json.dumps(dict(path=f"lane/a{char}b.md", render_state="rendered", duplicate=False))
                rc, out = self._hook_output(self.payload(f"tools/relay submit d.md --root {self.root}", receipt))
                self.assertEqual(rc, 0)
                hint = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                self.assertEqual(len(hint.splitlines()), 1)
                self.assertTrue(all(32 <= ord(c) <= 126 for c in hint))
                self.assertNotIn("bind --manual", hint)
                self.assertIn(ascii(char)[1:-1], hint)

    def test_hook_context_escapes_non_ascii_and_control_roots(self):
        import shlex
        for char in ("\v", "\f", "\x85", "\u2028", "\x1b", "é"):
            with self.subTest(char=repr(char)):
                root = self.base / f"root{char}name"
                (root / ".engine").mkdir(parents=True)
                rc, out = self._hook_output(self.payload(f"tools/relay submit d.md --root {shlex.quote(str(root))}", self.receipt))
                self.assertEqual(rc, 0)
                self.assertEqual(self.store.load()["bind_observation"]["root"], str(root))
                hint = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                self.assertEqual(len(hint.splitlines()), 1)
                self.assertTrue(all(32 <= ord(c) <= 126 for c in hint))
                self.assertNotIn("bind --manual", hint)
                self.assertIn(ascii(char)[1:-1], hint)

    def _assert_hook_input_is_total(self, payload, mode="bind"):
        result = subprocess.run([sys.executable, str(SCRIPT), mode, "--host", "codex"],
                                input=payload, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if result.stdout.strip():
            self.assertIsInstance(json.loads(result.stdout), dict)

    def _assert_hook_observation(self, payload, reason, provenance, receipt_path):
        self.store.path.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, str(SCRIPT), "bind", "--host", "codex"],
                                input=payload, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stdout.strip(), "seen receipt must produce a hint")
        context = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "PostToolUse")
        hint = context["additionalContext"]
        self.assertTrue(hint.startswith("# rebind: incomplete"))
        self.assertIn("seen=", hint)
        self.assertEqual(len(hint.splitlines()), 1)
        self.assertTrue(self.store.path.exists(), "seen receipt must record an observation")
        observation = self.store.load()["bind_observation"]
        self.assertEqual(observation["reason"], reason)
        self.assertEqual(observation["root_provenance"], provenance)
        self.assertEqual(observation["receipts"], [receipt_path])
        self.assertEqual(observation["root"], str(self.root) if provenance == "command" else None)

    def test_hook_nul_receipt_exits_zero(self):
        receipt = json.dumps(dict(path="lane/nu\0l.md", render_state="rendered", duplicate=False))
        self._assert_hook_observation(self.payload(f"tools/relay submit d.md --root {self.root}", receipt),
                                      "receipt-mismatch", "command", "lane/nu\0l.md")

    def test_hook_nul_root_and_cwd_exit_zero(self):
        for field in ("root", "cwd"):
            with self.subTest(field=field):
                payload = json.loads(self.payload("tools/relay submit d.md", self.receipt))
                if field == "root":
                    payload["tool_input"]["command"] += " --root '/nu\0l'"
                else:
                    payload["cwd"] = "/nu\0l"
                self._assert_hook_observation(json.dumps(payload), "no-root", "none", "lane/r1.md")

    def test_hook_overlong_root_and_cwd_exit_zero(self):
        for field in ("root", "cwd"):
            with self.subTest(field=field):
                payload = json.loads(self.payload("tools/relay submit d.md", self.receipt))
                value = "/" + "x" * 300
                if field == "root":
                    payload["tool_input"]["command"] += " --root " + value
                else:
                    payload["cwd"] = value
                self._assert_hook_observation(json.dumps(payload), "no-root", "none", "lane/r1.md")

    def test_hook_deep_json_exits_zero(self):
        # Inject the decoder failure: actual depth thresholds vary by interpreter.
        for mode in ("bind", "drain", "session-start"):
            with self.subTest(mode=mode):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(self.rm.json, "load", side_effect=RecursionError("injected payload recursion")),
                    mock.patch("sys.stdin", io.StringIO("{}")),
                    mock.patch("sys.stdout", output),
                    mock.patch("sys.stderr", errors),
                ):
                    self.assertEqual(self.rm.main([mode, "--host", "codex"]), 0)
                if mode == "bind":
                    self.assertEqual(output.getvalue(), "")
                elif mode == "drain":
                    self.assertEqual(json.loads(output.getvalue()), {})
                else:
                    self.assertEqual(json.loads(output.getvalue()), {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "relay-monitor: no-session-identity"}})
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertFalse(self.store.path.exists())

    def test_hook_deep_receipt_json_exits_zero(self):
        # Initial payload decoding remains real; inject only receipt decoding.
        decoder = json.JSONDecoder()
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(decoder, "raw_decode", side_effect=RecursionError("injected receipt recursion")),
            mock.patch.object(self.rm.json, "JSONDecoder", return_value=decoder),
            mock.patch("sys.stdin", io.StringIO(self.payload("tools/relay submit d.md", self.receipt))),
            mock.patch("sys.stdout", output),
            mock.patch("sys.stderr", errors),
        ):
            self.assertEqual(self.rm.main(["bind", "--host", "codex"]), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertFalse(self.store.path.exists())

    def test_quoted_equals_root_option_is_not_an_indication(self):
        command = "tools/relay submit d.md '--root=/B' | cat"
        self.assertEqual(self.rm.root_indications(command), ([], False))
        self.assertEqual(self._indicated_miss(command)["root_provenance"], "inferred")

    def test_submit_ending_in_fd_duplication_parses(self):
        for suffix in ("2>&1", ">&2", "2>&-", "1>&-"):
            with self.subTest(suffix=suffix):
                parsed = self.rm.parse_submit_command(
                    "tools/relay submit d.md --key K --root R " + suffix)
                self.assertTrue(parsed.ok)
                self.assertEqual((parsed.key, parsed.root_operand), ("K", "R"))

    def test_adjacent_fd_is_consumed_separated_number_is_argument(self):
        p = self.rm.parse_submit_command
        for suffix in ("2>&1", "2 >&1"):
            self.assertTrue(p("tools/relay submit d.md " + suffix).ok)
        self.assertIsNone(p("tools/relay submit d.md --root 2>&1").root_operand)
        self.assertEqual(p("tools/relay submit d.md --root 2 >&1").root_operand, "2")
        self.assertEqual(p("tools/relay submit d.md --root '2'>&1").root_operand, "2")

    def test_separated_gt_amp_is_backgrounding(self):
        self.assertEqual(self.rm.parse_submit_command(
            "tools/relay submit d.md > &1").reason, "unsupported-syntax")

    def test_dup_operand_may_follow_blanks(self):
        self.assertTrue(self.rm.parse_submit_command("tools/relay submit d.md 2>& 1").ok)

    def test_dup_with_non_fd_operand_refused(self):
        for suffix in (">&out.txt", ">&", ">&; echo hi"):
            self.assertEqual(self.rm.parse_submit_command(
                "tools/relay submit d.md " + suffix).reason, "unsupported-syntax")

    def test_submit_with_ampersand_redirect_refused(self):
        for suffix in ("&>out", "&>>out"):
            self.assertEqual(self.rm.parse_submit_command(
                "tools/relay submit d.md " + suffix).reason, "unsupported-syntax")

    def test_submit_backgrounded_refused(self):
        self.assertEqual(self.rm.parse_submit_command(
            "tools/relay submit d.md &").reason, "unsupported-syntax")

    def test_heredoc_controls_report_unsupported_syntax(self):
        for suffix in ("<<EOF\nx\nEOF", "<<<x"):
            self.assertEqual(self.rm.parse_submit_command(
                "tools/relay submit d.md " + suffix).reason, "unsupported-syntax")

    def setUp(self):
        super().setUp()
        write_index(self.index, row(1, frm="a.planner", to="orch.orchestrator-planner"))
        (self.root / "lane" / "r1.md").write_text("x\n")
        self.store = self.rm.NoteStore("sess")
        self.receipt = (
            '{"advisories":[],"duplicate":false,"path":"lane/r1.m'
            'd","render_state":"rendered"}'
        )
        self.key = str(self.root / ".engine" / "seats" / "a.planner" / "k.key")

    def payload(self, command, stdout=None, host="claude", call_id=None):
        base = {
            "session_id": "sess",
            "cwd": str(self.root),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        if host == "claude":
            base["prompt_id"] = "p"
            base["tool_response"] = {
                "stdout": stdout or "",
                "stderr": "",
                "interrupted": False,
            }
        else:
            base.update(
                {
                    "turn_id": "t",
                    "tool_use_id": "u",
                    "transcript_path": None,
                    "model": "m",
                    "permission_mode": "default",
                }
            )
            base["tool_response"] = {
                "output": stdout or "",
                "metadata": {"exit_code": 0},
            }
        if call_id is not None:
            base["tool_use_id"] = call_id
        return json.dumps(base)

    def run_bind(self, payload_text):
        with mock.patch("sys.stdin", io.StringIO(payload_text)):
            return self.rm.bind(ns())

    def test_submit_parse_table(self):
        cases = (
            ("tools/relay submit d.md --key K --root R", "K", "R"),
            ("relay submit d.md --root R --key K", "K", "R"),
            ("/opt/relay submit d.md --key K --root R", "K", "R"),
            ("RELAY_KEY=K tools/relay submit d.md --root R", "K", "R"),
            ("X=1 RELAY_KEY=K tools/relay submit d.md --root R", "K", "R"),
            (
                "tools/relay submit d.md --admits-against boot.md --key K --root R",
                "K",
                "R",
            ),
        )
        for bare, key, root in cases:
            for cd in ("", "cd X && "):
                for interpreter in (
                    "",
                    "python3 ",
                    "python ",
                    "py ",
                    "python3.12 ",
                    "/usr/bin/python3 ",
                ):
                    with self.subTest(bare=bare, cd=cd, interpreter=interpreter):
                        words = bare.split(" ")
                        prefix = next(
                            (
                                i
                                for i, word in enumerate(words)
                                if word.endswith("relay")
                            ),
                            0,
                        )
                        command = cd + " ".join(
                            words[:prefix]
                            + ([interpreter.strip()] if interpreter else [])
                            + words[prefix:]
                        )
                        got = self.rm.parse_submit_command(command)
                        self.assertEqual(
                            (
                                got.ok,
                                got.reason,
                                got.key,
                                got.root_operand,
                                got.cd_prefix,
                            ),
                            (True, None, key, root, "X" if cd else None),
                        )
        for command in (
            "python3 tools/relay submit d.md --key K --admits-against boot.md --root R",
            "python3 tools/relay submit d.md --admits-against boot.md --root R --key K",
        ):
            with self.subTest(command=command):
                got = self.rm.parse_submit_command(command)
                self.assertEqual((got.ok, got.key, got.root_operand), (True, "K", "R"))

    def test_interpreter_prefixed_submit_binds(self):
        command = f"python3 tools/relay submit d.md --key {self.key} --root {self.root}"
        self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
        note = self.store.load()
        self.assertEqual(
            (note["seat"], note["binding_source"], note["bind_observation"]),
            ("a.planner", "hook", None),
        )

    def test_near_miss_reason(self):
        for command in (
            "python3 -B tools/relay submit d.md",
            "env python3 tools/relay submit d.md",
            "/usr/bin/env python3 tools/relay submit d.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.rm.parse_submit_command(command).reason, "submit-near-miss"
                )

    def test_near_miss_requires_executed_relay(self):
        for command in (
            "python3 other.py tools/relay submit d.md",
            "python3 -c 'pass' tools/relay submit d.md",
            "python3 -cpass tools/relay submit d.md",
            "python3 -mhttp.server tools/relay submit d.md",
            "env echo tools/relay submit d.md",
            "echo tools/relay submit d.md",
            "python3 -B 'tools/relay' submit d.md",
            "python3 -B tools/relay 'submit' d.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.rm.parse_submit_command(command).reason, "no-submit"
                )

    def test_launcher_and_option_operand_near_misses(self):
        commands = (
            "uv run tools/relay submit d.md",
            "uv run python3 tools/relay submit d.md",
            "/usr/bin/uv run python3 -W ignore tools/relay submit d.md",
            "python3 -W ignore tools/relay submit d.md",
            "python3 -X dev tools/relay submit d.md",
            "python3 -BW ignore -Xdev tools/relay submit d.md",
            "env python3 -W ignore tools/relay submit d.md",
            "python3 -- tools/relay submit d.md",
        )
        for command in commands:
            with self.subTest(command=command, bound=False):
                self.store.path.unlink(missing_ok=True)
                self.assertEqual(
                    self.rm.parse_submit_command(command).reason, "submit-near-miss"
                )
                before = self.rm.readiness(self.store)[0]
                self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
                self.assertEqual(self.rm.readiness(self.store)[0], before)
                self.assertEqual(self.store.load()["bind_observation"]["reason"], "submit-near-miss")
                self.assertRegex(
                    self.rm.status_line(self.store, None).splitlines()[0],
                    r" binding=none .* miss=submit-near-miss$",
                )
        self.store.path.unlink(missing_ok=True)
        self._bound_following()
        fields = (
            "seat",
            "root",
            "anchor",
            "binding_source",
            "binding_gen",
            "phase",
            "progress",
            "printed",
        )
        before = self.store.load()
        for command in commands:
            with self.subTest(command=command, bound=True):
                self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
                after = self.store.load()
                self.assertEqual(
                    {field: after[field] for field in fields},
                    {field: before[field] for field in fields},
                )
                self.assertEqual(after["bind_observation"]["reason"], "submit-near-miss")
                self.assertEqual(self.rm.readiness(self.store)[0], "armed")
                with mock.patch.object(
                    self.rm, "check_identity", return_value=("ok", {}, {})
                ):
                    self.assertTrue(
                        self.rm.status_line(self.store, None)
                        .splitlines()[0]
                        .endswith("miss=submit-near-miss")
                    )

    def test_terminal_combined_and_launcher_data_stay_silent(self):
        self._advance_stamps()
        commands = (
            "python3 -Bcpass tools/relay submit d.md",
            "python3 -Bmhttp.server tools/relay submit d.md",
            "python3 -V tools/relay submit d.md",
            "python3 -BV tools/relay submit d.md",
            "python3 --version tools/relay submit d.md",
            "python3 -h tools/relay submit d.md",
            "python3 --help tools/relay submit d.md",
            "python3 -W tools/relay submit d.md",
            "python3 -X tools/relay submit d.md",
            "uv run echo tools/relay submit d.md",
            "uv run python3 -Bcpass tools/relay submit d.md",
            "uv run python3 -V tools/relay submit d.md",
            "uv run 'tools/relay' submit d.md",
            "uv run tools/relay 'submit' d.md",
            "env -u tools/relay submit d.md",
        )
        for bound in (False, True):
            if bound:
                self._bound_following()
            for command in commands:
                with self.subTest(command=command, bound=bound):
                    self.assertEqual(
                        self.rm.parse_submit_command(command).reason, "no-submit"
                    )
                    before = (
                        self.store.path.read_bytes()
                        if self.store.path.exists()
                        else None
                    )
                    binding_before = self.store.load()
                    readiness_before = self.rm.readiness(self.store)[0]
                    self.assertEqual(
                        self.run_bind(self.payload(command, self.receipt)), 0
                    )
                    after = (
                        self.store.path.read_bytes()
                        if self.store.path.exists()
                        else None
                    )
                    if "'tools/relay'" in command or "'submit'" in command:
                        self.assertEqual(after, before)
                    else:
                        note = self.store.load()
                        self.assertEqual(note["bind_observation"]["reason"], "submit-near-miss")
                        for field in ("seat", "root", "anchor", "binding_source", "binding_gen", "progress", "printed"):
                            self.assertEqual(note[field], binding_before[field])
                        self.assertEqual(self.rm.readiness(self.store)[0], readiness_before)

    def test_launcher_near_miss_preserves_mixed_precedence(self):
        good = f"tools/relay submit d.md --key {self.key} --root {self.root}"
        for bad in (
            "uv run tools/relay submit other.md",
            "python3 -W ignore tools/relay submit other.md",
        ):
            for command in (f"{good}; {bad}", f"{bad}; {good}"):
                with self.subTest(command=command):
                    self.store.path.unlink(missing_ok=True)
                    self.assertTrue(self.rm.parse_submit_command(command).ok)
                    self.assertEqual(
                        self.run_bind(self.payload(command, self.receipt)), 0
                    )
                    note = self.store.load()
                    self.assertEqual(note["seat"], "a.planner")
                    self.assertIsNone((note.get("bind_observation") or {}).get("reason"))
                    self.assertIsNone(note["bind_observation"])

    def test_unrelated_commands_stay_silent(self):
        self._advance_stamps()
        for command in (
            "git status",
            "tools/relay show d.md",
            "python3 tools/relay lint d.md",
            "python3 tools/relay-lint.py d.md",
            "python3 tools/adapters/relay-monitor.py status",
            "echo 'tools/relay submit d.md'",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.rm.parse_submit_command(command).reason, "no-submit"
                )
                before = (
                    self.store.path.read_bytes() if self.store.path.exists() else None
                )
                self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
                after = (
                    self.store.path.read_bytes() if self.store.path.exists() else None
                )
                self.assertEqual(after, before)

    def test_near_miss_visible_when_unbound(self):
        before = self.rm.readiness(self.store)[0]
        self.assertEqual(
            self.run_bind(
                self.payload("python3 -B tools/relay submit d.md", self.receipt)
            ),
            0,
        )
        self.assertEqual(self.rm.readiness(self.store)[0], before)
        self.assertEqual(self.store.load()["bind_observation"]["reason"], "submit-near-miss")
        self.assertRegex(
            self.rm.status_line(self.store, None).splitlines()[0],
            r" binding=none .* miss=submit-near-miss$",
        )

    def _bound_following(self):
        command = f"tools/relay submit d.md --key {self.key} --root {self.root}"
        self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
        leader = self.rm.leader_record()
        lock = self.rm.LeaderLock("sess")
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        lock.stamp(leader["instance"])
        self.store.update(
            lambda note: note.update({"leader": leader, "phase": "following"})
        )

    def test_near_miss_leaves_bound_seat_delivering(self):
        self._bound_following()
        fields = (
            "seat",
            "root",
            "anchor",
            "binding_source",
            "binding_gen",
            "phase",
            "progress",
            "printed",
        )
        before = self.store.load()
        self.assertEqual(self.rm.readiness(self.store)[0], "armed")
        self.assertEqual(
            self.run_bind(
                self.payload("env python3 tools/relay submit d.md", self.receipt)
            ),
            0,
        )
        after = self.store.load()
        self.assertEqual(
            {field: after[field] for field in fields},
            {field: before[field] for field in fields},
        )
        self.assertEqual(after["bind_observation"]["reason"], "submit-near-miss")
        self.assertEqual(self.rm.readiness(self.store)[0], "armed")
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            self.assertTrue(
                self.rm.status_line(self.store, None)
                .splitlines()[0]
                .endswith("miss=submit-near-miss")
            )

    def test_successful_bind_clears_miss(self):
        self.assertEqual(
            self.run_bind(
                self.payload("python3 -B tools/relay submit d.md", self.receipt)
            ),
            0,
        )
        self.assertTrue(
            self.rm.status_line(self.store, None)
            .splitlines()[0]
            .endswith("miss=submit-near-miss")
        )
        command = f"python3 tools/relay submit d.md --key {self.key} --root {self.root}"
        self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            self.assertNotIn("miss=", self.rm.status_line(self.store, None))

        self.run_bind(self._miss())
        self.assertEqual(self._manual(), 0)
        self.assertIsNone(self.store.load().get("bind_observation"))

    def test_later_near_miss_survives_successful_bind(self):
        first_miss = "python3 -B tools/relay submit first.md"
        later_miss = "env python3 tools/relay submit later.md"
        self.assertEqual(self.run_bind(self.payload(first_miss, self.receipt)), 0)
        original = self.rm.bind_from_receipt

        def bind_then_miss(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertTrue(result[0])
            self.assertEqual(self.run_bind(self.payload(later_miss, self.receipt)), 0)
            return result

        command = f"tools/relay submit d.md --key {self.key} --root {self.root}"
        with mock.patch.object(
            self.rm, "bind_from_receipt", side_effect=bind_then_miss
        ):
            self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
        note = self.store.load()
        self.assertEqual(
            (note["seat"], note["binding_source"], note["bind_observation"]["reason"]),
            ("a.planner", "hook", "submit-near-miss"),
        )
        self.assertEqual((note.get("bind_observation") or {}).get("reason"), "submit-near-miss")
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            self.assertTrue(
                self.rm.status_line(self.store, None)
                .splitlines()[0]
                .endswith("miss=submit-near-miss")
            )

    def test_earlier_near_miss_clears_at_successful_bind(self):
        command = f"tools/relay submit d.md --key {self.key} --root {self.root}"
        prior = "python3 -B tools/relay submit prior.md"
        just_before = "env python3 tools/relay submit just-before.md"
        original = self.rm.bind_from_receipt
        for has_prior in (False, True):
            with self.subTest(has_prior=has_prior):
                self.store.path.unlink(missing_ok=True)
                if has_prior:
                    self.assertEqual(
                        self.run_bind(self.payload(prior, self.receipt)), 0
                    )

                def miss_then_bind(*args, **kwargs):
                    self.assertEqual(
                        self.run_bind(self.payload(just_before, self.receipt)), 0
                    )
                    return original(*args, **kwargs)

                with mock.patch.object(
                    self.rm, "bind_from_receipt", side_effect=miss_then_bind
                ):
                    self.assertEqual(
                        self.run_bind(self.payload(command, self.receipt)), 0
                    )
                    self.assertEqual(self._manual(), 0)
                note = self.store.load()
                self.assertEqual((note["seat"], note["binding_gen"]), ("a.planner", 1))
                self.assertIsNone((note.get("bind_observation") or {}).get("reason"))
                with mock.patch.object(
                    self.rm, "check_identity", return_value=("ok", {}, {})
                ):
                    self.assertNotIn("miss=", self.rm.status_line(self.store, None))

    def test_mixed_accepted_and_near_miss_keep_todays_result(self):
        good = f"tools/relay submit d.md --key {self.key} --root {self.root}"
        bad = "python3 -B tools/relay submit other.md"
        for command in (f"{good}; {bad}", f"{bad}; {good}", f"{good} && {bad}"):
            with self.subTest(command=command):
                self.store.path.unlink(missing_ok=True)
                parsed = self.rm.parse_submit_command(command)
                self.assertEqual(
                    (parsed.ok, parsed.key, parsed.root_operand),
                    (True, self.key, str(self.root)),
                )
                self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
                note = self.store.load()
                self.assertEqual(
                    (note["seat"], note["bind_observation"], (note.get("bind_observation") or {}).get("reason")),
                    ("a.planner", None, None),
                )
                self.assertEqual(
                    self.run_bind(
                        self.payload(command, self.receipt + "\n" + self.receipt)
                    ),
                    0,
                )
                self.assertEqual(
                    self.store.load()["bind_observation"]["reason"], "ambiguous-receipt"
                )

    def test_interpreter_submits_follow_bare_rules(self):
        for command, reason in (
            (
                "python3 tools/relay submit a; python3 tools/relay submit b",
                "ambiguous-command",
            ),
            ("python3 tools/relay submit a --key K | tail -3", "unsupported-syntax"),
        ):
            with self.subTest(command=command):
                bare = command.replace("python3 tools/relay", "tools/relay")
                self.assertEqual(self.rm.parse_submit_command(command).reason, reason)
                self.assertEqual(self.rm.parse_submit_command(bare).reason, reason)

    def test_near_miss_inside_unsupported_shell(self):
        for command in (
            "python3 -B tools/relay submit a 2>&1 | tail -3",
            "python3 -B tools/relay submit a; python3 -B tools/relay submit b",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.rm.parse_submit_command(command).reason, "submit-near-miss"
                )
        self._bound_following()
        self.assertEqual(
            self.run_bind(
                self.payload(
                    "python3 -B tools/relay submit a 2>&1 | tail -3", self.receipt
                )
            ),
            0,
        )
        self.assertEqual(self.store.load()["bind_observation"]["reason"], "submit-near-miss")
        self.assertEqual(self.rm.readiness(self.store)[0], "armed")
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            self.assertTrue(
                self.rm.status_line(self.store, None)
                .splitlines()[0]
                .endswith("miss=submit-near-miss")
            )

    def test_lexer_and_grammar(self):
        p = self.rm.parse_submit_command
        self.assertTrue(p("tools/relay submit .engine/drafts/x.md --key K --root R").ok)
        self.assertEqual(
            p(
                "tools/relay submit a.md --key K\ntools/relay submit b.md --key K"
            ).reason,
            "ambiguous-command",
        )  # newline-separated submits (P5)
        self.assertEqual(
            p("tools/relay submit d.md --root ';' --key K").root_operand, ";"
        )  # quoted punctuation is an operand
        self.assertEqual(
            p("ls\ncd /w && tools/relay submit d.md --key K").reason,
            "unsupported-syntax",
        )  # non-leading cd after a newline
        self.assertTrue(p("cd /w && tools/relay submit d.md --key K").ok)
        self.assertTrue(p("RELAY_KEY=K tools/relay submit d.md").ok)
        self.assertEqual(p("RELAY_KEY='K' tools/relay submit d.md").key, "K")
        self.assertEqual(p('RELAY_KEY="K" tools/relay submit d.md').key, "K")
        self.assertEqual(
            p("RELAY_KEY='/p/with space/k' ADT_X=1 tools/relay submit d.md").key,
            "/p/with space/k",
        )
        self.assertEqual(p('"RELAY_KEY=K" tools/relay submit d.md').reason, "no-submit")
        self.assertEqual(
            p("echo 'RELAY_KEY=K tools/relay submit d.md'").reason, "no-submit"
        )
        for bad in (
            'RELAY_"KEY"=K tools/relay submit d.md',
            'RELAY_KEY"="K tools/relay submit d.md',
            "RELAY_KEY\\=K tools/relay submit d.md",
            "\\RELAY_KEY=K tools/relay submit d.md",
        ):
            self.assertEqual(p(bad).reason, "no-submit", bad)
        self.assertEqual(
            p('tools/relay submit d.md --root "/p/with space" --key K').root_operand,
            "/p/with space",
        )
        self.assertEqual(p("echo 'tools/relay submit d.md'").reason, "no-submit")
        self.assertEqual(
            p("tools/relay submit a.md --key K;tools/relay submit b.md --key K").reason,
            "ambiguous-command",
        )
        self.assertEqual(
            p(
                "tools/relay submit a.md --key K; tools/relay submit b.md --key K"
            ).reason,
            "ambiguous-command",
        )
        self.assertEqual(
            p("(cd /x; tools/relay submit d.md --key K)").reason, "unsupported-syntax"
        )
        self.assertEqual(
            p("cat d.md | tools/relay submit - --key K").reason, "unsupported-syntax"
        )
        self.assertEqual(
            p("tools/relay submit d.md --key K 2>&1 | tail -3").reason,
            "unsupported-syntax",
        )
        self.assertEqual(
            p("echo x; cd /w && tools/relay submit d.md --key K").reason,
            "unsupported-syntax",
        )
        self.assertEqual(
            p("tools/relay submit d.md --key K # --root R").root_operand, None
        )
        self.assertEqual(p("tools/relay submit 'd#1.md' --key K").ok, True)
        self.assertEqual(p("tools/relay submit d.md --key K >/dev/null").ok, True)
        self.assertEqual(p("tools/relay submit d.md a=b --key K").key, "K")
        self.assertEqual(
            p("tools/relay submit d.md --key K; echo 'a\"b").reason, "unsupported-syntax"
        )

    def test_quoted_assignment_key_binds_and_a_quoted_foreign_key_degrades(self):
        self.assertEqual(
            self.run_bind(
                self.payload(
                    f"RELAY_KEY='{self.key}' tools/relay submit d.md "
                    f"--root "
                    f"{self.root}",
                    self.receipt,
                )
            ),
            0,
        )
        n = self.store.load()
        self.assertEqual(
            (n["seat"], n["binding_gen"], n.get("bind_observation")),
            ("a.planner", 1, None),
        )
        foreign = (
            f'RELAY_KEY="{self.root}/.engine/seats/z.planner/k.key" '
            f"tools/relay submit d.md --root "
            f"{self.root}"
        )
        self.run_bind(self.payload(foreign, self.receipt))
        n = self.store.load()
        self.assertEqual(n["bind_observation"]["reason"], "key-mismatch")
        self.assertEqual(n["binding_gen"], 1)

    def test_plain_submit_binds_and_initializes_progress_once(self):
        self._advance_stamps()
        cmd = f"tools/relay submit d.md --key {self.key} --root {self.root}"
        self.assertEqual(self.run_bind(self.payload(cmd, self.receipt)), 0)
        n = self.store.load()
        self.assertEqual(
            (n["seat"], n["root"], n["anchor"], n["binding_gen"], n["binding_source"]),
            ("a.planner", str(self.root), "lane/r1.md", 1, "hook"),
        )
        self.assertEqual(
            n["progress"][str(self.root)], {"file": "lane/r1.md", "position": 1}
        )
        self.assertEqual(
            n["floors"],
            [
                {
                    "root": str(self.root),
                    "seat": "a.planner",
                    "file": "lane/r1.md",
                    "position": 1,
                }
            ],
        )
        self.store.update(
            lambda x: (
                x["progress"].__setitem__(
                    str(self.root), {"file": "lane/r9.md", "position": 9}
                ),
                x["printed"].__setitem__(str(self.root), ["lane/r9.md"]),
            )
        )
        before_repeat = self.store.path.read_bytes()
        self.run_bind(self.payload(cmd, self.receipt))
        self.assertEqual(self.store.path.read_bytes(), before_repeat)
        n = self.store.load()
        self.assertEqual(n["binding_gen"], 1)
        self.assertEqual(n["progress"][str(self.root)]["file"], "lane/r9.md")
        self.assertEqual(n["printed"][str(self.root)], ["lane/r9.md"])
        self.assertEqual(n["floors"][0]["file"], "lane/r1.md")
        self.assertEqual(len(n["floors"]), 1)  # a rebind appends nothing

    def other_root(self):
        other = self.base / "other" / ".relays" / "v2"
        (other / ".engine" / "seats" / "a.planner").mkdir(parents=True)
        (other / "lane").mkdir()
        write_index(other / "INDEX.md", row(1, frm="a.planner", to="orch"))
        (other / "lane" / "r1.md").write_text("y\n")
        return other

    def test_root_or_seat_change_resets_progress_and_ring_but_leaves_frame(self):
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
            )
        )
        frame = {
            "identity": {
                "root": str(self.root),
                "seat": "a.planner",
                "binding_gen": 1,
                "leader": "L",
                "file": "lane/r9.md",
            },
            "data": "00",
            "accepted": 1,
        }
        self.store.update(
            lambda x: (
                x["printed"].__setitem__(str(self.root), ["lane/r1.md"]),
                x.__setitem__("frame", frame),
            )
        )
        other = self.other_root()
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key "
                f"{other}/.engine/seats/a.planner/k.key --root "
                f"{other}",
                self.receipt,
            )
        )
        n = self.store.load()
        self.assertEqual(n["root"], str(other))
        self.assertEqual(n["frame"], frame)
        self.assertEqual(n["printed"].get(str(other), []), [])
        self.assertEqual(n["progress"][str(other)]["file"], "lane/r1.md")

    def _real_bind_mid_frame(self, other_rows):
        # follower bound to root1, addressed row r2 from orch; the bind then switches to
        # a root whose index holds `other_rows`
        write_index(
            self.index,
            row(1, frm="a.planner", to="orch")
            + row(2, frm="orch.orchestrator-planner", to="a.planner"),
        )
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
            )
        )
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        snap = self.rm.read_index(self.index)
        sink = StallingSink(accept_first=5)
        r = self.rm.deliver_once(
            self.store,
            sink,
            binding,
            snap,
            end=time.monotonic() + 0.2,
            leader_instance="L",
        )
        self.assertEqual(r.aborted, "output-stalled")
        other = self.other_root()
        write_index(other / "INDEX.md", other_rows)
        ok, _ = self.rm.bind_from_receipt(
            self.store,
            root=str(other),
            receipt_path="lane/r1.md",
            key_seat=None,
            source="hook",
        )
        self.assertTrue(ok)
        self.assertIsNotNone(
            self.store.load()["frame"]
        )  # the real bind left the prefix evidence in place
        sink.released = True
        new_binding = self.rm.resolve_binding(self.store.load(), {}, None)
        with self.assertRaises(self.rm.TransportFailed):
            self.rm.deliver_once(
                self.store,
                sink,
                new_binding,
                self.rm.read_index(other / "INDEX.md"),
                end=time.monotonic() + 0.5,
                leader_instance="L",
            )
        self.assertEqual(len(sink.buf), 5)  # no new-root bytes on the damaged stream

    def test_real_bind_during_paused_follower_frame_abandons_with_empty_tail(self):
        self._real_bind_mid_frame(
            row(1, frm="a.planner", to="orch")
        )  # only the anchor: zero candidates, still abandons (R2)

    def test_real_bind_during_paused_follower_frame_abandons_with_addressed_row_control(
        self,
    ):
        self._real_bind_mid_frame(
            row(1, frm="a.planner", to="orch")
            + row(2, frm="orch.orchestrator-planner", to="a.planner")
        )  # an addressed row waits in the new root: still abandons, still no bytes

    def test_discovery_forms(self):
        below = self.root / "lane"
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {below}", self.receipt
            )
        )
        self.assertEqual(self.store.load()["root"], str(self.root))
        self.store.path.unlink()
        self.run_bind(
            self.payload(f"tools/relay submit d.md --key {self.key}", self.receipt)
        )
        self.assertEqual(self.store.load()["root"], str(self.root))
        self.store.path.unlink()
        self.run_bind(
            self.payload(f"cd {self.root} && tools/relay submit d.md", self.receipt)
        )
        self.assertEqual(self.store.load()["seat"], "a.planner")

    def test_codex_shaped_payload_binds_through_string_leaves(self):
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
                host="codex",
            )
        )
        self.assertEqual(self.store.load()["seat"], "a.planner")

    def test_response_strings_keeps_document_order_and_every_leaf(self):  # T4-R1
        rs = self.rm.response_strings
        self.assertEqual(
            rs({"z": "first", "a": "second"}), ["first", "second"]
        )  # insertion order, not sorted
        self.assertEqual(
            rs({"output": "same", "metadata": {"repeat": "same"}}), ["same", "same"]
        )  # equal strings at distinct leaves stay distinct
        self.assertEqual(
            rs({"stdout": "s", "stderr": "e", "nested": {"stdout": "inner"}}),
            ["s", "e", "inner"],
            # prioritized top-level stdout once; a nested key named stdout is an
            # ordinary
            # leaf
        )
        self.assertEqual(rs("bare"), ["bare"])
        self.assertEqual(rs({"a": ["x", {"b": "y"}]}), ["x", "y"])

    def test_equal_receipts_are_ambiguous_and_single_stdout_is_scanned_once(
        self,
    ):  # T4-R1
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
            )
        )
        before = self.store.load()
        self.assertEqual(
            len(
                self.rm.response_strings(
                    json.loads(self.payload("x", self.receipt))["tool_response"]
                )
            ),
            2,
        )  # stdout once, stderr once; interrupted is a boolean, not a leaf
        self.assertEqual(before["binding_gen"], 1)
        self.assertIsNone(
            before["bind_observation"]
        )  # the single-stdout control: bound exactly once
        codex = json.loads(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
                host="codex",
            )
        )
        codex["tool_response"]["metadata"]["repeat"] = (
            self.receipt
        )  # the same receipt at a second leaf
        self.assertEqual(self.run_bind(json.dumps(codex)), 0)
        n = self.store.load()
        self.assertEqual(n["bind_observation"]["reason"], "ambiguous-receipt")
        self.assertEqual(
            (n["seat"], n["root"], n["binding_gen"]),
            (before["seat"], before["root"], before["binding_gen"]),
        )  # prior binding preserved

    def test_negatives_preserve_prior_binding(self):
        self._advance_stamps()
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
            )
        )
        before = self.store.load()
        cases = {
            "quoted": (
                f"echo 'tools/relay submit d.md --key {self.key}'",
                self.receipt,
                None,
            ),
            "two-attached": (
                f"tools/relay submit a.md --key "
                f"{self.key};tools/relay submit b.md --key "
                f"{self.key}",
                self.receipt + "\n" + self.receipt,
                "ambiguous-command",
            ),
            "two-newline": (
                f"tools/relay submit a.md --key "
                f"{self.key}\ntools/relay submit b.md --key "
                f"{self.key}",
                self.receipt + "\n" + self.receipt,
                "ambiguous-command",
            ),
            "subshell": (
                f"(tools/relay submit d.md --key {self.key})",
                self.receipt,
                "unsupported-syntax",
            ),
            "key-mismatch": (
                f"tools/relay submit d.md --key "
                f"{self.root}/.engine/seats/z.planner/k.key",
                self.receipt,
                "key-mismatch",
            ),
            "foreign-row": (
                f"tools/relay submit d.md --key {self.key}",
                self.receipt.replace("lane/r1.md", "lane/nope.md"),
                "receipt-mismatch",
            ),
            "escape-path": (
                f"tools/relay submit d.md --key {self.key}",
                self.receipt.replace("lane/r1.md", "../../x.md"),
                "receipt-mismatch",
            ),
            "redirected": (
                f"tools/relay submit d.md --key {self.key} >/dev/null",
                "",
                "no-receipt",
            ),
            "escaped-envelope": (
                f"tools/relay submit d.md --key {self.key}",
                json.dumps(self.receipt),
                "no-receipt",
            ),
            "failed": (
                f"tools/relay submit d.md --key {self.key}",
                '{"code":"E-ENVELOPE"}',
                "no-receipt",
            ),
            "two-receipts": (
                f"tools/relay submit d.md --key {self.key}",
                self.receipt + "\n" + self.receipt,
                "ambiguous-receipt",
            ),
        }
        for name, (cmd, out, reason) in cases.items():
            with self.subTest(name):
                prior_bytes = self.store.path.read_bytes()
                self.assertEqual(self.run_bind(self.payload(cmd, out)), 0)
                n = self.store.load()
                self.assertEqual(
                    (n["seat"], n["root"], n["binding_gen"]),
                    (before["seat"], before["root"], before["binding_gen"]),
                )
                if reason is None or reason == "no-receipt":
                    self.assertEqual(self.store.path.read_bytes(), prior_bytes)
                else:
                    self.assertEqual(n["bind_observation"]["reason"], reason)

    def test_non_submit_shell_commands_preserve_note_bytes(self):
        self._advance_stamps()
        self.run_bind(
            self.payload(
                f"tools/relay submit d.md --key {self.key} --root {self.root}",
                self.receipt,
            )
        )
        before = self.store.path.read_bytes()
        commands = (
            "ls | head",
            "sleep 5 &",
            "(cd x && make)",
            "cat < f",
            "echo 'unfinished",
            'echo "unfinished',
            "echo 'tools/relay submit unfinished",
            "echo tools/relay submit | head",
            "ls; cd x",
            "cat <<'EOF'\ntools/relay submit draft.md\nEOF\n",
            "cat <<EOF\ntools/relay submit draft.md\nEOF\n",
            "cat <<-EOF\n\ttools/relay submit draft.md\n\tEOF\n",
        )
        for command in commands:
            with self.subTest(command=command):
                self.store.path.write_bytes(before)
                self.assertEqual(self.run_bind(self.payload(command)), 0)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_heredoc_data_preserves_binding_and_pending_stop_delivery(self):
        self._advance_stamps()
        commands = (
            "cat <<'EOF'\ntools/relay submit draft.md\nEOF\n",
            "cat <<EOF\ntools/relay submit draft.md\nEOF\n",
            "cat <<-EOF\n\ttools/relay submit draft.md\n\tEOF\n",
        )
        for command in commands:
            with self.subTest(command=command):
                self.store.path.unlink(missing_ok=True)
                write_index(self.index, row(1, frm="a.planner", to="orch"))
                self.assertEqual(
                    self.run_bind(
                        self.payload(
                            f"tools/relay submit d.md --key {self.key} "
                            f"--root {self.root}",
                            self.receipt,
                        )
                    ),
                    0,
                )
                before = self.store.path.read_bytes()
                write_index(
                    self.index,
                    row(1, frm="a.planner", to="orch")
                    + row(2, frm="b.planner", to="a.planner"),
                )
                self.assertEqual(self.run_bind(self.payload(command)), 0)
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertIsNone(self.store.load()["bind_observation"])
                drain = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "drain",
                        "--host",
                        "codex",
                        "--session",
                        "sess",
                    ],
                    input=json.dumps({"session_id": "sess", "turn_id": "t"}),
                    capture_output=True,
                    text=True,
                    env=dict(os.environ),
                    timeout=5,
                )
                self.assertEqual(drain.returncode, 0, drain.stderr)
                document = json.loads(drain.stdout)
                self.assertEqual(document["decision"], "block")
                self.assertIn('"file":"lane/r2.md"', document["reason"])
                self.assertEqual(
                    self.store.load()["progress"][str(self.root)],
                    {"file": "lane/r2.md", "position": 2},
                )

    def test_submit_after_heredoc_and_uncertainty_controls(self):
        parse = self.rm.parse_submit_command
        for command in (
            "cat <<'EOF'\ndata\nEOF\ntools/relay submit draft.md",
            "cat <<EOF\ntools/relay submit draft.md\n",
            "tools/relay submit draft.md; cat <<'EOF\ndata",
            "cat <<$'E\\x4fF'\ntools/relay submit draft.md\nEOF\n",
        ):
            with self.subTest(command=command):
                self.assertEqual(parse(command).reason, "unsupported-syntax")
        for command in (
            "cat <<EOF\nordinary data\n",
            "cat <<'EOF\nordinary data\n",
            "cat <<$'E\\x4fF'\nordinary data\nEOF\n",
        ):
            with self.subTest(command=command):
                self.assertEqual(parse(command).reason, "no-submit")
        self.assertEqual(
            parse("tools/relay submit a.md; tools/relay submit b.md").reason,
            "ambiguous-command",
        )

    def test_unreadable_submit_with_receipt_records_observation(self):
        commands = (
            "tools/relay submit d.md | head",
            "ls | tools/relay submit d.md",
            "(tools/relay submit d.md)",
            "tools/relay submit 'unfinished",
        )
        self.run_bind(self._submit())
        before = self.store.load()
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.run_bind(self.payload(command, self.receipt)), 0)
                self.assertEqual(
                    self.store.load()["bind_observation"]["reason"],
                    "unsupported-syntax",
                )

                for field in ("seat", "root", "anchor", "binding_gen", "progress"):
                    self.assertEqual(self.store.load()[field], before[field])

    def test_failure_payload_is_negative_control_and_manual_rebind_works(self):
        failure = {
            "session_id": "sess",
            "cwd": str(self.root),
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {
                "command": f"tools/relay submit d.md --key {self.key}; false"
            },
            "error": "Command failed: ...interleaved " + self.receipt[:20],
            "prompt_id": "p",
        }
        self.assertEqual(self.run_bind(json.dumps(failure)), 0)
        self.assertIsNone(self.store.load()["seat"])
        self.assertEqual(
            self.rm.bind(
                ns(
                    session="sess",
                    manual=True,
                    root=str(self.root),
                    anchor="lane/r1.md",
                )
            ),
            0,
        )
        self.assertEqual(self.store.load()["seat"], "a.planner")

    def _hook_output(self, payload):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            rc = self.run_bind(payload)
        return rc, output.getvalue()

    def test_hook_emits_additional_context_on_new_observation(self):
        rc, text = self._hook_output(self._miss("one"))
        self.assertEqual(rc, 0)
        doc = json.loads(text)
        self.assertEqual(set(doc), {"hookSpecificOutput"})
        self.assertEqual(doc["hookSpecificOutput"], {"hookEventName": "PostToolUse", "additionalContext": self.rm.rebind_line(self.store.load(), "sess")})
        self.assertTrue(doc["hookSpecificOutput"]["additionalContext"].startswith("rebind"))

    def test_hook_emits_nothing_for_handled_call(self):
        first = self._miss("one")
        self._hook_output(first)
        self.assertEqual(self._hook_output(first), (0, ""))
        self._manual()
        self.assertEqual(self._hook_output(first), (0, ""))

    def test_hook_emits_nothing_on_successful_bind(self):
        self.assertEqual(self._hook_output(self._submit("one")), (0, ""))
        self.assertEqual(self._hook_output(self._submit("one")), (0, ""))

    def test_hook_never_blocks(self):
        outcomes = ["invalid-json", "[]", self.payload("tools/relay submit d.md"), self.payload("ls", self.receipt), self._submit("ok"), self._miss("miss"), self.payload("tools/relay submit d.md | head", self.receipt), self.payload("tools/relay submit d.md --root /missing", self.receipt), self.payload("tools/relay submit d.md", self.receipt + self.receipt), self.payload("tools/relay submit d.md", self.receipt.replace("r1.md", "missing.md")), self.payload("tools/relay submit d.md --key /x/.engine/seats/z.planner/key", self.receipt)]
        emitted = 0
        for payload in outcomes:
            rc, text = self._hook_output(payload)
            self.assertEqual(rc, 0)
            if text:
                doc = json.loads(text)
                self.assertEqual(set(doc), {"hookSpecificOutput"})
                self.assertEqual(set(doc["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
                self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "PostToolUse")
                self.assertLessEqual(len(doc["hookSpecificOutput"]["additionalContext"]), 1024)
                emitted += 1
        self.assertEqual(emitted, 6)

    def test_second_call_same_display_emits_again(self):
        rc, first = self._hook_output(self._miss("one"))
        rc2, second = self._hook_output(self._miss("two"))
        self.assertEqual((rc, rc2), (0, 0))
        self.assertEqual(json.loads(first), json.loads(second))
        self.assertNotEqual(self.rm.call_identity("sess", {"tool_use_id": "one"}), self.rm.call_identity("sess", {"tool_use_id": "two"}))

    def _submit(self, call=None, root=None, anchor="lane/r1.md"):
        root = root or self.root
        command = f"tools/relay submit d.md --root {root}"
        receipt = self.receipt.replace("lane/r1.md", anchor)
        return self.payload(command, receipt, call_id=call)

    def _manual(self, root=None, anchor="lane/r1.md"):
        return self.rm.bind(ns(manual=True, session="sess", root=str(root or self.root), anchor=anchor))

    def _miss(self, call=None):
        return self.payload("env tools/relay submit d.md", self.receipt, call_id=call)

    def _observation(self):
        value = self.store.load().get("bind_observation")
        self.assertIsInstance(value, dict)
        self.assertEqual(set(value), {"reason", "stamp", "receipts", "root", "root_provenance", "call"})
        return value

    def test_help_piped_to_head_leaves_armed_note_unchanged(self):
        self._advance_stamps()
        self._bound_following()
        before = self.store.path.read_bytes()
        self.assertEqual(self.run_bind(self.payload("tools/relay submit --help | head")), 0)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.rm.readiness(self.store)[0], "armed")

    def test_refused_submit_records_nothing(self):
        self._advance_stamps()
        for bound in (False, True):
            if bound:
                self.run_bind(self._submit())
            before = self.store.path.read_bytes() if self.store.path.exists() else None
            self.assertEqual(self.run_bind(self.payload("tools/relay submit bad.md", '{"code":"E-ENVELOPE"}')), 0)
            self.assertEqual(self.store.path.read_bytes() if self.store.path.exists() else None, before)

    def test_near_miss_shapes_record_observation(self):
        for command in ("py -3 tools/relay submit d.md", "for x in a; do tools/relay submit d.md; done", "if tools/relay submit d.md; then true; fi", "time tools/relay submit d.md"):
            with self.subTest(command=command):
                self.store.path.unlink(missing_ok=True)
                self.run_bind(self.payload(command, self.receipt))
                self.assertEqual(self._observation()["reason"], "submit-near-miss")
                self.assertIsNone(self.store.load()["seat"])

    def test_verb_in_heredoc_body_is_not_a_verb(self):
        for command in ("cat <<EOF\ntools/relay submit d.md\nEOF\n", "echo 'tools/relay submit d.md'"):
            self.run_bind(self.payload(command, self.receipt))
            self.assertFalse(self.store.path.exists())

    def test_displayed_receipt_without_verb_records_nothing(self):
        for command in ("echo receipt", "tools/relay show d.md", "git status"):
            self.run_bind(self.payload(command, self.receipt))
            self.assertFalse(self.store.path.exists())

    def test_unterminated_quote_naming_verb_exits_zero(self):
        self.assertEqual(self.run_bind(self.payload("tools/relay submit d.md 'unfinished", self.receipt)), 0)
        self.assertEqual(self._observation()["reason"], "unsupported-syntax")
        self.store.path.unlink()
        self.run_bind(self.payload("echo 'tools/relay submit unfinished", self.receipt))
        self.assertFalse(self.store.path.exists())

    def test_echo_with_fabricated_receipt_records_suspicion_only(self):
        self.run_bind(self.payload("echo tools/relay submit d.md", self.receipt.replace("r1.md", "fake.md")))
        record = self._observation()
        self.assertEqual((record["root"], record["root_provenance"], record["receipts"]), (None, "none", ["lane/fake.md"]))
        self.assertIsNone(self.store.load()["seat"])

    def test_unexecuted_branch_beside_receipt_binds_nothing(self):
        self.run_bind(self.payload("if false; then tools/relay submit d.md; fi; echo receipt", self.receipt))
        self.assertEqual(self._observation()["reason"], "submit-near-miss")
        self.assertIsNone(self.store.load()["seat"])

    def _indicated_miss(self, command, cwd=None):
        payload = json.loads(self.payload(command, self.receipt))
        if cwd is not None:
            payload["cwd"] = str(cwd)
        self.run_bind(json.dumps(payload))
        return self.store.load()["bind_observation"]

    def test_unreadable_other_root_with_colliding_path_gets_incomplete_hint(self):
        self.run_bind(self._submit("initial"))
        other = self.other_root()
        before = self.store.load()
        record = self._indicated_miss(f"tools/relay submit d.md --root {other} | cat")
        self.assertEqual(record["root_provenance"], "none")
        self.assertEqual(record["command_roots"], [str(other)])
        line = self.rm.rebind_line(self.store.load(), "sess")
        self.assertIn("command text", line)
        self.assertIn(str(other), line)
        self.assertNotIn("bind --manual", line)
        for key in ("seat", "root", "anchor", "binding_gen", "progress"):
            self.assertEqual(self.store.load()[key], before[key])

    def test_unreadable_same_root_operand_keeps_inferred_hint(self):
        record = self._indicated_miss(f"tools/relay submit d.md --root {self.root} | cat")
        self.assertEqual(record["root_provenance"], "inferred")

    def test_quoted_and_equals_root_spellings_withhold_inference(self):
        for operand in ('--root "/B with space"', '--root=/B', '--root="/B with space"'):
            with self.subTest(operand=operand):
                record = self._indicated_miss(f"tools/relay submit d.md {operand} | cat")
                self.assertEqual(record["root_provenance"], "none")

    def test_dynamic_root_operand_withholds_inference(self):
        record = self._indicated_miss('tools/relay submit d.md --root "$R" | cat')
        self.assertEqual(record["root_provenance"], "none")
        self.assertEqual(record["command_roots"], ['"$R"'])

    def test_single_quoted_dollar_is_static_double_quoted_is_dynamic(self):
        candidate = self.base / "$literal"
        (candidate / ".engine").mkdir(parents=True)
        write_index(candidate / "INDEX.md", row(1))
        for quote, provenance in (("'", "inferred"), ('"', "none")):
            command = f"tools/relay submit d.md --root {quote}{candidate}{quote} | cat"
            record = self._indicated_miss(command, candidate)
            self.assertEqual(record["root_provenance"], provenance)
            indications, directory = self.rm.root_indications(command)
            self.assertEqual(indications, [(f"{quote}{candidate}{quote}", str(candidate), quote == '"')])
            self.assertFalse(directory)

    def test_quoted_static_operand_resolves_without_its_quotes(self):
        candidate = self.base / "root with space"
        (candidate / ".engine").mkdir(parents=True)
        write_index(candidate / "INDEX.md", row(1))
        record = self._indicated_miss(f'tools/relay submit d.md --root "{candidate}" | cat', candidate)
        self.assertEqual(record["root_provenance"], "inferred")
        self.assertEqual(record["command_roots"], [f'"{candidate}"'])

    def test_multiple_distinct_root_operands_withhold_inference(self):
        record = self._indicated_miss(f"tools/relay submit d.md --root {self.root} --root {self.root}/. | cat")
        self.assertEqual(record["root_provenance"], "none")
        self.assertEqual(record["command_roots"], [str(self.root), str(self.root) + "/."])

    def test_directory_word_withholds_inference(self):
        for word in ("cd", "pushd"):
            record = self._indicated_miss(f"{word} /B; time tools/relay submit d.md")
            self.assertEqual(record["root_provenance"], "none")

    def test_quoted_root_option_token_is_not_an_indication(self):
        command = "tools/relay submit d.md '--root' /B | cat"
        self.assertEqual(self.rm.root_indications(command), ([], False))
        self.assertEqual(self._indicated_miss(command)["root_provenance"], "inferred")

    def test_inference_uses_untruncated_operand(self):
        candidate = self.base / ("a" * 80) / ("b" * 80) / ("c" * 80)
        (candidate / ".engine").mkdir(parents=True)
        write_index(candidate / "INDEX.md", row(1))
        record = self._indicated_miss(f"tools/relay submit d.md --root {candidate} | cat", candidate)
        self.assertEqual(record["root_provenance"], "inferred")
        self.assertEqual(record["root"], str(candidate))
        self.assertEqual(record["command_roots"], [str(candidate)[:200]])

    def test_indications_never_bind(self):
        self._indicated_miss(f"time tools/relay submit d.md --root {self.root}")
        self.assertIsNone(self.store.load()["seat"])
        self.assertEqual(self.store.load()["binding_gen"], 0)

    def test_bound_to_a_filing_to_b_records(self):
        self.run_bind(self._submit())
        before = self.store.load()
        self.run_bind(self.payload("ssh elsewhere tools/relay submit d.md", self.receipt.replace("r1.md", "r99.md")))
        record = self._observation()
        self.assertEqual((record["root"], record["root_provenance"]), (None, "none"))
        self.assertEqual(self.store.load()["anchor"], before["anchor"])

    def test_same_path_in_two_roots_is_not_duplicate(self):
        self.run_bind(self._submit())
        other = self.other_root()
        payload = json.loads(self._miss())
        payload["cwd"] = str(other)
        self.run_bind(json.dumps(payload))
        record = self._observation()
        self.assertEqual((record["root"], record["root_provenance"]), (None, "none"))
        self.assertEqual(self.store.load()["root"], str(self.root))

    def test_unreadable_index_and_unrendered_row_still_record(self):
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                self.store.path.unlink(missing_ok=True)
                if unreadable:
                    self.index.unlink()
                else:
                    write_index(self.index, "")
                self.run_bind(self._miss())
                self.assertEqual(self._observation()["root_provenance"], "none")

    def test_manual_bind_to_current_anchor_clears_without_advancing(self):
        self.run_bind(self._submit("file"))
        self.run_bind(self._miss("miss"))
        self._observation()
        before = self.store.load()
        self.assertEqual(self._manual(), 0)
        after = self.store.load()
        self.assertIsNone(after.get("bind_observation"))
        self.assertEqual(after["binding_gen"], before["binding_gen"])
        self.assertEqual(after["progress"], before["progress"])

    def test_delayed_duplicate_hook_leaves_newer_observation(self):
        self._advance_stamps()
        payload = self._submit("file")
        self.run_bind(payload)
        self.run_bind(self._miss("miss"))
        before = self.store.path.read_bytes()
        self.run_bind(payload)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_repeat_r_then_s_then_delayed_repeat_keeps_s(self):
        self._advance_stamps()
        write_index(self.index, row(1) + row(2))
        (self.root / "lane/r2.md").write_text("r2\n")
        self.run_bind(self._submit("initial"))
        repeat = self._submit("repeat")
        self.run_bind(repeat)
        self.run_bind(self._submit("later", anchor="lane/r2.md"))
        self.run_bind(self._miss("observation"))
        before = self.store.path.read_bytes()
        self.run_bind(repeat)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load()["anchor"], "lane/r2.md")
        self.assertEqual(self.store.load()["binding_gen"], 2)

    def test_bind_s_clears_observation_recorded_before_it(self):
        write_index(self.index, row(1) + row(2))
        (self.root / "lane/r2.md").write_text("r2\n")
        self.run_bind(self._submit("initial"))
        self.run_bind(self._submit("repeat"))
        self.run_bind(self._miss("observation"))
        self.assertIsNotNone(self.store.load()["bind_observation"])
        self.run_bind(self._submit("later", anchor="lane/r2.md"))
        self.assertIsNone(self.store.load()["bind_observation"])
        self.assertEqual(self.store.load()["binding_gen"], 2)

    def test_repeat_sequence_across_root_change_keeps_new_binding(self):
        self._advance_stamps()
        self.run_bind(self._submit("initial"))
        repeat = self._submit("repeat")
        self.run_bind(repeat)
        other = self.other_root()
        self.run_bind(self._submit("later", root=other))
        self.run_bind(self._miss("observation"))
        before = self.store.path.read_bytes()
        self.run_bind(repeat)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load()["root"], str(other))
        self.assertEqual(self.store.load()["binding_gen"], 2)

    def test_same_anchor_identified_call_records_only_its_identity(self):
        self.run_bind(self._submit("initial"))
        self.run_bind(self._miss("observation"))
        before = self.store.load()
        with mock.patch.object(self.rm, "now_stamp", return_value="later-stamp"):
            self.run_bind(self._submit("repeat"))
        expected = dict(before)
        expected["handled_calls"] = before["handled_calls"] + [
            self.rm.call_identity("sess", {"tool_use_id": "repeat"})
        ]
        self.assertEqual(self.store.load(), expected)

    def test_same_anchor_without_identifier_is_byte_identical(self):
        self._advance_stamps()
        self.run_bind(self._submit("initial"))
        self.run_bind(self._miss("observation"))
        before = self.store.path.read_bytes()
        self.run_bind(self._submit())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_identified_help_and_refused_submit_not_remembered(self):
        self._advance_stamps()
        self.run_bind(self._submit("initial"))
        before = self.store.path.read_bytes()
        for command, response in (("tools/relay submit --help", "usage"),
                                  ("tools/relay submit bad.md", '{"code":"E-ENVELOPE"}')):
            self.run_bind(self.payload(command, response, call_id=command))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_eight_unrelated_identified_calls_do_not_evict(self):
        self._advance_stamps()
        original = self._submit("initial")
        self.run_bind(original)
        before = self.store.path.read_bytes()
        for i in range(8):
            self.run_bind(self.payload("echo unrelated", self.receipt, call_id=f"unrelated-{i}"))
            self.assertEqual(self.store.path.read_bytes(), before)
        self.run_bind(original)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_r_then_s_then_delayed_r_keeps_s(self):
        self._advance_stamps()
        write_index(self.index, row(1) + row(2))
        (self.root / "lane/r2.md").write_text("r2\n")
        first = self._submit("r")
        self.run_bind(first)
        self.run_bind(self._submit("s", anchor="lane/r2.md"))
        before = self.store.path.read_bytes()
        self.run_bind(first)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load()["binding_gen"], 2)
        self.assertEqual(self.store.load()["anchor"], "lane/r2.md")

    def test_separate_call_same_content_binds_again(self):
        self.run_bind(self._submit("first"))
        self._manual(root=self.other_root())
        self.run_bind(self._submit("second"))
        self.assertEqual(self.store.load()["root"], str(self.root))
        self.assertEqual(self.store.load()["binding_gen"], 3)

    def test_separate_call_same_display_records_again(self):
        self._advance_stamps()
        self.run_bind(self._submit("file"))
        first = self._miss("first")
        self.run_bind(first)
        self._manual()
        before = self.store.path.read_bytes()
        self.run_bind(first)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.run_bind(self._miss("second"))
        self.assertEqual(self._observation()["call"], self.rm.call_identity("sess", {"tool_use_id": "second"}))

    def test_same_anchor_absent_progress_establishes_progress(self):
        self.run_bind(self._submit())
        self.store.update(lambda n: n["progress"].clear())
        self.run_bind(self._submit())
        self.assertEqual(self.store.load()["progress"][str(self.root)], {"file": "lane/r1.md", "position": 1})
        self.assertEqual(self.store.load()["binding_gen"], 2)

    def test_miss_after_bind_survives(self):
        for source in ("hook", "manual"):
            with self.subTest(source=source):
                self.store.path.unlink(missing_ok=True)
                if source == "hook":
                    self.run_bind(self._submit())
                else:
                    self._manual()
                self.run_bind(self._miss())
                self.assertEqual(self._observation()["reason"], "submit-near-miss")
                self.assertEqual(self.store.load()["binding_source"], source)

    def test_three_concurrent_hooks_one_call_advance_once(self):
        payload = self._submit("concurrent")
        children = [subprocess.Popen([sys.executable, str(SCRIPT), "bind"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for child in children:
            self.addCleanup(lambda p=child: p.kill() if p.poll() is None else None)
            child.stdin.write(payload)
            child.stdin.close()
            child.stdin = None
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual((child.returncode, out, err), (0, "", ""))
        self.assertEqual(self.store.load()["binding_gen"], 1)
        self.assertEqual(len(self.store.load()["handled_calls"]), 1)

    def test_no_call_id_only_consecutive_duplicates_collapse(self):
        # Deliberately exercise the weaker guarantee of hosts without a call id.
        self._advance_stamps()
        self.run_bind(self._submit())
        before = self.store.path.read_bytes()
        self.run_bind(self._submit())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.run_bind(self._miss())
        before = self.store.path.read_bytes()
        self.run_bind(self._miss())
        self.assertEqual(self.store.path.read_bytes(), before)
        self._manual()
        self.run_bind(self._miss())
        self._observation()
        self.assertEqual(self.store.load()["handled_calls"], [])

    def test_ninth_call_evicts_oldest(self):
        for i in range(9):
            self.run_bind(self._miss(str(i)))
        self.assertEqual(self.store.load()["handled_calls"], [self.rm.call_identity("sess", {"tool_use_id": str(i)}) for i in range(1, 9)])
        self.run_bind(self._miss("0"))
        self.assertEqual(self._observation()["call"], self.rm.call_identity("sess", {"tool_use_id": "0"}))

    def test_receipt_with_findings_and_routed_notice_binds_author_relay(self):
        output = '{"findings":[{"path":"other.md"}]}\n' + self.receipt + '\nMONITOR-NOTICE: {"file":"lane/notice.md"}'
        self.run_bind(self.payload("tools/relay submit d.md", output))
        self.assertEqual(self.store.load()["anchor"], "lane/r1.md")
        self.assertIsNone(self.store.load().get("bind_observation"))

    def test_legacy_note_keys_read_and_cleared(self):
        self.run_bind(self._submit())
        self.store.update(lambda n: n.update(binding_degraded={"reason": "no-receipt", "stamp": "old"}, submit_miss="submit-near-miss", submit_miss_binding_gen=1))
        self.assertEqual(self.store.load()["submit_miss"], "submit-near-miss")
        self._manual()
        raw = json.loads(self.store.path.read_text())
        for key in ("binding_degraded", "submit_miss", "submit_miss_binding_gen"):
            self.assertNotIn(key, raw)



class DrainTests(TmpEnv):

    def test_recovered_empty_scan_restores_phase_with_observation(self):
        for path in ("shared-follower", "codex-drain"):
            with self.subTest(path=path):
                self.store.path.unlink(missing_ok=True)
                self.bind_and_rows(1)
                leader = self.rm.leader_record()
                lock = self.rm.LeaderLock("sess")
                if path == "shared-follower":
                    self.assertTrue(lock.acquire(blocking=False))
                    lock.stamp(leader["instance"])
                observation = {"reason": "submit-near-miss", "receipts": ["lane/seen.md"], "root_provenance": "none"}
                self.store.update(lambda n: (n.update(phase="unavailable", reason="index-missing", leader=leader if path == "shared-follower" else None, bind_observation=observation), n["progress"].update({str(self.root): {"file": "lane/r1.md", "position": 1}})))
                try:
                    if path == "shared-follower":
                        sink = self.rm.ByteSink()
                        result = self.rm.deliver_once(self.store, sink, self.rm.resolve_binding(self.store.load(), {}, None), self.rm.read_index(self.index), end=time.monotonic()+2, leader_instance=leader["instance"])
                        self.assertEqual(result.emitted, 0)
                        self.assertEqual(bytes(sink.buf), b"")
                    else:
                        rc, out = self.run_drain(self.codex_stop())
                        self.assertEqual(rc, 0)
                        self.assertEqual(json.loads(out), {})
                    self.assertEqual(self.store.load()["phase"], "following")
                    self.assertEqual(self.store.load()["bind_observation"], observation)
                finally:
                    lock.release()

    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("sess")

    def _matrix(self, case):
        # Followers share deliver_once; drain uses its real Stop entry point.
        # Separate notes and real leader/owner locks isolate all three paths.
        for path in ("codex-follower", "codex-drain", "claude-code-follower"):
            with self.subTest(path=path, case=case):
                self.store.path.unlink(missing_ok=True)
                write_index(self.index, row(1, to="b.implementer") + row(2, to="b.implementer"))
                leader = self.rm.leader_record()
                lock = self.rm.LeaderLock("sess")
                owner = None
                has_leader = case != "no-leader"
                if has_leader:
                    self.assertTrue(lock.acquire(blocking=False))
                    lock.stamp(leader["instance"])
                try:
                    def seed(n):
                        n.update(seat="b.implementer", root=str(self.root), anchor="lane/r1.md", binding_gen=1, binding_source="hook", phase="following", host="claude-code" if path.startswith("claude") else "codex", leader=leader if has_leader else None)
                        n["progress"][str(self.root)] = {"file": "lane/r1.md", "position": 1}
                        if case == "unbound":
                            n.update(seat=None, root=None, anchor=None, phase="waiting-for-binding")
                        if case in ("observation", "recovered"):
                            n["bind_observation"] = {"reason": "unsupported-syntax", "root": None, "root_provenance": "none", "receipts": ["lane/seen.md"], "call": None, "stamp": "s"}
                        if case in ("legacy", "recovered"):
                            n["binding_degraded"] = {"reason": "no-receipt", "stamp": "s"}
                        if case == "recovered":
                            n.update(phase="unavailable", reason="index-missing")
                        if case in ("lost", "fallback"):
                            n["progress"][str(self.root)] = {"file": "lane/gone.md", "position": 9}
                            if case == "lost":
                                n["anchor"] = "lane/missing.md"
                        if case == "sentinel":
                            n["progress"][str(self.root)] = dict(self.rm.SENTINEL)
                        if case == "foreign":
                            n["phase"] = "waiting-for-binding"
                    self.store.update(seed)
                    if case == "missing-index":
                        self.index.unlink()
                    elif case == "malformed-index":
                        self.index.write_text("not an index\n")
                    if case == "foreign":
                        owner = self.rm.stream_owner(self.store, time.monotonic() + 2)
                        self.assertIsNotNone(owner)
                        binding = self.rm.resolve_binding(self.store.load(), {}, None)
                        frame = {"identity": self.rm.frame_identity(binding, owner["instance"], "lane/r2.md", owner), "data": "00", "accepted": 0}
                        self.store.update(lambda n: n.update(frame=frame))
                    expected = case not in ("unbound", "missing-index", "malformed-index", "lost", "foreign") and (has_leader or path == "codex-drain")
                    wanted_file = "lane/r1.md" if case == "sentinel" else "lane/r2.md"
                    if path == "codex-drain":
                        rc, output = self.run_drain(self.codex_stop())
                        self.assertEqual(rc, 0)
                        document = json.loads(output)
                        if expected:
                            self.assertEqual(document["decision"], "block")
                            self.assertIn('"file":"' + wanted_file + '"', document["reason"])
                        else:
                            self.assertEqual(document, {})
                    else:
                        sink = self.rm.ByteSink()
                        if has_leader:
                            result = self.rm.deliver_once(self.store, sink, self.rm.resolve_binding(self.store.load(), {}, None), self.rm.read_index(self.index), end=time.monotonic() + 2, leader_instance=leader["instance"], limit=1 if case == "sentinel" else None)
                            self.assertEqual(result.emitted, int(expected))
                        lines = bytes(sink.buf).decode().splitlines()
                        self.assertEqual([json.loads(line)["file"] for line in lines], [wanted_file] if expected else [])
                    state = self.rm.readiness(self.store)[0]
                    expected_state = "not-started" if not has_leader else "waiting-for-binding" if case in ("unbound", "foreign") else "unavailable" if case in ("missing-index", "malformed-index", "lost") else "armed"
                    self.assertEqual(state, expected_state)
                    identity = "version-mismatch" if case == "mismatch" else "ok"
                    with mock.patch.object(self.rm, "check_identity", return_value=(identity, {}, {})):
                        line = self.rm.status_line(self.store, None).splitlines()[0]
                    self.assertTrue(line.startswith("relay-monitor: " + expected_state))
                    if case == "lost":
                        self.assertTrue(line.startswith("relay-monitor: unavailable: anchor-not-found "))
                    if case == "mismatch":
                        self.assertTrue(line.endswith("identity=mismatch"))
                    if expected:
                        self.assertEqual(self.store.load()["progress"][str(self.root)]["file"], wanted_file)
                    if case == "foreign":
                        self.assertEqual(self.store.load()["frame"], frame)
                finally:
                    if owner:
                        owner["_lock"].release(unlink=True)
                    lock.release()

    def test_matrix_healthy_delivers(self):
        self._matrix("healthy")

    def test_matrix_observation_delivers(self):
        self._matrix("observation")
        self._matrix("legacy")

    def test_matrix_unbound_delivers_nothing(self):
        self._matrix("unbound")

    def test_matrix_index_missing_or_malformed_delivers_nothing(self):
        self._matrix("missing-index")
        self._matrix("malformed-index")

    def test_matrix_missing_cursor_and_anchor_delivers_nothing(self):
        self._matrix("lost")

    def test_matrix_fallback_anchor_rebases_and_delivers(self):
        self._matrix("fallback")

    def test_matrix_start_sentinel_delivers_first_row(self):
        self._matrix("sentinel")

    def test_matrix_recovered_fault_restores_phase(self):
        self._matrix("recovered")

    def test_matrix_no_leader_follower_none_drain_delivers(self):
        self._matrix("no-leader")

    def test_matrix_version_mismatch_delivers(self):
        self._matrix("mismatch")

    def test_matrix_foreign_frame_waits(self):
        self._matrix("foreign")

    def test_armed_across_arrivals_without_commands(self):
        for host in ("codex", "claude-code"):
            with self.subTest(host=host):
                self.store.path.unlink(missing_ok=True)
                self.bind_and_rows(0)
                leader = self.rm.leader_record()
                lock = self.rm.LeaderLock("sess")
                self.assertTrue(lock.acquire(blocking=False))
                lock.stamp(leader["instance"])
                try:
                    self.store.update(lambda n: n.update(leader=leader, phase="following", host=host))
                    for number in range(1, 4):
                        write_index(self.index, "".join(row(i, to="b.implementer") for i in range(1, number + 1)))
                        sink = self.rm.ByteSink()
                        result = self.rm.deliver_once(self.store, sink, self.rm.resolve_binding(self.store.load(), {}, None), self.rm.read_index(self.index), end=time.monotonic() + 2, leader_instance=leader["instance"])
                        self.assertEqual(result.emitted, 1)
                        self.assertEqual(json.loads(bytes(sink.buf))["file"], f"lane/r{number}.md")
                        self.assertEqual(self.rm.readiness(self.store)[0], "armed")
                finally:
                    lock.release()

    def codex_stop(self, active=False):
        return {
            "session_id": "sess",
            "cwd": str(self.root),
            "hook_event_name": "Stop",
            "turn_id": "t",
            "stop_hook_active": active,
            "last_assistant_message": None,
            "model": "m",
            "permission_mode": "default",
            "transcript_path": None,
        }

    def bind_and_rows(self, n):
        write_index(
            self.index, "".join(row(i, to="b.implementer") for i in range(1, n + 1))
        )
        self.store.update(
            lambda x: (
                x.update(
                    {
                        "seat": "b.implementer",
                        "root": str(self.root),
                        "binding_gen": 1,
                        "binding_source": "hook",
                        "host": "codex",
                    }
                ),
                x["progress"].__setitem__(str(self.root), dict(self.rm.SENTINEL)),
            )
        )

    def run_drain(self, payload, deadline=5.0, env=None):
        r, w = os.pipe()
        writer = os.fdopen(w, "w")
        out = b""
        try:
            with (
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                mock.patch.dict(os.environ, env or {}),
                mock.patch.object(sys, "stdout", writer),
            ):
                rc = self.rm.drain(ns(deadline=deadline))
            writer.close()
            while True:
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                out += chunk
        finally:
            with contextlib.suppress(OSError):
                writer.close()
            with contextlib.suppress(OSError):
                os.close(r)
        return (
            rc,
            out.decode(),
            # the writer is closed before the read so EOF arrives; both ends closed
            # exactly
            # once (P4-R2)
        )

    def test_claude_and_unknown_hosts_return_empty(self):
        self.assertEqual(
            json.loads(
                self.run_drain(
                    {"session_id": "sess", "hook_event_name": "Stop", "prompt_id": "p"}
                )[1]
            ),
            {},
        )
        self.assertEqual(
            json.loads(
                self.run_drain({"session_id": "sess", "hook_event_name": "Stop"})[1]
            ),
            {},
        )

    def test_unbound_degraded_and_no_arrival_return_empty(self):
        self.assertEqual(json.loads(self.run_drain(self.codex_stop())[1]), {})
        self.bind_and_rows(0)
        self.assertEqual(json.loads(self.run_drain(self.codex_stop())[1]), {})
        self.bind_and_rows(1)
        self.store.update(
            lambda n: n.update(
                {"binding_degraded": {"reason": "no-receipt", "stamp": "s"}}
            )
        )
        doc = json.loads(self.run_drain(self.codex_stop())[1])
        self.assertEqual(doc["decision"], "block")
        self.assertIn('"file":"lane/r1.md"', doc["reason"])

    def test_one_row_per_stop_then_next_then_empty(self):
        self.bind_and_rows(2)
        doc = json.loads(self.run_drain(self.codex_stop())[1])
        self.assertEqual(doc["decision"], "block")
        self.assertIn('"file":"lane/r1.md"', doc["reason"])
        self.assertEqual(doc["reason"].count("\n"), 1)
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r1.md"
        )
        self.assertIn(
            '"file":"lane/r2.md"',
            json.loads(self.run_drain(self.codex_stop(True))[1])["reason"],
        )
        self.assertEqual(json.loads(self.run_drain(self.codex_stop(True))[1]), {})

    def test_deadline_before_output_leaves_row_eligible(self):
        self.bind_and_rows(1)
        self.assertEqual(
            json.loads(self.run_drain(self.codex_stop(), deadline=0.0)[1]), {}
        )
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )

    def test_total_deadline_holds_with_lock_held_and_with_index_missing(self):
        self.bind_and_rows(1)

        def run(deadline):
            t0 = time.monotonic()
            p = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "drain",
                    "--host",
                    "codex",
                    "--deadline",
                    str(deadline),
                ],
                input=json.dumps(self.codex_stop()),
                capture_output=True,
                text=True,
                env=dict(os.environ),
                timeout=deadline + 5,
            )
            return time.monotonic() - t0, p

        with self.store.state_lock():  # the lock is held for the whole budget
            elapsed, p = run(1.5)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(json.loads(p.stdout), {})
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )
        self.index.unlink()
        with self.store.state_lock():
            elapsed, p = run(1.5)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(json.loads(p.stdout), {})

    def test_frame_created_between_load_and_lock_is_frame_busy(self):
        self.bind_and_rows(1)
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        snap = self.rm.read_index(self.index)
        pre_read = dict(
            self.store.load()["progress"][str(self.root)]
        )  # drain's unlocked read happens here, before any frame exists
        stalled = StallingSink(accept_first=5)
        r = self.rm.deliver_once(
            self.store,
            stalled,
            binding,
            snap,
            end=time.monotonic() + 0.2,
            leader_instance="L",
        )
        self.assertEqual(r.aborted, "output-stalled")
        frame = self.store.load()["frame"]
        self.assertEqual(
            frame["identity"]["leader"], "L"
        )  # the follower's partial frame now exists and its lock is released
        out = self.rm.ByteSink()
        r = self.rm.deliver_once(
            self.store,
            out,
            binding,
            snap,
            end=time.monotonic() + 0.5,
            leader_instance="drain-probe",
            limit=1,
            encode=self.rm.stop_document,
            start_cursor=pre_read,
        )
        self.assertEqual((r.emitted, r.aborted), (0, "frame-busy"))
        self.assertEqual(bytes(out.buf), b"")
        self.assertEqual(
            self.store.load()["frame"], frame
        )  # drain neither failed nor touched the follower's frame

    def wait_note(self, pred, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            note = self.store.load()
            if pred(note):
                return note
            time.sleep(0.02)
        self.fail(
            "note condition not observed: "
            + json.dumps(self.store.load(), default=str)[:300]
        )

    def test_two_stops_two_sinks_never_resume_each_others_prefix(self):
        self.bind_and_rows(1)
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        snap = self.rm.read_index(self.index)
        far = time.monotonic() + 5.0
        a_owner, b_owner = (
            self.rm.stream_owner(self.store, far),
            self.rm.stream_owner(self.store, far),
        )
        self.assertNotEqual(a_owner["instance"], b_owner["instance"])
        self.addCleanup(
            lambda: [o["_lock"].release(unlink=True) for o in (a_owner, b_owner)]
        )
        a_sink = StallingSink(accept_first=5)
        r = self.rm.deliver_once(
            self.store,
            a_sink,
            binding,
            snap,
            end=time.monotonic() + 0.2,
            leader_instance=a_owner["instance"],
            owner=a_owner,
            limit=1,
            encode=self.rm.stop_document,
        )
        self.assertEqual(
            r.aborted, "output-stalled"
        )  # A's transaction released with accepted=5 saved; A has not cleaned up yet
        b_sink = self.rm.ByteSink()
        r = self.rm.deliver_once(
            self.store,
            b_sink,
            binding,
            snap,
            end=time.monotonic() + 0.5,
            leader_instance=b_owner["instance"],
            owner=b_owner,
            limit=1,
            encode=self.rm.stop_document,
        )
        self.assertEqual((r.emitted, r.aborted), (0, "frame-busy"))
        self.assertEqual(
            bytes(b_sink.buf), b""
        )  # B never writes data[5:] to its fresh pipe
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )

        def clear_a(n):
            if (
                n.get("frame")
                and n["frame"]["identity"]["leader"] == a_owner["instance"]
            ):
                n["frame"] = None

        def clear_b(n):
            if (
                n.get("frame")
                and n["frame"]["identity"]["leader"] == b_owner["instance"]
            ):
                n["frame"] = None

        self.store.update(clear_b)
        self.assertIsNotNone(
            self.store.load()["frame"]
        )  # B's cleanup cannot clear A's frame
        self.store.update(clear_a)
        self.assertIsNone(self.store.load()["frame"])  # A's exact-identity cleanup does
        c_owner, c_sink = self.rm.stream_owner(self.store, far), self.rm.ByteSink()
        self.addCleanup(lambda: c_owner["_lock"].release(unlink=True))
        r = self.rm.deliver_once(
            self.store,
            c_sink,
            binding,
            snap,
            end=time.monotonic() + 0.5,
            leader_instance=c_owner["instance"],
            owner=c_owner,
            limit=1,
            encode=self.rm.stop_document,
        )
        self.assertEqual(r.emitted, 1)
        doc = json.loads(bytes(c_sink.buf))
        self.assertEqual(
            doc["decision"], "block"
        )  # a fresh Stop starts at byte zero and emits the whole document

    def test_stale_stop_frame_is_reclaimed_and_live_or_unknown_owner_is_not(self):
        self.bind_and_rows(1)

        def plant(lock):
            return self.store.update(
                lambda n: n.__setitem__(
                    "frame",
                    {
                        "identity": {
                            "root": str(self.root),
                            "seat": "b.implementer",
                            "binding_gen": 1,
                            "leader": "drain-other",
                            "file": "lane/r1.md",
                            "owner": {"lock": str(lock)},
                        },
                        "data": "00",
                        "accepted": 1,
                    },
                )
            )

        unheld = self.store.owner_lock_path("drain-unheld")
        unheld.touch()
        plant(unheld)
        with mock.patch.object(
            self.rm.subprocess, "run", side_effect=AssertionError("external probe")
        ):
            t0 = time.monotonic()
            rc, out = self.run_drain(self.codex_stop())
            took = time.monotonic() - t0
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertIsNone(self.store.load()["frame"])
        self.assertLess(
            took, 0.5
        )  # dead owner reclaimed, no external probe, short budget respected
        self.bind_and_rows(1)
        live = self.store.owner_lock_path("drain-live")
        hold_owner_lock(self, live)
        plant(live)
        rc, out = self.run_drain(self.codex_stop(), deadline=0.5)
        self.assertEqual(json.loads(out), {})
        self.assertIsNotNone(self.store.load()["frame"])  # live owner: {} and intact
        plant(self.store.owner_lock_path("drain-missing"))
        rc, out = self.run_drain(self.codex_stop(), deadline=0.5)
        self.assertEqual(json.loads(out), {})
        self.assertIsNotNone(self.store.load()["frame"])  # unknown owner: {} and intact
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_stream_owner_is_cheap_and_probe_free(self):
        with mock.patch.object(
            self.rm.subprocess, "run", side_effect=AssertionError("external probe")
        ):
            t0 = time.monotonic()
            me = self.rm.stream_owner(self.store, t0 + 1.0)
            took = time.monotonic() - t0
        self.assertIsNotNone(me)
        self.assertLess(took, 0.05)
        self.assertTrue(pathlib.Path(me["lock"]).exists())
        with self.rm.NoteStore("sess").state_lock():
            t0 = time.monotonic()
            self.assertIsNone(self.rm.stream_owner(self.store, t0 + 0.2))
            self.assertLess(
                time.monotonic() - t0, 0.6
            )  # contended: bounded by the budget, None
        self.assertEqual(
            self.rm.owner_state({"identity": {"owner": {"lock": me["lock"]}}}), "alive"
        )  # held by this process: would block
        me["_lock"].release(unlink=True)
        self.assertFalse(pathlib.Path(me["lock"]).exists())

    def test_combined_stop_budget_lock_wait_prefix_stall_and_contended_cleanup(self):
        self.bind_and_rows(1)
        flag = self.base / "release-cleanup"
        env = dict(
            os.environ,
            ADT_TEST_SINK_ACCEPT="5",
            ADT_TEST_PAUSE_BEFORE_CLEANUP=str(flag),
        )
        with (
            self.store.state_lock()
        ):  # the drain spends most of its 1.5 s budget waiting for this lock
            p = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "drain",
                    "--host",
                    "codex",
                    "--deadline",
                    "1.5",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            t_start = time.monotonic()  # timing anchor: observed subprocess start
            self.addCleanup(
                lambda: (
                    p.kill(),
                    p.wait(),
                    p.stdin.close(),
                    p.stdout.close(),
                    p.stderr.close(),
                )
            )
            p.stdin.write(json.dumps(self.codex_stop()).encode())
            p.stdin.close()
            while time.monotonic() < t_start + 1.2:
                time.sleep(0.01)  # hold until 1.2 s after start
        note = self.wait_note(
            lambda n: n.get("frame")
            and n["frame"]["identity"]["leader"].startswith("drain-")
        )  # prefix written, stall recorded, transaction lock released
        with self.store.state_lock():  # cleanup is contended for the rest of the budget
            flag.touch()
            p.wait(timeout=5)
            t_exit = time.monotonic()
        out = p.stdout.read()
        # Bound: exit within budget + 0.5 s startup tolerance = 2.0 s after start. With
        # the remaining-budget cleanup the drain exits by
        # t_start + startup + 1.5 <= 1.8 s (startup < 0.3 s measured at T0). A fresh 1.0
        # s cleanup timeout, which starts no earlier than
        # 1.2 s, cannot exit before 2.2 s and fails this bound; the mutation control in
        # Step 6 shows that failure.
        self.assertLess(t_exit - t_start, 2.0)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(out), 5)  # only the prefix reached the pipe
        after = self.store.load()
        self.assertEqual(after["progress"][str(self.root)], self.rm.SENTINEL)
        self.assertEqual(
            after["frame"], note["frame"]
        )  # eligibility unchanged, frame left for reclaim
        self.assertTrue(
            pathlib.Path(after["frame"]["identity"]["owner"]["lock"]).exists()
        )
        with self.store.state_lock():
            self.assertEqual(self.rm.owner_state(after["frame"]), "dead")
            self.assertEqual(
                self.rm.owner_state(after["frame"]), "dead"
            )  # observational: repeatable, file left in place
        self.assertTrue(
            pathlib.Path(after["frame"]["identity"]["owner"]["lock"]).exists()
        )  # the recovery evidence survives the assertion (P5-R1)
        q = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "drain",
                "--host",
                "codex",
                "--deadline",
                "5",
            ],
            input=json.dumps(self.codex_stop()),
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=10,
        )
        self.assertEqual(json.loads(q.stdout)["decision"], "block")
        self.assertIsNone(self.store.load()["frame"])
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r1.md"
        )  # recovery from byte zero

    def test_crash_before_output_and_between_output_and_persist(self):
        self.bind_and_rows(1)
        env = dict(os.environ)
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "drain", "--host", "codex"],
            input=json.dumps(self.codex_stop()),
            capture_output=True,
            text=True,
            env={**env, "ADT_TEST_CRASH_BEFORE_OUTPUT": "1"},
        )
        self.assertEqual((p.returncode, p.stdout), (9, ""))
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "drain", "--host", "codex"],
            input=json.dumps(self.codex_stop()),
            capture_output=True,
            text=True,
            env={**env, "ADT_TEST_CRASH_AFTER_OUTPUT": "1"},
        )
        self.assertEqual(p.returncode, 9)
        self.assertEqual(json.loads(p.stdout)["decision"], "block")
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )  # output happened, persist did not: one re-emission allowed
        self.assertIn(
            '"file":"lane/r1.md"',
            json.loads(self.run_drain(self.codex_stop())[1])["reason"],
        )  # the ruled single replay

    def test_write_failure_appends_nothing(self):
        self.bind_and_rows(1)
        r, w = os.pipe()
        os.close(r)
        writer = os.fdopen(w, "w")

        def close_writer():
            with contextlib.suppress(OSError):
                writer.close()

        self.addCleanup(close_writer)
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(self.codex_stop()))),
            mock.patch.object(sys, "stdout", writer),
        ):
            self.assertEqual(self.rm.drain(ns()), 1)
        self.assertEqual(
            self.store.load()["progress"][str(self.root)], self.rm.SENTINEL
        )

    def test_concurrent_follow_and_bind_around_stop(self):
        self.bind_and_rows(1)
        (self.root / "lane" / "r1.md").write_text("x\n")
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        snap = self.rm.read_index(self.index)
        held = GateSink()
        res = {}
        t = threading.Thread(
            target=lambda: res.update(
                f=self.rm.deliver_once(
                    self.store,
                    held,
                    binding,
                    snap,
                    end=time.monotonic() + 5.0,
                    leader_instance="L",
                )
            )
        )
        self.addCleanup(lambda: (held.release.set(), t.join(5)))
        t.start()
        self.assertTrue(
            held.entered.wait(5.0)
        )  # follower paused inside its locked write
        bind_store = self.rm.NoteStore("sess")
        attempt = threading.Event()
        bind_store.observer = lambda ev: attempt.set()
        tb = threading.Thread(
            target=lambda: self.rm.bind_from_receipt(
                bind_store,
                root=str(self.root),
                receipt_path="lane/r1.md",
                key_seat=None,
                source="hook",
            )
        )  # a real bind arrives concurrently
        tb.start()
        self.assertTrue(attempt.wait(5.0))
        self.addCleanup(lambda: tb.join(5))
        t2 = threading.Thread(
            target=lambda: res.update(d=self.run_drain(self.codex_stop()))
        )
        t2.start()
        self.addCleanup(lambda: t2.join(5))
        time.sleep(0.1)
        held.release.set()
        t.join(5)
        tb.join(5)
        t2.join(5)
        self.assertEqual(res["f"].emitted, 1)
        self.assertEqual(
            json.loads(res["d"][1]),
            {},
            # follower won r1 while holding the lock; the bind then refreshed the same
            # seat/root; drain found nothing
        )
        n = self.store.load()
        self.assertEqual(n["binding_gen"], 2)
        self.assertEqual(n["progress"][str(self.root)]["file"], "lane/r1.md")


class SessionStartTests(TmpEnv):
    FRESH_CODEX = {
        "cwd": "/w",
        "hook_event_name": "SessionStart",
        "model": "m",
        "permission_mode": "default",
        "session_id": "sess",
        "source": "startup",
        "transcript_path": None,
    }

    def run_ss(self, payload, env):
        out = io.StringIO()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", out),
            mock.patch.dict(os.environ, env),
            mock.patch.object(
                self.rm,
                "check_identity",
                return_value=(
                    "ok",
                    {"kit": "2.9.3", "fp": "deadbeef"},
                    {"kit": "2.9.3", "fp": "deadbeef"},
                ),
            ),
            mock.patch.object(
                self.rm.subprocess,
                "run",
                side_effect=AssertionError("session-start must not spawn a process"),
            ),
            # A6.1: the identity check is stubbed (it is unchanged and tested in
            # StatusTests); anything else spawning is the removed probe
        ):
            self.rm.session_start(ns(plugin_root="/plug"))
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_session_start_context_uses_new_grammar(self):
        self.rm.NoteStore("sess").update(lambda n: n.update(binding_source="hook", binding_degraded={"reason": "unsupported-shell"}))
        ctx = self.run_ss(self.FRESH_CODEX, {"ADT_HOST": "codex"})
        self.assertIn("binding=hook", ctx.splitlines()[0])
        self.assertIn("last-bind=unsupported-shell", ctx.splitlines()[0])
        self.assertTrue(ctx.splitlines()[1].startswith("# rebind: incomplete"))

    def test_fresh_codex_arms_under_plugin_route_env_and_under_launcher(self):
        ctx = self.run_ss(
            self.FRESH_CODEX,
            {
                "PLUGIN_ROOT": "/plug",
                "CLAUDE_PLUGIN_ROOT": "/plug",
                "PLUGIN_DATA": "/d",
                "CLAUDE_PLUGIN_DATA": "/d",
            },
        )
        self.assertIn("action `start`", ctx)
        self.assertIn("follow --host codex --session sess", ctx)
        [
            os.environ.pop(k, None)
            for k in (
                "PLUGIN_ROOT",
                "PLUGIN_DATA",
                "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_PLUGIN_DATA",
            )
        ]
        ctx = self.run_ss(self.FRESH_CODEX, {"ADT_HOST": "codex"})
        self.assertIn("action `start`", ctx)

    def test_evidence_free_claude_session_start_gets_status_only_and_writes_no_host(
        self,
    ):
        ctx = self.run_ss(
            {
                "session_id": "sess",
                "hook_event_name": "SessionStart",
                "source": "startup",
                "cwd": "/w",
                "transcript_path": "/t",
            },
            {},
        )
        self.assertIn("relay-monitor:", ctx)
        self.assertNotIn("action `start`", ctx)
        self.assertIsNone(self.rm.NoteStore("sess").load()["host"])

    def test_override_falls_back_without_arming(self):  # A6.2
        ctx = self.run_ss(
            self.FRESH_CODEX, {"ADT_HOST": "codex", "ADT_CODEX_FORK": "0"}
        )
        self.assertIn("fallback: pointer (ADT_CODEX_FORK=0)", ctx)
        self.assertNotIn("action `start`", ctx)
        self.assertTrue(self.rm.NoteStore("sess").load()["codex_fork_override"])
        ctx = self.run_ss(
            self.FRESH_CODEX, {"ADT_HOST": "codex", "ADT_CODEX_FORK": "1"}
        )
        self.assertIn("action `start`", ctx)  # any other value arms

    def test_arming_context_order_and_absent_tool_report(self):  # A6.3
        ctx = self.run_ss(self.FRESH_CODEX, {"ADT_HOST": "codex"})
        marks = [
            ctx.index("advertised tool surface"),
            ctx.index("action `start`"),
            ctx.index("action `list`"),
            ctx.index('relay-monitor.py" status --session sess'),
            ctx.index("unavailable: monitor-tool"),
        ]
        self.assertEqual(marks, sorted(marks))
        self.assertIn("nested code-mode", ctx)
        self.assertIn("make no call", ctx)

    def test_shipped_script_never_invokes_codex(self):  # A6.4 (source-level)
        text = SCRIPT.read_text()
        self.assertNotIn('"codex", "features"', text)
        self.assertNotIn("features list", text)
        self.assertNotIn("fork_marker", text)

    def test_resume_instructs_list_first(self):
        ctx = self.run_ss(
            {**self.FRESH_CODEX, "source": "resume"}, {"ADT_HOST": "codex"}
        )
        self.assertLess(ctx.index("action `list`"), ctx.index("action `start`"))

    def test_ambiguous_host_writes_no_host(self):
        self.run_ss({**self.FRESH_CODEX, "prompt_id": "p"}, {"PLUGIN_ROOT": "/plug"})
        self.assertIsNone(self.rm.NoteStore("sess").load()["host"])


class StatusTests(TmpEnv):

    def test_incomplete_hint_is_inert_in_shell(self):
        marker = self.base / "must-not-exist"
        for value in (f"lane/`touch {marker}`.md", f"lane/$(touch {marker}).md", "lane/a\nb\x1b.md"):
            with self.subTest(value=value):
                self._record(provenance="none", receipts=[value])
                hint = self.rm.rebind_line(self.store.load(), "sess")
                parsed = subprocess.run(["sh", "-n"], input=hint, text=True, capture_output=True)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)
                ran = subprocess.run(["sh", "-c", hint], cwd=self.base, text=True, capture_output=True)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual(ran.stdout, "")
                self.assertTrue(hint.startswith("#"))
                self.assertEqual(len(hint.splitlines()), 1)

    def test_status_escapes_non_ascii_and_control_arguments(self):
        for char in ("\v", "\f", "\x85", "\u2028", "\x1b", "é"):
            for provenance, field in ((p, f) for p in ("command", "inferred") for f in ("root", "receipt")):
                with self.subTest(char=repr(char), provenance=provenance):
                    root = f"/a{char}b" if field == "root" else "/a/b"
                    receipt = f"lane/a{char}b.md" if field == "receipt" else "lane/r1.md"
                    self._record(provenance=provenance, root=root, receipts=[receipt])
                    self.store.update(lambda n: (n.update(root=root), n["progress"].update({root: {"file": receipt, "position": 1}})))
                    status = self._status()
                    self.assertEqual(len(status.splitlines()), 2)
                    self.assertTrue(all(c == "\n" or 32 <= ord(c) <= 126 for c in status))
                    self.assertNotIn("bind --manual", status)
                    self.assertIn(ascii(char)[1:-1], status)

    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("sess")

    def test_record_without_command_roots_renders_hint(self):
        self._record(provenance="none")
        self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("# rebind: incomplete"))

    def test_incomplete_hint_shows_operands_as_command_text_on_one_line(self):
        self._record(provenance="none")
        self.store.update(lambda n: n["bind_observation"].update(command_roots=["/B with space", "line\nnext", "x" * 200] * 10))
        line = self.rm.rebind_line(self.store.load(), "sess")
        self.assertIn("command text", line)
        self.assertIn("'/B with space'", line)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertLessEqual(len(line), 1024)
        self.assertNotIn("bind --manual", line)

    def _live_status(self, phase="following", bound=True):
        lock = self.rm.LeaderLock("sess")
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        me = self.rm.leader_record()
        lock.stamp(me["instance"])
        def seed(note):
            note.update(leader=me, phase=phase)
            if bound:
                note.update(seat="a.planner", root=str(self.root), binding_source="hook")
                note["progress"][str(self.root)] = {"file": "lane/r1.md", "position": 1}
        self.store.update(seed)

    def _record(self, reason="unsupported-syntax", provenance="command", root=None, receipts=None):
        self.store.update(lambda n: n.update(bind_observation={"reason": reason, "root": str(root or self.root) if provenance != "none" else None, "root_provenance": provenance, "receipts": receipts or ["lane/r1.md"], "stamp": "s", "call": None}))

    def _status(self, identity="ok"):
        with mock.patch.object(self.rm, "check_identity", return_value=(identity, {"kit": "new", "fp": "aa"}, {"kit": "old" if identity == "version-mismatch" else "new", "fp": "bb"})):
            return self.rm.status_line(self.store, None)

    def test_observation_does_not_produce_unavailable(self):
        self._live_status()
        self._record()
        self.store.update(lambda n: n.update(binding_degraded={"reason": "no-receipt"}))
        self.assertEqual(self.rm.readiness(self.store)[0], "armed")

    def test_phase_unavailable_still_reads_unavailable(self):
        self._live_status(phase="unavailable")
        self.assertEqual(self.rm.readiness(self.store)[0], "unavailable")

    def test_unbound_live_follower_reads_waiting_for_binding_after_any_command(self):
        self._live_status(phase="waiting-for-binding", bound=False)
        for command, response in (("ls", ""), ("tools/relay submit d.md", ""), ("time tools/relay submit d.md", '{"path":"lane/r1.md","render_state":"rendered","duplicate":false}')):
            payload = dict(session_id="sess", cwd=str(self.root), hook_event_name="PostToolUse", tool_input={"command": command}, tool_response=response)
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                self.assertEqual(self.rm.bind(ns()), 0)
            self.assertEqual(self.rm.readiness(self.store)[0], "waiting-for-binding")

    def test_binding_cell_reports_source_only(self):
        self.store.update(lambda n: n.update(binding_degraded={"reason": "no-receipt"}))
        for source in (None, "hook", "manual", "env"):
            self.store.update(lambda n: n.update(binding_source=source))
            self.assertEqual(next(x for x in self._status().splitlines()[0].split() if x.startswith("binding=")), "binding=" + (source or "none"))

    def test_one_record_prints_one_cell(self):
        for reason, cell, absent in (("submit-near-miss", "miss=submit-near-miss", "last-bind="), ("unsupported-syntax", "last-bind=unsupported-syntax", "miss=")):
            self._record(reason=reason)
            first = self._status().splitlines()[0]
            self.assertEqual(first.split().count(cell), 1)
            self.assertNotIn(absent, first)

    def test_legacy_degraded_beside_new_record_prints_new_only(self):
        self._record()
        self.store.update(lambda n: n.update(binding_degraded={"reason": "unsupported-shell"}))
        self.assertEqual([v for v in self._status().splitlines()[0].split() if v.startswith("last-bind=")], ["last-bind=unsupported-syntax"])

    def test_legacy_miss_and_new_last_bind_can_coexist(self):
        self._record()
        self.store.update(lambda n: n.update(submit_miss="submit-near-miss"))
        first = self._status().splitlines()[0]
        self.assertIn(" miss=submit-near-miss last-bind=unsupported-syntax", first)

    def test_rebind_complete_form_round_trips_shell_quoting(self):
        import shlex
        root = "/a space/it's-root"
        self._record(root=root, receipts=["lane/it's here.md"])
        self.assertTrue(self.rm.rebind_line(self.store.load(), "session quoted").startswith("# rebind: incomplete"))
        fixture = self.base / "quoting-index.md"
        write_index(fixture, row(1, file="lane/it's here.md"))
        snapshot = self.rm.read_index(fixture)
        with mock.patch.object(self.rm, "read_index", return_value=snapshot) as reader:
            line = self.rm.rebind_line(self.store.load(), "session quoted")
        reader.assert_called_once_with(pathlib.Path(root) / "INDEX.md")
        self.assertTrue(line.startswith("rebind, may need operator approval: "))
        words = shlex.split(line[len("rebind, may need operator approval: "):])
        self.assertEqual(words[words.index("--root") + 1], root)
        self.assertEqual(words[words.index("--anchor") + 1], "lane/it's here.md")
        self.assertEqual(words[words.index("--session") + 1], "session quoted")
        self.assertIn("--manual", words)

    def test_rebind_inferred_form_is_labelled(self):
        self._record(provenance="inferred")
        self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("# rebind: incomplete"))
        write_index(self.index, row(1))
        self.assertTrue(self.rm.rebind_line(self.store.load(), "sess").startswith("rebind (root inferred, check it, may need operator approval): "))

    def test_rebind_incomplete_form_is_not_runnable_and_omits_anchor(self):
        self.store.update(lambda n: n.update(anchor="lane/old-anchor.md"))
        self._record(provenance="none", receipts=["lane/seen.md"])
        line = self.rm.rebind_line(self.store.load(), "sess")
        self.assertTrue(line.startswith("# rebind: incomplete, supply "))
        self.assertNotIn("lane/old-anchor.md", line)
        self.assertNotIn("--manual", line)
        self.assertIn("lane/seen.md", line)
        self._record(provenance="none", receipts=["x" * 5000])
        self.assertLessEqual(len(self.rm.rebind_line(self.store.load(), "sess")), 1024)

    def test_mismatch_on_armed_seat_reads_armed_with_cell_and_remedy(self):
        self._live_status()
        lines = self._status("version-mismatch").splitlines()
        self.assertTrue(lines[0].startswith("relay-monitor: armed "))
        self.assertTrue(lines[0].endswith("identity=mismatch"))
        self.assertTrue(lines[1].startswith("remedy: "))

    def test_mismatch_with_no_follower_reads_not_started_with_cell(self):
        self.store.update(lambda n: n.update(root=str(self.root)))
        line = self._status("version-mismatch").splitlines()[0]
        self.assertTrue(line.startswith("relay-monitor: not-started "))
        self.assertTrue(line.endswith("identity=mismatch"))

    def test_mismatch_cell_on_every_reading(self):
        self.store.update(lambda n: n.update(root=str(self.root)))
        for _ in range(2):
            text = self._status("version-mismatch")
            self.assertTrue(text.splitlines()[0].endswith("identity=mismatch"))
            self.assertTrue(text.splitlines()[1].startswith("remedy: "))

    def test_tiers_mixed_on_differing_identity(self):
        note = {"leader": {"identity": {"kit": "1", "fp": "a"}}, "standbys": [{"identity": {"kit": "1", "fp": "b"}}]}
        with mock.patch.object(self.rm, "process_liveness", return_value=self.rm.PROC_UNKNOWN):
            self.assertTrue(self.rm.tiers_mixed(note))
            self.store.update(lambda n: n.update(note))
            self.assertTrue(self._status().splitlines()[0].endswith("tiers=mixed"))
            note["standbys"][0]["identity"]["fp"] = "a"
            self.assertFalse(self.rm.tiers_mixed(note))

    def test_tiers_mixed_ignores_dead_record(self):
        note = {"leader": {"identity": {"kit": "1", "fp": "a"}}, "standbys": [{"identity": {"kit": "2", "fp": "b"}}]}
        with mock.patch.object(self.rm, "process_liveness", side_effect=[self.rm.PROC_ALIVE, self.rm.PROC_DEAD]):
            self.assertFalse(self.rm.tiers_mixed(note))

    def test_record_without_identity_counts_as_different(self):
        note = {"leader": {"identity": {"kit": "1", "fp": "a"}}, "standbys": [{}]}
        with mock.patch.object(self.rm, "process_liveness", return_value=self.rm.PROC_ALIVE):
            self.assertTrue(self.rm.tiers_mixed(note))
            note["leader"] = {}
            self.assertFalse(self.rm.tiers_mixed(note))

    def test_leader_record_carries_own_install_identity(self):
        with mock.patch.object(self.rm, "run_relay", return_value=({"kit": "2.9.7", "fingerprint": "abc"}, 0)):
            self.assertEqual(self.rm.leader_record()["identity"], {"kit": "2.9.7", "fp": "abc"})

    def test_status_returns_word_for_out_of_range_leader_pid(self):
        lock = self.rm.LeaderLock("sess")
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        lock.stamp("oversized")
        self.store.update(lambda n: n.update({
            "leader": {"pid": 2**31, "instance": "oversized"},
        }))
        self.assertTrue(self.rm.status_line(self.store, None).startswith(
            "relay-monitor: starting "
        ))

    def test_status_returns_word_for_out_of_range_standby_pid(self):
        self.store.update(lambda n: n.update({"standbys": [{"pid": 2**31}]}))
        self.assertTrue(self.rm.status_line(self.store, None).startswith(
            "relay-monitor: not-started "
        ))

    def test_refused_probe_never_reads_dead(self):
        rm = self.rm
        leader = rm.leader_record()
        lock = rm.LeaderLock("sess")
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        lock.stamp(leader["instance"])
        self.store.update(
            lambda note: note.update(
                {
                    "leader": leader,
                    "phase": "following",
                    "root": str(self.root),
                    "progress": {str(self.root): {"file": "x", "position": 1}},
                }
            )
        )
        for error, observed, expected in (
            (errno.EPERM, None, "armed"),
            (errno.EINVAL, None, "armed"),
            (errno.ESRCH, None, "starting"),
            (errno.EPERM, "different-start", "starting"),
        ):
            with self.subTest(error=error, observed=observed):
                with (
                    mock.patch.object(
                        rm.os, "kill", side_effect=OSError(error, "probe failed")
                    ),
                    mock.patch.object(rm, "process_start_time", return_value=observed),
                ):
                    self.assertEqual(rm.readiness(self.store)[0], expected)

    def test_standby_with_refused_probe_counts(self):
        rm = self.rm
        self.store.update(
            lambda note: note.update(
                {"standbys": [{"pid": os.getpid(), "start_time": "A"}]}
            )
        )
        for error, expected in (
            (errno.EPERM, "standby-only"),
            (errno.EINVAL, "standby-only"),
            (errno.ESRCH, "not-started"),
        ):
            with self.subTest(error=error):
                with (
                    mock.patch.object(
                        rm.os, "kill", side_effect=OSError(error, "probe failed")
                    ),
                    mock.patch.object(rm, "process_start_time", return_value=None),
                ):
                    self.assertEqual(rm.readiness(self.store)[0], expected)

    def test_readiness_states_tied_to_lock_holder(self):
        rm = self.rm
        self.assertEqual(rm.readiness(self.store)[0], "not-started")
        lock = rm.LeaderLock("sess")
        lock.acquire(blocking=False)
        self.assertEqual(
            rm.readiness(self.store)[0], "starting"
        )  # lock held, no stamp, no record
        me = rm.leader_record()
        lock.stamp(me["instance"])
        self.store.update(
            lambda n: n.update({"leader": me, "phase": "waiting-for-binding"})
        )
        self.assertEqual(rm.readiness(self.store)[0], "waiting-for-binding")
        self.store.update(
            lambda n: (
                n.update({"phase": "following", "seat": "a", "root": str(self.root)}),
                n["progress"].__setitem__(str(self.root), {"file": "x", "position": 1}),
            )
        )
        self.assertEqual(rm.readiness(self.store)[0], "armed")
        self.assertEqual(
            rm.readiness(self.store, _between=lambda: lock.stamp("someone-else"))[0],
            "starting",
        )  # takeover between the probe and the record read
        lock.stamp(me["instance"])
        self.store.update(lambda n: n.update({"leader": {**me, "start_time": "1970"}}))
        self.assertEqual(rm.readiness(self.store)[0], "starting")  # pid reuse
        self.store.update(
            lambda n: n.update(
                {
                    "leader": me,
                    "binding_degraded": {"reason": "no-receipt", "stamp": "s"},
                }
            )
        )
        self.assertEqual(rm.readiness(self.store)[0], "armed")
        lock.release()
        self.store.update(
            lambda n: n.update({"binding_degraded": None, "standbys": [me]})
        )
        self.assertEqual(rm.readiness(self.store)[0], "standby-only")

    def test_real_stopped_daemon_classification_and_status_remedy(self):
        self.store.update(lambda note: note.update({"root": str(self.root)}))
        direct = subprocess.run(
            [
                sys.executable,
                str(SCRIPT.parents[1] / "relay"),
                "status",
                "--root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(direct.returncode, 1)
        self.assertEqual(direct.stderr.splitlines()[0], "E-DAEMON-DOWN")
        remedy = next(
            line for line in direct.stderr.splitlines() if line.startswith("remedy:")
        )
        with self.subTest("classification"):
            self.assertEqual(
                self.rm.check_identity(None, str(self.root))[0], "daemon-down"
            )
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(self.rm.status(ns(session="sess")), 0)
        with self.subTest("rendered daemon"):
            self.assertIn("daemon=down", out.getvalue())
        with self.subTest("rendered remedy"):
            self.assertIn(remedy, out.getvalue().splitlines())

    def test_non_json_cli_errors_classify_first_stderr_line(self):
        client = {"kit": "2.9.5", "fingerprint": "aa"}
        for text, expected in (
            ("E-DAEMON-DOWN\ncause: not running", "daemon-down"),
            ("E-WIRE-VERSION\ncause: incompatible", "wire-version"),
            ("E-ROOT-MISSING\nremedy: choose root", "root-missing"),
            ("unrecognized socket diagnostic", "unknown"),
        ):
            with (
                self.subTest(text=text),
                mock.patch.object(
                    self.rm.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(client), ""),
                        subprocess.CompletedProcess([], 1, "diagnostic stdout", text),
                    ],
                ),
            ):
                self.assertEqual(
                    self.rm.check_identity(None, str(self.root))[0], expected
                )

    def test_identity_comparison_requires_both_valid_identities(self):
        client = {"kit": "2.9.5", "fingerprint": "aa"}
        daemon = {"daemon": {"identity": {"kit": "2.9.5", "fp": "aa"}}}
        for client_doc, daemon_doc in (
            ({"raw": "invalid version"}, daemon),
            (client, {"daemon": {"identity": {}}}),
            ({}, {}),
            ([], daemon),
            (client, {"daemon": {"identity": "invalid"}}),
        ):
            with self.subTest(client=client_doc, daemon=daemon_doc):
                with mock.patch.object(
                    self.rm,
                    "run_relay",
                    side_effect=[
                        (client_doc, 0),
                        (daemon_doc, 0),
                    ],
                ):
                    self.assertEqual(
                        self.rm.check_identity(None, str(self.root))[0], "unknown"
                    )

    def test_status_line_renders_identity_outcomes(self):
        client = {"kit": "2.9.5", "fingerprint": "aa"}
        hand = self.base / "hand-status"
        hand.mkdir()
        for root, responses, expected in (
            (hand, [(client, 0)], "daemon=hand-root"),
            (self.root, [(client, 0), ({"code": "E-DAEMON-DOWN"}, 1)], "daemon=down"),
            (
                self.root,
                [
                    (client, 0),
                    (
                        {
                            "daemon": {
                                "identity": {
                                    "kit": "2.9.4",
                                    "fp": "bb",
                                }
                            }
                        },
                        0,
                    ),
                ],
                "identity=mismatch",
            ),
        ):
            with self.subTest(expected=expected):
                self.store.update(lambda note: note.update({"root": str(root)}))
                with mock.patch.object(
                    self.rm,
                    "run_relay",
                    side_effect=responses,
                ):
                    self.assertIn(expected, self.rm.status_line(self.store, None))

    def test_identity_paths(self):
        rm = self.rm
        engine_root = str(self.root)
        with mock.patch.object(
            rm,
            "run_relay",
            side_effect=[
                ({"kit": "2.9.5", "fingerprint": "aa"}, 0),
                ({"daemon": {"identity": {"kit": "2.9.5", "fp": "aa"}}}, 0),
            ],
        ):
            self.assertEqual(rm.check_identity("/plug", engine_root)[0], "ok")
        with mock.patch.object(
            rm,
            "run_relay",
            side_effect=[
                ({"kit": "2.9.5", "fingerprint": "aa"}, 0),
                ({"daemon": {"identity": {"kit": "2.9.3", "fp": "bb"}}}, 0),
            ],
        ):
            self.assertEqual(
                rm.check_identity("/plug", engine_root)[0], "version-mismatch"
            )
        with mock.patch.object(
            rm,
            "run_relay",
            side_effect=[
                ({"kit": "2.9.5", "fingerprint": "aa"}, 0),
                ({"code": "E-WIRE-VERSION"}, 1),
            ],
        ):
            self.assertEqual(rm.check_identity("/plug", engine_root)[0], "wire-version")
        with mock.patch.object(
            rm,
            "run_relay",
            side_effect=[
                ({"kit": "2.9.5", "fingerprint": "aa"}, 0),
                ({"code": "E-DAEMON-DOWN"}, 1),
            ],
        ):
            self.assertEqual(rm.check_identity("/plug", engine_root)[0], "daemon-down")
        hand = self.base / "hand"
        hand.mkdir()
        with mock.patch.object(
            rm, "run_relay", side_effect=[({"kit": "2.9.5", "fingerprint": "aa"}, 0)]
        ):
            self.assertEqual(rm.check_identity("/plug", str(hand))[0], "hand-root")


class OperatorReplayTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("sess")

    def rows(self, specs):
        write_index(
            self.index,
            "".join(row(i, frm=frm, to=to, cc=cc) for i, frm, to, cc in specs),
        )

    def test_operator_fires_once_per_new_row_not_for_backlog_and_not_after_restart(
        self,
    ):
        self.rows(
            [
                (1, "a.planner", "operator", "—"),
                (2, "a.planner", "b.implementer", "operator"),
            ]
        )
        fired = []
        with mock.patch.object(
            self.rm, "notify", side_effect=lambda t, x, notifier: fired.append(x)
        ):
            self.assertEqual(
                self.rm.operator_scan([str(self.root)], notifier="osascript"), 0
            )
            self.rows(
                [
                    (1, "a.planner", "operator", "—"),
                    (2, "a.planner", "b.implementer", "operator"),
                    (3, "a.planner", "operator", "—"),
                    (4, "a.planner", "x.planner", "—"),
                ]
            )
            self.assertEqual(
                self.rm.operator_scan([str(self.root)], notifier="osascript"), 1
            )
            self.assertIn("lane/r3.md", fired[0])
        self.rm = load_script()
        with mock.patch.object(
            self.rm, "notify", side_effect=lambda t, x, notifier: fired.append(x)
        ):
            self.assertEqual(
                self.rm.operator_scan([str(self.root)], notifier="osascript"), 0
            )

    def test_notifier_failure_is_recorded_and_does_not_stall(self):
        self.rows([(1, "a.planner", "operator", "—")])
        self.rm.operator_scan([str(self.root)], notifier="none")
        self.rows(
            [(1, "a.planner", "operator", "—"), (2, "a.planner", "operator", "—")]
        )
        with mock.patch.object(self.rm, "notify", side_effect=OSError("no osascript")):
            self.assertEqual(
                self.rm.operator_scan([str(self.root)], notifier="osascript"), 1
            )
        op = self.rm.NoteStore("operator").load()
        self.assertEqual(op["progress"][str(self.root)]["file"], "lane/r2.md")
        self.assertIn("notify-failed", op["reason"])

    def run_replay(self, **kw):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = self.rm.replay(ns(session="sess", **kw))
        return rc, out.getvalue().splitlines()

    def seed(
        self, progress_file, progress_pos, floor=None, root=None, seat="b.implementer"
    ):
        root = str(root or self.root)
        self.store.update(
            lambda n: (
                n.update(
                    {
                        "seat": seat,
                        "root": root,
                        "binding_gen": n.get("binding_gen", 0) + 1,
                        "binding_source": "hook",
                        "anchor": progress_file,
                    }
                ),
                n["progress"].__setitem__(
                    root, {"file": progress_file, "position": progress_pos}
                ),
                n["floors"].append(
                    {"root": root, "seat": seat, **(floor or dict(self.rm.SENTINEL))}
                ),
            )
        )

    def other_root(self, name):
        other = self.base / name / ".relays" / "v2"
        (other / "lane").mkdir(parents=True)
        return other

    def test_two_bindings_replayed_each_from_own_floor(self):  # B6.1
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 4)])
        self.seed("lane/r3.md", 3)  # root A, seat X: floor sentinel, upper r3
        other = self.other_root("B")
        write_index(
            other / "INDEX.md",
            row(1, frm="q.planner", to="c.reviewer")
            + row(2, frm="q.planner", to="c.reviewer")
            + row(3, frm="q.planner", to="c.reviewer"),
        )
        self.seed(
            "lane/r3.md",
            3,
            floor={"file": "lane/r1.md", "position": 1},
            root=other,
            seat="c.reviewer",
        )  # root B, seat Y: floor r1, upper r3
        rc, lines = self.run_replay(limit=2)
        token = json.loads(lines[-1])["continue"]
        page1 = [json.loads(line) for line in lines[:-1]]
        rc, lines = self.run_replay(limit=10, token=token)
        page2 = [json.loads(line) for line in lines[:-1]]
        self.assertEqual(json.loads(lines[-1]), {"done": True})
        self.assertEqual(rc, 0)
        got = [(d["root"], d["file"]) for d in page1 + page2]
        self.assertEqual(
            got,
            [
                (str(self.root), "lane/r1.md"),
                (str(self.root), "lane/r2.md"),
                (str(self.root), "lane/r3.md"),
                (str(other), "lane/r2.md"),
                (str(other), "lane/r3.md"),
            ],
        )
        n = self.store.load()
        self.assertEqual(n["progress"][str(self.root)]["file"], "lane/r3.md")
        self.assertEqual(
            n["progress"][str(other)]["file"], "lane/r3.md"
        )  # progress unchanged in both roots

    def test_rebind_and_rebase_leave_floors_unchanged(self):  # B6.2
        self.rows(
            [
                (1, "a.planner", "b.implementer", "—"),
                (2, "b.implementer", "a.planner", "—"),
            ]
        )
        (self.root / "lane" / "r1.md").write_text("x\n")
        (self.root / "lane" / "r2.md").write_text("y\n")
        ok, _ = self.rm.bind_from_receipt(
            self.store,
            root=str(self.root),
            receipt_path="lane/r2.md",
            key_seat=None,
            source="hook",
        )
        self.assertTrue(ok)
        floors = self.store.load()["floors"]
        self.assertEqual(len(floors), 1)
        ok, _ = self.rm.bind_from_receipt(
            self.store,
            root=str(self.root),
            receipt_path="lane/r2.md",
            key_seat=None,
            source="hook",
        )  # rebind, same pair
        self.assertEqual(self.store.load()["floors"], floors)
        self.store.update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/gone.md", "position": 9}
            )
        )  # lost progress → rebase on the next transaction
        binding = self.rm.resolve_binding(self.store.load(), {}, None)
        self.rm.deliver_once(
            self.store,
            self.rm.ByteSink(),
            binding,
            self.rm.read_index(self.index),
            end=time.monotonic() + 0.5,
            leader_instance="L",
        )
        n = self.store.load()
        self.assertEqual(n["reason"], "rebased")
        self.assertEqual(n["floors"], floors)

    def test_same_root_two_seats_each_from_own_floor(self):  # A/X then A/Y
        self.rows(
            [
                (1, "a.planner", "b.implementer", "—"),
                (2, "a.planner", "c.reviewer", "—"),
                (3, "a.planner", "c.reviewer", "—"),
            ]
        )
        self.seed("lane/r3.md", 3, seat="b.implementer")
        self.seed(
            "lane/r3.md",
            3,
            floor={"file": "lane/r2.md", "position": 2},
            seat="c.reviewer",
        )
        rc, lines = self.run_replay(limit=50)
        docs = [json.loads(line) for line in lines[:-1]]
        self.assertEqual(
            [(d["file"], d["to"]) for d in docs],
            [("lane/r1.md", "b.implementer"), ("lane/r3.md", "c.reviewer")],
        )
        self.assertEqual(rc, 0)

    def test_binding_appended_mid_walk_is_not_incorporated_and_failure_persists(self):
        self.seed(
            "lane/r1.md",
            1,
            root=self.base / "missing" / ".relays" / "v2",
            seat="c.reviewer",
        )  # unreadable entry first
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 4)])
        self.seed("lane/r3.md", 3)
        rc, lines = self.run_replay(limit=2)
        docs = [json.loads(line) for line in lines]
        self.assertEqual(docs[0]["error"], "cannot-establish-recovery")
        self.assertEqual([d["file"] for d in docs[1:-1]], ["lane/r1.md", "lane/r2.md"])
        token = docs[-1]["continue"]
        late = self.other_root("late")
        write_index(late / "INDEX.md", row(1, frm="q.planner", to="b.implementer"))
        self.seed("lane/r1.md", 1, root=late)  # appended after page 1
        rc, lines = self.run_replay(limit=50, token=token)
        docs = [json.loads(line) for line in lines]
        self.assertEqual([d.get("file") for d in docs if "file" in d], ["lane/r3.md"])
        self.assertEqual(docs[-1], {"done": True})
        self.assertEqual(rc, 3)  # frozen set; earlier failure kept
        rc, lines = self.run_replay(limit=50)
        self.assertIn(
            str(late),
            [
                json.loads(line).get("root")
                for line in lines
                if "file" in json.loads(line)
            ],
        )  # a fresh walk includes it

    def test_after_round_trip_across_pages_with_producer_advance(self):  # D10-R1
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 5)])
        self.seed("lane/r4.md", 4)
        rc, lines = self.run_replay(limit=1, after="lane/r1.md")
        self.assertEqual(rc, 0)
        self.assertEqual(
            [json.loads(line)["file"] for line in lines[:-1]], ["lane/r2.md"]
        )
        token = json.loads(lines[-1])["continue"]
        frozen = json.loads(base64.b64decode(token))
        self.assertEqual(
            (frozen["floors"][0]["floor_file"], frozen["floors"][0]["floor_position"]),
            ("lane/r1.md", 1),
        )  # consistent pair
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 7)])
        self.store.update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/r6.md", "position": 6}
            )
        )  # producer advanced
        rc, lines = self.run_replay(limit=1, after="lane/r1.md", token=token)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [json.loads(line)["file"] for line in lines[:-1]], ["lane/r3.md"]
        )
        token = json.loads(lines[-1])["continue"]
        rc, lines = self.run_replay(limit=1, after="lane/r1.md", token=token)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [json.loads(line)["file"] for line in lines[:-1]], ["lane/r4.md"]
        )
        self.assertEqual(
            json.loads(lines[-1]), {"done": True}
        )  # r5, r6 never emitted: upper fixed at r4
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r6.md"
        )
        rc, lines = self.run_replay(limit=50, after="lane/absent.md")
        docs = [json.loads(line) for line in lines]
        self.assertEqual(rc, 3)
        self.assertEqual(
            docs[0], {"error": "cannot-establish-recovery", "root": str(self.root)}
        )
        self.assertEqual(docs[-1], {"done": True})
        self.assertEqual(len(docs), 2)

    def test_unreadable_root_yields_one_error_and_continues(self):  # B6.3
        self.rows([(1, "a.planner", "b.implementer", "—")])
        self.seed("lane/r1.md", 1)
        self.seed(
            "lane/r1.md",
            1,
            root=self.base / "missing" / ".relays" / "v2",
            seat="c.reviewer",
        )  # no INDEX.md there
        rc, lines = self.run_replay(limit=50)
        docs = [json.loads(line) for line in lines]
        self.assertEqual(rc, 3)
        self.assertEqual([d.get("file") for d in docs if "file" in d], ["lane/r1.md"])
        errors = [d for d in docs if "error" in d]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "cannot-establish-recovery")
        self.assertIn("missing", errors[0]["root"])
        self.assertEqual(docs[-1], {"done": True})

    def test_schedule_a_dropped_b_then_received_c(self):
        self.rows(
            [
                (1, "a.planner", "b.implementer", "—"),
                (2, "a.planner", "b.implementer", "—"),
                (3, "a.planner", "b.implementer", "—"),
            ]
        )
        self.seed(
            "lane/r3.md", 3
        )  # B=r2 dropped by the host, C=r3 received; progress past both
        rc, lines = self.run_replay(limit=50)
        self.assertEqual(rc, 0)
        self.assertIn("lane/r2.md", [json.loads(line).get("file") for line in lines])
        self.assertEqual(json.loads(lines[-1]), {"done": True})
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r3.md"
        )

    def test_schedule_b_dropped_b_then_own_d_refreshes_binding(self):
        self.rows([(1, "a.planner", "b.implementer", "—")])
        (self.root / "lane" / "r1.md").write_text("x\n")
        self.seed("lane/r1.md", 1)  # B=r1 dropped; nothing received yet
        self.rows(
            [
                (1, "a.planner", "b.implementer", "—"),
                (2, "b.implementer", "a.planner", "—"),
            ]
        )
        (self.root / "lane" / "r2.md").write_text("d\n")
        ok, _ = self.rm.bind_from_receipt(
            self.store,
            root=str(self.root),
            receipt_path="lane/r2.md",
            key_seat=None,
            source="hook",
        )  # own D filed → binding refresh
        self.assertTrue(ok)
        self.assertEqual(self.store.load()["anchor"], "lane/r2.md")
        rc, lines = self.run_replay(limit=50)
        self.assertEqual(
            [json.loads(line).get("file") for line in lines[:-1]], ["lane/r1.md"]
        )

    def test_paging_keeps_first_upper_bound_while_producer_advances(self):
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 4)])
        self.seed("lane/r3.md", 3)
        rc, lines = self.run_replay(limit=2)
        token = json.loads(lines[-1])["continue"]
        self.assertEqual(
            [json.loads(line)["file"] for line in lines[:-1]],
            ["lane/r1.md", "lane/r2.md"],
        )
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 6)])
        self.store.update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/r5.md", "position": 5}
            )
        )  # producer advanced
        rc, lines = self.run_replay(limit=2, token=token)
        self.assertEqual(
            [json.loads(line)["file"] for line in lines[:-1]], ["lane/r3.md"]
        )
        self.assertEqual(json.loads(lines[-1]), {"done": True})
        self.assertEqual(
            self.store.load()["progress"][str(self.root)]["file"], "lane/r5.md"
        )

    def test_cannot_establish_recovery(self):
        self.rows([(1, "a.planner", "b.implementer", "—")])
        self.seed("lane/r1.md", 1, floor={"file": "lane/gone.md", "position": 7})
        rc, lines = self.run_replay(limit=50)
        self.assertEqual(
            (rc, json.loads(lines[0])),
            (3, {"error": "cannot-establish-recovery", "root": str(self.root)}),
        )
        self.assertEqual(json.loads(lines[-1]), {"done": True})
        self.store.update(lambda n: n.__setitem__("floors", []))
        self.seed("lane/r1.md", 1)

        def tok(**kw):
            base = {
                "floors": [
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": None,
                        "floor_position": 0,
                        "upper_file": "lane/r1.md",
                        "upper_position": 1,
                    }
                ],
                "index": 0,
                "next_position": 1,
                "failed": False,
            }
            base.update(kw)
            return base64.b64encode(json.dumps(base).encode()).decode()

        malformed = [
            tok(
                floors=[
                    {
                        "root": "/elsewhere",
                        "seat": "b.implementer",
                        "floor_file": None,
                        "floor_position": 0,
                        "upper_file": "lane/r1.md",
                        "upper_position": 1,
                    }
                ]
            ),  # a root this session never bound
            tok(floors=[]),  # D8-R2: empty walk
            tok(index=999),  # D8-R2: impossible index
            tok(index=1),  # index at the end: nothing left to visit is not success
            tok(
                floors=[{"root": str(self.root), "seat": "b.implementer"}]
            ),  # D8-R2: entry missing its bounds at a valid index
            tok(next_position=0),
            tok(failed="no"),
            tok(next_position=999),  # D9-R1: cursor beyond the frozen upper bound
            tok(next_position=2),  # D9-R1: cursor beyond upper (upper is r1)
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": "lane/r1.md",
                        "floor_position": 1,
                        "upper_file": "lane/r1.md",
                        "upper_position": 1,
                    }
                ],
                next_position=1,
            ),  # cursor at the floor
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": "lane/r1.md",
                        "floor_position": 1,
                        "upper_file": "lane/r1.md",
                        "upper_position": 1,
                    }
                ],
                next_position=0,
            ),  # cursor before the floor
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": None,
                        "floor_position": True,
                        "upper_file": "lane/r1.md",
                        "upper_position": 1,
                    }
                ]
            ),  # boolean position
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": None,
                        "floor_position": 0,
                        "upper_file": "lane/r1.md",
                        "upper_position": -1,
                    }
                ]
            ),  # negative position
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": None,
                        "floor_position": 0,
                        "unavailable": "yes",
                    }
                ]
            ),  # truthy non-boolean marker
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": None,
                        "floor_position": 4,
                        "upper_file": "lane/r1.md",
                        "upper_position": 1,
                    }
                ]
            ),  # inconsistent sentinel
            tok(
                floors=[
                    {
                        "root": str(self.root),
                        "seat": "b.implementer",
                        "floor_file": "lane/r1.md",
                        "floor_position": 1,
                        "upper_file": None,
                        "upper_position": 0,
                    }
                ],
                next_position=1,
            ),  # upper below floor
        ]
        for bad in malformed:
            rc, lines = self.run_replay(limit=50, token=bad)
            docs = [json.loads(line) for line in lines]
            self.assertEqual(rc, 3, bad)
            self.assertEqual(
                docs[0], {"error": "cannot-establish-recovery", "root": None}
            )
            self.assertEqual(docs[-1], {"done": True})
            self.assertEqual(len(docs), 2)
        rc, lines = self.run_replay(limit=50, token=tok())
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(lines[0])["file"], "lane/r1.md"
        )  # the well-formed token still walks
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 4)])
        self.store.update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/r3.md", "position": 3}
            )
        )
        edge = tok(
            floors=[
                {
                    "root": str(self.root),
                    "seat": "b.implementer",
                    "floor_file": None,
                    "floor_position": 0,
                    "upper_file": "lane/r3.md",
                    "upper_position": 3,
                }
            ],
            next_position=3,
        )
        rc, lines = self.run_replay(limit=50, token=edge)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [json.loads(line)["file"] for line in lines[:-1]], ["lane/r3.md"]
        )  # a valid continuation exactly at the boundary row
        gap = tok(
            floors=[
                {
                    "root": str(self.root),
                    "seat": "b.implementer",
                    "floor_file": None,
                    "floor_position": 0,
                    "upper_file": "lane/r3.md",
                    "upper_position": 3,
                }
            ],
            next_position=2,
        )
        self.index.write_text(
            HEADER + row(1, to="b.implementer") + row(3, to="b.implementer")
        )  # a rewritten index: r3 now sits at position 2, not its frozen 3
        rc, lines = self.run_replay(limit=50, token=gap)
        docs = [json.loads(line) for line in lines]
        self.assertEqual(rc, 3)
        self.assertEqual(
            docs,
            [
                {"error": "cannot-establish-recovery", "root": str(self.root)},
                {"done": True},
            ],
        )  # the entry fails as recovery, not as a malformed token
        self.store.update(lambda n: n.__setitem__("floors", []))
        self.rows([(i, "a.planner", "b.implementer", "—") for i in range(1, 5)])
        self.seed("lane/r4.md", 4, floor={"file": "lane/r2.md", "position": 2})
        rc, lines = self.run_replay(limit=1)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(lines[0])["file"], "lane/r3.md")
        moved = json.loads(lines[-1])["continue"]  # a real emitted token, cursor at r4
        self.index.write_text(
            HEADER
            + row(2, to="b.implementer")
            + row(1, to="b.implementer")
            + row(3, to="b.implementer")
            + row(4, to="b.implementer")
        )  # floor r2 now sits at position 1; upper r4 unchanged
        rc, lines = self.run_replay(limit=50, token=moved)
        docs = [json.loads(line) for line in lines]
        self.assertEqual(rc, 3)
        self.assertEqual(
            docs,
            [
                {"error": "cannot-establish-recovery", "root": str(self.root)},
                {"done": True},
            ],
        )  # the moved floor fails the entry on the continuation page too (D24-R1)


class EnvRestartReplayTests(FollowProcessMixin, TmpEnv):
    """
    The replay half of the D8-R1 witness: a real follower restart with a new seat, then
    the real replay subprocess.
    """

    def test_env_restart_then_replay_recovers_the_new_seats_dropped_row(self):
        self.index.write_text(HEADER)
        p = self.spawn(seat="b.implementer")
        first = self.wait_note(
            lambda n: self.bound(n) and len(n.get("floors", [])) == 1
        )
        old_floor = dict(first["floors"][0])
        write_index(self.index, row(1, to="b.implementer"))
        self.assertEqual(self.read_lines(p, 1)[0]["file"], "lane/r1.md")
        self.wait_note(
            lambda n: n["progress"][str(self.root)].get("file") == "lane/r1.md"
        )
        p.kill()
        p.wait()
        q = self.spawn(seat="c.reviewer")
        self.wait_note(
            lambda n: n.get("seat") == "c.reviewer" and len(n.get("floors", [])) == 2
        )
        write_index(self.index, row(1, to="b.implementer") + row(2, to="c.reviewer"))
        self.assertEqual(self.read_lines(q, 1)[0]["file"], "lane/r2.md")
        self.wait_note(
            lambda n: n["progress"][str(self.root)].get("file") == "lane/r2.md"
        )
        q.kill()
        q.wait()
        write_index(
            self.index,
            row(1, to="b.implementer")
            + row(2, to="c.reviewer")
            + row(3, to="c.reviewer"),
        )
        self.rm.NoteStore("psess").update(
            lambda n: n["progress"].__setitem__(
                str(self.root), {"file": "lane/r3.md", "position": 3}
            )
        )  # r3 checkpointed but dropped by the host
        out = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "replay",
                "--session",
                "psess",
                "--limit",
                "50",
            ],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=20,
        )
        docs = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            [(d["file"], d["to"]) for d in docs if "file" in d],
            [
                ("lane/r1.md", "b.implementer"),
                ("lane/r2.md", "c.reviewer"),
                ("lane/r3.md", "c.reviewer"),
            ],
        )  # X's floor and Y's floor both walked; Y's dropped r3 recovered
        after = self.rm.NoteStore("psess").load()
        self.assertEqual(len(after["floors"]), 2)
        self.assertEqual(after["floors"][0], old_floor)


class CliSmokeTests(TmpEnv):
    def test_help_and_status_run_as_a_script(self):
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )
        self.assertEqual(p.returncode, 0)
        for mode in (
            "follow",
            "bind",
            "drain",
            "session-start",
            "status",
            "operator",
            "replay",
        ):
            self.assertIn(mode, p.stdout)
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "status", "--session", "smoke"],
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith("relay-monitor: not-started"))
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "drain", "--host", "claude-code"],
            input="{}",
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )
        self.assertEqual(json.loads(p.stdout), {})


class BoundedRootCallTests(TmpEnv):
    """Catch unbounded waits, duplicate workers and budget resets using raw barriers."""

    def _probe(self, body):
        import textwrap
        bootstrap = '''import importlib.util, sys, threading, time, unittest, errno
spec = importlib.util.spec_from_file_location("budget_probe", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
t = unittest.TestCase()
release = threading.Event()
entered = threading.Event()
def blocked():
    entered.set()
    release.wait()
    return "late"
def reap():
    release.set()
    for worker in threading.enumerate():
        if worker is not threading.current_thread():
            worker.join(2)
            t.assertFalse(worker.is_alive(), "worker not reaped")
'''
        code = bootstrap + "\ntry:\n" + textwrap.indent(textwrap.dedent(body), "    ") + "\nfinally:\n    reap()\n"
        try:
            result = subprocess.run([sys.executable, "-c", code, str(SCRIPT)],
                                    capture_output=True, text=True, timeout=10,
                                    env=dict(os.environ))
        except subprocess.TimeoutExpired as exc:
            self.fail(f"blocked past SUPERVISOR_TIMEOUT; stdout={exc.stdout!r}; stderr={exc.stderr!r}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stalled_call_reports_and_starts_one_worker(self):
        self._probe('''
            start = time.monotonic()
            result = m.bounded_root_call(blocked)
            t.assertEqual(result.kind, "stalled")
            t.assertTrue(entered.is_set())
            t.assertLess(time.monotonic() - start, 1.25)
            count = threading.active_count()
            start = time.monotonic()
            t.assertEqual(m.bounded_root_call(lambda: t.fail("second worker")).kind, "stalled")
            t.assertLess(time.monotonic() - start, .05)
            t.assertEqual(threading.active_count(), count)
            reap()
            t.assertEqual(m.bounded_root_call(lambda: 7).value, 7)
        ''')

    def test_late_result_is_discarded(self):
        self._probe('''
            store = m.NoteStore("late-result")
            before = store.load()
            with m.root_budget(.02):
                result = m.bounded_root_call(blocked)
            t.assertEqual(result.kind, "stalled")
            reap()
            t.assertIsNone(result.value)
            t.assertEqual(store.load(), before)
            t.assertEqual(m.bounded_root_call(lambda: "fresh").value, "fresh")
        ''')

    def test_immediate_stall_consumes_no_budget(self):
        self._probe('''
            with m.root_budget(.02):
                t.assertEqual(m.bounded_root_call(blocked).kind, "stalled")
            with m.root_budget() as budget:
                before = budget.remaining()
                t.assertEqual(m.bounded_root_call(lambda: None).kind, "stalled")
                t.assertEqual(budget.remaining(), before)
            reap()
            with m.root_budget() as budget:
                before = budget.remaining()
                t.assertEqual(m.bounded_root_call(lambda: threading.Event().wait(.3)).kind, "ok")
                t.assertGreaterEqual(before - budget.remaining(), .3)
        ''')

    def test_spent_budget_returns_stalled_without_a_thread(self):
        self._probe('''
            with m.root_budget(0):
                count = threading.active_count()
                t.assertEqual(m.bounded_root_call(lambda: t.fail("started")).kind, "stalled")
                t.assertEqual(threading.active_count(), count)
        ''')

    def test_cap_bounds_the_deadline(self):
        self._probe('''
            start = time.monotonic()
            with m.root_budget(cap=start + .2):
                t.assertEqual(m.bounded_root_call(blocked).kind, "stalled")
            t.assertLess(time.monotonic() - start, .45)
        ''')

    def test_failed_carries_errno_name(self):
        self._probe('''
            def denied(): raise PermissionError(errno.EACCES, "denied")
            def invalid(): raise ValueError("invalid")
            for fn, detail in [(denied, "EACCES"), (invalid, "ValueError")]:
                result = m.bounded_root_call(fn)
                t.assertEqual((result.kind, result.detail), ("failed", detail))
        ''')

    def test_scoped_budget_is_shared_across_calls(self):
        self._probe('''
            with m.root_budget():
                t.assertEqual(m.bounded_root_call(lambda: threading.Event().wait(.6)).kind, "ok")
                start = time.monotonic()
                t.assertEqual(m.bounded_root_call(blocked).kind, "stalled")
                t.assertLess(time.monotonic() - start, .55)
            reap()
            release.clear()
            start = time.monotonic()
            with m.root_budget():
                t.assertEqual(m.bounded_root_call(blocked).kind, "stalled")
            t.assertGreaterEqual(time.monotonic() - start, .95)
        ''')

    def test_nested_scope_reuses_outer_budget(self):
        self._probe('''
            with m.root_budget() as outer:
                outer.spend(.8)
                with m.root_budget() as inner:
                    t.assertIs(inner, outer)
                    t.assertLessEqual(inner.remaining(), .2)
            for outer_cap, inner_cap in [(1, .1), (.1, 1)]:
                start = time.monotonic()
                release.clear()
                with m.root_budget(cap=start + outer_cap):
                    with m.root_budget(cap=start + inner_cap):
                        t.assertEqual(m.bounded_root_call(blocked).kind, "stalled")
                t.assertLess(time.monotonic() - start, .25)
                reap()
        ''')

    def test_scope_resets_on_normal_and_exceptional_exit(self):
        self._probe('''
            t.assertIsNone(m.current_budget())
            with m.root_budget() as outer:
                for throw in [False, True]:
                    try:
                        with m.use_budget(m.RootBudget(.2)):
                            t.assertIsNot(m.current_budget(), outer)
                            if throw: raise ValueError("scope")
                    except ValueError: pass
                    t.assertIs(m.current_budget(), outer)
            t.assertIsNone(m.current_budget())
            try:
                with m.root_budget(): raise ValueError("outer")
            except ValueError: pass
            t.assertIsNone(m.current_budget())
        ''')

    def test_use_budget_reactivates_a_retained_budget(self):
        self._probe('''
            a, b = m.RootBudget(), m.RootBudget()
            with m.use_budget(a):
                t.assertEqual(m.bounded_root_call(lambda: threading.Event().wait(.3)).kind, "ok")
                remaining = a.remaining()
            with m.use_budget(b):
                t.assertEqual(b.remaining(), 1)
                b.spend(.1)
            with m.use_budget(a):
                t.assertEqual(m.current_budget().remaining(), remaining)
            t.assertAlmostEqual(b.remaining(), .9)
        ''')

    def test_no_scope_gives_one_call_budget(self):
        self._probe('''
            t.assertIsNone(m.current_budget())
            start = time.monotonic()
            t.assertEqual(m.bounded_root_call(blocked).kind, "stalled")
            t.assertLess(time.monotonic() - start, 1.25)
            t.assertIsNone(m.current_budget())
        ''')


class ExitRecordTests(FollowProcessMixin, TmpEnv):
    """Exit durability, legacy interoperation, and follower-only recovery."""

    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("psess")
        write_index(self.index, "")
        self.store.update(lambda n: n.update(
            seat="b.implementer", root=str(self.root), binding_source="hook",
            progress={str(self.root): {"file": None, "position": 0}}))

    def _status(self):
        with mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})):
            return self.rm.status_line(self.store, None)

    def _seed_exit(self, reason="follower-error"):
        # A persisted B7 record is a fixture, not a call to the helper under test.
        record = dict(reason=reason, detail="ValueError", at="20261015-000000", instance="b7")
        self.store.update(lambda n: n.update(exit=record, reason=reason, leader=None, phase="unavailable"))
        return record

    def _legacy_exit(self):
        self.store.update(lambda n: n.update(phase="unavailable", reason="transport-failed", leader=None))

    def _child_error(self, lock_failure=False):
        # Inject at the raw signature boundary, after follower leader setup.
        code = f'''
import importlib.util, sys, os, subprocess
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("relay_monitor", {str(SCRIPT)!r})
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
holder = None
def fail(path):
    global holder
    if {lock_failure!r}:
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()", str(m.NoteStore('psess').path.parent / 'psess.state')], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert holder.stdout.readline() == b'ready\\n'
    raise ValueError("original-loop-error")
m.index_signature = fail
try:
    m.follow(SimpleNamespace(session="psess", host="claude-code", root=None, anchor=None, plugin_root=None))
finally:
    if holder:
        holder.communicate(timeout=3)
'''
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)

    def test_recorded_exit_is_shown_and_cleared(self):
        p = self.spawn()
        self.wait_note(lambda n: n.get("phase") == "following")
        p.stdout.close()
        write_index(self.index, row(1, to="b.implementer"))
        p.wait(timeout=10)
        self.assertIn("relay-monitor: unavailable: transport-failed", self._status())
        self.assertEqual(self.store.load()["exit"]["reason"], "transport-failed")
        # Empty scan, with established progress, must also clear a prior exit.
        write_index(self.index, "")
        self.store.update(lambda n: n["progress"].update({str(self.root): dict(file=None, position=0)}))
        q = self.spawn()
        self.wait_note(lambda n: n.get("phase") == "following" and not n.get("exit"))
        q.kill()
        q.wait(timeout=10)
        failed = self._child_error()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("ValueError: original-loop-error", failed.stderr)
        self.assertEqual(self.store.load()["exit"]["detail"], "ValueError")
        self.assertIn("relay-monitor: unavailable: follower-error", self._status())
        self.spawn()
        self.wait_note(lambda n: n.get("phase") == "following" and not n.get("exit"))
        self.assertIsNone(self.store.load().get("reason"))

    def _drain(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "drain", "--host", "codex", "--session", "psess"], input="{}", text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_drain_scan_does_not_hide_or_clear_follower_exit(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                record = self._seed_exit()
                if unavailable:
                    self.index.unlink()
                self._drain()
                self.assertEqual(self.store.load()["exit"], record)
                self.assertIn("relay-monitor: unavailable: follower-error", self._status())

    def test_last_resort_record_failure_keeps_original(self):
        failed = self._child_error(lock_failure=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("follower error: ValueError (not recorded:", failed.stderr)
        self.assertIn("ValueError: original-loop-error", failed.stderr)
        self.assertNotIn("raise LockTimeout", failed.stderr)

    def test_legacy_leader_sequence_follows_contract(self):
        self._seed_exit()
        lock = self.rm.LeaderLock("psess")
        self.assertTrue(lock.acquire(blocking=False))
        legacy = self.rm.leader_record()
        lock.stamp(legacy["instance"])
        try:
            self.store.update(lambda n: n.update(leader=legacy, phase="following", reason=None))
            self.assertIn("relay-monitor: armed", self._status())
            self.assertNotIn("follower-error", self._status())
            self._legacy_exit()
        finally:
            lock.release()
        self.assertIn("relay-monitor: unavailable: transport-failed", self._status())

    def test_reason_writer_preserves_later_exit(self):
        for writer in ("drain", "operator", "session"):
            with self.subTest(writer=writer):
                self.store = self.rm.NoteStore("operator" if writer == "operator" else "psess")
                write_index(self.index, "")
                self._seed_exit()
                self._legacy_exit()
                if writer == "drain":
                    self.index.unlink()
                    self._drain()
                elif writer == "operator":
                    self.store.update(lambda n: n["progress"].update({str(self.root): dict(file="gone.md", position=9)}))
                    self.rm.operator_scan([str(self.root)], notifier="none")
                else:
                    with mock.patch.object(self.rm, "read_payload", return_value={}), mock.patch.object(self.rm, "decide_host", return_value="host-unknown"), mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})), contextlib.redirect_stdout(io.StringIO()):
                        self.rm.session_start(ns(session="psess"))
                self.assertEqual(self.store.load()["exit"], dict(reason="transport-failed", detail=None, at=None, instance=None, inferred=True))
                self.assertIn("relay-monitor: unavailable: transport-failed", self._status())

    def test_legacy_writer_residual_is_pinned(self):
        self._seed_exit()
        self._legacy_exit()
        self.store.update(lambda n: n.update(reason="index-missing"))
        self.assertIn("relay-monitor: unavailable: follower-error", self._status())

    def test_same_reason_exit_keeps_reason(self):
        self._seed_exit("transport-failed")
        self._legacy_exit()
        self.assertIn("relay-monitor: unavailable: transport-failed", self._status())
        self.assertEqual(self.store.load()["exit"]["instance"], "b7")

    def test_uninferred_same_reason_record_is_not_latest_provenance(self):
        self._seed_exit("transport-failed")
        self._legacy_exit()
        self.assertIn("relay-monitor: unavailable: transport-failed", self._status())
        record = self.store.load()["exit"]
        self.assertNotIn("inferred", record)
        self.assertEqual(record["instance"], "b7")
        self.assertNotEqual(record["instance"], "legacy")

    def test_signal_killed_follower_still_not_started(self):
        p = self.spawn()
        self.wait_note(lambda n: n.get("phase") == "following")
        p.kill()
        p.wait(timeout=10)
        self.assertFalse(self.store.load().get("exit"))
        self.assertIn("relay-monitor: not-started", self._status())

    def _bind(self, receipt=False):
        if receipt:
            write_index(self.index, row(1))
            (self.root / "lane" / "r1.md").write_text("relay\n")
        payload = dict(session_id="psess", cwd=str(self.root), hook_event_name="PostToolUse", tool_name="Bash", prompt_id="p", tool_input={"command": f"tools/relay submit d.md --root {self.root}"}, tool_response={"stdout": json.dumps(dict(path="lane/r1.md" if receipt else "lane/missing.md", render_state="rendered", duplicate=False)), "stderr": "", "interrupted": False})
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            self.rm.bind(ns())

    def test_legacy_note_loads_in_every_mode(self):
        note = self.store.load()
        for key in ("exit", "reason_detail", "observation_seq", "seq"):
            note.pop(key, None)
        self.store.save(note)
        self._status()
        with mock.patch.object(self.rm, "read_payload", return_value={}), mock.patch.object(self.rm, "check_identity", return_value=("ok", {}, {})), contextlib.redirect_stdout(io.StringIO()):
            self.rm.session_start(ns(session="psess", host="claude-code"))
        self._drain()
        self._bind()
        self.spawn()
        self.wait_note(lambda n: n.get("phase") == "following")

    def test_bind_hook_never_writes_top_level_reason(self):
        for receipt in (False, True):
            self._seed_exit()
            self._legacy_exit()
            before = self.store.load()
            self._bind(receipt)
            after = self.store.load()
            if receipt:
                self.assertEqual(after["anchor"], "lane/r1.md")
            else:
                self.assertIsNotNone(after.get("bind_observation"))
            self.assertEqual(after["reason"], before["reason"])
            self.assertEqual(after["exit"], before["exit"])
