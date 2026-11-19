"""v3 references: spelling, Git resolution through the rules seam, local digests (DD-v3-engine § 6)."""

import hashlib
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from relay_engine import errors
from relay_engine import refs
from relay_engine import rules
from relay_engine.tests.xroot_test_stops import stop


ODD = "range-@@ -01 +1 @@.diff"
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": "2026-11-04T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-04T00:00:00Z",
}


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
    env = dict(_git_child_env(), **_GIT_ENV)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True).stdout.strip()


def git_input(repo, *args, data: bytes) -> None:
    subprocess.run(["git", "-C", str(repo), *args], input=data, check=True,
                   capture_output=True, env=dict(_git_child_env(), **_GIT_ENV))


class Repo:
    """A scratch repository: main holds `files`; branch fix@v2 holds `fix_files`."""

    def __init__(self, files, fix_files=None, links=()):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(os.path.realpath(self._dir.name))
        git(self.path, "init", "-q", "-b", "main")
        self.commit_files(files, links)
        if fix_files is not None:
            git(self.path, "switch", "-q", "-c", "fix@v2")
            self.commit_files(fix_files)
            git(self.path, "switch", "-q", "main")

    def commit_files(self, files, links=()):
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for name, points_to in links:
            os.symlink(points_to, self.path / name)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", "c")

    def close(self):
        self._dir.cleanup()


def clean_git_environment(test):
    """Resolution refuses any GIT_ variable (fold R2), so resolving tests run without them."""
    patcher = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                           if not k.startswith("GIT_")}, clear=True)
    patcher.start()
    test.addCleanup(patcher.stop)


class Refused(unittest.TestCase):
    def refused(self, code, fn, *args):
        with self.assertRaises(errors.EngineError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code, caught.exception.cause)
        return caught.exception


class TestSpelling(Refused):
    def test_split_rule(self):
        text = "bivpak@fix@v2:%s#L1-L3" % ODD
        spec = refs.parse_ref(text)
        self.assertEqual((spec.kind, spec.repo, spec.rev, spec.path,
                          spec.line_start, spec.line_end),
                         ("git", "bivpak", "fix@v2", ODD, 1, 3))
        self.assertEqual(refs.render_ref(spec), text)
        spec = refs.parse_ref("bivpak@main:docs/a#b.md")
        self.assertEqual((spec.path, spec.line_start, spec.line_end), ("docs/a#b.md", None, None))
        spec = refs.parse_ref("bivpak@main:a:b")  # the first colon after the first @
        self.assertEqual((spec.rev, spec.path), ("main", "a:b"))
        spec = refs.parse_ref("bivpak@main:a.md#L7")
        self.assertEqual((spec.path, spec.line_start, spec.line_end), ("a.md", 7, 7))

    def test_relay_and_path_spellings(self):
        uuid = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"
        for text, kind in (("#12", "relay"), ("#12@pdc", "relay"), (uuid, "relay"),
                           ("file:/abs/out.txt", "path"), ("file:~/ev/out.txt", "path")):
            with self.subTest(text=text):
                spec = refs.parse_ref(text)
                self.assertEqual(spec.kind, kind)
                self.assertEqual(refs.render_ref(spec), text)
        self.assertEqual(refs.parse_ref("#12@pdc").relay, "#12@pdc")
        self.assertEqual(refs.parse_ref("file:~/ev/out.txt").path, "~/ev/out.txt")

    def test_round_trip_or_refuse(self):
        for text in ("bivpak@ma in:p", "file:/../x", "file:~/../x",
                     "#012", "#0", "#12@", "#12@PDC", "file:rel/x", "file:/a/../b",
                     "file:/a//b", "file:/a/./b", "file:~x", "http://example.invalid/x",
                     "Bivpak@main:x", "bivpak@:x", "bivpak@main:", "bivpak@main:/abs",
                     "bivpak@-x:p", "bivpak@main:a/./b", "bivpak@main:a/../b",
                     "bivpak@main:p#L2-L2", "bivpak@main:p#L3-L1", "bivpak",
                     "0192F3A4-5B6C-7D8E-9F01-23456789ABCD"):
            with self.subTest(text=text):
                self.refused("E-REF-SPELLING", refs.parse_ref, text)
        # positive control: the canonical forms of the two range cases parse
        self.assertEqual(refs.render_ref(refs.parse_ref("bivpak@main:p#L2")), "bivpak@main:p#L2")
        self.assertEqual(refs.parse_ref("bivpak@main:p#L1-L3").line_end, 3)


