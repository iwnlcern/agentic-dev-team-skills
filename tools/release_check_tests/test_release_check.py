"""Tests for tools/release-check.py (DD-v3-tests § 3.2, PL-v3-tests-1 Task 1)."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "release-check.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rc = load_runner()

SCRATCH_BASE = Path.home() / ".cache" / "adt-release-check-tests"

# A child that forks, calls setsid and forks again, as daemon.launch does
# (tools/relay_engine/daemon.py:1150-1159); the grandchild writes its pid and sleeps.
DAEMONIZE = textwrap.dedent('''
    import os, time
    def daemonize(pidfile):
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                with open(pidfile, "w") as handle:
                    handle.write(str(os.getpid()))
                time.sleep(60)
                os._exit(0)
            os._exit(0)
        while not os.path.exists(pidfile) or not open(pidfile).read():
            time.sleep(0.02)
''')


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ask ps for its state.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_gone(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


class FixtureRepo:
    """A throwaway repository tree under $HOME with fixture test modules."""

    def __init__(self, test_case):
        SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="repo-", dir=SCRATCH_BASE))
        test_case.addCleanup(shutil.rmtree, self.root, True)
        self.tests = self.root / "tools" / "relay_engine" / "tests"
        self.tests.mkdir(parents=True)
        (self.root / "tools" / "relay_engine" / "__init__.py").write_text("")
        (self.tests / "__init__.py").write_text("")
        self.out = self.root.parent / (self.root.name + "-out")
        test_case.addCleanup(shutil.rmtree, self.out, True)
        self.pids = self.root.parent / (self.root.name + "-pids")
        self.pids.mkdir()
        test_case.addCleanup(shutil.rmtree, self.pids, True)

    def module(self, name, body):
        source = "import os, sys, time, unittest\n" + DAEMONIZE + textwrap.dedent(body)
        (self.tests / f"{name}.py").write_text(source)

    def passing(self, name):
        self.module(name, '''
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
        ''')

    def tool(self, relative, source):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
        return path

    def commit(self):
        git = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "fixture"], check=True)
        return subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def run(self, *args, environ=None, exclusions=()):
        argv = ["full", "--leg", "mac", "--out", str(self.out)] + list(args)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rc.main(argv, repo=self.root,
                           environ=dict(os.environ if environ is None else environ),
                           exclusions=tuple(exclusions))
        return code, err.getvalue()

    def results(self):
        return json.loads((self.out / "mac" / "results.json").read_text())

    def step(self, step_id):
        return next(s for s in self.results()["steps"] if s["id"] == step_id)


class CapAndDescendants(unittest.TestCase):

    def test_cap_marks_running_and_pending_unfinished(self):
        repo = FixtureRepo(self)
        child_pid = repo.pids / "grandchild"
        self_pid = repo.pids / "module"
        repo.module("test_a_slow", f'''
            class T(unittest.TestCase):
                def test_slow(self):
                    open({str(self_pid)!r}, "w").write(str(os.getpid()))
                    daemonize({str(child_pid)!r})
                    time.sleep(60)
        ''')
        repo.passing("test_b_pending")
        code, err = repo.run("--workers", "1", "--cap-seconds", "3")
        self.assertEqual(code, 1, err)
        self.assertEqual(repo.step("relay_engine.tests.test_a_slow")["outcome"], "unfinished")
        self.assertEqual(repo.step("relay_engine.tests.test_b_pending")["outcome"], "unfinished")
        self.assertTrue(wait_gone(int(self_pid.read_text())), "module process survived")
        self.assertTrue(wait_gone(int(child_pid.read_text())), "setsid grandchild survived")

    def test_cap_control_passes_when_steps_finish(self):
        repo = FixtureRepo(self)
        repo.module("test_a_quick", '''
            class T(unittest.TestCase):
                def test_quick(self):
                    time.sleep(1)
        ''')
        code, err = repo.run("--workers", "1", "--cap-seconds", "60")
        self.assertEqual(code, 0, err)
        step = repo.step("relay_engine.tests.test_a_quick")
        self.assertEqual(step["outcome"], "pass")
        self.assertEqual(step["leaked"], [])

    def test_leaked_daemon_is_stopped_and_reported(self):
        repo = FixtureRepo(self)
        child_pid = repo.pids / "grandchild"
        repo.module("test_a_leaks", f'''
            class T(unittest.TestCase):
                def test_leak(self):
                    daemonize({str(child_pid)!r})
        ''')
        code, err = repo.run("--workers", "1")
        self.assertEqual(code, 0, err)
        step = repo.step("relay_engine.tests.test_a_leaks")
        pid = int(child_pid.read_text())
        self.assertEqual(step["outcome"], "pass")
        self.assertIn(pid, step["leaked"])
        self.assertTrue(wait_gone(pid), "leaked daemon was not stopped")

    def test_reaped_child_is_not_reported_as_leaked(self):
        repo = FixtureRepo(self)
        repo.module("test_a_reaps", '''
            class T(unittest.TestCase):
                def test_reap(self):
                    pid = os.fork()
                    if pid == 0:
                        os._exit(0)
                    os.waitpid(pid, 0)
        ''')
        code, err = repo.run("--workers", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(repo.step("relay_engine.tests.test_a_reaps")["leaked"], [])


class ValidatorPin(unittest.TestCase):

    def test_validator_pinned_to_real_path(self):
        repo = FixtureRepo(self)
        bins = repo.pids / "bin"
        path_dir = repo.pids / "path"
        bins.mkdir()
        path_dir.mkdir()
        for name, text in (("claude-a", "A 1.0"), ("claude-b", "B 2.0")):
            binary = bins / name
            binary.write_text(f"#!/bin/sh\necho '{text}'\n")
            binary.chmod(0o755)
        link = path_dir / "claude"
        link.symlink_to(bins / "claude-a")
        seen = repo.pids / "seen"
        # Runs first (workers 1, engine modules before checks): retarget the link mid-leg.
        repo.module("test_a_retarget", f'''
            class T(unittest.TestCase):
                def test_retarget(self):
                    tmp = {str(path_dir / "claude.new")!r}
                    os.symlink({str(bins / "claude-b")!r}, tmp)
                    os.replace(tmp, {str(link)!r})
        ''')
        repo.tool("tools/check-generate-plugins.py", f'''
            import os
            open({str(seen)!r}, "w").write(os.environ.get("CLAUDE_BIN", ""))
        ''')
        environ = {k: v for k, v in os.environ.items() if k != "CLAUDE_BIN"}
        environ["PATH"] = f"{path_dir}:/usr/bin:/bin"
        code, err = repo.run("--workers", "1", environ=environ)
        self.assertEqual(code, 0, err)
        real_a = os.path.realpath(bins / "claude-a")
        self.assertEqual(seen.read_text(), real_a)
        self.assertEqual(os.path.realpath(link), os.path.realpath(bins / "claude-b"))
        results = repo.results()
        self.assertEqual(results["claude_bin"], real_a)
        self.assertEqual(results["claude_version"], "A 1.0")


class StartRefusals(unittest.TestCase):

    def test_release_mode_refuses_nonempty_exclusions(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.tool("tools/check-old.py", "pass\n")
        exclusion = rc.Exclusion("tools/check-old.py", "R250.5: called by nothing; removed at DD stage 4")
        code, err = repo.run("--release", exclusions=[exclusion])
        self.assertEqual(code, 2)
        self.assertIn("tools/check-old.py", err)
        self.assertFalse((repo.out / "mac" / "results.json").exists())

    def test_release_mode_starts_with_empty_exclusions(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run("--release")
        self.assertEqual(code, 0, err)

    def test_uncovered_module_refuses_to_start(self):
        repo = FixtureRepo(self)
        ran = repo.pids / "ran"
        repo.module("test_a_marks", f'''
            class T(unittest.TestCase):
                def test_mark(self):
                    open({str(ran)!r}, "w").write("ran")
        ''')
        repo.tool("tools/check-new.py", "pass\n")
        code, err = repo.run()
        self.assertEqual(code, 2)
        self.assertIn("tools/check-new.py", err)
        self.assertFalse(ran.exists(), "a step ran before the refusal")

    def test_stale_exclusion_refuses_to_start(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        exclusion = rc.Exclusion("tools/check-gone.py", "R250.5: removed at DD stage 4")
        code, err = repo.run(exclusions=[exclusion])
        self.assertEqual(code, 2)
        self.assertIn("tools/check-gone.py", err)

    def test_out_dir_outside_home_refused(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        outside = ["/tmp/adt-release-check-x", "/private/tmp/adt-release-check-x",
                   os.path.join(os.path.realpath(tempfile.gettempdir()), "adt-release-check-x")]
        self.assertTrue(any(p.startswith("/var/folders") or p.startswith("/private/var/folders")
                            for p in outside) or sys.platform != "darwin")
        for path in outside:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = rc.main(["full", "--leg", "mac", "--out", path], repo=repo.root,
                               environ=dict(os.environ), exclusions=())
            self.assertEqual(code, 2, path)
            self.assertFalse(os.path.exists(path), path)

    def test_out_dir_under_home_accepted(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run()
        self.assertEqual(code, 0, err)


class StepsAndResults(unittest.TestCase):

    def test_failure_exits_one(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.module("test_b_fails", '''
            class T(unittest.TestCase):
                def test_fail(self):
                    self.fail("fixture failure")
        ''')
        code, err = repo.run()
        self.assertEqual(code, 1, err)
        self.assertEqual(repo.step("relay_engine.tests.test_b_fails")["outcome"], "fail")
        self.assertEqual(repo.step("relay_engine.tests.test_a_ok")["outcome"], "pass")

    def test_all_passing_exits_zero(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.passing("test_b_ok")
        code, err = repo.run()
        self.assertEqual(code, 0, err)

    def test_child_environment(self):
        repo = FixtureRepo(self)
        for name in ("test_a_env", "test_b_env"):
            record = repo.pids / name
            repo.module(name, f'''
                import json
                class T(unittest.TestCase):
                    def test_env(self):
                        data = sys.stdin.read()
                        json.dump({{"cwd": os.getcwd(), "stdin": data,
                                   "env": dict(os.environ)}}, open({str(record)!r}, "w"))
            ''')
        code, err = repo.run("--workers", "2")
        self.assertEqual(code, 0, err)
        seen = [json.loads((repo.pids / n).read_text()) for n in ("test_a_env", "test_b_env")]
        for record in seen:
            self.assertEqual(os.path.realpath(record["cwd"]), os.path.realpath(repo.root))
            self.assertEqual(record["stdin"], "")
            self.assertEqual(record["env"].get("PYTHONDONTWRITEBYTECODE"), "1")
            self.assertEqual(record["env"].get("PYTHONWARNINGS"), "error::ResourceWarning")
            self.assertEqual(record["env"].get("TMPDIR"), os.environ.get("TMPDIR"))
            self.assertTrue(record["env"].get("ADT_RELEASE_STEP"))
        for key in ("RELAY_ENGINE_RESULTS_ROOT", "RELAY_ENGINE_MATRIX_RUN"):
            self.assertTrue(seen[0]["env"].get(key), key)
            self.assertEqual(seen[0]["env"][key], seen[1]["env"][key], key)
        self.assertNotEqual(seen[0]["env"]["ADT_RELEASE_STEP"], seen[1]["env"]["ADT_RELEASE_STEP"])

    def test_results_file_fields(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.tool("tools/check-old.py", "pass\n")
        commit = repo.commit()
        exclusion = rc.Exclusion("tools/check-old.py", "R250.5: called by nothing; removed at DD stage 4")
        code, err = repo.run(exclusions=[exclusion])
        self.assertEqual(code, 0, err)
        results = repo.results()
        for field in ("leg", "commit", "os", "python", "workers", "load", "claude_bin",
                      "claude_version", "cap_seconds", "started", "finished", "exclusions",
                      "steps", "exit"):
            self.assertIn(field, results)
        self.assertEqual(results["leg"], "mac")
        self.assertEqual(results["commit"], commit)
        self.assertEqual(set(results["python"]), {"path", "version"})
        self.assertEqual(set(results["load"]), {"start", "end"})
        self.assertEqual(results["exit"], 0)
        self.assertEqual(results["exclusions"],
                         [{"path": "tools/check-old.py", "reason": exclusion.reason}])
        for step in results["steps"]:
            self.assertEqual(set(step), {"id", "argv", "outcome", "exit_code", "wall_seconds",
                                         "log", "leaked"})
            self.assertIsInstance(step["wall_seconds"], (int, float))
            self.assertTrue(Path(step["log"]).is_file())


class RealTree(unittest.TestCase):

    def test_real_tree_is_covered(self):
        self.assertEqual(rc.uncovered(REPO, rc.step_table(REPO), rc.EXCLUSIONS), [])


if __name__ == "__main__":
    unittest.main()
