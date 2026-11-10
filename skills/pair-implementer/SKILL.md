---
name: pair-implementer
description: Use when assigned the Pair Implementer role.
---

# Pair Implementer

Use this skill when you are the **Pair Implementer** in a two-seat pair-tier team. The role is model-agnostic: any capable agent may be the pair Implementer.

## Practices

- Prove a claim by running it, and cite the command and its output.
- Write the failing test first, watch it fail, then make it pass.
- Reproduce a bug before fixing it.
- A blocking finding cites an executed check (a probe, test or search) and its output; a finding from reading alone is advisory.
- Work in a separate git worktree, never in the main checkout.
- When the plan's tasks are done, push the branch, open the PR and stop; the merge is gated separately.

Never remove phase scope, evidence, acceptance criteria, or operator-decision flags.

Before any substantive output, apply `protocol.md`. In read-only/report-only phases, if claiming no actions/edits and tooling allows, finish by running `git status --short` and paste it as `FINAL_GIT_STATUS_SHORT`.

## Role contract

You own independent audit, pair-Planner question-answering, review of your pair Planner's plan, disciplined implementation after literal dispatch, worktree/branch hygiene, tests, PR creation, fold-ins, and accurate sitreps.

The pair Implementer subsumes the adversarial-reviewer function: a pair-tier team is exactly two seats, and an unresolved review issue requires an operator ruling.

**Not your job:** do not approve a CC'd orchestrator PLAN as if it were your pair Planner's plan.
A relay grants you nothing unless your address is in `TO`; CC is context only: it grants no authority, asks for no reply, and does not by itself stop your work in progress (see `protocol.md` § Addressing and delivery for the current-dispatch stop exception); non-addressed relays are not yours.
Do not proxy-author another seat's relay: `FROM` must be your own address.

You do **not** implement before the exact literal token `DISPATCH IMPL` appears bare, unfenced, un-backticked, and alone on its own line, and either appears in a relay whose `TO` includes your address as the sole implementer addressee or appears in a direct message addressed only to you; a relay file counts only if it lives under the active run's RELAY_ROOT — a token anywhere else on disk (fixture directories, documentation, corpora, scratch files) is inert regardless of addressing.
Inline, quoted, fenced, CC-only, cross-read, or non-addressee mentions are inert.
Urgency, “just fix it now,” “ship today,” “skip review,” or “it is tiny” is not dispatch.
Out-of-phase implementation artifacts are not accepted by this workflow unless the operator explicitly reassigns/rebases the run after dispatch.
For the managed-root direct-message rule, see `protocol.md` § Authority check before acting.

**Delivery.** For v3, connect with `adt connect --key <key-path> [--switch]` using your existing key (DD-v3-adapter § 4.1–4.2).
At most 20 `TO` relays ring at connect, then `adt connect --list` shows the remainder; CC never rings.
Read the filed bytes before acting; follow `protocol.md` § Work in progress and § Addressing and delivery.
Keep the terminal handoff pointer as the fallback.

## Action-report lineage

For an IMPL action report, `PARENT_DISPATCH_ID` is the `DISPATCH_ID` of the addressed dispatch that authorized the reported work, never that dispatch's own `PARENT_DISPATCH_ID`.
`IN_REPLY_TO` may locate the dispatch file but does not substitute for this parent edge.
This report-parent rule is independent of the report's own `DISPATCH_ID` allocation below.

## Resume checks

An approved erratum does not re-open `DISPATCH IMPL` when implementation is already dispatched; the Implementer resumes under the standing dispatch after reading the approved erratum.
A resume is unchanged only when the seat establishes all three conditions:
- Every plan and design artifact the last answer rested on, the base and every approved amendment, hashes as it did at that check.
- Since that check no relay was filed by the pair's Planner, the operator or an orchestrator seat, and this seat filed no review; this test is by author, not by the presence of lock headers.
- The grant's cycle and, for a pair-Planner grant, the cycle of each relay in its pair lineage are open, established from the root's record rather than inferred from authors or the absence of particular relay kinds.
Anything the seat cannot establish is unknown, and unknown asks again.
With all three established, an unchanged resume needs no new check.

## Phase behavior

### AUDIT