class TestResolve(Refused):
    def setUp(self):
        self.repo = Repo({ODD: "main 1\n", "p": "one\ntwo\nthree\n", "real": "x\n",
                          "d/f": "f\n"},
                         fix_files={ODD: "fix 1\nfix 2\nfix 3\n"},
                         links=(("link", "real"), ("dirlink", "d")))
        self.addCleanup(self.repo.close)
        clean_git_environment(self)

    def resolve(self, text):
        return refs.resolve_git(self.repo.path, refs.parse_ref(text))

    def test_resolves_exact_blob(self):
        got = self.resolve("bivpak@fix@v2:%s#L1-L3" % ODD)
        self.assertEqual(got.commit, git(self.repo.path, "rev-parse", "fix@v2"))
        self.assertEqual(got.blob_sha, git(self.repo.path, "rev-parse", "fix@v2:" + ODD))
        self.assertEqual(got.line_count, 3)
        main = self.resolve("bivpak@main:%s" % ODD)
        self.assertNotEqual(main.blob_sha, got.blob_sha)
        self.assertEqual(main.line_count, 1)
        self.assertIsNone(rules._XROOT_MODE.get())  # the read mode does not leak

    def test_ambiguous_suffix(self):
        self.resolve("bivpak@main:p#L1")  # positive control: only p exists
        self.repo.commit_files({"p#L1": "literal\n", "q#L1": "only the literal\n",
                                "r": "r\n", "r#L1/inner": "a directory\n", "s": "s\n"},
                               links=(("s#L1", "real"),))
        for text, literal in (("bivpak@main:p#L1", "p#L1"), ("bivpak@main:r#L1", "r#L1"),
                              ("bivpak@main:s#L1", "s#L1")):
            with self.subTest(text=text):
                error = self.refused("E-REF-AMBIGUOUS", self.resolve, text)
                self.assertIn("an entry named %s" % literal, error.cause)
        # DD § 6: only the literal exists -> not ambiguous, unresolved, naming the literal
        error = self.refused("E-REF-UNRESOLVED", self.resolve, "bivpak@main:q#L1")
        self.assertIn("q#L1", error.cause)

    def test_range_beyond_blob(self):
        self.assertEqual(self.resolve("bivpak@main:p#L3").line_count, 3)
        error = self.refused("E-REF-RANGE", self.resolve, "bivpak@main:p#L2-L4")
        self.assertIn("3 lines", error.cause)

    def test_symlink_path_refused(self):
        self.resolve("bivpak@main:real")
        self.resolve("bivpak@main:d/f")
        for text in ("bivpak@main:link", "bivpak@main:dirlink/f"):
            with self.subTest(text=text):
                error = self.refused("E-REF-UNRESOLVED", self.resolve, text)
                self.assertIn("traverses symlink component", error.cause)

    def test_unresolved_targets(self):
        for text, reason in (("bivpak@nope:p", "revision"), ("bivpak@main:absent", "no file"),
                             ("bivpak@main:d", "directory")):
            with self.subTest(text=text):
                error = self.refused("E-REF-UNRESOLVED", self.resolve, text)
                self.assertIn(reason, error.cause)
        self.refused("E-REF-UNRESOLVED", refs.resolve_git, self.repo.path / "d",
                     refs.parse_ref("bivpak@main:p"))
        self.refused("E-REF-UNRESOLVED", refs.resolve_git, self.repo.path,
                     refs.parse_ref("#12"))

    def test_seam_patch_intercepts(self):
        real = rules.xroot_git
        calls = []

        def recording(repo, *args, **kwargs):
            calls.append(args[:2])
            return real(repo, *args, **kwargs)

        with mock.patch.object(rules, "xroot_git", side_effect=recording):
            self.resolve("bivpak@main:p")  # positive control: resolves through the wrapper
        # refs' own calls, not only those inside rules' helpers, reach the seam
        self.assertIn(("rev-parse", "--verify"), calls)
        self.assertIn(("cat-file", "blob"), calls)
        # every other seam name refs uses is reached through rules as well
        for name in ("xroot_repository_and_commit", "xroot_walk", "xroot_valid_relpath"):
            with self.subTest(name=name):
                with mock.patch.object(rules, name, side_effect=getattr(rules, name)) as seen:
                    if name == "xroot_valid_relpath":
                        refs.parse_ref("bivpak@main:p")
                    else:
                        self.resolve("bivpak@main:p")
                self.assertTrue(seen.called)
        probe = rules.XrootAxisError("repository", "patched seam probe")
        with mock.patch.object(rules, "xroot_git", side_effect=probe) as patched:
            error = self.refused("E-REF-UNRESOLVED", self.resolve, "bivpak@main:p")
        self.assertTrue(patched.called)
        self.assertIn("patched seam probe", error.cause)


