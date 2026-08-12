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
    extra = "".join(f"{key}: {value}\n" for key, value in fields)
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
    members["MT67-pair-invented-design-doc/01-consumer.md"] = consumer_relay("c5-pair-invented")
    for seat, role, from_addr, to_addr, phase, authority in LIFECYCLE_CONSUMERS:
        case = f"c5-missing-review-{seat}"
        members[f"MT68-all-seat-missing-review-{seat}/01-origin.md"] = origin_relay(case)
        members[f"MT68-all-seat-missing-review-{seat}/02-consumer.md"] = consumer_relay(
            case, role=role, from_addr=from_addr, to_addr=to_addr, phase=phase, authority=authority,
        )
    return members


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
