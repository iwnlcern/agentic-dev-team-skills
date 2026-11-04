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
        for text in ("#012", "#0", "#12@", "#12@PDC", "file:rel/x", "file:/a/../b",
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
        self.repo.commit_files({"p#L1": "literal\n"})
        error = self.refused("E-REF-AMBIGUOUS", self.resolve, "bivpak@main:p#L1")
        self.assertIn("p#L1", error.cause)

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
                self.assertIn("symlink", error.cause)

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
            self.refused("E-REF-UNRESOLVED", refs.file_digest, base / "link.txt")
            self.refused("E-REF-UNRESOLVED", refs.file_digest, base / "missing.txt")
            self.refused("E-REF-UNRESOLVED", refs.file_digest, base)


class TestCodes(unittest.TestCase):
    def test_codes_explainable(self):
        for code in ("E-REF-SPELLING", "E-REF-UNRESOLVED", "E-REF-AMBIGUOUS", "E-REF-RANGE"):
            with self.subTest(code=code):
                cause, remedy = errors.explain_for(code)
                self.assertTrue(cause and remedy)


if __name__ == "__main__":
    unittest.main()
