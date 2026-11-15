"""Git children must use the named repository despite a caller's GIT_DIR."""
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import xrootfixgen as xf
from relay_engine import rules
from relay_engine.tests import test_refs, test_authority_fold10, test_bootstrap_tools
from relay_engine.tests import test_b9_fold1, test_xroot

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

    def test_fixture_generator_keeps_caller(self):
        # Breaks: either generator Git child retains the caller's GIT_DIR.
        target = self.base / "generator"
        self.checked_site(lambda: xf._init_repo(target),
                          lambda _: self.assertTrue((target / ".git").is_dir()))
        (target / "written.txt").write_text("written\n")

        def writing():
            xf._git(target, "add", "-A")
            xf._git(target, "commit", "-q", "-m", "w")

        self.checked_site(writing, lambda _: self.assertTrue(caller_state(target, target)[0]))

    def test_refs_helpers_keep_caller(self):
        # Breaks: either refs-test Git spawn retains the caller's GIT_DIR.
        target = make_target(self.base)
        for call in (
                lambda: test_refs.git(target, "commit", "--allow-empty", "-q", "-m", "w"),
                lambda: test_refs.git_input(target, "commit", "--allow-empty", "-q",
                                            "-F", "-", data=b"w\n")):
            old = caller_state(target, target)[0]
            self.checked_site(call, lambda _: self.assertNotEqual(
                caller_state(target, target)[0], old))

    def test_fold10_helper_keeps_caller(self):
        # Breaks: the Authority helper's Git child retains GIT_DIR.
        target = make_target(self.base)
        old = caller_state(target, target)[0]
        self.checked_site(
            lambda: test_authority_fold10.TestAuthorityFold10.git(
                None, target, "commit", "--allow-empty", "-q", "-m", "w"),
            lambda _: self.assertNotEqual(caller_state(target, target)[0], old))

    def test_bootstrap_make_repo_keeps_caller(self):
        # Breaks: make_repo's nested Git helper retains GIT_DIR.
        helper = test_bootstrap_tools.TestBootstrapTools()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        self.checked_site(lambda: helper.make_repo(census=False),
                          lambda result: self.assertTrue((Path(result[0]) / ".git").is_dir()))

    def test_b9_backend_init_keeps_caller(self):
        # Breaks: the backend probe initializes the caller's git folder.
        target = self.base / "backend"
        self.checked_site(lambda: test_b9_fold1._init_backend(target, "files"),
                          lambda result: (self.assertTrue((target / ".git").is_dir()),
                                          self.assertEqual(result.returncode, 0)))

    def test_cross_root_reader_answers_named_repository(self):
        # Breaks: either reader answers the caller's HEAD instead of target's.
        target = make_target(self.base)
        expected = caller_state(target, target)[0]
        self.assertNotEqual(expected, caller_state(self.main, self.linked)[0])
        for module in (rules, test_xroot._standalone()):
            with self.subTest(module=module.__name__):
                self.checked_site(lambda: module.xroot_git(target, "rev-parse", "HEAD"),
                                  lambda result: self.assertEqual(result, expected),
                                  error_type=module.XrootAxisError)

    def test_local_env_sets_pinned(self):
        # Breaks: a file's removal set changes or Git adds a repository variable.
        expected = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
            "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
            "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
            "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
        }
        root = Path(__file__).resolve().parents[3]
        paths = [
            "tools/xrootfixgen.py", "tools/release_check_tests/test_release_check.py",
            "tools/relay_engine/tests/test_refs.py",
            "tools/relay_engine/tests/test_authority_fold10.py",
            "tools/relay_engine/tests/test_bootstrap_tools.py",
            "tools/relay_engine/tests/test_b9_fold1.py",
            "tools/adapters/tests/test_scope_proof.py",
            "tools/relay_engine/rules.py", "tools/relay-lint.py",
        ]
        reader_expected = {"GIT_CONFIG", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS"}
        reader_paths = {"tools/relay_engine/rules.py", "tools/relay-lint.py"}
        for path in paths:
            with self.subTest(path=path):
                tree = ast.parse((root / path).read_text())
                names = ["_GIT_LOCAL_ENV_VARS"]
                if path in reader_paths:
                    names.append("_XROOT_READER_ENV_VARS")
                sets = {}
                for name in names:
                    assignments = [node for node in tree.body
                                   if isinstance(node, ast.Assign)
                                   and any(isinstance(t, ast.Name) and t.id == name
                                           for t in node.targets)]
                    self.assertEqual(len(assignments), 1)
                    value = assignments[0].value
                    self.assertIsInstance(value, ast.Call)
                    self.assertIsInstance(value.func, ast.Name)
                    self.assertEqual(value.func.id, "frozenset")
                    self.assertEqual(len(value.args), 1)
                    self.assertEqual(value.keywords, [])
                    self.assertIsInstance(value.args[0], ast.Set)
                    self.assertTrue(all(isinstance(n, ast.Constant) and isinstance(n.value, str)
                                        for n in value.args[0].elts))
                    sets[name] = {n.value for n in value.args[0].elts}
                if path in reader_paths:
                    self.assertEqual(sets["_XROOT_READER_ENV_VARS"], reader_expected)
                    self.assertTrue(sets["_GIT_LOCAL_ENV_VARS"].isdisjoint(
                        sets["_XROOT_READER_ENV_VARS"]))
                    self.assertEqual(sets["_GIT_LOCAL_ENV_VARS"] |
                                     sets["_XROOT_READER_ENV_VARS"], expected)
                else:
                    self.assertEqual(sets["_GIT_LOCAL_ENV_VARS"], expected)
        try:
            result = subprocess.run(["git", "rev-parse", "--local-env-vars"],
                                    capture_output=True, text=True, env=clean_env())
        except FileNotFoundError:
            with self.subTest(command="git absent; runtime pin not applicable"):
                self.assertEqual(len(expected), 15)
        else:
            with self.subTest(command=f"git exit {result.returncode}"):
                if result.returncode == 0:
                    self.assertLessEqual(set(result.stdout.splitlines()), expected)
