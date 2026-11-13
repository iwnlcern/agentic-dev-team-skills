"""Routed Kit documentation: silent-counterpart copy and generated parity."""

from __future__ import annotations

from test_kit_stage_a import KitTreeCase, PLUGINS, ROOT


PAIR_LINES: tuple[str, ...] = (
    "When a relay you are waiting on from another seat is well past when it was due by your pair's own pace, send that seat one short relay saying what you are waiting for and the last relay you saw from it, and asking it to report or to say it cannot go on.",
    "Copy the Orchestrator Planner in an orchestrated run, and the silent seat's pair Planner when that is not you.",
    "First check that the seat is not just waiting its turn behind other work.",
    "Send one nudge per wait; if it brings nothing, say so to the Orchestrator Planner, or to the operator in a standalone run.",
    "A nudge grants nothing, changes no grant and starts no work, and it is no reason to act for the silent seat or to read its worktree.",
)
ORCHESTRATOR_LINES: tuple[str, ...] = (
    "**Silent seats.** When a seat owes you a relay and it is well past when it was due, send that seat one short relay saying what you are waiting for and the last relay you saw from it, and asking it to report or to say it cannot go on; copy its pair Planner.",
    "First check that the seat is not just waiting its turn behind other work.",
    "Send one nudge per wait, from waits you already know of; this is not a reason to poll.",
    "When a nudge, yours or a pair's, brings nothing, tell the operator.",
    "A nudge grants nothing, changes no grant and starts no work, and it is no reason to act for the silent seat or to read its worktree.",
)
ANCHORS: dict[str, tuple[str, str]] = {
    "pair-planner": ("## Silent counterpart", "## Standalone pair (no orchestrator)"),
    "pair-implementer": ("## Silent counterpart", "## Implementation report"),
    "orchestrator-planner": (
        "- the merge report arrives — then run the MERGE/LIVE-VERIFY gate.",
        "## Routing rules",
    ),
}


class TestRoutedDocs(KitTreeCase):
    """Missing, duplicated or misplaced nudge copy must fail in every tier."""

    def test_seat_nudge_paragraph(self):
        for role, (before, after) in ANCHORS.items():
            pair_role = role != "orchestrator-planner"
            expected = PAIR_LINES if pair_role else ORCHESTRATOR_LINES
            rel = f"skills/{role}/SKILL.md"
            copies = {rel: self.canonical_text(rel)}
            variant = ROOT / "skills" / "variants" / role / "SKILL.md"
            if variant.is_file():
                copies[variant.relative_to(ROOT).as_posix()] = variant.read_text(encoding="utf-8")
            installed = self.installed_texts(rel)
            tiers = {"adt-orchestrator", "adt-master"}
            if pair_role:
                tiers.add("adt-pair")
            self.assertEqual(set(installed), tiers, role)
            copies.update({f"plugins/{tier}/{rel}": text for tier, text in installed.items()})

            for where, text in copies.items():
                lines = text.splitlines()
                for line in expected:
                    self.assertEqual(lines.count(line), 1, f"{where}: {line}")
                start = lines.index(expected[0])
                paragraph = lines[start:start + 5]
                self.assertEqual(paragraph, list(expected), where)
                self.assertGreaterEqual(start, 2, where)
                self.assertEqual(lines[start - 2:start], [before, ""], where)
                self.assertEqual(lines[start + 5:start + 7], ["", after], where)
                if pair_role:
                    self.assertEqual(lines.count("## Silent counterpart"), 1, where)
                    self.assertIn(
                        "A nudge grants nothing, changes no grant and starts no work, and it is no reason to act for the silent seat or to read its worktree.",
                        paragraph,
                        where,
                    )
                else:
                    self.assertIn(
                        "Send one nudge per wait, from waits you already know of; this is not a reason to poll.",
                        paragraph,
                        where,
                    )
                self.assertNotRegex("\n".join(paragraph), r"\d", where)

        protocols = {"shared/protocol.md": self.canonical_text("shared/protocol.md")}
        protocols.update({
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(PLUGINS.glob("*/skills/*/protocol.md"))
        })
        for where, text in protocols.items():
            self.assertNotIn("nudge", text.lower(), where)
