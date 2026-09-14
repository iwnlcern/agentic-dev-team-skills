import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "scope_proof.sh"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
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
