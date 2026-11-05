"""B-2: shipped reviewer instructions and their packaging contract."""

import ast
import re

from test_kit_stage_a import KitTreeCase, PLUGINS, ROOT, _generator, _section

REVIEWERS = {
    "orchestrator-reviewer": ("adt-orchestrator", "adt-master"),
    "master-reviewer": ("adt-master",),
}
ASSETS = ("review-panels.md", "reviewer-spawn-prompts.md")
VERIFICATION_RULE = [
    "Re-running a claim's own checks never verifies it alone.",
    "For text, run a probe with a control against the behaviour the sentence states.",
    "For tests, run a mutant the tests should catch.",
    "File the outputs and name them in the review.",
]
REMOVED = (
    "become questions or are dropped",
    "Findings without evidence",
    "Convert weak claims into questions",
    "Convert E0-only findings into questions",
    "Review those relays on your own cadence",
)
PLAN_CLAIMS = "Every codebase claim in a plan carries its search or probe; the reviewer re-runs at least the ones the plan's correctness rests on, and for each completeness claim also derives its own search with a control."
RELEASE = "At release only, for the kit repository's own release, run `python3 tools/release-check.py full --out <dir under $HOME>`."
AUTHORITY = "The claim kinds are the authority pair's (DD-v3-authority A-OP2); until its table is published, verify every claim by the rule above."
QUESTION = "Ask for an operator decision with a `KIND: QUESTION` relay addressed to `operator` (DD-v3-engine § 3)."
PLAIN = "Write every operator question in plain language: explain the decision and its consequences without requiring knowledge of relay internals."


def lines(text):
    return text.splitlines()


