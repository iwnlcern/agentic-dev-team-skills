"""Falsification tests for check_plan_contract.py (R1 of the r31 review).

The checker's claim is placement, not occurrence: these tests build a
minimal conforming plan around the real committed artifacts, prove it
green, then prove each corruption class red -- a wrong-site digest that
still occurs elsewhere, a missing command tag, a stale publication
primitive, a stale signature arity, and a census drift.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.normpath(os.path.join(HERE, "..", "..",
                                    "relay-engine-fixtures", "bootstrap"))
RESOLVER = os.path.join(FIX, "bootstrap_resolve.py")
RUNNER = os.path.join(FIX, "hostile_legs.py")
CHECKER = os.path.join(FIX, "check_plan_contract.py")
UNITTESTS = os.path.join(HERE, "test_bootstrap_tools.py")
SELF = os.path.abspath(__file__).replace(".pyc", ".py")

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
         6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
         11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
         15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
         19: "nineteen", 20: "twenty"}


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def methods_of(path, cls):
    import ast
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return sorted(
        node.name
        for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
        and c.name == cls
        for node in c.body if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_"))


def census_phrase(cls, path):
    ms = methods_of(path, cls)
    word = WORDS.get(len(ms), str(len(ms)))
    return "`%s` (%s tests: %s)" % (
        cls, word, ", ".join("`%s`" % m for m in ms))


def conforming_plan():
    rd = sha256_file(RUNNER)
    vd = sha256_file(RESOLVER)
    td = sha256_file(UNITTESTS)
    cd = sha256_file(CHECKER)
    pd = sha256_file(SELF)

    def pin(path, hexv):
        return ("[ \"$(shasum -a 256 %s | cut -d' ' -f1)\" = \"%s\" ]"
                % (path, hexv))

    lines = [
        "PLAN_ID: PLAN-synthetic (lineage history; old digests may occur"
        " here: %s)" % ("0" * 64),
        "",
        "### Task 0: the binding preflight",
        "",
        "with RESOLVE=\"$RELAY_ENGINE_RESULTS_ROOT/"
        "v29-engine-bootstrap-resolve.py\", executing:",
        "",
        "1. commit the machinery census; idempotent byte-identical skip."
        " No destructive discard op lives here (Option B); any"
        " discard-and-rebind is issued in the single-use dispatch.",
        "2. " + " && ".join((
            pin("tools/relay-engine-fixtures/bootstrap/hostile_legs.py",
                rd),
            pin("tools/relay-engine-fixtures/bootstrap/"
                "bootstrap_resolve.py", vd),
            pin("tools/relay-engine-fixtures/bootstrap/"
                "check_plan_contract.py", cd),
            pin("tools/relay_engine/tests/test_bootstrap_tools.py", td),
            pin("tools/relay_engine/tests/test_plan_contract.py", pd))),
        "3. \"$PYTHON\" tools/relay-engine-fixtures/bootstrap/"
        "check_plan_contract.py runs the parity gate.",
        "4. the unittest command.",
        "5. Install:"
        " RSHA=\"$(shasum -a 256"
        " \"$RELAY_ENGINE_RESULTS_ROOT/v29-engine-hostile-legs.py\" |"
        " cut -d' ' -f1)\" &&"
        " [ \"$RSHA\" = \"%s\" ] && [ \"$(shasum -a 256 \"$RESOLVE\" |"
        " cut -d' ' -f1)\" = \"%s\" ]" % (rd, vd),
        "6. \"$RESOLVE\" self-test \"$RELAY_ENGINE_RESULTS_ROOT\"",
        "7. \"$RESOLVE\" make-carrier \"$RELAY_ENGINE_RESULTS_ROOT\" <n>"
        " v29-engine-env.sh \"$DETECTION_REF\" \"$DETECTION_SHA\"",
        "8. \"$RESOLVE\" fault-leg \"$RELAY_ENGINE_RESULTS_ROOT\""
        " v29-engine-hostile-legs.py v29-engine-env.sh \"$RSHA\"",
        "9. \"$RESOLVE\" bootstrap-if-absent \"$PROOF\" <n>"
        " \"$RELAY_ENGINE_RESULTS_ROOT\" <payload-sha256> &&"
        " \"$RESOLVE\" supersede-if-absent \"$PROOF\" <n-1>",
        "10. \"$RESOLVE\" append \"$PROOF\" \"invocation line\" &&"
        " { \"$RESOLVE\" run-legs \"$PROOF\" <n>"
        " \"$RELAY_ENGINE_RESULTS_ROOT\" v29-engine-env.sh"
        " v29-engine-hostile-legs.py \"$RSHA\" \"$DETECTION_REF\""
        " \"$DETECTION_SHA\"; RST=$?; } &&"
        " \"$RESOLVE\" append \"$PROOF\" \"exit line\"",
        "11. grep for the leg statuses.",
        "12. \"$RESOLVE\" pass-if-absent \"$PROOF\" <n> <payload-sha256>"
        " \"$RSHA\"",
        "13. \"$RESOLVE\" binding-append \"$PROOF\" feat/v29-engine"
        " <bound-sha> <branch-tip> <previous-bound-sha|none>"
        " <task0|rebind>",
        "14. \"$RESOLVE\" resolve \"$PROOF\" \"$RSHA\" \"$DETECTION_SHA\""
        " \"$PWD\"",
        "",
        "The contract: `terminate <proof>`; `append <proof> <body>`"
        " (command 10); `make-carrier <root> <n> <env-basename> <ref>"
        " <sha>` (command 7); `bootstrap-if-absent <proof> <n> <root>"
        " <payload-sha256>` (command 9); `supersede-if-absent <proof>"
        " <n>` (command 9); `pass-if-absent <proof> <n> <payload-sha256>"
        " <runner-sha256>` (command 12); `binding-append <proof> <branch>"
        " <bound-sha> <branch-tip> <previous|none> <producer>`"
        " (command 13); `latest-binding <proof>`; `witness-append <proof>"
        " <worktree> <onto-sha>`; `classify <proof> <worktree>"
        " <detection-sha>`; `fault-leg <root> <runner-basename>"
        " <env-basename> <runner-sha256>` (command 8); `run-legs <proof>"
        " <n> <root> <env-basename> <runner-basename> <runner-sha256>"
        " <ref> <sha>` (command 10); `resolve <proof> <runner-sha256>"
        " <sha> <worktree>` (command 14); `self-test <results-root>`"
        " (command 6) -- twenty-three resolve triples across all seven"
        " record classes; the carrier publish is link-based no-replace.",
        "",
        "The census: %s and %s run at every bind."
        % (census_phrase("TestBootstrapTools", UNITTESTS),
           census_phrase("TestPlanContract", SELF)),
        "",
        "**Consumer preamble:** [ \"$(shasum -a 256"
        " '<abs>/v29-engine-bootstrap-resolve.py' | cut -d' ' -f1)\" ="
        " '%s' ] && [ \"$('<py>' '<abs>/v29-engine-bootstrap-resolve.py'"
        " resolve '<abs-proof>' '%s' '<detection-sha>' '<abs-worktree>')\""
        " = 'current=<n> payload-sha256=<payload-sha256>' ]" % (vd, rd),
        "",
        "**The pinned machinery artifacts (D31):** `hostile_legs.py`"
        " (the runner, SHA-256 `%s`), `bootstrap_resolve.py` (the"
        " resolver, SHA-256 `%s`), `test_bootstrap_tools.py` (SHA-256"
        " `%s`), `check_plan_contract.py` (SHA-256 `%s`), and"
        " `test_plan_contract.py` (SHA-256 `%s`)."
        % (rd, vd, td, cd, pd),
        "",
        "### Task 1: later work",
        "",
        "1. a later checklist entry that is outside Task 0 and never"
        " collides with the sequence.",
        "",
    ]
    return "\n".join(lines)


class TestPlanContract(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="plan-contract-")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_checker(self, plan_text):
        plan = os.path.join(self.d, "plan.md")
        with open(plan, "w") as f:
            f.write(plan_text)
        return subprocess.run(
            [sys.executable, CHECKER, plan, RESOLVER, RUNNER, UNITTESTS,
             CHECKER, SELF], capture_output=True, text=True)

    def test_green_on_conforming_plan(self):
        out = self.run_checker(conforming_plan())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("plan-contract-ok"),
                        out.stdout)

    def test_red_on_wrong_site_digest_despite_occurrence(self):
        # The command-2 resolver pin is zeroed while the true digest
        # still occurs in the artifact table and preamble: occurrence
        # no longer certifies placement.
        vd = sha256_file(RESOLVER)
        plan = conforming_plan().replace(
            "bootstrap_resolve.py | cut -d' ' -f1)\" = \"%s\"" % vd,
            "bootstrap_resolve.py | cut -d' ' -f1)\" = \"%s\"" % ("0" * 64))
        self.assertIn(vd, plan)
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("command 2 pins resolver", out.stderr)

    def test_red_on_missing_command_tag(self):
        plan = conforming_plan().replace(
            " <sha> <worktree>` (command 14)", " <sha> <worktree>`")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("command-tag bijection broken for resolve",
                      out.stderr)

    def test_red_on_misattributed_tag(self):
        plan = conforming_plan().replace(
            "`latest-binding <proof>`", "`latest-binding <proof>`"
            " (command 11)")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("command-tag bijection broken for latest-binding",
                      out.stderr)

    def test_red_on_stale_publication_primitive(self):
        plan = conforming_plan().replace(
            "link-based no-replace",
            "temp write, fsync, rename, dirfd fsync")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("stale phrase still present", out.stderr)

    def test_red_on_stale_signature_arity(self):
        plan = conforming_plan().replace(
            "`resolve <proof> <runner-sha256> <sha> <worktree>`",
            "`resolve <proof> <runner-sha256> <sha>`")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("contract signature for resolve", out.stderr)

    def test_red_on_census_drift(self):
        plan = conforming_plan().replace(
            "`test_resolver_self_test_green`, ", "", 1)
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("TestBootstrapTools census", out.stderr)

    def test_red_on_stale_record_class_count(self):
        plan = conforming_plan().replace("all seven record classes",
                                         "all six record classes")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("record classes", out.stderr)

    def test_red_on_bare_and_hyphenated_class_forms(self):
        # The r32 review's invisible spellings: `all six classes` and
        # `six-class` about the record machinery are each red.
        base = conforming_plan()
        plan = base + ("\nThe truncation fixtures cover all six classes"
                       " of framed records.\n")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("record-machinery classes", out.stderr)
        plan = base + ("\nThe fault leg performs a record-free"
                       " six-class scan.\n")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("record-machinery classes", out.stderr)

    def test_red_on_duplicate_path_pin(self):
        # A wrong earlier equality for a path must never be shadowed by
        # a correct later one on the same executable chain.
        vd = sha256_file(RESOLVER)
        good = ("[ \"$(shasum -a 256 tools/relay-engine-fixtures/"
                "bootstrap/bootstrap_resolve.py | cut -d' ' -f1)\" ="
                " \"%s\" ]" % vd)
        plan = conforming_plan().replace(
            good, good.replace(vd, "0" * 64) + " && " + good)
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("pins resolver 2 times", out.stderr)

    def test_red_on_wrong_install_path_binding(self):
        # Hashing the wrong installed file with both digest literals in
        # their original order must be red.
        plan = conforming_plan().replace(
            "\"$RELAY_ENGINE_RESULTS_ROOT/v29-engine-hostile-legs.py\" |"
            " cut",
            "\"$RELAY_ENGINE_RESULTS_ROOT/"
            "v29-engine-bootstrap-resolve.py\" | cut")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must hash exactly", out.stderr)

    def test_red_on_wrong_runner_root(self):
        # An attacker-selected root with the pinned basename must be red
        # (r33 R1: suffix matching accepted "$EVIL/...").
        plan = conforming_plan().replace(
            "\"$RELAY_ENGINE_RESULTS_ROOT/v29-engine-hostile-legs.py\" |"
            " cut",
            "\"$EVIL/v29-engine-hostile-legs.py\" | cut")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must hash exactly", out.stderr)

    def test_red_on_displaced_resolve_binding(self):
        # A wrong executable RESOLVE assignment with a correct decoy
        # occurrence later in the section must be red (r33 R1).
        good = ("RESOLVE=\"$RELAY_ENGINE_RESULTS_ROOT/"
                "v29-engine-bootstrap-resolve.py\"")
        plan = conforming_plan().replace(
            good, "RESOLVE=\"$EVIL/v29-engine-bootstrap-resolve.py\"")
        plan = plan.replace(
            "14. \"$RESOLVE\" resolve",
            "The prose decoy restates %s verbatim.\n"
            "14. \"$RESOLVE\" resolve" % good)
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must bind RESOLVE exactly once", out.stderr)
        # The wrong assignment alone, with no decoy, is also red.
        plan = conforming_plan().replace(
            good, "RESOLVE=\"$EVIL/v29-engine-bootstrap-resolve.py\"")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must bind RESOLVE exactly once", out.stderr)

    def test_red_on_destructive_op_in_plan(self):
        # Option B (PLAN-orchestrator-planner-20260808-142107.md): no
        # destructive discard op may appear in the revision-stable plan.
        # A presence check on the forbidden op -- deletion-proof, unlike
        # the r40 check 13 whose guard-deletion survived (R3). R1 of the
        # r41/r42 reviews: the recogniser must catch ordinary spellings
        # of the same operation, not exact substrings. The r41 forms -- a
        # git global option before the subcommand, a doubled-whitespace
        # reset, and equivalent ref deletion/movement -- and the r42 forms
        # -- a valid option INTERPOSED between the subcommand and its
        # destructive flag -- each returned exit 0 from the respective
        # prior gate and must now go red by subcommand+flag membership.
        for op in ('git reset --hard "$DETECTION_SHA"',
                   "git branch -D feat/v29-engine",
                   "git worktree remove ../v29-engine-wt",
                   'git -C "$PWD" reset --hard "$DETECTION_SHA"',
                   "git reset  --hard HEAD~1",
                   "git update-ref -d refs/heads/feat/v29-engine",
                   "git update-ref refs/heads/feat/v29-engine "
                   "$DETECTION_SHA",
                   "git reset --quiet --hard HEAD~1",
                   "git branch --force --delete feat/v29-engine",
                   "git update-ref --no-deref -d "
                   "refs/heads/feat/v29-engine",
                   # r42 review R1: lexed shell arguments -- a quoted
                   # flag/ref, an absolute-path git, and a shell wrapper
                   # each defeated raw whitespace tokenization and must
                   # now go red.
                   "git reset '--hard' HEAD~1",
                   'git update-ref "refs/heads/feat/v29-engine" '
                   '"$DETECTION_SHA"',
                   "/usr/bin/git reset --hard HEAD~1",
                   "sh -c 'git reset --hard HEAD~1'",
                   # r44 review R1: shell-argv reconstruction -- adjacent-
                   # fragment concatenation, backslash escapes, backslash-
                   # newline continuation, and executable-producing command
                   # substitution each defeated quote-deletion and must now
                   # go red.
                   "git re'set' --hard HEAD~1",
                   "g''it reset --hard HEAD~1",
                   "git reset --har\\d HEAD~1",
                   "git reset \\\n--hard HEAD~1",
                   "$(command -v git) reset --hard HEAD~1"):
            plan = conforming_plan() + (
                "\nStray line that must be refused: %s\n" % op)
            out = self.run_checker(plan)
            self.assertEqual(out.returncode, 1, op)
            self.assertIn("Option B forbids the destructive op",
                          out.stderr)

    def test_documents_static_obfuscation_residual(self):
        # Option 1 ruling (PLAN-orchestrator-planner-20260808-160930.md):
        # check 13 guards against regression, not evasion. These three
        # fully-static self-obfuscating forms -- alias delegation, brace
        # expansion, and ANSI-C quoting -- carry the complete destructive
        # op in the plan with no runtime data, and the good-faith
        # regression detector does NOT catch them. This control DOCUMENTS
        # the residual executably (the fault-leg move): it asserts the
        # checker returns exit 0 on each today, so the disclosure cannot
        # quietly go stale -- if a later revision ever closes one of these
        # forms, this control fails and forces an honest scope update.
        for op in ("git -c alias.wipe='reset --hard' wipe",
                   "git {reset,--hard} HEAD~1",
                   r"git $'\x72\x65\x73\x65\x74' --hard HEAD~1"):
            plan = conforming_plan() + (
                "\nStray line, an accepted static-obfuscation residual"
                " check 13 does not catch: %s\n" % op)
            out = self.run_checker(plan)
            self.assertEqual(out.returncode, 0, op)
            self.assertIn("plan-contract-ok", out.stdout)

    def test_red_on_hardcoded_handoff_pair(self):
        # A hard-coded live plan/review pair is consumed by its own
        # review and goes stale inside the document (r34 R1); the body
        # may only define the pair referentially.
        plan = conforming_plan() + ("\nThe live pair is"
                                    " v29-engine-plan-23 and"
                                    " v29-engine-plan-review-23.\n")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("hard-coded handoff id", out.stderr)

    def test_red_on_extra_lineage_line(self):
        # A second PLAN_ID: line inside the normative sections would
        # smuggle banned text out of the sweeps.
        plan = conforming_plan().replace(
            "### Task 0: the binding preflight",
            "### Task 0: the binding preflight\n"
            "PLAN_ID: fsync, rename, dirfd fsync")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 1)
        self.assertIn("exactly one PLAN_ID lineage line", out.stderr)

    def test_lineage_line_is_exempt(self):
        # Old digests and stale phrases inside the PLAN_ID lineage line
        # are history, not normative text.
        plan = conforming_plan().replace(
            "old digests may occur here",
            "old digests and the abolished fenced program wording occur"
            " here")
        out = self.run_checker(plan)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