class TestFileDigest(Refused):
    def test_file_digest(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            (base / "out.txt").write_bytes(b"result\n")
            self.assertEqual(refs.file_digest(base / "out.txt"),
                             hashlib.sha256(b"result\n").hexdigest())
            os.symlink(base / "out.txt", base / "link.txt")
            for path, reason in ((base / "link.txt", "the path is a symlink"),
                                 (base / "missing.txt", "cannot open"),
                                 (base, "not a regular file")):
                with self.subTest(path=path.name):
                    error = self.refused("E-REF-UNRESOLVED", refs.file_digest, path)
                    self.assertIn(reason, error.cause)


class TestCodes(unittest.TestCase):
    def test_codes_explainable(self):
        for code in ("E-REF-SPELLING", "E-REF-UNRESOLVED", "E-REF-AMBIGUOUS", "E-REF-RANGE",
                     "E-REF-TIMEOUT", "E-REF-GIT-ENVIRONMENT"):
            with self.subTest(code=code):
                cause, remedy = errors.explain_for(code)
                self.assertTrue(cause and remedy)



class TestPanelFold(Refused):
    """PR-2 panel fold 1 (v3-engine-pr2-review-1): R1-R9."""

    def setUp(self):
        self.repo = Repo({"p": "one\ntwo\nthree\n", "nonl": "a\nb", "a:b": "colon\n"})
        self.addCleanup(self.repo.close)
        clean_git_environment(self)

    def resolve(self, text):
        return refs.resolve_git(self.repo.path, refs.parse_ref(text))

    def test_replacements_are_off(self):  # R1
        real_commit = git(self.repo.path, "rev-parse", "main")
        real_blob = git(self.repo.path, "rev-parse", "main:p")
        fake = self.repo.path / "fake"
        fake.write_text("replacement\n")
        fake_blob = git(self.repo.path, "hash-object", "-w", str(fake))
        git(self.repo.path, "replace", real_blob, fake_blob)
        got = self.resolve("bivpak@main:p#L3")
        self.assertEqual((got.commit, got.blob_sha, got.line_count), (real_commit, real_blob, 3))
        other_tree = git(self.repo.path, "mktree", "--missing")  # an empty tree: no p, no q
        fake_commit = git(self.repo.path, "commit-tree", other_tree, "-m", "fake")
        git(self.repo.path, "replace", "-f", real_commit, fake_commit)
        self.assertEqual(self.resolve("bivpak@main:p").commit, real_commit)
        self.assertIsNone(rules._XROOT_MODE.get())

    def test_git_environment_refused(self):  # R2
        self.resolve("bivpak@main:p")  # positive control: a clean environment resolves
        for name in ("GIT_DIR", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_COUNT", "GIT_EDITOR"):
            with self.subTest(name=name):
                with mock.patch.dict(os.environ, {name: "1"}):
                    error = self.refused("E-REF-GIT-ENVIRONMENT", self.resolve, "bivpak@main:p")
                self.assertIn(name, error.cause)

    def test_only_typed_errors(self):  # R3
        latin = self.repo.path / "latin.txt"
        latin.write_text("x\n")
        git(self.repo.path, "add", "latin.txt")
        git_input(self.repo.path, "-c", "i18n.commitEncoding=latin1",
                  "commit", "-q", "-F", "-", data=b"caf\xe9\n")
        self.assertEqual(self.resolve("bivpak@main:latin.txt").line_count, 1)
        for text in ("bivpak@ma\x00in:p", "bivpak@ma\x01in:p", "file:/a\x00b",
                     "bivpak@main:a\ud800b", "bivpak@main:" + "\x01" * 150, "#1\x85"):
            with self.subTest(text=repr(text)[:30]):
                self.refused("E-REF-SPELLING", refs.parse_ref, text)
        self.assertLessEqual(len(refs._display("\x01" * 300, 300)), 303)

    def test_built_spec_stays_typed(self):  # R3: resolve_git also takes specs not from parse_ref
        for rev, path in (("ma\x00in", "p"), ("main", "a\ud800b"), ("main", "p\x00q")):
            with self.subTest(rev=repr(rev), path=repr(path)):
                spec = refs.RefSpec("git", "built", None, "bivpak", rev, path, None, None)
                self.refused("E-REF-UNRESOLVED", refs.resolve_git, self.repo.path, spec)

    def test_corpus_outcomes_are_typed(self):  # R3 corpus
        import random
        rng = random.Random(44)
        seeds = ["bivpak@main:p#L1-L2", "bivpak@main:nonl", "file:/tmp/x", "#12@pdc",
                 "0192f3a4-5b6c-7d8e-9f01-23456789abcd", "bivpak@main:a:b"]
        alphabet = "@:#L-/.~ \x00\x01\x85é\ud800abp0129"
        resolved = 0
        for count in range(3000):
            text = list(rng.choice(seeds))
            for _ in range(rng.randint(1, 4)):
                at = rng.randrange(len(text) + 1)
                if rng.random() < 0.5:
                    text.insert(at, rng.choice(alphabet))
                elif text:
                    del text[min(at, len(text) - 1)]
            text = "".join(text)
            try:
                spec = refs.parse_ref(text)
            except errors.EngineError:
                continue
            if spec.kind == "git" and resolved < 120:
                resolved += 1
                try:
                    refs.resolve_git(self.repo.path, spec)
                except errors.EngineError:
                    pass
        self.assertGreater(resolved, 10)

    def test_fifo_refused_promptly(self):  # R4
        import threading
        with tempfile.TemporaryDirectory() as name:
            fifo = Path(name) / "fifo"
            os.mkfifo(fifo)
            outcome = []

            def digest():
                try:
                    refs.file_digest(fifo)
                except errors.EngineError as error:
                    outcome.append(error)

            worker = threading.Thread(target=digest, daemon=True)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "file_digest blocked on a FIFO")
            self.assertEqual(outcome[0].code, "E-REF-UNRESOLVED")
            self.assertIn("not a regular file", outcome[0].cause)

    def test_submodule_refused(self):  # R5
        head = git(self.repo.path, "rev-parse", "main")
        absent = "1234567890abcdef1234567890abcdef12345678"  # a submodule's usual case
        for name, oid in (("sm", head), ("deep/sm[1]", absent)):
            git(self.repo.path, "update-index", "--add", "--cacheinfo", "160000,%s,%s" % (oid, name))
        git(self.repo.path, "commit", "-q", "-m", "gitlinks")
        for text in ("bivpak@main:sm", "bivpak@main:deep/sm[1]"):
            with self.subTest(text=text):
                error = self.refused("E-REF-UNRESOLVED", self.resolve, text)
                self.assertIn("is a submodule", error.cause)
        self.assertEqual(self.resolve("bivpak@main:p").line_count, 3)  # control
        # a path is matched literally: x[1] is a file even beside a gitlink x1
        self.repo.commit_files({"x[1]": "one\n"})
        git(self.repo.path, "update-index", "--add", "--cacheinfo", "160000,%s,x1" % absent)
        git(self.repo.path, "commit", "-q", "-m", "x1")
        self.assertEqual(self.resolve("bivpak@main:x[1]").line_count, 1)

    def test_mode_cleared_inside_stops(self):  # R1: replacements stay off or nothing answers
        real = rules.xroot_repository_and_commit

        def clearing(*args, **kwargs):
            answer = real(*args, **kwargs)
            rules._XROOT_MODE.set(None)
            return answer

        with mock.patch.object(rules, "xroot_repository_and_commit", side_effect=clearing):
            error = self.refused("E-REF-UNRESOLVED", self.resolve, "bivpak@main:p")
        self.assertIn("replacement", error.cause)

    def test_timeout_is_its_own_code(self):  # R7
        with mock.patch.object(rules, "XROOT_GIT_TIMEOUT_SECONDS", 0):
            error = self.refused("E-REF-TIMEOUT", self.resolve, "bivpak@main:p")
        self.assertNotIn("does not resolve", error.cause)
        self.assertIn("retry", error.remedy)

    def test_relative_path_refused(self):  # R8
        for path in (Path("rel.txt"), Path("~/x")):
            with self.subTest(path=str(path)):
                error = self.refused("E-REF-UNRESOLVED", refs.file_digest, path)
                self.assertIn("not absolute", error.cause)

    def test_line_count_without_final_newline(self):  # R9
        self.assertEqual(self.resolve("bivpak@main:nonl#L2").line_count, 2)
        self.refused("E-REF-RANGE", self.resolve, "bivpak@main:nonl#L3")

    def test_colon_path_resolves(self):  # R9
        self.assertEqual(self.resolve("bivpak@main:a:b").line_count, 1)



# Plan Task 13 (DD-v3-tests § 3.4, erratum 1): the engine's tests off the shipped linter.
TASK13_MODULES = ("test_judging", "test_judging_agreement", "test_routed_notice",
                  "test_draft_shape_lint", "test_draft_shape", "test_root_lint_corrections",
                  "test_finding_tags", "test_history_audit", "test_human_gate", "test_refs")
# spelled in parts so that this module, one of the ten, names none of them
LINTER_NAMES = ("relay-" + "lint.py", "relay-" + "lint-fixtures", "_stand" + "alone")


class TestLinterSeam(unittest.TestCase):
    def test_no_linter_loader(self):
        tests = Path(__file__).resolve().parent
        for name in TASK13_MODULES:
            text = (tests / (name + ".py")).read_text(encoding="utf-8")
            for word in LINTER_NAMES:
                with self.subTest(module=name, word=word):
                    self.assertNotIn(word, text)
        # an executed import of each, in a copy of tools/ with the linter and its
        # fixtures absent (v3-a4-order-ruling-4), not only a text scan
        tools = tests.parents[1]
        hidden = {LINTER_NAMES[0], LINTER_NAMES[1], "relay-engine-fixtures", "__pycache__"}
        with tempfile.TemporaryDirectory() as name:
            copy = Path(name, "tools")
            __import__("shutil").copytree(tools, copy,
                                          ignore=lambda folder, names: hidden & set(names))
            self.assertFalse((copy / LINTER_NAMES[0]).exists())
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env.update(PYTHONPATH=str(copy), PYTHONDONTWRITEBYTECODE="1")
            run = subprocess.run(
                [__import__("sys").executable, "-c",
                 "import importlib, sys\n"
                 "for name in sys.argv[1:]:\n"
                 "    module = importlib.import_module('relay_engine.tests.' + name)\n"
                 "    assert module.__file__.startswith(%r), module.__file__\n" % str(copy),
                 *TASK13_MODULES],
                cwd=name, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_seam_expired_deadline_spawns_no_git(self):
        import time
        with tempfile.TemporaryDirectory() as name, mock.patch.object(
                rules, "_xroot_start_git", side_effect=AssertionError("Git was spawned")) as start:
            for axis in ("design", "plan", "artifact"):
                with self.subTest(axis=axis):
                    with self.assertRaises(rules.XrootAxisError) as caught:
                        rules.xroot_git(Path(name), "cat-file", "-e", "HEAD", axis=axis,
                                        deadline=time.monotonic() - 1)
                    self.assertIn("timed out", str(caught.exception))
            start.assert_not_called()

class TestOneShotGit(unittest.TestCase):
    def setUp(self):
        self.enterContext(stop(30))
        clean_git_environment(self)
        self.enterContext(mock.patch.dict(os.environ, _GIT_ENV))
        self.repo = Repo({"held": "seed\n"})
        self.addCleanup(self.repo.close)

    def blocked_object(self):
        oid = "a" * 40
        obj = self.repo.path / ".git" / "objects" / oid[:2] / oid[2:]
        obj.parent.mkdir(exist_ok=True)
        os.mkfifo(obj)
        return oid

    def close_child(self, child):
        try:
            if child.returncode is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def git_descriptors(self, child):
        proc = Path(f"/proc/{child.pid}")
        if sys.platform.startswith("linux"):
            self.assertEqual("git", (proc / "comm").read_text().strip())
            rows = {}
            for descriptor in (proc / "fd").iterdir():
                try:
                    name = os.readlink(descriptor)
                except FileNotFoundError:
                    continue
                rows[int(descriptor.name)] = ("PIPE" if name.startswith("pipe:[") else "FILE", name)
        else:
            listing = subprocess.run(
                ["lsof", "-nP", "-p", str(child.pid), "-Fpcftn"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=True,
            )
            fields = listing.stdout.splitlines()
            self.assertIn(f"p{child.pid}", fields)
            self.assertIn("cgit", fields)
            rows = {}
            descriptor = None
            for field in fields:
                key, value = field[:1], field[1:]
                if key == "f":
                    descriptor = int(value) if value.isdecimal() else None
                    if descriptor is not None:
                        rows[descriptor] = ("", "")
                elif descriptor is not None and key in ("t", "n"):
                    kind, name = rows[descriptor]
                    rows[descriptor] = (value, name) if key == "t" else (kind, value)
        self.assertIsNone(child.poll(), "Git exited before its descriptors could be observed")
        self.assertEqual(rows.get(1, ("", ""))[0], "PIPE")
        self.assertEqual(rows.get(2, ("", ""))[0], "PIPE")
        return rows

    def test_git_child_excludes_inheritable_descriptor(self):
        oid = self.blocked_object()
        marker = self.repo.path / "inheritable-marker"
        marker.write_text("marker\n")
        with marker.open("rb") as handle:
            fd = handle.fileno()
            os.set_inheritable(fd, True)
            command = ["git", "-C", str(self.repo.path), "cat-file", "blob", oid]
            control = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(fd,),
            )
            try:
                self.assertEqual(self.git_descriptors(control)[fd][1], str(marker))
            finally:
                self.close_child(control)

            start = rules._xroot_start_git
            observed = []

            def inspect(*args, **kwargs):
                child = start(*args, **kwargs)
                try:
                    observed.append(self.git_descriptors(child))
                except BaseException:
                    self.close_child(child)
                    raise
                self.addCleanup(self.close_child, child)
                return child

            with mock.patch.object(rules, "_xroot_start_git", side_effect=inspect), \
                 mock.patch.object(rules, "XROOT_GIT_TIMEOUT_SECONDS", 0.25):
                with self.assertRaises(rules.XrootAxisError) as error:
                    rules.xroot_git(self.repo.path, "cat-file", "blob", oid)
            self.assertEqual(error.exception.axis, "capacity")
            self.assertEqual(len(observed), 1)
            self.assertNotIn(str(marker), (name for _, name in observed[0].values()))

    def test_git_query_finishes_with_unrelated_writer_open(self):
        content = b"held-writer fixture\n" * 10000
        git_input(self.repo.path, "hash-object", "-w", "--stdin", data=content)
        blob = self.repo.path / "held"
        blob.write_bytes(content)
        oid = git(self.repo.path, "hash-object", "held")
        missing = ("rev-parse", "--verify", "missing-ref")
        expected_failure = subprocess.run(
            ["git", "-C", str(self.repo.path), *missing], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=5,
        )
        self.assertNotEqual(expected_failure.returncode, 0)
        real_pipe = os.pipe
        start = rules._xroot_start_git
        for failure in (False, True):
            with self.subTest(failure=failure):
                held = []

                def identity(fd):
                    status = os.fstat(fd)
                    return status.st_dev, status.st_ino, status.st_mode

                def pipe():
                    read_fd, write_fd = real_pipe()
                    if not held:
                        try:
                            writer = identity(write_fd)
                            duplicate = os.dup(write_fd)
                        except BaseException:
                            os.close(read_fd)
                            os.close(write_fd)
                            raise
                        held.append((read_fd, duplicate, writer))
                    return read_fd, write_fd

                def retain_writer(*args, **kwargs):
                    with mock.patch.object(os, "pipe", side_effect=pipe):
                        child = start(*args, **kwargs)
                    try:
                        self.assertEqual(len(held), 1)
                        read_fd, duplicate, writer = held[0]
                        # This is the stdout reader from the very allocation
                        # whose writer was duplicated, even on split-inode pipes.
                        self.assertEqual(child.stdout.fileno(), read_fd)
                        self.assertEqual(identity(duplicate), writer)
                        self.assertTrue(stat.S_ISFIFO(os.fstat(read_fd).st_mode))
                    except BaseException:
                        self.close_child(child)
                        raise
                    self.addCleanup(self.close_child, child)
                    return child

                try:
                    with mock.patch.object(rules, "_xroot_start_git", side_effect=retain_writer):
                        if failure:
                            with self.assertRaises(rules.XrootAxisError) as caught:
                                rules.xroot_git(self.repo.path, *missing, binary=True)
                            error = caught.exception
                            self.assertEqual((error.axis, error.detail), ("repository", "Git object query failed"))
                            self.assertEqual(error.returncode, expected_failure.returncode)
                            self.assertEqual(error.stdout, expected_failure.stdout)
                            self.assertEqual(error.stderr, expected_failure.stderr)
                        else:
                            result = rules.xroot_git(self.repo.path, "cat-file", "blob", oid, binary=True)
                            self.assertEqual(result, content)
                            with self.assertRaises(AssertionError):
                                self.assertEqual(result[:-1], content)
                    self.assertEqual(len(held), 1)
                    _, duplicate, writer = held[0]
                    self.assertEqual(identity(duplicate), writer)
                finally:
                    for _, duplicate, _ in held:
                        os.close(duplicate)

    def assert_reaped(self, child):
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)

    def test_git_query_deadline_reaps_blocked_child(self):
        oid = self.blocked_object()
        fixture = self.repo.path / "launcher"
        fixture.mkdir()
        gate = fixture / "gate"
        ready = fixture / "ready"
        os.mkfifo(gate)
        launcher = fixture / "git"
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import os\n"
            "os.close(1)\n"
            "os.close(2)\n"
            f"open({str(ready)!r}, 'xb').close()\n"
            f"with open({str(gate)!r}, 'rb') as gate:\n"
            "    gate.read(1)\n"
        )
        launcher.chmod(0o700)

        control = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read(1)"],
                                   stdin=subprocess.PIPE)
        try:
            with self.assertRaises(AssertionError):
                self.assert_reaped(control)
        finally:
            self.close_child(control)

        start = rules._xroot_start_git
        selector_type = selectors.DefaultSelector
        for closed_streams in (False, True):
            with self.subTest(closed_streams=closed_streams):
                children = []
                stream_fds = set()
                eof_fds = set()

                def remember_child(*args, **kwargs):
                    child = start(*args, **kwargs)
                    children.append(child)
                    self.addCleanup(self.close_child, child)
                    stream_fds.update((child.stdout.fileno(), child.stderr.fileno()))
                    return child

                class ObserveEof(selector_type):
                    def unregister(self, fd):
                        key = super().unregister(fd)
                        if closed_streams and fd in stream_fds:
                            testcase.assertIsNone(children[0].returncode)
                            os.kill(children[0].pid, 0)
                            eof_fds.add(fd)
                        return key

                testcase = self
                path = os.environ["PATH"]
                if closed_streams:
                    path = str(fixture) + os.pathsep + path
                with mock.patch.dict(os.environ, PATH=path), \
                     mock.patch.object(rules, "_xroot_start_git", side_effect=remember_child), \
                     mock.patch.object(rules, "XROOT_GIT_TIMEOUT_SECONDS", 1), \
                     mock.patch.object(selectors, "DefaultSelector", ObserveEof):
                    started = time.monotonic()
                    with self.assertRaises(rules.XrootAxisError) as caught:
                        rules.xroot_git(self.repo.path, "cat-file", "blob", oid)
                self.assertEqual(len(children), 1)
                self.assert_reaped(children[0])
                self.assertGreaterEqual(time.monotonic() - started, 1)
                self.assertEqual(caught.exception.axis, "capacity")
                self.assertEqual(
                    caught.exception.detail,
                    "Git object query timed out at the 5 s bound during cat-file; "
                    "this is a time limit on a cold read, not a lineage fault; "
                    "a large root can reach it on a busy machine",
                )
                if closed_streams:
                    self.assertTrue(ready.is_file())
                    self.assertEqual(eof_fds, stream_fds)


if __name__ == "__main__":
    unittest.main()

