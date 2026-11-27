"""Placement and shipped reply guidance for the eight Kit role skills."""

import sys
from unittest.mock import patch

from test_kit_stage_a import KitTreeCase, PLUGINS, ROOT

REPLY_BODY: tuple[str, str, str] = (
    "A reply may answer more than one relay: `PARENT` names one, and each other relay it answers goes on its own `ANSWERS` line, written as `PARENT` is, `#<n>` or a lowercase UUID.",
    "Do not list the parent in `ANSWERS` as well.",
    "The engine refuses an `ANSWERS` line that names no relay of the project (`E-ANSWERS-UNKNOWN`), a relay not sent to you in `TO` or `CC` (`E-ANSWERS-NOT-ADDRESSED`), a relay you have not read yet (`E-ANSWERS-UNOPENED`), or the parent or a relay already listed (`E-ANSWERS-REPEATED`).",
)
REPLY_LEAD = "**Replies.** "
ANSWERS_CODES: tuple[str, ...] = (
    "E-ANSWERS-UNKNOWN",
    "E-ANSWERS-NOT-ADDRESSED",
    "E-ANSWERS-UNOPENED",
    "E-ANSWERS-REPEATED",
)
REPLY_ANCHORS: dict[str, tuple[str, str | None]] = {
    'pair-planner': (
        '12. **MERGE/LIVE-VERIFY gate** — recommend one verdict from `protocol.md`. Treat merge, deploy, and live verification as separate evidence levels.',
        '## Silent counterpart',
    ),
    'pair-implementer': (
        'Copy the actual command output for your act, not these example values.',
        '## Silent counterpart',
    ),
    'orchestrator-planner': (
        'Prefer file-first relays under `.relays/<RUN_ID>/` using the layout in `protocol.md`; include the addressee in the INDEX `to` column. Print the pointer context — `FROM` and a 3-6 line summary — followed by the exact terminal `RELAY`/`TO`/`CC` hand-off block per `protocol.md`. For v3 delivery, connect with `adt connect --key <key-path> [--switch]` (DD-v3-adapter § 4.1–4.2); the pointer block remains the fallback. If a receiver cannot access the path, relay or attach the file contents.',
        '## Ready work and real dependencies',
    ),
    'orchestrator-reviewer': (
        'Do not replace a missing relay with a review panel; require the relay to be written, then review it.',
        '## Orchestrator-review visibility gate',
    ),
    'master-planner': (
        '**Authority (DD-v29-master-authority-20260809).** This seat carries no `DISPATCH IMPL` or `DISPATCH MERGE`; commissioned work is executed by the commissioned pair under the settled pair gate, whose pair planner issues the token. It may issue reviewer-gated design locks only for governance artifacts—cross-domain decomposition records and sub-team charters—as record kind `design-doc` or `audit-record`; a lock becomes citable only after a master-reviewer `DESIGN-REVIEW` approval parented to the exact originating `DESIGN` or planner `AUDIT` relay. It may grant delegation but never receive it; only a commissioning `PLAN` dispatch, under the equality-bound commissioning machine, may carry a grant, addressed to the chartered pair planner and citing the operator-authorized scope and charter, never to itself, any master/domain seat, or outside a commission. `direct-override` is never allowed.',
        '## Ready work and real dependencies',
    ),
    'master-reviewer': (
        '**Authority (DD-v29-master-authority-20260809).** This seat carries no `DISPATCH IMPL` or `DISPATCH MERGE`; it may cite locks; never issues them. It never grants or receives delegation, and `direct-override` is never allowed.',
        '## Ready work and real dependencies',
    ),
    'domain-planner': (
        "**Authority (DD-v29-master-authority-20260809).** This seat carries no `DISPATCH IMPL` or `DISPATCH MERGE`; commissioned pair work implements the spec-of-record change and cites the domain lock. It may issue reviewer-gated locks only for the domain spec of record and its scoped addenda as record kind `design-doc` or `audit-record`; a lock becomes citable only after a domain-reviewer `DESIGN-REVIEW` approval parented to the exact originating `DESIGN` or planner `AUDIT` relay. It never grants or receives delegation; its `amend` / `accept` / `decline-with-reason` rulings govern content, while execution rides the requesting sub-team's existing commission. `direct-override` is never allowed.",
        None,
    ),
    'domain-reviewer': (
        '**Authority (DD-v29-master-authority-20260809).** This seat carries no `DISPATCH IMPL` or `DISPATCH MERGE`; it may cite locks; never issues them. It never grants or receives delegation, and `direct-override` is never allowed. Consumer review of adjacent specs at locked seams is citation-only by construction.',
        None,
    ),
}

