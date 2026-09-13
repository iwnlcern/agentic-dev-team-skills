# tools/adapters/tests/test_relay_monitor.py
import base64, contextlib, importlib.util, io, json, os, pathlib, select, subprocess, sys, tempfile, threading, time, types, unittest
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
    base = dict(host="auto", session=None, plugin_root=None, anchor=None, manual=False, root=None, deadline=5.0, since=None, notifier="none", after=None, limit=50, token=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


HEADER = "| time | phase | role | dispatch | parent | from | to | cc | status | file |\n|---|---|---|---|---|---|---|---|---|---|\n"


def row(n, frm="a.planner", to="orch.orchestrator-planner", cc="—", file=None, status="—"):
    return f"| 20260912-{n:06d} | SITREP | Pair Planner | d{n} | — | {frm} | {to} | {cc} | {status} | {file or f'lane/r{n}.md'} |\n"


def write_index(path, rows_text):
    tmp = path.with_suffix(".tmp"); tmp.write_text(HEADER + rows_text, encoding="utf-8"); os.replace(tmp, path)


class TmpEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TMPDIR": self.tmp.name, "ADT_PACE_WINDOW": "0.5", "ADT_PACE_LINES": "5"}, clear=False)
        self.env.start()
        for k in ("ADT_SEAT", "ADT_RELAY_ROOT", "ADT_RELAY_ANCHOR", "ADT_HOST", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "PLUGIN_ROOT", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(k, None)
        self.rm = load_script()
        self.root = pathlib.Path(self.tmp.name) / "sprint" / ".relays" / "v1"
        (self.root / ".engine" / "seats" / "a.planner").mkdir(parents=True); (self.root / "lane").mkdir()
        self.index = self.root / "INDEX.md"

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()


class NoteStoreTests(TmpEnv):
    def test_note_dir_is_private_and_under_tmpdir(self):
        d = self.rm.note_dir()
        self.assertEqual(d, pathlib.Path(self.tmp.name) / "adt-relay-monitor")
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")

    def test_load_returns_defaults_when_absent(self):
        note = self.rm.NoteStore("sess-1").load()
        self.assertEqual((note["binding_gen"], note["phase"], note["progress"], note["frame"]), (0, "not-started", {}, None))

    def test_save_is_atomic_and_private(self):
        store = self.rm.NoteStore("sess-1")
        with store.state_lock():
            note = store.load(); note["seat"] = "a.planner"; store.save(note)
        self.assertEqual(oct(store.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(store.path.read_text())["seat"], "a.planner")
        self.assertEqual([p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_update_is_read_modify_write_under_lock(self):
        store = self.rm.NoteStore("sess-1")
        store.update(lambda n: n.__setitem__("seat", "a.planner")); store.update(lambda n: n.__setitem__("root", "/r"))
        note = store.load(); self.assertEqual((note["seat"], note["root"]), ("a.planner", "/r"))

    def test_observer_reports_one_event_per_acquisition(self):
        store = self.rm.NoteStore("sess-1"); seen = []
        store.observer = lambda ev: seen.append(ev)
        with store.state_lock():
            pass
        self.assertEqual(seen, ["lock-attempt"])                                                     # uncontended: one event
        holder = self.rm.NoteStore("sess-1"); seen.clear()
        with holder.state_lock():
            with self.assertRaises(self.rm.LockTimeout):
                with store.state_lock(timeout=0.15):                                                  # contended: several 10 ms retries, still one event
                    pass
        self.assertEqual(seen, ["lock-attempt"])

    def test_state_lock_is_bounded(self):
        store = self.rm.NoteStore("sess-1"); other = self.rm.NoteStore("sess-1")
        with store.state_lock():
            t0 = time.monotonic()
            with self.assertRaises(self.rm.LockTimeout):
                with other.state_lock(timeout=0.2):
                    pass
            self.assertLess(time.monotonic() - t0, 1.0)

    def test_session_sources_in_order(self):
        rm = self.rm
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "c", "CODEX_THREAD_ID": "x"}):
            self.assertEqual(rm.resolve_session(ns(session="flag")), "flag"); self.assertEqual(rm.resolve_session(ns()), "c")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "x"}):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None); self.assertEqual(rm.resolve_session(ns()), "x")
        self.assertIsNone(rm.resolve_session(ns()))


class LeaderLockTests(TmpEnv):
    def test_second_acquire_blocks_probe_reports_held_and_instance_is_readable(self):
        a = self.rm.LeaderLock("s"); b = self.rm.LeaderLock("s")
        self.assertTrue(a.acquire(blocking=False)); a.stamp("inst-a")
        self.assertFalse(b.acquire(blocking=False)); self.assertEqual(b.holder_instance(), "inst-a")
        a.release(); self.assertTrue(b.acquire(blocking=False)); b.release()

    def test_leader_record_matches_live_process(self):
        rec = self.rm.leader_record()
        self.assertEqual(rec["pid"], os.getpid()); self.assertTrue(self.rm.process_alive(rec))
        self.assertFalse(self.rm.process_alive({**rec, "start_time": "1970-01-01 00:00:00"}))
        self.assertFalse(self.rm.process_alive({**rec, "pid": 2**22 - 1}))


class SinkTests(TmpEnv):
    def test_fd_sink_is_nonblocking_and_waits_bounded(self):
        r, w = os.pipe(); sink = self.rm.FdSink(w)
        total = 0
        while True:
            n = sink.write(b"x" * 65536)
            if n == 0: break
            total += n
        self.assertGreater(total, 0)
        t0 = time.monotonic(); self.assertFalse(sink.wait_writable(0.1)); self.assertLess(time.monotonic() - t0, 0.5)
        os.read(r, total); self.assertTrue(sink.wait_writable(0.5)); os.close(r); os.close(w)