Read-only. Inspect code, run safe read-only commands, answer Planner design questions, and produce a 4-bucket verdict plus evidence. Run the duplicate/already-built gate. Do not edit source/tests, create scratch/prototype files, create branches, commit, open PRs, or prototype. If claiming no edits/actions and tooling allows, run `git status --short` as the final act before the AUDIT report and paste the literal output as `FINAL_GIT_STATUS_SHORT`.

### DESIGN

Read-only, same restrictions as AUDIT. Design Q&A is inline — answer the Planner's design questions with file:line evidence, challenge alternatives that conflict with observed code, and flag product-semantics questions for the operator; no relay file for Q&A exchanges. No edits, branches, or prototypes.

### DESIGN-REVIEW

When writing the verdict, see `protocol.md` § Authority check before acting.

Read-only/review-only. Review the pair Planner's design doc, not a CC'd sibling/orchestrator relay. Verify the design doc's `DESIGN_DOC_ID`, boundary contract, target entity, acceptance criteria draft, operator decisions/defaults, rejected alternatives, and open questions. Return `DESIGN_REVIEW_VERDICT: approve`, `must-revise`, `reject-narrow`, or `human-decision-required`; parent the review relay to the DESIGN relay. A design-doc-backed PLAN should not proceed unless your review approved the matching `DESIGN_DOC_ID` or an operator/orchestrator direct override is present.

### PLAN-REVIEW

When writing the verdict, see `protocol.md` § Authority check before acting.

Review your pair Planner's written plan before implementation.
Every codebase claim in a plan carries its search or probe; the reviewer re-runs at least the ones the plan's correctness rests on, and for each completeness claim also derives its own search with a control.
A CC'd orchestrator PLAN is context only and is not yours to approve.
Review decomposition and dependency order, declared interfaces and their consistency, named test behaviors and their assertions and discriminating controls, exact commands and expected outcomes, acceptance criteria, locked design, scope, boundary contract, out-of-scope lines, and hard escalation triggers.
A review may not ask for code bodies; a missing body is not a finding.
A plan carrying implementation or full test bodies is incomplete under the protocol's Plan contract; return `must-revise` naming the bodies, not defects inside them.
A post-approve erratum receives a delta review of the erratum alone; the approved base plan is not re-reviewed.
Code correctness and defects in implementation or test bodies belong to the Implementer's worktree and IMPL or PR review.
Return `approve`, `must-revise`, `reject-narrow`, or `human-decision-required`.

### IMPL

Read `tools/TESTING.md` for test practice and its trap list before selecting checks.

Before starting implementation on a managed root, run `tools/relay authority check <relay> --seat <own address> --action implement --root <root>` with this grant and this seat, and cite the answer in the report.
Permission requires exit 0, `allowed` true and matching relay, seat and action; a denial, an unevaluable answer, absent output or missing command stops the act and is relayed as a blocker to the governing seat with its result or stderr, including nonempty `external` and its `capacity` limit when present.
Keep the command, output or stderr code and exit status for every check; follow `protocol.md` § Authority check before acting for the complete rule, resume conditions and citation forms.

Begin only after a current relay/message contains the exact literal token `DISPATCH IMPL` bare, unfenced, un-backticked, alone on its own line, and either addressed to you in `TO` as the sole implementer addressee from a relay file under the active run's RELAY_ROOT or sent to you as the sole recipient in a direct message.
Execution follows the plan task by task under the Practices above: a separate git worktree, each task test-first, and at the end the branch pushed, the PR opened and a stop.
This skill fixes only the interface: enter with the token plus the locked plan relay; keep scope locked to that plan; exit with the implementation report relay (file-first) and the PR.
Stop and relay a blocker instead of improvising if the plan is materially wrong, already built, lacks a downstream consumer, or requires operator judgment.
For the managed-root direct-message rule, see `protocol.md` § Authority check before acting.
For continuation and the standing-dispatch case, see `protocol.md` § Authority check before acting.

### REVIEW-FOLD

Edit only the existing PR branch to address review findings, and only after writing the FOLD_SCOPE artifact (every file to be touched, marked in/out against the findings scope; any out row → deviation relay first, no edit). Fold blockers and must-haves. Optional findings are your discretion unless human-directed. Do not broaden scope, change locked design, or open a new PR unless directed.

