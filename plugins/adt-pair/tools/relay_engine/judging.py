"""Admission-time candidate rules over an immutable predecessor population.

Unlike a sweep, only the subject is evaluated; helper selection still sees
all predecessor carriers. The separate sweep remains the agreement oracle.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import time

from relay_engine import rules
from relay_engine.envelope import Envelope
from relay_engine.rules import *
from relay_engine.rules import _structural


@dataclass(frozen=True)
class Corpus:
    entries: tuple[tuple[int, str, bytes], ...]


def corpus_before(ledger, seq: int | None) -> Corpus:
    rows = ledger.execute(
        "SELECT seq,rendered_path,body FROM relays WHERE (? IS NULL OR seq<?) ORDER BY seq",
        (seq, seq)).fetchall()
    return Corpus(tuple((number, path, bytes(body)) for number, path, body in rows))


def partition_of(case) -> str:
    if case["order_divergent"]:
        return "order-divergent"
    if case["future_sensitive"]:
        return "future-sensitive"
    return "aligned"


def _review_visibility(path: Path, phases, candidates) -> List[Finding]:
    """Orchestrator-pair review visibility gate.

    Every orchestrator authority relay in the broad SET must address an
    orchestrator-reviewer in TO or CC unless the run root contains an
    operator-authored no-reviewer waiver. Pair-seat and operator-authored relays
    are exempt by construction because the FROM-set check comes first.
    """
    errors: List[Finding] = []
    waiver = run_has_operator_orch_review_waiver(phases)
    for f, _order, phase, fields, text in candidates:
        from_addrs = split_addresses(fields.get("FROM"))
        if len(from_addrs) != 1:
            continue
        from_addr = from_addrs[0]
        is_orchestrator_from = (from_role(from_addr) == "orchestrator-planner") or (normalized_addr(from_addr) == "orchestrator")
        if not is_orchestrator_from:
            continue
        if not orchestrator_review_in_broad_set(phase, fields, text):
            continue
        addrs = split_addresses(fields.get("TO")) + split_addresses(fields.get("CC"))
        if any(address_role(addr) == "orchestrator-reviewer" for addr in addrs):
            continue
        if waiver:
            continue
        errors.append(_structural('lineage.review-visibility', f.relative_to(path), f"{f.relative_to(path)}: orchestrator authority relay must CC <run>.orchestrator-reviewer (or run under an operator no-reviewer waiver)", operands=()))
    return errors

def _lock_lifecycle(root: Path, phases, candidates):
    """Precompute every consumer route and all foreign-branch diagnostics."""
    routes: Dict[Path, str] = {}
    errors: List[Finding] = []
    for consumer in candidates:
        consumer_path, consumer_order, _phase, _fields, consumer_text = consumer
        lock_values = h27_occurrences(consumer_text, "DESIGN_LOCK_ID")
        if not lock_values:
            continue
        consumer_name = consumer_path.relative_to(root)
        consumer_conflict = False
        for key in ("DESIGN_LOCK_ID", "DESIGN_RECORD_KIND", "FROM"):
            conflict = h27_conflict_message(consumer_text, key)
            if conflict:
                consumer_conflict = True
                if not h27_has_foreign_from_occurrence(consumer_text):
                    errors.append(_structural('lineage.lock-lifecycle', consumer_name, f"{consumer_name}: {conflict}", operands=()))
        if consumer_conflict:
            routes[consumer_path] = "refuse"
            continue
        lock_id, _ = h27_resolved_discriminator(consumer_text, "DESIGN_LOCK_ID")
        if not lock_id:
            routes[consumer_path] = "refuse"
            continue
        origins = h27_select_origins(phases, lock_id, consumer_order)
        route, owner, foreign_origins, refusal = h27_group_and_route(origins, consumer_text)
        routes[consumer_path] = route
        if refusal == "collision":
            errors.append(
                _structural('lineage.lock-lifecycle', consumer_name, f"{consumer_name}: DESIGN_LOCK_ID {lock_id!r} resolves to both pair and master/domain origin "
                f"groups; lock routing fails closed{H27_LOCK_RULE}", operands=())
            )
        elif refusal == "multiple-foreign":
            owner_count = len({from_owner(h27_single_from(item[4])) for item in origins if h27_tier_classification(h27_single_from(item[4])) == "foreign"})
            errors.append(
                _structural('lineage.lock-lifecycle', consumer_name, f"{consumer_name}: DESIGN_LOCK_ID {lock_id!r} resolves to {owner_count} master/domain owner "
                f"groups; lock routing fails closed{H27_LOCK_RULE}", operands=())
            )
        elif refusal == "master-no-origin":
            errors.append(
                _structural('lineage.lock-lifecycle', consumer_name, f"{consumer_name}: master/domain-seat consumer DESIGN_LOCK_ID {lock_id!r} has no resolvable "
                f"earlier master/domain origin{H27_LOCK_RULE}", operands=())
            )
        elif refusal == "origin-conflict":
            conflicted_origin = next(
                item for item in origins if h27_resolved_discriminator(item[4], "FROM")[1]
            )
            origin_name = conflicted_origin[0].relative_to(root)
            origin_label = "foreign origin" if h27_has_foreign_from_occurrence(conflicted_origin[4]) else "origin"
            errors.append(
                _structural('lineage.lock-lifecycle', consumer_name, f"{consumer_name}: {origin_label} {origin_name} has conflicting FROM occurrences; "
                f"lock lifecycle refuses first-value resolution{H27_LOCK_RULE}", operands=(origin_name,))
            )
        elif route == "foreign":
            errors.extend(h27_foreign_lock_errors(root, consumer, lock_id, owner, foreign_origins, phases))
    return routes, errors

def _commission(root: Path, phases, candidates) -> List[Finding]:
    """Validate commission carriers, exact ancestry, and authorization lifetime."""
    errors: List[Finding] = []

    dispatch_holders: Dict[str, List[object]] = {}
    for phase_item in phases:
        for dispatch_id in set(h27_occurrences(phase_item[4], "DISPATCH_ID")):
            dispatch_holders.setdefault(dispatch_id, []).append(phase_item)

    def relname(item) -> str:
        return str(item[0].relative_to(root))

    def marker_universe(marker: str, commission_id: str, before_order):
        return sorted(
            (
                item for item in phases
                if h27_is_earlier(item[1], before_order)
                and h27_occurrences(item[4], marker)
                and commission_id in h27_occurrences(item[4], "COMMISSION_ID")
            ),
            key=lambda item: item[1],
        )

    def latest_at_one_position(items):
        if not items:
            return None, 0
        latest = h27_same_position_latest(items)
        return (latest[0] if len(latest) == 1 else None), len(latest)

    def selected_conflict(item, keys) -> str | None:
        for key in keys:
            if h27_resolved_discriminator(item[4], key)[1]:
                return key
        return None

    def resolve_ancestry(parent_id: str | None, before_order, consumer, edge: str, site_rule: str):
        """Resolve one edge over every local DISPATCH_ID occurrence, then validate."""
        if parent_id in {None, ""}:
            return None
        candidates = sorted(
            (item for item in dispatch_holders.get(parent_id, []) if h27_is_earlier(item[1], before_order)),
            key=lambda item: item[1],
        )
        if not candidates:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: {edge} {parent_id!r} does not resolve to an earlier relay{site_rule}", operands=())
            )
            return None
        latest = h27_same_position_latest(candidates)
        if len(latest) != 1:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: {edge} {parent_id!r} has {len(latest)} same-position latest holders; "
                f"ambiguity fails closed{site_rule}", operands=())
            )
            return None
        selected = latest[0]
        if h27_resolved_discriminator(selected[4], "DISPATCH_ID")[1]:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: selected {edge} relay {relname(selected)} has conflicting DISPATCH_ID "
                f"occurrences; ancestry resolution refuses first-value resolution{site_rule}", operands=(relname(selected),))
            )
            return None
        return selected

    def latest_stage(candidates, consumer, label: str, site_rule: str):
        candidates = sorted(candidates, key=lambda item: item[1])
        if not candidates:
            return None
        latest = h27_same_position_latest(candidates)
        if len(latest) != 1:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: commission {h27_resolved_discriminator(consumer[4], 'COMMISSION_ID')[0]!r} "
                f"has {len(latest)} {label} at the latest order position; ambiguity fails closed{site_rule}", operands=())
            )
            return False
        return latest[0]

    def charter_candidates(charter_doc_id: str, commission_id: str, before_order):
        return [
            item for item in phases
            if h27_is_earlier(item[1], before_order)
            and (
                charter_doc_id in h27_occurrences(item[4], "DESIGN_DOC_ID")
                or commission_id in h27_occurrences(item[4], "COMMISSION_ID")
            )
            and h27_occurrences(item[4], "DESIGN_DOC_ID")
            and h27_any_role_occurrence(item[4], {"master-planner"})
        ]

    def review_candidates(charter, charter_doc_id: str, commission_id: str, before_order):
        charter_id, charter_id_conflict = h27_resolved_discriminator(charter[4], "DISPATCH_ID")
        if charter_id_conflict or charter_id in {None, ""}:
            return []
        return [
            item for item in phases
            if h27_is_earlier(item[1], before_order)
            and (
                charter_doc_id in h27_occurrences(item[4], "DESIGN_DOC_ID")
                or commission_id in h27_occurrences(item[4], "COMMISSION_ID")
            )
            and h27_occurrences(item[4], "DESIGN_DOC_ID")
            and h27_any_role_occurrence(item[4], {"master-reviewer"})
        ]

    def stage_conflict(item, keys, consumer, label: str, site_rule: str) -> bool:
        conflict = selected_conflict(item, keys)
        if conflict:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: selected {label} {relname(item)} has conflicting {conflict} occurrences; "
                f"ancestry resolution refuses first-value resolution{site_rule}", operands=(relname(item),))
            )
            return True
        return False

    def same_owner_reviewer(review, charter) -> bool:
        review_from = h27_single_from(review[4])
        charter_from = h27_single_from(charter[4])
        expected = same_owner_addr(from_owner(charter_from), "master-reviewer")
        return from_role(review_from) == "master-reviewer" and normalized_addr(review_from) == normalized_addr(expected)

    def charter_parent_authorization(charter, authorization, consumer, site_rule: str) -> bool:
        parent_id, parent_conflict = h27_resolved_discriminator(charter[4], "PARENT_DISPATCH_ID")
        if parent_conflict:
            return False
        if parent_id in {None, ""}:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: selected charter revision {relname(charter)} lacks a usable "
                f"PARENT_DISPATCH_ID{site_rule}", operands=(relname(charter),))
            )
            return False
        parent = resolve_ancestry(parent_id, charter[1], consumer, "charter parent", site_rule)
        if parent is None:
            return False
        if parent is not authorization:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: charter parent {parent_id!r} resolves to {relname(parent)}, which is not "
                f"the reselected stage-(a) authorization{site_rule}", operands=(relname(parent),))
            )
            return False
        return True

    def selected_authorization_conflict(selected, consumer) -> bool:
        conflict = selected_conflict(
            selected,
            ("FROM", "PHASE", "AUTHORITY", "TO", "DISPATCH_ID", "COMMISSION_AUTHORIZATION")
            + H27_COMMISSION_SURFACE,
        )
        if conflict:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: selected authorization-universe member {relname(selected)} has conflicting "
                f"{conflict} occurrences; universe selection refuses first-value resolution{H27_COMMISSION_AUTH_RULE}", operands=(relname(selected),))
            )
            return True
        return False

    def authorization_for(consumer, commission_id: str, site_rule: str):
        candidates = marker_universe("COMMISSION_AUTHORIZATION", commission_id, consumer[1])
        selected, tied = latest_at_one_position(candidates)
        if not candidates:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: commission {commission_id!r} has no earlier authorization-universe member{site_rule}", operands=())
            )
            return None
        if selected is None:
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: commission {commission_id!r} has {tied} authorization-universe candidates "
                f"at the latest order position; ambiguity fails closed{H27_COMMISSION_AUTH_RULE}", operands=())
            )
            return None
        if selected_authorization_conflict(selected, consumer):
            return None
        if not h27_commission_auth_shape_ok(selected):
            errors.append(
                _structural('lineage.commission', relname(consumer), f"{relname(consumer)}: latest authorization-universe member {relname(selected)} fails stage-(a) "
                f"shape; marker-bearing malformed authorization shadows and fails{H27_COMMISSION_AUTH_RULE}", operands=(relname(selected),))
            )
            return None
        return selected

    def authorization_conflict_for(consumer, commission_id: str) -> bool:
        """Report only a uniquely selected authorization's conflict, without trusting its shape."""
        selected, _tied = latest_at_one_position(
            marker_universe("COMMISSION_AUTHORIZATION", commission_id, consumer[1])
        )
        if selected is None:
            return False
        return selected_authorization_conflict(selected, consumer)

    def add_nonmaster_conflicts(item) -> bool:
        """File-mode H27 already reports master-seat conflicts; cover special/pair stages here."""
        from_addr = h27_single_from(item[4])
        is_master = from_role(from_addr) in MASTER_TIER_ROLES
        conflicted = False
        for key in H27_CONFLICT_FIELDS:
            conflict = h27_conflict_message(item[4], key)
            if conflict:
                conflicted = True
                if not is_master:
                    errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: {conflict}", operands=()))
        return conflicted

    for item in candidates:
        _path, order, _phase, _fields, text = item
        carrier_presence = {key for key in H27_COMMISSION_CARRIERS if h27_occurrences(text, key)}
        dda_presence = bool(h27_occurrences(text, "DELEGATED_DISPATCH_AUTHORITY"))
        master_grant_marker = dda_presence and h27_any_role_occurrence(text, {"master-planner"})
        if not carrier_presence and not master_grant_marker:
            continue

        commission_id, commission_id_conflict = h27_resolved_discriminator(text, "COMMISSION_ID")
        if (
            carrier_presence == {"COMMISSION_ID"}
            and not dda_presence
            and not commission_id_conflict
            and commission_id not in {None, ""}
        ):
            # A non-empty ID alone is an inert reference. Empty ID is engaged.
            continue

        from_addr = h27_single_from(text)
        from_role_value = from_role(from_addr)
        phase, _ = h27_resolved_discriminator(text, "PHASE")
        auth_marker = bool(h27_occurrences(text, "COMMISSION_AUTHORIZATION"))

        # A direct grant is insulated only when it carries none of the five
        # commission carriers. Any one carrier engages the full machine; the
        # authorization carrier produces the both-markers case.
        if h27_direct_path_has_commission_carrier(from_role_value, dda_presence, carrier_presence):
            errors.append(
                _structural('lineage.commission', relname(item), f"{relname(item)}: a direct delegation grant carries commission gating carriers; "
                "direct path must carry none of the five"
                f"{H27_COMMISSION_RULE}", operands=())
            )
            continue

        if (
            auth_marker
            and not dda_presence
            and h27_occurrences(text, "COMMISSION_ID")
            and not commission_id_conflict
            and not h27_commission_id_grammatical(commission_id)
        ):
            errors.append(
                _structural('lineage.commission', relname(item), f"{relname(item)}: commission machine engaged with non-grammatical COMMISSION_ID "
                f"{commission_id!r}; expected [a-z0-9][a-z0-9-]*{H27_COMMISSION_RULE}", operands=())
            )
            continue

        # Logical CH identity is validated at the authorization trust root.
        # Charter-local DESIGN_DOC_ID equality is checked at its own stage;
        # later equality-surface diagnostics remain unchanged.
        charter_doc_id, _ = h27_resolved_discriminator(text, "CHARTER_DOC_ID")
        if (
            auth_marker
            and not commission_id_conflict
            and h27_commission_id_grammatical(commission_id)
            and charter_doc_id not in {None, f"CH-{commission_id}"}
        ):
            errors.append(
                _structural('lineage.commission', relname(item), f"{relname(item)}: CHARTER_DOC_ID {charter_doc_id!r} must equal "
                f"{'CH-' + commission_id!r}{H27_COMMISSION_RULE}", operands=())
            )
            continue

        # Stage (a) is marker-defined. Both markers intentionally do not exit
        # here; they fall through to delegation-stage classification.
        if auth_marker and not dda_presence:
            if add_nonmaster_conflicts(item):
                continue
            if not h27_commission_auth_shape_ok(item):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: relay carries COMMISSION_AUTHORIZATION but fails stage-(a) shape "
                    "(literal yes, direct-authority FROM, PHASE PLAN, AUTHORITY plan-only, TO exactly one "
                    "master-planner, complete equality surface, pair-planner COMMISSION_TO)"
                    f"{H27_COMMISSION_AUTH_RULE}", operands=())
                )
            continue

        is_receipt = phase == "PLAN" and canonical_role(from_role_value) == "planner"
        if is_receipt and not h27_commission_surface_complete(text):
            if add_nonmaster_conflicts(item):
                continue
            errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: commissioned pair receipt has incomplete equality surface{H27_COMMISSION_RECEIPT_RULE}", operands=()))
            continue

        if add_nonmaster_conflicts(item):
            continue
        if commission_id in {None, ""}:
            errors.append(
                _structural('lineage.commission', relname(item), f"{relname(item)}: commission machine engaged but relay is not consumed by any Task 9.5a stage"
                f"{H27_COMMISSION_RULE}", operands=())
            )
            continue

        # Charter stage: select the marker-universe authorization, then resolve
        # the mandatory parent generically across every earlier DISPATCH_ID holder.
        design_doc_id, _ = h27_resolved_discriminator(text, "DESIGN_DOC_ID")
        if phase == "DESIGN" and from_role_value == "master-planner" and design_doc_id != charter_doc_id:
            errors.append(
                _structural('lineage.commission', relname(item), f"{relname(item)}: charter DESIGN_DOC_ID {design_doc_id!r} must equal "
                f"CHARTER_DOC_ID {charter_doc_id!r}{H27_COMMISSION_CHARTER_RULE}", operands=())
            )
            continue
        if phase == "DESIGN" and from_role_value == "master-planner" and design_doc_id == charter_doc_id:
            if not h27_commission_charter_shape_ok(item):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter fails stage-(b) shape; requires master-planner PHASE DESIGN, "
                    "AUTHORITY design-only, matching document identity, and DESIGN_RECORD_KIND design-doc"
                    f"{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            authorization = authorization_for(item, commission_id, H27_COMMISSION_CHARTER_RULE)
            if authorization is None:
                continue
            parent_id, _ = h27_resolved_discriminator(text, "PARENT_DISPATCH_ID")
            if parent_id in {None, ""}:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter PARENT_DISPATCH_ID is mandatory and must resolve the selected "
                    f"authorization{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            parent = resolve_ancestry(parent_id, order, item, "charter parent", H27_COMMISSION_CHARTER_RULE)
            if parent is None:
                continue
            if parent is not authorization:
                selected_id, _ = h27_resolved_discriminator(authorization[4], "DISPATCH_ID")
                if h27_commission_auth_shape_ok(parent):
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: charter parent {parent_id!r} does not equal selected authorization "
                        f"{selected_id!r}{H27_COMMISSION_CHARTER_RULE}", operands=())
                    )
                else:
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: charter parent {parent_id!r} resolves to {relname(parent)}, which is not "
                        f"the selected stage-(a) authorization{H27_COMMISSION_CHARTER_RULE}", operands=(relname(parent),))
                    )
                continue
            auth_to, _ = h27_resolved_discriminator(authorization[4], "TO")
            auth_to_addrs = split_addresses(auth_to)
            if len(auth_to_addrs) != 1 or normalized_addr(auth_to_addrs[0]) != normalized_addr(from_addr):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter FROM must equal the master-planner address selected authorization TO"
                    f"{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            if h27_commission_surface(text) != h27_commission_surface(authorization[4]):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter equality surface is not byte-equal to the selected authorization"
                    f"{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
            continue

        # Approval resolves the latest charter revision, its exact edge, and
        # independently reselects the live authorization before accepting it.
        if phase == "DESIGN-REVIEW" and from_role_value == "master-reviewer" and design_doc_id not in {None, ""}:
            if not h27_commission_approval_shape_ok(item):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter approval requires DESIGN_RECORD_KIND: design-doc and a "
                    f"grammatical DESIGN_REVIEW_VERDICT{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            charter = latest_stage(
                charter_candidates(charter_doc_id, commission_id, order), item,
                "charter revisions", H27_COMMISSION_CHARTER_RULE,
            )
            if charter is False:
                continue
            if charter is None:
                errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: charter approval has no earlier charter revision{H27_COMMISSION_CHARTER_RULE}", operands=()))
                continue
            parent_id, _ = h27_resolved_discriminator(text, "PARENT_DISPATCH_ID")
            if parent_id in {None, ""}:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter approval PARENT_DISPATCH_ID is mandatory and must resolve the latest "
                    f"charter revision{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            parent = resolve_ancestry(parent_id, order, item, "charter approval parent", H27_COMMISSION_CHARTER_RULE)
            if parent is None:
                continue
            if parent is not charter:
                charter_id_value, _ = h27_resolved_discriminator(charter[4], "DISPATCH_ID")
                if h27_commission_charter_shape_ok(parent):
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: charter approval parent {parent_id!r} does not equal latest charter "
                        f"revision {charter_id_value!r}{H27_COMMISSION_CHARTER_RULE}", operands=())
                    )
                else:
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: charter approval parent {parent_id!r} resolves to {relname(parent)}, "
                        f"which is not the latest charter revision{H27_COMMISSION_CHARTER_RULE}", operands=(relname(parent),))
                    )
                continue
            charter_conflicted = stage_conflict(
                charter,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                item, "charter revision", H27_COMMISSION_CHARTER_RULE,
            )
            if charter_conflicted:
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_charter_shape_ok(charter):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter approval parent is not a valid stage-(b) charter revision"
                    f"{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            if not same_owner_reviewer(item, charter):
                charter_owner = from_owner(h27_single_from(charter[4]))
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter approval must be FROM {same_owner_addr(charter_owner, 'master-reviewer')}, "
                    f"the charter owner's tier reviewer{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
                continue
            charter_id, conflict = h27_resolved_discriminator(charter[4], "COMMISSION_ID")
            if conflict or charter_id in {None, ""}:
                errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: selected charter revision has no usable COMMISSION_ID{H27_COMMISSION_CHARTER_RULE}", operands=()))
                continue
            authorization = authorization_for(item, charter_id, H27_COMMISSION_CHARTER_RULE)
            if authorization is None:
                continue
            if not charter_parent_authorization(charter, authorization, item, H27_COMMISSION_CHARTER_RULE):
                continue
            surfaces = {
                h27_commission_surface(text),
                h27_commission_surface(charter[4]),
                h27_commission_surface(authorization[4]),
            }
            if len(surfaces) != 1:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: approval equality surface is not byte-equal to the selected authorization "
                    f"and charter revision{H27_COMMISSION_CHARTER_RULE}", operands=())
                )
            continue

        # Grant local shape and four-way equality. The literal marker is
        # checked separately so YES/true remain universe members but fail.
        if dda_presence and from_role_value == "master-planner":
            marker, _ = h27_resolved_discriminator(text, "DELEGATED_DISPATCH_AUTHORITY")
            if marker != "yes":
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: master-planner grant requires literal DELEGATED_DISPATCH_AUTHORITY: yes; "
                    f"got {marker!r}{H27_COMMISSION_GRANT_RULE}", operands=())
                )
                continue
            commission_to, _ = h27_resolved_discriminator(text, "COMMISSION_TO")
            if not h27_pair_planner_address(commission_to):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: COMMISSION_TO {commission_to!r} is not exactly one non-special "
                    f"pair-planner address{H27_COMMISSION_GRANT_RULE}", operands=())
                )
                continue
            to_raw, _ = h27_resolved_discriminator(text, "TO")
            to_addrs = split_addresses(to_raw)
            if len(to_addrs) != 1 or normalized_addr(to_addrs[0]) != normalized_addr(commission_to):
                errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: grant TO must be exactly the COMMISSION_TO address{H27_COMMISSION_GRANT_RULE}", operands=()))
                continue
            if not h27_commission_grant_shape_ok(item):
                errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: master-planner grant fails stage-(c) shape{H27_COMMISSION_GRANT_RULE}", operands=()))
                continue
            charter = latest_stage(
                charter_candidates(charter_doc_id, commission_id, order), item,
                "charter revisions", H27_COMMISSION_GRANT_RULE,
            )
            if charter is False:
                continue
            if charter is None:
                errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: grant lacks an earlier charter revision{H27_COMMISSION_GRANT_RULE}", operands=()))
                continue
            charter_conflicted = stage_conflict(
                charter,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                item, "latest charter revision", H27_COMMISSION_GRANT_RULE,
            )
            if charter_conflicted:
                eager_approval, _tied = latest_at_one_position(
                    review_candidates(charter, charter_doc_id, commission_id, order)
                )
                if eager_approval is not None:
                    stage_conflict(
                        eager_approval,
                        ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                         "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                        item, "charter review", H27_COMMISSION_GRANT_RULE,
                    )
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_charter_shape_ok(charter):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected latest charter revision {relname(charter)} fails stage-(b) shape"
                    f"{H27_COMMISSION_GRANT_RULE}", operands=(relname(charter),))
                )
                continue
            approval = latest_stage(
                review_candidates(charter, charter_doc_id, commission_id, order), item,
                "charter reviews", H27_COMMISSION_GRANT_RULE,
            )
            if approval is False:
                continue
            if approval is None:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: grant has no earlier charter review for the latest charter revision"
                    f"{H27_COMMISSION_GRANT_RULE}", operands=())
                )
                continue
            approval_conflicted = stage_conflict(
                approval,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                item, "charter review", H27_COMMISSION_GRANT_RULE,
            )
            if approval_conflicted:
                authorization_conflict_for(item, commission_id)
                continue
            verdict, _ = h27_resolved_discriminator(approval[4], "DESIGN_REVIEW_VERDICT")
            if not h27_commission_approval_shape_ok(approval) or verdict != "approve":
                if h27_commission_approval_shape_ok(approval):
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: latest charter review {relname(approval)} requires "
                        f"DESIGN_REVIEW_VERDICT: approve; got {verdict!r}{H27_COMMISSION_GRANT_RULE}", operands=(relname(approval),))
                    )
                else:
                    errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: grant parent is not a valid charter review{H27_COMMISSION_GRANT_RULE}", operands=()))
                continue
            if not same_owner_reviewer(approval, charter):
                charter_owner = from_owner(h27_single_from(charter[4]))
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter review must be FROM {same_owner_addr(charter_owner, 'master-reviewer')}, "
                    f"the charter owner's tier reviewer{H27_COMMISSION_GRANT_RULE}", operands=())
                )
                continue
            parent_id, _ = h27_resolved_discriminator(text, "PARENT_DISPATCH_ID")
            if parent_id in {None, ""}:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: grant PARENT_DISPATCH_ID is mandatory and must resolve the latest approving "
                    f"review{H27_COMMISSION_GRANT_RULE}", operands=())
                )
                continue
            parent = resolve_ancestry(parent_id, order, item, "grant parent", H27_COMMISSION_GRANT_RULE)
            if parent is None:
                continue
            if parent is not approval:
                approval_id, _ = h27_resolved_discriminator(approval[4], "DISPATCH_ID")
                if h27_commission_approval_shape_ok(parent):
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: grant parent {parent_id!r} does not equal latest charter review "
                        f"{approval_id!r}{H27_COMMISSION_GRANT_RULE}", operands=())
                    )
                else:
                    errors.append(
                        _structural('lineage.commission', relname(item), f"{relname(item)}: grant parent {parent_id!r} resolves to {relname(parent)}, which is not "
                        f"the latest charter review{H27_COMMISSION_GRANT_RULE}", operands=(relname(parent),))
                    )
                continue
            approval_parent_id, _ = h27_resolved_discriminator(approval[4], "PARENT_DISPATCH_ID")
            if approval_parent_id in {None, ""}:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected charter review {relname(approval)} lacks a usable "
                    f"PARENT_DISPATCH_ID{H27_COMMISSION_GRANT_RULE}", operands=(relname(approval),))
                )
                continue
            approval_parent = resolve_ancestry(
                approval_parent_id, approval[1], item, "approval parent", H27_COMMISSION_GRANT_RULE,
            )
            if approval_parent is None:
                continue
            if approval_parent is not charter:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: approval parent {approval_parent_id!r} resolves to {relname(approval_parent)}, "
                    f"which is not the charter revision{H27_COMMISSION_GRANT_RULE}", operands=(relname(approval_parent),))
                )
                continue
            charter_id, conflict = h27_resolved_discriminator(charter[4], "COMMISSION_ID")
            if conflict or charter_id in {None, ""}:
                errors.append(_structural('lineage.commission', relname(item), f"{relname(item)}: selected charter revision has no usable COMMISSION_ID{H27_COMMISSION_GRANT_RULE}", operands=()))
                continue
            authorization = authorization_for(item, charter_id, H27_COMMISSION_GRANT_RULE)
            if authorization is None:
                continue
            if not charter_parent_authorization(charter, authorization, item, H27_COMMISSION_GRANT_RULE):
                continue
            auth_to, _ = h27_resolved_discriminator(authorization[4], "TO")
            auth_to_addrs = split_addresses(auth_to)
            if len(auth_to_addrs) != 1 or normalized_addr(auth_to_addrs[0]) != normalized_addr(from_addr):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: grant FROM must equal the master-planner address selected authorization TO"
                    f"{H27_COMMISSION_GRANT_RULE}", operands=())
                )
                continue
            surfaces = {
                h27_commission_surface(text), h27_commission_surface(approval[4]),
                h27_commission_surface(charter[4]), h27_commission_surface(authorization[4]),
            }
            if len(surfaces) != 1:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: grant equality surface is not byte-equal across selected authorization, "
                    f"charter revision, approval, and grant{H27_COMMISSION_GRANT_RULE}", operands=())
                )
            continue

        # Pair receipt: target first, then latest grant-universe selection.
        if is_receipt:
            if h27_receipt_self_grants(text, is_receipt):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: commissioned pair receipt carries DELEGATED_DISPATCH_AUTHORITY: yes; "
                    f"self-grant is prohibited{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            commission_to, _ = h27_resolved_discriminator(text, "COMMISSION_TO")
            if normalized_addr(from_addr) != normalized_addr(commission_to):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: commissioned pair receipt FROM {from_addr!r} does not equal "
                    f"COMMISSION_TO {commission_to!r}{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            candidates = marker_universe("DELEGATED_DISPATCH_AUTHORITY", commission_id, order)
            selected, tied = latest_at_one_position(candidates)
            if not candidates:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: commission {commission_id!r} has no earlier grant-universe member"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            if selected is None:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: commission {commission_id!r} has {tied} grant-universe candidates at the "
                    f"latest order position; ambiguity fails closed{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            conflict = selected_conflict(
                selected,
                ("FROM", "PHASE", "AUTHORITY", "TO", "DELEGATED_DISPATCH_AUTHORITY") + H27_COMMISSION_SURFACE,
            )
            if conflict:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected grant-universe member {relname(selected)} has conflicting {conflict} "
                    f"occurrences; universe selection refuses first-value resolution{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(selected),))
                )
                eager_charter, _tied = latest_at_one_position(
                    charter_candidates(charter_doc_id, commission_id, selected[1])
                )
                if eager_charter is not None:
                    stage_conflict(
                        eager_charter,
                        ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                         "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                        item, "latest charter revision", H27_COMMISSION_RECEIPT_RULE,
                    )
                    eager_approval, _tied = latest_at_one_position(
                        review_candidates(eager_charter, charter_doc_id, commission_id, selected[1])
                    )
                    if eager_approval is not None:
                        stage_conflict(
                            eager_approval,
                            ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                             "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                            item, "charter review", H27_COMMISSION_RECEIPT_RULE,
                        )
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_grant_shape_ok(selected):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: latest grant-universe member {relname(selected)} fails stage-(c) shape; "
                    f"marker-bearing malformed grant shadows and fails{H27_COMMISSION_GRANT_RULE}", operands=(relname(selected),))
                )
                continue
            if h27_commission_surface(text) != h27_commission_surface(selected[4]):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: latest grant-universe member is not targeted to this pair planner with a "
                    f"byte-equal surface{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            grant_parent_id, grant_parent_conflict = h27_resolved_discriminator(
                selected[4], "PARENT_DISPATCH_ID",
            )
            if grant_parent_conflict:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected grant-universe member {relname(selected)} has conflicting "
                    f"PARENT_DISPATCH_ID occurrences; ancestry resolution refuses first-value resolution"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(selected),))
                )
                continue
            if grant_parent_id in {None, ""}:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: grant PARENT_DISPATCH_ID is mandatory and must resolve an approving review"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            charter = latest_stage(
                charter_candidates(charter_doc_id, commission_id, selected[1]), item,
                "charter revisions", H27_COMMISSION_RECEIPT_RULE,
            )
            if charter is False:
                continue
            if charter is None:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected latest charter revision <none> fails stage-(b) shape"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            charter_conflicted = stage_conflict(
                charter,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                item, "latest charter revision", H27_COMMISSION_RECEIPT_RULE,
            )
            if charter_conflicted:
                eager_approval, _tied = latest_at_one_position(
                    review_candidates(charter, charter_doc_id, commission_id, selected[1])
                )
                if eager_approval is not None:
                    stage_conflict(
                        eager_approval,
                        ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                         "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                        item, "charter review", H27_COMMISSION_RECEIPT_RULE,
                    )
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_charter_shape_ok(charter):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected latest charter revision {relname(charter)} fails stage-(b) shape"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(charter),))
                )
                continue
            approval = latest_stage(
                review_candidates(charter, charter_doc_id, commission_id, selected[1]), item,
                "charter reviews", H27_COMMISSION_RECEIPT_RULE,
            )
            if approval is False:
                continue
            if approval is None:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected grant has no earlier charter review"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            approval_conflicted = stage_conflict(
                approval,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                item, "charter review", H27_COMMISSION_RECEIPT_RULE,
            )
            if approval_conflicted:
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_approval_shape_ok(approval):
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected charter review {relname(approval)} fails stage-(b) review shape"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(approval),))
                )
                continue
            approval_verdict, _ = h27_resolved_discriminator(approval[4], "DESIGN_REVIEW_VERDICT")
            if approval_verdict != "approve":
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected charter review {relname(approval)} requires "
                    f"DESIGN_REVIEW_VERDICT: approve; got {approval_verdict!r}{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(approval),))
                )
                continue
            if not same_owner_reviewer(approval, charter):
                charter_owner = from_owner(h27_single_from(charter[4]))
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: charter review must be FROM {same_owner_addr(charter_owner, 'master-reviewer')}, "
                    f"the charter owner's tier reviewer{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            grant_parent = resolve_ancestry(
                grant_parent_id, selected[1], item, "grant parent", H27_COMMISSION_RECEIPT_RULE,
            )
            if grant_parent is None:
                continue
            if grant_parent is not approval:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected grant does not parent to the latest charter review before the grant"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            approval_parent_id, approval_parent_conflict = h27_resolved_discriminator(
                approval[4], "PARENT_DISPATCH_ID",
            )
            if approval_parent_conflict:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected charter review {relname(approval)} has conflicting "
                    f"PARENT_DISPATCH_ID occurrences; ancestry resolution refuses first-value resolution"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(approval),))
                )
                continue
            if approval_parent_id in {None, ""}:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: selected charter review {relname(approval)} lacks a usable "
                    f"PARENT_DISPATCH_ID{H27_COMMISSION_RECEIPT_RULE}", operands=(relname(approval),))
                )
                continue
            approval_parent = resolve_ancestry(
                approval_parent_id, approval[1], item, "approval parent", H27_COMMISSION_RECEIPT_RULE,
            )
            if approval_parent is None:
                continue
            if approval_parent is not charter:
                errors.append(
                    _structural('lineage.commission', relname(item), f"{relname(item)}: approval parent {approval_parent_id!r} does not resolve to the latest "
                    f"charter revision before the review"
                    f"{H27_COMMISSION_RECEIPT_RULE}", operands=())
                )
                continue
            authorization = authorization_for(item, commission_id, H27_COMMISSION_RECEIPT_RULE)
            if authorization is None:
                continue
            if not charter_parent_authorization(
                charter, authorization, item, H27_COMMISSION_RECEIPT_RULE,
            ):
                continue
            continue

        errors.append(
            _structural('lineage.commission', relname(item), f"{relname(item)}: commission machine engaged but relay is not consumed by any Task 9.5a stage"
            f"{H27_COMMISSION_RULE}", operands=())
        )
    return errors