PAIR_ROLES = ("pair-planner", "pair-implementer")
INSTALLED_TIERS = {
    **dict.fromkeys(PAIR_ROLES, {"adt-pair", "adt-orchestrator", "adt-master"}),
    **dict.fromkeys(("orchestrator-planner", "orchestrator-reviewer"), {"adt-orchestrator", "adt-master"}),
    **dict.fromkeys(("master-planner", "master-reviewer", "domain-planner", "domain-reviewer"), {"adt-master"}),
}


GO_AHEAD_LINES: tuple[str, str, str, str] = (
    'If an implementing agent approves a plan, receives its go-ahead, and later asks for revisions to that same plan, it can still commit under the go-ahead it holds until that go-ahead is revoked or its bundle closes.',
    'A seat holds one live go-ahead per bundle and repository: a newer one replaces the earlier.',
    "To stop a seat's work in a bundle, revoke its go-ahead there.",
    "No page or command lists a seat's live go-aheads.",
)
STEP_8_LAST: str = 'After approve, a correction that leaves bound items unchanged is an erratum with a delta review of the erratum alone, per the Plan contract section of `protocol.md`; a change to a bound item is a reissue.'


class TestSeatReplies(KitTreeCase):
    def _role_copies(self, role):
        rel = f"skills/{role}/SKILL.md"
        yield "canonical", self.canonical_text(rel)
        if role in PAIR_ROLES:
            yield "variant", self.canonical_text(f"skills/variants/{role}/SKILL.md")
        installed = self.installed_texts(rel)
        self.assertEqual(set(installed), INSTALLED_TIERS[role])
        yield from installed.items()

    def _assert_placement(self, role, text):
        lines = text.splitlines()
        b1, b2, b3 = REPLY_BODY
        before, after = REPLY_ANCHORS[role]
        if role in PAIR_ROLES:
            self.assertEqual(lines.count("## Replies"), 1)
            at = lines.index("## Replies")
            block = ["## Replies", "", b1, b2, b3]
        else:
            lead = REPLY_LEAD + b1
            self.assertEqual(lines.count(lead), 1)
            at = lines.index(lead)
            block = [lead, b2, b3]
        self.assertGreaterEqual(at, 2)
        self.assertEqual(lines[at - 2 : at + len(block)], [before, "", *block])
        if after is None:
            self.assertEqual(lines[at + len(block) :], [])
            self.assertTrue(text.endswith("\n"))
        else:
            self.assertEqual(lines[at + len(block) : at + len(block) + 2], ["", after])

    def _assert_code(self, b3, code, errors):
        self.assertIn(f"(`{code}`)", b3)
        self.assertIn(code, errors)

    def test_reply_paragraph(self):
        with patch.object(sys, "path", [str(ROOT / "tools"), *sys.path]):
            from relay_engine.errors import ERRORS

        for role in REPLY_ANCHORS:
            for copy, text in self._role_copies(role):
                with self.subTest(role=role, copy=copy):
                    lines = text.splitlines()
                    for line in REPLY_BODY[1:]:
                        if lines.count(line) != 1:
                            self.assertEqual(lines.count(line), 1)
                    self._assert_placement(role, text)
                    b3 = lines[lines.index(REPLY_BODY[2])]
                    for code in ANSWERS_CODES:
                        with self.subTest(code=code):
                            self._assert_code(b3, code, ERRORS)
                            with self.assertRaises(AssertionError):
                                self._assert_code(b3.replace(code, ""), code, ERRORS)
                    if REPLY_ANCHORS[role][1] is None:
                        with self.assertRaises(AssertionError):
                            self._assert_placement(role, text + "Trailing material.\n")

        for path in [ROOT / "shared/protocol.md", *sorted(PLUGINS.glob("*/skills/*/protocol.md"))]:
            with self.subTest(protocol=path.relative_to(ROOT)):
                self.assertNotIn("ANSWERS", path.read_text(encoding="utf-8"))


    def test_live_go_ahead_line(self):
        for copy, text in self._role_copies("pair-planner"):
            with self.subTest(copy=copy):
                lines = text.splitlines()
                for line in GO_AHEAD_LINES:
                    self.assertEqual(lines.count(line), 1)
                at = lines.index(GO_AHEAD_LINES[0])
                self.assertGreater(at, 0)
                self.assertEqual(lines[at - 1 : at + 4], [STEP_8_LAST, *GO_AHEAD_LINES])
                self.assertLess(at + 4, len(lines))
                self.assertTrue(lines[at + 4].startswith("9. **Implementation dispatch**"))
        protocol = self.canonical_text("shared/protocol.md")
        for line in GO_AHEAD_LINES:
            self.assertNotIn(line, protocol)
