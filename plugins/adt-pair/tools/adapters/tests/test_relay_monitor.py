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
            self.store.load()["phase"], "waiting-for-binding"
        )  # degraded: never promoted
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
            mock.patch.object(self.rm.time, "sleep", side_effect=StopLoop),
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

    def payload(self, command, stdout=None, host="claude"):
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
        return json.dumps(base)

    def run_bind(self, payload_text):
        with mock.patch("sys.stdin", io.StringIO(payload_text)):
            return self.rm.bind(ns())

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
            "unsupported-shell",
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
            p("(cd /x; tools/relay submit d.md --key K)").reason, "unsupported-shell"
        )
        self.assertEqual(
            p("cat d.md | tools/relay submit - --key K").reason, "unsupported-shell"
        )
        self.assertEqual(
            p("tools/relay submit d.md --key K 2>&1 | tail -3").reason,
            "unsupported-shell",
        )
        self.assertEqual(
            p("echo x; cd /w && tools/relay submit d.md --key K").reason,
            "unsupported-shell",
        )
        self.assertEqual(
            p("tools/relay submit d.md --key K # --root R").root_operand, None
        )
        self.assertEqual(p("tools/relay submit 'd#1.md' --key K").ok, True)
        self.assertEqual(p("tools/relay submit d.md --key K >/dev/null").ok, True)
        self.assertEqual(p("tools/relay submit d.md a=b --key K").key, "K")
        self.assertEqual(
            p("tools/relay submit d.md --key K; echo 'a\"b").reason, "unsupported-shell"
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
            (n["seat"], n["binding_gen"], n.get("binding_degraded")),
            ("a.planner", 1, None),
        )
        foreign = (
            f'RELAY_KEY="{self.root}/.engine/seats/z.planner/k.key" '
            f"tools/relay submit d.md --root "
            f"{self.root}"
        )
        self.run_bind(self.payload(foreign, self.receipt))
        n = self.store.load()
        self.assertEqual(n["binding_degraded"]["reason"], "key-mismatch")
        self.assertEqual(n["binding_gen"], 1)

    def test_plain_submit_binds_and_initializes_progress_once(self):
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
        self.run_bind(self.payload(cmd, self.receipt))
        n = self.store.load()
        self.assertEqual(n["binding_gen"], 2)
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
            before["binding_degraded"]
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
        self.assertEqual(n["binding_degraded"]["reason"], "ambiguous-receipt")
        self.assertEqual(
            (n["seat"], n["root"], n["binding_gen"]),
            (before["seat"], before["root"], before["binding_gen"]),
        )  # prior binding preserved

    def test_negatives_preserve_prior_binding(self):
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
                "unsupported-shell",
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
                self.assertEqual(self.run_bind(self.payload(cmd, out)), 0)
                n = self.store.load()
                self.assertEqual(
                    (n["seat"], n["root"], n["binding_gen"]),
                    (before["seat"], before["root"], before["binding_gen"]),
                )
                if reason is None:
                    self.assertIsNone(n["binding_degraded"])
                else:
                    self.assertEqual(n["binding_degraded"]["reason"], reason)

    def test_non_submit_shell_commands_preserve_note_bytes(self):
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
                self.assertIsNone(self.store.load()["binding_degraded"])
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
                self.assertEqual(parse(command).reason, "unsupported-shell")
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

    def test_submit_with_unsupported_shell_still_degrades(self):
        commands = (
            "tools/relay submit d.md | head",
            "ls | tools/relay submit d.md",
            "(tools/relay submit d.md)",
            "tools/relay submit 'unfinished",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.run_bind(self.payload(command)), 0)
                self.assertEqual(
                    self.store.load()["binding_degraded"]["reason"],
                    "unsupported-shell",
                )

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


class DrainTests(TmpEnv):
    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("sess")

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
        self.assertEqual(json.loads(self.run_drain(self.codex_stop())[1]), {})

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
    def setUp(self):
        super().setUp()
        self.store = self.rm.NoteStore("sess")

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
        self.assertEqual(rm.readiness(self.store)[0], "unavailable")
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
                "unavailable: version-mismatch",
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