def _candidate_lineage(path, phases, candidates, result):
    by_id = dispatch_id_map(phases)

    def resolve_parent(by_id, did, before_order, predicate):
        items = by_id.get(did or "", [])
        if not items:
            return None, [], []
        if len(items) == 1:
            return items[0], items, []
        qualifying = sorted((it for it in items if it[1] < before_order and predicate(it)), key=lambda it: it[1])
        if not qualifying:
            return None, items, []
        return qualifying[-1], items, qualifying[:-1]

    def owner_review_by_id(did: str | None, owner: str | None, want_lock: str, before_order):
        # Latest same-owner Implementer DESIGN-REVIEW of the
        # locked doc, before the PLAN. Do not filter on verdict; the
        # downstream DESIGN_REVIEW_VERDICT check must catch a thread ending in
        # must-revise after an earlier approve.
        if not did or not owner:
            return None
        owned = []
        for item in by_id.get(did, []):
            _p, item_order, item_phase, item_fields, _text = item
            if item_phase != "DESIGN-REVIEW" or item_fields.get("DESIGN_DOC_ID") != want_lock or item_order >= before_order:
                continue
            addrs = split_addresses(item_fields.get("FROM"))
            if len(addrs) == 1 and addr_is(addrs[0], owner, "implementer"):
                owned.append(item)
        return sorted(owned, key=lambda item: item[1])[-1] if owned else None

    def owner_design_by_id(did: str | None, owner: str | None, want_lock: str, before_order):
        # Latest same-owner pair-Planner DESIGN of the locked doc,
        # before the DESIGN-REVIEW. Orchestrator PHASE: DESIGN dispatches that
        # share the thread id are never design-of-record candidates.
        if not did or not owner:
            return None
        owned = []
        for item in by_id.get(did, []):
            _p, item_order, item_phase, item_fields, _text = item
            if item_phase != "DESIGN" or item_fields.get("DESIGN_DOC_ID") != want_lock or item_order >= before_order:
                continue
            addrs = split_addresses(item_fields.get("FROM"))
            if len(addrs) == 1 and addr_is(addrs[0], owner, "planner"):
                owned.append(item)
        return sorted(owned, key=lambda item: item[1])[-1] if owned else None

    lock_routes, lock_lifecycle_errors = _lock_lifecycle(path, phases, candidates)
    for lock_lifecycle_error in lock_lifecycle_errors:
        result.add_finding(lock_lifecycle_error)

    for receiving_seat_error in h27_receiving_seat_errors(path, candidates):
        result.add_finding(receiving_seat_error)

    for commission_error in _commission(path, phases, candidates):
        result.add_finding(commission_error)

    # Design-review lineage gate. A pair-Planner PLAN that locks a
    # design-doc-backed design must parent to an approving Implementer
    # DESIGN-REVIEW relay, and that review must parent to the DESIGN relay that
    # introduced the matching DESIGN_DOC_ID.
    for f, order, phase, fields, text in candidates:
        if phase != "PLAN":
            continue
        if _design_gate(path, f, fields, text, phases, result):
            continue
        if (
            any(h27_occurrences(text, key) for key in ("PLAN_SOURCE_REPO", "PLAN_SOURCE_COMMIT"))
            and fields.get("DESIGN_LOCK_ID")
            and (
                "/" in fields["DESIGN_LOCK_ID"].split(" @ ", 1)[0]
                or fields["DESIGN_LOCK_ID"].split(" @ ", 1)[0].endswith(".md")
            )
        ):
            continue
        if lock_routes.get(f, "pair") != "pair": continue
        if not fields.get("DESIGN_LOCK_ID"):
            continue
        from_addrs = split_addresses(fields.get("FROM"))
        if len(from_addrs) != 1:
            continue
        from_addr = from_addrs[0]
        plan_owner = from_owner(from_addr)
        if canonical_role(from_role(from_addr)) != "planner" or not plan_owner or plan_owner in SPECIAL_ADDRESS_VALUES:
            continue
        lock_id = fields.get("DESIGN_LOCK_ID", "")
        kind = fields.get("DESIGN_RECORD_KIND")

        if design_record_kind_error_context(fields, plan_owner, phases):
            result.error(f"{f.relative_to(path)}: DESIGN_LOCK_ID present with design-review context but DESIGN_RECORD_KIND is missing", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=())
            continue

        if kind == "direct-override":
            if not is_direct_design_override(fields):
                result.error(f"{f.relative_to(path)}: pair Planner cannot use DESIGN_RECORD_KIND: direct-override; only operator/orchestrator/orchestrator-planner authority may override design review", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=())
            continue

        same_owner_designs = design_doc_items_for_owner(phases, plan_owner)
        if kind == "audit-record":
            if same_owner_designs:
                result.error(f"{f.relative_to(path)}: DESIGN_RECORD_KIND: audit-record conflicts with observable same-owner DESIGN relay carrying DESIGN_DOC_ID", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=())
            continue

        if not design_review_gate_applies(fields, plan_owner, phases):
            continue

        if kind not in {"design-doc", None}:
            # Enum errors are handled in lint_file; avoid duplicate/confusing lineage errors.
            continue
        if kind is None:
            # Missing-kind with context was handled above; isolated legacy path is exempt.
            continue

        design_candidates = [item for item in same_owner_designs if item[3].get("DESIGN_DOC_ID") == lock_id]
        earlier_designs = [item for item in design_candidates if item[1] < order]
        if not earlier_designs:
            result.error(f"{f.relative_to(path)}: DESIGN_LOCK_ID {lock_id!r} has no earlier same-owner DESIGN relay carrying matching DESIGN_DOC_ID", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=())
            continue
        design_item = sorted(earlier_designs, key=lambda item: item[1])[-1]
        dp, do, dph, dfields, dtext = design_item

        if not fields.get("PARENT_DISPATCH_ID"):
            result.error(f"{f.relative_to(path)}: design-doc PLAN requires PARENT_DISPATCH_ID to the approving DESIGN-REVIEW relay", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path),))
            continue
        review_item = owner_review_by_id(fields.get("PARENT_DISPATCH_ID"), plan_owner, lock_id, order)
        if review_item is None:
            result.error(f"{f.relative_to(path)}: design-doc PLAN parent {fields.get('PARENT_DISPATCH_ID')!r} does not resolve to a relay in this lineage", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path),))
            continue
        rp, ro, rph, rfields, rtext = review_item
        if ro >= order:
            result.error(f"{f.relative_to(path)}: approving DESIGN-REVIEW parent is not earlier than the PLAN relay", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path),))
        if rph != "DESIGN-REVIEW":
            result.error(f"{f.relative_to(path)}: design-doc PLAN parent must be a DESIGN-REVIEW relay", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path),))
        rfrom = split_addresses(rfields.get("FROM"))
        if len(rfrom) != 1 or not addr_is(rfrom[0], plan_owner, "implementer"):
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent must be FROM {plan_owner}.implementer", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path),))
        if rfields.get("DESIGN_REVIEW_VERDICT") != "approve":
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path),))
        if rfields.get("DESIGN_DOC_ID") != lock_id:
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent DESIGN_DOC_ID must match PLAN DESIGN_LOCK_ID {lock_id!r}", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path),))
        review_parent = owner_design_by_id(rfields.get("PARENT_DISPATCH_ID"), plan_owner, lock_id, ro)
        if review_parent is None:
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent lacks a resolvable DESIGN parent", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path),))
            continue
        vdp, vdo, vdph, vdfields, vdtext = review_parent
        if vdo >= ro:
            result.error(f"{f.relative_to(path)}: DESIGN parent is not earlier than the DESIGN-REVIEW relay", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path), vdp.relative_to(path),))
        if vdph != "DESIGN":
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent must point to a DESIGN relay", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path), vdp.relative_to(path),))
        vfrom = split_addresses(vdfields.get("FROM"))
        if len(vfrom) != 1 or not addr_is(vfrom[0], plan_owner, "planner"):
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW must review the pair Planner's DESIGN relay", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path), vdp.relative_to(path),))
        if vdfields.get("DESIGN_DOC_ID") != lock_id:
            result.error(f"{f.relative_to(path)}: DESIGN parent DESIGN_DOC_ID must match PLAN DESIGN_LOCK_ID {lock_id!r}", check_id='lineage.design-review', subject=str(f.relative_to(path)), operands=(dp.relative_to(path), rp.relative_to(path), vdp.relative_to(path),))

    for f, order, phase, fields, text in candidates:
        clean = sanitized_text(text)
        if not own_line_dispatch_present(text):
            continue
        from_addrs = split_addresses(fields.get("FROM"))
        from_addr = from_addrs[0] if len(from_addrs) == 1 else ""
        from_role_val = from_role(from_addr)
        to_addrs = split_addresses(fields.get("TO"))
        dispatch_owner = address_owner(to_addrs[0]) if len(to_addrs) == 1 else None

        # Direct operator/orchestrator/orchestrator-planner grants are explicit
        # override paths and do not belong to the pair-lineage walk. Pair
        # Planner grants are different: the privileged action is inert without
        # its approving PLAN-REVIEW parent. The parent edge is
        # mandatory for that path because the observed failures were edge-less.
        if from_role_val in LINEAGE_DIRECT_FROM_ROLES:
            continue
        if canonical_role(from_role_val) != "planner":
            continue
        if not dispatch_owner or dispatch_owner in SPECIAL_ADDRESS_VALUES:
            continue
        if from_owner(from_addr) != dispatch_owner:
            result.error(f"{f.relative_to(path)}: pair-Planner DISPATCH IMPL FROM owner does not match TO implementer owner", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
            continue
        if not fields.get("PARENT_DISPATCH_ID"):
            result.error(f"{f.relative_to(path)}: pair-Planner DISPATCH IMPL requires PARENT_DISPATCH_ID to an approving PLAN-REVIEW relay; absence is not a delegated-dispatch escape hatch", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
            continue

        did = fields.get("PARENT_DISPATCH_ID")
        review_item, holders, extra_qualifying = resolve_parent(
            by_id,
            did,
            order,
            lambda it: it[2] == "PLAN-REVIEW"
            and (lambda a: len(a) == 1 and addr_is(a[0], dispatch_owner, "implementer"))(
                split_addresses(it[3].get("FROM"))
            ),
        )
        if review_item is None:
            if holders:
                result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent {did!r} is held by {len(holders)} relays "
                    f"({', '.join(h[0].name for h in holders)}); none is an earlier PLAN-REVIEW relay "
                    f"from {dispatch_owner}.implementer", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
            else:
                result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent {did!r} does not resolve to a relay in this lineage", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
            continue
        if extra_qualifying:
            result.warn(f"{f.relative_to(path)}: {len(extra_qualifying)+1} relays under {did!r} qualify as the "
                f"PLAN-REVIEW parent; selected latest {review_item[0].name}; candidates: "
                f"{', '.join(h[0].name for h in extra_qualifying)}", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
        rp, ro, rph, rfields, rtext = review_item
        if ro >= order:
            result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent {fields.get('PARENT_DISPATCH_ID')!r} is not earlier than the dispatch relay", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path),))
        if rph != "PLAN-REVIEW" or not plan_review_approved(rtext):
            result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent must be an earlier PLAN-REVIEW relay with verdict approve", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path),))
        rfrom = split_addresses(rfields.get("FROM"))
        if len(rfrom) != 1 or not addr_is(rfrom[0], dispatch_owner, "implementer"):
            result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent must be FROM {dispatch_owner}.implementer", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path),))
        plan_did = rfields.get("PARENT_DISPATCH_ID")
        plan_item, holders, extra_qualifying = resolve_parent(
            by_id,
            plan_did,
            ro,
            lambda it: it[2] == "PLAN"
            and (lambda a: len(a) == 1 and addr_is(a[0], dispatch_owner, "planner"))(
                split_addresses(it[3].get("FROM"))
            ),
        )
        if plan_item is None:
            if holders:
                result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent {plan_did!r} is held by {len(holders)} relays "
                    f"({', '.join(h[0].name for h in holders)}); none is an earlier PLAN relay "
                    f"from {dispatch_owner}.planner", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
            else:
                result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent lacks a resolvable pair-Planner PLAN parent", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
            continue
        if extra_qualifying:
            result.warn(f"{f.relative_to(path)}: {len(extra_qualifying)+1} relays under {plan_did!r} qualify as the "
                f"PLAN parent; selected latest {plan_item[0].name}; candidates: "
                f"{', '.join(h[0].name for h in extra_qualifying)}", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=())
        pp, po, pph, pfields, ptext = plan_item
        if po >= ro:
            result.error(f"{f.relative_to(path)}: pair-Planner PLAN parent is not earlier than the PLAN-REVIEW relay", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path), pp.relative_to(path),))
        if pph != "PLAN":
            result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent must point to a PLAN relay", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path), pp.relative_to(path),))
        pfrom = split_addresses(pfields.get("FROM"))
        if len(pfrom) != 1 or not addr_is(pfrom[0], dispatch_owner, "planner"):
            result.error(f"{f.relative_to(path)}: PLAN-REVIEW must review the pair Planner's PLAN, not a CC'd orchestrator dispatch", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path), pp.relative_to(path),))
        if not any(addr_is(a, dispatch_owner, "implementer") for a in split_addresses(pfields.get("TO"))):
            result.error(f"{f.relative_to(path)}: pair-Planner PLAN must address the Implementer in TO for review", check_id='lineage.dispatch', subject=str(f.relative_to(path)), operands=(rp.relative_to(path), pp.relative_to(path),))

    # Non-addressee action trap: if an IMPL report claims substantive
    # implementation actions, it must point back to the dispatch that authorized
    # those actions. Missing parent is an error, not an opt-out; otherwise a
    # sibling Implementer can act on a CC/cross-read dispatch and report clean.
    # Merge-claim visibility is handled by the separate MERGE-GATE lineage below.
    for f, order, phase, fields, text in candidates:
        if phase != "IMPL":
            continue
        if not implementation_work_claimed(text, fields):
            continue
        if from_is_direct_authority(fields) and own_line_dispatch_present(text):
            # KR-8 Class A (D26): a relay issued by a direct-authority seat
            # (operator/orchestrator/orchestrator-planner) that itself carries
            # the live own-line token IS the dispatch, not a report owing a
            # parent edge. Seat-scoped deliberately: any other FROM -- an
            # implementer embedding a live token to dodge the trap -- still
            # falls through and is caught below.
            continue
        if not fields.get("PARENT_DISPATCH_ID"):
            result.error(f"{f.relative_to(path)}: IMPL report with substantive actions requires PARENT_DISPATCH_ID to the addressed DISPATCH IMPL relay", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=())
            continue
        actor_from = split_addresses(fields.get("FROM"))
        did = fields.get("PARENT_DISPATCH_ID")
        parent, holders, extra_qualifying = resolve_parent(
            by_id,
            did,
            order,
            lambda it: impl_dispatch_parent_qualifies(actor_from, it),
        )
        if parent is None:
            if holders:
                actor = actor_from[0] if len(actor_from) == 1 else fields.get("FROM", "")
                result.error(f"{f.relative_to(path)}: IMPL report parent {did!r} is held by {len(holders)} relays "
                    f"({', '.join(h[0].name for h in holders)}); none is an earlier DISPATCH IMPL relay "
                    f"addressed to {actor}", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=())
            else:
                result.error(f"{f.relative_to(path)}: IMPL report parent {did!r} does not resolve to a relay in this lineage", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=())
            continue
        if len(holders) == 1 and impl_dispatch_parent_qualifies(actor_from, parent) and parent[1] >= order:
            result.error(f"{f.relative_to(path)}: IMPL report parent {did!r} is held by 1 relays "
                f"({parent[0].name}); none is an earlier DISPATCH IMPL relay addressed to {actor_from[0]}", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=(parent[0].relative_to(path),))
            continue
        if extra_qualifying:
            result.warn(f"{f.relative_to(path)}: {len(extra_qualifying)+1} relays under {did!r} qualify as the "
                f"DISPATCH IMPL parent; selected latest {parent[0].name}; candidates: "
                f"{', '.join(h[0].name for h in extra_qualifying)}", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=(parent[0].relative_to(path),))
        pp, po, pph, pfields, ptext = parent
        if not own_line_dispatch_present(ptext):
            result.error(f"{f.relative_to(path)}: IMPL report parent must be a DISPATCH IMPL relay", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=(parent[0].relative_to(path),))
            continue
        parent_to = split_addresses(pfields.get("TO"))
        if len(actor_from) == 1 and len(parent_to) == 1 and not (
            address_role(actor_from[0]) is not None
            and address_role(parent_to[0]) is not None
            and address_owner(actor_from[0]) == address_owner(parent_to[0])
            and canonical_role(address_role(actor_from[0])) == canonical_role(address_role(parent_to[0]))
        ):
            result.error(f"{f.relative_to(path)}: IMPL report FROM {actor_from[0]!r} is not the addressee of the parent DISPATCH IMPL relay", check_id='lineage.impl-report-parent', subject=str(f.relative_to(path)), operands=(parent[0].relative_to(path),))

    # Merge-claim visibility: report claiming a merge commit needs an earlier merge authorization.
    for f, order, phase, fields, text in candidates:
        if merge_claimed(text, fields):
            claim_dispatch_id = fields.get("DISPATCH_ID")
            prior_merge_auth = any(
                o < order
                and claim_dispatch_id
                and fl.get("DISPATCH_ID") == claim_dispatch_id
                and merge_authorized(tx, fl)
                for p, o, ph, fl, tx in phases
            )
            if not prior_merge_auth:
                result.error(f"{f.relative_to(path)}: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID", check_id='lineage.merge-claim', subject=str(f.relative_to(path)), operands=())

    # Reconciliation-layer honest-false-row support: when a lineage declares
    # ROW_TRUTH_CHECK, an OUT->IN flip for the same path is structurally dirty
    # unless a future release adds an explicit, evidence-backed override.
    row_state: Dict[Tuple[str, str], Tuple[str, Path]] = {}
    for older_path, older_order, _, older_fields, older_text in phases:
        if older_order >= candidates[0][1] or older_fields.get("ROW_TRUTH_CHECK", "").lower() not in {"yes", "true", "required"}:
            continue
        for header in ("SCOPE_DIFF", "FOLD_SCOPE"):
            rows, row_errors = parse_marked_rows(older_text, header)
            if not row_errors:
                for row_path, value in rows:
                    row_state[(older_fields.get("DISPATCH_ID", ""), row_path)] = (value, older_path)
    for f, order, phase, fields, text in candidates:
        if fields.get("ROW_TRUTH_CHECK", "").lower() not in {"yes", "true", "required"}:
            continue
        did = fields.get("DISPATCH_ID", "")
        for header in ("SCOPE_DIFF", "FOLD_SCOPE"):
            rows, row_errors = parse_marked_rows(text, header)
            if row_errors:
                continue
            for row_path, val in rows:
                key = (did, row_path)
                if key in row_state:
                    prior_val, prior_file = row_state[key]
                    if prior_val == "out" and val == "in":
                        result.error(f"{f.relative_to(path)}: {header} flips {row_path!r} from OUT in {prior_file.relative_to(path)} to IN without a reconciliation evidence override", check_id='lineage.row-truth', subject=str(f.relative_to(path)), operands=(prior_file.relative_to(path),))
                row_state[key] = (val, f)

    # Orchestrator-review visibility gate. Every orchestrator
    # authority-bearing relay in the broad SET must make the reviewer seat
    # visible by address, unless the run carries an operator-authored waiver.
    for err in _review_visibility(path, phases, candidates):
        result.add_finding(err)



