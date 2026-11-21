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
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "release-check.py"


_GIT_LOCAL_ENV_VARS = frozenset({
    'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    'GIT_CONFIG',
    'GIT_CONFIG_PARAMETERS',
    'GIT_CONFIG_COUNT',
    'GIT_OBJECT_DIRECTORY',
    'GIT_DIR',
    'GIT_WORK_TREE',
    'GIT_IMPLICIT_WORK_TREE',
    'GIT_GRAFT_FILE',
    'GIT_INDEX_FILE',
    'GIT_NO_REPLACE_OBJECTS',
    'GIT_REPLACE_REF_BASE',
    'GIT_PREFIX',
    'GIT_SHALLOW_FILE',
    'GIT_COMMON_DIR',
})


def _git_child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key not in _GIT_LOCAL_ENV_VARS}


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
        subprocess.run(git[:3] + ["init", "-q"], check=True, env=_git_child_env())
        subprocess.run(git + ["add", "-A"], check=True, env=_git_child_env())
        subprocess.run(git + ["commit", "-qm", "fixture"], check=True, env=_git_child_env())
        return subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True, env=_git_child_env()).stdout.strip()

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

    def refuse_release(self, repo, policy, exclusions, *options):
        # create=True lets the controls exercise the former guard before implementation.
        with mock.patch.object(rc, "RELEASE_POLICY", policy, create=True), \
             mock.patch.object(rc, "run_leg") as mac, \
             mock.patch.object(rc, "_run_linux_host") as linux:
            code, err = repo.run("--release", "--leg", "both", *options, exclusions=exclusions)
        self.assertEqual(code, 2, err)
        self.assertFalse(repo.out.exists())
        mac.assert_not_called()
        linux.assert_not_called()
        return err

    def test_release_accepts_empty_policy_and_preserves_leg_verdicts(self):
        for mac_exit, linux_exit in ((0, 0), (0, 1), (0, 2), (1, 2)):
            with self.subTest(mac=mac_exit, linux=linux_exit):
                repo = FixtureRepo(self)
                repo.passing("test_a_ok")
                with mock.patch.object(rc, "RELEASE_POLICY", (), create=True), \
                     mock.patch.object(rc, "run_leg", return_value=rc.LegResult("mac", mac_exit)) as mac, \
                     mock.patch.object(rc, "_run_linux_host",
                                       return_value=rc.LegResult("linux", linux_exit)) as linux:
                    code, err = repo.run("--release", "--leg", "both", "--cap-seconds", "1800")
                self.assertEqual(code, max(mac_exit, linux_exit), err)
                mac.assert_called_once()
                linux.assert_called_once()
                summary = json.loads((repo.out / "release-check.json").read_text())
                self.assertEqual(summary["cap_seconds"], 1800)
                self.assertEqual(summary["exit"], code)
                self.assertEqual(summary["exclusions"], [])
                self.assertEqual({leg: value["exit"] for leg, value in summary["legs"].items()},
                                 {"mac": mac_exit, "linux": linux_exit})

    def test_release_exact_selection_and_serialized_receipts(self):
        repo = FixtureRepo(self)
        for name in ("test_a_ok", "test_legacy_a", "test_legacy_z", "test_pdc_consumer"):
            repo.passing(name)
        literal = "tools/check-old[1].py"
        active = "tools/check-old1.py"
        repo.tool(literal, "raise SystemExit(5)")
        repo.tool(active, "pass")
        repo.tool("AGENTS.md", "fixture")
        head = repo.commit()
        exclusions = (rc.Exclusion("tools/relay_engine/tests/test_legacy*.py", "legacy"),
                      rc.Exclusion("tools/check-old[[]1].py", "obsolete"))
        legacy_paths = ("tools/relay_engine/tests/test_legacy_a.py",
                        "tools/relay_engine/tests/test_legacy_z.py")
        policy = ((exclusions[0], legacy_paths), (exclusions[1], (literal,)))

        def local_transport(steps, args, root, environ, exclusions, history, *, exact_exclusions=False):
            leg_dir = Path(args.out) / "linux"
            leg_dir.mkdir()
            rc.write_container_plan(leg_dir, steps, "linux", exclusions, (),
                                    exact_exclusions=exact_exclusions)
            args = type(args)(**vars(args))
            args.out = str(leg_dir)
            code = rc._run_in_container(args, root, dict(environ, ADT_EXPECTED_HEAD=head,
                                                        ADT_SOURCE_MOUNTS=""))
            return rc.LegResult("linux", code, leg_dir / "results.json")

        with mock.patch.object(rc, "RELEASE_POLICY", policy, create=True), \
             mock.patch.object(rc, "KNOWN_CHECKS", ((active, (), rc.BOTH),)), \
             mock.patch.object(rc, "_run_linux_host", side_effect=local_transport) as linux, \
             mock.patch.object(rc, "run_leg", wraps=rc.run_leg) as run_leg:
            code, err = repo.run("--release", "--leg", "both", "--workers", "1", exclusions=exclusions)
        self.assertEqual(code, 0, err)
        linux.assert_called_once()
        self.assertCountEqual([call.args[0] for call in run_leg.call_args_list], ["mac", "linux"])
        mac = repo.results()
        linux = json.loads((repo.out / "linux" / "results.json").read_text())
        plan = json.loads((repo.out / "linux" / rc.CONTAINER_PLAN).read_text())
        summary = json.loads((repo.out / "release-check.json").read_text())
        expected = [{"path": path, "reason": reason} for path, reason in
                    sorted([(literal, "obsolete"), *((path, "legacy") for path in legacy_paths)])]
        for receipt in (summary, plan, mac, linux):
            self.assertEqual(receipt["exclusions"], expected)
        self.assertTrue(plan["exact_exclusions"])
        self.assertEqual(summary["exit"], 0)
        self.assertTrue(summary["release"])
        self.assertFalse(summary["interrupted"])
        both = ["relay_engine.tests.test_a_ok", "check-old1.py"]
        self.assertEqual(plan["step_ids"], both)
        expected_steps = {"mac": [both[0], "relay_engine.tests.test_pdc_consumer", both[1]], "linux": both}
        for receipt in (mac, linux):
            leg = receipt["leg"]
            roster = expected_steps[leg]
            self.assertEqual([row["id"] for row in receipt["steps"]], roster)
            self.assertEqual([row["outcome"] for row in receipt["steps"]], ["pass"] * len(roster))
            self.assertEqual([row["leaked"] for row in receipt["steps"]], [[] for _ in roster])
            self.assertEqual(receipt["unattributed"], [])
            self.assertIsNone(receipt["error"])
            self.assertFalse(receipt["interrupted"])
            self.assertEqual(receipt["exit"], 0)
            leg_dir = repo.out / leg
            self.assertEqual(summary["legs"][leg], {
                "exit": 0,
                "results": str(leg_dir / "results.json"),
            })
            started = (leg_dir / rc.STARTED_LOG).read_text()
            self.assertEqual([row["id"] for row in map(json.loads, started.splitlines())], roster)

        # The focused comparison can preserve the real serialized fixture files before cleanup.
        evidence = os.environ.get("ADT_RELEASE_POLICY_TEST_EVIDENCE")
        if evidence:
            dest = Path(evidence) / "controlled-legs"
            for name in ("release-check.json", f"linux/{rc.CONTAINER_PLAN}",
                         "mac/results.json", "linux/results.json",
                         f"mac/{rc.STARTED_LOG}", f"linux/{rc.STARTED_LOG}"):
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repo.out / name, target)
            (dest / "expected.json").write_text(json.dumps(
                {"exclusions": expected, "step_ids": expected_steps}, indent=2) + "\n")

    def test_full_keeps_glob_and_summary_shape(self):
        repo = FixtureRepo(self)
        repo.passing("test_a_ok")
        repo.passing("test_old")
        exclusion = rc.Exclusion("tools/relay_engine/tests/test_old*.py", "legacy")
        code, err = repo.run(exclusions=[exclusion])
        self.assertEqual(code, 0, err)
        self.assertEqual(repo.results()["exclusions"], [{"path": exclusion.path, "reason": "legacy"}])
        self.assertEqual([row["id"] for row in repo.results()["steps"]], ["relay_engine.tests.test_a_ok"])
        summary = json.loads((repo.out / "release-check.json").read_text())
        self.assertNotIn("exclusions", summary)
        self.assertEqual(set(summary["legs"]), {"mac"})

    def test_release_refuses_table_disagreement(self):
        repo = FixtureRepo(self)
        literal = "tools/check-old.py"
        repo.tool(literal, "")
        approved = rc.Exclusion(literal, "legacy")
        other = rc.Exclusion("tools/check-old*.py", "legacy")
        policy = ((approved, (literal,)),)
        self.assertIn("duplicate approved", self.refuse_release(repo, policy * 2, (approved,)))
        for table, message in (
            ((approved, other), "unknown"),
            ((approved, approved), "duplicate"),
            ((rc.Exclusion(literal, "changed"),), "reason"),
            ((), "missing"),
        ):
            with self.subTest(table=table):
                err = self.refuse_release(repo, policy, table)
                self.assertIn(message, err)
        err = self.refuse_release(repo, (*policy, (other, (literal,))), (approved, other))
        self.assertIn("overlap", err)
        self.assertIn(literal, err)

    def test_release_refuses_lost_added_or_stale_members(self):
        for stem in ("test_xroot", "test_b9_fold", "test_authority_b9"):
            with self.subTest(pattern=stem):
                repo = FixtureRepo(self)
                repo.passing(stem + "_a")
                repo.passing(stem + "_b")
                repo.passing(stem + "_new")
                folder = "tools/relay_engine/tests/"
                row = rc.Exclusion(folder + stem + "*.py", "legacy")
                pinned = tuple(folder + stem + suffix + ".py" for suffix in ("_a", "_b", "_lost"))
                err = self.refuse_release(repo, ((row, pinned),), (row,))
                self.assertIn("missing", err)
                self.assertIn(folder + stem + "_lost.py", err)
                self.assertIn("extra", err)
                self.assertIn(folder + stem + "_new.py", err)
                actual = tuple(folder + stem + suffix + ".py" for suffix in ("_a", "_b", "_new"))
                err = self.refuse_release(repo, ((row, actual[:2]),), (row,))
                self.assertIn(f"extra paths {[actual[2]]}", err)
                err = self.refuse_release(repo, ((row, (*actual, pinned[2])),), (row,))
                self.assertIn(f"missing paths {[pinned[2]]}", err)
        repo = FixtureRepo(self)
        row = rc.Exclusion("tools/check-stale.py", "legacy")
        err = self.refuse_release(repo, ((row, (row.path,)),), (row,))
        self.assertIn("stale", err)
        self.assertIn(row.path, err)

    def test_release_refuses_uncovered_or_misaligned_steps(self):
        repo = FixtureRepo(self)
        repo.tool("tools/check-new.py", "")
        self.assertIn("neither a step nor an exclusion", self.refuse_release(repo, (), ()))

        repo = FixtureRepo(self)
        repo.passing("test_legacy")
        repo.passing("test_live")
        literal = "tools/relay_engine/tests/test_legacy.py"
        row = rc.Exclusion(literal, "legacy")
        for steps, message in (
            ([rc.Step("relay_engine.tests.test_legacy", [], rc.MAC, (literal,))], "suppression"),
            ([rc.Step("mixed", [], rc.BOTH, (literal, "tools/relay_engine/tests/test_live.py"))], "executing"),
            ([rc.Step("directory", [], rc.BOTH, ("tools/relay_engine/tests",))], "executing"),
        ):
            with self.subTest(steps=steps), mock.patch.object(rc, "step_table", return_value=steps):
                self.assertIn(message, self.refuse_release(repo, ((row, (literal,)),), (row,)))

    def test_release_refuses_one_leg(self):
        for leg in ("mac", "linux"):
            repo = FixtureRepo(self)
            self.assertIn(f"--leg {leg}", self.refuse_release(repo, (), (), "--leg", leg))

    def test_release_refuses_long_cap(self):
        repo = FixtureRepo(self)
        err = self.refuse_release(repo, (), (), "--cap-seconds", "1801")
        self.assertIn("--cap-seconds 1801", err)
        self.assertNotIn("--leg", err)

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
        if sys.platform == "darwin":
            darwin_temp = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True,
                                                  stdin=subprocess.DEVNULL).strip()
            outside.append(str(Path(darwin_temp).resolve() / name))
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
        stderr_file="$PWD/.adapter-unittest.err"
        if ! python3 -m unittest {modules} 2>"$stderr_file"; then
          cat "$stderr_file" >&2; exit 1
        fi
        echo "PASS relay-monitor unittest"
        cat "$stderr_file" >&2
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
        outer_tmp = repo.pids / "outer-tmp"
        outer_tmp.mkdir(mode=0o700)
        environ = dict(os.environ, TMPDIR=str(outer_tmp))
        code, err = repo.run(environ=environ)
        self.assertEqual(code, 1, err)
        step = repo.step("test-adapter.sh")
        self.assertEqual((step["outcome"], step["reason"]), ("fail", "skipped"))
        self.assertEqual(list(outer_tmp.iterdir()), [])

        control = adapter_repo(self, ["test_named"])
        code, err = control.run(environ=environ)
        self.assertEqual(code, 0, err)
        step = control.step("test-adapter.sh")
        self.assertEqual((step["outcome"], step["reason"]), ("pass", None))
        self.assertEqual(list(outer_tmp.iterdir()), [])


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

    def __init__(self, test_case, linked=False, image=None):
        super().__init__(test_case)
        self.image = image
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
                            str(self.linked_dir), "HEAD"], check=True, env=_git_child_env())
            self.repo = self.linked_dir
        return self

    def run_legs(self, leg, environ=None, history=(), extra=()):
        argv = ["full", "--leg", leg, "--out", str(self.out), "--workers", "2", *extra]
        if self.image is not None:
            argv += ["--image", self.image]
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



