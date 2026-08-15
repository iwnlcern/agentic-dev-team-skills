# Orchestrator Handoff Templates

The orchestrator-review visibility gate requires orchestrator-planner authority relays in the broad SET to CC `<run>.orchestrator-reviewer` unless the run has an operator-authored `ORCH_REVIEW_WAIVER`. This is visibility, not approval.

Use these relay-ready templates. Always write the routing relay; do not replace a non-PR routing relay with a review panel. Panels are for code/PR review or explicitly requested adversarial review of a written relay. Write full relays to the file-first transport described in `protocol.md`, preferably `.relays/<RUN_ID>/<cycle-id>/<PHASE>-<ROLE>-<timestamp>.md` (the `<timestamp>` is the real clock time at authoring — see the timestamp policy in `protocol.md`), then print the pointer context — `FROM` and a 3–6 line summary — followed by the exact terminal `RELAY`/`TO`/`CC` hand-off block per `protocol.md`. If the receiver cannot access the path, relay or attach the file contents.

Canonical inline pointer shape:

```text
FROM: pair-1.planner
<3-6 line summary>

RELAY: .relays/<RUN_ID>/<cycle-id>/PLAN-planner-<ts>.md
TO: pair-1.implementer
CC: orchestrator, <run>.orchestrator-reviewer
```

Orchestrator never implements. Downstream pairs follow role contracts:

- Planner audits, designs, plans, reviews, and may issue delegated `DISPATCH IMPL` only when the PLAN dispatch explicitly delegates it and the protocol conditions are met.
- Implementer independently audits, answers design questions, reviews plans, implements only after the exact literal token `DISPATCH IMPL` appears bare, unfenced, un-backticked, alone on its own line, and is addressed to that Implementer in `TO` for relay files under the active run's RELAY_ROOT or directly to that single Implementer in a direct message; then folds review findings. Merge remains separate: only a valid MERGE-GATE field-form authorization with exactly one operational authorization-verdict line, or a bare, own-line `DISPATCH MERGE` addressed to exactly one Implementer, authorizes merge.

Use minimal headers by default, including `FROM` / `TO` / `CC` for orchestrator-tier or multi-pair relays. Add `PARENT_DISPATCH_ID` for pair-Planner implementation dispatches, substantive IMPL action reports, and every relay that participates in a lineage gate; `IN_REPLY_TO` is local/display-only and never a gate input. Add tier/risk-dependent fields when they affect lineage, routing, merge, or verification. When `relay-lint` is available, lint substantive relay files before delegated dispatch, merge, or adapter/CI consumption.

`HUMAN_GATE_REQUIRED: yes` if and only if this relay's requested next transition cannot occur without a fresh operator decision; the operator may answer directly or route the ask onward — the field marks who is being asked, not who must answer. A `yes` names its ask in the annotation (`yes — <the decision>`); a bare `yes` is malformed. Standing downstream gates are named only after `downstream:` or in prose; the field is free-form (`yes|no — <reason>`), not an enum. The field is a predicate re-evaluated at each relay, not a latch.

`DESIGN_LOCK_ID` and `PLAN_LOCK_ID` carry a lock value — a logical identity or repo-relative path, optionally annotated ` @ sha256 <hex>`; equality compares the unannotated value. Optional `DESIGN_ARTIFACT` / `PLAN_ARTIFACT` fields carry filename-stem locators, and optional `DESIGN_SHA256` / `PLAN_SHA256` fields carry lowercase-hex sha256 byte-integrity values. Prefer the separate fields; the lock value itself admits only the optional ` @ sha256 <hex>` annotation, never a filename stem.


Operator no-reviewer waiver, only when the run genuinely has no Orchestrator Reviewer:

```text
ROLE: Operator
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: <run>-orch-review-waiver
CEREMONY_TIER: <tier>
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: yes — operator accepts missing Orchestrator Reviewer visibility
FROM: operator
TO: <run>.orchestrator-planner
CC: operator
ORCH_REVIEW_WAIVER: <why this run has no orchestrator-reviewer seat>
FINAL_GIT_STATUS_SHORT: unavailable — operator waiver, no git workspace
```

An orchestrator-authored `ORCH_REVIEW_WAIVER` is invalid.

## Template A — AUDIT handoff