def _external(subject, gate, cause):
    return Finding('external.could-not-evaluate', 'error', subject, (),
                   {'gate': gate, 'cause': cause}, f'{gate} could not be evaluated: {cause}')


def _design_gate(path, f, fields, text, phases, result):
    temporary = LintResult()
    budget = rules._XROOT_BUDGET.get()
    start = len(budget[1]) if budget else 0
    engaged = xroot_design_gate(path, f, fields, text, phases, temporary)
    if budget and len(budget[1]) > start:
        result.add_finding(_external(str(f.relative_to(path)), 'cross-root-design', budget[1][-1]))
    else:
        for finding in temporary.findings:
            result.add_finding(finding)
    return engaged


def _artifact_findings(root, subject, text):
    path = root / subject
    result = []
    for dkey, akey, sub in A5_LOCK_PAIRS:
        declaration = a5_eligible_declaration(path, text, dkey, akey)
        if declaration is None:
            continue
        _, stem, declared = declaration
        rel = f"{sub}/{stem}.md"
        probes = ((root / rel, False), (Path(rel), True), (root.parent.parent / rel, False))
        candidates = [(p, cwd) for p, cwd in probes if p.exists()]
        if not candidates:
            result.append(_external(subject, dkey, 'artifact unavailable'))
        for candidate, cwd in candidates:
            try:
                if not candidate.is_file():
                    raise OSError('not a regular file')
                data = candidate.read_bytes()
            except OSError:
                result.append(_external(subject, dkey, 'artifact unreadable'))
                continue
            actual = hashlib.sha256(data).hexdigest()
            if actual != declared:
                if cwd:
                    display = './' + str(candidate)
                else:
                    try:
                        display = str(candidate.relative_to(root))
                    except ValueError:
                        display = rules.os.path.relpath(candidate)
                message = f"{dkey}: {display} digest {actual} does not match the declared {declared}"
                result.append(Finding('lineage.governing-digest', 'error', subject, (), {}, f'{subject}: {message}'))
    return result