class TestReviewRules(KitTreeCase):
    def shared_copies(self, name):
        return {f"shared/{name}": self.canonical_text(f"shared/{name}"), **self.installed_shared_copies(name)}

    def test_advisory_template_has_no_brackets(self):
        for where, text in self.shared_copies("review-panels.md").items():
            with self.subTest(where=where):
                header = _section(text, "## Reviewer output").split("In the review body:", 1)[0]
                finding_lines = [line for line in text.splitlines() if line.startswith("FINDING:")]
                self.assertTrue(finding_lines)
                self.assertFalse(any("[" in line or "]" in line for line in finding_lines))
                self.assertIn("FINDING: advisory <summary>", header.splitlines())
                self.assertIn("FINDING: advisory <check-REF-index> <summary>", header.splitlines())

    def test_reviewer_names_three_duties(self):
        rel = "skills/orchestrator-reviewer/SKILL.md"
        copies = {rel: self.canonical_text(rel), **self.installed_texts(rel)}
        expected = "Your addressed duties are verification requests at a pair's completion claim, gate reviews and reconcile requests; each is addressed `TO` your reviewer seat."
        for where, text in copies.items():
            with self.subTest(where=where):
                self.assertIn(expected, _section(text, "## Verification requests").splitlines())

    def test_blocking_finding_needs_check(self):
        wanted = {
            "review-panels.md": [
                "FINDING: blocking <check-REF-index> <summary>",
                "FINDING: advisory <summary>",
                "FINDING: advisory <check-REF-index> <summary>",
                "Put `FINDING` lines in the relay's header block; `<check-REF-index>` is the 1-based position of a `REF` line in that header.",
                "A blocking finding's `REF` names the executed check's output.",
                "A finding from reading alone is `advisory`.",
                "The finding syntax is decided by `_findings` in `tools/relay_engine/header.py`; the status set is `_FINDING_STATUS` in `tools/relay_engine/schema.py`.",
                "No round cap applies; the loop signal is in DD-v3-engine § 8 and the round count in § 9.",
            ],
            "reviewer-spawn-prompts.md": [
                "- In the relay's header block, use `FINDING: blocking <check-REF-index> <summary>`; its `REF` names the executed check's output.",
                "- `<check-REF-index>` is the 1-based position of a `REF` line in that header; use `FINDING: advisory [<check-REF-index>] <summary>` for advisories.",
                "- A finding from reading alone is `advisory`.",
            ],
        }
        for name in ASSETS:
            for where, text in self.shared_copies(name).items():
                with self.subTest(where=where):
                    body = _section(text, "## Reviewer output" if name == ASSETS[0] else "## Shared review context")
                    for line in wanted[name]:
                        self.assertIn(line, lines(body))
                    for gone in REMOVED:
                        self.assertNotIn(gone, text)
                    if name == ASSETS[0]:
                        consolidation = lines(_section(text, "## Consolidation rules"))
                        self.assertIn("- A blocking finding blocks until `fix-accepted` or `withdrawn`; only the raiser or the orchestrator resolves it (DD-v3-engine § 8).", consolidation)
                        self.assertIn("- Panel verdicts `block` and `must-fix` map to `blocking` findings; `optional` maps to `advisory`.", consolidation)
                        self.assertIn("- Preserve reading-only findings as advisory observations.", consolidation)

    def test_reviewer_verification_rule(self):
        for where, text in self.shared_copies("review-panels.md").items():
            with self.subTest(where=where):
                self.assertEqual([line for line in lines(_section(text, "## Verification rule")) if line.strip()], VERIFICATION_RULE)
        for role, tiers in REVIEWERS.items():
            paths = [ROOT / "skills" / role / "SKILL.md"]
            paths += [PLUGINS / tier / "skills" / role / "SKILL.md" for tier in tiers]
            for path in paths:
                with self.subTest(path=path):
                    text = path.read_text(encoding="utf-8")
                    body = lines(_section(text, "## Verification requests"))
                    for line in (("Your addressed duties are verification requests at a pair's completion claim, gate reviews and reconcile requests; each is addressed `TO` your reviewer seat." if role == "orchestrator-reviewer" else "Your work is the verification request addressed `TO` your reviewer seat at a pair's completion claim."), "Run the checks the claim rests on and follow the adjacent `review-panels.md` § Verification rule.", AUTHORITY, RELEASE, "The release runner's planned subcommands are `consumers`, `frank`, `docs` and `all` (DD-v3-tests § 3.2); each must exist at the release head.", "Cite each command's output file.", "`CC` is visibility only; it is not a verification request or a reply obligation."):
                        self.assertIn(line, body)
                    for gone in REMOVED:
                        self.assertNotIn(gone, text)
                    if role == "orchestrator-reviewer":
                        self.assertIn("Independently examine copied orchestrator rulings and authority-bearing relays on your own cadence for decomposition, routing, sequencing, stale state, scope or boundary, and verification risks; file supported findings under your standing reviewer role without waiting for a Planner-selected addressed request.", lines(_section(text, "## Orchestrator-review visibility gate")))
        # Check every installed adjacent pointer, including spawn prompts.
        paths = list(PLUGINS.glob("*/skills/*/reviewer-spawn-prompts.md"))
        paths += [PLUGINS / tier / "skills" / role / "SKILL.md" for role, tiers in REVIEWERS.items() for tier in tiers]
        for path in paths:
            pointers = re.findall(r"`([a-z-]+\.md)` § (Verification rule|Verification[a-z ]*)", path.read_text(encoding="utf-8"))
            self.assertTrue(pointers, str(path))
            for name, heading in pointers:
                target = path.parent / name
                self.assertTrue(target.is_file(), str(target))
                self.assertIn(f"## {heading}", lines(target.read_text(encoding="utf-8")))

    def test_reviewer_skills_receive_review_panels(self):
        # Independent consumer row expectation catches a green reversion of the mirror.
        module = ast.parse(self.canonical_text("tools/check-generate-plugins.py"))
        mapping = next(n.value for n in module.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LOCKED_SHARED_ASSETS" for t in n.targets))
        rows = {ast.literal_eval(k): v for k, v in zip(mapping.keys, mapping.values)}
        for name in ASSETS:
            self.assertEqual(set(ast.literal_eval(rows[name].args[0])), {"pair-planner", "orchestrator-planner", "orchestrator-reviewer", "master-reviewer"})
        for role, tiers in REVIEWERS.items():
            self.assertEqual(set(self.installed_texts(f"skills/{role}/SKILL.md")), set(tiers))
            for tier in tiers:
                for name in ASSETS:
                    with self.subTest(tier=tier, role=role, name=name):
                        banner = f"<!-- GENERATED by tools/generate-plugins.py from shared/{name} — kit v{_generator().KIT_VERSION}. Do not edit; edit the canonical source. -->\n\n"
                        expected = banner.encode() + (ROOT / "shared" / name).read_bytes()
                        path = PLUGINS / tier / "skills" / role / name
                        self.assertTrue(path.is_file(), str(path))
                        self.assertEqual(path.read_bytes(), expected)

    def test_plan_claims_carry_searches(self):
        anchors = {"pair-planner": "Decompose it into right-sized tasks with their file structure, and review it yourself before sending it (see Practices).", "pair-implementer": "Review your pair Planner's written plan before implementation."}
        for role, anchor in anchors.items():
            rel = f"skills/{role}/SKILL.md"
            copies = {rel: self.canonical_text(rel), **self.installed_texts(rel)}
            for where, text in copies.items():
                with self.subTest(role=role, where=where):
                    body = lines(text)
                    self.assertEqual(body.count(PLAN_CLAIMS), 1)
                    self.assertEqual(body[body.index(anchor) + 1], PLAN_CLAIMS)

    def test_spawn_prompts_question_rule(self):
        for where, text in self.shared_copies("reviewer-spawn-prompts.md").items():
            with self.subTest(where=where):
                self.assertIsNone(re.search(r"human[\s_-]*gate[\s_-]*required", text, re.I))
                intro = lines(text.split("## Shared review context", 1)[0])
                for line in (QUESTION, PLAIN, "Use `NEEDS: answer` for seat-to-seat questions.", "Follow `review-panels.md` § Verification rule for every spawned review."):
                    self.assertIn(line, intro)