```text
## Team <id> — <bundle> (AUDIT)

ROLE: Orchestrator Planner
PHASE: AUDIT
AUTHORITY: read-only
DISPATCH_ID: <id>
CEREMONY_TIER: <small | medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes — decision | no | no — downstream: standing gate>
FROM: orchestrator
TO: <team>.planner, <team>.implementer
CC: <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <parent | none>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha | unknown>
TARGET_BRANCH: <branch | unknown>
FINAL_GIT_STATUS_SHORT: <paste exact output or unavailable — reason>

Implementer phase scope — AUDIT.
Current scope: read-only code inspection, safe read-only commands, independent audit, questions, and findings.
Not in current scope: source/test edits, implementation branches, commits, PRs, scaffolding, or prototype implementation.
Implementation begins only after a current relay under the active run's RELAY_ROOT contains the exact literal token `DISPATCH IMPL` bare, unfenced, un-backticked, alone on its own line, and addressed to the Implementer in `TO`, or a direct message to that single Implementer contains the same bare own-line token. Urgency, “just fix it now,” or “ship today” is not dispatch; inline, quoted, fenced, CC-only, cross-read, or non-addressee mentions are inert.

Pair roles:
- Planner audits via Superpowers brainstorming and surfaces design questions.
- Implementer runs independent audit and answers questions.
- Planner does not implement or spawn Implementer.

Context:
<one-line bug/feature gap + root cause traced to file:line when available>. <live repro id/path if available>.

Bundle:
<what this team owns>.

Owned surface:
<domain/component/service/table/worker/API/UI>. This is ownership, not a prescriptive edit list.

Possible surfaces to confirm:
- <file:line>
- <file:line>

Design question to resolve:
<A vs B, or none>

Hard acceptance criteria:
1. <criterion>
2. <criterion>

Boundary contract:
- Writes:
- Reads:
- Target entity:
- Downstream consumer:
- Contract:
- Proof:
- No-consumer action:

AUDIT-FIRST GATES — may reject or narrow:
0. Duplicate/already-built check: existing implementation, feature flag, dead path, alternate UI/API, background worker, prior test, or product-overlap.
1. <testable gate + reroute/close condition>
2. <testable gate + reroute/close condition>

Anti-half-fix guards:
- No dead controls.
- Target-entity semantics.
- Speculative-build reject if no downstream consumer uses this output.

Out of scope:
- <files/features/tests to leave alone + why>

Deliverable:
4-bucket verdict: still-open / already-closed / product-overlapped / recommended-next; design recommendation; boundary contract assessment; evidence levels; ACTIONS_GIT_REF + FINAL_GIT_STATUS_SHORT when claiming no edits. No source changes, no PR. In read-only phases, `ACTIONS: none` requires a final `FINAL_GIT_STATUS_SHORT` block with the exact output of `git status --short`; unexpected files must be listed.
```

## Template B — DESIGN handoff

Use for new-feature / `still-open` work at medium tier or above after audit reconciliation. For tiny/small fixes and `already-closed`/promote-existing work, the audit's design recommendation may serve as the design record if stated explicitly.

```text
## Team <id> — <bundle>: PROCEED TO DESIGN

ROLE: Orchestrator Planner
PHASE: DESIGN
AUTHORITY: design-only for Planner; read-only challenge/answers for Implementer
DISPATCH_ID: <id>
CEREMONY_TIER: <medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes — product/design decision | no | no — downstream: standing gate>
GRILL_REQUIRED: <yes|no>
FROM: orchestrator
TO: <team>.planner
CC: <team>.implementer, <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <audit/reconcile dispatch id>
DESIGN_DOC_ID: <logical design id to create>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha | unknown>
FINAL_GIT_STATUS_SHORT: <paste exact output or unavailable — reason>

Phase scope — DESIGN.
Current scope: Planner uses Superpowers brainstorming; Implementer answers design questions, challenges alternatives with evidence, and flags product-semantics decisions.
Not in current scope: source/test edits, implementation branches, commits, PRs, scaffolding, or prototype implementation.
Implementation begins only after a bare, unfenced, un-backticked own-line `DISPATCH IMPL` token in a later relay whose `TO` addresses the Implementer, or in a direct message to that single Implementer.

Reconciled audit summary:
- Primary bucket:
- Still-open issue / product gap:
- Already-built/product-overlap notes:
- Evidence:

Design questions to resolve with operator/Implementer:
1. <question>
2. <question>

Required design alternatives:
- Option A:
- Option B:
- Tradeoff criteria:

Boundary contract to design around:
- Writes:
- Reads:
- Target entity:
- Downstream consumer:
- Contract:
- Proof:

Out of scope:
- <files/features/tests + why>

Deliverable:
Design lock via Superpowers brainstorming, recorded as DESIGN_DOC_ID, with selected option, rejected alternatives, operator decisions/defaults, boundary contract, acceptance criteria draft, open questions, and evidence levels. Design Q&A may stay inline; the design lock is file-first. After writing the design doc, the pair Planner must send Template I `TO: <team>.implementer`; a design `TO` the orchestrator with the Implementer only on `CC` is not a DESIGN-REVIEW request.
```

