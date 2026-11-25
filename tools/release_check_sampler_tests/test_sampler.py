"""Stage 1 sampler controls: the legacy result and signal trace stay independent."""

import contextlib
import importlib.util
import io
import json
import os
import re
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sampler_runner", REPO / "tools/release-check.py")
rc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rc
SPEC.loader.exec_module(rc)


def proc_stat(pid, tick, group, name=b"process ) name"):
    return b"%d (%s) " % (pid, name) + b" ".join(
        [b"S", b"1", str(group).encode()] + [b"0"] * 16 + [str(tick).encode()])


def independent_tick(pid):
    fields = re.fullmatch(rb"\d+ \(.*\) (.*)\n?", Path(f"/proc/{pid}/stat").read_bytes(), re.S)
    return int(fields[1].split()[19])


class SamplerTests(unittest.TestCase):
    def join(self, ticks, instances=()):
        self.assertTrue(hasattr(rc, "_name_leaked"), "No optional leak attribution on the base")
        return rc._name_leaked([42], {42: list(enumerate(ticks))}, dict(instances))[0]

    def test_unnamed_no_tick(self):
        self.assertEqual(self.join([None])["summary"], "unnamed")

    def test_unnamed_no_instance(self):
        self.assertEqual(self.join([17], [((42, 22), {"last_sampled_command": "different"})]),
                         {"pid": 42, "summary": "unnamed",
                          "observations": [{"start_tick": 17, "status": "unnamed"}]})

    def test_pid_joined_beside_unnamed(self):
        row = self.join([None], [((42, 17), {"last_sampled_command": "candidate"})])
        self.assertEqual(row["summary"], "unnamed")
        self.assertEqual(row["pid_joined_unconfirmed"]["last_sampled_command"], "candidate")
        self.assertEqual(row["observations"], [{"status": "no-key"}])

    def test_key_change_during_capture(self):
        self.assertTrue(hasattr(rc, "_Sampler"), "No coherent sampler on the base")
        sampler = rc._Sampler()
        sampler.activate("step", b"witness", 42)
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp, "42")
            proc.mkdir()
            (proc / "environ").write_bytes(b"ADT_RELEASE_STEP=witness\0")
            (proc / "cmdline").write_bytes(b"replacement\0")
            (proc / "stat").write_bytes(proc_stat(42, 17, 42))
            original = Path.open

            def replace_on_command(path, *args, **kwargs):
                if path.name == "cmdline":
                    (proc / "stat").write_bytes(proc_stat(42, 22, 42))
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "open", replace_on_command):
                sampler._scan(Path(tmp))
        self.assertEqual(self.join([17], sampler.snapshot(0.5)["instances"].items())["summary"],
                         "unnamed")

    def test_key_only_never_names(self):
        row = self.join([17], [((42, 17), {"last_sampled_command": None})])
        self.assertEqual(row["summary"], "unnamed")
        self.assertEqual(row["observations"], [{"start_tick": 17, "status": "unnamed"}])

    def test_two_ticks_one_pid_ambiguous(self):
        row = self.join([17, 22], [((42, 17), {"last_sampled_command": "earlier"})])
        self.assertEqual(row["summary"], "ambiguous")
        self.assertEqual([(item["start_tick"], item["status"]) for item in row["observations"]],
                         [(17, "named"), (22, "unnamed")])
        self.assertNotIn("pid_joined_unconfirmed", row)
        self.assertNotIn("last_sampled_command", row)

    def test_tick_and_no_key_ambiguous(self):
        row = self.join([17, None], [((42, 17), {"last_sampled_command": "earlier"})])
        self.assertEqual(row["summary"], "ambiguous")
        self.assertEqual([item["status"] for item in row["observations"]], ["named", "no-key"])

    def test_excluded_leader_not_named(self):
        self.assertTrue(hasattr(rc, "_name_leaked"), "No finalized leak join on the base")
        row = {"id": "step"}
        sweep = rc._Sweep(b"m", 11, row, exclude=11, seen={11, 12})
        sweep.finish()
        got = rc._name_leaked(row["leaked"], {11: [(1, 17)], 12: [(1, 18)]},
                             {(11, 17): {"last_sampled_command": "leader"}})
        self.assertEqual([(entry["pid"], entry["summary"]) for entry in got], [(12, "unnamed")])

    def test_instance_cap(self):
        self.assertTrue(hasattr(rc, "_Sampler"), "No bounded instance store on the base")
        sampler = rc._Sampler()
        sampler.activate("step", b"marker", 42)
        for tick in range(4097):
            sampler._keep({(42, tick): {"last_sampled_command": "sampled"}})
        snapshot = sampler.snapshot(0.5)
        self.assertEqual(len(snapshot["instances"]), 4096)
        self.assertTrue(snapshot["metrics"]["instance-cap"])
        self.assertEqual(self.join([4095], snapshot["instances"].items())["summary"], "named")
        self.assertEqual(self.join([4096], snapshot["instances"].items())["summary"], "unnamed")

    def test_tick_index(self):
        got = {}
        rc._listing(got.__setitem__)
        self.assertEqual(got[os.getpid()], independent_tick(os.getpid()))

    def test_named_real_child(self):
        self.assertTrue(hasattr(rc, "_Sampler"), "No live sampler on the base")
        self._real_child_result()

    def _real_child_result(self, extra_env=None):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp, "ready")
            script = (
                "import subprocess,sys,time;from pathlib import Path;"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',"
                "'recorder-live-command'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "b=Path(f'/proc/{p.pid}/stat').read_bytes();"
                "Path(sys.argv[1]).write_text(str(p.pid)+' '+"
                "str(int(b[b.rindex(b')')+2:].split(None,20)[19])));"
                "time.sleep(0.45)"
            )
            step = rc.Step("live", [sys.executable, "-c", script, str(ready)], rc.BOTH, ())
            try:
                result = rc.run_leg("linux", [step], 1, 8, tmp, exclusions=(),
                                    environ={**os.environ, **(extra_env or {})})
                payload = result.results_path.read_text()
                pid, expected_tick = map(int, ready.read_text().split())
                entry = next(row for row in json.loads(payload)["steps"][0]
                             ["leaked_named"] if row["pid"] == pid)
                self.assertEqual(result.exit, 0)
                self.assertEqual(entry["summary"], "named")
                observation, = entry["observations"]
                self.assertEqual(observation["start_tick"], expected_tick)
                self.assertIn("recorder-live-command", observation["instance"]["last_sampled_command"])
                return payload
            finally:
                if ready.exists():
                    pid, tick = map(int, ready.read_text().split())
                    with contextlib.suppress(ProcessLookupError, FileNotFoundError):
                        if independent_tick(pid) == tick:
                            os.kill(pid, signal.SIGKILL)

    def test_no_environment_kept(self):
        self.assertTrue(hasattr(rc, "_Sampler"), "No optional capture on the base")
        value = "SAMPLER-WITNESS-SYNTHETIC-SECRET"
        self.assertNotIn(value, self._real_child_result({"SAMPLE_TEST_SECRET": value}))

    def scripted_leg(self, leaky, mode, *, stop=False, fault=None):
        self.assertTrue(hasattr(rc, "_Sampler"), "No sampler modes on the base")
        rc.STOP.clear()
        self.addCleanup(rc.STOP.clear)
        step = rc.Step("step", ["scripted"], rc.BOTH, ())
        child = mock.Mock(pid=410)
        child.poll.side_effect = ([None, None, 0] if stop else [0])
        child.returncode = 0
        listed = iter([[(411, 410, frozenset())], []] if leaky else [[]])
        signals = []

        def listing(collector=None):
            rows = next(listed, [])
            if collector:
                for pid, _, _ in rows:
                    collector(pid, 43)
            return rows

        def start(sampler):
            if fault == "start":
                raise RuntimeError("start")
            if mode == "raised":
                with mock.patch.object(sampler, "_scan", side_effect=RuntimeError("scan")):
                    sampler._run()

        def send(pid, sig, group=False):
            signals.append((pid, sig, group))

        def schedule(*args, **kwargs):
            original(*args, **kwargs)
            if stop:
                rc.STOP.set()

        original = rc._schedule
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(rc, "_listing", listing))
            stack.enter_context(mock.patch.object(rc, "_signal", send))
            stack.enter_context(mock.patch.object(rc, "_commit", return_value="candidate"))
            stack.enter_context(mock.patch.object(rc, "_unattributed", return_value=[]))
            stack.enter_context(mock.patch.object(rc, "_now", return_value="fixed"))
            stack.enter_context(mock.patch.object(rc, "_schedule", schedule))
            stack.enter_context(mock.patch.object(rc._Sampler, "start", start))
            stack.enter_context(mock.patch.object(rc.platform, "platform", return_value="fixture"))
            stack.enter_context(mock.patch.object(rc.subprocess, "Popen", return_value=child))
            stack.enter_context(mock.patch.object(rc.os, "getloadavg", return_value=(0, 0, 0)))
            if mode == "off":
                stack.enter_context(mock.patch.object(rc, "_new_sampler", return_value=None))
            if stop:
                # Next scheduler pass reaches the cap; cleanup is still the real _stop_all.
                stack.enter_context(mock.patch.object(rc.time, "monotonic",
                                                      side_effect=[0, 0, 0, 2] + [2] * 100))
            else:
                stack.enter_context(mock.patch.object(rc.time, "monotonic", return_value=0))
            if fault == "snapshot":
                stack.enter_context(mock.patch.object(rc._Sampler, "snapshot", return_value=None))
            if fault == "serialize":
                stack.enter_context(mock.patch.object(rc, "_name_leaked", side_effect=TypeError("name")))
            if fault == "history":
                stack.enter_context(mock.patch.object(rc._Sampler, "observe", side_effect=ValueError("history")))
            stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            result = rc.run_leg("linux", [step], 1, 1, tmp, exclusions=())
            data = json.loads(result.results_path.read_bytes())
        out = data.pop("sampler", None)
        for row in data["steps"]:
            row.pop("leaked_named", None)
            # Each test leg owns a different temp output path.
            row["log"] = Path(row["log"]).name
            row["wall_seconds"] = "<measured>"
        strict_stop = any(row["leaked"] for row in data["steps"])
        return (result.exit, json.dumps(data), stderr.getvalue(), signals, strict_stop), out

    def test_sampler_failures_non_gating(self):
        baseline, _ = self.scripted_leg(True, "off")
        for point in ("start", "scan", "snapshot", "serialize"):
            with self.subTest(point=point):
                got, sampler = self.scripted_leg(True, "raised" if point == "scan" else "on",
                                                 fault=point)
                self.assertEqual(got, baseline)
                self.assertIn(point, {entry["point"] for entry in sampler["sampler-incomplete"]})

    def test_recording_only_byte_equal(self):
        for leaky in (False, True):
            for stopping in (False, True):
                with self.subTest(leaky=leaky, stopping=stopping):
                    baseline, _ = self.scripted_leg(leaky, "off", stop=stopping)
                    on, _ = self.scripted_leg(leaky, "on", stop=stopping)
                    raised, _ = self.scripted_leg(leaky, "raised", stop=stopping)
                    self.assertEqual(on, baseline)
                    self.assertEqual(raised, baseline)
                    self.assertEqual(baseline[0], 1 if stopping else 0)
                    self.assertEqual(baseline[-1], leaky)
                    if stopping:
                        self.assertIn((410, signal.SIGKILL, True), baseline[3])
                    if leaky:
                        self.assertIn((411, signal.SIGTERM, False), baseline[3])

    def test_history_fault_on_stop_path(self):
        baseline, _ = self.scripted_leg(True, "off", stop=True)
        got, sampler = self.scripted_leg(True, "on", stop=True, fault="history")
        self.assertEqual(got, baseline)
        self.assertIn("history", {entry["point"] for entry in sampler["sampler-incomplete"]})
