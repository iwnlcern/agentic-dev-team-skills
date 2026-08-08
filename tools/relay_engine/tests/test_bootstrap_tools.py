import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.normpath(os.path.join(HERE, "..", "..",
                                    "relay-engine-fixtures", "bootstrap"))
RUNNER = os.path.join(FIX, "hostile_legs.py")
RESOLVER = os.path.join(FIX, "bootstrap_resolve.py")
SHA_A = "a" * 40
BRANCH = "feat/v29-engine"
CENSUS = (
    "tools/relay-engine-fixtures/bootstrap/bootstrap_resolve.py",
    "tools/relay-engine-fixtures/bootstrap/check_plan_contract.py",
    "tools/relay-engine-fixtures/bootstrap/hostile_legs.py",
    "tools/relay_engine/__init__.py",
    "tools/relay_engine/tests/__init__.py",
    "tools/relay_engine/tests/test_bootstrap_tools.py",
    "tools/relay_engine/tests/test_plan_contract.py",
)


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestBootstrapTools(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bootstrap-tools-")

    def tearDown(self):
        shutil.rmtree(self.root)

    def resolver(self, *args):
        return subprocess.run([sys.executable, RESOLVER] + list(args),
                              capture_output=True, text=True)

    def write_env(self, sha=SHA_A):
        env = os.path.join(self.root, "v29-engine-env.sh")
        tmp = env + ".new"
        with open(tmp, "w") as f:
            f.write("export COORD_ROOT='/repo'\n")
            f.write("export RELAY_ENGINE_RESULTS_ROOT='%s'\n" % self.root)
            f.write("export PYTHON='%s'\n" % sys.executable)
            f.write("export DETECTION_REF='refs/heads/x'\n")
            f.write("export DETECTION_SHA='%s'\n" % sha)
        os.replace(tmp, env)
        return env

    def install_runner(self):
        shutil.copy(RUNNER, os.path.join(self.root, "hostile_legs.py"))
        return sha256_file(RUNNER)

    def run_legs(self, proof, n, rsha, sha=SHA_A):
        return self.resolver("run-legs", proof, n, self.root,
                             "v29-engine-env.sh", "hostile_legs.py",
                             rsha, "refs/heads/x", sha)

    def add_census_commit(self, wt, g, paths=CENSUS):
        # The real D31 machinery-commit shape: exactly the census paths,
        # nothing synthetic (R3 of the r31 review).
        for rel in paths:
            p = os.path.join(wt, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("# machinery placeholder: %s\n" % rel)
        g("add", *paths)
        self.assertEqual(
            g("commit", "-q", "-m",
              "test(relay-engine): bootstrap proof machinery"
              " (D31)").returncode, 0)
        return g("rev-parse", "HEAD").stdout.strip()

    def make_repo(self, census=True):
        wt = os.path.join(self.root, "wt")
        os.mkdir(wt)

        def g(*args):
            return subprocess.run(
                ["git", "-C", wt, "-c", "user.name=v29",
                 "-c", "user.email=v29@local"] + list(args),
                capture_output=True, text=True)

        self.assertEqual(g("init", "-q", "-b", "main").returncode, 0)
        with open(os.path.join(wt, "base.txt"), "w") as f:
            f.write("base\n")
        g("add", "base.txt")
        self.assertEqual(g("commit", "-q", "-m", "base").returncode, 0)
        sha0 = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(g("checkout", "-q", "-b", BRANCH).returncode, 0)
        if census:
            self.add_census_commit(wt, g)
        tip = g("rev-parse", "HEAD").stdout.strip()
        return wt, g, sha0, tip

    def classify(self, proof, wt, sha):
        return self.resolver("classify", proof, wt, sha)

    def arm_of(self, out):
        for line in (out.stdout + out.stderr).splitlines():
            if line.startswith("arm="):
                return line.split("=", 1)[1]
        return None

    def assert_classify_frozen(self, proof, wt, g, sha):
        # A classified STOP must mutate nothing: HEAD, worktree status,
        # and proof bytes are byte-identical across the call.
        head = g("rev-parse", "HEAD").stdout
        status = g("status", "--porcelain").stdout
        before = None
        if os.path.exists(proof):
            with open(proof, "rb") as f:
                before = f.read()
        out = self.classify(proof, wt, sha)
        self.assertEqual(g("rev-parse", "HEAD").stdout, head)
        self.assertEqual(g("status", "--porcelain").stdout, status)
        after = None
        if os.path.exists(proof):
            with open(proof, "rb") as f:
                after = f.read()
        self.assertEqual(before, after)
        return out

    def first_binding(self, wt, g, sha0, proof):
        self.write_env(sha0)
        rsha = self.install_runner()
        co = self.resolver("make-carrier", self.root, "1",
                           "v29-engine-env.sh", "refs/heads/x", sha0)
        self.assertEqual(co.returncode, 0, co.stderr)
        ph = co.stdout.strip().split("payload-sha256=")[1]
        self.assertEqual(
            self.resolver("bootstrap-if-absent", proof, "1",
                          self.root, ph).returncode, 0)
        run = self.run_legs(proof, "1", rsha, sha0)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            self.resolver("pass-if-absent", proof, "1", ph,
                          rsha).returncode, 0)
        tip = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            self.resolver("binding-append", proof, BRANCH,
                          sha0, tip, "none", "task0").returncode, 0)
        return rsha, ph

    def test_resolver_self_test_green(self):
        out = self.resolver("self-test", self.root)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_runner_requires_held_descriptor(self):
        env = self.write_env()
        proof = os.path.join(self.root, "proof.txt")
        out = subprocess.run(
            [sys.executable, RUNNER, "1", env, proof, "/repo", self.root,
             sys.executable, "refs/heads/x", SHA_A],
            capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("inherited descriptor", out.stderr)

    def test_runner_fault_seam_short_write(self):
        env = self.write_env()
        proof = os.path.join(self.root, "proof.txt")
        fd = os.open(proof, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            e = dict(os.environ)
            e["V29_RUNNER_FAULT"] = "short-transcript"
            out = subprocess.run(
                [sys.executable, RUNNER, "1", env, "fd:%d" % fd, "/repo",
                 self.root, sys.executable, "refs/heads/x", SHA_A],
                env=e, pass_fds=(fd,), capture_output=True, text=True)
        finally:
            os.close(fd)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("short write: 1 of ", out.stderr)

    def test_run_legs_three_legs_green_and_correlated(self):
        self.write_env()
        rsha = self.install_runner()
        proof = os.path.join(self.root, "proof.txt")
        out = self.run_legs(proof, "7", rsha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "legs-ran")
        with open(proof) as f:
            text = f.read()
        for leg in ("missing", "symlink", "stale"):
            self.assertRegex(text,
                             r"binding=7 leg=%s status=[1-9][0-9]*" % leg)
        for var in ("COORD_ROOT", "RELAY_ENGINE_RESULTS_ROOT", "PYTHON",
                    "DETECTION_REF", "DETECTION_SHA"):
            self.assertIn("binding=7 leg=missing %s=unset" % var, text)

    def test_run_legs_refuses_replaced_runner(self):
        self.write_env()
        rsha = self.install_runner()
        proof = os.path.join(self.root, "proof.txt")
        with open(os.path.join(self.root, "hostile_legs.py"), "w") as f:
            f.write("import sys\n"
                    "print('binding=1 leg=missing status=1')\n"
                    "sys.exit(0)\n")
        out = self.run_legs(proof, "1", rsha)
        self.assertEqual(out.returncode, 1)
        self.assertIn("runner bytes do not match the pinned digest",
                      out.stderr)
        self.assertFalse(os.path.exists(proof))

    def test_run_legs_refuses_symlinked_and_fifo_proof(self):
        self.write_env()
        rsha = self.install_runner()
        real = os.path.join(self.root, "proof.txt")
        with open(real, "w") as f:
            f.write("")
        link = os.path.join(self.root, "proof-link.txt")
        os.symlink(real, link)
        out = self.run_legs(link, "1", rsha)
        self.assertEqual(out.returncode, 1)
        self.assertIn("io failure", out.stderr)
        fifo = os.path.join(self.root, "proof-fifo.txt")
        os.mkfifo(fifo)
        out = self.run_legs(fifo, "1", rsha)
        self.assertEqual(out.returncode, 1)
        self.assertIn("not a regular file", out.stderr)

    def test_fault_leg_subcommand(self):
        self.write_env()
        rsha = self.install_runner()
        out = self.resolver("fault-leg", self.root, "hostile_legs.py",
                            "v29-engine-env.sh", rsha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "fault-leg-ok")

    def test_end_to_end_first_binding_in_real_repo(self):
        wt, g, sha0, tip = self.make_repo()
        proof = os.path.join(self.root, "v29-engine-detection-ref.txt")
        self.write_env(sha0)
        rsha = self.install_runner()
        co = self.resolver("make-carrier", self.root, "1",
                           "v29-engine-env.sh", "refs/heads/x", sha0)
        self.assertEqual(co.returncode, 0, co.stderr)
        ph = co.stdout.strip().split("payload-sha256=")[1]
        self.assertEqual(
            self.resolver("bootstrap-if-absent", proof, "1",
                          self.root, ph).returncode, 0)
        run = self.run_legs(proof, "1", rsha, sha0)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            self.resolver("pass-if-absent", proof, "1", ph,
                          rsha).returncode, 0)
        early = self.resolver("resolve", proof, rsha, sha0, wt)
        self.assertEqual(early.returncode, 1)
        self.assertIn("no binding block", early.stderr)
        self.assertEqual(
            self.resolver("binding-append", proof, BRANCH,
                          sha0, tip, "none", "task0").returncode, 0)
        lb = self.resolver("latest-binding", proof)
        self.assertEqual(lb.returncode, 0, lb.stderr)
        self.assertIn("bound_sha=%s" % sha0, lb.stdout)
        self.assertIn("branch_tip=%s" % tip, lb.stdout)
        res = self.resolver("resolve", proof, rsha, sha0, wt)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(),
                         "current=1 payload-sha256=%s" % ph)
        with open(os.path.join(wt, "engine.txt"), "w") as f:
            f.write("engine work\n")
        g("add", "engine.txt")
        self.assertEqual(g("commit", "-q", "-m", "later work").returncode, 0)
        res = self.resolver("resolve", proof, rsha, sha0, wt)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(g("checkout", "-q", "main").returncode, 0)
        off = self.resolver("resolve", proof, rsha, sha0, wt)
        self.assertEqual(off.returncode, 1)
        self.assertIn("worktree is not on %s" % BRANCH, off.stderr)
        self.assertEqual(g("checkout", "-q", BRANCH).returncode, 0)
        self.assertEqual(g("reset", "-q", "--hard", sha0).returncode, 0)
        reset = self.resolver("resolve", proof, rsha, sha0, wt)
        self.assertEqual(reset.returncode, 1)
        self.assertIn("recorded branch_tip is neither the worktree tip"
                      " nor its ancestor", reset.stderr)

    def test_rebind_state_machine_crash_prefixes(self):
        wt, g, sha0, tip0 = self.make_repo()
        proof = os.path.join(self.root, "v29-engine-detection-ref.txt")
        rsha, ph1 = self.first_binding(wt, g, sha0, proof)
        self.assertEqual(
            self.resolver("resolve", proof, rsha, sha0, wt).returncode, 0)
        self.assertEqual(g("checkout", "-q", "main").returncode, 0)
        with open(os.path.join(wt, "detection.txt"), "w") as f:
            f.write("new head\n")
        g("add", "detection.txt")
        self.assertEqual(g("commit", "-q", "-m", "new head").returncode, 0)
        sha1 = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(g("checkout", "-q", BRANCH).returncode, 0)
        # Pre-rebase state: new base not yet an ancestor, recorded tip is.
        self.assertNotEqual(
            g("merge-base", "--is-ancestor", sha1, "HEAD").returncode, 0)
        self.assertEqual(
            g("merge-base", "--is-ancestor", tip0, "HEAD").returncode, 0)
        # The stale binding is consumer-RED against the new routed value.
        stale = self.resolver("resolve", proof, rsha, sha1, wt)
        self.assertEqual(stale.returncode, 1)
        # Rebind step (a): env rewritten from the routed replacement value.
        self.write_env(sha1)
        # The PRE-REBASE arm's durable witness precedes the only rebase.
        wa = self.resolver("witness-append", proof, wt, sha1)
        self.assertEqual(wa.returncode, 0, wa.stderr)
        # Rebase, then crash before any rebind record: the review's exact
        # probe -- the new base IS an ancestor, the old recorded tip is NOT.
        self.assertEqual(
            g("rebase", "--onto", sha1, sha0, BRANCH).returncode, 0)
        out = self.classify(proof, wt, sha1)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.arm_of(out), "REBASE-COMPLETE")
        self.assertEqual(
            g("merge-base", "--is-ancestor", sha1, "HEAD").returncode, 0)
        self.assertNotEqual(
            g("merge-base", "--is-ancestor", tip0, "HEAD").returncode, 0)
        # Recovery is the sequence rerun with the successor number; no
        # second rebase is needed or performed.
        self.assertEqual(
            self.resolver("supersede-if-absent", proof, "1").returncode, 0)
        co = self.resolver("make-carrier", self.root, "2",
                           "v29-engine-env.sh", "refs/heads/x", sha1)
        self.assertEqual(co.returncode, 0, co.stderr)
        ph2 = co.stdout.strip().split("payload-sha256=")[1]
        self.assertEqual(
            self.resolver("bootstrap-if-absent", proof, "2",
                          self.root, ph2).returncode, 0)
        run = self.run_legs(proof, "2", rsha, sha1)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            self.resolver("pass-if-absent", proof, "2", ph2,
                          rsha).returncode, 0)
        tip1 = g("rev-parse", "HEAD").stdout.strip()
        ba = self.resolver("binding-append", proof, BRANCH,
                           sha1, tip1, sha0, "rebind")
        self.assertEqual(ba.returncode, 0, ba.stderr)
        self.assertEqual(ba.stdout.strip(), "binding-appended")
        res = self.resolver("resolve", proof, rsha, sha1, wt)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(),
                         "current=2 payload-sha256=%s" % ph2)
        # Crash between binding-append and resolve: the rerun derives the
        # previous sha from latest-binding (now sha1 itself) and the helper
        # classifies already-current as a no-op instead of appending.
        lb = self.resolver("latest-binding", proof)
        derived = [l.split("=", 1)[1] for l in lb.stdout.splitlines()
                   if l.startswith("bound_sha=")][0]
        self.assertEqual(derived, sha1)
        noop = self.resolver("binding-append", proof, BRANCH,
                             sha1, tip1, derived, "rebind")
        self.assertEqual(noop.returncode, 0, noop.stderr)
        self.assertEqual(noop.stdout.strip(), "binding-current")
        # Fully-complete restart: every step is a recognized no-op and the
        # proof bytes do not change -- exactly-once history.
        with open(proof, "rb") as f:
            before = f.read()
        self.assertEqual(
            self.resolver("supersede-if-absent", proof, "1").returncode, 0)
        self.assertEqual(
            self.resolver("bootstrap-if-absent", proof, "2",
                          self.root, ph2).returncode, 0)
        self.assertEqual(
            self.resolver("pass-if-absent", proof, "2", ph2,
                          rsha).returncode, 0)
        self.assertEqual(
            self.resolver("binding-append", proof, BRANCH, sha1, tip1,
                          sha1, "rebind").stdout.strip(), "binding-current")
        self.assertEqual(
            self.resolver("resolve", proof, rsha, sha1, wt).returncode, 0)
        with open(proof, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)
        text = after.decode("utf-8")
        self.assertEqual(len(re.findall(r"rec \d+ [0-9a-f]{64} bootstrap ",
                                        text)), 2)
        self.assertEqual(len(re.findall(
            r"rec \d+ [0-9a-f]{64} supersede bootstrap ", text)), 1)
        self.assertEqual(len(re.findall(
            r"rec \d+ [0-9a-f]{64} hostile-legs-pass ", text)), 2)
        self.assertEqual(len(re.findall(
            r"rec \d+ [0-9a-f]{64} binding branch=", text)), 2)

    def test_classifier_fresh_root_and_crash_prefixes(self):
        # R2 of the r31 review: the classifier is the plan artifact, so a
        # true first bind -- no proof file, no installed programs, an
        # empty results root -- classifies INITIAL-BIND instead of the
        # probe's Python exit 2, and every crash prefix of commands 1-5
        # (and beyond) re-selects a correct arm.
        wt, g, sha0, _ = self.make_repo(census=False)
        proof = os.path.join(self.root, "v29-engine-detection-ref.txt")
        out = self.classify(proof, wt, sha0)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.arm_of(out), "INITIAL-BIND")
        self.assertIn("next_n=1", out.stdout)
        # Crash after command 1: the machinery commit exists, nothing is
        # installed yet; restarts stay INITIAL-BIND (commands 2-4 leave
        # no new durable state, so this is also their crash fixture).
        self.add_census_commit(wt, g)
        for _ in range(2):
            out = self.classify(proof, wt, sha0)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(self.arm_of(out), "INITIAL-BIND")
        # Crash after command 5 (installed copies), command 7 (carrier),
        # command 9 (bootstrap record), and command 12 (PASS record).
        rsha = self.install_runner()
        shutil.copy(RESOLVER, os.path.join(
            self.root, "v29-engine-bootstrap-resolve.py"))
        out = self.classify(proof, wt, sha0)
        self.assertEqual(self.arm_of(out), "INITIAL-BIND")
        self.write_env(sha0)
        co = self.resolver("make-carrier", self.root, "1",
                           "v29-engine-env.sh", "refs/heads/x", sha0)
        self.assertEqual(co.returncode, 0, co.stderr)
        ph = co.stdout.strip().split("payload-sha256=")[1]
        out = self.classify(proof, wt, sha0)
        self.assertEqual(self.arm_of(out), "INITIAL-BIND")
        self.assertEqual(
            self.resolver("bootstrap-if-absent", proof, "1",
                          self.root, ph).returncode, 0)
        out = self.classify(proof, wt, sha0)
        self.assertEqual(self.arm_of(out), "INITIAL-BIND")
        run = self.run_legs(proof, "1", rsha, sha0)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            self.resolver("pass-if-absent", proof, "1", ph,
                          rsha).returncode, 0)
        out = self.classify(proof, wt, sha0)
        self.assertEqual(self.arm_of(out), "INITIAL-BIND")
        # Crash after command 13: the binding exists and binds the routed
        # sha with intact Git truth -- CURRENT-BINDING, resume/no-op.
        tip = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            self.resolver("binding-append", proof, BRANCH,
                          sha0, tip, "none", "task0").returncode, 0)
        out = self.classify(proof, wt, sha0)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.arm_of(out), "CURRENT-BINDING")
        self.assertIn("n=1", out.stdout)
        res = self.resolver("resolve", proof, rsha, sha0, wt)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_classifier_stop_arms_zero_mutation(self):
        # R3 of the r31 review: every STOP classification is proven to
        # mutate nothing, and the two anomalies the review executed --
        # the same-sha reset and the recreated branch on the new base --
        # both stop instead of resuming or rebinding.
        wt, g, sha0, _ = self.make_repo(census=False)
        proof = os.path.join(self.root, "v29-engine-detection-ref.txt")
        # Pre-binding, wrong census: one commit that is not the machinery
        # census refuses INITIAL-BIND.
        self.add_census_commit(wt, g, paths=("tools/other.txt",))
        out = self.assert_classify_frozen(proof, wt, g, sha0)
        self.assertEqual(out.returncode, 3, out.stdout)
        self.assertEqual(self.arm_of(out), "STOP-PREBIND")
        self.assertIn("does not match the machinery census", out.stderr)
        # Pre-binding, two commits past the detection sha.
        self.add_census_commit(wt, g)
        out = self.assert_classify_frozen(proof, wt, g, sha0)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.arm_of(out), "STOP-PREBIND")
        self.assertIn("2 commits past the detection sha", out.stderr)
        # Same-sha reset (the committed reset probe): the binding still
        # binds the routed sha, Git truth is red -- stop, never resume.
        g("reset", "-q", "--hard", sha0)
        self.add_census_commit(wt, g)
        rsha, ph = self.first_binding(wt, g, sha0, proof)
        g("reset", "-q", "--hard", sha0)
        out = self.assert_classify_frozen(proof, wt, g, sha0)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.arm_of(out), "STOP-ANOMALOUS")
        self.assertIn("current binding with broken git truth", out.stderr)
        self.assertIn("recorded branch_tip is neither", out.stderr)
        # Recreated branch at the new detection sha: NEWANC holds, the
        # recorded lineage is gone, no witness exists -- the r31 probe
        # that REBASE-COMPLETE used to bind now stops.
        self.assertEqual(g("checkout", "-q", "main").returncode, 0)
        with open(os.path.join(wt, "detection.txt"), "w") as f:
            f.write("new head\n")
        g("add", "detection.txt")
        self.assertEqual(g("commit", "-q", "-m", "new head").returncode, 0)
        sha1 = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(g("branch", "-D", BRANCH).returncode, 0)
        self.assertEqual(
            g("checkout", "-q", "-b", BRANCH, sha1).returncode, 0)
        out = self.assert_classify_frozen(proof, wt, g, sha1)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.arm_of(out), "STOP-ANOMALOUS")
        self.assertIn("without a matching rebase witness", out.stderr)
        # Moved branch: neither the new base nor the recorded tip is in
        # HEAD's ancestry.
        self.assertEqual(g("checkout", "-q", "main").returncode, 0)
        self.assertEqual(g("branch", "-D", BRANCH).returncode, 0)
        self.assertEqual(
            g("checkout", "-q", "-b", BRANCH, sha0).returncode, 0)
        with open(os.path.join(wt, "unrelated.txt"), "w") as f:
            f.write("unrelated\n")
        g("add", "unrelated.txt")
        self.assertEqual(g("commit", "-q", "-m", "unrelated").returncode, 0)
        out = self.assert_classify_frozen(proof, wt, g, sha1)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.arm_of(out), "STOP-ANOMALOUS")
        self.assertIn("no longer carries the recorded binding lineage",
                      out.stderr)

    def test_classifier_rebase_in_progress_stops(self):
        wt, g, sha0, _ = self.make_repo()
        proof = os.path.join(self.root, "v29-engine-detection-ref.txt")
        with open(os.path.join(wt, "base.txt"), "w") as f:
            f.write("branch edit\n")
        g("add", "base.txt")
        self.assertEqual(g("commit", "-q", "-m", "branch edit").returncode,
                         0)
        self.assertEqual(g("checkout", "-q", "main").returncode, 0)
        with open(os.path.join(wt, "base.txt"), "w") as f:
            f.write("main edit\n")
        g("add", "base.txt")
        self.assertEqual(g("commit", "-q", "-m", "main edit").returncode, 0)
        sha1 = g("rev-parse", "HEAD").stdout.strip()
        # The conflicted rebase stops mid-flight and leaves rebase state.
        self.assertNotEqual(
            g("rebase", "--onto", sha1, sha0, BRANCH).returncode, 0)
        out = self.assert_classify_frozen(proof, wt, g, sha1)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.arm_of(out), "STOP-ANOMALOUS")
        self.assertEqual(g("rebase", "--abort").returncode, 0)

    def test_witnessed_rebase_cycle_no_repeated_rebase(self):
        # R3 of the r31 review: the PRE-REBASE arm writes the durable
        # witness, the only rebase runs once, REBASE-COMPLETE is selected
        # by the witnessed lineage (not bare ancestry), and the restart
        # after completion is CURRENT-BINDING.
        wt, g, sha0, tip0 = self.make_repo()
        proof = os.path.join(self.root, "v29-engine-detection-ref.txt")
        rsha, ph1 = self.first_binding(wt, g, sha0, proof)
        self.assertEqual(g("checkout", "-q", "main").returncode, 0)
        with open(os.path.join(wt, "detection.txt"), "w") as f:
            f.write("new head\n")
        g("add", "detection.txt")
        self.assertEqual(g("commit", "-q", "-m", "new head").returncode, 0)
        sha1 = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(g("checkout", "-q", BRANCH).returncode, 0)
        out = self.classify(proof, wt, sha1)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.arm_of(out), "PRE-REBASE")
        self.assertIn("next_n=2", out.stdout)
        self.assertIn("previous_bound_sha=%s" % sha0, out.stdout)
        wa = self.resolver("witness-append", proof, wt, sha1)
        self.assertEqual(wa.returncode, 0, wa.stderr)
        self.assertEqual(wa.stdout.strip(), "witness-appended")
        wa = self.resolver("witness-append", proof, wt, sha1)
        self.assertEqual(wa.stdout.strip(), "witness-present")
        self.assertEqual(
            g("rebase", "--onto", sha1, sha0, BRANCH).returncode, 0)
        # Crash landed after the rebase, before any rebind record: the
        # witnessed lineage proves completion; no second rebase arm.
        out = self.classify(proof, wt, sha1)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.arm_of(out), "REBASE-COMPLETE")
        self.assertIn("next_n=2", out.stdout)
        self.write_env(sha1)
        self.assertEqual(
            self.resolver("supersede-if-absent", proof, "1").returncode, 0)
        co = self.resolver("make-carrier", self.root, "2",
                           "v29-engine-env.sh", "refs/heads/x", sha1)
        self.assertEqual(co.returncode, 0, co.stderr)
        ph2 = co.stdout.strip().split("payload-sha256=")[1]
        self.assertEqual(
            self.resolver("bootstrap-if-absent", proof, "2",
                          self.root, ph2).returncode, 0)
        run = self.run_legs(proof, "2", rsha, sha1)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            self.resolver("pass-if-absent", proof, "2", ph2,
                          rsha).returncode, 0)
        tip1 = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            self.resolver("binding-append", proof, BRANCH, sha1, tip1,
                          sha0, "rebind").returncode, 0)
        out = self.classify(proof, wt, sha1)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.arm_of(out), "CURRENT-BINDING")
        res = self.resolver("resolve", proof, rsha, sha1, wt)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(),
                         "current=2 payload-sha256=%s" % ph2)
        with open(proof) as f:
            self.assertEqual(len(re.findall(r"rebase-witness from=",
                                            f.read())), 1)

    def test_carrier_content_is_the_pasted_bootstrap_line(self):
        self.write_env()
        co = self.resolver("make-carrier", self.root, "1",
                           "v29-engine-env.sh", "refs/heads/x", SHA_A)
        self.assertEqual(co.returncode, 0, co.stderr)
        with open(os.path.join(
                self.root, "v29-engine-bootstrap-line-1.txt")) as f:
            line = f.read()
        self.assertTrue(line.startswith("ENGINE_ENV='"))
        self.assertIn("[ \"$DETECTION_SHA\" = '%s' ]" % SHA_A, line)
        self.assertIn("sys.version_info >= (3,11)", line)


if __name__ == "__main__":
    unittest.main()