## Template C — DESIGN-REVIEW handoff

Use this when a pair Planner has produced a real design doc. The Implementer reviews the design before any design-doc-backed PLAN consumes it.

```text
## Team <id> — <bundle>: DESIGN-REVIEW

ROLE: Downstream Implementer
PHASE: DESIGN-REVIEW
AUTHORITY: review-only
DISPATCH_ID: <id>
CEREMONY_TIER: <medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes — decision | no | no — downstream: standing gate>
FROM: <team>.implementer
TO: <team>.planner
CC: <operator | orchestrator | boundary-adjacent owner.role | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <DESIGN relay dispatch id>
DESIGN_DOC_ID: <logical design id>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
DESIGN_REVIEW_VERDICT: <approve | must-revise | reject-narrow | human-decision-required>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>

Current scope: read-only design review only.
Check: target entity, boundary contract, acceptance criteria draft, rejected alternatives, operator decisions/defaults, and unresolved questions.
Return one verdict: approve / must-revise / reject-narrow / human-decision-required.
FINAL_GIT_STATUS_SHORT: <paste output or unavailable — reason>
```

## Template I — DESIGN request from pair Planner to pair Implementer

This template is the shared asset `design-request-template.md`, shipped adjacent to this skill and to the pair-planner skill. Use the copy in this skill directory; the canonical source lives in the repo's `shared/`.

## DESIGN-completion SITREP — pair Planner reports approved design to orchestrator

Use this after Template C returns `DESIGN_REVIEW_VERDICT: approve`. It keeps the orchestrator engaged through the DESIGN->PLAN boundary without moving the gated design-doc PLAN out of the pair-Planner seat.

```text
## Team <id> — <bundle>: DESIGN COMPLETE / READY FOR PROCEED-TO-PLAN

ROLE: Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: <design-complete id>
CEREMONY_TIER: <medium | large | production-risk>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes — remaining operator decision | no | no — downstream: standing gate>
FROM: <team>.planner
TO: orchestrator
CC: <team>.implementer, <operator | boundary-adjacent owner.role | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <approving DESIGN-REVIEW dispatch id>
DESIGN_DOC_ID: <approved logical design id>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
DESIGN_REVIEW_VERDICT: approve
APPROVING_DESIGN_REVIEW_DISPATCH_ID: <approving DESIGN-REVIEW dispatch id>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>

Design status: approved by pair Implementer; ready for orchestrator sequencing.
Operator decisions/defaults:
- <decision/default or none>
Remaining risks / hard triggers:
- <risk or none>

Requested orchestrator action: reconcile this report, then issue Template D `PROCEED-TO-PLAN` to the pair Planner. Do not emit the gated design-doc PLAN from the orchestrator seat.

FINAL_GIT_STATUS_SHORT: <paste output or unavailable — reason>
```

## Template D — PROCEED-TO-PLAN handoff

