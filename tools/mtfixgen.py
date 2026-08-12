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
H27_NON_A4_CONFLICTS = (
    ("AUTHORITY", "report-only", "read-only"),
    ("PHASE", "SITREP", "PLAN"),
    ("FROM", "qi.master-planner", "qi.master-reviewer"),
    ("TO", "qi.implementer", "qi.pair-implementer"),
    ("DESIGN_DOC_ID", "dd-one", "dd-two"),
    ("DESIGN_REVIEW_VERDICT", "approve", "must-revise"),
    ("DISPATCH_ID", "mt-conflict-one", "mt-conflict-two"),
    ("PARENT_DISPATCH_ID", "mt-parent-one", "mt-parent-two"),
    ("COMMISSION_AUTHORIZATION", "yes", "no"),
    ("COMMISSION_ID", "commission-one", "commission-two"),
    ("COMMISSION_SCOPE", "scope-one", "scope-two"),
    ("COMMISSION_TO", "qi.pair-planner", "zz.pair-planner"),
    ("CHARTER_DOC_ID", "charter-one", "charter-two"),
)
H27_A4_CONFLICTS = (
    ("DELEGATED_DISPATCH_AUTHORITY", "yes", "no"),
    ("DESIGN_LOCK_ID", "lock-one", "lock-two"),
    ("DESIGN_RECORD_KIND", "design-doc", "audit-record"),
)


def conflict_member_name(prefix: str, family: str, key: str) -> str:
    return f"{prefix}{family}-{key.lower().replace('_', '-')}.md"


def h27_conflict_content(*, key: str, first: str, second: str) -> str:
    role = "Master Planner"
    fields = {
        "PHASE": "SITREP",
        "AUTHORITY": "read-only",
        "DISPATCH_ID": "mt-h27-conflict",
        "FROM": "qi.master-planner",
        "TO": "qi.implementer",
    }
    fields[key] = first
    extra_first = "" if key in {"PHASE", "AUTHORITY", "DISPATCH_ID", "FROM", "TO"} else f"{key}: {first}\n"
    return f"""ROLE: {role}
PHASE: {fields['PHASE']}
AUTHORITY: {fields['AUTHORITY']}
DISPATCH_ID: {fields['DISPATCH_ID']}
CEREMONY_TIER: small
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: {fields['FROM']}
TO: {fields['TO']}
{extra_first}{key}: {second}

Generated H27 distinct-value fixture.

FINAL_GIT_STATUS_SHORT: none — fixture, clean tree
"""


def h27_launder_content(*, master_first: bool, template_mode: bool) -> str:
    if template_mode:
        first, second = (
            ("<owner>.master-planner | <owner>.planner", "<owner>.planner") if master_first
            else ("<owner>.planner", "<owner>.planner | <owner>.master-planner")
        )
        role = "<role>"
    else:
        first, second = (
            ("qi.master-planner", "qi.planner") if master_first
            else ("qi.planner", "qi.master-planner")
        )
        role = "Master Planner" if master_first else "Planner"
    return f"""ROLE: {role}
PHASE: SITREP
AUTHORITY: read-only
DISPATCH_ID: mt-h27-from-launder
CEREMONY_TIER: small
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: {first}
TO: qi.implementer
FROM: {second}

Generated H27 FROM-launder fixture.

FINAL_GIT_STATUS_SHORT: none — fixture, clean tree
"""


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
    for key, first, second in H27_NON_A4_CONFLICTS:
        members[conflict_member_name("MT2", "4-conflict", key)] = h27_conflict_content(
            key=key, first=first, second=second,
        )
    for key, first, second in H27_A4_CONFLICTS:
        members[conflict_member_name("MT2", "5-a4-conflict", key)] = h27_conflict_content(
            key=key, first=first, second=second,
        )
    members["MT26-repeat-authority.md"] = h27_conflict_content(
        key="AUTHORITY", first="read-only", second="read-only",
    )
    members["MT27-repeat-delegated-dispatch-authority.md"] = h27_conflict_content(
        key="DELEGATED_DISPATCH_AUTHORITY", first="yes", second="yes",
    )
    for prefix, template_mode in (("MT2", False), ("MTT2", True)):
        members[f"{prefix}8-from-launder-master-first.md"] = h27_launder_content(
            master_first=True, template_mode=template_mode,
        )
        members[f"{prefix}9-from-launder-master-last.md"] = h27_launder_content(
            master_first=False, template_mode=template_mode,
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