class ImageResolution(unittest.TestCase):
    TAG = "adt-release-check:5ac9796fa9ca"
    IMAGE = "sha256:" + "a" * 64
    OTHER = "sha256:" + "b" * 64
    THIRD = "sha256:" + "c" * 64
    MISS = (0, 1, "", f"Error response from daemon: No such image: {TAG}")

    def stub(self, *, inspect=None, wake=(0, 0, "", ""), info=(0, 0, "arm64", ""),
             build=(0, 0, "", "")):
        fixture = LinuxFixture(self).finish()
        log = fixture.pids / "argv"
        before = fixture.out / "before.json"
        if inspect is None:
            inspect = [(0, 0, self.IMAGE + " linux/arm64", "")]
        source = f"""#!{sys.executable}
import json
import os
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
command = argv[0]
if command == "image":
    command = argv[1]
log = Path({str(log)!r})
prior = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
before = Path({str(before)!r})
with log.open("a") as stream:
    stream.write(json.dumps({{"seq": len(prior) + 1, "argv": argv, "command": command,
                              "pid": os.getpid(), "before": before.is_file()}}) + "\\n")
if command == "inspect":
    rows = {inspect!r}
    offset = sum(row["command"] == command for row in prior)
    delay, code, out, err = rows[min(offset, len(rows) - 1)]
    if code == 0 and "{{{{.Architecture}}}}" in argv and out.startswith("sha256:"):
        out = out.split()[-1]
    if code == 0 and "{{{{.Id}}}}" in argv and out.startswith("sha256:"):
        out = out.split()[0]
else:
    delay, code, out, err = {{
        "info": {info!r}, "ps": {wake!r}, "build": {build!r}, "run": (0, 2, "", ""),
    }}.get(command, (0, 2, "", "unexpected Docker command"))
time.sleep(delay)
if out:
    print(out)
if err:
    print(err, file=sys.stderr)
raise SystemExit(code)
"""
        docker = fixture.pids / "docker"
        with open(docker, "x", opener=lambda name, flags: os.open(name, flags, 0o755)) as stream:
            stream.write(source)
        return fixture, docker, log

    def rows(self, log):
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def commands(self, log):
        return [row["command"] for row in self.rows(log)]

    @contextlib.contextmanager
    def timings(self, window=0.8, probe=0.35, interval=0.05):
        with mock.patch.object(rc, "WINDOW_SECONDS", window, create=True), \
                mock.patch.object(rc, "PROBE_SECONDS", probe, create=True), \
                mock.patch.object(rc, "PROBE_INTERVAL_SECONDS", interval, create=True):
            yield

    def full(self, fixture, docker, *args, timings=()):
        with self.timings(*timings):
            return fixture.run_legs("linux", environ={**os.environ, "DOCKER": str(docker)}, extra=args)

    def image_build(self, fixture, docker, *, statement=True):
        argv = ["image", "build", "--out", str(fixture.out)]
        if statement:
            argv += ["--operator-verified-absent"]
        with self.timings(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return rc.main(argv, repo=fixture.repo, environ={**os.environ, "DOCKER": str(docker)})

    def image_record(self, fixture):
        return json.loads((fixture.out / "linux" / "image.json").read_text())

    def test_unanswered_lookup_stops_without_building(self):
        fixture, docker, log = self.stub(inspect=[self.MISS])
        status, err = self.full(fixture, docker, timings=(2.0, 1.5, 0.05))
        self.assertTrue({"build", "pull", "run"}.isdisjoint(self.commands(log)), self.rows(log))
        self.assertEqual(status, 2, err)
        self.assertIn(self.TAG, err)
        self.assertIn("all not-found", err)
        record = self.image_record(fixture)
        self.assertEqual(record["result"], "unresolved")
        wake, *inspects = record["probes"]
        self.assertEqual((wake["kind"], wake["class"]), ("wake", "woke"))
        self.assertTrue(inspects, record)
        self.assertTrue(all(row["kind"] == "inspect" for row in inspects), record)
        self.assertTrue(all(row["class"] == "not-found" for row in inspects[:-1]), record)
        self.assertIn(inspects[-1]["class"], ("not-found", "cut"), record)

    def test_idle_miss_then_answer_runs_by_id(self):
        fixture, docker, log = self.stub(inspect=[self.MISS, (0, 0, self.IMAGE + " linux/arm64", "")])
        self.full(fixture, docker)
        self.assertEqual(self.commands(log), ["info", "ps", "inspect", "inspect", "run"], self.rows(log))
        argv = self.rows(log)[-1]["argv"]
        self.assertEqual(argv[argv.index("python3") - 1], self.IMAGE)
        self.assertIn("ADT_IMAGE_ID=" + self.IMAGE, argv)
        self.assertEqual(argv.count("--pull=never"), 1)
        self.assertNotIn(self.TAG, argv)
        record = self.image_record(fixture)
        self.assertEqual(record["result"], "present")
        self.assertEqual([row["class"] for row in record["probes"]], ["woke", "not-found", "answer"])

    def test_inconclusive_lookups_stop_without_building(self):
        for case, kwargs, message in [
            ("wake", {"wake": (0, 1, "", "wake refused"), "inspect": [self.MISS]},
             "wake failed: wake refused"),
            ("transport", {"inspect": [(0, 1, "", "Cannot connect to the Docker daemon at "
                                       "unix:///var/run/docker.sock. Is the docker daemon running?")]},
             "unresolved"),
            ("timeout", {"inspect": [(2.0, 1, "", "")]}, "inspect timeout"),
            ("malformed", {"inspect": [(0, 0, "not an image record", "")]}, "unresolved"),
            ("foreign", {"inspect": [(0, 0, self.OTHER + " linux/amd64", "")]}, "foreign"),
            ("info", {"info": (1.05, 0, "arm64", ""), "inspect": [self.MISS]}, "platform"),
        ]:
            with self.subTest(case=case):
                fixture, docker, log = self.stub(**kwargs)
                timings = (3.0, 0.5, 0.05) if case == "timeout" else ()
                status, err = self.full(fixture, docker, timings=timings)
                self.assertTrue({"build", "pull", "run"}.isdisjoint(self.commands(log)), self.rows(log))
                self.assertEqual(status, 2, err)
                self.assertIn(message, err)
                if case == "wake":
                    self.assertEqual(self.commands(log), ["info", "ps"])
                    probes = self.image_record(fixture)["probes"]
                    self.assertEqual([(row["kind"], row["class"]) for row in probes],
                                     [("wake", "failed")])
                elif case == "timeout":
                    _, first, *later = self.image_record(fixture)["probes"]
                    self.assertEqual((first["kind"], first["class"]), ("inspect", "timeout"))
                    self.assertTrue(all(row["class"] in ("timeout", "cut") for row in later), later)

    def test_image_pin_mismatch_stops_before_any_container(self):
        fixture, docker, log = self.stub()
        status, err = self.full(fixture, docker, "--image", self.OTHER)
        self.assertEqual(status, 2, err)
        self.assertIn(self.IMAGE, err)
        self.assertIn(self.OTHER, err)
        self.assertNotIn("run", self.commands(log))
        rc.STOP.clear()
        with self.timings():
            for state in ("present", "unresolved"):
                inspected = None if state == "present" else [self.MISS]
                _, probe, _ = self.stub(inspect=inspected)
                record = rc.resolve_image(str(probe), "linux/arm64")
                self.assertEqual(record.result, state)
                with self.assertRaises(rc.SetupError):
                    rc.pinned(record, self.OTHER)

    def test_image_build_needs_the_statement_and_refuses_a_resolving_tag(self):
        for case, inspect, statement, expected in [
            ("no-statement", None, False, 2),
            ("present", None, True, 2),
            ("foreign", [(0, 0, self.OTHER + " linux/amd64", "")], True, 2),
            ("unresolved", [self.MISS], True, 0),
        ]:
            with self.subTest(case=case):
                fixture, docker, log = self.stub(inspect=inspect)
                if case == "unresolved":
                    with self.post_build_answer(docker, self.IMAGE + " linux/arm64"):
                        status = self.image_build(fixture, docker)
                else:
                    status = self.image_build(fixture, docker, statement=statement)
                self.assertEqual(status, expected)
                commands = self.commands(log)
                if not statement:
                    self.assertEqual(commands, [])
                else:
                    before = json.loads((fixture.out / "before.json").read_text())
                    self.assertEqual(before["record"]["result"], case)
                builds = [row for row in self.rows(log) if row["command"] == "build"]
                if expected == 0:
                    self.assertEqual(len(builds), 1)
                    self.assertEqual(builds[0]["argv"],
                                     ["build", "-q", "--platform", "linux/arm64", "-t", self.TAG, "-"])
                    self.assertTrue(builds[0]["before"])
                    after = json.loads((fixture.out / "after.json").read_text())
                    self.assertEqual((after["build_exit"], after["record"]["result"]), (0, "present"))
                else:
                    self.assertEqual(builds, [])

    @contextlib.contextmanager
    def post_build_answer(self, docker, answer):
        script = docker.read_text().replace(
            'if command == "inspect":',
            f'if command == "inspect" and any(row["command"] == "build" for row in prior):\n'
            f'    print({answer!r})\n    raise SystemExit(0)\nif command == "inspect":',
        )
        docker.write_text(script)
        yield

    def test_docker_args_run_by_immutable_id(self):
        repo = Path("/Users/fixture/worktree")
        args = rc.docker_args(repo, repo / ".git", repo / "out", self.IMAGE, "abc123")
        self.assertEqual(args.count("--pull=never"), 1)
        self.assertEqual(args[args.index("python3") - 1], self.IMAGE)
        with self.assertRaises(ValueError):
            rc.docker_args(repo, repo / ".git", repo / "out", self.TAG, "abc123")

    def test_testing_md_states_the_image_rule(self):
        doc = (REPO / "tools" / "TESTING.md").read_text()
        self.assertIn("image build --operator-verified-absent", doc)
        self.assertIn("check never builds", doc)

    def test_full_owns_the_pin_in_mac_leg_steps(self):
        for args, wanted in [(("--image", self.IMAGE), self.IMAGE), ((), None)]:
            with self.subTest(args=args):
                fixture = FixtureRepo(self)
                fixture.module("test_pin", f"""
                    class T(unittest.TestCase):
                        def test_pin(self):
                            self.assertEqual(os.environ.get("ADT_RELEASE_CHECK_IMAGE"), {wanted!r})
                """)
                status, err = fixture.run(*args, environ={**os.environ, "ADT_RELEASE_CHECK_IMAGE": self.THIRD})
                self.assertEqual(status, 0, (err, fixture.results()))

    @contextlib.contextmanager
    def stop_on(self, log, command):
        finished = threading.Event()

        def stop():
            deadline = time.monotonic() + 3
            while not finished.is_set() and time.monotonic() < deadline:
                if command in self.commands(log):
                    rc.STOP.set()
                    return
                finished.wait(0.02)

        watcher = threading.Thread(target=stop)
        watcher.start()
        try:
            yield
        finally:
            finished.set()
            watcher.join()
            rc.STOP.clear()

    def test_resolution_stops_on_interrupt(self):
        resolve_image = rc.resolve_image
        _, docker, log = self.stub(inspect=[(5, 0, self.IMAGE + " linux/arm64", "")])
        with self.timings(window=5, probe=4), self.stop_on(log, "inspect"):
            start = time.monotonic()
            with self.assertRaises(rc.Stopped):
                resolve_image(str(docker), "linux/arm64")
            self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(alive(self.rows(log)[-1]["pid"]))

    def test_image_build_keeps_its_record_when_the_write_goes_wrong(self):
        for case in ("build-failed", "unresolved", "foreign", "stopped"):
            with self.subTest(case=case):
                fixture, docker, log = self.stub(
                    inspect=[self.MISS],
                    build=(5 if case == "stopped" else 0, int(case == "build-failed"), "", "build stderr"),
                )
                with contextlib.ExitStack() as stack:
                    if case == "foreign":
                        stack.enter_context(self.post_build_answer(docker, self.OTHER + " linux/amd64"))
                    elif case == "stopped":
                        stack.enter_context(self.stop_on(log, "build"))
                    status = self.image_build(fixture, docker)
                self.assertEqual(status, 1)
                before = json.loads((fixture.out / "before.json").read_text())
                self.assertTrue(before["operator_verified_absent"])
                self.assertEqual(before["record"]["result"], "unresolved")
                after = json.loads((fixture.out / "after.json").read_text())
                builds = [row for row in self.rows(log) if row["command"] == "build"]
                self.assertEqual(len(builds), 1)
                self.assertTrue(builds[0]["before"])
                if case == "stopped":
                    self.assertEqual(after["stopped"], "build")
                    self.assertFalse(alive(builds[0]["pid"]))
                elif case == "build-failed":
                    self.assertEqual(after["build_exit"], 1)
                    self.assertIn("build stderr", after["build_stderr"])
                else:
                    self.assertEqual(after["build_exit"], 0)
                    self.assertEqual(after["record"]["result"], case)

    def test_shared_window_cuts_a_late_inspect(self):
        fixture, docker, log = self.stub(inspect=[self.MISS, (1.3, 0, self.IMAGE + " linux/arm64", "")])
        status, err = self.full(fixture, docker, timings=(2.0, 1.6, 1.0))
        self.assertTrue({"build", "pull", "run"}.isdisjoint(self.commands(log)), self.rows(log))
        self.assertEqual(status, 2, err)
        self.assertEqual(self.commands(log), ["info", "ps", "inspect", "inspect"])
        record = self.image_record(fixture)
        self.assertEqual(record["result"], "unresolved")
        self.assertFalse(alive(self.rows(log)[-1]["pid"]))
        self.assertEqual([(row["kind"], row["class"]) for row in record["probes"]],
                         [("wake", "woke"), ("inspect", "not-found"), ("inspect", "cut")], err)
        self.assertIn("all not-found; last inspect cut at the window's end", err)


class DockerArgs(unittest.TestCase):

    def test_mount_args_are_readonly_binds(self):
        worktree = Path("/Users/someone/worktrees/w")
        common = Path("/Users/someone/repo/.git")
        args = rc.docker_args(worktree, common, Path("/Users/someone/out"), "sha256:" + "a" * 64, "abc123",
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

    def test_linux_leg_names_the_acl_folder(self):
        # Breaks: a Linux leg whose ACL tests have no folder that accepts a default ACL.
        args = rc.docker_args(Path("/Users/someone/worktrees/w"), Path("/Users/someone/repo/.git"),
                              Path("/Users/someone/out"), "sha256:" + "a" * 64, "abc123", platform="linux/arm64")
        values = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-e"]
        self.assertEqual(values.count("RELAY_ENGINE_ACL_DIR=/var/tmp"), 1, args)
        self.assertEqual(args.count("RELAY_ENGINE_ACL_DIR=/var/tmp"), 1, args)


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
        docker = docker_binary()
        if not docker:
            raise rc.SetupError("docker not found")
        cls.platform = rc.native_platform(docker)
        record = rc.resolve_image(docker, cls.platform)
        cls.image = rc.pinned(record, os.environ.get(rc.PIN_ENV))

    def fixture(self, *, linked=False):
        return LinuxFixture(self, linked=linked, image=self.image)

    def readonly_args(self, fixture, expected_head=None):
        common = rc.common_git_dir(fixture.repo)
        return rc.docker_args(fixture.repo, common, fixture.out, self.image,
                              expected_head or fixture.head, platform=self.platform)

    def test_preflight_rejects_writable_mount(self):
        fixture = self.fixture().finish()
        args = self.readonly_args(fixture)
        writable = [a.replace(f"dst={fixture.repo},readonly", f"dst={fixture.repo}") for a in args]
        result = fixture.container(writable)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"source mount {fixture.repo} accepts a write", result.stdout + result.stderr)
        control = fixture.container(args)
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
        self.assertIn("preflight: clean", control.stdout)

    def test_preflight_rejects_missing_history(self):
        fixture = self.fixture().finish()
        code, err = fixture.run_legs("linux", history=("0123abc",))
        self.assertEqual(code, 2, err)
        self.assertIn("git show 0123abc: fails in the container",
                      (fixture.out / "linux" / "docker.log").read_text())
        fixture.out = fixture.out.parent / (fixture.out.name + "-control")
        self.addCleanup(shutil.rmtree, fixture.out, True)
        code, err = fixture.run_legs("linux", history=(fixture.head[:7],))
        self.assertEqual(code, 0, err)

    def test_container_runs_as_host_uid(self):
        fixture = self.fixture()
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
        fixture = self.fixture()
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
        fixture = self.fixture().finish()
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
        fixture = self.fixture()
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
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        step = next(s for s in linux["steps"] if s["id"] == "relay_engine.tests.test_b_orphan")
        self.assertEqual(step["outcome"], "fail")

    def test_scratch_is_executable_on_linux(self):
        fixture = self.fixture()
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
        fixture = self.fixture().finish()
        code, err = fixture.run_legs("linux")
        self.assertEqual(code, 0, err)
        native = subprocess.run([docker_binary(), "info", "--format", "{{.Architecture}}"],
                                capture_output=True, text=True, check=True).stdout.strip()
        linux = json.loads((fixture.out / "linux" / "results.json").read_text())
        self.assertEqual(linux["machine"], native)
        self.assertTrue(linux["image"].startswith("sha256:"), linux["image"])

    def test_linux_sweep_stops_setsid_daemon(self):
        fixture = self.fixture()
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
        fixture = self.fixture()
        for name, body in modules.items():
            fixture.module(name, body)
        fixture.finish()
        driver = start_driver(fixture, "full", "--leg", "linux", "--workers", str(len(modules)),
                              "--out", str(fixture.out), "--image", self.image)
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
        argv = [docker_binary(), "run", "-d", "--rm", "--pull=never", "--label",
                f"{rc.RUN_LABEL}=bystander-{os.getpid()}", self.image, "sleep", "300"]
        self.assertEqual(argv.count("--pull=never"), 1)
        self.assertEqual(argv[argv.index("--label") + 2], self.image)
        self.assertNotIn(rc.image_tag(), argv)
        bystander = subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
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
        fixture = self.fixture()
        fixture.module("test_b_slow", self.STARTS.format(name="test_b_slow"))
        fixture.finish()
        mark = fixture.pids / "docker-run-called"
        docker = self.fake_docker(fixture, f": > {mark}\nsleep 3")
        driver = start_driver(fixture, "full", "--leg", "linux", "--out", str(fixture.out),
                              "--image", self.image, env={**os.environ, "DOCKER": str(docker)})
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
        fixture = self.fixture()
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
        fixture = self.fixture().finish()
        code, err = fixture.run_legs("mac")
        self.assertEqual(code, 0, err)
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertEqual((summary["release"], summary["cap_seconds"], set(summary["legs"])),
                         (False, 1800, {"mac"}))
        # Control for the release refusals: both legs and the full cap start and pass.
        fixture.out = fixture.out.parent / (fixture.out.name + "-release")
        self.addCleanup(shutil.rmtree, fixture.out, True)
        with mock.patch.object(rc, "RELEASE_POLICY", ()):
            code, err = fixture.run_legs("both", extra=("--release", "--cap-seconds", "1800"))
        self.assertEqual(code, 0, err)
        summary = json.loads((fixture.out / "release-check.json").read_text())
        self.assertEqual((summary["release"], summary["cap_seconds"], set(summary["legs"])),
                         (True, 1800, {"mac", "linux"}))

    def test_sigkill_cleanup_finds_leftovers(self):
        fixture = self.fixture()
        fixture.module("test_b_slow", self.STARTS.format(name="test_b_slow"))
        fixture.finish()
        driver = start_driver(fixture, "full", "--leg", "both", "--workers", "2",
                              "--out", str(fixture.out), "--image", self.image)
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
        fixture = self.fixture().finish()
        base = fixture.out
        for name, exit_code, message in (
            ("killed", 137, "container exited 137, not the runner's 0, 1 or 2"),
            ("daemon-error", 125, "container exited 125, not the runner's 0, 1 or 2"),
            ("no-results", 0, "exited 0 but wrote no"),
        ):
            with self.subTest(name=name):
                fixture.out = base.parent / f"{base.name}-{name}"
                self.addCleanup(shutil.rmtree, fixture.out, True)
                log = fixture.pids / f"{name}-runs"
                docker = self.fake_docker(fixture, f'echo run >> "{log}"\nexit {exit_code}')
                code, err = fixture.run_legs("linux", environ={**os.environ, "DOCKER": str(docker)})
                self.assertEqual(code, 2, err)
                summary = json.loads((fixture.out / "release-check.json").read_text())
                self.assertEqual(summary["legs"]["linux"], {"exit": 2, "results": None})
                self.assertEqual(log.read_text().splitlines(), ["run"])
                self.assertIn(message, err)

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
        fixture = self.fixture(linked=True).finish(linked=True)
        self.assertNotEqual(rc.common_git_dir(fixture.repo), fixture.repo / ".git")
        result = fixture.container(self.readonly_args(fixture))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("preflight: clean", result.stdout + result.stderr)

    def test_linux_only_failure_fails_whole_check(self):
        fixture = self.fixture()
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

    def test_real_tree_release_policy(self):
        # Expected literals and reasons transcribed from the reviewed 142543 comparison.
        root = "tools/relay_engine/tests/"
        names = (
            "test_xroot", "test_xroot_b9_peer", "test_xroot_cache", "test_xroot_cache_differential",
            "test_xroot_deadline", "test_xroot_narrow", "test_xroot_narrow_corpus", "test_xroot_reader",
            "test_xroot_spawn", "test_b9_fold1", "test_b9_fold2", "test_b9_fold3", "test_b9_fold4",
            "test_authority_b9", "test_authority_b9_ancestors",
        )
        paths = ("tools/check-index-oracle.py", "tools/check-t11-closure.py",
                 "tools/check-dispatch-head.py", "tools/check-s375-escaped-rows.py")
        reason = "R250.5: argument-driven v2.9 oracle called by nothing (audit Q1); removed at DD stage 4"
        expected = {path: reason for path in paths}
        expected["tools/mtbattery.py"] = (
            "R250.5: mutation battery retired once the live matrix cases move (operator O5); "
            "removed at DD stage 4"
        )
        expected.update({
            root + name + ".py": (
                "R250.5: cross-root reader replaced by brief 5.9 (operator O1); removed with its code "
                "at DD stage 3; excluded so a leg's cap measures surviving code"
            ) for name in names
        })
        steps = rc.step_table(REPO)
        exclusions, problems = rc.release_preflight(REPO, steps, rc.EXCLUSIONS)
        self.assertEqual(problems, [])
        self.assertEqual([(entry.path, entry.reason) for entry in exclusions], sorted(expected.items()))
        expected_omissions = sorted("relay_engine.tests." + name for name in names)
        observed = {}
        for leg in ("mac", "linux"):
            selected = rc._selected_steps(steps, leg, exclusions, exact_exclusions=True)
            ids = [step.id for step in selected]
            offered = [step.id for step in steps if leg in step.legs]
            omitted = sorted(set(offered) - set(ids))
            self.assertEqual(omitted, expected_omissions)
            self.assertEqual("relay_engine.tests.test_pdc_consumer" in ids, leg == "mac")
            observed[leg] = {"selected": ids, "omitted": omitted}

        evidence = os.environ.get("ADT_RELEASE_POLICY_TEST_EVIDENCE")
        if evidence:
            (Path(evidence) / "real-tree-policy.json").write_text(json.dumps({
                "expected_paths_and_reasons": expected,
                "observed_paths_and_reasons": {entry.path: entry.reason for entry in exclusions},
                "expected_omissions_per_leg": expected_omissions,
                "observed": observed,
            }, indent=2) + "\n")

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

    def test_linux_runs_name_the_acl_folder(self):
        # Breaks: a contributor's Linux run of the suite that fails the ACL tests unexplained.
        everyday = TESTING_MD.read_text().split("## Everyday", 1)[1].split("\n## ", 1)[0]
        commands = (REPO / "AGENTS.md").read_text().split("## Commands", 1)[1].split("\n## ", 1)[0]
        for name, section in (("tools/TESTING.md Everyday", everyday), ("AGENTS.md Commands", commands)):
            with self.subTest(section=name):
                self.assertIn("RELAY_ENGINE_ACL_DIR", section)

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
    # minus the runner's exclusion table); reader counts may shrink as modules retire.
    MEASURED_ROWS = {"`tools/relay-lint.py`", "`tools/relay-engine-fixtures/**`",
                     "`tools/relay-lint-fixtures/**`", "`tools/check-relay-lint-fixtures.py`",
                     "`tools/relay`", "`tools/measure-plan-shape.py`", "`tools/**`"}

    def row(self, glob):
        lines = [line for line in trap_lines(TESTING_MD.read_text()) if line.split(" → ")[0] == f"- {glob}"]
        self.assertEqual(len(lines), 1, glob)
        return TICKED.findall(lines[0].split(" → ", 1)[1])

    def test_trap_rows_follow_the_measured_rule(self):
        for glob in self.MEASURED_ROWS:
            targets = self.row(glob)
            self.assertEqual(len(set(targets)), len(targets), glob)
            excluded = [t for t in targets if any(fnmatch.fnmatchcase(f"tools/relay_engine/tests/{t}.py", e.path)
                                                  or fnmatch.fnmatchcase(f"tools/{t}", e.path)
                                                  for e in rc.EXCLUSIONS)]
            self.assertEqual(excluded, [], glob)
        self.assertIn("test-adapter.sh", self.row("`tools/relay_engine/*.py`"))
        self.assertEqual(set(self.row("`tools/**`")),
                         {"test_version", "test_identity_matrix", "test_mtbattery_guard", "check-generate-plugins.py"})
        self.assertNotIn("capture_order_baseline", TESTING_MD.read_text())

    def test_trap_row_drop_is_not_a_failure(self):
        text = TESTING_MD.read_text()
        row = next(line for line in trap_lines(text)
                   if line.startswith("- `tools/relay-lint.py` → "))
        dropped = text.replace(row, row.replace(", `test_version`", "", 1), 1)
        self.assertNotEqual(dropped, text)
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "TESTING.md"
            document.write_text(dropped)
            with mock.patch(__name__ + ".TESTING_MD", document):
                self.test_trap_rows_follow_the_measured_rule()
                self.test_testing_md_trap_list_resolves()

    def test_trap_row_duplicate_or_excluded_is_a_failure(self):
        text = TESTING_MD.read_text()
        row = next(line for line in trap_lines(text)
                   if line.startswith("- `tools/relay-lint.py` → "))
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "TESTING.md"
            for target in ("test_version", "mtbattery.py"):
                with self.subTest(target=target):
                    document.write_text(text.replace(row, row + f", `{target}`", 1))
                    with mock.patch(__name__ + ".TESTING_MD", document):
                        with self.assertRaises(AssertionError):
                            self.test_trap_rows_follow_the_measured_rule()

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
