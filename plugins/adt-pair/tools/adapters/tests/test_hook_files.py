"""Hook file placement, chaining, and checkout resolution (hooks design §6)."""

import contextlib
import errno
import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from relay_engine import slips
from tools.adapters import hook_files as hooks


MARKERS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")
GIT = shutil.which("git")
HANG_CEILING_S = 60.0
REAP_S = 10.0


class HookFileTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()
        self.home = self.scratch / "test home"
        self.home.mkdir()
        self.repo = self.scratch / "checkout"
        self.env = {
            key: value for key, value in os.environ.items()
            if key not in MARKERS and not key.startswith("GIT_")
        }
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"))
        env = mock.patch.dict(os.environ, self.env, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.git("init", "--template=", str(self.repo))
        self.common = self.repo / ".git"
        self.folder = self.common / "hooks"
        self.held = os.open(self.common, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.held)

    def git(self, *args, repo=None, data=None):
        command = [GIT]
        if repo is not None:
            command.extend(("-C", str(repo)))
        return subprocess.run(
            [*command, *args], env=self.env, input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

    def config(self, *args):
        self.git("config", *args, repo=self.repo)

    def executable(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    def fake_git(self, body):
        bin_dir = self.scratch / "bin"
        self.executable(bin_dir / "git", body)
        return mock.patch.dict(os.environ, PATH=str(bin_dir) + os.pathsep + self.env["PATH"])

    def snapshot(self, folder):
        if not folder.exists():
            return None
        return {
            path.name: (stat.S_IMODE(path.lstat().st_mode),
                        os.readlink(path) if path.is_symlink() else path.read_bytes())
            for path in folder.iterdir()
        }

    def install(self):
        result = hooks.install_hooks(self.held)
        self.assertIsInstance(result, hooks.Installed)
        return result

    def linked(self, name="linked"):
        path = self.scratch / name
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-m", "fixture", repo=self.repo)
        self.git("worktree", "add", "--quiet", "--detach", str(path), repo=self.repo)
        return path

    def test_absent_default_does_not_redirect(self):
        elsewhere = self.scratch / "external"
        self.executable(elsewhere / "pre-push", "exit 7")
        self.config("core.hooksPath", str(elsewhere))
        contents = self.snapshot(elsewhere)
        expected = hooks.HooksDirElsewhere(".", "local", str(elsewhere))
        self.assertEqual(hooks.install_hooks(self.held), expected)
        self.assertEqual(hooks.inspect_hooks(self.held), expected)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.snapshot(elsewhere), contents)

    def test_hooks_setting_by_scope(self):
        self.assertEqual(hooks.inspect_hooks(self.held).missing, hooks.HOOK_NAMES)
        for value in (str(self.folder), str(self.scratch / "external"), ".husky", "hooks"):
            with self.subTest(value=value):
                self.config("core.hooksPath", value)
                result = hooks.inspect_hooks(self.held)
                if value == str(self.folder):
                    self.assertEqual(result.missing, hooks.HOOK_NAMES)
                else:
                    self.assertEqual(result, hooks.HooksDirElsewhere(".", "local", value))
        with self.subTest(setting="multiline local core.hooksPath"):
            # The last line starts with a different valid scope.
            value = "one\nworktree\tfake"
            self.config("core.hooksPath", value)
            expected = hooks.HooksDirElsewhere(".", "local", value)
            self.assertEqual(hooks.inspect_hooks(self.held), expected)
            self.assertEqual(hooks.install_hooks(self.held), expected)
        self.config("--unset", "core.hooksPath")
        include = self.scratch / "included"
        include.write_text("[core]\n hooksPath = elsewhere\n")
        for setting in ("include.path", f"includeIf.gitdir:{self.common}.path"):
            with self.subTest(setting=setting):
                self.config(setting, str(include))
                self.assertEqual(hooks.inspect_hooks(self.held),
                                 hooks.HooksDirElsewhere(".", "local", "elsewhere"))
                self.config("--unset", setting)
        self.config("--global", "core.hooksPath", "global-hooks")
        self.assertEqual(hooks.inspect_hooks(self.held),
                         hooks.HooksDirElsewhere(".", "global", "global-hooks"))
        self.config("--global", "--unset", "core.hooksPath")
        linked = self.linked()
        self.config("extensions.worktreeConfig", "true")
        self.git("config", "--worktree", "core.hooksPath", "worktree-hooks", repo=linked)
        self.assertEqual(hooks.inspect_hooks(self.held),
                         hooks.HooksDirElsewhere("worktrees/linked", "worktree", "worktree-hooks"))

    def assert_deadline(self, call, is_resolve):
        pidfile = self.scratch / "git.pid"
        version = 'if [ "$1" = --version ]; then echo "git version 2.50.1"; exit 0; fi'
        body = f'{version}\nprintf "%s\\n" "$$" > {shlex.quote(str(pidfile))}\nexec /bin/sleep 30'
        survivor = subprocess.Popen(["/bin/sleep", "30"])
        try:
            with self.fake_git(body):
                start = time.monotonic()
                result = call()
                elapsed = time.monotonic() - start
            kind = "git_no_answer" if is_resolve else "hooks_unreadable"
            self.assertEqual(result.kind, kind)
            self.assertGreaterEqual(elapsed, 9.0)
            self.assertLess(elapsed, 13.0)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
            self.assertIsNone(survivor.poll())
            os.fstat(self.held)
            self.assertFalse(self.folder.exists())
            return result
        finally:
            survivor.terminate()
            survivor.wait()

    def test_child_deadline_kills_its_own(self):
        self.assert_deadline(lambda: hooks.install_hooks(self.held), False)
        with self.fake_git('printf "git version 2.25.9\\n"'):
            self.assertEqual(hooks.install_hooks(self.held), hooks.GitTooOld("2.25.9"))

    def test_hooks_link_refused(self):
        self.install()
        external = self.scratch / "external"
        self.folder.rename(external)
        self.executable(external / "pre-push.adt-local", "exit 7")
        before = self.snapshot(external)
        self.folder.symlink_to(external)
        for operation in (hooks.install_hooks, hooks.inspect_hooks, hooks.remove_hooks):
            self.assertEqual(operation(self.held), hooks.HooksNotAFolder("link", str(external)))
            self.assertEqual(self.snapshot(external), before)
        self.folder.unlink()
        self.folder.write_bytes(b"not a directory")
        for operation in (hooks.install_hooks, hooks.inspect_hooks, hooks.remove_hooks):
            self.assertEqual(operation(self.held), hooks.HooksNotAFolder("file", None))
        self.assertEqual(self.folder.read_bytes(), b"not a directory")
        self.folder.unlink()
        mkdir = os.mkdir

        def link_before_mkdir(path, mode, *, dir_fd=None):
            self.folder.symlink_to(external)
            return mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch.object(hooks.os, "mkdir", side_effect=link_before_mkdir):
            self.assertEqual(hooks.install_hooks(self.held), hooks.HooksNotAFolder("link", str(external)))
        self.assertEqual(self.snapshot(external), before)

    def test_refused_install_writes_nothing(self):
        ref, push = hooks.HOOK_NAMES
        for exists in (False, True):
            if exists:
                self.executable(self.folder / ref, "exit 7")
            before = self.snapshot(self.folder)
            self.config("core.hooksPath", "other")
            self.assertEqual(hooks.install_hooks(self.held).kind, "hooks_dir_elsewhere")
            self.config("--unset", "core.hooksPath")
            with self.fake_git('printf "git version 2.25.9\\n"'):
                self.assertEqual(hooks.install_hooks(self.held).kind, "git_too_old")
            with self.fake_git(
                'if [ "$1" = --version ]; then echo "git version 2.50.1"; exit 0; fi\n'
                'echo unreadable >&2\nexit 128'
            ):
                self.assertEqual(hooks.install_hooks(self.held), hooks.HooksUnreadable("unreadable"))
            self.assertEqual(self.snapshot(self.folder), before)
        self.executable(self.folder / push, "exit 8")
        self.executable(self.folder / (push + ".adt-local"), "exit 9")
        before = self.snapshot(self.folder)
        self.assertEqual(hooks.install_hooks(self.held).kind, "local_hook_conflict")
        self.assertEqual(self.snapshot(self.folder), before)

    def fail_restore(self, name):
        rename = os.rename

        def fail(source, target, **kwargs):
            if source == name + ".adt-local" and target == name:
                raise OSError(errno.EACCES, "injected restore failure")
            return rename(source, target, **kwargs)

        return mock.patch.object(hooks.os, "rename", side_effect=fail)

    def test_remove_failed_partial(self):
        ref, push = hooks.HOOK_NAMES
        self.executable(self.folder / push, "exit 7")
        self.install()
        stub = (self.folder / push).read_bytes()
        with self.fail_restore(push):
            result = hooks.remove_hooks(self.held)
        self.assertEqual(result, hooks.RemoveFailed(
            ((ref, "deleted"),), ((push, "stub"),), hooks.RemoveError("rename", push, errno.EACCES, None),
        ))
        self.assertFalse((self.folder / ref).exists())
        self.assertEqual((self.folder / push).read_bytes(), stub)
        self.assertEqual(hooks.remove_hooks(self.held), hooks.Removed(((push, "restored"),)))
        self.assertEqual((self.folder / push).read_bytes(), b"#!/bin/sh\nexit 7\n")

    def test_remove_conflict_survives_rerun(self):
        name = "pre-push"
        self.executable(self.folder / name, "exit 7")
        self.executable(self.folder / (name + ".adt-local"), "exit 8")
        before = self.snapshot(self.folder)
        for _ in range(2):
            result = hooks.remove_hooks(self.held)
            self.assertEqual(result.remaining, ((name, "conflict"),))
            self.assertEqual(result.error.files, (name, name + ".adt-local"))
            self.assertEqual(result.done, ())
            self.assertEqual(self.snapshot(self.folder), before)

    def test_remove_failed_restore_from_absent(self):
        name = "reference-transaction"
        saved = self.executable(self.folder / (name + ".adt-local"), "exit 7")
        with self.fail_restore(name):
            result = hooks.remove_hooks(self.held)
        self.assertEqual(result.remaining, ((name, "absent"),))
        self.assertEqual(result.error, hooks.RemoveError("rename", name, errno.EACCES, None))
        self.assertTrue(saved.exists())
        self.assertFalse((self.folder / name).exists())
        self.assertEqual(hooks.remove_hooks(self.held), hooks.Removed(((name, "restored"),)))

    def test_hook_writes_follow_the_descriptor(self):
        moved = self.scratch / "moved"
        external = self.executable(self.scratch / "local", "exit 7")
        mkdir = os.mkdir

        def move_before_mkdir(path, mode, *, dir_fd=None):
            self.repo.rename(moved)
            mkdir(path, mode, dir_fd=dir_fd)
            (moved / ".git" / "hooks" / "pre-push").symlink_to(external)

        with mock.patch.object(hooks.os, "mkdir", side_effect=move_before_mkdir):
            result = self.install()
        moved_hooks = moved / ".git" / "hooks"
        self.assertEqual(result.chained, ("pre-push",))
        self.assertFalse(self.repo.exists())
        self.assertEqual(os.readlink(moved_hooks / "pre-push.adt-local"), str(external))
        self.assertEqual((moved_hooks / "pre-push").read_bytes(), hooks.stub_text("pre-push"))

    def test_replaced_clone_hooks_untouched(self):
        self.install()
        moved = self.scratch / "moved"
        replacement = self.scratch / "replacement"
        external = self.scratch / "external"
        self.git("init", "--template=", str(replacement))
        self.git("config", "core.hooksPath", str(external), repo=replacement)
        self.executable(external / "pre-push", "exit 7")
        self.executable(replacement / ".git/hooks/pre-push", "exit 8")
        before = (self.snapshot(external), self.snapshot(replacement / ".git/hooks"),
                  (replacement / ".git/config").read_bytes())
        entered, release = self.scratch / "entered", self.scratch / "release"
        launcher = f"""
if [ "$2" = config ]; then
    : > {shlex.quote(str(entered))}
    while [ ! -e {shlex.quote(str(release))} ]; do /bin/sleep .02; done
fi
exec {shlex.quote(GIT)} "$@"
"""
        failures = []

        def replace():
            try:
                end = time.monotonic() + 5
                while not entered.exists():
                    if time.monotonic() >= end:
                        raise TimeoutError("configuration child did not reach the barrier")
                    time.sleep(.01)
                self.repo.rename(moved)
                replacement.rename(self.repo)
            except Exception as exc:
                failures.append(exc)
            finally:
                release.touch()

        worker = threading.Thread(target=replace)
        worker.start()
        try:
            with self.fake_git(launcher):
                self.install()
        finally:
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(hooks.remove_hooks(self.held).done,
                         tuple((name, "deleted") for name in hooks.HOOK_NAMES))
        self.assertEqual(list((moved / ".git/hooks").iterdir()), [])
        self.assertEqual(before, (self.snapshot(external), self.snapshot(self.folder),
                                  (self.common / "config").read_bytes()))

    def test_install_chains_and_is_idempotent(self):
        ref, push = hooks.HOOK_NAMES
        local = self.executable(self.folder / push, "exit 7").read_bytes()
        self.assertEqual(self.install(), hooks.Installed(hooks.HOOK_NAMES, (push,)))
        before = self.snapshot(self.folder)
        self.assertEqual(self.install().chained, ())
        self.assertEqual(self.snapshot(self.folder), before)
        self.assertEqual((self.folder / (push + ".adt-local")).read_bytes(), local)
        self.assertFalse((self.folder / (ref + ".adt-local")).exists())
        self.executable(self.folder / push, "exit 9")
        before = self.snapshot(self.folder)
        self.assertEqual(hooks.install_hooks(self.held),
                         hooks.LocalHookConflict(push, (push, push + ".adt-local")))
        self.assertEqual(self.snapshot(self.folder), before)

    def test_inspect_names_each_state(self):
        ref, push = hooks.HOOK_NAMES
        self.install()
        path = self.folder / push
        path.unlink()
        self.assertEqual(hooks.inspect_hooks(self.held).missing, (push,))
        path.write_bytes(hooks.stub_text(push))
        self.assertEqual(hooks.inspect_hooks(self.held).not_current, ((push, "not-executable"),))
        lines = hooks.stub_text(push).splitlines(keepends=True)
        lines[1] = lines[1].replace(str(hooks.STUB_VERSION).encode(), b"0")
        path.write_bytes(b"".join(lines))
        path.chmod(0o755)
        (self.folder / ref).unlink()
        (self.folder / ref).mkdir()
        self.assertEqual(hooks.inspect_hooks(self.held).not_current,
                         ((ref, "not-a-file"), (push, "old-version")))
        (self.folder / ref).rmdir()
        self.assertEqual(self.install().written, hooks.HOOK_NAMES)
        self.assertEqual(path.read_bytes(), hooks.stub_text(push))

    def test_functions_conditions(self):
        self.install()
        before = self.snapshot(self.folder)
        hooks.remove_hooks(self.held)
        cwd = os.getcwd()
        old_mask = os.umask(0o077)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            os.chdir(self.home)
            fds = len(os.listdir("/dev/fd"))
            with mock.patch.dict(os.environ, {
                "HOME": str(self.scratch / "different home"),
                "GIT_DIR": "/not/the/repository",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "different-hooks",
            }), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.install()
                self.assertEqual(fds, len(os.listdir("/dev/fd")))
                self.assertEqual(self.snapshot(self.folder), before)
                for call in (hooks.inspect_hooks, hooks.remove_hooks):
                    call(self.held)
                    self.assertEqual(fds, len(os.listdir("/dev/fd")))
                    os.fstat(self.held)
        finally:
            os.chdir(cwd)
            os.umask(old_mask)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        for mode, content in before.values():
            self.assertEqual(mode, 0o755)
            self.assertIn(b"$HOME/.adt/bin/adt", content)
            self.assertNotIn(os.fsencode(self.home), content)

    def test_remove_without_git(self):
        name = "reference-transaction"
        local = self.executable(self.folder / name, "exit 7").read_bytes()
        for old_git in (False, True):
            self.install()
            if old_git:
                env = self.fake_git('printf "git version 2.25.9\\n"')
            else:
                env = mock.patch.dict(os.environ, PATH=str(self.scratch / "empty"))
            with env:
                if old_git:
                    for call in (hooks.install_hooks, hooks.inspect_hooks):
                        self.assertEqual(call(self.held), hooks.GitTooOld("2.25.9"))
                self.assertEqual(hooks.remove_hooks(self.held).done,
                                 ((name, "restored"), ("pre-push", "deleted")))
            self.assertEqual((self.folder / name).read_bytes(), local)

    def launcher(self, body):
        return self.executable(self.home / ".adt/bin/adt", body)

    def record(self, host, session):
        folder = self.home / ".adt/sessions"
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(session.encode()).hexdigest()
        record = folder / f"{host}-{digest}"
        record.touch()
        return record

    def stop_stub(self, child: subprocess.Popen) -> bool:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
        try:
            child.wait(timeout=REAP_S)
        except subprocess.TimeoutExpired:
            return False
        return True

    def wait_or_fail(self, child: subprocess.Popen, launched: float, *,
                     data: bytes | None) -> tuple[bytes, bytes]:
        try:
            return child.communicate(
                input=data, timeout=HANG_CEILING_S - (time.monotonic() - launched),
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - launched
            load = os.getloadavg()
            reaped = self.stop_stub(child)
            reap_note = "" if reaped else f"; direct child reap exceeded {REAP_S:.1f}s"
            self.fail(f"Stub {child.args!r} exceeded its hang ceiling after {elapsed:.3f}s "
                      f"from launch (load averages {load!r}): host delay or hang, "
                      f"not a verdict on the test's subject{reap_note}")
        except BaseException:
            self.stop_stub(child)
            raise

    def run_stub(self, name, *args, data=b"", command=(), cwd=None, **environment):
        env = dict(self.env)
        for key, value in environment.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        launched = time.monotonic()
        child = subprocess.Popen(
            [*command, str(self.folder / name), *args], env=env, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = self.wait_or_fail(child, launched, data=data)
        return subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)

    def run_stub_with_failed_cleanup(self, name, **environment):
        with tempfile.TemporaryDirectory(dir=self.scratch) as directory:
            scratch = Path(directory)
            receipt = scratch / "cleanup.args"
            fake_bin = scratch / "bin"
            self.executable(
                fake_bin / "rm",
                f"printf '%s %s\\n' \"$@\" >> {shlex.quote(str(receipt))}\n"
                "printf 'rm stand-in failed\\n' >&2\n"
                "exit 1",
            )
            path = environment.get("PATH", self.env["PATH"])
            environment["PATH"] = str(fake_bin) + os.pathsep + path
            result = self.run_stub(name, TMPDIR=scratch, **environment)

            self.assertTrue(receipt.is_file(), "cleanup trap did not record an attempt")
            lines = receipt.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith("-f "))
            staged = Path(lines[0][3:])
            self.assertEqual(staged.parent, scratch)
            self.assertTrue(staged.name.startswith("adt-hook."))
            self.assertTrue(staged.is_file(), "staging file should survive the cleanup failure")
            staged.unlink()
            return result

    def fault_lines(self):
        reason, remedy = slips.unavailable_words("fault")
        return f"refused: {reason} (fault)\nnext: {remedy}; ask operator if you cannot\n".encode()

    def test_chain_first(self):
        local_body = 'printf "%s\\n" "$@"; cat'
        self.executable(self.folder / "pre-push", local_body + "\nexit 17")
        self.executable(self.folder / "reference-transaction", local_body)
        self.launcher('echo "adt started"')
        self.install()
        data = b"one\ntwo\n"
        for env in ({}, {"CODEX_SESSION_ID": "one"}):
            result = self.run_stub("pre-push", "origin", "local", data=data, **env)
            self.assertEqual((result.returncode, result.stdout), (17, b"origin\nlocal\n" + data))
        for state in ("prepared", "committed", "aborted"):
            result = self.run_stub("reference-transaction", state, data=data, CODEX_SESSION_ID="one")
            kit = b"adt started\n" if state == "prepared" else b""
            self.assertEqual((result.returncode, result.stdout), (0, state.encode() + b"\n" + data + kit))

    def test_unmarked_without_adt(self):
        self.install()
        for name in hooks.HOOK_NAMES:
            self.assertEqual(self.run_stub(name, "prepared").returncode, 0)
        self.launcher('echo "should not run"; exit 23')
        for name in hooks.HOOK_NAMES:
            result = self.run_stub(name, "prepared")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.executable(self.folder / "pre-push.adt-local", 'echo local; exit 19')
        result = self.run_stub("pre-push")
        self.assertEqual((result.returncode, result.stdout), (19, b"local\n"))
        local = self.folder / "pre-push.adt-local"
        missing_tmp = self.scratch / "missing tmp"
        data = b"first line\nsecond line\n"
        for status in (0, 1, None):
            with self.subTest(missing_tmp=True, saved_status=status):
                if status is None:
                    local.unlink()
                    expected_input = b""
                else:
                    self.executable(local, f"cat\nexit {status}")
                    expected_input = data
                result = self.run_stub("pre-push", data=data, TMPDIR=missing_tmp)
                expected_status = 0 if status is None else status
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (expected_status, expected_input, b""),
                )

        with self.subTest(preservation="large input"):
            result = self.run_stub("pre-push", data=b"ref update\n" * 20_000)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

        with self.subTest(preservation="real Git"):
            self.assertFalse(local.exists())
            self.assertFalse((self.folder / "reference-transaction.adt-local").exists())
            self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-m", "fixture", repo=self.repo)
            oid = self.git("rev-parse", "HEAD", repo=self.repo).strip().decode()
            refs = {f"refs/heads/test-{index:04d}" for index in range(2_000)}
            updates = "".join(f"create {ref} {oid}\n" for ref in sorted(refs)).encode()
            self.git("update-ref", "--stdin", repo=self.repo, data=updates)
            remote = self.scratch / "bare remote"
            self.git("init", "--bare", "--template=", str(remote))
            self.git("-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                     "push", "--all", str(remote), repo=self.repo)
            received = self.git("for-each-ref", "--format=%(refname)", "refs/heads", repo=remote)
            self.assertEqual(set(received.decode().splitlines()) & refs, refs)

    def test_stub_fault_by_record(self):
        self.install()
        for body in ("exit 1", 'kill -KILL "$$"', None):
            with self.subTest(body=body):
                if body is None:
                    (self.home / ".adt/bin/adt").unlink()
                else:
                    self.launcher(body)
                record = self.record("claude-code", "agent")
                result = self.run_stub("pre-push", CLAUDE_CODE_SESSION_ID="agent")
                self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))
                record.unlink()
                result = self.run_stub("pre-push", CLAUDE_CODE_SESSION_ID="agent")
                self.assertEqual((result.returncode, result.stderr), (0, b""))
        with self.subTest(record="dangling symlink", launcher="absent"):
            record = self.record("claude-code", "agent")
            record.unlink()
            record.symlink_to(self.home / "missing-session-record")
            result = self.run_stub("pre-push", CLAUDE_CODE_SESSION_ID="agent")
            self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))
            record.unlink()
        self.record("claude-code", "agent")
        self.launcher("echo 'its refusal' >&2; exit 10")
        result = self.run_stub("pre-push", CLAUDE_CODE_SESSION_ID="agent")
        self.assertEqual((result.returncode, result.stderr), (1, b"its refusal\n"))
        with self.subTest(cleanup_failure=True):
            self.launcher("printf '%s\\n' 'its refusal' 'its next step' >&2\nexit 10")
            result = self.run_stub_with_failed_cleanup("pre-push", CLAUDE_CODE_SESSION_ID="agent")
            self.assertEqual((result.returncode, result.stderr), (1, b"its refusal\nits next step\n"))

    def test_stub_marks_by_four_variables(self):
        self.install()
        self.launcher('printf "%s\\n" "$@"')
        for marker in MARKERS:
            with self.subTest(marker=marker):
                result = self.run_stub("pre-push", "origin", **{marker: "set"})
                self.assertEqual((result.returncode, result.stdout), (0, b"hook\npre-push\norigin\n"))
        self.assertEqual(self.run_stub("pre-push").stdout, b"")

    def test_stub_fault_record_by_codex_session_id(self):
        self.install()
        self.launcher("exit 1")
        record = self.record("codex", "session")
        env = {"CODEX_SESSION_ID": "session", "CODEX_THREAD_ID": "thread"}
        result = self.run_stub("pre-push", **env)
        self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))
        record.unlink()
        self.record("codex", "thread")
        result = self.run_stub("pre-push", **env)
        self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_stub_is_sh(self):
        self.install()
        for name in hooks.HOOK_NAMES:
            self.assertEqual((self.folder / name).read_bytes().splitlines()[0], b"#!/bin/sh")
            result = self.run_stub(name, "prepared", command=("/bin/sh", "-u"))
            self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_stub_runs_with_space_in_home(self):
        self.install()
        self.assertIn(" ", str(self.home))
        self.launcher("printf '%s\\n' started")
        result = self.run_stub("pre-push", CODEX_SESSION_ID="agent")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"started\n", b""))

    def test_stub_home_unset(self):
        self.install()
        self.record("codex", "agent")
        command = ("/bin/sh", "-u")
        for home in (None, "", "relative/home"):
            with self.subTest(marked=True, home=home):
                result = self.run_stub("pre-push", command=command, cwd=self.scratch,
                                       HOME=home, CODEX_SESSION_ID="agent")
                self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))

        with self.subTest(marked=True, decoy=True):
            marker = self.scratch / "decoy started"
            self.executable(self.scratch / "relative/home/.adt/bin/adt",
                            f"touch {shlex.quote(str(marker))}")
            result = self.run_stub("pre-push", command=command, cwd=self.scratch,
                                   HOME="relative/home", CODEX_SESSION_ID="agent")
            self.assertFalse(marker.exists(), "relative HOME launched the decoy")
            self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))

        local = self.folder / "pre-push.adt-local"
        for status in (0, 1):
            with self.subTest(preservation="unmarked HOME", saved_status=status):
                self.executable(local, f"printf 'saved once\\n'\nexit {status}")
                result = self.run_stub("pre-push", command=command, HOME=None)
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (status, b"saved once\n", b""),
                )

    def test_stub_staging_failure_is_a_fault(self):
        marker = self.scratch / "saved started"
        self.executable(self.folder / "pre-push", f"touch {shlex.quote(str(marker))}")
        self.install()
        tmp = self.scratch / "valid tmp"
        tmp.mkdir()
        fake_bin = self.scratch / "bin"
        self.executable(fake_bin / "cat", "printf 'copy failed\\n' >&2\nexit 1")
        copy_path = str(fake_bin) + os.pathsep + self.env["PATH"]
        for has_record in (False, True):
            if has_record:
                self.record("codex", "agent")
            for failure, environment in (
                ("mktemp", {"TMPDIR": self.scratch / "missing tmp"}),
                ("copy", {"TMPDIR": tmp, "PATH": copy_path}),
            ):
                with self.subTest(has_record=has_record, failure=failure):
                    result = self.run_stub("pre-push", data=b"ref update\n",
                                           CODEX_SESSION_ID="agent", **environment)
                    self.assertFalse(marker.exists(), "saved hook ran without a complete copy")
                    self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))

            with self.subTest(has_record=has_record, cleanup_failure=True):
                result = self.run_stub_with_failed_cleanup(
                    "pre-push", data=b"ref update\n", CODEX_SESSION_ID="agent", PATH=copy_path,
                )
                self.assertFalse(marker.exists(), "saved hook ran without a complete copy")
                self.assertEqual((result.returncode, result.stderr), (1, self.fault_lines()))

    def test_saved_hook_ignoring_input_does_not_hang(self):
        self.executable(self.folder / "pre-push", "exit 0")
        self.launcher("cat")
        self.install()
        data = b"input line " * 15 + b"\n"
        data *= 20_000
        result = self.run_stub("pre-push", data=data, CLAUDECODE="1")
        self.assertEqual((result.returncode, result.stdout), (0, data))

    def test_large_input_reaches_both_readers(self):
        saved = self.scratch / "saved input"
        self.executable(self.folder / "pre-push", f"cat > {shlex.quote(str(saved))}")
        self.launcher("cat")
        self.install()
        data = b"".join(f"line {n}\n".encode() for n in range(20_000))
        result = self.run_stub("pre-push", data=data, CODEX_THREAD_ID="thread")
        self.assertEqual((result.returncode, result.stdout), (0, data))
        self.assertEqual(saved.read_bytes(), data)

    def test_concurrent_stubs_use_distinct_input_files(self):
        first = self.linked("first")
        # Worktree add needs only the existing commit from the first worktree.
        second = self.scratch / "second"
        self.git("worktree", "add", "--quiet", "--detach", str(second), repo=self.repo)
        self.executable(self.folder / "pre-push", 'cat > "$PWD/saved-input"')
        receipt = self.scratch / "temp paths"
        fake_bin = self.scratch / "bin"
        real_mktemp = shlex.quote(shutil.which("mktemp"))
        self.executable(fake_bin / "mktemp",
                        f'p=$({real_mktemp} "$@") || exit $?\n'
                        f'printf "%s\\n" "$p" >> {shlex.quote(str(receipt))}\n'
                        'printf "%s\\n" "$p"')
        self.launcher("cat")
        self.install()
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"], CLAUDECODE="1")
        # Use disk-backed, independent pipes for output to avoid serial pipe drains.
        with contextlib.ExitStack() as stack:
            runs = []
            unwaited = set()

            def stop_if_unwaited(child):
                if child in unwaited:
                    self.stop_stub(child)

            for worktree, data in ((first, b"first\n" * 20_000), (second, b"second\n" * 20_000)):
                source = stack.enter_context(tempfile.TemporaryFile())
                destination = stack.enter_context(tempfile.TemporaryFile())
                source.write(data)
                source.seek(0)
                launched = time.monotonic()
                proc = subprocess.Popen(
                    [str(self.folder / "pre-push")], cwd=worktree, env=env,
                    stdin=source, stdout=destination, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                unwaited.add(proc)
                stack.callback(stop_if_unwaited, proc)
                runs.append((proc, launched, worktree, data, destination))
            for proc, launched, worktree, data, destination in runs:
                unwaited.remove(proc)
                _, stderr = self.wait_or_fail(proc, launched, data=None)
                self.assertEqual((proc.returncode, stderr), (0, b""))
                destination.seek(0)
                self.assertEqual(destination.read(), data)
                self.assertEqual((worktree / "saved-input").read_bytes(), data)
        paths = receipt.read_text().splitlines()
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_resolve_absolute_and_relative_common_dir(self):
        linked = self.linked()
        for path in (self.repo, linked):
            self.assertEqual(hooks.resolve_checkout_path(str(path)),
                             hooks.ResolvedCheckout(str(self.common), str(path)))

    def test_resolve_symlinked_path_gives_real_paths(self):
        alias = self.scratch / "symlink"
        alias.symlink_to(self.repo)
        self.assertEqual(hooks.resolve_checkout_path(str(alias)),
                         hooks.ResolvedCheckout(str(self.common), str(self.repo)))

    def test_resolve_plain_folder_and_bare_repo_not_a_checkout(self):
        bare = self.scratch / "bare"
        self.git("init", "--bare", "--template=", str(bare))
        for path, message in ((self.home, "not a git repository"), (bare, "must be run in a work tree")):
            result = hooks.resolve_checkout_path(str(path))
            self.assertEqual(result.kind, "not_a_checkout")
            self.assertIn(message, result.git_message)

    def test_resolve_missing_path_git_no_answer(self):
        path = self.scratch / "missing"
        result = hooks.resolve_checkout_path(str(path))
        self.assertEqual(result.kind, "git_no_answer")
        self.assertIn(str(path), result.git_message)

    def test_resolve_child_deadline(self):
        result = self.assert_deadline(lambda: hooks.resolve_checkout_path(str(self.repo)), True)
        self.assertIsNone(result.git_message)

    def test_resolve_ignores_caller_git_env(self):
        with mock.patch.dict(os.environ, GIT_DIR="/absent", GIT_WORK_TREE="/different"):
            self.assertEqual(hooks.resolve_checkout_path(str(self.repo)),
                             hooks.ResolvedCheckout(str(self.common), str(self.repo)))

    def test_resolve_quote_and_newline_path(self):
        path = self.scratch / "quote ' and line\n"
        self.repo.rename(path)
        self.assertEqual(hooks.resolve_checkout_path(str(path)),
                         hooks.ResolvedCheckout(str(path / ".git"), str(path)))

    def test_resolve_relative_path_raises(self):
        with mock.patch.object(hooks.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                hooks.resolve_checkout_path("relative")
        popen.assert_not_called()

    def test_result_kinds_per_function(self):
        expected = {
            "install_hooks": frozenset({
                "installed", "hooks_dir_elsewhere", "hooks_not_a_folder", "local_hook_conflict",
                "hooks_unreadable", "git_too_old",
            }),
            "remove_hooks": frozenset({"removed", "hooks_not_a_folder", "remove_failed"}),
            "inspect_hooks": frozenset({
                "hooks_state", "hooks_dir_elsewhere", "hooks_not_a_folder", "hooks_unreadable",
                "git_too_old",
            }),
            "resolve_checkout_path": frozenset({"resolved", "not_a_checkout", "git_no_answer"}),
        }
        samples = {
            "installed": hooks.Installed((), ()),
            "hooks_dir_elsewhere": hooks.HooksDirElsewhere(".", "local", "other"),
            "hooks_not_a_folder": hooks.HooksNotAFolder("file", None),
            "local_hook_conflict": hooks.LocalHookConflict("pre-push", ("pre-push", "pre-push.adt-local")),
            "hooks_unreadable": hooks.HooksUnreadable("unreadable"),
            "git_too_old": hooks.GitTooOld("2.25.9"),
            "removed": hooks.Removed(()),
            "remove_failed": hooks.RemoveFailed((), (), hooks.RemoveError("fsync", "hooks", 5, None)),
            "hooks_state": hooks.HooksState((), ()),
            "resolved": hooks.ResolvedCheckout("/common", "/top"),
            "not_a_checkout": hooks.ResolveFailure("not_a_checkout", "not a git repository"),
            "git_no_answer": hooks.ResolveFailure("git_no_answer", None),
        }
        self.assertEqual(hooks.RESULT_KINDS, expected)
        for kinds in expected.values():
            self.assertEqual(frozenset(samples[kind].kind for kind in kinds), kinds)

    def test_bounds_constants(self):
        self.assertEqual(hooks.DECISION_CHILD_S, 10.0)
        self.assertEqual(hooks.RESOLVE_CHILD_S, 10.0)
        self.assertEqual(hooks.GIT_FLOOR, (2, 26))
