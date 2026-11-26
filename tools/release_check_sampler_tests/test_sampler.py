"""Stage 1 sampler controls: the legacy result and signal trace stay independent."""

import contextlib
import importlib.util
import io
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from math import ceil, floor
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


def proc_field_tick(pid):
    # Literal field comparison for the live join and its PID-reuse cleanup guard.
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
        uptime = Path("/proc/uptime")
        hz = os.sysconf("SC_CLK_TCK")
        cmd = [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)"]
        before = Decimal(uptime.read_text().split()[0])
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            try:
                after = Decimal(uptime.read_text().split()[0])
                # /proc/uptime truncates to hundredths; include the final partial tick.
                lo = floor(before * hz)
                hi = ceil((after + Decimal("0.01")) * hz)
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "child not ready")
                self.assertEqual(child.stdout.readline(), b"ready\n")
                proc = Path("/proc", str(child.pid))
                # Field 21 (itrealvalue) is zero on Linux. Field 23 (vsize) is
                # the virtual page count from statm, expressed in bytes.
                vsize = int((proc / "statm").read_text().split()[0]) * os.sysconf("SC_PAGE_SIZE")
                self.assertGreater(lo, 0)
                self.assertFalse(lo <= vsize <= hi, f"vsize {vsize} overlaps {lo}..{hi}")
                print(f"tick oracle: hz={hz} window={lo}..{hi} width_ticks={hi-lo} "
                      f"itrealvalue=0 vsize_bytes={vsize}", file=sys.stderr)
                got = {}
                rc._listing(got.__setitem__)
                for seam, tick in (("listing", got[child.pid]), ("sampler", rc._sample_stat(proc)[1])):
                    with self.subTest(seam=seam):
                        self.assertTrue(lo <= tick <= hi, f"start tick {tick} outside {lo}..{hi}")
            finally:
                # EOF releases this test's live child, including on an assertion failure.
                child.stdin.close()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

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
                        if proc_field_tick(pid) == tick:
                            os.kill(pid, signal.SIGKILL)

    def test_no_environment_kept(self):
        self.assertTrue(hasattr(rc, "_Sampler"), "No optional capture on the base")
        value = "SAMPLER-WITNESS-SYNTHETIC-SECRET"
        self.assertNotIn(value, self._real_child_result({"SAMPLE_TEST_SECRET": value}))

    def scripted_leg(self, leaky, mode, *, stop=False, fault=None, out=None, at_write=None):
        self.assertTrue(hasattr(rc, "_Sampler"), "No sampler modes on the base")
        rc.STOP.clear()
        self.addCleanup(rc.STOP.clear)
        step = rc.Step("step", ["scripted"], rc.BOTH, ())
        child = mock.Mock(pid=410)
        child.poll.side_effect = ([None, None, 0] if stop else [0])
        child.returncode = 0
        leak = [(411, 410, frozenset())]
        listed = iter([leak, leak, []] if fault == "history-once" else [leak, []] if leaky else [[]])
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
        with contextlib.ExitStack() as stack:
            tmp = out if out is not None else stack.enter_context(tempfile.TemporaryDirectory())
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
            if fault == "history-once":
                observe = rc._Sampler.observe
                seen = False

                def observe_after_fault(sampler, step_id, pids, ticks):
                    nonlocal seen
                    if pids and not seen:
                        seen = True
                        raise ValueError("history")
                    observe(sampler, step_id, pids, ticks)

                stack.enter_context(mock.patch.object(rc._Sampler, "observe", observe_after_fault))
                stack.enter_context(mock.patch.object(rc._Sampler, "snapshot", return_value={
                    "instances": {(411, 43): {"last_sampled_command": "same key"}},
                    "metrics": {},
                }))
            stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            if at_write is not None:
                at_write(stack)
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

    def publication_faults(self, fault, events, *, legacy_error=None):
        dumps, write, replace, write_text = json.dumps, Path.write_bytes, os.replace, Path.write_text
        tmp_name = "results.json.sampler-tmp"

        def encode(value, *args, **kwargs):
            if isinstance(value, dict) and "steps" in value:
                if "sampler" in value:
                    events.append("augmented-encode")
                    if fault == "encode":
                        raise TypeError("augmented encoding")
                else:
                    events.append("legacy-encode")
            text = dumps(value, *args, **kwargs)
            if isinstance(value, dict) and "sampler" in value and "steps" in value:
                events.append((text + "\n").encode("utf-8"))
            return text

        def stage(path, data):
            if path.name == tmp_name:
                events.append("stage-write")
                if fault == "write":
                    with path.open("wb") as stream:
                        stream.write(data[:12])
                    self.assertEqual(path.read_bytes(), data[:12])
                    raise OSError("after partial write")
            return write(path, data)

        def publish(src, dst):
            if Path(src).name == tmp_name:
                self.assertEqual(Path(dst).name, "results.json")
                events.append("replace")
                if fault == "replace":
                    self.assertGreater(Path(src).stat().st_size, 0)
                    raise OSError("replace")
            return replace(src, dst)

        def required(path, data, *args, **kwargs):
            if path.name == "results.json" and "sampler" not in json.loads(data):
                events.append("legacy-write")
                if legacy_error is not None:
                    raise legacy_error
            return write_text(path, data, *args, **kwargs)

        def install(stack):
            stack.enter_context(mock.patch.object(json, "dumps", encode))
            stack.enter_context(mock.patch.object(Path, "write_bytes", stage))
            stack.enter_context(mock.patch.object(os, "replace", publish))
            stack.enter_context(mock.patch.object(Path, "write_text", required))

        return install

    def assert_legacy_fallback(self, fault, operations, warning):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp, "linux", "results.json")
            baseline, _ = self.scripted_leg(True, "off", out=tmp)
            legacy = results.read_bytes()
            results.unlink()
            events = []
            got, sampler = self.scripted_leg(True, "on", out=tmp,
                                              at_write=self.publication_faults(fault, events))
            self.assertEqual([event for event in events if isinstance(event, str)], operations)
            self.assertEqual(results.read_bytes(), legacy)
            self.assertEqual(got[2], warning + baseline[2])
            self.assertEqual(got[:2] + got[3:], baseline[:2] + baseline[3:])
            self.assertIsNone(sampler)
            self.assertFalse(results.with_name("results.json.sampler-tmp").exists())

    def test_publication_fallback(self):
        for fault, operations in (
            ("write", ["legacy-encode", "augmented-encode", "stage-write", "legacy-write"]),
            ("replace", ["legacy-encode", "augmented-encode", "stage-write", "replace", "legacy-write"]),
        ):
            with self.subTest(fault=fault):
                self.assert_legacy_fallback(fault, operations,
                                            "release-check: linux: sampler output not published (OSError)\n")
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            got, sampler = self.scripted_leg(True, "on", out=tmp,
                                              at_write=self.publication_faults(None, events))
            self.assertEqual([event for event in events if isinstance(event, str)],
                             ["legacy-encode", "augmented-encode", "stage-write", "replace"])
            expected, = (event for event in events if isinstance(event, bytes))
            results = Path(tmp, "linux", "results.json")
            self.assertEqual(results.read_bytes(), expected)
            self.assertFalse(results.with_name("results.json.sampler-tmp").exists())
            self.assertEqual(got[0], 0)
            self.assertIsNotNone(sampler)

    def test_publication_encode_fault(self):
        self.assert_legacy_fallback("encode", ["legacy-encode", "augmented-encode", "legacy-write"],
                                    "release-check: linux: sampler output not published (TypeError)\n")

    def test_legacy_write_failure_propagates(self):
        events = []
        legacy_error = PermissionError("required legacy write")
        original_fault = rc._Sampler.fault
        recorded_kinds = []

        def note(sampler, point, kind, **kwargs):
            recorded_kinds.append(kind)
            return original_fault(sampler, point, kind, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rc._Sampler, "fault", note):
            with self.assertRaises(PermissionError) as caught:
                self.scripted_leg(True, "on", out=tmp,
                                  at_write=self.publication_faults("replace", events, legacy_error=legacy_error))
            self.assertIs(caught.exception, legacy_error)
            self.assertEqual(recorded_kinds, ["OSError"])
            self.assertFalse(Path(tmp, "linux", "results.json").exists())
        self.assertEqual([event for event in events if isinstance(event, str)],
                         ["legacy-encode", "augmented-encode", "stage-write", "replace", "legacy-write"])

    def test_history_fault_withholds_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sampler = self.scripted_leg(True, "on", out=tmp, fault="history-once")
            self.assertTrue(any(row["point"] == "history" and row["steps"] == ["step"]
                                for row in sampler["sampler-incomplete"]))
            data = json.loads(Path(tmp, "linux", "results.json").read_bytes())
            step, = data["steps"]
            self.assertEqual(step["leaked"], [411])
            entry, = step["leaked_named"]
            self.assertEqual((entry["summary"], entry.get("reason")), ("unnamed", "history-incomplete"))
            self.assertEqual(entry["observations"], [{
                "start_tick": 43, "status": "unnamed", "reason": "history-incomplete",
            }])