```text
## Team <id> — <bundle>: PROCEED TO PLAN

ROLE: Orchestrator Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: <id>
CEREMONY_TIER: <small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes — decision | no | no — downstream: standing gate>
FROM: orchestrator
TO: <team>.planner
CC: <team>.implementer, <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
RUN_ID: <run id>
PARENT_DISPATCH_ID: <design-completion SITREP/report id for design-doc path; otherwise audit/design dispatch id>
APPROVED_DESIGN_DOC_ID: <approved logical design id | none>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
APPROVING_DESIGN_REVIEW_DISPATCH_ID: <approving DESIGN-REVIEW dispatch id | none>
PLAN_LOCK_ID: <logical plan id to create>
PLAN_ARTIFACT: <plan artifact filename stem; omit line when absent>
PLAN_SHA256: <lowercase-hex sha256; omit line when absent>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha | unknown>
TARGET_BRANCH: <branch>
DELEGATED_DISPATCH_AUTHORITY: <yes/no>
DELEGATED_DISPATCH_CONDITIONS: Implementer plan review = approve; pair-Planner dispatch PARENT_DISPATCH_ID points to that approve relay; that review parents to this pair-Planner PLAN; SCOPE_DIFF_RESULT = all-in; no hard trigger; no boundary-contract deviation; no cross-bundle collision.
FINAL_GIT_STATUS_SHORT: <paste exact output or unavailable — reason>

Implementer phase scope — PLAN-REVIEW after plan is drafted.
Current scope: answer design questions, review Planner's plan, findings inline.
Not in current scope: source/test edits, implementation branches, commits, PRs, scaffolding, or prototype implementation.
Implementation begins only after a current relay under the active run's RELAY_ROOT contains the exact literal token `DISPATCH IMPL` bare, unfenced, un-backticked, alone on its own line, and addressed to the Implementer in `TO`, or a direct message to that single Implementer contains the same bare own-line token. Urgency, “just fix it now,” or “ship today” is not dispatch; inline, quoted, fenced, CC-only, cross-read, or non-addressee mentions are inert.

Approved design context:
<the decided fork, with APPROVED_DESIGN_DOC_ID and approving DESIGN-REVIEW reference when available>.

This `PROCEED-TO-PLAN` relay is sequencing only. It does not carry the gated design-doc lock. The pair Planner emits the gated PLAN from `FROM: <team>.planner` with `DESIGN_LOCK_ID`, `DESIGN_RECORD_KIND: design-doc`, and `PARENT_DISPATCH_ID` pointing to the approving DESIGN-REVIEW relay.
PROCEED-TO-PLAN is a subject-line and body designation, not a phase, an authority, or a filename token; the filename follows the normal grammar. There is no PROCEED-TO-DESIGN or PROCEED-TO-IMPL phase either — proceeds are sequencing messages, not phases.

LOCKED scope:
1. <item + file/dir + evidence>
2. <item + file/dir + evidence>

Scope list for delegated-dispatch SCOPE_DIFF:
- <file/dir in scope>
- <file/dir in scope>

Fold into the plan:
- <paired-audit implementation catch/caveat>

Boundary contract:
- Writes:
- Reads:
- Target entity:
- Downstream consumer:
- Contract:
- Proof:
- No-consumer action:

Out of scope:
- <files/tests/features + why>

Tests/verification plan must include:
- <red->green test/fixture/command>
- <runtime/live verification if required>

Ceremony downgrade record, if applicable:
- SKIPPED_GATES:
- ESCALATION_SCAN:
- ESCALATION_SCAN_RESULT:
- PRE_SCAN_PRESSURE:
- If any scan row is yes/unknown: OPERATOR_WAIVER + WAIVED_RISK_ACCEPTANCE only; do not write WHY_DOWNGRADE_IS_SAFE.
- If every scan row is no: WHY_DOWNGRADE_IS_SAFE.

Before writing the gated PLAN, pair Planner must keep the design-doc lock in the pair-Planner seat; do not ask the orchestrator to emit `DESIGN_LOCK_ID`/`DESIGN_RECORD_KIND: design-doc` on the pair's behalf.

Before delegated dispatch, pair Planner must run and lint:
SCOPE_DIFF:
- <each file/dir in locked plan> -> <in | OUT vs Scope list>
SCOPE_DIFF_RESULT: <all-in | deviation-present>
SCOPE_ROW_EVIDENCE: <required when ROW_TRUTH_CHECK is enabled>
- <path> -> <locked-scope evidence>
PARENT_DISPATCH_ID: <the approving PLAN-REVIEW dispatch id on the later dispatch relay>

If `SCOPE_DIFF_RESULT: deviation-present`, do not issue `DISPATCH IMPL`; relay the plan plus justification to the orchestrator and wait.

Deliverable:
Written plan via Superpowers writing-plans + Implementer plan review + SCOPE_DIFF if delegated dispatch is being considered + ACTIONS_GIT_REF + FINAL_GIT_STATUS_SHORT when claiming no edits. No PR yet.
```

