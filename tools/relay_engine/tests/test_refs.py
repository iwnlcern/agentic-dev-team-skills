"""v3 references: spelling, Git resolution through the rules seam, local digests (DD-v3-engine § 6)."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from relay_engine import errors
from relay_engine import refs
from relay_engine import rules


ODD = "range-@@ -01 +1 @@.diff"
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": "2026-11-04T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-04T00:00:00Z",
}


def git(repo, *args):
    env = dict(os.environ, **_GIT_ENV)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True).stdout.strip()


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
        env = dict(os.environ, **_GIT_ENV)
        subprocess.run(["git", "-C", str(self.repo.path), "-c", "i18n.commitEncoding=latin1",
                        "commit", "-q", "-F", "-"], input=b"caf\xe9\n", env=env, check=True,
                       capture_output=True)
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


if __name__ == "__main__":
    unittest.main()

