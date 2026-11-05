"""Tests for tools/release-check.py (DD-v3-tests § 3.2, PL-v3-tests-1 Task 1)."""

import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

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


# Starts argv in a new session with its working directory at cwd and stdio on /dev/null,
# leaving it behind when the step exits (its parent becomes init).
DETACH = textwrap.dedent('''
    def detach(argv, cwd, pidfile):
        pid = os.fork()
        if pid == 0:
            os.setsid()
            os.chdir(cwd)
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            os.execv(argv[0], argv)
        with open(pidfile, "w") as handle:
            handle.write(str(pid))
''')


def kill_group(pgid):
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)


def kill_pidfile(path):
    try:
        os.kill(int(Path(path).read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


class Sweep(unittest.TestCase):

    def test_platform_binary_orphan_is_reported_not_killed(self):
        repo = FixtureRepo(self)
        shell, python, rooted = (repo.pids / n for n in ("shell", "python", "rooted"))
        for path in (shell, python, rooted):
            self.addCleanup(kill_pidfile, path)
        repo.module("test_a_detaches", DETACH + textwrap.dedent(f'''
            class T(unittest.TestCase):
                def test_detach(self):
                    detach(["/bin/sh", "-c", "while :; do sleep 1; done"], os.getcwd(), {str(shell)!r})
                    detach([sys.executable, "-c", "import time; time.sleep(60)"], os.getcwd(),
                           {str(python)!r})
                    detach(["/bin/sleep", "60"], "/", {str(rooted)!r})
        '''))
        code, err = repo.run("--workers", "1")
        self.assertEqual(code, 0, err)
        results = repo.results()
        listed = {entry["pid"]: entry for entry in results["unattributed"]}
        shell_pid, python_pid, rooted_pid = (int(p.read_text()) for p in (shell, python, rooted))
        self.assertIn(shell_pid, listed, results["unattributed"])
        self.assertEqual(os.path.realpath(listed[shell_pid]["cwd"]), os.path.realpath(repo.root))
        self.assertTrue(alive(shell_pid), "an unattributed process was killed")
        step = repo.step("relay_engine.tests.test_a_detaches")
        self.assertIn(python_pid, step["leaked"])
        self.assertTrue(wait_gone(python_pid), "the attributable Python child survived")
        self.assertNotIn(rooted_pid, listed, "a process outside the repository was listed")
        self.assertNotIn(rooted_pid, step["leaked"])

    def test_non_utf8_environment_does_not_crash(self):
        bystander = subprocess.Popen(
            [os.fsencode(sys.executable), b"-c", b"import time; time.sleep(60)", b"caf\xe9"],
            env={**os.environb, b"X_BYTES": b"caf\xe9"}, stdin=subprocess.DEVNULL)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run("--workers", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(repo.step("relay_engine.tests.test_a_ok")["outcome"], "pass")

    def test_internal_error_still_stops_and_writes(self):
        repo = FixtureRepo(self)
        slow_pid = repo.pids / "slow"
        repo.module("test_a_quick", '''
            class T(unittest.TestCase):
                def test_quick(self):
                    time.sleep(1)
        ''')
        repo.module("test_b_slow", f'''
            class T(unittest.TestCase):
                def test_slow(self):
                    open({str(slow_pid)!r}, "w").write(str(os.getpid()))
                    time.sleep(60)
        ''')
        with mock.patch.object(rc, "_listing", side_effect=RuntimeError("injected sweep failure")):
            code, err = repo.run("--workers", "2")
        self.assertEqual(code, 2, err)
        results = repo.results()
        self.assertIn("injected sweep failure", results["error"])
        self.assertEqual(results["exit"], 2)
        self.assertEqual(repo.step("relay_engine.tests.test_b_slow")["outcome"], "unfinished")
        self.assertTrue(wait_gone(int(slow_pid.read_text())), "a running step survived the error")

    def test_forking_leftover_is_fully_stopped(self):
        repo = FixtureRepo(self)
        forked = repo.pids / "forked"
        forked.mkdir()

        def kill_forked():
            for entry in forked.iterdir():
                with contextlib.suppress(OSError, ValueError):
                    os.kill(int(entry.name), signal.SIGKILL)
        self.addCleanup(kill_forked)
        # A leftover that ignores SIGTERM and forks every 20 ms, so children appear between
        # any listing of the step's processes and the kill that follows it.
        repo.module("test_a_forks", f'''
            import signal
            class T(unittest.TestCase):
                def test_fork(self):
                    if os.fork() == 0:
                        os.setsid()
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                        while True:
                            if os.fork() == 0:
                                open(os.path.join({str(forked)!r}, str(os.getpid())), "w").close()
                                time.sleep(30)
                                os._exit(0)
                            while True:
                                try:
                                    if os.waitpid(-1, os.WNOHANG)[0] == 0:
                                        break
                                except ChildProcessError:
                                    break
                            time.sleep(0.02)
                    while len(os.listdir({str(forked)!r})) < 10:
                        time.sleep(0.02)
        ''')
        code, err = repo.run("--workers", "1")
        self.assertEqual(code, 0, err)
        time.sleep(1)
        survivors = [int(e.name) for e in forked.iterdir() if alive(int(e.name))]
        self.assertEqual(survivors, [], f"{len(survivors)} of {len(list(forked.iterdir()))} forked children survived")


DRIVER = textwrap.dedent('''
    import importlib.util, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("release_check", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.exit(module.main(sys.argv[3:], repo=Path(sys.argv[2]), exclusions=(), history=()))
''')


def start_driver(repo, *argv, env=None):
    """The runner in its own process (so a test can signal it), on a fixture repository."""
    driver = repo.pids / "driver.py"
    driver.write_text(DRIVER)
    return subprocess.Popen([sys.executable, str(driver), str(SCRIPT), str(repo.repo), *argv],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env)


def stop_driver(driver):
    if driver.poll() is None:
        driver.kill()
    if driver.stdout and not driver.stdout.closed:
        driver.communicate()


IGNORES_SIGTERM = '''
    import signal
    class T(unittest.TestCase):
        def test_stubborn(self):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            open({pidfile!r}, "w").write(str(os.getpid()))
            time.sleep(120)
'''


def wait_for_path(path, seconds=20.0):
    """Wait until path exists; a .json record must also be complete (it ends with a newline)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and (path.suffix != ".json" or path.read_text().endswith("\n")):
            return True
        time.sleep(0.05)
    return False


def wait_for(path, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return True
        time.sleep(0.05)
    return False


class Interrupts(unittest.TestCase):

    def interrupted_run(self, sig):
        repo = FixtureRepo(self)
        repo.repo = repo.root
        module_pid = repo.pids / "module"
        child_pid = repo.pids / "grandchild"
        repo.module("test_a_slow", f'''
            class T(unittest.TestCase):
                def test_slow(self):
                    daemonize({str(child_pid)!r})
                    open({str(module_pid)!r}, "w").write(str(os.getpid()))
                    time.sleep(60)
        ''')
        repo.passing("test_b_pending")
        driver = start_driver(repo, "full", "--leg", "mac", "--workers", "1", "--out", str(repo.out))
        self.addCleanup(stop_driver, driver)
        self.assertTrue(wait_for(module_pid), "fixture module never started")
        driver.send_signal(sig)
        output, _ = driver.communicate(timeout=60)
        self.assertEqual(driver.returncode, 1, output)
        self.assertTrue(wait_gone(int(module_pid.read_text())), "step survived the interrupt")
        self.assertTrue(wait_gone(int(child_pid.read_text())), "setsid grandchild survived")
        self.assertEqual(repo.step("relay_engine.tests.test_a_slow")["outcome"], "unfinished")
        self.assertEqual(repo.step("relay_engine.tests.test_b_pending")["outcome"], "unfinished")
        self.assertTrue(repo.results()["interrupted"])

    def test_sigint_stops_steps_and_writes_results(self):
        self.interrupted_run(signal.SIGINT)

    def test_sigterm_stops_steps_and_writes_results(self):
        self.interrupted_run(signal.SIGTERM)

    def test_sighup_stops_and_writes(self):
        self.interrupted_run(signal.SIGHUP)

    def stubborn_repo(self):
        repo = FixtureRepo(self)
        repo.repo = repo.root
        pidfile = repo.pids / "stubborn"
        self.addCleanup(kill_pidfile, pidfile)
        repo.module("test_a_stubborn", IGNORES_SIGTERM.format(pidfile=str(pidfile)))
        return repo, pidfile

    def test_cap_escalates_to_sigkill(self):
        repo, pidfile = self.stubborn_repo()
        driver = start_driver(repo, "full", "--leg", "mac", "--workers", "1", "--cap-seconds", "2",
                              "--out", str(repo.out))
        self.addCleanup(stop_driver, driver)
        output, _ = driver.communicate(timeout=60)
        self.assertEqual(driver.returncode, 1, output)
        self.assertTrue(wait_gone(int(pidfile.read_text())), "a SIGTERM-ignoring step survived the cap")
        self.assertEqual(repo.step("relay_engine.tests.test_a_stubborn")["outcome"], "unfinished")

    def test_second_interrupt_during_grace(self):
        repo, pidfile = self.stubborn_repo()
        driver = start_driver(repo, "full", "--leg", "mac", "--workers", "1", "--out", str(repo.out))
        self.addCleanup(stop_driver, driver)
        self.assertTrue(wait_for(pidfile), "fixture module never started")
        driver.send_signal(signal.SIGINT)
        time.sleep(1)
        driver.send_signal(signal.SIGINT)
        output, _ = driver.communicate(timeout=60)
        self.assertEqual(driver.returncode, 1, output)
        self.assertTrue((repo.out / "mac" / "results.json").is_file(), output)
        self.assertTrue(wait_gone(int(pidfile.read_text())), "the step was not SIGKILLed")
        self.assertEqual(repo.step("relay_engine.tests.test_a_stubborn")["outcome"], "unfinished")
        self.assertTrue(repo.results()["interrupted"])


class UsedOut(unittest.TestCase):

    def test_used_out_dir_refused(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run()
        self.assertEqual(code, 0, err)
        first = (repo.out / "mac" / "results.json").read_text()
        code, err = repo.run()
        self.assertEqual(code, 2)
        self.assertIn("already holds", err)
        self.assertEqual((repo.out / "mac" / "results.json").read_text(), first)


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

    def test_release_refuses_one_leg(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run("--release")
        self.assertEqual(code, 2, err)
        self.assertIn("--leg mac", err)
        self.assertFalse(repo.out.exists(), "a refused release run wrote results")

    def test_release_refuses_long_cap(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run("--release", "--leg", "both", "--cap-seconds", "1801")
        self.assertEqual(code, 2, err)
        self.assertIn("--cap-seconds 1801", err)
        self.assertNotIn("--leg", err)
        self.assertFalse(repo.out.exists(), "a refused release run wrote results")

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
        # A directory left at the old fixed name by an earlier run must not matter.
        legacy = Path("/tmp/adt-release-check-x")
        if not legacy.exists():
            legacy.mkdir()
            self.addCleanup(shutil.rmtree, legacy, True)
        name = f"adt-release-check-{uuid.uuid4().hex}"
        outside = [f"/tmp/{name}", f"/private/tmp/{name}-p",
                   os.path.join(os.path.realpath(tempfile.gettempdir()), name)]
        for path in outside:
            self.addCleanup(shutil.rmtree, path, True)
        self.assertTrue(any(p.startswith("/var/folders") or p.startswith("/private/var/folders")
                            for p in outside) or sys.platform != "darwin")
        for path in outside:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = rc.main(["full", "--leg", "mac", "--out", path], repo=repo.root,
                               environ=dict(os.environ), exclusions=())
            self.assertEqual(code, 2, path)
            self.assertFalse(os.path.exists(path), path)

    def test_out_is_file_is_setup_error(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.out.write_text("not a directory\n")
        code, err = repo.run()
        self.assertEqual(code, 2, err)
        self.assertIn("not a directory", err)

    def test_out_symlink_outside_home_refused(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        target = Path(tempfile.mkdtemp(prefix="adt-release-check-target-"))
        self.addCleanup(shutil.rmtree, target, True)
        self.assertFalse(rc._under_home(str(target), dict(os.environ)), target)
        link = repo.pids / "out-link"
        link.symlink_to(target)
        out = link / "out"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rc.main(["full", "--leg", "mac", "--out", str(out)], repo=repo.root,
                           environ=dict(os.environ), exclusions=())
        self.assertEqual(code, 2, err.getvalue())
        self.assertFalse((target / "out").exists(), "results were written outside $HOME")

    def test_out_dir_under_home_accepted(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run()
        self.assertEqual(code, 0, err)


ADAPTER_SCRIPT = "tools/adapters/claude-code/test-adapter.sh"


def adapter_repo(test_case, named, extra=(), skip=False):
    """A fixture whose test-adapter.sh runs the named adapter modules, as the real one does."""
    repo = FixtureRepo(test_case)
    repo.passing("test_a_ok")
    for directory in ("tools/adapters", "tools/adapters/tests"):
        repo.tool(f"{directory}/__init__.py", "")
    body = '''
        import unittest
        class T(unittest.TestCase):
            def test_ok(self):
                pass
    '''
    if skip:
        body += '''
            def test_skip(self):
                self.skipTest("fixture skip")
        '''
    for name in (*named, *extra):
        repo.tool(f"tools/adapters/tests/{name}.py", body)
    modules = " ".join(f"tools.adapters.tests.{name}" for name in named)
    script = repo.tool(ADAPTER_SCRIPT, f'''
        echo "e1 cases: PASS"
        if ! python3 -m unittest {modules} 2>"$TMPDIR/unittest.err.$$"; then
          cat "$TMPDIR/unittest.err.$$" >&2; exit 1
        fi
        echo "PASS relay-monitor unittest"
        cat "$TMPDIR/unittest.err.$$" >&2
    ''')
    script.chmod(0o755)
    return repo


class Coverage(unittest.TestCase):

    def test_unnamed_adapter_module_is_uncovered(self):
        repo = adapter_repo(self, ["test_named"], extra=["test_new"])
        code, err = repo.run()
        self.assertEqual(code, 2, err)
        self.assertIn("tools/adapters/tests/test_new.py", err)
        self.assertNotIn("tools/adapters/tests/test_named.py", err)
        control = adapter_repo(self, ["test_named"])
        self.assertEqual(rc.uncovered(control.root, rc.step_table(control.root), ()), [])

    def test_adapter_block_skip_fails_step(self):
        repo = adapter_repo(self, ["test_named"], skip=True)
        code, err = repo.run()
        self.assertEqual(code, 1, err)
        step = repo.step("test-adapter.sh")
        self.assertEqual((step["outcome"], step["reason"]), ("fail", "skipped"))
        control = adapter_repo(self, ["test_named"])
        code, err = control.run()
        self.assertEqual(code, 0, err)
        step = control.step("test-adapter.sh")
        self.assertEqual((step["outcome"], step["reason"]), ("pass", None))


class Discovery(unittest.TestCase):

    def test_nested_and_new_test_dirs_are_discovered(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.tool("tools/console_tests/test_x.py", "import unittest\n")
        repo.tool("tools/relay_engine/tests/sub/test_y.py", "import unittest\n")
        repo.tool("tools/relay_engine/tests/__pycache__/test_z.py", "")
        code, err = repo.run()
        self.assertEqual(code, 2, err)
        self.assertIn("tools/console_tests/test_x.py", err)
        self.assertIn("tools/relay_engine/tests/sub/test_y.py", err)
        self.assertNotIn("__pycache__", err)


class StartedLog(unittest.TestCase):

    def test_started_log_written_before_step_ends(self):
        repo = FixtureRepo(self)
        log = repo.out / "mac" / "started.jsonl"
        repo.module("test_a_reads", f'''
            import json
            class T(unittest.TestCase):
                def test_reads(self):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        rows = [json.loads(line) for line in open({str(log)!r})] if os.path.exists({str(log)!r}) else []
                        mine = [r for r in rows if r["id"] == "relay_engine.tests.test_a_reads"]
                        if mine:
                            break
                        time.sleep(0.05)
                    self.assertEqual(mine, [{{"id": "relay_engine.tests.test_a_reads", "pid": os.getpid(),
                                              "marker": os.environ["ADT_RELEASE_STEP"]}}])
        ''')
        code, err = repo.run("--workers", "1")
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

    def test_skipped_test_fails_step(self):
        repo = FixtureRepo(self)
        repo.module("test_a_skips", '''
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
                def test_skip(self):
                    self.skipTest("fixture skip")
        ''')
        code, err = repo.run()
        self.assertEqual(code, 1, err)
        step = repo.step("relay_engine.tests.test_a_skips")
        self.assertEqual((step["outcome"], step["reason"]), ("fail", "skipped"))

    def test_empty_module_fails_step(self):
        repo = FixtureRepo(self)
        repo.module("test_a_empty", '''
            HELPER = 1
        ''')
        code, err = repo.run()
        self.assertEqual(code, 1, err)
        step = repo.step("relay_engine.tests.test_a_empty")
        self.assertEqual((step["outcome"], step["reason"]), ("fail", "no-tests"))

    def test_passing_module_has_no_reason(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        code, err = repo.run()
        self.assertEqual(code, 0, err)
        step = repo.step("relay_engine.tests.test_a_ok")
        self.assertEqual((step["outcome"], step["reason"]), ("pass", None))

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
                      "steps", "exit", "unattributed", "error"):
            self.assertIn(field, results)
        self.assertEqual(results["leg"], "mac")
        self.assertEqual(results["commit"], commit)
        self.assertEqual(set(results["python"]), {"path", "version"})
        self.assertEqual(set(results["load"]), {"start", "end"})
        self.assertEqual(results["exit"], 0)
        self.assertEqual((results["unattributed"], results["error"]), ([], None))
        self.assertEqual(results["exclusions"],
                         [{"path": "tools/check-old.py", "reason": exclusion.reason}])
        for step in results["steps"]:
            self.assertEqual(set(step), {"id", "argv", "outcome", "reason", "exit_code",
                                         "wall_seconds", "log", "leaked"})
            self.assertIsInstance(step["wall_seconds"], (int, float))
            self.assertTrue(Path(step["log"]).is_file())


def docker_binary():
    return shutil.which("docker")


class LinuxFixture(FixtureRepo):
    """A committed fixture repository the linux leg can run: it carries the runner itself."""

    def __init__(self, test_case, linked=False):
        super().__init__(test_case)
        (self.root / "tools" / "release-check.py").write_text(SCRIPT.read_text())
        (self.root / "AGENTS.md").write_text("fixture\n")
        self.passing("test_a_ok")
        self.head = None
        self.repo = self.root
        if linked:
            self.linked_dir = self.root.parent / (self.root.name + "-wt")
            test_case.addCleanup(shutil.rmtree, self.linked_dir, True)

    def finish(self, linked=False):
        self.head = self.commit()
        if linked:
            subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-q", "--detach",
                            str(self.linked_dir), "HEAD"], check=True)
            self.repo = self.linked_dir
        return self

    def run_legs(self, leg, environ=None, history=(), extra=()):
        argv = ["full", "--leg", leg, "--out", str(self.out), "--workers", "2", *extra]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rc.main(argv, repo=self.repo,
                           environ=dict(os.environ if environ is None else environ), exclusions=(),
                           history=history)
        return code, err.getvalue()

    def container(self, args):
        """Run the leg's container directly with the given docker arguments."""
        (self.out / "linux").mkdir(parents=True, exist_ok=True)
        rc.write_container_plan(self.out / "linux", rc.step_table(self.repo), "linux", (), ())
        return subprocess.run([docker_binary()] + args, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)


class DockerArgs(unittest.TestCase):

    def test_mount_args_are_readonly_binds(self):
        worktree = Path("/Users/someone/worktrees/w")
        common = Path("/Users/someone/repo/.git")
        args = rc.docker_args(worktree, common, Path("/Users/someone/out"), "img:tag", "abc123",
                              platform="linux/arm64")
        self.assertNotIn("-v", args)
        self.assertNotIn("--volume", args)
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
        self.assertIn(f"type=bind,src={worktree},dst={worktree},readonly", mounts)
        self.assertIn(f"type=bind,src={common},dst={common},readonly", mounts)
        self.assertIn("type=bind,src=/Users/someone/out/linux,dst=/out", mounts)
        self.assertIn("--init", args)
        self.assertEqual(args[args.index("--tmpfs") + 1], "/scratch:rw,exec,mode=1777")
        self.assertEqual(args[args.index("--platform") + 1], "linux/arm64")
        self.assertIn("ADT_EXPECTED_HEAD=abc123", args)
        self.assertEqual(args[args.index("-w") + 1], str(worktree))


class Preflight(unittest.TestCase):

    def environ(self, mounts):
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        return {"ADT_SOURCE_MOUNTS": os.pathsep.join(map(str, mounts)), "ADT_EXPECTED_HEAD": head}

    def test_preflight_rejects_missing_mount(self):
        missing = SCRATCH_BASE / "no-such-mount"
        self.assertEqual(rc.preflight(REPO, self.environ([missing])),
                         [f"source mount {missing} does not exist"])

    def test_preflight_rejects_unprobeable_mount(self):
        SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
        locked = Path(tempfile.mkdtemp(prefix="locked-", dir=SCRATCH_BASE))
        self.addCleanup(shutil.rmtree, locked, True)
        self.addCleanup(locked.chmod, 0o755)
        locked.chmod(0o555)
        problems = rc.preflight(REPO, self.environ([locked]))
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(f"source mount {locked} could not be probed"), problems)


class LinuxLeg(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.image = rc.ensure_image(docker_binary())

    def readonly_args(self, fixture, expected_head=None):
        common = rc.common_git_dir(fixture.repo)
        return rc.docker_args(fixture.repo, common, fixture.out, self.image,
                              expected_head or fixture.head, platform=rc.native_platform(docker_binary()))

    def test_preflight_rejects_writable_mount(self):
        fixture = LinuxFixture(self).finish()
        args = self.readonly_args(fixture)
        writable = [a.replace(f"dst={fixture.repo},readonly", f"dst={fixture.repo}") for a in args]
        result = fixture.container(writable)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"source mount {fixture.repo} accepts a write", result.stdout + result.stderr)
        control = fixture.container(args)
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)

    def test_preflight_rejects_missing_history(self):
        fixture = LinuxFixture(self).finish()
        code, err = fixture.run_legs("linux", history=("0123abc",))
        self.assertEqual(code, 2, err)
        self.assertIn("git show 0123abc: fails in the container",
                      (fixture.out / "linux" / "docker.log").read_text())
        fixture.out = fixture.out.parent / (fixture.out.name + "-control")
        self.addCleanup(shutil.rmtree, fixture.out, True)
        code, err = fixture.run_legs("linux", history=(fixture.head[:7],))
        self.assertEqual(code, 0, err)

    def test_container_runs_as_host_uid(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_refused", '''
            import tempfile
            class T(unittest.TestCase):
                def test_refused(self):
                    path = os.path.join(tempfile.gettempdir(), "unreadable")
                    open(path, "w").close()
                    os.chmod(path, 0)
                    with self.assertRaises(PermissionError):
                        open(path)
        ''')
        fixture.finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 0, err)

    def test_scratch_env_in_container(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_scratch", '''
            import tempfile
            class T(unittest.TestCase):
                def test_scratch(self):
                    self.assertEqual((os.environ.get("TMPDIR"), os.environ.get("HOME")),
                                     ("/scratch", "/scratch"))
                    self.assertEqual(tempfile.gettempdir(), "/scratch")
        ''')
        fixture.finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 0, err)

    def test_preflight_rejects_head_mismatch(self):
        fixture = LinuxFixture(self).finish()
        result = fixture.container(self.readonly_args(fixture, expected_head="0" * 40))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("HEAD", result.stdout + result.stderr)

    ORPHAN = '''
        import signal
        class T(unittest.TestCase):
            def test_orphan_reaped(self):
                read, write = os.pipe()
                if os.fork() == 0:
                    if os.fork() == 0:
                        os.write(write, str(os.getpid()).encode()); time.sleep(60); os._exit(0)
                    os._exit(0)
                orphan = int(os.read(read, 32))
                os.kill(orphan, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(orphan, 0)
                    except ProcessLookupError:
                        return
                    time.sleep(0.05)
                self.fail("killed orphan still answers kill(pid, 0)")
    '''

    def test_orphan_is_reaped_on_linux(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_orphan", self.ORPHAN)
        fixture.finish()
        args = self.readonly_args(fixture)
        result = fixture.container(args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        step = next(s for s in linux["steps"] if s["id"] == "relay_engine.tests.test_b_orphan")
        self.assertEqual(step["outcome"], "pass")
        control = fixture.container([a for a in args if a != "--init"])
        self.assertEqual(control.returncode, 1, control.stdout + control.stderr)

    def test_scratch_is_executable_on_linux(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_exec", '''
            import subprocess, tempfile
            class T(unittest.TestCase):
                def test_exec(self):
                    script = os.path.join(tempfile.gettempdir(), "fixture.sh")
                    with open(script, "w") as handle:
                        handle.write("#!/bin/sh\\necho exec-ok\\n")
                    os.chmod(script, 0o755)
                    self.assertEqual(subprocess.run([script], capture_output=True, text=True,
                                                    check=True).stdout.strip(), "exec-ok")
        ''')
        fixture.finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 0, err)

    def test_linux_leg_runs_native_platform(self):
        fixture = LinuxFixture(self).finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 0, err)
        native = subprocess.run([docker_binary(), "info", "--format", "{{.Architecture}}"],
                                capture_output=True, text=True, check=True).stdout.strip()
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        self.assertEqual(linux["machine"], native)
        self.assertTrue(linux["image"].startswith("sha256:"), linux["image"])

    def test_linux_sweep_stops_setsid_daemon(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_daemon", '''
            class T(unittest.TestCase):
                def test_daemon(self):
                    daemonize(os.path.join(os.environ["TMPDIR"], "daemon.pid"))
        ''')
        fixture.finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 0, err)
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        step = next(s for s in linux["steps"] if s["id"] == "relay_engine.tests.test_b_daemon")
        self.assertEqual(len(step["leaked"]), 1, step)
        self.assertEqual(linux["unattributed"], [])

    STARTS = '''
        class T(unittest.TestCase):
            def test_slow(self):
                open(os.path.join(os.environ["RELAY_ENGINE_RESULTS_ROOT"], {name!r}), "w").close()
                time.sleep(120)
    '''

    def interrupted_linux_leg(self, modules):
        """Interrupt a linux leg once every named module has started; return its records."""
        fixture = LinuxFixture(self)
        for name, body in modules.items():
            fixture.module(name, body)
        fixture.finish()
        driver = start_driver(fixture, "full", "--leg", "linux", "--workers", str(len(modules)),
                              "--out", str(fixture.out))
        self.addCleanup(stop_driver, driver)
        label = self.run_label(fixture)
        self.addCleanup(self.remove_labelled, label)
        started = fixture.out / "linux" / "engine-results"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not all((started / n).exists() for n in modules):
            time.sleep(0.2)
        self.assertTrue(all((started / n).exists() for n in modules), "the steps never started")
        driver.send_signal(signal.SIGINT)
        output, _ = driver.communicate(timeout=120)
        self.assertEqual(driver.returncode, 1, output)
        self.assertEqual(self.labelled(label), [], "a container of this run survived the interrupt")
        results = fixture.out / "linux" / "results.json"
        self.assertTrue(results.is_file(), output)
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["legs"]["linux"]["results"], str(results))
        linux = json.loads(results.read_text())
        self.assertTrue(linux["interrupted"])
        return linux

    def test_interrupt_kills_the_container(self):
        # Another container of the same image (another worktree's check) must not be taken for
        # this run's: containers are found by this run's label (plan r4 T11).
        bystander = subprocess.run([docker_binary(), "run", "-d", "--rm", "--label",
                                    f"{rc.RUN_LABEL}=bystander-{os.getpid()}", self.image, "sleep", "300"],
                                   capture_output=True, text=True, check=True).stdout.strip()
        self.addCleanup(subprocess.run, [docker_binary(), "rm", "-f", bystander], capture_output=True)
        linux = self.interrupted_linux_leg({"test_b_slow": self.STARTS.format(name="test_b_slow")})
        running = subprocess.run([docker_binary(), "ps", "-q", "--no-trunc", "--filter", f"id={bystander}"],
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(running, [bystander], "the bystander container was stopped")
        step = next(s for s in linux["steps"] if s["id"] == "relay_engine.tests.test_b_slow")
        self.assertEqual(step["outcome"], "unfinished")

    def test_linux_interrupt_writes_results(self):
        stubborn = self.STARTS.format(name="test_b_stubborn").replace(
            "def test_slow(self):", "def test_slow(self):\n                import signal; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)")
        linux = self.interrupted_linux_leg({"test_b_plain": self.STARTS.format(name="test_b_plain"),
                                            "test_b_stubborn": stubborn})
        outcomes = {s["id"]: s["outcome"] for s in linux["steps"]}
        self.assertEqual(outcomes["relay_engine.tests.test_b_stubborn"], "unfinished")
        self.assertEqual(outcomes["relay_engine.tests.test_b_plain"], "unfinished")

    def fake_docker(self, fixture, run_lines):
        """A docker that runs `run_lines` for `docker run` and passes everything else through."""
        script = fixture.pids / "docker"
        script.write_text(f"#!/bin/sh\nif [ \"$1\" = run ]; then\n{run_lines}\nfi\n"
                          f"exec {docker_binary()} \"$@\"\n")
        script.chmod(0o755)
        return script

    def test_interrupt_during_docker_setup(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_slow", self.STARTS.format(name="test_b_slow"))
        fixture.finish()
        mark = fixture.pids / "docker-run-called"
        docker = self.fake_docker(fixture, f": > {mark}\nsleep 3")
        driver = start_driver(fixture, "full", "--leg", "linux", "--out", str(fixture.out),
                              env={**os.environ, "DOCKER": str(docker)})
        self.addCleanup(stop_driver, driver)
        label = self.run_label(fixture)
        self.addCleanup(self.remove_labelled, label)
        self.assertTrue(wait_for_path(mark), "docker run was never called")
        self.assertEqual(self.labelled(label), [], "the container was already visible")
        driver.send_signal(signal.SIGINT)
        output, _ = driver.communicate(timeout=90)
        self.assertEqual(driver.returncode, 1, output)
        self.assertEqual(self.labelled(label), [], output)
        time.sleep(5)
        self.assertEqual(self.labelled(label), [], "a container of this run started after the leg ended")
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertTrue(summary["interrupted"])

    def test_empty_module_fails_on_linux(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_empty", '''
            HELPER = 1
        ''')
        fixture.finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 1, err)
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        self.assertEqual(linux["python"]["version"].rsplit(".", 1)[0], "3.11")
        step = next(s for s in linux["steps"] if s["id"] == "relay_engine.tests.test_b_empty")
        self.assertEqual((step["outcome"], step["reason"], step["exit_code"]), ("fail", "no-tests", 0))

    def test_summary_records_release_legs_cap(self):
        fixture = LinuxFixture(self).finish()
        code, err = fixture.run_legs("mac")
        self.assertEqual(code, 0, err)
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertEqual((summary["release"], summary["cap_seconds"], set(summary["legs"])),
                         (False, 1800, {"mac"}))
        # Control for the release refusals: both legs and the full cap start and pass.
        fixture.out = fixture.out.parent / (fixture.out.name + "-release")
        self.addCleanup(shutil.rmtree, fixture.out, True)
        code, err = fixture.run_legs("both", extra=("--release", "--cap-seconds", "1800"))
        self.assertEqual(code, 0, err)
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertEqual((summary["release"], summary["cap_seconds"], set(summary["legs"])),
                         (True, 1800, {"mac", "linux"}))

    def test_sigkill_cleanup_finds_leftovers(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_slow", self.STARTS.format(name="test_b_slow"))
        fixture.finish()
        driver = start_driver(fixture, "full", "--leg", "both", "--workers", "2", "--out", str(fixture.out))
        self.addCleanup(stop_driver, driver)
        label = self.run_label(fixture)
        self.addCleanup(self.remove_labelled, label)
        started = [fixture.out / leg / "engine-results" / "test_b_slow" for leg in ("mac", "linux")]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not all(p.exists() for p in started):
            time.sleep(0.2)
        self.assertTrue(all(p.exists() for p in started), "the steps never started")
        driver.send_signal(signal.SIGKILL)
        driver.communicate()
        self.assertFalse((fixture.out / "release-check.json").exists())
        rows = [json.loads(line) for line in (fixture.out / "mac" / "started.jsonl").read_text().splitlines()]
        step = next(r for r in rows if r["id"] == "relay_engine.tests.test_b_slow")
        self.addCleanup(kill_group, step["pid"])
        # The documented commands, run as written against what the killed run left.
        block = sigkill_block(TESTING_MD.read_text()).replace("<the run's --out directory>", str(fixture.out))
        listed = subprocess.run(["bash", "-c", block], capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual(set(self.labelled(label)) & {line.strip() for line in listed},
                         set(self.labelled(label)), listed)
        self.assertTrue(self.labelled(label), "the container was already gone")
        self.assertIn(f"{step['pid']} {step['marker']}", listed)
        marked = subprocess.run(["bash", "-c", sigkill_marker_command(TESTING_MD.read_text())
                                 .replace("<marker>", step["marker"])], capture_output=True, text=True).stdout
        self.assertIn(str(step["pid"]), marked.split())

    def test_container_kill_is_not_green(self):
        fixture = LinuxFixture(self).finish()
        base = fixture.out
        for name, lines in (("killed", "exit 137"), ("daemon-error", "exit 125"),
                            ("no-results", "exit 0")):
            fixture.out = base.parent / f"{base.name}-{name}"
            self.addCleanup(shutil.rmtree, fixture.out, True)
            docker = self.fake_docker(fixture, lines)
            code, err = fixture.run_legs("linux", environ={**os.environ, "DOCKER": str(docker)})
            self.assertEqual(code, 2, f"{name}: {err}")
            summary = json.loads((fixture.out / "release-check.json").read_text())
            self.assertEqual(summary["legs"]["linux"], {"exit": 2, "results": None}, name)

    def run_label(self, fixture, seconds=60.0):
        record = fixture.out / "linux" / "container.json"
        self.assertTrue(wait_for_path(record, seconds), "the leg wrote no container record")
        return json.loads(record.read_text())["label"]

    def labelled(self, label):
        listing = subprocess.run([docker_binary(), "ps", "-aq", "--filter", f"label={label}"],
                                 capture_output=True, text=True, check=True).stdout
        return listing.split()

    def remove_labelled(self, label):
        for container in self.labelled(label):
            subprocess.run([docker_binary(), "rm", "-f", container], capture_output=True)

    def test_git_works_in_linked_worktree(self):
        fixture = LinuxFixture(self, linked=True).finish(linked=True)
        self.assertNotEqual(rc.common_git_dir(fixture.repo), fixture.repo / ".git")
        result = fixture.container(self.readonly_args(fixture))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("preflight: clean", result.stdout + result.stderr)

    def test_linux_only_failure_fails_whole_check(self):
        fixture = LinuxFixture(self)
        fixture.module("test_b_linux_only", '''
            class T(unittest.TestCase):
                def test_platform(self):
                    if sys.platform.startswith("linux"):
                        self.fail("fails only on linux")
        ''')
        fixture.finish()
        code, err = fixture.run_legs("both")
        self.assertEqual(code, 1, err)
        mac = json.loads((fixture.out / "mac" / "results.json").read_text())
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        step = "relay_engine.tests.test_b_linux_only"
        self.assertEqual(next(s for s in mac["steps"] if s["id"] == step)["outcome"], "pass")
        self.assertEqual(next(s for s in linux["steps"] if s["id"] == step)["outcome"], "fail")
        self.assertEqual(linux["python"]["version"].rsplit(".", 1)[0], "3.11")
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertEqual(summary["exit"], 1)
        self.assertEqual({leg: summary["legs"][leg]["exit"] for leg in ("mac", "linux")},
                         {"mac": 0, "linux": 1})


class MissingDocker(unittest.TestCase):

    def test_missing_docker_is_setup_error(self):
        fixture = LinuxFixture(self).finish()
        environ = dict(os.environ)
        environ["DOCKER"] = str(fixture.pids / "no-such-docker")
        code, err = fixture.run_legs("linux", environ=environ)
        self.assertEqual(code, 2, err)
        self.assertIn("docker", err)
        fixture.out = fixture.out.parent / (fixture.out.name + "-both")
        self.addCleanup(shutil.rmtree, fixture.out, True)
        code, err = fixture.run_legs("both", environ=environ)
        self.assertEqual(code, 2, err)
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertEqual(summary["legs"]["linux"]["exit"], 2)
        self.assertEqual(summary["legs"]["mac"]["exit"], 0)


class RealTree(unittest.TestCase):

    def test_real_tree_is_covered(self):
        self.assertEqual(rc.uncovered(REPO, rc.step_table(REPO), rc.EXCLUSIONS), [])

    def test_pdc_consumer_runs_on_mac_only(self):  # PR-3 G-E2; DD-v3-tests § 3.6
        module = "relay_engine.tests.test_pdc_consumer"
        steps = rc.step_table(REPO)
        by_id = {s.id: s for s in steps}
        self.assertEqual(by_id[module].legs, rc.MAC)
        self.assertEqual(by_id[module].covers, ("tools/relay_engine/tests/test_pdc_consumer.py",))
        self.assertEqual([s.id for s in steps if s.id.startswith("relay_engine.tests.")
                          and s.legs != rc.BOTH], [module])
        with tempfile.TemporaryDirectory() as name:
            linux = json.loads(rc.write_container_plan(name, steps, "linux", rc.EXCLUSIONS)
                               .read_text())["step_ids"]
        self.assertNotIn(module, linux)
        self.assertIn("relay_engine.tests.test_header", linux)
        self.assertIn(module, [s.id for s in steps if "mac" in s.legs
                               and not rc._excluded(s, rc.EXCLUSIONS)])


TESTING_MD = REPO / "tools" / "TESTING.md"
TICKED = re.compile(r"`([^`]+)`")


def trap_lines(text):
    """Trap entries: list items of the form - `glob`[, `glob`] → `target`[, `target`]."""
    return [line for line in text.splitlines() if line.startswith("- `") and " → " in line]


def resolve_target(repo, target):
    if "/" in target:
        return (repo / target).exists()
    candidates = [repo / "tools" / target, repo / "tools/adapters/claude-code" / target]
    if not target.endswith((".py", ".sh")):
        candidates = [repo / "tools/relay_engine/tests" / f"{target}.py",
                      repo / "tools/adapters/tests" / f"{target}.py"]
    return any(c.exists() for c in candidates)


def trap_problems(repo, text):
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True,
                             check=True).stdout.splitlines()
    problems = []
    lines = trap_lines(text)
    if not lines:
        problems.append("no trap lines found")
    for line in lines:
        left, right = line.split(" → ", 1)
        for glob in TICKED.findall(left):
            if not any(fnmatch.fnmatchcase(path, glob) for path in tracked):
                problems.append(f"glob {glob} matches no tracked file")
        targets = TICKED.findall(right)
        if not targets:
            problems.append(f"no target on line: {line}")
        for target in targets:
            if not resolve_target(repo, target):
                problems.append(f"target {target} does not exist")
    return problems


def sigkill_block(text):
    """The indented command block under the SIGKILL residual's heading."""
    section = text.split("### After a SIGKILL", 1)[1].split("\n## ", 1)[0]
    return "\n".join(line[4:] for line in section.splitlines() if line.startswith("    "))


def sigkill_marker_command(text):
    section = text.split("### After a SIGKILL", 1)[1].split("\n## ", 1)[0]
    return next(c for c in TICKED.findall(section) if c.startswith("ps -E"))


class TestingDoc(unittest.TestCase):

    def test_testing_md_states_sigkill_residual(self):
        text = TESTING_MD.read_text()
        section = text.split("### After a SIGKILL", 1)[1].split("\n## ", 1)[0]
        for needle in ("SIGKILL", "started.jsonl", "container.json", "docker ps -q --filter",
                       "kill -TERM -- -<pid>", "docker stop"):
            self.assertIn(needle, section)
        self.assertNotIn("label=adt.release-check.run=", section)

    def test_testing_md_trap_list_resolves(self):
        text = TESTING_MD.read_text()
        self.assertEqual(trap_problems(REPO, text), [])
        self.assertIn("release-check.py full", text)

    def test_module_command_sets_the_results_environment(self):
        # test_e2e_chain skips its census test without these (test_e2e_chain.py:425-436).
        text = TESTING_MD.read_text()
        command = next(line for line in text.splitlines() if "relay_engine.tests.<module>" in line)
        for name in ("RELAY_ENGINE_RESULTS_ROOT=", "RELAY_ENGINE_MATRIX_RUN=", "PYTHONPATH=tools"):
            self.assertIn(name, command)

    def test_trap_list_names_adapters(self):
        lines = trap_lines(TESTING_MD.read_text())
        self.assertTrue(any("`tools/adapters/**`" in line for line in lines))

    # Rows measured by the one rule TESTING.md states (direct or through an imported helper,
    # minus the runner's exclusion table); counts from PL-v3-tests-1 r4 erratum 1.
    MEASURED_ROWS = {"`tools/relay-lint.py`": 38, "`tools/relay-engine-fixtures/**`": 23,
                     "`tools/relay-lint-fixtures/**`": 26, "`tools/check-relay-lint-fixtures.py`": 5,
                     "`tools/relay`": 3, "`tools/measure-plan-shape.py`": 1, "`tools/**`": 4}

    def row(self, glob):
        lines = [line for line in trap_lines(TESTING_MD.read_text()) if line.split(" → ")[0] == f"- {glob}"]
        self.assertEqual(len(lines), 1, glob)
        return TICKED.findall(lines[0].split(" → ", 1)[1])

    def test_trap_rows_follow_the_measured_rule(self):
        for glob, count in self.MEASURED_ROWS.items():
            targets = self.row(glob)
            self.assertEqual(len(targets), count, (glob, targets))
            self.assertEqual(len(set(targets)), count, glob)
            excluded = [t for t in targets if any(fnmatch.fnmatchcase(f"tools/relay_engine/tests/{t}.py", e.path)
                                                  or fnmatch.fnmatchcase(f"tools/{t}", e.path)
                                                  for e in rc.EXCLUSIONS)]
            self.assertEqual(excluded, [], glob)
        self.assertIn("test-adapter.sh", self.row("`tools/relay_engine/*.py`"))
        self.assertEqual(set(self.row("`tools/**`")),
                         {"test_version", "test_identity_matrix", "test_mtbattery_guard", "check-generate-plugins.py"})
        self.assertNotIn("capture_order_baseline", TESTING_MD.read_text())

    def test_descendant_rule_states_the_macos_residual(self):
        text = TESTING_MD.read_text()
        self.assertIn("`unattributed`", text)
        self.assertIn("process group", text)

    def test_trap_list_control_names_a_renamed_module(self):
        text = TESTING_MD.read_text()
        self.assertIn("`test_version`", text)
        broken = text.replace("`test_version`", "`test_version_renamed`", 1)
        self.assertIn("target test_version_renamed does not exist", trap_problems(REPO, broken))
        broken = text.replace("`shared/**`", "`no-such-dir/**`", 1)
        self.assertIn("glob no-such-dir/** matches no tracked file", trap_problems(REPO, broken))


if __name__ == "__main__":
    unittest.main()