def judge_candidate(corpus: Corpus, subject: str, envelope: Envelope, body: bytes, *, artifact_root: Path, deadline: float) -> tuple[Finding, ...]:
    root = Path(artifact_root)
    text = body.decode('utf-8')
    phases = []
    for seq, path, raw in corpus.entries:
        older_text = raw.decode('utf-8')
        fields = header_fields(older_text)
        phases.append((root / path, (0, seq, path), fields.get('PHASE', ''), fields, older_text))
    seq = max((row[0] for row in corpus.entries), default=0) + 1
    fields = header_fields(text)
    candidate = (root / subject, (0, seq, subject), fields.get('PHASE', ''), fields, text)
    phases.append(candidate)
    result = LintResult()
    per_file = lint_file(root / subject, freshness=False, _text=text, _raw=body, artifact_root=root)
    for severity, messages in (('error', per_file.errors), ('warn', per_file.warnings)):
        for message in messages:
            result.add_finding(Finding('file.lint', severity, subject, (), {}, f'{subject}: {message}'))
    token = rules._XROOT_BUDGET.set((deadline, []))
    try:
        _candidate_lineage(root, phases, [candidate], result)
        source_engaged = any(h27_occurrences(text, key) for key in ('PLAN_SOURCE_REPO', 'PLAN_SOURCE_COMMIT'))
        for key in ('DESIGN_LOCK_ID', 'PLAN_LOCK_ID'):
            value = fields.get(key, '')
            bare = value.split(' @ ', 1)[0]
            if source_engaged:
                gate_fields = dict(fields)
                gate_fields.pop('PLAN_LOCK_ID' if key == 'DESIGN_LOCK_ID' else 'DESIGN_LOCK_ID', None)
                temporary = LintResult()
                failures = rules._XROOT_BUDGET.get()[1]
                count = len(failures)
                xroot_plan_gate(root, root / subject, gate_fields, text, temporary)
                if len(failures) > count:
                    result.add_finding(_external(subject, 'cross-root-' + key, failures[-1]))
                else:
                    for finding in temporary.findings:
                        result.add_finding(finding)
                continue
            if bare and ('/' in bare or bare.endswith('.md')):
                probes = (Path(bare),) if Path(bare).is_absolute() else (root / bare, Path(bare), root.parent.parent / bare)
                if not any(p.exists() for p in probes):
                    result.add_finding(Finding('lineage.lock-path', 'error', subject, (), {}, f'{subject}: {key} references missing file {bare}'))
        for finding in _artifact_findings(root, subject, text):
            result.add_finding(finding)
    finally:
        rules._XROOT_BUDGET.reset(token)
    targets = split_addresses(fields.get('TO'))
    if (fields.get('PHASE') == 'MERGE-GATE' and from_is_direct_authority(fields)
            and len(targets) == 1 and canonical_role(address_role(targets[0])) == 'implementer'
            and not merge_authorized(text, fields) and not merge_authorization_verdict_lines(text)):
        result.add_finding(Finding('authority.grant-shape-absent', 'warn', subject, (), {},
                                  'MERGE-GATE addressed to an Implementer carries no authorization shape and no verdict; it grants nothing'))
    unique = {}
    for finding in result.findings:
        identity = (finding.id, finding.lint_severity, finding.subject, finding.operands,
                    json.dumps(finding.params, sort_keys=True), finding.message)
        unique[identity] = finding
    return tuple(sorted(unique.values(), key=lambda f: (f.id, f.operands, f.message)))