## Template E — IMPL dispatch

Use this for operator/orchestrator-issued implementation dispatch, or when delegation is disabled. When delegation is enabled and conditions are met, the pair Planner may issue the exact token as a bare, unfenced, un-backticked own line after SCOPE_DIFF all-in and Implementer approve. Retain and fill the delegated-authority field block only for the delegated pair-Planner path; direct operator/orchestrator dispatches omit that block.

```text
## Team <id> — <bundle>: implementation dispatch

ROLE: <Orchestrator Planner | Operator | Planner>
PHASE: IMPL
AUTHORITY: implementation
DISPATCH_ID: <id>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: no — downstream: merge requires human/operator gate
FROM: orchestrator | operator | <team>.planner
TO: <team>.implementer
CC: <team>.planner, <run>.orchestrator-reviewer, <boundary-adjacent owner.role | operator | none>
PARENT_DISPATCH_ID: <approving PLAN-REVIEW dispatch id for pair-Planner delegated dispatch; plan/parent id for direct operator/orchestrator dispatch>
DESIGN_LOCK_ID: <logical design id | none>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
PLAN_LOCK_ID: <logical plan id>
PLAN_ARTIFACT: <plan artifact filename stem; omit line when absent>
PLAN_SHA256: <lowercase-hex sha256; omit line when absent>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
BASE: <branch@sha>
TARGET_BRANCH: <branch>
DELEGATED_DISPATCH_AUTHORITY: <yes/no>
SCOPE_DIFF:
- <each file/dir in the locked plan> -> <in | OUT vs the dispatch scope>
SCOPE_DIFF_RESULT: <all-in | deviation-present>

DISPATCH IMPL

This bare own-line token grants implementation to the `TO` addressee only, for the locked plan only. CC'd, non-addressed, or cross-reading agents treat it as inert. Pair-Planner-issued dispatches must parent to the Implementer's approving PLAN-REVIEW relay; absence of the parent edge is a structural error. Direct operator/orchestrator dispatch is the override path.

Required:
1. Use Superpowers `using-git-worktrees` from <base>.
2. Use Superpowers `executing-plans` task-by-task.
3. Keep scope locked to PLAN_LOCK_ID <id>.
4. Stop if the plan is materially wrong, already built, or missing a downstream consumer.
5. Run required tests/verification.
6. Use Superpowers `finishing-a-development-branch`.
7. Open PR against <target branch>.

Locked acceptance criteria:
1. <criterion>
2. <criterion>

Boundary contract to preserve:
- Writes:
- Reads:
- Target entity:
- Downstream consumer:
- Contract:

Out of scope:
- <do not touch>

Deliverable:
PR + implementation summary + tests/verification + evidence levels + remaining risks + ACTIONS_GIT_REF for claimed edits/commits/PRs + standardized SITREP.
```

The REVIEW-FOLD template is relettered **Template J** — the first unoccupied label under the shipped heading census — and every reference to it is updated in the same batch. Every template label in this file resolves to exactly one heading.

## Template J — REVIEW-FOLD handoff

