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
LIFECYCLE_CONSUMERS = (
    ("pair-planner", "Pair Planner", "beta.pair-planner", "beta.pair-implementer", "PLAN", "plan-only"),
    ("pair-implementer", "Pair Implementer", "beta.pair-implementer", "beta.pair-planner", "SITREP", "report-only"),
    ("master-planner", "Master Planner", "beta.master-planner", "beta.master-reviewer", "PLAN", "plan-only"),
    ("master-reviewer", "Master Reviewer", "beta.master-reviewer", "beta.master-planner", "SITREP", "report-only"),
    ("domain-planner", "Domain Planner", "beta.domain-planner", "beta.domain-reviewer", "PLAN", "plan-only"),
    ("domain-reviewer", "Domain Reviewer", "beta.domain-reviewer", "beta.domain-planner", "SITREP", "report-only"),
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

COMMISSION_SURFACE_FIELDS = (
    "COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
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


def dispatch_id_conflict_content() -> str:
    """Pair-tier relay with two distinct DISPATCH_ID values for C14."""
    return lifecycle_relay(
        role="Planner",
        phase="SITREP",
        authority="report-only",
        dispatch_id="di1-first",
        from_addr="qi.planner",
        to_addr="qi.implementer",
        fields=(("DISPATCH_ID", "di1-second"),),
    )


def h27_launder_content(*, master_first: bool, template_mode: bool, template_owner: str = "<owner>") -> str:
    if template_mode:
        first, second = (
            (f"{template_owner}.master-planner | {template_owner}.planner", f"{template_owner}.planner") if master_first
            else (f"{template_owner}.planner", f"{template_owner}.planner | {template_owner}.master-planner")
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


def lifecycle_relay(
    *, role: str, phase: str, authority: str, dispatch_id: str,
    from_addr: str, to_addr: str, fields: tuple[tuple[str, str], ...] = (),
) -> str:
    extra = "".join(f"{key}:{' ' + value if value else ''}\n" for key, value in fields)
    return f"""ROLE: {role}
PHASE: {phase}
AUTHORITY: {authority}
DISPATCH_ID: {dispatch_id}
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: yes
FROM: {from_addr}
TO: {to_addr}
{extra}FINAL_GIT_STATUS_SHORT: none — generated lifecycle fixture
"""


def origin_relay(
    case: str, *, owner: str = "alpha", tier: str = "master", kind: str | None = "design-doc",
    phase: str | None = None, authority: str | None = None, reviewer: bool = False,
    dispatch_id: str | None = None, extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    planner_role = f"{tier}-planner"
    reviewer_role = f"{tier}-reviewer"
    from_role = reviewer_role if reviewer else planner_role
    resolved_phase = phase or ("DESIGN" if kind == "design-doc" else "AUDIT")
    resolved_authority = authority or ("design-only" if resolved_phase == "DESIGN" else "report-only")
    fields = [("DESIGN_DOC_ID", f"lock-{case}")]
    if kind is not None:
        fields.append(("DESIGN_RECORD_KIND", kind))
    fields.extend(extra_fields)
    return lifecycle_relay(
        role=" ".join(part.title() for part in from_role.split("-")),
        phase=resolved_phase,
        authority=resolved_authority,
        dispatch_id=dispatch_id or f"{case}-origin",
        from_addr=f"{owner}.{from_role}",
        to_addr=f"{owner}.{reviewer_role if not reviewer else planner_role}",
        fields=tuple(fields),
    )


def review_relay(
    case: str, *, owner: str = "alpha", tier: str = "master", kind: str | None = "design-doc",
    verdict: str = "approve", parent: str | None = None, dispatch_id: str | None = None,
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    fields = [("PARENT_DISPATCH_ID", parent or f"{case}-origin"), ("DESIGN_DOC_ID", f"lock-{case}")]
    if kind is not None:
        fields.append(("DESIGN_RECORD_KIND", kind))
    fields.extend(extra_fields)
    fields.append(("DESIGN_REVIEW_VERDICT", verdict))
    return lifecycle_relay(
        role=f"{tier.title()} Reviewer",
        phase="DESIGN-REVIEW",
        authority="review-only",
        dispatch_id=dispatch_id or f"{case}-review",
        from_addr=f"{owner}.{tier}-reviewer",
        to_addr=f"{owner}.{tier}-planner",
        fields=tuple(fields),
    )


def consumer_relay(
    case: str, *, role: str = "Pair Planner", from_addr: str = "beta.pair-planner",
    to_addr: str = "beta.pair-implementer", phase: str = "PLAN", authority: str = "plan-only",
    kind: str | None = "design-doc", lock_id: str | None = None,
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    fields = [("DESIGN_LOCK_ID", lock_id or f"lock-{case}")]
    if kind is not None:
        fields.append(("DESIGN_RECORD_KIND", kind))
    fields.extend(extra_fields)
    return lifecycle_relay(
        role=role,
        phase=phase,
        authority=authority,
        dispatch_id=f"{case}-consumer",
        from_addr=from_addr,
        to_addr=to_addr,
        fields=tuple(fields),
    )


def add_chain(
    members: dict[str, str], name: str, *, case: str | None = None,
    origin: str | None = None, reviews: tuple[str, ...] = (), consumer: str | None = None,
) -> None:
    fixture_case = case or name.lower()
    if origin is None:
        origin = origin_relay(fixture_case)
    members[f"{name}/01-origin.md"] = origin
    for index, review in enumerate(reviews or (review_relay(fixture_case),), start=2):
        members[f"{name}/{index:02d}-review.md"] = review
    members[f"{name}/{len(reviews or (review_relay(fixture_case),)) + 2:02d}-consumer.md"] = (
        consumer or consumer_relay(fixture_case)
    )


def c13_dispatch_index_subtree() -> dict[str, str]:
    """Pair-lineage topology whose second DISPATCH_ID changes one diagnostic."""
    return {
        "01-plan.md": lifecycle_relay(
            role="Planner", phase="PLAN", authority="plan-only",
            dispatch_id="c13-plan", from_addr="alpha.planner", to_addr="alpha.implementer",
        ),
        "02-plan-review.md": lifecycle_relay(
            role="Implementer", phase="PLAN-REVIEW", authority="review-only",
            dispatch_id="c13-decoy", from_addr="alpha.implementer", to_addr="alpha.planner",
            fields=(
                ("DISPATCH_ID", "c13-review"),
                ("PARENT_DISPATCH_ID", "c13-plan"),
            ),
        ),
        "03-dispatch.md": lifecycle_relay(
            role="Planner", phase="PLAN", authority="plan-only",
            dispatch_id="c13-dispatch", from_addr="alpha.planner", to_addr="alpha.implementer",
            fields=(
                ("PARENT_DISPATCH_ID", "c13-review"),
                ("DELEGATED_DISPATCH_AUTHORITY", "yes"),
                ("SCOPE_DIFF", "\n- src/fix.ts -> in"),
                ("SCOPE_DIFF_RESULT", "all-in"),
            ),
        ).replace(
            "FINAL_GIT_STATUS_SHORT: none — generated lifecycle fixture\n",
            "\nDISPATCH IMPL\n\nFINAL_GIT_STATUS_SHORT: none — generated lifecycle fixture\n",
        ).replace("SCOPE_DIFF: \n", "SCOPE_DIFF:\n"),
    }


def commission_surface(
    case: str, *, overrides: dict[str, str] | None = None,
    omit: tuple[str, ...] = (), extra: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[str, str], ...]:
    values = {
        "COMMISSION_ID": case,
        "COMMISSION_SCOPE": f"scope-{case}",
        "COMMISSION_TO": "beta.pair-planner",
        "CHARTER_DOC_ID": f"CH-{case}",
    }
    values.update(overrides or {})
    fields = [(key, values[key]) for key in COMMISSION_SURFACE_FIELDS if key not in omit]
    fields.extend(extra)
    return tuple(fields)


def commission_authorization(
    case: str, *, value: str = "yes", role: str = "Operator", from_addr: str = "operator",
    to_addr: str = "alpha.master-planner", phase: str = "PLAN", authority: str = "plan-only",
    dispatch_id: str | None = None, surface: tuple[tuple[str, str], ...] | None = None,
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    return lifecycle_relay(
        role=role, phase=phase, authority=authority,
        dispatch_id=dispatch_id or f"{case}-auth", from_addr=from_addr, to_addr=to_addr,
        fields=(("COMMISSION_AUTHORIZATION", value),) + (
            surface if surface is not None else commission_surface(case)
        ) + extra_fields,
    )


def commission_charter(
    case: str, *, authority: str = "design-only", kind: str | None = "design-doc",
    parent: str | None = None,
    include_parent: bool = True,
    phase: str = "DESIGN",
    design_doc_id: str | None = None,
    dispatch_id: str | None = None, surface: tuple[tuple[str, str], ...] | None = None,
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    fields = []
    if include_parent:
        fields.append(("PARENT_DISPATCH_ID", parent or f"{case}-auth"))
    fields.append(("DESIGN_DOC_ID", design_doc_id or f"CH-{case}"))
    if kind is not None:
        fields.append(("DESIGN_RECORD_KIND", kind))
    fields.extend(surface if surface is not None else commission_surface(case))
    fields.extend(extra_fields)
    return lifecycle_relay(
        role="Master Planner", phase=phase, authority=authority,
        dispatch_id=dispatch_id or f"{case}-charter", from_addr="alpha.master-planner",
        to_addr="alpha.master-reviewer", fields=tuple(fields),
    )


def commission_approval(
    case: str, *, kind: str | None = "design-doc", verdict: str | None = "approve",
    parent: str | None = None, include_parent: bool = True,
    dispatch_id: str | None = None, role: str = "Master Reviewer",
    from_addr: str = "alpha.master-reviewer",
    surface: tuple[tuple[str, str], ...] | None = None,
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    fields = []
    if include_parent:
        fields.append(("PARENT_DISPATCH_ID", parent or f"{case}-charter"))
    fields.append(("DESIGN_DOC_ID", f"CH-{case}"))
    if kind is not None:
        fields.append(("DESIGN_RECORD_KIND", kind))
    if verdict is not None:
        fields.append(("DESIGN_REVIEW_VERDICT", verdict))
    fields.extend(surface if surface is not None else commission_surface(case))
    fields.extend(extra_fields)
    return lifecycle_relay(
        role=role, phase="DESIGN-REVIEW", authority="review-only",
        dispatch_id=dispatch_id or f"{case}-approval", from_addr=from_addr,
        to_addr="alpha.master-planner",
        fields=tuple(fields),
    )


def commission_grant(
    case: str, *, value: str = "yes", to_addr: str = "beta.pair-planner",
    role: str = "Master Planner", from_addr: str = "alpha.master-planner",
    dispatch_id: str | None = None, parent: str | None = None, include_parent: bool = True,
    surface: tuple[tuple[str, str], ...] | None = None,
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    return lifecycle_relay(
        role=role, phase="PLAN", authority="plan-only",
        dispatch_id=dispatch_id or f"{case}-grant", from_addr=from_addr, to_addr=to_addr,
        fields=(((("PARENT_DISPATCH_ID", parent or f"{case}-approval"),) if include_parent else ()) +
                (("DELEGATED_DISPATCH_AUTHORITY", value),) +
                (surface if surface is not None else commission_surface(case)) + extra_fields),
    )


def commission_receipt(
    case: str, *, from_addr: str = "beta.pair-planner",
    surface: tuple[tuple[str, str], ...] | None = None,
) -> str:
    return lifecycle_relay(
        role="Pair Planner", phase="PLAN", authority="plan-only",
        dispatch_id=f"{case}-receipt", from_addr=from_addr, to_addr="beta.pair-implementer",
        fields=surface if surface is not None else commission_surface(case),
    )


def direct_delegation(
    case: str, *, from_addr: str = "operator", role: str = "Operator",
    to_addr: str = "beta.pair-planner",
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    """A settled direct grant used to prove commission-carrier insulation."""
    return lifecycle_relay(
        role=role, phase="PLAN", authority="plan-only",
        dispatch_id=f"{case}-direct", from_addr=from_addr, to_addr=to_addr,
        fields=(("DELEGATED_DISPATCH_AUTHORITY", "yes"),) + extra_fields,
    )


def add_commission_chain(
    members: dict[str, str], name: str, case: str, *,
    auth: str | None = None, charter: str | None = None, approval: str | None = None,
    grant: str | None = None, receipt: str | None = None,
) -> None:
    members[f"{name}/01-auth.md"] = auth or commission_authorization(case)
    members[f"{name}/02-charter.md"] = charter or commission_charter(case)
    members[f"{name}/03-approval.md"] = approval or commission_approval(case)
    members[f"{name}/04-grant.md"] = grant or commission_grant(case)
    members[f"{name}/05-receipt.md"] = receipt or commission_receipt(case)


def commission_members() -> dict[str, str]:
    members: dict[str, str] = {}
    add_commission_chain(members, "CM77-valid-chain", "cm77")

    members["CM78-auth-uppercase/01-auth.md"] = commission_authorization("cm78", value="YES")
    members["CM79-auth-wrong-phase/01-auth.md"] = commission_authorization("cm79", phase="SITREP")
    members["CM80-auth-wrong-authority/01-auth.md"] = commission_authorization("cm80", authority="read-only")
    members["CM81-auth-wrong-grantor/01-auth.md"] = commission_authorization(
        "cm81", role="Master Reviewer", from_addr="alpha.master-reviewer",
    )
    members["CM82-auth-wrong-to/01-auth.md"] = commission_authorization("cm82", to_addr="alpha.master-reviewer")
    members["CM270-auth-to-multiple/01-auth.md"] = commission_authorization(
        "cm270", to_addr="alpha.master-planner, gamma.master-planner",
    )
    members["CM83-auth-nonpair-commission-to/01-auth.md"] = commission_authorization(
        "cm83", surface=commission_surface("cm83", overrides={"COMMISSION_TO": "beta.pair-implementer"}),
    )
    members["CM84-both-markers-delegation/01-relay.md"] = commission_authorization(
        "cm84", extra_fields=(("DELEGATED_DISPATCH_AUTHORITY", "yes"),),
    )

    members["CM85-id-reference-inert/01-status.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm85-status",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_ID", "cm85"),),
    )
    members["CM86-partial-correct-seat/01-receipt.md"] = commission_receipt(
        "cm86", surface=commission_surface("cm86", omit=("COMMISSION_SCOPE",)),
    )
    members["CM87-empty-correct-seat/01-receipt.md"] = commission_receipt(
        "cm87", surface=commission_surface("cm87", overrides={"COMMISSION_SCOPE": ""}),
    )
    members["CM88-wrong-seat-receipt/01-receipt.md"] = commission_receipt("cm88", from_addr="gamma.pair-planner")

    members["CM89-charter-wrong-authority/01-auth.md"] = commission_authorization("cm89")
    members["CM89-charter-wrong-authority/02-charter.md"] = commission_charter("cm89", authority="plan-only")
    members["CM90-charter-missing-kind/01-auth.md"] = commission_authorization("cm90")
    members["CM90-charter-missing-kind/02-charter.md"] = commission_charter("cm90", kind=None)

    members["CM91-auth-shadow-no/01-auth-valid.md"] = commission_authorization("cm91", dispatch_id="cm91-auth-valid")
    members["CM91-auth-shadow-no/02-auth-no.md"] = commission_authorization("cm91", value="no", dispatch_id="cm91-auth-no")
    members["CM91-auth-shadow-no/03-charter.md"] = commission_charter("cm91")
    members["CM92-unmarked-same-id-control/01-auth.md"] = commission_authorization("cm92")
    members["CM92-unmarked-same-id-control/02-status.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm92-status",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_ID", "cm92"),),
    )
    members["CM92-unmarked-same-id-control/03-charter.md"] = commission_charter("cm92")
    for number, order in ((93, "match-first"), (94, "match-last")):
        case = f"cm{number}"
        ids = (("COMMISSION_ID", case), ("COMMISSION_ID", "other"))
        if order == "match-last":
            ids = tuple(reversed(ids))
        surface = commission_surface(case, omit=("COMMISSION_ID",), extra=ids)
        members[f"CM{number}-auth-membership-conflict-{order}/01-auth.md"] = commission_authorization(case, surface=surface)
        members[f"CM{number}-auth-membership-conflict-{order}/02-charter.md"] = commission_charter(case)
    members["CM95-auth-same-position/a/01-auth.md"] = commission_authorization("cm95", dispatch_id="cm95-auth-a")
    members["CM95-auth-same-position/b/01-auth.md"] = commission_authorization("cm95", dispatch_id="cm95-auth-b")
    members["CM95-auth-same-position/02-charter.md"] = commission_charter("cm95")

    for number, field in ((96, "COMMISSION_SCOPE"), (97, "COMMISSION_TO"), (98, "CHARTER_DOC_ID")):
        case = f"cm{number}"
        override = {
            "COMMISSION_SCOPE": "scope-other",
            "COMMISSION_TO": "gamma.pair-planner",
            "CHARTER_DOC_ID": f"CH-{case}-other",
        }[field]
        members[f"CM{number}-charter-equality-{field.lower().replace('_', '-')}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-charter-equality-{field.lower().replace('_', '-')}/02-charter.md"] = commission_charter(
            case, design_doc_id=(override if field == "CHARTER_DOC_ID" else None),
            surface=commission_surface(case, overrides={field: override}),
        )

    equality_fields = ("COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID")
    for number, field in enumerate(equality_fields, start=99):
        case = f"cm{number}"
        override = "other" if field == "COMMISSION_ID" else (
            "scope-other" if field == "COMMISSION_SCOPE" else (
                "gamma.pair-planner" if field == "COMMISSION_TO" else f"CH-{case}-other"
            )
        )
        members[f"CM{number}-approval-equality-{field.lower().replace('_', '-')}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-approval-equality-{field.lower().replace('_', '-')}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-approval-equality-{field.lower().replace('_', '-')}/03-approval.md"] = commission_approval(
            case, surface=commission_surface(case, overrides={field: override}),
        )
    for number, field in enumerate(equality_fields, start=103):
        case = f"cm{number}"
        override = "other" if field == "COMMISSION_ID" else (
            "scope-other" if field == "COMMISSION_SCOPE" else (
                "gamma.pair-planner" if field == "COMMISSION_TO" else f"CH-{case}-other"
            )
        )
        members[f"CM{number}-grant-equality-{field.lower().replace('_', '-')}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-grant-equality-{field.lower().replace('_', '-')}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-grant-equality-{field.lower().replace('_', '-')}/03-approval.md"] = commission_approval(case)
        members[f"CM{number}-grant-equality-{field.lower().replace('_', '-')}/04-grant.md"] = commission_grant(
            case, to_addr=(override if field == "COMMISSION_TO" else "beta.pair-planner"),
            surface=commission_surface(case, overrides={field: override}),
        )

    add_commission_chain(
        members, "CM107-grant-nonpair-target", "cm107",
        grant=commission_grant("cm107", to_addr="beta.pair-implementer", surface=commission_surface("cm107", overrides={"COMMISSION_TO": "beta.pair-implementer"})),
    )
    members.pop("CM107-grant-nonpair-target/05-receipt.md")
    add_commission_chain(members, "CM108-grant-to-mismatch", "cm108")
    members.pop("CM108-grant-to-mismatch/05-receipt.md")
    members["CM108-grant-to-mismatch/04-grant.md"] = commission_grant("cm108", to_addr="gamma.pair-planner")
    add_commission_chain(members, "CM109-malformed-grant-shadow", "cm109")
    members["CM109-malformed-grant-shadow/05-grant-no.md"] = commission_grant(
        "cm109", value="no", dispatch_id="cm109-grant-no",
    )
    members.pop("CM109-malformed-grant-shadow/05-receipt.md")
    members["CM109-malformed-grant-shadow/06-receipt.md"] = commission_receipt("cm109")
    add_commission_chain(members, "CM110-unmarked-grant-control", "cm110")
    members["CM110-unmarked-grant-control/05-status.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm110-status",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_ID", "cm110"),),
    )
    members["CM110-unmarked-grant-control/06-receipt.md"] = commission_receipt("cm110")
    for number, order in ((111, "match-first"), (112, "match-last")):
        case = f"cm{number}"
        add_commission_chain(members, f"CM{number}-grant-membership-conflict-{order}", case)
        ids = (("COMMISSION_ID", case), ("COMMISSION_ID", "other"))
        if order == "match-last":
            ids = tuple(reversed(ids))
        members[f"CM{number}-grant-membership-conflict-{order}/05-grant-conflict.md"] = commission_grant(
            case, dispatch_id=f"{case}-grant-conflict",
            surface=commission_surface(case, omit=("COMMISSION_ID",), extra=ids),
        )
        members.pop(f"CM{number}-grant-membership-conflict-{order}/05-receipt.md")
        members[f"CM{number}-grant-membership-conflict-{order}/06-receipt.md"] = commission_receipt(case)
    add_commission_chain(members, "CM113-grant-same-position", "cm113")
    members["CM113-grant-same-position/a/05-grant.md"] = commission_grant("cm113", dispatch_id="cm113-grant-a")
    members["CM113-grant-same-position/b/05-grant.md"] = commission_grant("cm113", dispatch_id="cm113-grant-b")
    members.pop("CM113-grant-same-position/05-receipt.md")
    members["CM113-grant-same-position/06-receipt.md"] = commission_receipt("cm113")
    members["CM114-half-carrier/01-relay.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm114-relay",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_SCOPE", "scope-cm114"),),
    )

    members["CM115-auth-reissue-pass/01-auth-v1.md"] = commission_authorization("cm115", dispatch_id="cm115-auth-v1")
    members["CM115-auth-reissue-pass/02-auth-v2.md"] = commission_authorization("cm115", dispatch_id="cm115-auth-v2")
    members["CM115-auth-reissue-pass/03-charter.md"] = commission_charter("cm115", parent="cm115-auth-v2")
    members["CM116-later-auth-inert/01-auth.md"] = commission_authorization("cm116")
    members["CM116-later-auth-inert/02-charter.md"] = commission_charter("cm116")
    members["CM116-later-auth-inert/03-auth-later.md"] = commission_authorization("cm116", dispatch_id="cm116-auth-later")
    members["CM117-unmarked-review-excluded/01-auth.md"] = commission_authorization("cm117")
    members["CM117-unmarked-review-excluded/02-review.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm117-review",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm117"),),
    )
    members["CM117-unmarked-review-excluded/03-charter.md"] = commission_charter("cm117")

    add_commission_chain(members, "CM118-grant-reissue-pass", "cm118")
    members["CM118-grant-reissue-pass/05-grant-v2.md"] = commission_grant("cm118", dispatch_id="cm118-grant-v2")
    members.pop("CM118-grant-reissue-pass/05-receipt.md")
    members["CM118-grant-reissue-pass/06-receipt.md"] = commission_receipt("cm118")
    add_commission_chain(members, "CM119-later-grant-inert", "cm119")
    members["CM119-later-grant-inert/06-grant-later.md"] = commission_grant("cm119", dispatch_id="cm119-grant-later")
    add_commission_chain(members, "CM120-unmarked-review-grant-excluded", "cm120")
    members["CM120-unmarked-review-grant-excluded/05-review-status.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm120-status",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm120"),),
    )
    members["CM120-unmarked-review-grant-excluded/06-receipt.md"] = commission_receipt("cm120")
    add_commission_chain(members, "CM121-prior-receipt-excluded", "cm121")
    members["CM121-prior-receipt-excluded/06-receipt.md"] = commission_receipt("cm121")
    members["CM121-prior-receipt-excluded/07-receipt.md"] = lifecycle_relay(
        role="Pair Planner", phase="PLAN", authority="plan-only", dispatch_id="cm121-receipt-2",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer", fields=commission_surface("cm121"),
    )
    for number, value in ((122, "YES"), (123, "true")):
        case = f"cm{number}"
        add_commission_chain(members, f"CM{number}-grant-marker-{value.lower()}", case)
        members[f"CM{number}-grant-marker-{value.lower()}/05-grant-malformed.md"] = commission_grant(
            case, value=value, dispatch_id=f"{case}-grant-malformed",
        )
        members.pop(f"CM{number}-grant-marker-{value.lower()}/05-receipt.md")
        members[f"CM{number}-grant-marker-{value.lower()}/06-receipt.md"] = commission_receipt(case)

    members["CM124-charter-owner-mismatch/01-auth.md"] = commission_authorization(
        "cm124", to_addr="other.master-planner",
    )
    members["CM124-charter-owner-mismatch/02-charter.md"] = commission_charter("cm124")
    members["CM125-charter-owner-mixedcase-pass/01-auth.md"] = commission_authorization(
        "cm125", to_addr="Alpha.Master-Planner",
    )
    members["CM125-charter-owner-mixedcase-pass/02-charter.md"] = commission_charter("cm125")
    members["CM126-grant-owner-mismatch/01-auth.md"] = commission_authorization("cm126")
    members["CM126-grant-owner-mismatch/02-charter.md"] = commission_charter("cm126")
    members["CM126-grant-owner-mismatch/03-approval.md"] = commission_approval("cm126")
    members["CM126-grant-owner-mismatch/04-grant.md"] = commission_grant(
        "cm126", from_addr="other.master-planner",
    )
    members["CM127-grant-owner-mixedcase-pass/01-auth.md"] = commission_authorization("cm127")
    members["CM127-grant-owner-mixedcase-pass/02-charter.md"] = commission_charter("cm127")
    members["CM127-grant-owner-mixedcase-pass/03-approval.md"] = commission_approval("cm127")
    members["CM127-grant-owner-mixedcase-pass/04-grant.md"] = commission_grant(
        "cm127", from_addr="Alpha.Master-Planner",
    )

    for number, kind, verdict in (
        (128, None, "approve"),
        (129, "audit-record", "approve"),
        (130, "design-doc", None),
        (131, "design-doc", "must-revise"),
        (132, "design-doc", "banana"),
    ):
        case = f"cm{number}"
        members[f"CM{number}-approval-local-shape/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-approval-local-shape/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-approval-local-shape/03-approval.md"] = commission_approval(
            case, kind=kind, verdict=verdict,
        )

    for number, field in enumerate(COMMISSION_SURFACE_FIELDS, start=133):
        case = f"cm{number}"
        members[f"CM{number}-wrong-seat-missing-{field.lower().replace('_', '-')}/01-receipt.md"] = commission_receipt(
            case, from_addr="gamma.pair-planner", surface=commission_surface(case, omit=(field,)),
        )
    for number, field in enumerate(COMMISSION_SURFACE_FIELDS, start=137):
        case = f"cm{number}"
        members[f"CM{number}-wrong-seat-empty-{field.lower().replace('_', '-')}/01-receipt.md"] = commission_receipt(
            case, from_addr="gamma.pair-planner", surface=commission_surface(case, overrides={field: ""}),
        )

    target_values = (
        ("special", "operator"),
        ("master", "beta.master-planner"),
        ("reviewer", "beta.reviewer"),
        ("multiple", "beta.pair-planner, gamma.pair-planner"),
    )
    for number, (label, target) in enumerate(target_values, start=141):
        case = f"cm{number}"
        members[f"CM{number}-auth-target-{label}/01-auth.md"] = commission_authorization(
            case, surface=commission_surface(case, overrides={"COMMISSION_TO": target}),
        )
    members["CM145-auth-target-legacy-pass/01-auth.md"] = commission_authorization(
        "cm145", surface=commission_surface("cm145", overrides={"COMMISSION_TO": "beta.planner"}),
    )
    members["CM146-auth-target-explicit-pass/01-auth.md"] = commission_authorization("cm146")

    for number, (label, target) in enumerate(target_values, start=147):
        case = f"cm{number}"
        members[f"CM{number}-grant-target-{label}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-grant-target-{label}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-grant-target-{label}/03-approval.md"] = commission_approval(case)
        members[f"CM{number}-grant-target-{label}/04-grant.md"] = commission_grant(
            case, to_addr=target, surface=commission_surface(case, overrides={"COMMISSION_TO": target}),
        )
    for number, target in ((151, "beta.planner"), (152, "beta.pair-planner")):
        case = f"cm{number}"
        surface = commission_surface(case, overrides={"COMMISSION_TO": target})
        members[f"CM{number}-grant-target-pass/01-auth.md"] = commission_authorization(case, surface=surface)
        members[f"CM{number}-grant-target-pass/02-charter.md"] = commission_charter(case, surface=surface)
        members[f"CM{number}-grant-target-pass/03-approval.md"] = commission_approval(case, surface=surface)
        members[f"CM{number}-grant-target-pass/04-grant.md"] = commission_grant(case, to_addr=target, surface=surface)

    members["CM153-byte-exact-address-surface/01-auth.md"] = commission_authorization("cm153")
    members["CM153-byte-exact-address-surface/02-charter.md"] = commission_charter(
        "cm153", surface=commission_surface("cm153", overrides={"COMMISSION_TO": "Beta.Pair-Planner"}),
    )
    for number, field in enumerate(
        ("COMMISSION_AUTHORIZATION", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID"), start=154,
    ):
        case = f"cm{number}"
        fields = ((field, ""),)
        members[f"CM{number}-empty-carrier-engages/01-relay.md"] = lifecycle_relay(
            role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id=f"{case}-relay",
            from_addr="beta.pair-planner", to_addr="beta.pair-implementer", fields=fields,
        )
    members["CM158-charter-equality-commission-id/01-auth.md"] = commission_authorization("cm158")
    members["CM158-charter-equality-commission-id/02-charter.md"] = commission_charter(
        "cm158", surface=commission_surface("cm158", overrides={"COMMISSION_ID": "other"}),
    )
    members["CM159-empty-commission-id-engages/01-relay.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm159-relay",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_ID", ""),),
    )
    reserved_targets = (
        ("operator-legacy", "operator.planner"),
        ("operator-explicit", "operator.pair-planner"),
        ("orchestrator-legacy", "orchestrator.planner"),
        ("orchestrator-explicit", "orchestrator.pair-planner"),
    )
    for number, (label, target) in enumerate(reserved_targets, start=160):
        case = f"cm{number}"
        members[f"CM{number}-auth-target-{label}/01-auth.md"] = commission_authorization(
            case, surface=commission_surface(case, overrides={"COMMISSION_TO": target}),
        )
    for number, (label, target) in enumerate(reserved_targets, start=164):
        case = f"cm{number}"
        members[f"CM{number}-grant-target-{label}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-grant-target-{label}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-grant-target-{label}/03-approval.md"] = commission_approval(case)
        members[f"CM{number}-grant-target-{label}/04-grant.md"] = commission_grant(
            case, to_addr=target, surface=commission_surface(case, overrides={"COMMISSION_TO": target}),
        )

    # Task 9.5b: exact commission ancestry and authorization lifetime.
    members["CM168-s1a-charter-parent-absent/01-auth.md"] = commission_authorization("cm168")
    members["CM168-s1a-charter-parent-absent/02-charter.md"] = commission_charter(
        "cm168", include_parent=False,
    )

    members["CM169-s1b-charter-parent-wrong/01-auth-v1.md"] = commission_authorization(
        "cm169", dispatch_id="cm169-auth-v1",
    )
    members["CM169-s1b-charter-parent-wrong/02-auth-v2.md"] = commission_authorization(
        "cm169", dispatch_id="cm169-auth-v2",
    )
    members["CM169-s1b-charter-parent-wrong/03-charter.md"] = commission_charter(
        "cm169", parent="cm169-auth-v1",
    )

    members["CM170-s1c-charter-parent-ambiguous/a/01-auth.md"] = commission_authorization(
        "cm170", dispatch_id="cm170-auth",
    )
    members["CM170-s1c-charter-parent-ambiguous/b/01-decoy.md"] = lifecycle_relay(
        role="Operator", phase="SITREP", authority="report-only", dispatch_id="cm170-auth",
        from_addr="operator", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm170"),),
    )
    members["CM170-s1c-charter-parent-ambiguous/02-charter.md"] = commission_charter("cm170")

    members["CM171-s1d-charter-parent-nonstage/01-auth.md"] = commission_authorization("cm171")
    members["CM171-s1d-charter-parent-nonstage/02-decoy.md"] = lifecycle_relay(
        role="Operator", phase="SITREP", authority="report-only", dispatch_id="cm171-auth",
        from_addr="operator", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm171"),),
    )
    members["CM171-s1d-charter-parent-nonstage/03-charter.md"] = commission_charter("cm171")

    for number, timing in ((172, "approval"), (173, "grant"), (174, "receipt")):
        case = f"cm{number}"
        members[f"CM{number}-auth-reselection-{timing}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-auth-reselection-{timing}/02-charter.md"] = commission_charter(case)
        if timing != "approval":
            members[f"CM{number}-auth-reselection-{timing}/03-approval.md"] = commission_approval(case)
        revision_position = "03" if timing == "approval" else ("04" if timing == "grant" else "05")
        members[f"CM{number}-auth-reselection-{timing}/{revision_position}-auth-no.md"] = commission_authorization(
            case, value="no", dispatch_id=f"{case}-auth-no",
        )
        if timing == "approval":
            members[f"CM{number}-auth-reselection-{timing}/04-approval.md"] = commission_approval(case)
        elif timing == "grant":
            members[f"CM{number}-auth-reselection-{timing}/05-grant.md"] = commission_grant(case)
        else:
            members[f"CM{number}-auth-reselection-{timing}/04-grant.md"] = commission_grant(case)
            members[f"CM{number}-auth-reselection-{timing}/06-receipt.md"] = commission_receipt(case)

    members["CM175-approval-parent-absent/01-auth.md"] = commission_authorization("cm175")
    members["CM175-approval-parent-absent/02-charter.md"] = commission_charter("cm175")
    members["CM175-approval-parent-absent/03-approval.md"] = commission_approval("cm175", include_parent=False)

    members["CM176-approval-parent-wrong/01-auth.md"] = commission_authorization("cm176")
    members["CM176-approval-parent-wrong/02-charter.md"] = commission_charter("cm176")
    members["CM176-approval-parent-wrong/03-status.md"] = lifecycle_relay(
        role="Master Planner", phase="SITREP", authority="report-only", dispatch_id="cm176-status",
        from_addr="alpha.master-planner", to_addr="alpha.master-reviewer",
        fields=(("COMMISSION_ID", "cm176"),),
    )
    members["CM176-approval-parent-wrong/04-approval.md"] = commission_approval("cm176", parent="cm176-status")

    members["CM177-approval-parent-ambiguous/01-auth.md"] = commission_authorization("cm177")
    members["CM177-approval-parent-ambiguous/a/02-charter.md"] = commission_charter("cm177")
    members["CM177-approval-parent-ambiguous/b/02-decoy.md"] = lifecycle_relay(
        role="Master Planner", phase="SITREP", authority="report-only", dispatch_id="cm177-charter",
        from_addr="alpha.master-planner", to_addr="alpha.master-reviewer",
        fields=(("COMMISSION_ID", "cm177"),),
    )
    members["CM177-approval-parent-ambiguous/03-approval.md"] = commission_approval("cm177")

    members["CM178-approval-parent-nonstage/01-auth.md"] = commission_authorization("cm178")
    members["CM178-approval-parent-nonstage/02-charter.md"] = commission_charter("cm178")
    members["CM178-approval-parent-nonstage/03-decoy.md"] = lifecycle_relay(
        role="Master Planner", phase="SITREP", authority="report-only", dispatch_id="cm178-charter",
        from_addr="alpha.master-planner", to_addr="alpha.master-reviewer",
        fields=(("COMMISSION_ID", "cm178"),),
    )
    members["CM178-approval-parent-nonstage/04-approval.md"] = commission_approval("cm178")

    members["CM179-charter-reissue-pass/01-auth.md"] = commission_authorization("cm179")
    members["CM179-charter-reissue-pass/02-charter-v1.md"] = commission_charter(
        "cm179", dispatch_id="cm179-charter-v1",
    )
    members["CM179-charter-reissue-pass/03-charter-v2.md"] = commission_charter(
        "cm179", dispatch_id="cm179-charter-v2",
    )
    members["CM179-charter-reissue-pass/04-approval.md"] = commission_approval(
        "cm179", parent="cm179-charter-v2",
    )

    members["CM180-charter-same-position/01-auth.md"] = commission_authorization("cm180")
    members["CM180-charter-same-position/a/02-charter.md"] = commission_charter(
        "cm180", dispatch_id="cm180-charter-a",
    )
    members["CM180-charter-same-position/b/02-charter.md"] = commission_charter(
        "cm180", dispatch_id="cm180-charter-b",
    )
    members["CM180-charter-same-position/03-approval.md"] = commission_approval(
        "cm180", parent="cm180-charter-a",
    )

    members["CM181-later-charter-inert/01-auth.md"] = commission_authorization("cm181")
    members["CM181-later-charter-inert/02-charter-v1.md"] = commission_charter(
        "cm181", dispatch_id="cm181-charter-v1",
    )
    members["CM181-later-charter-inert/03-approval.md"] = commission_approval(
        "cm181", parent="cm181-charter-v1",
    )
    members["CM181-later-charter-inert/04-charter-v2.md"] = commission_charter(
        "cm181", dispatch_id="cm181-charter-v2",
    )

    members["CM182-wrong-owner-reviewer/01-auth.md"] = commission_authorization("cm182")
    members["CM182-wrong-owner-reviewer/02-charter.md"] = commission_charter("cm182")
    members["CM182-wrong-owner-reviewer/03-approval.md"] = commission_approval(
        "cm182", from_addr="gamma.master-reviewer",
    )

    members["CM183-later-must-revise-shadow/01-auth.md"] = commission_authorization("cm183")
    members["CM183-later-must-revise-shadow/02-charter.md"] = commission_charter("cm183")
    members["CM183-later-must-revise-shadow/03-approval.md"] = commission_approval(
        "cm183", dispatch_id="cm183-approval-ok",
    )
    members["CM183-later-must-revise-shadow/04-review.md"] = commission_approval(
        "cm183", verdict="must-revise", dispatch_id="cm183-approval-no",
    )
    members["CM183-later-must-revise-shadow/05-grant.md"] = commission_grant(
        "cm183", parent="cm183-approval-ok",
    )

    members["CM184-grant-parent-absent/01-auth.md"] = commission_authorization("cm184")
    members["CM184-grant-parent-absent/02-charter.md"] = commission_charter("cm184")
    members["CM184-grant-parent-absent/03-approval.md"] = commission_approval("cm184")
    members["CM184-grant-parent-absent/04-grant.md"] = commission_grant("cm184", include_parent=False)

    members["CM185-grant-parent-wrong/01-auth.md"] = commission_authorization("cm185")
    members["CM185-grant-parent-wrong/02-charter.md"] = commission_charter("cm185")
    members["CM185-grant-parent-wrong/03-approval.md"] = commission_approval("cm185")
    members["CM185-grant-parent-wrong/04-status.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm185-status",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm185"),),
    )
    members["CM185-grant-parent-wrong/05-grant.md"] = commission_grant("cm185", parent="cm185-status")

    members["CM186-grant-parent-ambiguous/01-auth.md"] = commission_authorization("cm186")
    members["CM186-grant-parent-ambiguous/02-charter.md"] = commission_charter("cm186")
    members["CM186-grant-parent-ambiguous/a/03-approval.md"] = commission_approval("cm186")
    members["CM186-grant-parent-ambiguous/b/03-decoy.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm186-approval",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm186"),),
    )
    members["CM186-grant-parent-ambiguous/04-grant.md"] = commission_grant("cm186")

    members["CM187-grant-parent-nonstage/01-auth.md"] = commission_authorization("cm187")
    members["CM187-grant-parent-nonstage/02-charter.md"] = commission_charter("cm187")
    members["CM187-grant-parent-nonstage/03-approval.md"] = commission_approval("cm187")
    members["CM187-grant-parent-nonstage/04-decoy.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm187-approval",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm187"),),
    )
    members["CM187-grant-parent-nonstage/05-grant.md"] = commission_grant("cm187")

    members["CM188-later-approval-inert/01-auth.md"] = commission_authorization("cm188")
    members["CM188-later-approval-inert/02-charter.md"] = commission_charter("cm188")
    members["CM188-later-approval-inert/03-grant.md"] = commission_grant("cm188")
    members["CM188-later-approval-inert/04-approval.md"] = commission_approval("cm188")

    members["CM189-receipt-missing-grant/01-auth.md"] = commission_authorization("cm189")
    members["CM189-receipt-missing-grant/02-charter.md"] = commission_charter("cm189")
    members["CM189-receipt-missing-grant/03-approval.md"] = commission_approval("cm189")
    members["CM189-receipt-missing-grant/04-receipt.md"] = commission_receipt("cm189")

    members["CM190-unmarked-universe-controls/01-auth.md"] = commission_authorization("cm190")
    members["CM190-unmarked-universe-controls/02-status.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm190-status",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_ID", "cm190"),),
    )
    members["CM190-unmarked-universe-controls/03-charter.md"] = commission_charter("cm190")
    members["CM190-unmarked-universe-controls/04-approval.md"] = commission_approval("cm190")
    members["CM190-unmarked-universe-controls/05-grant.md"] = commission_grant("cm190")
    members["CM190-unmarked-universe-controls/06-status.md"] = lifecycle_relay(
        role="Pair Planner", phase="SITREP", authority="report-only", dispatch_id="cm190-status-two",
        from_addr="beta.pair-planner", to_addr="beta.pair-implementer",
        fields=(("COMMISSION_ID", "cm190"),),
    )
    members["CM190-unmarked-universe-controls/07-receipt.md"] = commission_receipt("cm190")

    for number, order in ((191, "parent-first"), (192, "other-first")):
        case = f"cm{number}"
        parents = (("PARENT_DISPATCH_ID", f"{case}-auth"), ("PARENT_DISPATCH_ID", "other"))
        if order == "other-first":
            parents = tuple(reversed(parents))
        members[f"CM{number}-charter-parent-conflict-{order}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-charter-parent-conflict-{order}/02-charter.md"] = commission_charter(
            case, include_parent=False, extra_fields=parents,
        )

    for number, order in ((193, "parent-first"), (194, "other-first")):
        case = f"cm{number}"
        parents = (("PARENT_DISPATCH_ID", f"{case}-charter"), ("PARENT_DISPATCH_ID", "other"))
        if order == "other-first":
            parents = tuple(reversed(parents))
        members[f"CM{number}-approval-parent-conflict-{order}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-approval-parent-conflict-{order}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-approval-parent-conflict-{order}/03-approval.md"] = commission_approval(
            case, include_parent=False, extra_fields=parents,
        )

    for number, order in ((195, "parent-first"), (196, "other-first")):
        case = f"cm{number}"
        parents = (("PARENT_DISPATCH_ID", f"{case}-approval"), ("PARENT_DISPATCH_ID", "other"))
        if order == "other-first":
            parents = tuple(reversed(parents))
        members[f"CM{number}-grant-parent-conflict-{order}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-grant-parent-conflict-{order}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-grant-parent-conflict-{order}/03-approval.md"] = commission_approval(case)
        members[f"CM{number}-grant-parent-conflict-{order}/04-grant.md"] = commission_grant(
            case, include_parent=False, extra_fields=parents,
        )

    for number, order in ((197, "id-first"), (198, "other-first")):
        case = f"cm{number}"
        ids = (("DISPATCH_ID", f"{case}-approval"), ("DISPATCH_ID", "other"))
        if order == "other-first":
            ids = tuple(reversed(ids))
        members[f"CM{number}-approval-id-conflict-{order}/01-auth.md"] = commission_authorization(case)
        members[f"CM{number}-approval-id-conflict-{order}/02-charter.md"] = commission_charter(case)
        members[f"CM{number}-approval-id-conflict-{order}/03-approval.md"] = commission_approval(
            case, dispatch_id="placeholder", extra_fields=ids,
        ).replace("DISPATCH_ID: placeholder\n", "", 1)
        members[f"CM{number}-approval-id-conflict-{order}/04-grant.md"] = commission_grant(case)

    members["CM199-grant-wrong-owner-reviewer/01-auth.md"] = commission_authorization("cm199")
    members["CM199-grant-wrong-owner-reviewer/02-charter.md"] = commission_charter("cm199")
    members["CM199-grant-wrong-owner-reviewer/03-approval.md"] = commission_approval(
        "cm199", from_addr="gamma.master-reviewer",
    )
    members["CM199-grant-wrong-owner-reviewer/04-grant.md"] = commission_grant("cm199")

    members["CM200-receipt-wrong-owner-reviewer/01-auth.md"] = commission_authorization("cm200")
    members["CM200-receipt-wrong-owner-reviewer/02-charter.md"] = commission_charter("cm200")
    members["CM200-receipt-wrong-owner-reviewer/03-approval.md"] = commission_approval(
        "cm200", from_addr="gamma.master-reviewer",
    )
    members["CM200-receipt-wrong-owner-reviewer/04-grant.md"] = commission_grant("cm200")
    members["CM200-receipt-wrong-owner-reviewer/05-receipt.md"] = commission_receipt("cm200")

    members["CM201-receipt-grant-parent-absent/01-auth.md"] = commission_authorization("cm201")
    members["CM201-receipt-grant-parent-absent/02-charter.md"] = commission_charter("cm201")
    members["CM201-receipt-grant-parent-absent/03-approval.md"] = commission_approval("cm201")
    members["CM201-receipt-grant-parent-absent/04-grant.md"] = commission_grant("cm201", include_parent=False)
    members["CM201-receipt-grant-parent-absent/05-receipt.md"] = commission_receipt("cm201")

    members["CM202-receipt-grant-parent-wrong/01-auth.md"] = commission_authorization("cm202")
    members["CM202-receipt-grant-parent-wrong/02-charter.md"] = commission_charter("cm202")
    members["CM202-receipt-grant-parent-wrong/03-approval.md"] = commission_approval("cm202")
    members["CM202-receipt-grant-parent-wrong/04-grant.md"] = commission_grant("cm202", parent="cm202-auth")
    members["CM202-receipt-grant-parent-wrong/05-receipt.md"] = commission_receipt("cm202")

    members["CM203-receipt-grant-parent-ambiguous/01-auth.md"] = commission_authorization("cm203")
    members["CM203-receipt-grant-parent-ambiguous/02-charter.md"] = commission_charter("cm203")
    members["CM203-receipt-grant-parent-ambiguous/a/03-approval.md"] = commission_approval("cm203")
    members["CM203-receipt-grant-parent-ambiguous/b/03-decoy.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm203-approval",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm203"),),
    )
    members["CM203-receipt-grant-parent-ambiguous/04-grant.md"] = commission_grant("cm203")
    members["CM203-receipt-grant-parent-ambiguous/05-receipt.md"] = commission_receipt("cm203")

    members["CM204-receipt-grant-parent-nonstage/01-auth.md"] = commission_authorization("cm204")
    members["CM204-receipt-grant-parent-nonstage/02-charter.md"] = commission_charter("cm204")
    members["CM204-receipt-grant-parent-nonstage/03-approval.md"] = commission_approval("cm204")
    members["CM204-receipt-grant-parent-nonstage/04-decoy.md"] = lifecycle_relay(
        role="Master Reviewer", phase="SITREP", authority="report-only", dispatch_id="cm204-approval",
        from_addr="alpha.master-reviewer", to_addr="alpha.master-planner",
        fields=(("COMMISSION_ID", "cm204"),),
    )
    members["CM204-receipt-grant-parent-nonstage/05-grant.md"] = commission_grant("cm204")
    members["CM204-receipt-grant-parent-nonstage/06-receipt.md"] = commission_receipt("cm204")

    members["CM205-receipt-approval-parent-absent/01-auth.md"] = commission_authorization("cm205")
    members["CM205-receipt-approval-parent-absent/02-charter.md"] = commission_charter("cm205")
    members["CM205-receipt-approval-parent-absent/03-approval.md"] = commission_approval("cm205", include_parent=False)
    members["CM205-receipt-approval-parent-absent/04-grant.md"] = commission_grant("cm205")
    members["CM205-receipt-approval-parent-absent/05-receipt.md"] = commission_receipt("cm205")

    members["CM206-receipt-approval-parent-wrong/01-auth.md"] = commission_authorization("cm206")
    members["CM206-receipt-approval-parent-wrong/02-charter.md"] = commission_charter("cm206")
    members["CM206-receipt-approval-parent-wrong/03-approval.md"] = commission_approval("cm206", parent="cm206-auth")
    members["CM206-receipt-approval-parent-wrong/04-grant.md"] = commission_grant("cm206")
    members["CM206-receipt-approval-parent-wrong/05-receipt.md"] = commission_receipt("cm206")

    members["CM207-receipt-approval-parent-ambiguous/01-auth.md"] = commission_authorization("cm207")
    members["CM207-receipt-approval-parent-ambiguous/a/02-charter.md"] = commission_charter("cm207")
    members["CM207-receipt-approval-parent-ambiguous/b/02-decoy.md"] = lifecycle_relay(
        role="Master Planner", phase="SITREP", authority="report-only", dispatch_id="cm207-charter",
        from_addr="alpha.master-planner", to_addr="alpha.master-reviewer",
        fields=(("COMMISSION_ID", "cm207"),),
    )
    members["CM207-receipt-approval-parent-ambiguous/03-approval.md"] = commission_approval("cm207")
    members["CM207-receipt-approval-parent-ambiguous/04-grant.md"] = commission_grant("cm207")
    members["CM207-receipt-approval-parent-ambiguous/05-receipt.md"] = commission_receipt("cm207")

    members["CM208-receipt-approval-parent-nonstage/01-auth.md"] = commission_authorization("cm208")
    members["CM208-receipt-approval-parent-nonstage/02-charter.md"] = commission_charter("cm208")
    members["CM208-receipt-approval-parent-nonstage/03-decoy.md"] = lifecycle_relay(
        role="Master Planner", phase="SITREP", authority="report-only", dispatch_id="cm208-charter",
        from_addr="alpha.master-planner", to_addr="alpha.master-reviewer",
        fields=(("COMMISSION_ID", "cm208"),),
    )
    members["CM208-receipt-approval-parent-nonstage/04-approval.md"] = commission_approval("cm208")
    members["CM208-receipt-approval-parent-nonstage/05-grant.md"] = commission_grant("cm208")
    members["CM208-receipt-approval-parent-nonstage/06-receipt.md"] = commission_receipt("cm208")

    members["CM209-receipt-later-grant-inert/01-auth.md"] = commission_authorization("cm209")
    members["CM209-receipt-later-grant-inert/02-charter.md"] = commission_charter("cm209")
    members["CM209-receipt-later-grant-inert/03-approval.md"] = commission_approval("cm209")
    members["CM209-receipt-later-grant-inert/04-receipt.md"] = commission_receipt("cm209")
    members["CM209-receipt-later-grant-inert/05-grant.md"] = commission_grant("cm209")

    members["CM210-review-reissue-pass/01-auth.md"] = commission_authorization("cm210")
    members["CM210-review-reissue-pass/02-charter.md"] = commission_charter("cm210")
    members["CM210-review-reissue-pass/03-review-v1.md"] = commission_approval(
        "cm210", dispatch_id="cm210-review-v1",
    )
    members["CM210-review-reissue-pass/04-review-v2.md"] = commission_approval(
        "cm210", dispatch_id="cm210-review-v2",
    )
    members["CM210-review-reissue-pass/05-grant.md"] = commission_grant("cm210", parent="cm210-review-v2")

    members["CM211-review-same-position/01-auth.md"] = commission_authorization("cm211")
    members["CM211-review-same-position/02-charter.md"] = commission_charter("cm211")
    members["CM211-review-same-position/a/03-review.md"] = commission_approval(
        "cm211", dispatch_id="cm211-review-a",
    )
    members["CM211-review-same-position/b/03-review.md"] = commission_approval(
        "cm211", dispatch_id="cm211-review-b",
    )
    members["CM211-review-same-position/04-grant.md"] = commission_grant("cm211", parent="cm211-review-a")

    members["CM212-auth-charter-same-position/a/01-auth.md"] = commission_authorization("cm212")
    members["CM212-auth-charter-same-position/b/01-charter.md"] = commission_charter("cm212")

    members["CM213-charter-approval-same-position/01-auth.md"] = commission_authorization("cm213")
    members["CM213-charter-approval-same-position/a/02-charter.md"] = commission_charter("cm213")
    members["CM213-charter-approval-same-position/b/02-approval.md"] = commission_approval("cm213")

    members["CM214-approval-grant-same-position/01-auth.md"] = commission_authorization("cm214")
    members["CM214-approval-grant-same-position/02-charter.md"] = commission_charter("cm214")
    members["CM214-approval-grant-same-position/a/03-approval.md"] = commission_approval("cm214")
    members["CM214-approval-grant-same-position/b/03-grant.md"] = commission_grant("cm214")

    members["CM215-grant-receipt-same-position/01-auth.md"] = commission_authorization("cm215")
    members["CM215-grant-receipt-same-position/02-charter.md"] = commission_charter("cm215")
    members["CM215-grant-receipt-same-position/03-approval.md"] = commission_approval("cm215")
    members["CM215-grant-receipt-same-position/a/04-grant.md"] = commission_grant("cm215")
    members["CM215-grant-receipt-same-position/b/04-receipt.md"] = commission_receipt("cm215")

    members["CM216-malformed-latest-charter/01-auth.md"] = commission_authorization("cm216")
    members["CM216-malformed-latest-charter/02-charter.md"] = commission_charter("cm216")
    members["CM216-malformed-latest-charter/03-charter-malformed.md"] = commission_charter(
        "cm216", phase="SITREP", authority="report-only", dispatch_id="cm216-charter-bad",
    )
    members["CM216-malformed-latest-charter/04-approval.md"] = commission_approval("cm216")
    members["CM216-malformed-latest-charter/05-grant.md"] = commission_grant("cm216")
    members["CM216-malformed-latest-charter/06-receipt.md"] = commission_receipt("cm216")

    members["CM217-wrong-parent-review-shadow/01-auth.md"] = commission_authorization("cm217")
    members["CM217-wrong-parent-review-shadow/02-charter.md"] = commission_charter("cm217")
    members["CM217-wrong-parent-review-shadow/03-approval.md"] = commission_approval(
        "cm217", dispatch_id="cm217-review-ok",
    )
    members["CM217-wrong-parent-review-shadow/04-review-wrong.md"] = commission_approval(
        "cm217", parent="cm217-auth", dispatch_id="cm217-review-wrong",
    )
    members["CM217-wrong-parent-review-shadow/05-grant.md"] = commission_grant(
        "cm217", parent="cm217-review-ok",
    )
    members["CM217-wrong-parent-review-shadow/06-receipt.md"] = commission_receipt("cm217")

    members["CM218-receipt-missing-charter/01-auth.md"] = commission_authorization("cm218")
    members["CM218-receipt-missing-charter/02-grant.md"] = commission_grant("cm218")
    members["CM218-receipt-missing-charter/03-receipt.md"] = commission_receipt("cm218")

    # Task 9.5c: direct grants with no commission carrier remain insulated;
    # each of the five carriers (the authorization carrier necessarily making
    # the both-markers shape) engages the full commission machine.
    members["CM219-direct-operator-control/01-grant.md"] = direct_delegation("cm219")
    members["CM220-direct-orchestrator-control/01-grant.md"] = direct_delegation(
        "cm220", from_addr="orchestrator", role="Orchestrator Planner",
        extra_fields=(("CC", "v29.orchestrator-reviewer"),),
    )
    for number, field, value in (
        (221, "COMMISSION_AUTHORIZATION", "yes"),
        (222, "COMMISSION_ID", "cm222"),
        (223, "COMMISSION_SCOPE", "scope-cm223"),
        (224, "COMMISSION_TO", "beta.pair-planner"),
        (225, "CHARTER_DOC_ID", "CH-cm225"),
    ):
        label = field.lower().replace("_", "-")
        members[f"CM{number}-direct-half-{label}/01-grant.md"] = direct_delegation(
            f"cm{number}", extra_fields=((field, value),),
        )

    # The complete chain is valid until the pair receipt attempts to carry its
    # own delegation grant. That self-grant must be refused independently.
    add_commission_chain(members, "CM226-pair-receipt-self-grant", "cm226")
    members["CM226-pair-receipt-self-grant/05-receipt.md"] = lifecycle_relay(
        role="Pair Planner", phase="PLAN", authority="plan-only",
        dispatch_id="cm226-receipt", from_addr="beta.pair-planner",
        to_addr="beta.pair-implementer",
        fields=(("DELEGATED_DISPATCH_AUTHORITY", "yes"),) + commission_surface("cm226"),
    )

    # Rule 3(e): all four receiving roles, then conflict-first occurrence
    # handling in both orders for both discriminators.
    for number, target in (
        (227, "alpha.master-planner"),
        (228, "alpha.master-reviewer"),
        (229, "alpha.domain-planner"),
        (230, "alpha.domain-reviewer"),
    ):
        role_label = target.split(".", 1)[1]
        members[f"CM{number}-receiving-{role_label}/01-grant.md"] = direct_delegation(
            f"cm{number}", to_addr=target,
        )
    for number, values, order_label in (
        (231, ("yes", "no"), "yes-first"),
        (232, ("no", "yes"), "yes-last"),
    ):
        members[f"CM{number}-receiving-dda-conflict-{order_label}/01-grant.md"] = lifecycle_relay(
            role="Operator", phase="PLAN", authority="plan-only",
            dispatch_id=f"cm{number}-direct", from_addr="operator",
            to_addr="alpha.master-planner",
            fields=tuple(("DELEGATED_DISPATCH_AUTHORITY", value) for value in values),
        )
    for number, values, order_label in (
        (233, ("alpha.master-planner", "beta.pair-planner"), "master-first"),
        (234, ("beta.pair-planner", "alpha.master-planner"), "master-last"),
    ):
        members[f"CM{number}-receiving-to-conflict-{order_label}/01-grant.md"] = lifecycle_relay(
            role="Operator", phase="PLAN", authority="plan-only",
            dispatch_id=f"cm{number}-direct", from_addr="operator",
            to_addr=values[0],
            fields=(("DELEGATED_DISPATCH_AUTHORITY", "yes"), ("TO", values[1])),
        )

    # S3's ten-cell triangle. Each ancestor class conflicts one discriminator
    # that every named downstream gate actually consumes.
    for number, gate in enumerate(("charter", "approval", "grant", "receipt"), start=235):
        case = f"cm{number}"
        name = f"CM{number}-conflict-authorization-at-{gate}"
        members[f"{name}/01-auth.md"] = commission_authorization(
            case, extra_fields=(("COMMISSION_AUTHORIZATION", "no"),),
        )
        members[f"{name}/02-charter.md"] = commission_charter(case)
        if gate in {"approval", "grant", "receipt"}:
            members[f"{name}/03-approval.md"] = commission_approval(case)
        if gate in {"grant", "receipt"}:
            members[f"{name}/04-grant.md"] = commission_grant(case)
        if gate == "receipt":
            members[f"{name}/05-receipt.md"] = commission_receipt(case)
    for number, gate in enumerate(("approval", "grant", "receipt"), start=239):
        case = f"cm{number}"
        name = f"CM{number}-conflict-charter-at-{gate}"
        members[f"{name}/01-auth.md"] = commission_authorization(case)
        members[f"{name}/02-charter.md"] = commission_charter(
            case, extra_fields=(("AUTHORITY", "plan-only"),),
        )
        members[f"{name}/03-approval.md"] = commission_approval(case)
        if gate in {"grant", "receipt"}:
            members[f"{name}/04-grant.md"] = commission_grant(case)
        if gate == "receipt":
            members[f"{name}/05-receipt.md"] = commission_receipt(case)
    for number, gate in enumerate(("grant", "receipt"), start=242):
        case = f"cm{number}"
        name = f"CM{number}-conflict-review-at-{gate}"
        members[f"{name}/01-auth.md"] = commission_authorization(case)
        members[f"{name}/02-charter.md"] = commission_charter(case)
        members[f"{name}/03-approval.md"] = commission_approval(
            case, extra_fields=(("DESIGN_REVIEW_VERDICT", "must-revise"),),
        )
        members[f"{name}/04-grant.md"] = commission_grant(case)
        if gate == "receipt":
            members[f"{name}/05-receipt.md"] = commission_receipt(case)
    members["CM244-conflict-grant-at-receipt/01-auth.md"] = commission_authorization("cm244")
    members["CM244-conflict-grant-at-receipt/02-charter.md"] = commission_charter("cm244")
    members["CM244-conflict-grant-at-receipt/03-approval.md"] = commission_approval("cm244")
    members["CM244-conflict-grant-at-receipt/04-grant.md"] = commission_grant(
        "cm244", extra_fields=(("TO", "gamma.pair-planner"),),
    )
    members["CM244-conflict-grant-at-receipt/05-receipt.md"] = commission_receipt("cm244")

    # Exact grammar boundaries: these are complete valid chains so a stricter
    # kebab interpretation cannot hide behind a malformed-stage refusal.
    grammar_positives = ("7", "7a", "a", "a--b", "a-", "a1", "71", "a-1")
    for number, commission_id in enumerate(grammar_positives, start=245):
        members[f"CM{number}-grammar-positive-{number - 244}/01-auth.md"] = (
            commission_authorization(commission_id, dispatch_id=f"cm{number}-auth")
        )
    for number, commission_id, label in (
        (253, "-a", "leading-hyphen"),
        (254, "A1", "leading-uppercase"),
        (255, "a-B", "tail-uppercase"),
        (256, "a_b", "underscore"),
        (257, "", "empty"),
    ):
        members[f"CM{number}-grammar-negative-{label}/01-auth.md"] = commission_authorization(
            commission_id, dispatch_id=f"cm{number}-auth",
            surface=commission_surface(commission_id),
        )

    members["CM258-identity-wrong-ch-form/01-auth.md"] = commission_authorization(
        "cm258", surface=commission_surface("cm258", overrides={"CHARTER_DOC_ID": "charter-cm258"}),
    )
    members["CM259-identity-charter-design-mismatch/01-auth.md"] = commission_authorization("cm259")
    members["CM259-identity-charter-design-mismatch/02-charter.md"] = commission_charter(
        "cm259", design_doc_id="CH-other",
    )
    members["CM260-identity-composition-spelling/01-auth.md"] = commission_authorization(
        "a--b", dispatch_id="cm260-auth",
        surface=commission_surface("a--b", overrides={"CHARTER_DOC_ID": "CH-a-b"}),
    )

    # Display fields are deliberately present on settled direct grants. Every
    # form includes both fields so consuming either one kills all four controls.
    for number, label, display_fields in (
        (261, "present", (("CHARTER_ARTIFACT", "charters/cm261.md"), ("CHARTER_SHA256", "abc123"))),
        (262, "malformed", (("CHARTER_ARTIFACT", ":::"), ("CHARTER_SHA256", "not-hex"))),
        (263, "repeated", (("CHARTER_ARTIFACT", "same"), ("CHARTER_ARTIFACT", "same"),
                            ("CHARTER_SHA256", "same"), ("CHARTER_SHA256", "same"))),
        (264, "conflicting", (("CHARTER_ARTIFACT", "one"), ("CHARTER_ARTIFACT", "two"),
                               ("CHARTER_SHA256", "aaa"), ("CHARTER_SHA256", "bbb"))),
    ):
        members[f"CM{number}-display-{label}/01-grant.md"] = direct_delegation(
            f"cm{number}", extra_fields=display_fields,
        )

    members["CM265-receiving-mixed-list/01-grant.md"] = direct_delegation(
        "cm265", to_addr="beta.pair-planner, alpha.master-planner",
    )
    members["CM266-receiving-identical-repeats/01-grant.md"] = lifecycle_relay(
        role="Operator", phase="PLAN", authority="plan-only",
        dispatch_id="cm266-direct", from_addr="operator",
        to_addr="alpha.master-planner",
        fields=(("DELEGATED_DISPATCH_AUTHORITY", "yes"),
                ("DELEGATED_DISPATCH_AUTHORITY", "yes"),
                ("TO", "alpha.master-planner")),
    )

    # Composite selected-ancestor conflicts prove that each consuming gate
    # reports every conflicted object it reads. The conflicted fields preserve
    # universe membership and ancestry selection.
    for number, gate in ((267, "approval"), (268, "grant"), (269, "receipt")):
        case = f"cm{number}"
        name = f"CM{number}-composite-conflicts-{gate}"
        members[f"{name}/01-auth.md"] = commission_authorization(
            case, extra_fields=(("COMMISSION_AUTHORIZATION", "no"),),
        )
        members[f"{name}/02-charter.md"] = commission_charter(
            case, extra_fields=(("AUTHORITY", "plan-only"),),
        )
        if gate == "approval":
            members[f"{name}/03-approval.md"] = commission_approval(case)
            continue
        members[f"{name}/03-approval.md"] = commission_approval(
            case, extra_fields=(("DESIGN_REVIEW_VERDICT", "must-revise"),),
        )
        members[f"{name}/04-grant.md"] = commission_grant(
            case,
            extra_fields=(("TO", "gamma.pair-planner"),) if gate == "receipt" else (),
        )
        if gate == "receipt":
            members[f"{name}/05-receipt.md"] = commission_receipt(case)

    return members


def lifecycle_members() -> dict[str, str]:
    members: dict[str, str] = {}
    add_chain(members, "MT34-foreign-design-kind-present", case="c4-design")
    add_chain(
        members, "MT35-foreign-design-kind-omitted", case="c4-omit",
        consumer=consumer_relay("c4-omit", kind=None),
    )
    add_chain(
        members, "MT36-foreign-audit-pair", case="c4-audit",
        origin=origin_relay("c4-audit", tier="domain", kind="audit-record"),
        reviews=(review_relay("c4-audit", tier="domain", kind="audit-record"),),
        consumer=consumer_relay("c4-audit", kind="audit-record"),
    )

    pair_case = "c4-pair"
    members["MT37-pair-route-control/01-origin.md"] = lifecycle_relay(
        role="Planner", phase="DESIGN", authority="design-only", dispatch_id=f"{pair_case}-origin",
        from_addr="beta.planner", to_addr="beta.implementer", fields=(("DESIGN_DOC_ID", f"lock-{pair_case}"),),
    )
    members["MT37-pair-route-control/02-review.md"] = lifecycle_relay(
        role="Implementer", phase="DESIGN-REVIEW", authority="review-only", dispatch_id=f"{pair_case}-review",
        from_addr="beta.implementer", to_addr="beta.planner",
        fields=(("PARENT_DISPATCH_ID", f"{pair_case}-origin"), ("DESIGN_DOC_ID", f"lock-{pair_case}"), ("DESIGN_REVIEW_VERDICT", "approve")),
    )
    members["MT37-pair-route-control/03-consumer.md"] = consumer_relay(
        pair_case, role="Planner", from_addr="beta.planner", to_addr="beta.implementer",
        extra_fields=(("PARENT_DISPATCH_ID", f"{pair_case}-review"),),
    )

    collision = "c4-collision"
    members["MT38-pair-foreign-collision/01-pair-origin.md"] = lifecycle_relay(
        role="Planner", phase="DESIGN", authority="design-only", dispatch_id=f"{collision}-pair",
        from_addr="beta.planner", to_addr="beta.implementer", fields=(("DESIGN_DOC_ID", f"lock-{collision}"),),
    )
    members["MT38-pair-foreign-collision/02-foreign-origin.md"] = origin_relay(collision)
    members["MT38-pair-foreign-collision/03-consumer.md"] = consumer_relay(collision)

    multiple = "c4-multiple"
    members["MT39-two-foreign-owners/01-alpha-origin.md"] = origin_relay(multiple, owner="alpha")
    members["MT39-two-foreign-owners/02-gamma-origin.md"] = origin_relay(multiple, owner="gamma", tier="domain", dispatch_id=f"{multiple}-gamma-origin")
    members["MT39-two-foreign-owners/03-consumer.md"] = consumer_relay(multiple)

    members["MT40-master-no-origin/01-consumer.md"] = consumer_relay(
        "c4-master-none", role="Master Planner", from_addr="beta.master-planner", to_addr="beta.master-reviewer", kind=None,
    )
    members["MT41-pair-no-origin-control/01-consumer.md"] = consumer_relay("c4-pair-none", kind=None)
    members["MT42-later-origin-control/01-consumer.md"] = consumer_relay("c4-later", kind=None)
    members["MT42-later-origin-control/02-origin.md"] = origin_relay("c4-later")

    revision = "c4-revision"
    members["MT43-same-owner-revisions/01-origin-v1.md"] = origin_relay(revision, dispatch_id=f"{revision}-v1")
    members["MT43-same-owner-revisions/02-origin-v2.md"] = origin_relay(revision, dispatch_id=f"{revision}-v2")
    members["MT43-same-owner-revisions/03-review-v2.md"] = review_relay(revision, parent=f"{revision}-v2")
    members["MT43-same-owner-revisions/04-consumer.md"] = consumer_relay(revision)

    ambiguous = "c4-origin-tie"
    members["MT44-same-position-origin-ambiguity/a/01-origin.md"] = origin_relay(ambiguous, dispatch_id=f"{ambiguous}-a")
    members["MT44-same-position-origin-ambiguity/b/01-origin.md"] = origin_relay(ambiguous, dispatch_id=f"{ambiguous}-b")
    members["MT44-same-position-origin-ambiguity/02-consumer.md"] = consumer_relay(ambiguous)

    add_chain(
        members, "MT45-reviewer-design-origin", case="c5-reviewer-design",
        origin=origin_relay("c5-reviewer-design", reviewer=True), reviews=(),
    )
    add_chain(
        members, "MT46-reviewer-audit-origin", case="c5-reviewer-audit",
        origin=origin_relay("c5-reviewer-audit", tier="domain", kind="audit-record", reviewer=True), reviews=(),
        consumer=consumer_relay("c5-reviewer-audit", kind="audit-record"),
    )
    add_chain(
        members, "MT47-origin-kind-missing", case="c5-origin-kind-missing",
        origin=origin_relay("c5-origin-kind-missing", kind=None),
    )
    add_chain(
        members, "MT48-approval-kind-missing", case="c5-review-kind-missing",
        reviews=(review_relay("c5-review-kind-missing", kind=None),),
    )
    add_chain(
        members, "MT49-approval-kind-mismatch", case="c5-review-kind-mismatch",
        reviews=(review_relay("c5-review-kind-mismatch", kind="audit-record"),),
    )
    add_chain(
        members, "MT50-consumer-kind-mismatch", case="c5-consumer-kind-mismatch",
        consumer=consumer_relay("c5-consumer-kind-mismatch", kind="audit-record"),
    )
    add_chain(
        members, "MT51-cross-owner-reviewer", case="c5-cross-review",
        reviews=(review_relay("c5-cross-review", owner="gamma"),),
    )

    unreviewed = "c5-unreviewed-v2"
    members["MT52-latest-origin-unreviewed/01-origin-v1.md"] = origin_relay(unreviewed, dispatch_id=f"{unreviewed}-v1")
    members["MT52-latest-origin-unreviewed/02-review-v1.md"] = review_relay(unreviewed, parent=f"{unreviewed}-v1")
    members["MT52-latest-origin-unreviewed/03-origin-v2.md"] = origin_relay(unreviewed, dispatch_id=f"{unreviewed}-v2")
    members["MT52-latest-origin-unreviewed/04-consumer.md"] = consumer_relay(unreviewed)

    latest_origin = "c5-v2-must-revise"
    members["MT53-latest-origin-must-revise/01-origin-v1.md"] = origin_relay(latest_origin, dispatch_id=f"{latest_origin}-v1")
    members["MT53-latest-origin-must-revise/02-origin-v2.md"] = origin_relay(latest_origin, dispatch_id=f"{latest_origin}-v2")
    members["MT53-latest-origin-must-revise/03-review-v2.md"] = review_relay(latest_origin, parent=f"{latest_origin}-v2", verdict="must-revise")
    members["MT53-latest-origin-must-revise/04-consumer.md"] = consumer_relay(latest_origin)

    latest_review = "c5-latest-review"
    members["MT54-latest-review-no-prefilter/01-origin.md"] = origin_relay(latest_review)
    members["MT54-latest-review-no-prefilter/02-review-approve.md"] = review_relay(latest_review, dispatch_id=f"{latest_review}-approve")
    members["MT54-latest-review-no-prefilter/03-review-must-revise.md"] = review_relay(latest_review, dispatch_id=f"{latest_review}-must", verdict="must-revise")
    members["MT54-latest-review-no-prefilter/04-consumer.md"] = consumer_relay(latest_review)

    stale = "c5-stale-review"
    members["MT55-review-must-parent-latest-revision/01-origin-v1.md"] = origin_relay(stale, dispatch_id=f"{stale}-v1")
    members["MT55-review-must-parent-latest-revision/02-review-v1.md"] = review_relay(stale, parent=f"{stale}-v1")
    members["MT55-review-must-parent-latest-revision/03-origin-v2.md"] = origin_relay(stale, dispatch_id=f"{stale}-v2")
    members["MT55-review-must-parent-latest-revision/04-consumer.md"] = consumer_relay(stale)

    later_review = "c5-later-review"
    members["MT56-later-approval-refused/01-origin.md"] = origin_relay(later_review)
    members["MT56-later-approval-refused/02-consumer.md"] = consumer_relay(later_review)
    members["MT56-later-approval-refused/03-review.md"] = review_relay(later_review)
    members["MT57-invented-reviewer-lock/01-consumer.md"] = consumer_relay(
        "c5-invented-reviewer", role="Master Reviewer", from_addr="beta.master-reviewer",
        to_addr="beta.master-planner", phase="SITREP", authority="report-only", kind=None,
    )

    review_tie = "c5-review-tie"
    members["MT58-same-position-review-ambiguity/01-origin.md"] = origin_relay(review_tie)
    members["MT58-same-position-review-ambiguity/a/02-review.md"] = review_relay(review_tie, dispatch_id=f"{review_tie}-a")
    members["MT58-same-position-review-ambiguity/b/02-review.md"] = review_relay(review_tie, dispatch_id=f"{review_tie}-b")
    members["MT58-same-position-review-ambiguity/03-consumer.md"] = consumer_relay(review_tie)

    for seat, role, from_addr, to_addr, phase, authority in LIFECYCLE_CONSUMERS:
        case = f"c5-seat-{seat}"
        add_chain(
            members, f"MT59-all-seat-{seat}", case=case,
            consumer=consumer_relay(case, role=role, from_addr=from_addr, to_addr=to_addr, phase=phase, authority=authority),
        )

    conflict_consumer = "c7-consumer-lock"
    add_chain(
        members, "MT60-consumer-lock-conflict", case=conflict_consumer,
        consumer=consumer_relay(
            conflict_consumer,
            extra_fields=(("DESIGN_LOCK_ID", "lock-other"),),
        ),
    )
    conflict_kind = "c7-consumer-kind"
    add_chain(
        members, "MT61-consumer-kind-conflict", case=conflict_kind,
        consumer=consumer_relay(
            conflict_kind,
            extra_fields=(("DESIGN_RECORD_KIND", "audit-record"),),
        ),
    )
    conflict_origin = "c7-origin-authority"
    add_chain(
        members, "MT62-origin-authority-conflict", case=conflict_origin,
        origin=origin_relay(conflict_origin, extra_fields=(("AUTHORITY", "review-only"),)),
    )
    conflict_review = "c7-review-verdict"
    add_chain(
        members, "MT63-review-verdict-conflict", case=conflict_review,
        reviews=(review_relay(conflict_review, extra_fields=(("DESIGN_REVIEW_VERDICT", "must-revise"),)),),
    )

    direct = "c4-direct"
    members["MT64-direct-origin-pair-control/01-direct-origin.md"] = lifecycle_relay(
        role="Operator", phase="DESIGN", authority="design-only", dispatch_id=f"{direct}-origin",
        from_addr="operator", to_addr="beta.pair-planner",
        fields=(("DESIGN_DOC_ID", f"lock-{direct}"), ("DESIGN_RECORD_KIND", "design-doc")),
    )
    members["MT64-direct-origin-pair-control/02-consumer.md"] = consumer_relay(direct, kind=None)

    add_chain(
        members, "MT65-design-kind-wrong-origin-shape", case="c5-design-shape",
        origin=origin_relay("c5-design-shape", kind="design-doc", phase="AUDIT", authority="report-only"),
    )
    add_chain(
        members, "MT66-audit-kind-wrong-origin-shape", case="c5-audit-shape",
        origin=origin_relay("c5-audit-shape", kind="audit-record", phase="DESIGN", authority="design-only"),
        reviews=(review_relay("c5-audit-shape", kind="audit-record"),),
        consumer=consumer_relay("c5-audit-shape", kind="audit-record"),
    )
    add_chain(
        members, "MT77-unsupported-origin-kind", case="c5-unsupported-kind",
        origin=origin_relay("c5-unsupported-kind", kind="unsupported-record"),
        reviews=(review_relay("c5-unsupported-kind", kind="unsupported-record"),),
        consumer=consumer_relay("c5-unsupported-kind", kind="unsupported-record"),
    )
    members["MT67-pair-invented-design-doc/01-consumer.md"] = consumer_relay("c5-pair-invented")
    for seat, role, from_addr, to_addr, phase, authority in LIFECYCLE_CONSUMERS:
        case = f"c5-missing-review-{seat}"
        members[f"MT68-all-seat-missing-review-{seat}/01-origin.md"] = origin_relay(case)
        members[f"MT68-all-seat-missing-review-{seat}/02-consumer.md"] = consumer_relay(
            case, role=role, from_addr=from_addr, to_addr=to_addr, phase=phase, authority=authority,
        )

    for number, order in ((69, "parent-first"), (70, "other-first")):
        case = f"c8-review-parent-{order}"
        members[f"MT{number}-later-review-parent-conflict-{order}/01-origin.md"] = origin_relay(case)
        members[f"MT{number}-later-review-parent-conflict-{order}/02-review-approve.md"] = review_relay(
            case, dispatch_id=f"{case}-approve",
        )
        parent = f"{case}-origin" if order == "parent-first" else f"{case}-other-parent"
        other_parent = f"{case}-other-parent" if order == "parent-first" else f"{case}-origin"
        members[f"MT{number}-later-review-parent-conflict-{order}/03-review-must-revise.md"] = review_relay(
            case,
            verdict="must-revise",
            parent=parent,
            dispatch_id=f"{case}-must-revise",
            extra_fields=(("PARENT_DISPATCH_ID", other_parent),),
        )
        members[f"MT{number}-later-review-parent-conflict-{order}/04-consumer.md"] = consumer_relay(case)

    doc_lock_first = "c8-origin-doc-lock-first"
    add_chain(
        members,
        "MT71-origin-doc-conflict-lock-first",
        case=doc_lock_first,
        origin=origin_relay(doc_lock_first, extra_fields=(("DESIGN_DOC_ID", "lock-other"),)),
    )
    doc_other_first = "c8-origin-doc-other-first"
    add_chain(
        members,
        "MT72-origin-doc-conflict-other-first",
        case=doc_other_first,
        origin=lifecycle_relay(
            role="Master Planner",
            phase="DESIGN",
            authority="design-only",
            dispatch_id=f"{doc_other_first}-origin",
            from_addr="alpha.master-planner",
            to_addr="alpha.master-reviewer",
            fields=(
                ("DESIGN_DOC_ID", "lock-other"),
                ("DESIGN_DOC_ID", f"lock-{doc_other_first}"),
                ("DESIGN_RECORD_KIND", "design-doc"),
            ),
        ),
    )

    phase_design_first = "c8-origin-phase-design-first"
    add_chain(
        members,
        "MT73-origin-phase-conflict-design-first",
        case=phase_design_first,
        origin=origin_relay(phase_design_first, extra_fields=(("PHASE", "AUDIT"),)),
    )
    phase_audit_first = "c8-origin-phase-audit-first"
    add_chain(
        members,
        "MT74-origin-phase-conflict-audit-first",
        case=phase_audit_first,
        origin=origin_relay(
            phase_audit_first,
            phase="AUDIT",
            authority="report-only",
            extra_fields=(("PHASE", "DESIGN"),),
        ),
    )

    collision = "c8-conflicted-collision"
    members["MT75-pair-conflicted-foreign-collision/01-pair-origin.md"] = lifecycle_relay(
        role="Planner",
        phase="DESIGN",
        authority="design-only",
        dispatch_id=f"{collision}-pair-origin",
        from_addr="beta.planner",
        to_addr="beta.implementer",
        fields=(("DESIGN_DOC_ID", f"lock-{collision}"),),
    )
    members["MT75-pair-conflicted-foreign-collision/02-foreign-origin.md"] = origin_relay(
        collision, extra_fields=(("DESIGN_DOC_ID", "lock-other"),),
    )
    members["MT75-pair-conflicted-foreign-collision/03-consumer.md"] = consumer_relay(collision)

    from_conflict = "c8-origin-from-conflict"
    add_chain(
        members,
        "MT76-origin-from-conflict",
        case=from_conflict,
        origin=origin_relay(from_conflict, extra_fields=(("FROM", "alpha.planner"),)),
    )
    return members


def generated_members() -> dict[str, str]:
    members = {"MT0-generator-smoke.md": SMOKE_CONTENT}
    for name, role, from_addr in (
        ("H33a-orchestrator-borrows-operator.md", "Operator", "orchestrator"),
        ("H33b-operator-borrows-orchestrator.md", "Orchestrator Planner", "operator"),
        ("H33c-orchestrator-truthful.md", "Orchestrator Planner", "orchestrator"),
        ("H33d-operator-truthful.md", "Operator", "operator"),
        ("H33e-operator-borrows-planner.md", "Planner", "operator"),
        ("H33f-operator-borrows-reviewer.md", "Reviewer", "operator"),
        ("H33g-orchestrator-borrows-planner.md", "Planner", "orchestrator"),
    ):
        members[name] = lifecycle_relay(
            role=role,
            phase="SITREP",
            authority="report-only",
            dispatch_id=name.removesuffix(".md").lower(),
            from_addr=from_addr,
            to_addr="qi.implementer",
        )
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
    members["DI1a-dispatch-id-conflict.md"] = dispatch_id_conflict_content()
    members["DI1c-dispatch-id-conflict-template.md"] = dispatch_id_conflict_content()
    for prefix, template_mode in (("MT2", False), ("MTT2", True)):
        members[f"{prefix}8-from-launder-master-first.md"] = h27_launder_content(
            master_first=True, template_mode=template_mode,
        )
        members[f"{prefix}9-from-launder-master-last.md"] = h27_launder_content(
            master_first=False, template_mode=template_mode,
        )
    for number, template_owner, master_first in (
        (30, "qi", True),
        (31, "qi", False),
        (32, "<team>", True),
        (33, "<team>", False),
    ):
        order = "master-first" if master_first else "master-last"
        members[f"MTT{number}-from-launder-{order}.md"] = h27_launder_content(
            master_first=master_first, template_mode=True, template_owner=template_owner,
        )
    members.update(lifecycle_members())
    members.update(commission_members())
    c13_subtree = c13_dispatch_index_subtree()
    for member_name in ("C13-CM79-dispatch-index", "C13-CM79c-dispatch-index-control"):
        for relative_name, content in c13_subtree.items():
            members[f"{member_name}/{relative_name}"] = content
    return members


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    members = generated_members()
    for path in FIXTURE_DIR.rglob("*.md"):
        if path.relative_to(FIXTURE_DIR).as_posix() not in members:
            path.unlink()
    for name, content in members.items():
        path = FIXTURE_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for directory in sorted((p for p in FIXTURE_DIR.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()


if __name__ == "__main__":
    main()