class HostTableTests(TmpEnv):
    def decide(self, flag="auto", env=None, payload=None):
        return self.rm.decide_host(ns(host=flag), payload, env or {})

    def test_row1_flag_and_env_override_win(self):
        self.assertEqual(self.decide(flag="codex", env={"CLAUDE_PROJECT_DIR": "/p"}, payload={"prompt_id": "x"}), "codex")
        self.assertEqual(self.decide(env={"ADT_HOST": "claude-code", "PLUGIN_ROOT": "/x"}), "claude-code")

    def test_row2_environment_evidence(self):
        fresh = {"cwd": "/w", "hook_event_name": "SessionStart", "model": "m", "permission_mode": "default", "session_id": "s", "source": "startup", "transcript_path": None}
        self.assertEqual(self.decide(env={"PLUGIN_ROOT": "/x", "PLUGIN_DATA": "/d"}, payload=fresh), "codex")
        self.assertEqual(self.decide(env={"CLAUDE_PROJECT_DIR": "/p"}, payload=fresh), "claude-code")

    def test_row3_payload_evidence(self):
        self.assertEqual(self.decide(payload={"turn_id": "t", "session_id": "s"}), "codex")
        self.assertEqual(self.decide(payload={"prompt_id": "p"}), "claude-code")
        self.assertEqual(self.decide(payload={"effort": {"level": "high"}}), "claude-code")

    def test_ambiguity_and_unknown(self):
        self.assertEqual(self.decide(payload={"turn_id": "t", "prompt_id": "p"}), "host-ambiguous")
        self.assertEqual(self.decide(env={"PLUGIN_ROOT": "/x"}, payload={"prompt_id": "p"}), "host-ambiguous")
        fresh = {"cwd": "/w", "hook_event_name": "SessionStart", "model": "m", "permission_mode": "default", "session_id": "s", "source": "startup", "transcript_path": None}
        self.assertEqual(self.decide(payload=fresh), "host-unknown")
        self.assertEqual(self.decide(env={"CLAUDECODE": "1"}, payload={"hook_event_name": "SessionStart", "session_id": "s"}), "host-unknown")
        self.assertEqual(self.decide(env={"ADT_HOST": "codex", "CLAUDECODE": "1"}, payload=fresh), "codex")


class IndexReaderTests(TmpEnv):
    def test_missing_and_malformed_header(self):
        self.assertEqual(self.rm.read_index(self.index).error, "index-missing")
        self.index.write_text("| a | b |\n|---|---|\n")
        self.assertEqual(self.rm.read_index(self.index).error, "index-malformed")

    def test_rows_parse_with_positions_and_unescaping(self):
        write_index(self.index, row(1, to=r"x\|y.planner") + row(2))
        snap = self.rm.read_index(self.index)
        self.assertIsNone(snap.error); self.assertEqual([r.position for r in snap.rows], [1, 2])
        self.assertEqual(snap.rows[0].cells["to"], "x|y.planner"); self.assertIsNone(snap.rows[1].cells["cc"])
        self.assertEqual(self.rm.unescape_cell(r"a\\b"), r"a\b")

    def test_partial_trailing_row_is_ignored_and_flagged(self):
        self.index.write_text(HEADER + row(1) + "| 20260912-000002 | SITREP | Pair")
        snap = self.rm.read_index(self.index)
        self.assertEqual(len(snap.rows), 1); self.assertTrue(snap.partial_tail); self.assertIsNone(snap.error)

    def test_malformed_complete_row_and_duplicate_file_halt(self):
        write_index(self.index, row(1) + "| only | three | cells |\n" + row(3))
        snap = self.rm.read_index(self.index); self.assertEqual(snap.error, "index-malformed-row:2"); self.assertEqual(len(snap.rows), 1)
        write_index(self.index, row(1, file="lane/same.md") + row(2, file="lane/same.md") + row(3))
        snap = self.rm.read_index(self.index); self.assertEqual(snap.error, "duplicate-file-cell:2"); self.assertEqual(len(snap.rows), 1)

    def test_line_grammar(self):
        write_index(self.index, row(1, to="B.Implementer, c.planner"))
        line = self.rm.row_to_line(self.rm.read_index(self.index).rows[0], "docs/x/.relays/v1")
        self.assertTrue(line.endswith("\n")); obj = json.loads(line)
        self.assertEqual(list(obj), sorted(obj)); self.assertEqual(obj["root"], "docs/x/.relays/v1")
        self.assertIsNone(obj["cc"]); self.assertIsNone(obj["parent"]); self.assertEqual(obj["file"], "lane/r1.md")
        self.assertNotIn(": ", line)

    def test_seat_matching_and_locate(self):
        write_index(self.index, row(1, frm="A.Planner", to="B.Implementer, c.planner", cc="ORCH.orchestrator-reviewer") + row(2))
        snap = self.rm.read_index(self.index); r = snap.rows[0]
        self.assertTrue(self.rm.names_seat(r, "c.planner")); self.assertTrue(self.rm.names_seat(r, "orch.orchestrator-reviewer"))
        self.assertFalse(self.rm.names_seat(r, "a.planner")); self.assertTrue(self.rm.is_own(r, "a.planner"))
        self.assertEqual(self.rm.locate(snap, self.rm.SENTINEL), -1); self.assertEqual(self.rm.locate(snap, {"file": "lane/r2.md", "position": 2}), 1)
        self.assertIsNone(self.rm.locate(snap, {"file": "lane/gone.md", "position": 9}))


if __name__ == "__main__":
    unittest.main()