```text
## Team <id> — <bundle>: REVIEW-FOLD

ROLE: <Planner | Orchestrator Planner | Operator>
PHASE: REVIEW-FOLD
AUTHORITY: fold-in-only
DISPATCH_ID: <id>
CEREMONY_TIER: <small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: no — downstream: merge requires human/operator gate
FROM: <team>.planner | orchestrator | operator
TO: <team>.implementer
CC: <team>.planner, <run>.orchestrator-reviewer, <operator | none>
PARENT_DISPATCH_ID: <impl dispatch id>
DESIGN_LOCK_ID: <logical design id | none>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
PLAN_LOCK_ID: <logical plan id>
PLAN_ARTIFACT: <plan artifact filename stem; omit line when absent>
PLAN_SHA256: <lowercase-hex sha256; omit line when absent>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
PR: <repo#number>

Current scope: edit the existing PR branch only to address review findings, run tests, and update PR. Before editing, write FOLD_SCOPE listing every file to touch as in/out against these findings; any out row stops the fold and requires a deviation relay.
Not in current scope: broaden scope, change locked design, unrelated cleanup, touching out-of-FOLD_SCOPE files, or a new PR unless directed.

Review source:
<Planner review / Team-of-2 / Team-of-4 / Team-of-5 / human review>

Must fold:
1. <blocker/must-have + evidence + required change>

Optional / Implementer discretion:
1. <should-have/optional + why it may be worth folding>

Do not fold:
- <finding rejected by Planner/human + reason>

Acceptance criteria still locked:
1. <criterion>

Required verification after fold:
- <test/command/live check>

Deliverable:
FOLD_SCOPE:
- <path> -> <in|out>
FOLD_SCOPE_RESULT: <all-in | out-pending-deviation>
FOLD_SCOPE_EVIDENCE: <required when ROW_TRUTH_CHECK is enabled>
- <path> -> <review finding / accepted optional / deviation evidence>
Fold-in summary: Fixed / Not folded + why / Tests / Boundary proof / Remaining risk / ACTIONS_GIT_REF / FINAL_GIT_STATUS_SHORT / Ready for Planner quick check yes-no.
```

## Template F1 — MERGE-GATE handoff

```text
## Team <id> — <bundle>: MERGE GATE

ROLE: Planner
PHASE: MERGE-GATE
AUTHORITY: merge-gated
DISPATCH_ID: <id>
PARENT_DISPATCH_ID: <implementation report or REVIEW-FOLD dispatch id>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: yes — merge/deploy/live-verification judgment
FROM: <team>.planner
TO: <orchestrator | operator>
CC: <team>.implementer, <run>.orchestrator-reviewer, <orchestrator | none>
DESIGN_LOCK_ID: <logical design id | none>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
PLAN_LOCK_ID: <logical plan id>
PLAN_ARTIFACT: <plan artifact filename stem; omit line when absent>
PLAN_SHA256: <lowercase-hex sha256; omit line when absent>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
TARGET_BRANCH: <branch>
PR: <repo#number>

Required before merge:
- Planner verdict: <merge-ready | merge-blocked | ship-ready-after-deploy-verification>
- Blocking findings: <none | waived by human | unresolved>
- Tests: <commands + results + evidence level>
- Scope check: matches locked PLAN_LOCK_ID <id>
- Boundary contract check: <satisfied/not satisfied + evidence>
- Human merge authorization: required; valid IMPL dispatch + green tests is not merge authority
- Merge-authorization relay: <Template K relay id/path or pending>

Recommendation:
<merge-ready | merge-blocked | human-decision-required>
```

## Template F2 — LIVE-VERIFY handoff

```text
## Team <id> — <bundle>: LIVE VERIFY

ROLE: Implementer
PHASE: LIVE-VERIFY
AUTHORITY: live-verify
DISPATCH_ID: <id>
PARENT_DISPATCH_ID: <merge claim or merge-gate dispatch id>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: <E3 | E4>
HUMAN_GATE_REQUIRED: <yes|no — reason>
FROM: <team>.implementer
TO: <team>.planner
CC: <run>.orchestrator-reviewer, <orchestrator | operator | none>
DESIGN_LOCK_ID: <logical design id | none>
PLAN_LOCK_ID: <logical plan id>
BUNDLE_ID: <bundle>
OWNER: <team/agent pair>
REPO: <repo/path>
TARGET_BRANCH: <branch>
PR: <repo#number>

After merge:
- Record merge commit:
- Verify target branch contains merge:
- Verify deployed SHA/container/image includes expected commit before live verification:

Live verification checklist:
1. <user/system action>
2. <canonical state check>
3. <target entity check>
4. <downstream consumer check>
5. <observability/log/event proof>

Completion verdict:
<complete | merged-not-deployed | deployed-not-live-verified | failed-live-verification | human-decision-required>
```

## Template K — merge grant

Use this only after the merge-gate record exists. The operator or orchestrator authors the grant and addresses exactly one Implementer.

```text
## Team <id> — <bundle>: MERGE GRANT

ROLE: <Operator | Orchestrator Planner>
PHASE: MERGE-GATE
AUTHORITY: merge-gated
DISPATCH_ID: <merge-handoff id>
PARENT_DISPATCH_ID: <merge-gate record dispatch id>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: <E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes|no — reason>
FROM: operator | orchestrator | <run>.orchestrator-planner
TO: <team>.implementer
CC: <run>.orchestrator-reviewer

DISPATCH MERGE

This bare own-line token grants merge authority only to the named `TO` Implementer for the parented gate record. Record the resulting merge claim under the same merge-handoff id.
```

