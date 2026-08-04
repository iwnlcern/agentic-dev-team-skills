#!/usr/bin/env python3
"""Run the relay-lint fixture matrix and compare expected exit codes.

This is a dev/release helper. It imports relay-lint directly to avoid shell
harness noise; it does not call any LLM, network, subprocess, or git command.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINT = ROOT / "tools" / "relay-lint.py"
FIXTURES = ROOT / "tools" / "relay-lint-fixtures"

EXPECTED = [
    ("file", "rolevocab/RV1a-pair-planner.md", 0),
    ("file", "rolevocab/RV1b-pair-implementer.md", 0),
    ("file", "rolevocab/RV2a-master-planner.md", 0),
    ("file", "rolevocab/RV2b-master-reviewer.md", 0),
    ("file", "rolevocab/RV3a-domain-planner.md", 0),
    ("file", "rolevocab/RV3b-domain-reviewer.md", 0),
    ("file", "rolevocab/RV4-invalid-role.md", 1),
    ("file", "rolevocab/RV5-planner-from-pair-planner.md", 0),
    ("file", "rolevocab/RV6-pair-implementer-from-implementer.md", 0),
    ("file", "rolevocab/RV7a-master-dispatch-failclosed.md", 1),
    ("file", "rolevocab/RV7b-domain-direct-override.md", 1),
    ("root", "rolevocab/RV5-new-vocab-chain", 0),
    ("root", "rolevocab/RV5d-newvocab-designdoc-chain", 0),
    ("root", "rolevocab/RV5dneg-designdoc-no-approve", 1),
    ("root", "rolevocab/RV6-mixed-vocab-chain", 0),
    ("root", "rolevocab/RV6i-mixed-dispatch-implreport", 0),
    ("root", "rolevocab/RV7a-master-dispatch-failclosed", 1),
    ("root", "rolevocab/RV7b-domain-direct-override", 1),
    ("root", "rolevocab/RV7c-master-merge-grant", 1),
    ("root", "rolevocab/RV7d-master-broadset-no-fire", 0),
    ("root", "rolevocab/RVn1-cross-owner-review", 1),
    ("root", "rolevocab/RVn2-wrong-to", 1),
    ("root", "rolevocab/RVn3-non-addressee-implreport", 1),
    ("file", "claude/A1-valid-audit.md", 0),
    ("file", "claude/A2-valid-downgrade.md", 0),
    ("file", "claude/B2-why-before-scan.md", 1),
    ("file", "claude/B3-yes-row-why.md", 1),
    ("file", "claude/B5-audit-no-final.md", 1),
    ("file", "claude/B6-audit-impl-authority.md", 1),
    ("file", "claude/B7-delegated-no-scopediff.md", 1),
    ("file", "claude/B8-deviation-dispatch.md", 1),
    ("file", "claude/B9-bad-enum.md", 1),
    ("file", "claude/C1-evasive-rows.md", 1),
    ("file", "claude/C2-enum-bypass.md", 1),
    ("file", "lint-test/bad1.md", 1),
    ("file", "lint-test/bad2.md", 1),
    ("file", "lint-test/bad3.md", 1),
    ("file", "lint-test/bad3b.md", 1),
    ("file", "lint-test/bad4.md", 1),
    ("file", "lint-test/good5.md", 0),
    ("root", "claude/L1", 0),
    ("root", "claude/L2", 0),
    ("root", "claude/L3", 1),
    ("file", "probes/N2-refusal-code-span.md", 0),
    ("file", "probes/N3-fenced-dispatch-inert.md", 0),
    ("file", "probes/N4-scan-result-mismatch.md", 1),
    ("file", "probes/N5-missing-scan-row.md", 1),
    ("file", "addressing/T1-valid-addressed-dispatch.md", 0),
    ("file", "addressing/T2-token-no-to.md", 1),
    ("file", "addressing/T3-token-to-planner.md", 1),
    ("file", "addressing/T4-token-two-implementers.md", 1),
    ("file", "addressing/T5-bad-address.md", 1),
    ("file", "addressing/T6-valid-audit-both-to.md", 0),
    ("file", "addressing/T7-valid-cc-context.md", 0),
    ("file", "addressing/T8-cc-trap-structurally-valid.md", 0),
    ("root", "addressing/G1-casefold-lineage", 0),
    ("root", "merge/M1-merge-claim-no-auth", 1),
    ("root", "merge/M2-merge-claim-with-auth", 0),
    ("root", "merge/M3-honest-not-merged", 0),
    ("root", "merge/M4-canonical-claim-no-auth", 1),
    ("root", "merge/M5-self-auth-forgery", 1),
    ("root", "merge/M7-continuation-prose-claim-no-auth", 1),
    ("root", "merge/M8-continuation-canonical-claim-no-auth", 1),
    ("root", "merge/BP5-blank-line-merge-evasion", 1),
    ("root", "merge/GA-flushleft-canonical-no-auth", 1),
    ("root", "merge/GB-tab-ambiguous-canonical", 1),
    ("root", "merge/GC-2blank-ambiguous-canonical", 1),
    ("root", "merge/GD-quoted-canonical-inert", 0),
    ("root", "merge/organic-m2-unauthorized-merge", 1),
    ("root", "merge-token/MT1-valid-token-grant", 0),
    ("root", "merge-token/MT2-backticked-token-inert", 1),
    ("file", "merge-token/MT3-self-granted-token.md", 1),
    ("file", "merge-token/MT4-multi-to-grant.md", 1),
    ("file", "merge-token/MT5-wrong-phase-token.md", 1),
    ("root", "merge-token/MT6-token-outside-root", 1),
    ("root", "merge-token/MT7-duplicate-authorization-decoy", 1),
    ("root", "merge-token/MT8-denied-only-no-auth", 1),
    ("root", "merge-token/MT9-cross-dispatch-runroot", 1),
    ("file", "content/E1-empty-final-git-status.md", 1),
    ("file", "content/E2-empty-actions-git-ref.md", 1),
    ("file", "content/E3-empty-scopediff-live-dispatch.md", 1),
    ("file", "content/E4-structured-unavailable.md", 0),
    ("file", "content/E5-clean-tree.md", 0),
    ("file", "content/E6-out-row-allin.md", 1),
    ("file", "content/E7-valid-scopediff-dispatch.md", 0),
    ("file", "content/E8-structured-none-scopediff.md", 1),
    ("file", "content/E9-unparseable-row.md", 1),
    ("file", "content/E10-bare-unavailable.md", 1),
    ("file", "content/E11-placeholder-field.md", 1),
    ("file", "content/E12-colon-rows-valid.md", 0),
    ("file", "content/E13-na-marker.md", 1),
    ("file", "content/E13-scopediff-detached-row.md", 1),
    ("file", "content/E14-scopediff-row-after-result.md", 1),
    ("file", "content/E15-dual-scopediff-decoy.md", 1),
    ("file", "content/E16-dual-scopediff-contiguous.md", 1),
    ("file", "fold/FD1-fold-edit-no-foldscope.md", 1),
    ("file", "fold/FD2-valid-fold-report.md", 0),
    ("file", "fold/FD3-out-row-with-edit.md", 1),
    ("file", "fold/FD4-empty-foldscope.md", 1),
    ("file", "fold/FD5-deviation-relay-out-rows-no-edit.md", 0),
    ("file", "fold/FD6-edit-then-waiver-shape.md", 1),
    ("file", "fold/FD7-fold-no-deviation-uncommitted.md", 1),
    ("file", "fold/FD8-scope-after-actions.md", 1),
    ("file", "fold/FD9-fold-no-wordlist-verbs.md", 1),
    ("file", "fold/FD10-detached-row-foldscope.md", 1),
    ("file", "fold/FD11-row-after-result-foldscope.md", 1),
    ("file", "fold/FD12-absence-prefixed-actions.md", 1),
    ("file", "fold/FD13-dual-foldscope-contiguous.md", 1),
    ("file", "identity/S4a-proxy-from.md", 1),
    ("root", "lineage/LI1-valid-parent-chain", 0),
    ("root", "lineage/LI2-cc-orchestrator-plan-trap", 1),
    ("root", "lineage/LI3-no-plan-review-parent", 1),
    ("root", "lineage/LI4-non-addressee-impl-report", 1),
    ("root", "lineage/LI5-edgeless-delegated-cc-trap", 1),
    ("root", "lineage/LI6-edgeless-no-approve", 1),
    ("root", "lineage/LI7-edgeless-non-addressee-report", 1),
    ("file", "rowtruth/RT1-valid-fold-evidence.md", 0),
    ("file", "rowtruth/RT2-missing-fold-evidence.md", 1),
    ("file", "rowtruth/RT3-valid-scope-evidence.md", 0),
    ("file", "rowtruth/RT4-missing-scope-evidence.md", 1),
    ("root", "rowtruth/RT5-two-relay-out-in-flip", 1),
    ("root", "p9/P9-blank-line-prose-after-block", 0),
    ("file", "p9/P9b-claim-after-scan-blank-line.md", 1),
    ("root", "design-review/DR1-valid-design-doc-chain", 0),
    ("root", "design-review/DR2-edge-less-no-review", 1),
    ("root", "design-review/DR3-direct-override", 0),
    ("root", "design-review/DR4-self-override", 1),
    ("root", "design-review/DR5-tiny-no-lock", 0),
    ("root", "design-review/DR6-genuine-audit-record", 0),
    ("root", "design-review/DR7a-audit-record-no-design-doc", 0),
    ("root", "design-review/DR7b-audit-record-design-doc-visible", 1),
    ("root", "design-review/DR8-review-not-from-implementer", 1),
    ("root", "design-review/DR9-verdict-not-approve", 1),
    ("root", "design-review/DR10-review-parent-not-design", 1),
    ("root", "design-review/DR11a-stale-review-parent", 1),
    ("root", "design-review/DR11b-locks-must-revise-v1", 1),
    ("root", "design-review/DR11c-positive-control-v2", 0),
    ("root", "design-review/DR13-omit-record-kind", 1),
    ("root", "design-review/DR14-legacy-lock-tolerated", 0),
    ("root", "design-review/DR15-verdict-human-decision", 1),
    ("root", "design-review/DR16-F-a-same-id-orch-design", 0),
    ("root", "design-review/DR17-F-relock-latest-before-review", 0),
    ("root", "design-review/DR18-F-thread-one-id-pass", 0),
    ("root", "design-review/DR19-F-thread-mustrevise", 1),
    ("root", "design-review/DR20-F-b-no-design-parent", 1),
    ("root", "design-review/DR21-F-b158-no-owner-review", 1),
    ("root", "design-review/DR22-F-c-cross-owner-design", 1),
    ("root", "orch-review/OR1-audit-cc", 0),
    ("root", "orch-review/OR2-design-cc", 0),
    ("root", "orch-review/OR3-plan-delegated-cc", 0),
    ("root", "orch-review/OR4-override-impl-cc", 0),
    ("root", "orch-review/OR5-merge-gate-cc", 0),
    ("root", "orch-review/OR6-review-fold-cc", 0),
    ("root", "orch-review/OR7-audit-no-reviewer", 1),
    ("root", "orch-review/OR8-design-no-reviewer", 1),
    ("root", "orch-review/OR9-plan-delegated-no-reviewer", 1),
    ("root", "orch-review/OR10-override-impl-no-reviewer", 1),
    ("root", "orch-review/OR11-merge-gate-no-reviewer", 1),
    ("root", "orch-review/OR12-review-fold-no-reviewer", 1),
    ("root", "orch-review/A1-no-reviewer-no-waiver", 1),
    ("root", "orch-review/A2-operator-waiver", 0),
    ("root", "orch-review/A3-self-waiver", 1),
    ("root", "orch-review/A4-solo-no-orchestrator-relay", 0),
    ("root", "orch-review/EX1-operator-authority", 0),
    ("root", "orch-review/EX2-pairplanner-impl", 0),
    ("root", "orch-review/EX3-reviewer-boot", 0),
    ("root", "orch-review/EX4-reconcile", 0),
    ("root", "orch-review/EX5-proceed-nodeleg", 0),
    ("root", "orch-review/EX6-boot-to-reviewer", 0),
]

EXPECTED_ERROR_SET = {
    "rolevocab/RV7a-master-dispatch-failclosed.md": [
        "authority semantics for FROM role 'master-planner' are unruled; a dispatch token from this seat is fail-closed pending the orchestrator ruling",
    ],
    "rolevocab/RV7b-domain-direct-override.md": [
        "authority semantics for FROM role 'domain-planner' are unruled; DESIGN_RECORD_KIND: direct-override from this seat is fail-closed pending the orchestrator ruling",
    ],
    "rolevocab/RV5dneg-designdoc-no-approve": [
        "03-plan.md: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve",
    ],
    "rolevocab/RV7a-master-dispatch-failclosed": [
        "RV7a-master-dispatch-failclosed.md: authority semantics for FROM role 'master-planner' are unruled; a dispatch token from this seat is fail-closed pending the orchestrator ruling",
    ],
    "rolevocab/RV7b-domain-direct-override": [
        "RV7b-domain-direct-override.md: authority semantics for FROM role 'domain-planner' are unruled; DESIGN_RECORD_KIND: direct-override from this seat is fail-closed pending the orchestrator ruling",
    ],
    "rolevocab/RV7c-master-merge-grant": [
        "02-impl-report.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "rolevocab/RVn1-cross-owner-review": [
        "03-dispatch.md: PLAN-REVIEW parent must be FROM qi.implementer",
    ],
    "rolevocab/RVn2-wrong-to": [
        "03-dispatch.md: DISPATCH IMPL requires TO to be exactly one implementer-role address",
    ],
    "rolevocab/RVn3-non-addressee-implreport": [
        "02-impl-report.md: IMPL report FROM 'zz.pair-implementer' is not the addressee of the parent DISPATCH IMPL relay",
    ],
    "design-review/DR15-verdict-human-decision": [
        "03-plan.md: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve",
    ],
    "design-review/DR19-F-thread-mustrevise": [
        "05-plan.md: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve",
    ],
    "design-review/DR20-F-b-no-design-parent": [
        "04-plan.md: DESIGN-REVIEW parent lacks a resolvable DESIGN parent",
    ],
    "design-review/DR21-F-b158-no-owner-review": [
        "03-plan.md: design-doc PLAN parent 'fb158-review' does not resolve to a relay in this lineage",
    ],
    "design-review/DR22-F-c-cross-owner-design": [
        "04-plan.md: DESIGN-REVIEW parent lacks a resolvable DESIGN parent",
    ],
    "merge/BP5-blank-line-merge-evasion": [
        "IMPL-report-20260610-103000.md: ACTIONS_GIT_REF has ambiguous indented continuation after a blank line at line 13; field blocks must be contiguous",
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/GA-flushleft-canonical-no-auth": [
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/GB-tab-ambiguous-canonical": [
        "IMPL-report-20260610-103000.md: ACTIONS_GIT_REF has ambiguous indented continuation after a blank line at line 13; field blocks must be contiguous",
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/GC-2blank-ambiguous-canonical": [
        "IMPL-report-20260610-103000.md: ACTIONS_GIT_REF has ambiguous indented continuation after a blank line at line 14; field blocks must be contiguous",
        "IMPL-report-20260610-103000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge/organic-m2-unauthorized-merge": [
        "IMPL-report-20260610-121000.md: missing required header field HUMAN_GATE_REQUIRED",
        "IMPL-report-20260610-121000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT2-backticked-token-inert": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT3-self-granted-token.md": [
        "DISPATCH MERGE FROM must be operator, orchestrator, or an orchestrator-planner-role address",
    ],
    "merge-token/MT4-multi-to-grant.md": [
        "DISPATCH MERGE requires exactly one TO addressee",
    ],
    "merge-token/MT5-wrong-phase-token.md": [
        "DISPATCH MERGE is valid only in PHASE: MERGE-GATE relay files",
    ],
    "merge-token/MT6-token-outside-root": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT7-duplicate-authorization-decoy": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
        "MERGE-GATE-20260610-130000.md: duplicate/conflicting merge authorization; the grant of record carries exactly one verdict line",
    ],
    "merge-token/MT8-denied-only-no-auth": [
        "IMPL-report-20260610-131000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "merge-token/MT9-cross-dispatch-runroot": [
        "d1/IMPL-report-20260611-121000.md: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID",
    ],
    "content/E3-empty-scopediff-live-dispatch.md": [
        "FINAL_GIT_STATUS_SHORT carries a placeholder, not content; placeholders are valid only in --templates mode",
        "delegated DISPATCH IMPL requires SCOPE_DIFF with >=1 parsed '<path> -> in|OUT' row; a vacuous or structured-none diff does not authorize dispatch",
    ],
    "content/E6-out-row-allin.md": [
        "SCOPE_DIFF_RESULT: all-in inconsistent with an OUT row",
    ],
    "content/E8-structured-none-scopediff.md": [
        "delegated DISPATCH IMPL requires SCOPE_DIFF with >=1 parsed '<path> -> in|OUT' row; a vacuous or structured-none diff does not authorize dispatch",
    ],
    "content/E9-unparseable-row.md": [
        "unparseable SCOPE_DIFF row: 'also touching the auth module a bit'",
    ],
    "content/E10-bare-unavailable.md": [
        "FINAL_GIT_STATUS_SHORT uses a reserved absence word without a reason; use 'unavailable — <reason>' / 'none — <reason>'",
    ],
    "content/E11-placeholder-field.md": [
        "FINAL_GIT_STATUS_SHORT carries a placeholder, not content; placeholders are valid only in --templates mode",
    ],
    "content/E13-na-marker.md": [
        "FINAL_GIT_STATUS_SHORT uses a reserved absence word without a reason; use 'unavailable — <reason>' / 'none — <reason>'",
    ],
    "content/E13-scopediff-detached-row.md": [
        "row-shaped line outside the SCOPE_DIFF block: '- src/billing/invoice.ts -> out'; rows must sit contiguously under their header",
    ],
    "content/E14-scopediff-row-after-result.md": [
        "row-shaped line outside the SCOPE_DIFF block: '- src/billing/invoice.ts -> out'; rows must sit contiguously under their header",
    ],
    "content/E15-dual-scopediff-decoy.md": [
        "duplicate SCOPE_DIFF block; the report of record carries exactly one",
        "duplicate SCOPE_DIFF_RESULT block; the report of record carries exactly one",
        "row-shaped line outside the SCOPE_DIFF block: '- src/billing/invoice.ts -> out'; rows must sit contiguously under their header",
    ],
    "content/E16-dual-scopediff-contiguous.md": [
        "duplicate SCOPE_DIFF block; the report of record carries exactly one",
        "duplicate SCOPE_DIFF_RESULT block; the report of record carries exactly one",
    ],
    "fold/FD1-fold-edit-no-foldscope.md": [
        "REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing",
        "REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT",
    ],
    "fold/FD3-out-row-with-edit.md": [
        "FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit",
    ],
    "fold/FD4-empty-foldscope.md": [
        "FOLD_SCOPE requires >=1 parsed '<path> -> in|out' row; an empty scope list does not license a fold edit",
    ],
    "fold/FD6-edit-then-waiver-shape.md": [
        "FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit",
    ],
    "fold/FD7-fold-no-deviation-uncommitted.md": [
        "FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit",
    ],
    "fold/FD8-scope-after-actions.md": [
        "FOLD_SCOPE appears after ACTIONS_GIT_REF; the scope artifact precedes the action claim (field-ordering)",
    ],
    "fold/FD9-fold-no-wordlist-verbs.md": [
        "REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing",
        "REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT",
    ],
    "fold/FD10-detached-row-foldscope.md": [
        "row-shaped line outside the FOLD_SCOPE block: '- src/format.js -> out'; rows must sit contiguously under their header",
    ],
    "fold/FD11-row-after-result-foldscope.md": [
        "row-shaped line outside the FOLD_SCOPE block: '- src/format.js -> out'; rows must sit contiguously under their header",
    ],
    "fold/FD12-absence-prefixed-actions.md": [
        "REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing",
        "REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT",
    ],
    "fold/FD13-dual-foldscope-contiguous.md": [
        "duplicate FOLD_SCOPE block; the report of record carries exactly one",
        "duplicate FOLD_SCOPE_RESULT block; the report of record carries exactly one",
    ],
    "lineage/LI5-edgeless-delegated-cc-trap": [
        "03-edgeless-dispatch.md: pair-Planner DISPATCH IMPL requires PARENT_DISPATCH_ID to an approving PLAN-REVIEW relay; absence is not a delegated-dispatch escape hatch",
    ],
    "lineage/LI6-edgeless-no-approve": [
        "01-edgeless-dispatch.md: pair-Planner DISPATCH IMPL requires PARENT_DISPATCH_ID to an approving PLAN-REVIEW relay; absence is not a delegated-dispatch escape hatch",
    ],
    "lineage/LI7-edgeless-non-addressee-report": [
        "01-impl-report.md: IMPL report with substantive actions requires PARENT_DISPATCH_ID to the addressed DISPATCH IMPL relay",
    ],
    "p9/P9b-claim-after-scan-blank-line.md": [
        "structurally detectable edit/commit/PR/migration claim lacks ACTIONS_GIT_REF",
    ],
}



def load_linter():
    spec = importlib.util.spec_from_file_location("relay_lint", LINT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {LINT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    lint = load_linter()
    failed = False
    for kind, rel, expected in EXPECTED:
        target = FIXTURES / rel
        if kind == "file":
            result = lint.lint_file(target)
        else:
            result = lint.lint_relay_root(target)
        observed = 0 if result.ok else 1
        expected_errors = EXPECTED_ERROR_SET.get(rel)
        if expected_errors is not None:
            observed_errors = sorted(result.errors)
            expected_sorted = sorted(expected_errors)
            ok = observed == expected and observed_errors == expected_sorted
        else:
            ok = observed == expected
        failed = failed or not ok
        label = f"--relay-root {rel}" if kind == "root" else rel
        print(f"{label}: expected={expected} observed={observed} {'PASS' if ok else 'FAIL'}")
        if expected_errors is not None:
            print(f"  expected_errors={len(expected_errors)} observed_errors={len(result.errors)}")
        if not ok:
            for err in result.errors:
                print(f"  ERROR {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