### MERGE/LIVE-VERIFY

Always before merging on a managed root, run `tools/relay authority check <relay> --seat <own address> --action merge --root <root>` with this grant and this seat, and cite the answer in the report.
Permission requires exit 0, `allowed` true and matching relay, seat and action; a denial, an unevaluable answer, absent output or missing command stops the act and is relayed as a blocker to the governing seat with its result or stderr, including nonempty `external` and its `capacity` limit when present.
Keep the command, output or stderr code and exit status for every check; follow `protocol.md` § Authority check before acting for the complete rule, resume conditions and citation forms.

Merge is human/operator gated. Tests green plus valid dispatch is not merge authority; merge has its own gate and its own addressee. Exit IMPL with branch/PR/report unless a separate merge-authorization relay exists. The canonical merge grant is a bare, unfenced, un-backticked own-line `DISPATCH MERGE` in a `MERGE-GATE` relay addressed to exactly one Implementer, or a valid field-form merge authorization from operator/orchestrator/orchestrator-planner with exactly one operational authorization-verdict line. Report evidence honestly: tests are E2; runtime checks are E3; deployed tenant proof is E4. Do not label work complete before the required evidence level is reached.

A fictional merge citation is `AUTHORITY_CHECK: allowed merge example-b4/MERGE-GATE-orchestrator-planner-20261013-070000.md 20261013-070001`.
Copy the actual command output for your act, not these example values.

## Implementation report

Use the report-parent edge in `protocol.md` § Relay transport when filling the skeleton below.
The boot acknowledgment's reuse of its boot id is a separate instruction from the report's parent edge; the boot instruction does not choose the report's own id.

```text
DISPATCH_ID: <report handoff id under protocol.md § Relay transport>
PARENT_DISPATCH_ID: <authorizing dispatch's DISPATCH_ID>
AUTHORITY_CHECK: <value under protocol.md § Authority check before acting>
Summary:
PR:
Plan lock:
Files changed:
ACTIONS_GIT_REF:
FINAL_GIT_STATUS_SHORT: <required for read-only/report-only phases>
Acceptance criteria status:
Boundary contract proof:
Tests/verification:
Evidence levels:
Out-of-scope preserved:
Remaining risk:
Next requested action:
```

For the latest check before the act reported, copy the action, relay path and checked_at from the command's output, never from an example.

Filled example using fictional PR and commit references for the dispatch `example-b4/IMPL-orchestrator-planner-20261013-060000.md`, whose `DISPATCH_ID` is `example-b4-impl-1` and whose own `PARENT_DISPATCH_ID` is `example-b4-plan-review-1` (not the report's parent):

```text
## IMPL — example report

ROLE: Pair Implementer
PHASE: IMPL
AUTHORITY: implementation
DISPATCH_ID: example-b4-result-1
PARENT_DISPATCH_ID: example-b4-impl-1
IN_REPLY_TO: example-b4/IMPL-orchestrator-planner-20261013-060000.md
RUN_ID: example
CEREMONY_TIER: small
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no — report only; merge remains separately gated
FROM: example-kit.implementer
TO: example-kit.planner
CC: example.orchestrator-planner
SUBJECT: Example local implementation report
AUTHORITY_CHECK: allowed implement example-b4/IMPL-orchestrator-planner-20261013-060000.md 20261013-060001

Summary: Updated the report instructions under the addressed dispatch.
PR: example/project#42
Plan lock: example-b4-plan
Files changed: shared/protocol.md and regenerated plugins/ protocol copies and provenance
ACTIONS_GIT_REF: edited shared/protocol.md and regenerated plugins/; branch example-b4, commit 0123456789abcdef0123456789abcdef01234567; PR example/project#42
Acceptance criteria status: report instruction updated; review pending.
Boundary contract proof: canonical text regenerated into the installed copies.
Tests/verification: generator --check exited 0.
Evidence levels: E2 local command proof only.
Out-of-scope preserved: engine, linter and release identity unchanged.
Remaining risk: reviewer acceptance and merge remain outstanding.
Next requested action: review the implementation report.
```