## Template G — BOOT relay for `init`

Use this only during the orchestrator-planner `init` directive, after `sprint-doc-setup` has prepared the sprint tree and `.relays/<RUN_ID>/` substrate. Emit one relay per downstream seat and print the file pointer for the operator to hand-relay. This is an onboarding relay, not work authorization.

Boot dispatch ids render as `<run>-boot-<owner>-<role>` with `<owner>` byte-equal to the target address's owner segment; run-prefix stutter (`s1-boot-s1-core-planner`) is accepted. Render the segment one way in the id, `TO`, heading, and `SUBJECT`.

```text
## BOOT — initialize <owner>.<role> for RUN_ID <run>

ROLE: Orchestrator Planner
PHASE: SITREP
AUTHORITY: report-only
DISPATCH_ID: <run>-boot-<owner>-<role>
PARENT_DISPATCH_ID: <run>-boot
RUN_ID: <run>
CEREMONY_TIER: <tiny | small | medium | large | production-risk>
EVIDENCE_TARGET: E1
HUMAN_GATE_REQUIRED: no
FROM: <run>.orchestrator-planner
TO: <pair>.<planner|implementer> | <run>.orchestrator-reviewer
CC: operator
SUBJECT: BOOT — initialize <owner>.<role> for RUN_ID <run>

You are <pair>.<role> or <run>.orchestrator-reviewer for RUN_ID <run>.
Load <pair-planner | pair-implementer | orchestrator-reviewer>.
Sprint root: <repo-relative sprint doc root>
Relay root: .relays/<RUN_ID>/
INDEX: .relays/<RUN_ID>/INDEX.md
Current authority: report-only onboarding. This boot relay grants no AUDIT, DESIGN, PLAN, IMPL, REVIEW-FOLD, MERGE, or LIVE-VERIFY work authority.
Acknowledge identity, loaded skill, reachable relay root, and stand by for the next addressed relay. Orchestrator Reviewer boot grants visibility/review context only, not approval authority.
FINAL_GIT_STATUS_SHORT: <paste git status --short or unavailable — reason>
```

Boot relays deliberately reuse `PHASE: SITREP` so the reviewer-boot relay remains exempt from the orchestrator-review visibility gate. Keep the `DISPATCH_ID` prefix and `SUBJECT: BOOT` distinguishers; a first-class `PHASE: BOOT` is deferred if the SITREP bucket-sharing becomes load-bearing.

## Template H — SITREP to another orchestrator/operator

```text
## SITREP — <team/orchestrator> / <bundle>

ROLE: <Orchestrator Planner | Orchestrator Reviewer | Planner | Implementer>
PHASE: <AUDIT | MERGE-GATE | LIVE-VERIFY | SITREP | RECONCILE>
AUTHORITY: report-only
DISPATCH_ID: <id>
CEREMONY_TIER: <tier>
EVIDENCE_TARGET: <E1 | E2 | E3 | E4>
HUMAN_GATE_REQUIRED: <yes — decision | no | no — downstream: standing gate>
FROM: <sender owner.role | orchestrator | operator>
TO: <recipient owner.role | orchestrator | operator>
CC: <boundary-adjacent owner.role; omit the line when none>
PARENT_DISPATCH_ID:
DESIGN_LOCK_ID: <logical design id | none>
DESIGN_ARTIFACT: <design artifact filename stem; omit line when absent>
DESIGN_SHA256: <lowercase-hex sha256; omit line when absent>
PLAN_LOCK_ID: <logical plan id | none>
PLAN_ARTIFACT: <plan artifact filename stem; omit line when absent>
PLAN_SHA256: <lowercase-hex sha256; omit line when absent>
OWNER:
REPO:
PR:

Claims:
- <claim> — evidence <E0-E4> — source <file/test/PR/live>
Done:
Not done:
Blocked:
Scope drift risk:
Tests / verification:
FINAL_GIT_STATUS_SHORT:
<required for read-only/report-only phases>
Next requested action:
```
