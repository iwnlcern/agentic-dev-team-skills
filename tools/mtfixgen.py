#!/usr/bin/env python3
"""Generate deterministic master-tier file and template fixtures."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tools" / "relay-lint-fixtures" / "mastertier"
SMOKE_CONTENT = """ROLE: Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: mt0-generator-smoke
CEREMONY_TIER: small
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: qi.planner
TO: qi.implementer

Generated fixture smoke control.

ACTIONS_GIT_REF: none — fixture relay only
FINAL_GIT_STATUS_SHORT: none — fixture, clean tree
"""

MASTER_TIER_SEATS = (
    ("master-planner", "Master Planner"),
    ("master-reviewer", "Master Reviewer"),
    ("domain-planner", "Domain Planner"),
    ("domain-reviewer", "Domain Reviewer"),
)


def fixture_content(*, role: str, from_addr: str, prohibition: str) -> str:
    if prohibition == "dispatch-impl":
        phase, authority, body = "IMPL", "implementation", "DISPATCH IMPL\n"
    elif prohibition == "dispatch-merge":
        phase, authority, body = "MERGE-GATE", "merge-gated", "DISPATCH MERGE\n"
    else:
        phase, authority, body = "PLAN", "plan-only", "DESIGN_RECORD_KIND: direct-override\n"
    return f"""ROLE: {role}
PHASE: {phase}
AUTHORITY: {authority}
DISPATCH_ID: mt-{prohibition}
CEREMONY_TIER: small
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: {from_addr}
TO: qi.implementer

{body}
FINAL_GIT_STATUS_SHORT: none — fixture, clean tree
"""


def generated_members() -> dict[str, str]:
    members = {"MT0-generator-smoke.md": SMOKE_CONTENT}
    for mode in ("file", "template"):
        prefix = "MT2" if mode == "file" else "MTT2"
        for number, prohibition in enumerate(("dispatch-impl", "dispatch-merge", "direct-override"), start=1):
            for seat, role in MASTER_TIER_SEATS:
                members[f"{prefix}{number}-{prohibition}-{seat}.md"] = fixture_content(
                    role=role, from_addr=f"qi.{seat}", prohibition=prohibition,
                )
            if prohibition == "dispatch-impl":
                control_role, control_from = "Planner", "qi.planner"
            elif prohibition == "dispatch-merge":
                control_role, control_from = "Orchestrator Planner", "qi.orchestrator-planner"
            else:
                control_role, control_from = "Operator", "operator"
            members[f"{prefix}{number}-{prohibition}-control.md"] = fixture_content(
                role=control_role, from_addr=control_from, prohibition=prohibition,
            )
    return members


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    members = generated_members()
    for path in FIXTURE_DIR.glob("*.md"):
        if path.name not in members:
            path.unlink()
    for name, content in members.items():
        (FIXTURE_DIR / name).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
