import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "scope_proof.sh"


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


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env=_git_child_env()
    ).stdout.strip()


class ScopeProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        for rel in ("tools/adapters/a.py", "docs/sprints/x.md", "src/core.py"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("v1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def run_proof(self, *expected, base=None):
        p = subprocess.run(
            ["bash", str(SCRIPT), base or self.base, *expected],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        return p.returncode, p.stdout + p.stderr

    def test_clean_is_ok(self):
        self.assertEqual(self.run_proof(), (0, "scope ok\n"))

    def test_known_untracked_docs_are_ok_but_unexpected_untracked_is_not(self):
        (self.repo / "docs/sprints/new.md").write_text("n\n")
        self.assertEqual(self.run_proof()[0], 0)
        (self.repo / "src/zz.py").write_text("z\n")
        rc, out = self.run_proof()
        self.assertEqual(rc, 1)
        self.assertIn("SCOPE DEVIATION (untracked): src/zz.py", out)
        self.assertNotIn("scope ok", out)

    def test_tracked_doc_change_is_a_deviation(self):
        (self.repo / "docs/sprints/x.md").write_text("v2\n")
        rc, out = self.run_proof()
        self.assertEqual(rc, 1)
        self.assertIn("SCOPE DEVIATION (tracked): docs/sprints/x.md", out)

    def test_staged_doc_is_a_deviation_and_staged_product_is_ok(self):
        (self.repo / "docs/reports/y.md").parent.mkdir(parents=True)
        (self.repo / "docs/reports/y.md").write_text("y\n")
        git(self.repo, "add", "docs/reports/y.md")
        rc, out = self.run_proof()
        self.assertEqual(rc, 1)
        self.assertIn("SCOPE DEVIATION (staged): docs/reports/y.md", out)
        git(self.repo, "reset", "-q")
        (self.repo / "tools/adapters/a.py").write_text("v2\n")
        git(self.repo, "add", "tools/adapters/a.py")
        self.assertEqual(self.run_proof("tools/adapters/a.py")[0], 0)

    def test_staged_set_mismatch_fails(self):
        (self.repo / "tools/adapters/a.py").write_text("v2\n")
        git(self.repo, "add", "tools/adapters/a.py")
        rc, out = self.run_proof("tools/adapters/b.py")
        self.assertEqual(rc, 1)
        self.assertIn("STAGED SET MISMATCH", out)
        self.assertNotIn("scope ok", out)

    def test_extra_allowed_but_unplanned_staged_path_fails(self):
        for rel in ("tools/adapters/a.py", "tools/adapters/extra.py"):
            (self.repo / rel).write_text("v2\n")
            git(self.repo, "add", rel)
        rc, out = self.run_proof("tools/adapters/a.py")
        self.assertEqual(rc, 1)
        self.assertIn("STAGED SET MISMATCH", out)
        self.assertIn("tools/adapters/extra.py", out)

    def test_collection_failure_is_not_ok(self):
        rc, out = self.run_proof(base="0000000000000000000000000000000000000000")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("scope ok", out)


import hashlib
import os
from pathlib import Path
from unittest import mock

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


class GitChildEnvTests(unittest.TestCase):

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

    def test_git_helper_keeps_caller(self):
        # Breaks: the adapter fixture helper's child retains GIT_DIR.
        target = make_target(self.base)
        old = caller_state(target, target)[0]
        self.checked_site(lambda: git(target, "commit", "--allow-empty", "-q", "-m", "w"),
                          lambda _: self.assertNotEqual(caller_state(target, target)[0], old))
