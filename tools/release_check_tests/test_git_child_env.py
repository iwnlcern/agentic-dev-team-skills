"""Git children must use the named repository despite a caller's GIT_DIR."""
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from test_release_check import LinuxFixture

def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return env


def make_caller(base: Path) -> tuple[Path, Path, str]:
    main, linked = base / "caller", base / "caller-wt"
    main.mkdir()
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.name", "environment fixture"],
                 ["config", "user.email", "environment@example.invalid"]):
        subprocess.run(["git", "-C", str(main), *args], check=True,
                       capture_output=True, env=clean_env())
    (main / "caller.txt").write_text("caller\n")
    for args in (["add", "-A"], ["commit", "-qm", "caller"],
                 ["worktree", "add", "-q", "--detach", str(linked), "HEAD"]):
        subprocess.run(["git", "-C", str(main), *args], check=True,
                       capture_output=True, env=clean_env())
    folder = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--absolute-git-dir"],
        check=True, capture_output=True, text=True, env=clean_env()).stdout.strip()
    return main, linked, folder


def caller_state(main: Path, linked: Path) -> tuple:
    answers = []
    for repo, args in (
            (linked, ["rev-parse", "HEAD"]), (main, ["rev-parse", "HEAD"]),
            (linked, ["write-tree"]), (main, ["worktree", "list", "--porcelain"]),
            (main, ["for-each-ref"])):
        answers.append(subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True,
            text=True, env=clean_env()).stdout.strip())
    return (*answers[:3], hashlib.sha256((main / ".git/config").read_bytes()).hexdigest(),
            *answers[3:])


def make_target(base: Path) -> Path:
    target = base / "target"
    target.mkdir()
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.name", "environment fixture"],
                 ["config", "user.email", "environment@example.invalid"]):
        subprocess.run(["git", "-C", str(target), *args], check=True,
                       capture_output=True, env=clean_env())
    (target / "target.txt").write_text("target\n")
    for args in (["add", "-A"], ["commit", "-qm", "target"]):
        subprocess.run(["git", "-C", str(target), *args], check=True,
                       capture_output=True, env=clean_env())
    return target


class TestGitChildEnv(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-child-env-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.main, self.linked, self.git_dir = make_caller(self.base)

    def checked_site(self, call, postcondition, error_type=subprocess.CalledProcessError):
        before = caller_state(self.main, self.linked)
        result, caught = None, None
        with mock.patch.dict(os.environ, dict(clean_env(), GIT_DIR=self.git_dir), clear=True):
            try:
                result = call()
            except error_type as error:
                caught = error
        self.assertEqual(caller_state(self.main, self.linked), before,
                         "site changed caller state")
        postcondition(result)
        self.assertIsNone(caught, str(caught))
        return result

    def test_release_fixture_keeps_caller(self):
        # Breaks: FixtureRepo.commit or finish's worktree-add retains GIT_DIR.
        first = LinuxFixture(self, linked=True)
        self.checked_site(first.commit,
                          lambda _: self.assertTrue((first.root / ".git").is_dir()))
        second = LinuxFixture(self, linked=True)
        self.checked_site(lambda: second.finish(linked=True),
                          lambda _: self.assertTrue((second.linked_dir / ".git").is_file()))
